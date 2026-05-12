from __future__ import annotations

import sys
from pathlib import Path
from unittest.mock import MagicMock, patch

import httpx
import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from mcp_github.auth import GitHubAppTokenMinter  # noqa: E402
from mcp_github.caller import CALLER, CallerIdentity  # noqa: E402
from mcp_github.github_client import GitHubClient  # noqa: E402
from mcp_github.minter_pool import MinterPool  # noqa: E402


def _pool() -> MinterPool:
    return MinterPool(
        host_minter=GitHubAppTokenMinter("host-app", "host-install", "host-key"),
        tank_operator_app_id="user-app",
        tank_operator_private_key="user-key",
    )


def _admin() -> CallerIdentity:
    return CallerIdentity(
        email="admin@example.test",
        installation_id=1,
        is_host=False,
        is_super_admin=True,
    )


def _user() -> CallerIdentity:
    return CallerIdentity(
        email="user@example.test",
        installation_id=42,
        is_host=False,
    )


def test_list_user_app_installations_requires_super_admin() -> None:
    client = GitHubClient(_pool())
    token = CALLER.set(_user())
    try:
        with pytest.raises(PermissionError):
            client.list_user_app_installations()
    finally:
        CALLER.reset(token)


def test_list_user_app_installations_delegates_for_super_admin() -> None:
    pool = _pool()
    pool.list_user_app_installations = MagicMock(return_value=[{"id": 42}])  # type: ignore[method-assign]
    client = GitHubClient(pool)
    token = CALLER.set(_admin())
    try:
        assert client.list_user_app_installations() == [{"id": 42}]
    finally:
        CALLER.reset(token)


def test_list_repos_for_installation_uses_requested_installation() -> None:
    pool = _pool()
    minter = pool.user_app_minter(42)
    minter.installation_token = MagicMock(return_value="install-42-token")  # type: ignore[method-assign]
    client = GitHubClient(pool)

    def fake_get(url, *, headers, params, timeout):
        assert headers["Authorization"] == "Bearer install-42-token"
        assert params == {"per_page": 100, "page": 1}
        req = httpx.Request("GET", url)
        return httpx.Response(
            200,
            json={
                "repositories": [
                    {
                        "full_name": "alice/project",
                        "private": True,
                        "default_branch": "main",
                    }
                ]
            },
            request=req,
        )

    token = CALLER.set(_admin())
    try:
        with patch("mcp_github.github_client.httpx.get", side_effect=fake_get):
            rows = client.list_repos_for_installation(42)
    finally:
        CALLER.reset(token)

    assert rows == [
        {
            "installation_id": 42,
            "full_name": "alice/project",
            "private": True,
            "default_branch": "main",
        }
    ]


def test_mint_scoped_token_for_installation_requires_super_admin() -> None:
    client = GitHubClient(_pool())
    token = CALLER.set(_user())
    try:
        with pytest.raises(PermissionError):
            client.mint_scoped_token_for_installation(
                42,
                repositories=["project"],
                permissions={"contents": "read"},
            )
    finally:
        CALLER.reset(token)
