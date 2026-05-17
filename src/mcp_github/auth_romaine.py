"""auth.romaine.life inbound JWT path for mcp-github.

mcp-github is migrating from per-session Tank attestations (signed by
tank-operator with audience=mcp-github-tank, carrying installation_id +
is_host directly) to auth.romaine.life-issued role=service JWTs that
carry only the caller's identity. The routing inputs the tool layer
needs (installation_id, is_host, is_super_admin) are resolved at
request time against tank-operator's
GET /api/internal/github/installation endpoint.

The migration is additive: ``caller.TankJWTAuthenticator`` stays in
place; ``http.CallerAuthMiddleware`` dispatches on the JWT's
unverified ``iss`` claim. When mcp-auth-proxy switches its bearer
source from Tank attestations to auth.romaine.life service JWTs, this
module's path is the only one that fires; Tank attestation
authentication gets retired in a follow-up.

This module mirrors mcp-glimmung's auth_verifier.py shape but tightens
the policy: only role=service tokens are accepted here (mcp-github is
only ever called by service principals in session pods). actor_email
is required for every accepted call.
"""

from __future__ import annotations

import logging
import os
import threading
import time
from dataclasses import dataclass

import httpx
import jwt
from jwt import PyJWKClient

from .caller import CallerAuthError, CallerIdentity

log = logging.getLogger(__name__)

DEFAULT_AUTH_ROMAINE_LIFE_ISSUER = "https://auth.romaine.life"
DEFAULT_AUTH_ROMAINE_LIFE_JWKS_URL = "https://auth.romaine.life/api/auth/jwks"
DEFAULT_TANK_OPERATOR_INSTALLATION_URL = (
    "http://tank-operator.tank-operator.svc.cluster.local"
    "/api/internal/github/installation"
)

# auth.romaine.life service tokens have a 15-min TTL. Cache the
# installation resolution for 5 minutes — short enough that a
# newly-onboarded user gets routed correctly within a few minutes,
# long enough that an active session doesn't pay an extra round-trip
# per tool call.
_INSTALLATION_CACHE_TTL_SECONDS = 300
_LEEWAY_SECONDS = 60


@dataclass(frozen=True)
class _CachedInstallation:
    installation_id: int | None
    is_host: bool
    is_super_admin: bool
    expires_at: float


class AuthRomaineLifeAuthenticator:
    """Verifies auth.romaine.life service JWTs and resolves the caller's
    installation_id by calling tank-operator's installation endpoint.

    Produces a CallerIdentity (the same shape ``TankJWTAuthenticator``
    produces) so the tool layer doesn't have to care which auth path
    fired.
    """

    def __init__(
        self,
        *,
        issuer: str = DEFAULT_AUTH_ROMAINE_LIFE_ISSUER,
        jwks_url: str = DEFAULT_AUTH_ROMAINE_LIFE_JWKS_URL,
        installation_url: str = DEFAULT_TANK_OPERATOR_INSTALLATION_URL,
        leeway: int = _LEEWAY_SECONDS,
        jwks_client: PyJWKClient | None = None,
        http_client: httpx.Client | None = None,
        cache_ttl_seconds: float = _INSTALLATION_CACHE_TTL_SECONDS,
    ) -> None:
        self._issuer = issuer
        self._leeway = leeway
        self._installation_url = installation_url
        self._jwks = jwks_client or PyJWKClient(jwks_url, cache_keys=True)
        self._http = http_client or httpx.Client(timeout=10.0)
        self._cache_ttl = cache_ttl_seconds
        self._cache: dict[str, _CachedInstallation] = {}
        self._lock = threading.Lock()

    def authenticate(self, authorization: str | None) -> CallerIdentity:
        if authorization is None:
            raise CallerAuthError("missing Authorization header")
        scheme, _, token = authorization.partition(" ")
        if scheme.lower() != "bearer" or not token.strip():
            raise CallerAuthError("missing bearer token")
        token = token.strip()

        try:
            signing_key = self._jwks.get_signing_key_from_jwt(token).key
            claims = jwt.decode(
                token,
                signing_key,
                algorithms=["RS256"],
                issuer=self._issuer,
                options={
                    "require": ["exp", "iat", "iss", "role"],
                    # Per-app aud pinning is a separate design decision;
                    # auth.romaine.life-issued tokens today carry
                    # aud=<issuer> which gives no per-app isolation.
                    "verify_aud": False,
                },
                leeway=self._leeway,
            )
        except jwt.InvalidTokenError as exc:
            raise CallerAuthError(f"invalid auth.romaine.life JWT: {exc}") from exc
        except Exception as exc:
            log.warning("auth.romaine.life JWT verification failed: %s", exc)
            raise CallerAuthError("could not verify auth.romaine.life JWT") from exc

        role = (claims.get("role") or "").strip()
        if role != "service":
            # mcp-github is only ever called by service principals in
            # session pods. Reject admin/user tokens — they'd skip the
            # installation routing this server depends on.
            raise CallerAuthError(f"mcp-github requires role=service; caller is role={role!r}")

        actor_email = (claims.get("actor_email") or "").strip().lower()
        if not actor_email:
            raise CallerAuthError("service token missing actor_email")

        sub = str(claims.get("sub") or "")
        session_id, session_scope = _parse_service_sub(sub)

        resolved = self._resolve_installation(actor_email, token)
        if not resolved.is_host and resolved.installation_id is None:
            raise CallerAuthError(
                f"no GitHub App installation registered for {actor_email}"
            )

        return CallerIdentity(
            email=actor_email,
            installation_id=resolved.installation_id,
            is_host=resolved.is_host,
            is_super_admin=resolved.is_super_admin,
            session_scope=session_scope,
            session_id=session_id,
            pod_name="",
        )

    def _resolve_installation(self, email: str, bearer: str) -> _CachedInstallation:
        now = time.time()
        with self._lock:
            cached = self._cache.get(email)
            if cached is not None and cached.expires_at > now:
                return cached

        try:
            r = self._http.get(
                self._installation_url,
                headers={"Authorization": f"Bearer {bearer}"},
            )
        except Exception as exc:
            raise CallerAuthError(
                f"tank-operator installation lookup failed: {exc}"
            ) from exc
        if r.status_code != 200:
            raise CallerAuthError(
                f"tank-operator installation lookup returned {r.status_code}: "
                f"{r.text[:200]}"
            )
        body = r.json()

        installation_id = body.get("installation_id")
        if installation_id is not None:
            installation_id = int(installation_id)
        entry = _CachedInstallation(
            installation_id=installation_id,
            is_host=bool(body.get("is_host", False)),
            is_super_admin=bool(body.get("is_super_admin", False)),
            expires_at=now + self._cache_ttl,
        )
        with self._lock:
            self._cache[email] = entry
        return entry


def _parse_service_sub(sub: str) -> tuple[str, str]:
    """Extract (session_id, session_scope) from a service JWT sub
    claim. The auth-side mints these as ``svc:<consumer>:<stableId>``
    where stableId for per-session consumers (tank) is the session id;
    for pod-stable consumers (mcp-glimmung etc.) it's the consumer
    slug. Returns ("", "") on unparseable input — these fields are
    informational only (logging/audit)."""
    parts = sub.split(":")
    if len(parts) != 3:
        return "", ""
    _prefix, consumer, stable_id = parts
    return stable_id, consumer


def default_authenticator() -> AuthRomaineLifeAuthenticator:
    """Construct from env-driven config. Production uses defaults;
    tests use env vars to point at a stub JWKS server and stub
    tank-operator."""
    return AuthRomaineLifeAuthenticator(
        issuer=os.environ.get("AUTH_ROMAINE_LIFE_ISSUER", DEFAULT_AUTH_ROMAINE_LIFE_ISSUER),
        jwks_url=os.environ.get("AUTH_ROMAINE_LIFE_JWKS_URL", DEFAULT_AUTH_ROMAINE_LIFE_JWKS_URL),
        installation_url=os.environ.get(
            "TANK_OPERATOR_INSTALLATION_URL", DEFAULT_TANK_OPERATOR_INSTALLATION_URL
        ),
    )


def unverified_issuer(authorization: str | None) -> str | None:
    """Peek at the JWT's iss claim WITHOUT verifying. Used by the
    HTTP middleware to dispatch between the Tank-attestation
    authenticator and the auth.romaine.life authenticator.

    Returns None if the header is missing or the token doesn't parse.
    The chosen authenticator does the real verification.
    """
    if not authorization:
        return None
    scheme, _, token = authorization.partition(" ")
    if scheme.lower() != "bearer" or not token.strip():
        return None
    try:
        claims = jwt.decode(token.strip(), options={"verify_signature": False})
    except Exception:
        return None
    iss = claims.get("iss")
    if isinstance(iss, str):
        return iss
    return None


def is_auth_romaine_token(authorization: str | None) -> bool:
    iss = unverified_issuer(authorization)
    if iss is None:
        return False
    # auth.romaine.life issuers always start with https://auth. Tank
    # attestation issuer is the literal "tank-operator". Prefix match
    # so a trailing slash variant still routes correctly.
    return iss.startswith("https://auth.")
