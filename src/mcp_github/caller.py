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

Failure mode: anything that goes wrong in steps 2/3 fails the request.
Caller identity is part of the security boundary; callers should not
silently fall back to another installation just because resolution broke.
"""

from __future__ import annotations

import logging
import os
import time
from contextvars import ContextVar
from dataclasses import dataclass
from pathlib import Path
from threading import Lock
from collections.abc import Sequence
from typing import Any

import httpx

log = logging.getLogger(__name__)

ORCHESTRATOR_URL = os.environ.get(
    "ORCHESTRATOR_INTERNAL_URL",
    "http://tank-operator.tank-operator.svc:80",
)
ORCHESTRATOR_URLS = tuple(
    url.strip().rstrip("/")
    for url in os.environ.get("ORCHESTRATOR_INTERNAL_URLS", ORCHESTRATOR_URL).split(",")
    if url.strip()
)
# Audience-scoped token path. The chart projects a token minted with
# audience "tank-operator" so the orchestrator can reject tokens not
# intended for it.
SA_TOKEN_PATH = os.environ.get("TANK_OPERATOR_SA_TOKEN_PATH", "")
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
    is_super_admin: bool = False

    @classmethod
    def from_dict(cls, body: dict[str, Any]) -> "CallerIdentity":
        is_host = bool(body.get("is_host", False))
        return cls(
            email=str(body.get("email", "")).lower(),
            installation_id=(
                int(body["installation_id"])
                if body.get("installation_id") is not None
                else None
            ),
            is_host=is_host,
            is_super_admin=bool(body.get("is_super_admin", False)),
        )


class CallerResolutionError(RuntimeError):
    """Caller identity could not be established for the request."""


CALLER: ContextVar[CallerIdentity | None] = ContextVar("mcp_github_caller", default=None)


def current_caller() -> CallerIdentity | None:
    """Return the caller identity for the in-flight MCP request, or None.

    ``None`` is only expected outside proxied MCP requests, such as
    process-local tests that set the context directly.
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
        orchestrator_urls: Sequence[str] | None = None,
        sa_token_path: str = SA_TOKEN_PATH,
        cache_ttl_seconds: float = CACHE_TTL_SECONDS,
    ) -> None:
        if orchestrator_urls is not None:
            urls = orchestrator_urls
        elif orchestrator_url != ORCHESTRATOR_URL:
            urls = (orchestrator_url,)
        else:
            urls = ORCHESTRATOR_URLS
        if not urls:
            urls = (orchestrator_url,)
        self._urls = tuple(url.rstrip("/") for url in urls if url.rstrip("/"))
        self._sa_token_path = Path(sa_token_path)
        self._cache_ttl = cache_ttl_seconds
        self._cache: dict[str, tuple[CallerIdentity | None, float]] = {}
        self._lock = Lock()

    def _read_sa_token(self) -> str | None:
        try:
            return self._sa_token_path.read_text().strip()
        except OSError:
            log.warning("could not read SA token at %s", self._sa_token_path)
            return None

    async def resolve(self, pod_ip: str) -> CallerIdentity:
        if not pod_ip:
            raise CallerResolutionError("missing source pod IP")
        with self._lock:
            cached = self._cache.get(pod_ip)
        if cached is not None:
            value, expires_at = cached
            if expires_at > time.time():
                return value

        token = self._read_sa_token()
        if not token:
            raise CallerResolutionError(f"could not read SA token at {self._sa_token_path}")

        saw_not_found = False
        for url in self._urls:
            try:
                async with httpx.AsyncClient(timeout=RESOLVE_TIMEOUT_SECONDS) as client:
                    resp = await client.get(
                        f"{url}/api/internal/resolve-caller",
                        params={"pod_ip": pod_ip},
                        headers={"Authorization": f"Bearer {token}"},
                    )
            except httpx.HTTPError as exc:
                log.warning("orchestrator resolve-caller failed for %s via %s: %s", pod_ip, url, exc)
                continue

            if resp.status_code == 404:
                saw_not_found = True
                continue
            if resp.status_code == 200:
                try:
                    value = CallerIdentity.from_dict(resp.json())
                except Exception:  # noqa: BLE001 - payload details are logged below
                    log.warning("malformed resolve-caller body from %s: %s", url, resp.text[:200])
                    raise CallerResolutionError("malformed resolve-caller body") from None
                break

            log.warning(
                "orchestrator resolve-caller %s for %s via %s: %s",
                resp.status_code,
                pod_ip,
                url,
                resp.text[:200],
            )
            raise CallerResolutionError(
                f"orchestrator resolve-caller returned {resp.status_code}"
            )
        else:
            if saw_not_found:
                raise CallerResolutionError(f"no session pod with IP {pod_ip}")
            raise CallerResolutionError("orchestrator resolve-caller request failed")

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
