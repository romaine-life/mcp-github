"""Tank session attestation authentication for HTTP MCP requests.

The GitHub MCP server is Tank-bound. Session pods do not call this
server with Kubernetes ServiceAccount identity; their local
``mcp-auth-proxy`` exchanges the pod token with Tank for a short-lived
RS256 JWT whose audience is ``mcp-github-tank``. This module verifies
that attestation and binds the caller identity to a ContextVar for the
tool layer.
"""

from __future__ import annotations

import logging
import os
from contextvars import ContextVar
from dataclasses import dataclass
from typing import Any

import jwt
from jwt import InvalidTokenError, PyJWKClient

log = logging.getLogger(__name__)

TANK_JWKS_URL = os.environ.get(
    "TANK_OPERATOR_JWKS_URL",
    "http://tank-operator.tank-operator.svc.cluster.local/api/internal/jwks",
)
TANK_JWT_ISSUER = os.environ.get("TANK_OPERATOR_JWT_ISSUER", "tank-operator")
TANK_JWT_AUDIENCE = os.environ.get("TANK_OPERATOR_JWT_AUDIENCE", "mcp-github-tank")


@dataclass(frozen=True)
class CallerIdentity:
    email: str
    installation_id: int | None
    is_host: bool
    is_super_admin: bool = False
    session_scope: str = ""
    session_id: str = ""
    pod_name: str = ""

    @classmethod
    def from_claims(cls, claims: dict[str, Any]) -> "CallerIdentity":
        email = _required_string(claims, "owner_email").lower()
        session_scope = _required_string(claims, "session_scope")
        session_id = _required_string(claims, "session_id")
        pod_name = _required_string(claims, "pod_name")
        is_host = _required_bool(claims, "is_host")
        is_super_admin = _required_bool(claims, "is_super_admin")
        installation_id = _optional_int(claims, "github_installation_id")
        if not is_host and installation_id is None:
            raise CallerAuthError("Tank attestation missing GitHub installation")
        return cls(
            email=email,
            installation_id=installation_id,
            is_host=is_host,
            is_super_admin=is_super_admin,
            session_scope=session_scope,
            session_id=session_id,
            pod_name=pod_name,
        )


class CallerAuthError(RuntimeError):
    """Caller identity could not be authenticated for the request."""


CALLER: ContextVar[CallerIdentity | None] = ContextVar("mcp_github_caller", default=None)


def current_caller() -> CallerIdentity | None:
    """Return the caller identity for the in-flight MCP request, or None."""
    return CALLER.get()


class TankJWTAuthenticator:
    def __init__(
        self,
        *,
        jwks_url: str = TANK_JWKS_URL,
        issuer: str = TANK_JWT_ISSUER,
        audience: str = TANK_JWT_AUDIENCE,
        jwks_client: PyJWKClient | None = None,
    ) -> None:
        self._issuer = issuer
        self._audience = audience
        self._jwks_client = jwks_client or PyJWKClient(jwks_url)

    def authenticate(self, authorization: str | None) -> CallerIdentity:
        token = _bearer_token(authorization)
        try:
            signing_key = self._jwks_client.get_signing_key_from_jwt(token).key
            claims = jwt.decode(
                token,
                signing_key,
                algorithms=["RS256"],
                audience=self._audience,
                issuer=self._issuer,
                leeway=10,
                options={"require": ["exp", "iat", "nbf", "iss", "aud", "sub"]},
            )
        except InvalidTokenError as exc:
            raise CallerAuthError("invalid Tank session attestation") from exc
        except Exception as exc:
            log.warning("Tank session attestation verification failed: %s", exc)
            raise CallerAuthError("could not verify Tank session attestation") from exc
        return CallerIdentity.from_claims(claims)


def _bearer_token(authorization: str | None) -> str:
    if authorization is None:
        raise CallerAuthError("missing Authorization header")
    scheme, _, token = authorization.partition(" ")
    if scheme.lower() != "bearer" or not token.strip():
        raise CallerAuthError("missing bearer token")
    return token.strip()


def _required_string(claims: dict[str, Any], name: str) -> str:
    value = claims.get(name)
    if not isinstance(value, str) or not value.strip():
        raise CallerAuthError(f"Tank attestation missing {name}")
    return value.strip()


def _required_bool(claims: dict[str, Any], name: str) -> bool:
    value = claims.get(name)
    if not isinstance(value, bool):
        raise CallerAuthError(f"Tank attestation missing {name}")
    return value


def _optional_int(claims: dict[str, Any], name: str) -> int | None:
    value = claims.get(name)
    if value is None:
        return None
    try:
        parsed = int(value)
    except (TypeError, ValueError) as exc:
        raise CallerAuthError(f"Tank attestation has invalid {name}") from exc
    if parsed <= 0:
        raise CallerAuthError(f"Tank attestation has invalid {name}")
    return parsed
