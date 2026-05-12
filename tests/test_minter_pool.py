"""MinterPool selection logic (#57 stage 3)."""

from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from mcp_github.auth import GitHubAppTokenMinter  # noqa: E402
from mcp_github.caller import CallerIdentity  # noqa: E402
from mcp_github.minter_pool import MinterPool  # noqa: E402


def _host_minter() -> GitHubAppTokenMinter:
    """Host minter with sentinel arguments — never actually executed
    against the GitHub API in these tests, only used for identity
    comparison."""
    return GitHubAppTokenMinter(
        app_id="host-app-id",
        installation_id="host-installation-id",
        private_key="-----host-key-----",
    )


def _enabled_pool() -> tuple[MinterPool, GitHubAppTokenMinter]:
    host = _host_minter()
    pool = MinterPool(
        host_minter=host,
        tank_operator_app_id="user-facing-app-id",
        tank_operator_private_key="-----user-facing-key-----",
    )
    return pool, host


def test_unknown_caller_uses_host() -> None:
    pool, host = _enabled_pool()
    assert pool.for_caller(None) is host


def test_host_caller_uses_host_minter() -> None:
    pool, host = _enabled_pool()
    caller = CallerIdentity(email="host@example.test", installation_id=1, is_host=True)
    assert pool.for_caller(caller) is host


def test_non_host_without_installation_falls_back_to_host() -> None:
    """Pre-onboarding: user has logged in but not installed the App yet.
    Don't 401 — fall back to the host's installation, which preserves
    today's behavior so a new user isn't blocked from cluster-side
    lookups before the install flow finishes."""
    pool, host = _enabled_pool()
    caller = CallerIdentity(
        email="newcomer@example.test", installation_id=None, is_host=False
    )
    assert pool.for_caller(caller) is host


def test_non_host_with_installation_uses_user_minter() -> None:
    pool, host = _enabled_pool()
    caller = CallerIdentity(
        email="alice@example.test", installation_id=42, is_host=False
    )
    minter = pool.for_caller(caller)
    assert minter is not host
    # The minter gets the user's installation_id — not the host's — so
    # tokens are minted from the user's tank-operator-app installation.
    assert minter._installation_id == "42"
    assert minter._app_id == "user-facing-app-id"


def test_per_user_minters_are_cached() -> None:
    pool, _ = _enabled_pool()
    caller = CallerIdentity(
        email="alice@example.test", installation_id=42, is_host=False
    )
    a = pool.for_caller(caller)
    b = pool.for_caller(caller)
    assert a is b, "the same installation_id should reuse one minter (token cache)"


def test_distinct_users_get_distinct_minters() -> None:
    pool, _ = _enabled_pool()
    alice = pool.for_caller(
        CallerIdentity(email="alice@example.test", installation_id=42, is_host=False)
    )
    bob = pool.for_caller(
        CallerIdentity(email="bob@example.test", installation_id=99, is_host=False)
    )
    assert alice is not bob
    assert alice._installation_id == "42"
    assert bob._installation_id == "99"


def test_disabled_tank_op_app_falls_back_to_host_for_everyone() -> None:
    """Chart upgrade window or dev cluster: tank-operator-app keys not
    yet synced. The pool degrades to host-minter-for-everyone instead
    of crashing or 401ing the tools — pre-stage-3 behavior, logged at
    boot via ``tank_operator_app_enabled``."""
    host = _host_minter()
    pool = MinterPool(
        host_minter=host,
        tank_operator_app_id=None,
        tank_operator_private_key=None,
    )
    assert pool.tank_operator_app_enabled is False
    caller = CallerIdentity(
        email="alice@example.test", installation_id=42, is_host=False
    )
    assert pool.for_caller(caller) is host


def test_partial_tank_op_keys_treated_as_disabled() -> None:
    """One-of-two keys is broken config. Don't half-enable the path
    (we'd build a bad minter and 401 every per-user call); treat it
    the same as fully disabled."""
    host = _host_minter()
    pool = MinterPool(
        host_minter=host,
        tank_operator_app_id="just-id",
        tank_operator_private_key=None,
    )
    assert pool.tank_operator_app_enabled is False
    caller = CallerIdentity(
        email="alice@example.test", installation_id=42, is_host=False
    )
    assert pool.for_caller(caller) is host


# ---------------------------------------------------------------------------
# Cross-installation repo-access cache
# ---------------------------------------------------------------------------


def test_host_property_returns_host_minter() -> None:
    pool, host = _enabled_pool()
    assert pool.host is host


def test_for_caller_repo_returns_user_minter_when_accessible() -> None:
    pool, host = _enabled_pool()
    caller = CallerIdentity(email="alice@example.test", installation_id=42, is_host=False)
    minter = pool.for_caller_repo(caller, ("nelsong6", "tank-operator"))
    assert minter is not host
    assert minter._installation_id == "42"


def test_for_caller_repo_returns_host_when_cached_inaccessible() -> None:
    pool, host = _enabled_pool()
    caller = CallerIdentity(email="alice@example.test", installation_id=42, is_host=False)
    pool.record_repo_inaccessible(caller, "nelsong6", "tank-operator")
    assert pool.for_caller_repo(caller, ("nelsong6", "tank-operator")) is host


def test_for_caller_repo_host_caller_unaffected_by_inaccessible_cache() -> None:
    """Host callers already use the host minter; the repo cache should not
    interfere and should not create false 'inaccessible' entries."""
    pool, host = _enabled_pool()
    host_caller = CallerIdentity(email="host@example.test", installation_id=1, is_host=True)
    pool.record_repo_inaccessible(host_caller, "nelsong6", "tank-operator")
    assert pool.caller_can_serve_repo(host_caller, "nelsong6", "tank-operator") is True


def test_caller_can_serve_repo_optimistic_for_unknown() -> None:
    pool, _ = _enabled_pool()
    caller = CallerIdentity(email="alice@example.test", installation_id=42, is_host=False)
    assert pool.caller_can_serve_repo(caller, "owner", "repo") is True


def test_caller_can_serve_repo_false_after_record() -> None:
    pool, _ = _enabled_pool()
    caller = CallerIdentity(email="alice@example.test", installation_id=42, is_host=False)
    pool.record_repo_inaccessible(caller, "owner", "repo")
    assert pool.caller_can_serve_repo(caller, "owner", "repo") is False


def test_repo_access_cache_is_case_insensitive() -> None:
    pool, _ = _enabled_pool()
    caller = CallerIdentity(email="alice@example.test", installation_id=42, is_host=False)
    pool.record_repo_inaccessible(caller, "NelsonG6", "Tank-Operator")
    assert pool.caller_can_serve_repo(caller, "nelsong6", "tank-operator") is False


def test_repo_access_cache_scoped_per_installation() -> None:
    """Alice's cache entry does not affect Bob's routing."""
    pool, _ = _enabled_pool()
    alice = CallerIdentity(email="alice@example.test", installation_id=42, is_host=False)
    bob = CallerIdentity(email="bob@example.test", installation_id=99, is_host=False)
    pool.record_repo_inaccessible(alice, "nelsong6", "tank-operator")
    assert pool.caller_can_serve_repo(bob, "nelsong6", "tank-operator") is True
