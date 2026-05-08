"""HTTP entrypoint — streamable-http transport.

Auth is handled by kube-rbac-proxy in front of this process: clients present
a K8s SA token, the proxy validates it via TokenReview + SubjectAccessReview,
and only authorized requests reach this server. Binding loopback so direct
pod-IP:8080 access bypasses nothing — only the proxy can talk to us.

Outgoing GitHub auth used to be one process-wide installation token from
the host's romaine-life-app App. #57 stage 3 changed that: a Starlette
middleware below recovers the source session pod's IP from
``X-Forwarded-For``, asks the orchestrator's
``/api/internal/resolve-caller`` for the caller's email +
installation_id, and stashes it in a ContextVar. The MinterPool reads
that on each tool call to pick the right App installation. The path is
fail-open — anything that goes wrong (no header, orchestrator down,
unknown pod) leaves the contextvar unset and the pool falls back to the
host minter, preserving pre-stage-3 behavior.
"""

import logging
import os
from contextlib import asynccontextmanager

from mcp.server.fastmcp import FastMCP
from mcp.server.transport_security import TransportSecuritySettings
from starlette.applications import Starlette
from starlette.middleware import Middleware
from starlette.middleware.base import BaseHTTPMiddleware
from starlette.requests import Request
from starlette.responses import Response
from starlette.routing import Mount, Route

from .auth import GitHubAppTokenMinter
from .caller import CALLER, CallerResolver, extract_source_pod_ip
from .github_client import GitHubClient
from .minter_pool import MinterPool
from .tools import register_tools

log = logging.getLogger(__name__)


def _req(name: str) -> str:
    v = os.environ.get(name)
    if not v:
        raise RuntimeError(f"missing env var: {name}")
    return v


def _opt(name: str) -> str | None:
    v = os.environ.get(name)
    return v if v else None


class CallerResolutionMiddleware(BaseHTTPMiddleware):
    """Resolve the caller per request and bind it to the ContextVar.

    Sits in front of the FastMCP-mounted streamable-http app so every
    JSON-RPC request gets its caller stamped before any tool runs. Both
    success and failure paths reset the contextvar token so we don't
    leak state into pooled async tasks.
    """

    def __init__(self, app, resolver: CallerResolver) -> None:
        super().__init__(app)
        self._resolver = resolver

    async def dispatch(self, request: Request, call_next):
        forwarded_for = request.headers.get("x-forwarded-for")
        peer_ip = request.client.host if request.client else None
        pod_ip = extract_source_pod_ip(forwarded_for, peer_ip)
        caller = await self._resolver.resolve(pod_ip) if pod_ip else None
        token = CALLER.set(caller)
        try:
            return await call_next(request)
        finally:
            CALLER.reset(token)


def build_app() -> Starlette:
    # The streamable_http transport ships a DNS-rebinding-protection middleware
    # that 421s any Host header not in `allowed_hosts`. Default whitelist only
    # covers localhost, so in-cluster requests to mcp-github.mcp-github.svc get
    # rejected. Disable here — kube-rbac-proxy in front of this process already
    # gates auth via K8s SA tokens, so DNS rebinding can't reach an unauthorized
    # caller anyway. Set streamable_http_path to "/" so requests POSTed to "/"
    # don't hit Starlette's trailing-slash redirect (was 307 → 421 loop).
    mcp = FastMCP(
        "github-mcp",
        stateless_http=True,
        streamable_http_path="/",
        transport_security=TransportSecuritySettings(
            enable_dns_rebinding_protection=False,
        ),
    )

    host_minter = GitHubAppTokenMinter(
        _req("GITHUB_APP_ID"),
        _req("GITHUB_APP_INSTALLATION_ID"),
        _req("GITHUB_APP_PRIVATE_KEY"),
    )
    pool = MinterPool(
        host_minter=host_minter,
        tank_operator_app_id=_opt("TANK_OPERATOR_APP_ID"),
        tank_operator_private_key=_opt("TANK_OPERATOR_APP_PRIVATE_KEY"),
    )
    if pool.tank_operator_app_enabled:
        log.info(
            "per-caller routing active: tank-operator-app keys present, "
            "non-host callers will mint via their installation_id"
        )
    else:
        log.warning(
            "per-caller routing degraded: tank-operator-app keys absent, "
            "all callers fall back to the host romaine-life-app installation"
        )
    register_tools(mcp, GitHubClient(pool))

    async def healthz(_: Request) -> Response:
        return Response("ok", media_type="text/plain")

    resolver = CallerResolver()

    # Starlette's Mount doesn't forward lifespan events to the inner app, so
    # FastMCP's session_manager.run() — which sets up the anyio task group
    # the streamable-http handler depends on — never fires when we mount it.
    # Wire the run() context into the outer app's lifespan ourselves; without
    # this every request 500s with "Task group is not initialized".
    @asynccontextmanager
    async def lifespan(_app: Starlette):
        async with mcp.session_manager.run():
            yield

    return Starlette(
        routes=[
            Route("/healthz", healthz),
            Mount("/", app=mcp.streamable_http_app()),
        ],
        # Middleware applies to every route, including /healthz — that's
        # fine; resolver short-circuits on missing pod IP and probes have
        # no X-Forwarded-For from the kubelet localhost-execed probe.
        middleware=[
            Middleware(CallerResolutionMiddleware, resolver=resolver),
        ],
        lifespan=lifespan,
    )


def main() -> None:
    logging.basicConfig(level=logging.INFO)
    import uvicorn

    port = int(os.environ.get("PORT", "8080"))
    uvicorn.run(build_app(), host="127.0.0.1", port=port)


if __name__ == "__main__":
    main()
