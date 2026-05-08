"""Stdio entrypoint — same tools as the HTTP server, no JWT layer.

For in-container use where we trust the caller (the local Claude Code agent)
and don't need to validate Entra tokens. Reads only the GitHub App env
vars; everything Entra-related is HTTP-only.

Per-caller routing (stage 3 of #57) is intentionally HTTP-only — there's
exactly one caller in stdio mode, so we always use the host minter via
the MinterPool's no-tank-operator-app code path.
"""
import logging
import os

from mcp.server.fastmcp import FastMCP

from .auth import GitHubAppTokenMinter
from .github_client import GitHubClient
from .minter_pool import MinterPool
from .tools import register_tools


def _req(name: str) -> str:
    v = os.environ.get(name)
    if not v:
        raise RuntimeError(f"missing env var: {name}")
    return v


def main() -> None:
    logging.basicConfig(level=logging.INFO)
    host_minter = GitHubAppTokenMinter(
        _req("GITHUB_APP_ID"),
        _req("GITHUB_APP_INSTALLATION_ID"),
        _req("GITHUB_APP_PRIVATE_KEY"),
    )
    pool = MinterPool(host_minter=host_minter, tank_operator_app_id=None, tank_operator_private_key=None)
    mcp = FastMCP("github-mcp")
    register_tools(mcp, GitHubClient(pool))
    mcp.run()


if __name__ == "__main__":
    main()
