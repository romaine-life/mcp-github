"""HTTP entrypoint for the GitHub MCP server.

Inbound auth: every request must present an auth.romaine.life-issued
role=service JWT in the Authorization header. The middleware verifies
it against the IdP's JWKS, resolves the caller's GitHub App
installation via tank-operator, and binds a ``CallerIdentity`` to the
``CALLER`` ContextVar so the tool layer picks the right minter.
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
from .auth_romaine import (
    AuthRomaineLifeAuthenticator,
    default_authenticator as default_auth_romaine_authenticator,
)
from .caller import CALLER, CallerAuthError
from .github_client import GitHubClient
from .minter_pool import MinterPool
from .tools import register_tools

log = logging.getLogger(__name__)


def _req(name: str) -> str:
    v = os.environ.get(name)
    if not v:
        raise RuntimeError(f"missing env var: {name}")
    return v


class CallerAuthMiddleware(BaseHTTPMiddleware):
    """Authenticate the caller per request and bind it to the ContextVar.

    /healthz bypasses auth so liveness probes work without a token.
    Every other path requires a valid auth.romaine.life service JWT.
    """

    def __init__(self, app, *, authenticator: AuthRomaineLifeAuthenticator) -> None:
        super().__init__(app)
        self._authenticator = authenticator

    async def dispatch(self, request: Request, call_next):
        if request.url.path == "/healthz":
            caller = None
        else:
            try:
                caller = self._authenticator.authenticate(request.headers.get("authorization"))
            except CallerAuthError as exc:
                return Response(f"caller authentication failed: {exc}", status_code=401)
        token = CALLER.set(caller)
        try:
            return await call_next(request)
        finally:
            CALLER.reset(token)


def build_app() -> Starlette:
    # The streamable_http transport ships DNS-rebinding protection that 421s
    # Host values outside its local allowlist. In-cluster requests legitimately
    # target mcp-github.mcp-github.svc, and the auth.romaine.life JWT gate is
    # the application-layer boundary for every non-health request.
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
        tank_operator_app_id=_req("TANK_OPERATOR_APP_ID"),
        tank_operator_private_key=_req("TANK_OPERATOR_APP_PRIVATE_KEY"),
    )
    log.info("GitHub MCP auth active: requires auth.romaine.life role=service JWTs")
    register_tools(mcp, GitHubClient(pool))

    async def healthz(_: Request) -> Response:
        return Response("ok", media_type="text/plain")

    async def delete_session(_: Request) -> Response:
        # MCP streamable-http spec says stateless servers SHOULD return 405
        # for DELETE, but Claude Code's MCP client treats 405 as a fatal error
        # rather than a graceful "no session to terminate" signal. Return 200
        # so the client can reconnect cleanly after a pod restart.
        return Response(status_code=200)

    authenticator = default_auth_romaine_authenticator()

    # Starlette's Mount doesn't forward lifespan events to the inner app, so
    # FastMCP's session_manager.run() never fires when we mount it. Wire the
    # run() context into the outer app's lifespan ourselves.
    @asynccontextmanager
    async def lifespan(_app: Starlette):
        async with mcp.session_manager.run():
            yield

    return Starlette(
        routes=[
            Route("/healthz", healthz),
            Route("/", delete_session, methods=["DELETE"]),
            Mount("/", app=mcp.streamable_http_app()),
        ],
        middleware=[
            Middleware(CallerAuthMiddleware, authenticator=authenticator),
        ],
        lifespan=lifespan,
    )


def main() -> None:
    logging.basicConfig(level=logging.INFO)
    import uvicorn

    port = int(os.environ.get("PORT", "8080"))
    uvicorn.run(build_app(), host="0.0.0.0", port=port)


if __name__ == "__main__":
    main()
