"""HTTP entrypoint — streamable-http transport, no incoming auth.

Auth is handled by kube-rbac-proxy in front of this process: clients present
a K8s SA token, the proxy validates it via TokenReview + SubjectAccessReview,
and only authorized requests reach this server. Binding loopback so direct
pod-IP:8080 access bypasses nothing — only the proxy can talk to us.

Outgoing GitHub auth is the GitHub App installation token, minted by
GitHubAppTokenMinter from env-injected App credentials. Same pattern as
the stdio variant; the diff is just the transport.
"""

import logging
import os
from contextlib import asynccontextmanager

from mcp.server.fastmcp import FastMCP
from mcp.server.transport_security import TransportSecuritySettings
from starlette.applications import Starlette
from starlette.requests import Request
from starlette.responses import Response
from starlette.routing import Mount, Route

from .auth import GitHubAppTokenMinter
from .github_client import GitHubClient
from .tools import register_tools


def _req(name: str) -> str:
    v = os.environ.get(name)
    if not v:
        raise RuntimeError(f"missing env var: {name}")
    return v


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
    gh = GitHubClient(GitHubAppTokenMinter(
        _req("GITHUB_APP_ID"),
        _req("GITHUB_APP_INSTALLATION_ID"),
        _req("GITHUB_APP_PRIVATE_KEY"),
    ))
    register_tools(mcp, gh)

    async def healthz(_: Request) -> Response:
        return Response("ok", media_type="text/plain")

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
        lifespan=lifespan,
    )


def main() -> None:
    logging.basicConfig(level=logging.INFO)
    import uvicorn

    port = int(os.environ.get("PORT", "8080"))
    uvicorn.run(build_app(), host="127.0.0.1", port=port)


if __name__ == "__main__":
    main()
