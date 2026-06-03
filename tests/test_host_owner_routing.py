"""Host App installation is resolved per owner so host/super-admin callers
can reach repos under any account the host App is installed on (e.g. the
romaine-life org), not just the single GITHUB_APP_INSTALLATION_ID."""

from __future__ import annotations

import sys
from pathlib import Path
from unittest.mock import MagicMock, patch

import httpx

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from mcp_github.auth import GitHubAppTokenMinter  # noqa: E402
from mcp_github.caller import CallerIdentity  # noqa: E402
from mcp_github.minter_pool import MinterPool  # noqa: E402

INSTALLS = [
    {"id": 111, "account": {"login": "nelsong6"}},
    {"id": 222, "account": {"login": "romaine-life"}},
]


def _pool_with_host_app() -> MinterPool:
    return MinterPool(
        host_minter=GitHubAppTokenMinter("host-app", "host-install", "host-key"),
        tank_operator_app_id="user-app",
        tank_operator_private_key="user-key",
        host_app_id="host-app",
        host_private_key="host-key",
    )


def _pool_no_host_app() -> MinterPool:
    return MinterPool(
        host_minter=GitHubAppTokenMinter("host-app", "host-install", "host-key"),
        tank_operator_app_id="user-app",
        tank_operator_private_key="user-key",
    )


def _host_caller() -> CallerIdentity:
    return CallerIdentity(
        email="host@example.test",
        installation_id=None,
        is_host=True,
        is_super_admin=True,
    )


def _fake_installations_get(url, *, headers, params, timeout):
    req = httpx.Request("GET", url)
    page = params["page"]
    body = INSTALLS if page == 1 else []
    return httpx.Response(200, json=body, request=req)


def test_host_for_owner_resolves_per_owner_and_caches() -> None:
    pool = _pool_with_host_app()
    with patch.object(GitHubAppTokenMinter, "app_jwt", return_value="jwt"), patch(
        "mcp_github.minter_pool.httpx.get", side_effect=_fake_installations_get
    ) as mock_get:
        org = pool.host_for_owner("romaine-life")
        personal = pool.host_for_owner("nelsong6")
        # second lookup is served from cache — no extra /app/installations call
        org_again = pool.host_for_owner("romaine-life")

    assert org._installation_id == "222"
    assert personal._installation_id == "111"
    assert org_again is org
    assert mock_get.call_count == 1  # one listing covered both owners


def test_host_for_owner_unknown_owner_falls_back_to_default() -> None:
    pool = _pool_with_host_app()
    with patch.object(GitHubAppTokenMinter, "app_jwt", return_value="jwt"), patch(
        "mcp_github.minter_pool.httpx.get", side_effect=_fake_installations_get
    ):
        minter = pool.host_for_owner("someone-else")
    assert minter is pool.host


def test_host_for_owner_without_host_app_creds_uses_default() -> None:
    pool = _pool_no_host_app()
    # No host App credentials configured -> never hits the API, returns default.
    with patch("mcp_github.minter_pool.httpx.get") as mock_get:
        minter = pool.host_for_owner("romaine-life")
    assert minter is pool.host
    mock_get.assert_not_called()


def test_for_caller_repo_routes_host_caller_by_owner() -> None:
    pool = _pool_with_host_app()
    pool.host_for_owner = MagicMock(return_value="sentinel-minter")  # type: ignore[method-assign]
    result = pool.for_caller_repo(_host_caller(), ("romaine-life", "infra-bootstrap"))
    assert result == "sentinel-minter"
    pool.host_for_owner.assert_called_once_with("romaine-life")
