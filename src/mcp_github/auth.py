"""GitHub App installation-token lifecycle.

GitHub's `POST /app/installations/{id}/access_tokens` returns the installation
token and an ISO 8601 `expires_at`. That `expires_at` is the authority clock —
the only durable signal about when this token stops working. This minter:

- Trusts `expires_at` literally, parsed from the response. We never synthesize
  an expiry from `time.time() + <hardcoded TTL>`: that was process-local
  optimism that could outlive the token under clock drift, and it's a
  deletion target rather than a fallback.
- Refreshes proactively while `expires_at - now > _REFRESH_SKEW_SECONDS`, so we
  never serve a token whose authority clock says it's within the skew of
  expiry. The skew also absorbs realistic NTP drift between this pod and
  GitHub.
- Exposes a `force_refresh(stale=...)` method for use by the HTTP layer when a
  request authenticated with the cached token receives a 401. Calls are
  single-flighted under the same lock that guards normal mints: concurrent
  refresh requests collapse into one upstream `/access_tokens` POST per
  invalidation event, even if many in-flight requests all 401 on the same
  stale token in the same instant.
- For scoped tokens minted on behalf of a session caller
  (`mint_scoped_token`), verifies the token is accepted by GitHub before
  returning it. GitHub's `/access_tokens` 201 response can race with the
  validation backend that subsequently sees the token: a freshly minted
  token occasionally 401's against the next request for a short window.
  Verifying before return collapses that window into our process rather
  than leaking it to every downstream `gh api` consumer.
"""

from __future__ import annotations

import logging
import random
import time
from datetime import datetime
from threading import Lock
from typing import Any

import httpx
import jwt

from .metrics import (
    record_token_refresh,
    record_token_warmup,
)


log = logging.getLogger(__name__)


# How close to the GitHub-reported expiry we'll let the cached token run
# before refreshing. Wide enough that bounded NTP drift between this pod
# and GitHub cannot cause us to serve a token GitHub considers expired,
# and wide enough that we never lean on the final minutes of a token's
# life during a sustained burst.
_REFRESH_SKEW_SECONDS = 600.0

# Bounded retry budget for the warmup probe on a freshly minted scoped
# token. Caps the wall-clock spent inside `mint_scoped_token` while
# accommodating GitHub's observed eventual-consistency window. Persistent
# failure raises `GitHubTokenNotReadyError` — we do not return a token
# we couldn't verify.
_WARMUP_MAX_ATTEMPTS = 6
_WARMUP_BASE_BACKOFF_SECONDS = 0.2
_WARMUP_MAX_BACKOFF_SECONDS = 1.5


class GitHubTokenNotReadyError(RuntimeError):
    """A freshly minted scoped token failed verification within the budget.

    Raised by ``mint_scoped_token`` when the warmup probe never stopped
    seeing 401 from GitHub within ``_WARMUP_MAX_ATTEMPTS``. Surfacing
    this as an explicit error — rather than handing the caller a token
    we couldn't confirm — keeps the eventual-consistency window
    observable instead of cascading into every downstream consumer.
    """


def _parse_github_expiry(raw: str) -> float:
    """Parse GitHub's ISO 8601 ``expires_at`` into a unix timestamp.

    GitHub returns e.g. ``"2026-06-10T17:01:42Z"``. We require a
    well-formed, timezone-aware value: a malformed expiry from GitHub
    would be a protocol-level surprise we want to see, not paper over.
    """
    if not raw:
        raise ValueError("expires_at is required from /access_tokens response")
    # `datetime.fromisoformat` accepts trailing "Z" only on 3.11+; we
    # support 3.10+ per pyproject.toml, so normalize first.
    normalized = raw[:-1] + "+00:00" if raw.endswith("Z") else raw
    dt = datetime.fromisoformat(normalized)
    if dt.tzinfo is None:
        raise ValueError(f"expires_at lacks timezone: {raw!r}")
    return dt.timestamp()


class GitHubAppTokenMinter:
    """Mints + caches a GitHub App installation token, keyed off GitHub's
    own ``expires_at`` rather than a locally guessed TTL."""

    def __init__(self, app_id: str, installation_id: str, private_key: str) -> None:
        self._app_id = app_id
        self._installation_id = installation_id
        self._private_key = private_key
        self._lock = Lock()
        self._token: str | None = None
        self._expires_at: float = 0.0

    @staticmethod
    def app_jwt(app_id: str, private_key: str) -> str:
        now = int(time.time())
        return jwt.encode(
            {"iat": now - 60, "exp": now + 540, "iss": app_id},
            private_key,
            algorithm="RS256",
        )

    def installation_token(self) -> str:
        """Return a usable installation token.

        Returns the cached token as long as GitHub's reported expiry is
        more than ``_REFRESH_SKEW_SECONDS`` away. Otherwise mints a fresh
        token under the lock so concurrent callers see exactly one
        upstream POST per refresh event.
        """
        with self._lock:
            if self._token is not None and self._expires_at - time.time() > _REFRESH_SKEW_SECONDS:
                return self._token
            trigger = "cold" if self._token is None else "ttl"
            self._token, self._expires_at = self._fetch(trigger=trigger)
            return self._token

    def force_refresh(self, *, stale: str | None = None) -> str:
        """Invalidate the cached token and mint a fresh one.

        The HTTP layer calls this when a request authenticated with this
        minter's cached token received a 401. ``stale`` is the exact
        token the caller observed the 401 on; if the cache has already
        advanced past that token (a concurrent refresh won the race),
        we return the newer cached token without another upstream mint.
        That collapses N concurrent 401s on the same stale token into
        one `/access_tokens` POST per invalidation event.
        """
        with self._lock:
            if (
                stale is not None
                and self._token is not None
                and self._token != stale
            ):
                return self._token
            self._token = None
            self._expires_at = 0.0
            self._token, self._expires_at = self._fetch(trigger="force_401")
            return self._token

    def _fetch(self, *, trigger: str) -> tuple[str, float]:
        """POST `/access_tokens` and return ``(token, expires_at_unix_ts)``.

        ``trigger`` labels the refresh in metrics so we can see how often
        the cache turns over because of natural TTL vs. an upstream 401.
        """
        app_jwt = self.app_jwt(self._app_id, self._private_key)
        try:
            r = httpx.post(
                f"https://api.github.com/app/installations/{self._installation_id}/access_tokens",
                headers={
                    "Authorization": f"Bearer {app_jwt}",
                    "Accept": "application/vnd.github+json",
                    "X-GitHub-Api-Version": "2022-11-28",
                },
                timeout=10.0,
            )
            r.raise_for_status()
            body = r.json()
            expires_at = _parse_github_expiry(body.get("expires_at", ""))
        except Exception:
            record_token_refresh(trigger=trigger, result="failed")
            raise
        record_token_refresh(trigger=trigger, result="ok")
        return body["token"], expires_at

    def mint_scoped_token(
        self,
        *,
        repositories: list[str] | None = None,
        permissions: dict[str, str] | None = None,
    ) -> tuple[str, str]:
        """One-shot mint of a downscoped installation token with a verified
        post-mint readiness probe.

        Does not touch the long-lived cached token: each scoped mint may
        differ in scope, and these are short-lived per-caller tokens. The
        returned ``(token, expires_at_iso8601)`` is GitHub's original
        response shape — we don't reshape the expiry on the way out so
        callers can record GitHub's authoritative clock verbatim.

        Before returning, probes ``GET /installation/repositories`` with
        the new token until GitHub stops returning 401 or the bounded
        retry budget runs out. The probe permission requirement is
        ``metadata: read`` which the scoped token is guaranteed to have
        — callers can't opt out of metadata via ``permissions=``.
        Persistent 401 raises :class:`GitHubTokenNotReadyError`.

        ``repositories`` are bare repo names ("glimmung", not
        "owner/glimmung") per the GH API contract. ``permissions`` may
        only subset the App's granted permissions; passing a higher
        level than the App holds 422s.
        """
        app_jwt = self.app_jwt(self._app_id, self._private_key)
        payload: dict[str, Any] = {}
        if repositories is not None:
            payload["repositories"] = repositories
        if permissions is not None:
            payload["permissions"] = permissions
        r = httpx.post(
            f"https://api.github.com/app/installations/{self._installation_id}/access_tokens",
            headers={
                "Authorization": f"Bearer {app_jwt}",
                "Accept": "application/vnd.github+json",
                "X-GitHub-Api-Version": "2022-11-28",
            },
            json=payload or None,
            timeout=10.0,
        )
        r.raise_for_status()
        body = r.json()
        token: str = body["token"]
        expires_at: str = body["expires_at"]

        attempts = _warmup_scoped_token(token)
        record_token_warmup(result="ok", attempts=attempts)
        return token, expires_at


def _warmup_scoped_token(token: str) -> int:
    """Probe a freshly minted scoped token until GitHub accepts it.

    Returns the attempt count (1-indexed) that produced the first
    non-401 response. Raises :class:`GitHubTokenNotReadyError` if all
    attempts inside the budget continue to 401.

    The probe is ``GET /installation/repositories?per_page=1``: it
    requires only the ``metadata: read`` permission that every scoped
    token carries unconditionally (`mint_clone_token` always includes
    it), and it returns quickly. We treat *any* non-401 status — even
    a 403 or 404 — as evidence that GitHub validated the token's
    authentication, which is the only failure mode this probe is here
    to detect.
    """
    for attempt in range(1, _WARMUP_MAX_ATTEMPTS + 1):
        r = httpx.get(
            "https://api.github.com/installation/repositories",
            headers={
                "Authorization": f"Bearer {token}",
                "Accept": "application/vnd.github+json",
                "X-GitHub-Api-Version": "2022-11-28",
            },
            params={"per_page": 1},
            timeout=10.0,
        )
        if r.status_code != 401:
            return attempt
        if attempt == _WARMUP_MAX_ATTEMPTS:
            break
        backoff = min(
            _WARMUP_BASE_BACKOFF_SECONDS * (2 ** (attempt - 1)),
            _WARMUP_MAX_BACKOFF_SECONDS,
        )
        # Jitter so multiple concurrent warmups don't all retry on the
        # same heartbeat.
        time.sleep(backoff * (0.5 + random.random()))
    record_token_warmup(result="not_ready", attempts=_WARMUP_MAX_ATTEMPTS)
    raise GitHubTokenNotReadyError(
        f"freshly minted scoped token still 401 after {_WARMUP_MAX_ATTEMPTS} probes"
    )
