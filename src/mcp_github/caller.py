"""Per-request caller resolution for #57 stage 3.

The HTTP MCP server fronts a single GitHub App, but the *App* identity
that every tool call mints from used to be the same for every session
pod (the host's romaine-life-app installation). Stage 3 wires the
caller's email + GitHub installation through to each tool so PRs,
issues, comments, and clone tokens are attributed to the right
installation.

Mechanics:

1. ``kube-rbac-proxy`` validates the inbound K8s SA token (binary
   pass/fail; the SA name itself is shared across all session pods).
2. A Starlette middleware (`http.py`) reads the source pod IP off
   ``X-Forwarded-For`` (kube-rbac-proxy is a thin Go reverse proxy and
   the Python upstream is loopback, so the *last* hop in the chain is
   what reached us — that's the session pod's pod IP).
3. The middleware calls ``GET tank-operator-orchestrator/api/internal/
   resolve-caller?pod_ip=<ip>`` (auth: this pod's projected SA token,
   accepted by the orchestrator's TokenReview gate). The response is
   the email, GitHub App installation_id, and an ``is_host`` flag.
4. The middleware stashes the result in a ``ContextVar`` so each
   ``@mcp.tool()`` body can call ``current_caller()`` without
   threading a request object through every tool signature.

Failure mode: anything that goes wrong in steps 2/3 (no pod IP, the
orchestrator is down, the pod is brand-new and not yet a session row)
leaves the ``ContextVar`` at its ``None`` default. The minter pool
falls back to the host's existing romaine-life-app installation in
that case — i.e. *today's* behavior. So this stage 3 wiring is fail-
open: the friend's session never breaks even if the orchestrator
endpoint is unreachable; it just falls back to the pre-stage-3
attribution.
"""

from __future__ import annotations

import logging
import os
import time
from contextvars import ContextVar
from dataclasses import dataclass
from pathlib import Path
from threading import Lock
from typing import Any

import httpx

log = logging.getLogger(__name__)

ORCHESTRATOR_URL = os.environ.get(
    "ORCHESTRATOR_INTERNAL_URL",
    "http://tank-operator.tank-operator.svc:80",
)
SA_TOKEN_PATH = os.environ.get(
    "SA_TOKEN_PATH",
    "/var/run/secrets/kubernetes.io/serviceaccount/token",
)
RESOLVE_TIMEOUT_SECONDS = float(os.environ.get("CALLER_RESOLVE_TIMEOUT", "3.0"))
# Caller-resolution cache TTL. The orchestrator lookup is cheap, but caching
# keeps a session pod's tool burst from amplifying into N internal-API
# round-trips. Short enough that a fresh GitHub App install (which mutates
# installation_id on the profile) gets picked up within a few minutes
# without a server restart.
CACHE_TTL_SECONDS = float(os.environ.get("CALLER_RESOLVE_TTL", "300"))


@dataclass(frozen=True)
class CallerIdentity:
    email: str
    installation_id: int | None
    is_host: bool

    @classmethod
    def from_dict(cls, body: dict[str, Any]) -> "CallerIdentity":
        return cls(
            email=str(body.get("email", "")).lower(),
            installation_id=(
                int(body["installation_id"])
                if body.get("installation_id") is not None
                else None
            ),
            is_host=bool(body.get("is_host", False)),
        )


CALLER: ContextVar[CallerIdentity | None] = ContextVar("mcp_github_caller", default=None)


def current_caller() -> CallerIdentity | None:
    """Return the caller identity for the in-flight MCP request, or None.

    Tools should treat ``None`` the same as "fall back to the default
    minter" — `MinterPool.for_caller` does that internally.
    """
    return CALLER.get()


class CallerResolver:
    """Calls the orchestrator's ``/api/internal/resolve-caller`` with a
    short TTL cache.

    Runs sync inside the async middleware via ``httpx.Client`` instead
    of ``AsyncClient`` so the resolver doesn't have to own its own
    event-loop bookkeeping; the middleware is the only caller and it
    awaits us via ``run_in_threadpool``-style boundaries are not
    involved (Starlette middleware is async, but a sync httpx call
    inside it is fine — the lookup is a single sub-second round-trip).
    """

    def __init__(
        self,
        orchestrator_url: str = ORCHESTRATOR_URL,
        sa_token_path: str = SA_TOKEN_PATH,
        cache_ttl_seconds: float = CACHE_TTL_SECONDS,
    ) -> None:
        self._url = orchestrator_url.rstrip("/")
        self._sa_token_path = Path(sa_token_path)
        self._cache_ttl = cache_ttl_seconds
        self._cache: dict[str, tuple[CallerIdentity | None, float]] = {}
        self._lock = Lock()

    def _read_sa_token(self) -> str | None:
        try:
            return self._sa_token_path.read_text().strip()
        except OSError:
            log.warning(
                "could not read SA token at %s; caller resolution disabled",
                self._sa_token_path,
            )
            return None

    async def resolve(self, pod_ip: str) -> CallerIdentity | None:
        if not pod_ip:
            return None
        with self._lock:
            cached = self._cache.get(pod_ip)
        if cached is not None:
            value, expires_at = cached
            if expires_at > time.time():
                return value

        token = self._read_sa_token()
        if not token:
            return None

        try:
            async with httpx.AsyncClient(timeout=RESOLVE_TIMEOUT_SECONDS) as client:
                resp = await client.get(
                    f"{self._url}/api/internal/resolve-caller",
                    params={"pod_ip": pod_ip},
                    headers={"Authorization": f"Bearer {token}"},
                )
        except httpx.HTTPError as exc:
            log.warning("orchestrator resolve-caller failed for %s: %s", pod_ip, exc)
            return None

        if resp.status_code == 404:
            value: CallerIdentity | None = None
        elif resp.status_code == 200:
            try:
                value = CallerIdentity.from_dict(resp.json())
            except Exception:  # noqa: BLE001 — bad payload is "no caller", same outcome
                log.warning("malformed resolve-caller body: %s", resp.text[:200])
                value = None
        else:
            log.warning(
                "orchestrator resolve-caller %s for %s: %s",
                resp.status_code,
                pod_ip,
                resp.text[:200],
            )
            return None

        with self._lock:
            self._cache[pod_ip] = (value, time.time() + self._cache_ttl)
        return value


def extract_source_pod_ip(forwarded_for: str | None, peer_ip: str | None) -> str | None:
    """Pick the session pod's IP off the X-Forwarded-For chain.

    kube-rbac-proxy fronts our Python upstream on loopback; it's a Go
    ``httputil.ReverseProxy`` underneath, which appends the immediate
    peer to ``X-Forwarded-For`` before forwarding. So the *last* hop
    is the IP that reached the proxy from outside the pod — i.e. the
    session pod's IP. ``peer_ip`` is the upstream's view (always
    127.0.0.1 in production), kept here for unit-test injection.
    """
    if forwarded_for:
        # Right-most entry was added by our front proxy; trust it.
        last = forwarded_for.split(",")[-1].strip()
        if last:
            return last
    return peer_ip
