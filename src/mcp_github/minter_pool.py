"""Per-caller GitHub App minter selection (#57 stage 3).

Two GitHub Apps live in this cluster (see ``tank-operator/CLAUDE.md`` →
"Two GitHub Apps live alongside each other"):

- ``romaine-life-app`` — the host's dev/automation bot. Already
  installed on host-owned repos. Process-wide minter built from the
  legacy ``GITHUB_APP_*`` env vars; this is the fallback path.
- ``tank-operator-romaine-life`` — the user-facing App. Each
  non-host user installs this on *their* repos via the SPA's
  onboarding flow (#57 stage 2). One App, many installations,
  one ``installation_id`` per user (stored on their Cosmos
  profile row). Loaded from the new ``TANK_OPERATOR_APP_*``
  ExternalSecret entries — the App identity is constant; we
  vary the ``installation_id`` per caller.

The pool's job is to pick the right minter per caller:

- Caller is the host (``is_host=true``) → host minter, no change
  from today.
- Caller is non-host with an installation_id → tank-operator-app
  minter scoped to that user's installation.
- Caller is non-host without an installation_id (no onboarding
  yet, or resolve-caller couldn't identify the pod) → host
  minter. Same as today's behavior; this is the fail-open path
  so a broken orchestrator endpoint or a fresh pod never blocks
  GitHub access.

Cross-installation downscoped fallback (#57 stage 3 follow-up). When a
non-host caller's user installation returns a 404 / 403 "Resource not
accessible by integration" for a repo it doesn't have installed (e.g. a
host-owned private repo), ``GitHubClient._with_fallback`` retries the
same call with the host minter and records the repo as inaccessible in a
TTL'd cache. ``for_caller_repo`` is the proactive side: it checks the
cache first and returns the host minter immediately, avoiding a doomed
round-trip for the remainder of the 30-min cache window.
"""

from __future__ import annotations

import logging
import time
from threading import Lock

from .auth import GitHubAppTokenMinter
from .caller import CallerIdentity

log = logging.getLogger(__name__)

# How long to cache a "this installation cannot serve this repo" result.
# Short enough that a user who *adds* the App to a newly-created repo will
# get correct routing within half an hour without a server restart.
REPO_ACCESS_CACHE_TTL = 1800.0


class MinterPool:
    def __init__(
        self,
        host_minter: GitHubAppTokenMinter,
        tank_operator_app_id: str | None,
        tank_operator_private_key: str | None,
    ) -> None:
        self._host = host_minter
        self._tank_op_app_id = tank_operator_app_id
        self._tank_op_private_key = tank_operator_private_key
        self._tank_op_enabled = bool(
            tank_operator_app_id and tank_operator_private_key
        )
        self._cache: dict[int, GitHubAppTokenMinter] = {}
        # Maps (installation_id, "owner/name") → (can_serve, expires_at).
        # False = confirmed inaccessible; True/absent = optimistic.
        self._repo_access_cache: dict[tuple[int, str], tuple[bool, float]] = {}
        self._lock = Lock()

    @property
    def host(self) -> GitHubAppTokenMinter:
        """The host romaine-life-app minter. Exposed so ``GitHubClient``
        can retry with the host token on cross-installation failures."""
        return self._host

    def for_caller(self, caller: CallerIdentity | None) -> GitHubAppTokenMinter:
        """Return the minter for this caller (caller-scoped, ignoring repo).

        Falls back to the host minter for any caller we can't route to a
        tank-operator-app installation: unknown caller (resolve-caller
        returned None or 404), self-identified host, or a non-host that
        hasn't installed the user-facing App yet. Same outcome as today,
        so a broken or unconfigured orchestrator never blocks tools.
        """
        if (
            caller is None
            or caller.is_host
            or caller.installation_id is None
            or not self._tank_op_enabled
        ):
            return self._host
        with self._lock:
            cached = self._cache.get(caller.installation_id)
            if cached is not None:
                return cached
            minter = GitHubAppTokenMinter(
                self._tank_op_app_id,  # type: ignore[arg-type]
                str(caller.installation_id),
                self._tank_op_private_key,  # type: ignore[arg-type]
            )
            self._cache[caller.installation_id] = minter
            return minter

    def caller_can_serve_repo(
        self, caller: CallerIdentity | None, owner: str, name: str
    ) -> bool:
        """True if the caller's installation *might* be able to serve this repo.

        Optimistic when unknown: ``GitHubClient._with_fallback`` will detect
        an actual failure and update the cache. Returns False only when a
        previous request confirmed the installation can't see this repo and
        the TTL hasn't expired yet. Always True for host/anonymous callers
        since those already route to the host minter.
        """
        if (
            caller is None
            or caller.is_host
            or caller.installation_id is None
            or not self._tank_op_enabled
        ):
            return True  # already routing to host; per-repo cache irrelevant
        key = (caller.installation_id, f"{owner}/{name}".lower())
        with self._lock:
            entry = self._repo_access_cache.get(key)
        if entry is None:
            return True
        can_serve, expires_at = entry
        if expires_at < time.time():
            return True  # TTL expired; optimistically assume accessible again
        return can_serve

    def record_repo_inaccessible(
        self, caller: CallerIdentity, owner: str, name: str
    ) -> None:
        """Cache that this caller's installation cannot serve ``owner/name``.

        Called by ``GitHubClient._with_fallback`` only after the host-minter
        retry succeeded, so we never poison the cache on a real 404
        (missing resource vs. wrong installation).
        """
        if caller.installation_id is None:
            return
        key = (caller.installation_id, f"{owner}/{name}".lower())
        expires_at = time.time() + REPO_ACCESS_CACHE_TTL
        with self._lock:
            self._repo_access_cache[key] = (False, expires_at)
        log.info(
            "cross-install fallback: installation %s cannot serve %s/%s "
            "(caching for %.0fs)",
            caller.installation_id, owner, name, REPO_ACCESS_CACHE_TTL,
        )

    def for_caller_repo(
        self,
        caller: CallerIdentity | None,
        repo: tuple[str, str] | None,
    ) -> GitHubAppTokenMinter:
        """Return the right minter for this caller + repo combination.

        Proactively returns the host minter when the repo-access cache
        confirms the caller's installation can't serve this repo, saving
        a doomed round-trip. Otherwise delegates to ``for_caller``.
        """
        if repo is not None and not self.caller_can_serve_repo(caller, *repo):
            return self._host
        return self.for_caller(caller)

    @property
    def tank_operator_app_enabled(self) -> bool:
        """Whether the tank-operator-romaine-life App keys are configured.

        ``False`` means the chart deployed without the new ExternalSecret
        entries — the pool degrades to host-minter-for-everyone, matching
        pre-stage-3 behavior. Surfaced for the boot-time log line so the
        operator can tell whether per-caller routing is active.
        """
        return self._tank_op_enabled
