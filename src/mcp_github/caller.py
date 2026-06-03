"""Caller identity surface for mcp-github HTTP requests.

Inbound auth resolves to a ``CallerIdentity`` (see ``auth_romaine.py``)
which the tool layer reads from the ``CALLER`` ContextVar to pick the
right GitHub App minter for each request.
"""

from __future__ import annotations

from contextvars import ContextVar
from dataclasses import dataclass


@dataclass(frozen=True)
class CallerIdentity:
    email: str
    installation_id: int | None
    is_host: bool
    is_super_admin: bool = False
    session_scope: str = ""
    session_id: str = ""
    pod_name: str = ""
    service_bearer: str = ""


class CallerAuthError(RuntimeError):
    """Caller identity could not be authenticated for the request."""


CALLER: ContextVar[CallerIdentity | None] = ContextVar("mcp_github_caller", default=None)


def current_caller() -> CallerIdentity | None:
    """Return the caller identity for the in-flight MCP request, or None."""
    return CALLER.get()
