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

We do *not* attempt cross-installation fallback in this PR (i.e.
"non-host caller touching a host-owned repo, downscope the host
minter to that single repo"). That's a follow-up; in this PR a
non-host caller hitting a host-owned repo gets a 404 from GitHub
because their own installation can't see it. Correct attribution
> ergonomics for v1.
"""

from __future__ import annotations

import logging
from threading import Lock

from .auth import GitHubAppTokenMinter
from .caller import CallerIdentity

log = logging.getLogger(__name__)


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
        self._lock = Lock()

    def for_caller(self, caller: CallerIdentity | None) -> GitHubAppTokenMinter:
        # Fall back to the host minter for any caller we can't route to a
        # tank-operator-app installation: unknown caller (resolve-caller
        # returned None or 404), self-identified host, or a non-host that
        # hasn't installed the user-facing App yet. Same outcome as today,
        # so a broken or unconfigured orchestrator never breaks tools.
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

    @property
    def tank_operator_app_enabled(self) -> bool:
        """Whether the tank-operator-romaine-life App keys are configured.

        ``False`` means the chart deployed without the new ExternalSecret
        entries — the pool degrades to host-minter-for-everyone, matching
        pre-stage-3 behavior. Surfaced for the boot-time log line so the
        operator can tell whether per-caller routing is active.
        """
        return self._tank_op_enabled
