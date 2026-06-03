"""Per-caller GitHub App minter selection (#57 stage 3).

Two GitHub Apps live in this cluster (see ``tank-operator/CLAUDE.md`` →
"Two GitHub Apps live alongside each other"):

- ``romaine-life-app`` — the host's dev/automation bot. Already
  installed on host-owned repos. Process-wide minter built from the
  ``GITHUB_APP_*`` env vars.
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
- Caller is non-host without an installation_id → unsupported. The
  frontend onboarding wall should prevent this before MCP calls happen.

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
from typing import Any

import httpx

from .auth import GitHubAppTokenMinter
from .caller import CallerIdentity

log = logging.getLogger(__name__)

# How long to cache a "this installation cannot serve this repo" result.
# Short enough that a user who *adds* the App to a newly-created repo will
# get correct routing within half an hour without a server restart.
REPO_ACCESS_CACHE_TTL = 1800.0

# How long to cache the host App's account-login → installation-id map
# (from GET /app/installations) before re-listing. Short enough that
# installing the host App on a new account is picked up without a restart.
HOST_INSTALLATIONS_CACHE_TTL = 600.0


class MinterPool:
    def __init__(
        self,
        host_minter: GitHubAppTokenMinter,
        tank_operator_app_id: str | None,
        tank_operator_private_key: str | None,
        *,
        host_app_id: str | None = None,
        host_private_key: str | None = None,
    ) -> None:
        self._host = host_minter
        # Host App credentials, used to resolve the host installation *per
        # owner* (via GET /app/installations) so host/super-admin callers can
        # reach repos under any account the host App is installed on — e.g.
        # the romaine-life org — instead of only the single installation baked
        # into `_host` (GITHUB_APP_INSTALLATION_ID). When these are unset,
        # `host_for_owner` falls back to `_host`, preserving prior behaviour.
        self._host_app_id = host_app_id
        self._host_private_key = host_private_key
        self._host_owner_minters: dict[str, GitHubAppTokenMinter] = {}
        self._host_installations: dict[str, int] = {}
        self._host_installations_expiry: float = 0.0
        self._tank_op_app_id = tank_operator_app_id
        self._tank_op_private_key = tank_operator_private_key
        if not tank_operator_app_id or not tank_operator_private_key:
            raise RuntimeError("user-facing GitHub App credentials are required")
        self._tank_op_enabled = True
        self._cache: dict[int, GitHubAppTokenMinter] = {}
        # Maps (installation_id, "owner/name") → (can_serve, expires_at).
        # False = confirmed inaccessible; True/absent = optimistic.
        self._repo_access_cache: dict[tuple[int, str], tuple[bool, float]] = {}
        self._lock = Lock()

    @property
    def host(self) -> GitHubAppTokenMinter:
        """The host romaine-life-app minter for the default installation
        (GITHUB_APP_INSTALLATION_ID). Exposed so ``GitHubClient`` can retry
        with the host token on cross-installation failures. Prefer
        ``host_for_owner`` when the target repo's owner is known."""
        return self._host

    def _refresh_host_installations(self) -> None:
        """Re-list the host App's installations into an owner→id map."""
        token = GitHubAppTokenMinter.app_jwt(
            self._host_app_id, self._host_private_key
        )
        mapping: dict[str, int] = {}
        page = 1
        while True:
            r = httpx.get(
                "https://api.github.com/app/installations",
                headers={
                    "Authorization": f"Bearer {token}",
                    "Accept": "application/vnd.github+json",
                    "X-GitHub-Api-Version": "2022-11-28",
                },
                params={"per_page": 100, "page": page},
                timeout=15.0,
            )
            r.raise_for_status()
            body = r.json()
            if not body:
                break
            for inst in body:
                login = (inst.get("account") or {}).get("login")
                if login:
                    mapping[login.lower()] = int(inst["id"])
            if len(body) < 100:
                break
            page += 1
        with self._lock:
            self._host_installations = mapping
            self._host_installations_expiry = time.time() + HOST_INSTALLATIONS_CACHE_TTL

    def host_for_owner(self, owner: str) -> GitHubAppTokenMinter:
        """Return a host-App minter scoped to ``owner``'s installation.

        Resolves the host App's installation for the given account from
        GET /app/installations (cached), so a host/super-admin caller can
        operate on repos under any account the host App is installed on.
        Falls back to the default host minter (``_host``) when host App
        credentials are not configured or the owner has no installation —
        existing single-installation behaviour is preserved.
        """
        if not self._host_app_id or not self._host_private_key:
            return self._host
        key = owner.lower()
        with self._lock:
            cached = self._host_owner_minters.get(key)
            if cached is not None:
                return cached
            fresh = time.time() < self._host_installations_expiry
            known = key in self._host_installations
        if not fresh or not known:
            try:
                self._refresh_host_installations()
            except httpx.HTTPError:
                log.warning("could not list host App installations for %s", owner)
        with self._lock:
            inst_id = self._host_installations.get(key)
            if inst_id is None:
                return self._host
            cached = self._host_owner_minters.get(key)
            if cached is not None:
                return cached
            minter = GitHubAppTokenMinter(
                self._host_app_id, str(inst_id), self._host_private_key
            )
            self._host_owner_minters[key] = minter
            return minter

    def for_caller(self, caller: CallerIdentity | None) -> GitHubAppTokenMinter:
        """Return the minter for this caller (caller-scoped, ignoring repo).

        Uses host auth only for the resolved host caller. Non-host callers
        must have an installation_id.
        """
        if caller is None:
            raise RuntimeError("caller identity is required")
        if caller.is_host:
            return self._host
        if caller.installation_id is None:
            raise RuntimeError("caller has no GitHub installation_id")
        with self._lock:
            cached = self._cache.get(caller.installation_id)
            if cached is not None:
                return cached
            minter = GitHubAppTokenMinter(
                self._tank_op_app_id,
                str(caller.installation_id),
                self._tank_op_private_key,
            )
            self._cache[caller.installation_id] = minter
            return minter

    def caller_can_serve_repo(
        self, caller: CallerIdentity | None, owner: str, name: str
    ) -> bool:
        """True if the caller's installation *might* be able to serve this repo.

        Returns False only when a
        previous request confirmed the installation can't see this repo and
        the TTL hasn't expired yet. Always True for the host caller since it
        already routes to the host minter.
        """
        if caller is None:
            raise RuntimeError("caller identity is required")
        if caller.is_host:
            return True  # already routing to host; per-repo cache irrelevant
        if caller.installation_id is None:
            raise RuntimeError("caller has no GitHub installation_id")
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
        # Host caller targeting a known repo: route to the host App's
        # installation for that repo's owner (e.g. the romaine-life org),
        # not just the single default host installation.
        if caller is not None and caller.is_host and repo is not None:
            return self.host_for_owner(repo[0])
        if (
            repo is not None
            and caller is not None
            and caller.is_super_admin
            and not self.caller_can_serve_repo(caller, *repo)
        ):
            return self._host
        return self.for_caller(caller)

    def user_app_minter(self, installation_id: int) -> GitHubAppTokenMinter:
        """Return a minter for a specific user-facing App installation.

        Caller authorization is intentionally handled by GitHubClient/tool
        methods before this is called. This method only centralizes minter
        caching and config validation.
        """
        with self._lock:
            cached = self._cache.get(installation_id)
            if cached is not None:
                return cached
            minter = GitHubAppTokenMinter(
                self._tank_op_app_id,
                str(installation_id),
                self._tank_op_private_key,
            )
            self._cache[installation_id] = minter
            return minter

    def list_user_app_installations(self) -> list[dict[str, Any]]:
        """List installations for the user-facing Tank GitHub App."""
        token = GitHubAppTokenMinter.app_jwt(
            self._tank_op_app_id,
            self._tank_op_private_key,
        )
        rows: list[dict[str, Any]] = []
        page = 1
        while True:
            r = httpx.get(
                "https://api.github.com/app/installations",
                headers={
                    "Authorization": f"Bearer {token}",
                    "Accept": "application/vnd.github+json",
                    "X-GitHub-Api-Version": "2022-11-28",
                },
                params={"per_page": 100, "page": page},
                timeout=15.0,
            )
            r.raise_for_status()
            body = r.json()
            if not body:
                break
            rows.extend(body)
            if len(body) < 100:
                break
            page += 1
        return rows

    @property
    def tank_operator_app_enabled(self) -> bool:
        """Whether the tank-operator-romaine-life App keys are configured.

        Always true after construction; missing credentials fail startup.
        """
        return self._tank_op_enabled
