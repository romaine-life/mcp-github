"""Cross-installation downscoped fallback (#57 stage 3 follow-up).

Tests cover:
- caller-can-access happy path: user minter used, no fallback
- caller-cannot-access (404): retries with host, primes inaccessible cache
- caller-cannot-access (403 "Resource not accessible"): same retry path
- cached inaccessible repo: host minter used proactively, no extra round-trip
- both user + host fail: original error propagates
- host retry fails (not installed): original error propagates
- mint_clone_token fallback for host-owned repos via non-host caller
- host caller: no fallback machinery involved
"""

from __future__ import annotations

import sys
from pathlib import Path
from unittest.mock import MagicMock, patch

import httpx
import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from mcp_github.auth import GitHubAppTokenMinter  # noqa: E402
from mcp_github.caller import CALLER, CallerIdentity  # noqa: E402
from mcp_github.github_client import (  # noqa: E402
    GitHubClient,
    ScopedTokenRepoMismatch,
    _is_cross_install_failure,
)
from mcp_github.minter_pool import MinterPool  # noqa: E402


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _pool() -> tuple[MinterPool, GitHubAppTokenMinter]:
    host = GitHubAppTokenMinter("host-app", "host-install", "host-key")
    pool = MinterPool(
        host_minter=host,
        tank_operator_app_id="user-app",
        tank_operator_private_key="user-key",
    )
    return pool, host


def _caller(
    *,
    is_host: bool = False,
    installation_id: int | None = 42,
    is_super_admin: bool = False,
) -> CallerIdentity:
    return CallerIdentity(
        email="alice@example.test",
        installation_id=installation_id,
        is_host=is_host,
        is_super_admin=is_super_admin,
    )


def _fake_response(status: int, body: str = "") -> httpx.Response:
    req = httpx.Request("GET", "https://api.github.com/repos/romaine-life/tank-operator")
    return httpx.Response(status_code=status, text=body, request=req)


def _client(pool: MinterPool) -> GitHubClient:
    return GitHubClient(pool)


def _token_for(minter: GitHubAppTokenMinter, tok: str) -> None:
    """Patch minter.installation_token() to return ``tok``."""
    minter.installation_token = MagicMock(return_value=tok)  # type: ignore[method-assign]


# ---------------------------------------------------------------------------
# _is_cross_install_failure
# ---------------------------------------------------------------------------


def test_cross_install_failure_detects_403_with_message() -> None:
    req = httpx.Request("GET", "https://api.github.com/x")
    r = httpx.Response(
        403,
        text='{"message":"Resource not accessible by integration"}',
        request=req,
    )
    assert _is_cross_install_failure(r) is True


def test_cross_install_failure_detects_404() -> None:
    req = httpx.Request("GET", "https://api.github.com/x")
    r = httpx.Response(404, text='{"message":"Not Found"}', request=req)
    assert _is_cross_install_failure(r) is True


def test_cross_install_failure_ignores_200() -> None:
    req = httpx.Request("GET", "https://api.github.com/x")
    assert _is_cross_install_failure(httpx.Response(200, text="{}", request=req)) is False


def test_cross_install_failure_ignores_403_without_integration_message() -> None:
    req = httpx.Request("GET", "https://api.github.com/x")
    r = httpx.Response(403, text='{"message":"Forbidden"}', request=req)
    assert _is_cross_install_failure(r) is False


def test_cross_install_failure_ignores_422() -> None:
    req = httpx.Request("GET", "https://api.github.com/x")
    r = httpx.Response(422, text='{"message":"Unprocessable"}', request=req)
    assert _is_cross_install_failure(r) is False


# ---------------------------------------------------------------------------
# MinterPool.caller_can_serve_repo / record_repo_inaccessible
# ---------------------------------------------------------------------------


def test_pool_optimistic_for_unknown_repo() -> None:
    pool, _ = _pool()
    caller = _caller()
    assert pool.caller_can_serve_repo(caller, "nelsong6", "tank-operator") is True


def test_pool_inaccessible_after_record() -> None:
    pool, _ = _pool()
    caller = _caller()
    pool.record_repo_inaccessible(caller, "nelsong6", "tank-operator")
    assert pool.caller_can_serve_repo(caller, "nelsong6", "tank-operator") is False


def test_pool_inaccessible_is_case_insensitive() -> None:
    pool, _ = _pool()
    caller = _caller()
    pool.record_repo_inaccessible(caller, "NelsonG6", "Tank-Operator")
    assert pool.caller_can_serve_repo(caller, "nelsong6", "tank-operator") is False


def test_pool_inaccessible_expires(monkeypatch: pytest.MonkeyPatch) -> None:
    pool, _ = _pool()
    caller = _caller()
    pool.record_repo_inaccessible(caller, "nelsong6", "tank-operator")
    import time as _time
    monkeypatch.setattr("mcp_github.minter_pool.time", MagicMock(time=lambda: _time.time() + 3700))
    assert pool.caller_can_serve_repo(caller, "nelsong6", "tank-operator") is True


def test_pool_host_caller_always_can_serve() -> None:
    pool, _ = _pool()
    host_caller = _caller(is_host=True)
    pool.record_repo_inaccessible(host_caller, "nelsong6", "tank-operator")
    assert pool.caller_can_serve_repo(host_caller, "nelsong6", "tank-operator") is True


def test_for_caller_repo_returns_host_when_inaccessible() -> None:
    pool, host = _pool()
    caller = _caller(is_super_admin=True)
    pool.record_repo_inaccessible(caller, "nelsong6", "tank-operator")
    minter = pool.for_caller_repo(caller, ("nelsong6", "tank-operator"))
    assert minter is host


def test_for_caller_repo_returns_user_minter_when_accessible() -> None:
    pool, host = _pool()
    caller = _caller()
    minter = pool.for_caller_repo(caller, ("nelsong6", "tank-operator"))
    assert minter is not host
    assert minter._installation_id == "42"


# ---------------------------------------------------------------------------
# GitHubClient._with_fallback — happy path (user install serves the repo)
# ---------------------------------------------------------------------------


def test_get_uses_user_minter_when_repo_accessible() -> None:
    pool, host = _pool()
    caller = _caller()
    client = _client(pool)

    user_minter = pool.for_caller(caller)
    _token_for(user_minter, "user-tok")
    _token_for(host, "host-tok")

    calls: list[str] = []

    def fake_get(url, *, headers, params, timeout):
        calls.append(headers["Authorization"].split()[1])
        req = httpx.Request("GET", url)
        return httpx.Response(200, json={"full_name": "romaine-life/tank-operator"}, request=req)

    token = CALLER.set(caller)
    try:
        with patch("mcp_github.github_client.httpx.get", side_effect=fake_get):
            result = client.get("/repos/romaine-life/tank-operator", repo=("nelsong6", "tank-operator"))
    finally:
        CALLER.reset(token)

    assert result["full_name"] == "romaine-life/tank-operator"
    assert calls == ["user-tok"], "should have used user token, not host"


# ---------------------------------------------------------------------------
# GitHubClient._with_fallback — 404 cross-install failure + fallback
# ---------------------------------------------------------------------------


def test_get_falls_back_to_host_on_404_and_primes_cache() -> None:
    pool, host = _pool()
    caller = _caller(is_super_admin=True)
    client = _client(pool)

    user_minter = pool.for_caller(caller)
    _token_for(user_minter, "user-tok")
    _token_for(host, "host-tok")

    call_tokens: list[str] = []

    def fake_get(url, *, headers, params, timeout):
        tok = headers["Authorization"].split()[1]
        call_tokens.append(tok)
        req = httpx.Request("GET", url)
        if tok == "user-tok":
            return httpx.Response(404, text='{"message":"Not Found"}', request=req)
        return httpx.Response(200, json={"full_name": "romaine-life/tank-operator"}, request=req)

    token = CALLER.set(caller)
    try:
        with patch("mcp_github.github_client.httpx.get", side_effect=fake_get):
            result = client.get("/repos/romaine-life/tank-operator", repo=("nelsong6", "tank-operator"))
    finally:
        CALLER.reset(token)

    assert result["full_name"] == "romaine-life/tank-operator"
    assert call_tokens == ["user-tok", "host-tok"], "should have tried user then host"
    # Cache should now say user install can't serve this repo.
    assert not pool.caller_can_serve_repo(caller, "nelsong6", "tank-operator")


def test_get_falls_back_on_403_resource_not_accessible() -> None:
    pool, host = _pool()
    caller = _caller(is_super_admin=True)
    client = _client(pool)

    user_minter = pool.for_caller(caller)
    _token_for(user_minter, "user-tok")
    _token_for(host, "host-tok")

    call_tokens: list[str] = []

    def fake_get(url, *, headers, params, timeout):
        tok = headers["Authorization"].split()[1]
        call_tokens.append(tok)
        req = httpx.Request("GET", url)
        if tok == "user-tok":
            return httpx.Response(
                403,
                text='{"message":"Resource not accessible by integration"}',
                request=req,
            )
        return httpx.Response(200, json={"ok": True}, request=req)

    token = CALLER.set(caller)
    try:
        with patch("mcp_github.github_client.httpx.get", side_effect=fake_get):
            client.get("/repos/romaine-life/tank-operator", repo=("nelsong6", "tank-operator"))
    finally:
        CALLER.reset(token)

    assert call_tokens == ["user-tok", "host-tok"]
    assert not pool.caller_can_serve_repo(caller, "nelsong6", "tank-operator")


def test_normal_user_does_not_fall_back_to_host_on_404() -> None:
    pool, host = _pool()
    caller = _caller()
    client = _client(pool)

    user_minter = pool.for_caller(caller)
    _token_for(user_minter, "user-tok")
    _token_for(host, "host-tok")

    call_tokens: list[str] = []

    def fake_get(url, *, headers, params, timeout):
        tok = headers["Authorization"].split()[1]
        call_tokens.append(tok)
        req = httpx.Request("GET", url)
        return httpx.Response(404, text='{"message":"Not Found"}', request=req)

    token = CALLER.set(caller)
    try:
        with patch("mcp_github.github_client.httpx.get", side_effect=fake_get):
            with pytest.raises(httpx.HTTPStatusError):
                client.get("/repos/romaine-life/tank-operator", repo=("nelsong6", "tank-operator"))
    finally:
        CALLER.reset(token)

    assert call_tokens == ["user-tok"]
    assert pool.caller_can_serve_repo(caller, "nelsong6", "tank-operator")


def test_subsequent_call_skips_user_install_when_cached() -> None:
    pool, host = _pool()
    caller = _caller(is_super_admin=True)
    client = _client(pool)

    user_minter = pool.for_caller(caller)
    _token_for(user_minter, "user-tok")
    _token_for(host, "host-tok")

    pool.record_repo_inaccessible(caller, "nelsong6", "tank-operator")
    call_tokens: list[str] = []

    def fake_get(url, *, headers, params, timeout):
        tok = headers["Authorization"].split()[1]
        call_tokens.append(tok)
        req = httpx.Request("GET", url)
        return httpx.Response(200, json={"ok": True}, request=req)

    token = CALLER.set(caller)
    try:
        with patch("mcp_github.github_client.httpx.get", side_effect=fake_get):
            client.get("/repos/romaine-life/tank-operator", repo=("nelsong6", "tank-operator"))
    finally:
        CALLER.reset(token)

    assert call_tokens == ["host-tok"], "should have gone straight to host, skipping user"


def test_both_fail_raises_original_404() -> None:
    pool, host = _pool()
    caller = _caller(is_super_admin=True)
    client = _client(pool)

    user_minter = pool.for_caller(caller)
    _token_for(user_minter, "user-tok")
    _token_for(host, "host-tok")

    def fake_get(url, *, headers, params, timeout):
        req = httpx.Request("GET", url)
        return httpx.Response(404, text='{"message":"Not Found"}', request=req)

    token = CALLER.set(caller)
    try:
        with patch("mcp_github.github_client.httpx.get", side_effect=fake_get):
            with pytest.raises(httpx.HTTPStatusError) as exc_info:
                client.get("/repos/romaine-life/tank-operator", repo=("nelsong6", "tank-operator"))
    finally:
        CALLER.reset(token)

    assert exc_info.value.response.status_code == 404
    # Nothing was cached — don't want to poison the cache if both failed
    # (might be a real missing resource, not a cross-install issue).
    assert pool.caller_can_serve_repo(caller, "nelsong6", "tank-operator")


# ---------------------------------------------------------------------------
# Host callers bypass fallback
# ---------------------------------------------------------------------------


def test_host_caller_does_not_trigger_fallback() -> None:
    pool, host = _pool()
    host_caller = _caller(is_host=True)
    client = _client(pool)

    _token_for(host, "host-tok")
    call_count = []

    def fake_get(url, *, headers, params, timeout):
        call_count.append(1)
        req = httpx.Request("GET", url)
        return httpx.Response(404, text='{"message":"Not Found"}', request=req)

    token = CALLER.set(host_caller)
    try:
        with patch("mcp_github.github_client.httpx.get", side_effect=fake_get):
            with pytest.raises(httpx.HTTPStatusError):
                client.get("/repos/romaine-life/tank-operator", repo=("nelsong6", "tank-operator"))
    finally:
        CALLER.reset(token)

    assert len(call_count) == 1, "host caller: exactly one API call, no retry"


def test_host_super_admin_retries_matching_user_installation_on_403() -> None:
    pool, host = _pool()
    caller = _caller(is_host=True, is_super_admin=True)
    client = _client(pool)

    _token_for(host, "host-tok")
    user_minter = pool.user_app_minter(99)
    _token_for(user_minter, "user-99-tok")

    post_tokens: list[str] = []

    def fake_post(url, *, headers, json, timeout):
        tok = headers["Authorization"].split()[1]
        post_tokens.append(tok)
        req = httpx.Request("POST", url)
        if tok == "host-tok":
            return httpx.Response(
                403,
                text='{"message":"Resource not accessible by integration"}',
                request=req,
            )
        return httpx.Response(201, json={"number": 7}, request=req)

    def fake_get(url, *, headers, params, timeout):
        req = httpx.Request("GET", url)
        assert headers["Authorization"].split()[1] == "user-99-tok"
        return httpx.Response(
            200,
            json={"repositories": [{"full_name": "diploidian/void_drifter"}]},
            request=req,
        )

    pool.list_user_app_installations = MagicMock(return_value=[{"id": 99}])  # type: ignore[method-assign]

    token = CALLER.set(caller)
    try:
        with (
            patch("mcp_github.github_client.httpx.post", side_effect=fake_post),
            patch("mcp_github.github_client.httpx.get", side_effect=fake_get),
        ):
            result = client.post(
                "/repos/diploidian/void_drifter/pulls",
                json={"title": "x"},
                repo=("diploidian", "void_drifter"),
            )
    finally:
        CALLER.reset(token)

    assert result["number"] == 7
    assert post_tokens == ["host-tok", "user-99-tok"]


def test_no_repo_kwarg_raises_without_retry() -> None:
    pool, host = _pool()
    caller = _caller()
    client = _client(pool)

    user_minter = pool.for_caller(caller)
    _token_for(user_minter, "user-tok")
    call_count = []

    def fake_get(url, *, headers, params, timeout):
        call_count.append(1)
        req = httpx.Request("GET", url)
        return httpx.Response(404, text='{"message":"Not Found"}', request=req)

    token = CALLER.set(caller)
    try:
        with patch("mcp_github.github_client.httpx.get", side_effect=fake_get):
            with pytest.raises(httpx.HTTPStatusError):
                client.get("/installation/repositories")  # no repo= kwarg
    finally:
        CALLER.reset(token)

    assert len(call_count) == 1, "no repo kwarg → no retry"


# ---------------------------------------------------------------------------
# mint_scoped_token fallback for non-host caller → host-owned repo
# ---------------------------------------------------------------------------


def test_mint_scoped_token_falls_back_to_host_on_422() -> None:
    pool, host = _pool()
    caller = _caller(is_super_admin=True)
    client = _client(pool)

    user_minter = pool.for_caller(caller)
    user_minter.mint_scoped_token = MagicMock(  # type: ignore[method-assign]
        side_effect=httpx.HTTPStatusError(
            "422 Unprocessable",
            request=httpx.Request("POST", "https://api.github.com/app/installations/42/access_tokens"),
            response=httpx.Response(
                422,
                text='{"message":"repositories not accessible"}',
                request=httpx.Request("POST", "https://api.github.com/app/installations/42/access_tokens"),
            ),
        )
    )
    host.mint_scoped_token = MagicMock(  # type: ignore[method-assign]
        return_value=("host-scoped-token", "2026-05-08T21:00:00Z")
    )

    def fake_get(url, *, headers, params, timeout):
        req = httpx.Request("GET", url)
        assert headers["Authorization"].split()[1] == "host-scoped-token"
        return httpx.Response(
            200,
            json={"repositories": [{"full_name": "romaine-life/tank-operator"}]},
            request=req,
        )

    token = CALLER.set(caller)
    try:
        with patch("mcp_github.github_client.httpx.get", side_effect=fake_get):
            result_token, expires = client.mint_scoped_token(
                repositories=["tank-operator"],
                permissions={"contents": "read", "metadata": "read"},
                repos_full=[("romaine-life", "tank-operator")],
            )
    finally:
        CALLER.reset(token)

    assert result_token == "host-scoped-token"
    # Repo should be marked inaccessible in the pool after fallback.
    assert not pool.caller_can_serve_repo(caller, "romaine-life", "tank-operator")


def test_mint_scoped_token_no_fallback_when_host_also_fails() -> None:
    pool, host = _pool()
    caller = _caller(is_super_admin=True)
    client = _client(pool)

    user_minter = pool.for_caller(caller)
    original_exc = httpx.HTTPStatusError(
        "422 Unprocessable",
        request=httpx.Request("POST", "https://api.github.com/app/installations/42/access_tokens"),
        response=httpx.Response(
            422,
            text='{"message":"user repos not accessible"}',
            request=httpx.Request("POST", "https://api.github.com/app/installations/42/access_tokens"),
        ),
    )
    user_minter.mint_scoped_token = MagicMock(side_effect=original_exc)  # type: ignore[method-assign]
    host.mint_scoped_token = MagicMock(  # type: ignore[method-assign]
        side_effect=httpx.HTTPStatusError(
            "422 Unprocessable",
            request=httpx.Request("POST", "https://api.github.com/app/installations/host/access_tokens"),
            response=httpx.Response(
                422,
                text='{"message":"host also cant mint"}',
                request=httpx.Request("POST", "https://api.github.com/app/installations/host/access_tokens"),
            ),
        )
    )
    pool.list_user_app_installations = MagicMock(return_value=[])  # type: ignore[method-assign]

    token = CALLER.set(caller)
    try:
        with pytest.raises(httpx.HTTPStatusError) as exc_info:
            client.mint_scoped_token(
                repositories=["some-other-repo"],
                permissions={"contents": "read", "metadata": "read"},
                repos_full=[("someuser", "some-other-repo")],
            )
    finally:
        CALLER.reset(token)

    # Should raise the original (user install) error, not the host error.
    assert exc_info.value is original_exc


def test_host_super_admin_scoped_token_retries_user_installation_when_host_token_has_wrong_owner() -> None:
    pool, host = _pool()
    caller = _caller(is_host=True, is_super_admin=True)
    client = _client(pool)

    host.mint_scoped_token = MagicMock(  # type: ignore[method-assign]
        return_value=("host-scoped-token", "2026-05-08T21:00:00Z")
    )
    user_minter = pool.user_app_minter(99)
    _token_for(user_minter, "user-install-token")
    user_minter.mint_scoped_token = MagicMock(  # type: ignore[method-assign]
        return_value=("user-scoped-token", "2026-05-08T21:00:00Z")
    )
    pool.list_user_app_installations = MagicMock(return_value=[{"id": 99}])  # type: ignore[method-assign]

    def fake_get(url, *, headers, params, timeout):
        tok = headers["Authorization"].split()[1]
        req = httpx.Request("GET", url)
        if tok == "host-scoped-token":
            return httpx.Response(
                200,
                json={"repositories": [{"full_name": "romaine-life/ambience"}]},
                request=req,
            )
        if tok in {"user-install-token", "user-scoped-token"}:
            return httpx.Response(
                200,
                json={"repositories": [{"full_name": "nelsong6/ambience"}]},
                request=req,
            )
        raise AssertionError(f"unexpected token {tok}")

    token = CALLER.set(caller)
    try:
        with patch("mcp_github.github_client.httpx.get", side_effect=fake_get):
            result_token, expires = client.mint_scoped_token(
                repositories=["ambience"],
                permissions={"contents": "write", "metadata": "read"},
                repos_full=[("nelsong6", "ambience")],
            )
    finally:
        CALLER.reset(token)

    assert result_token == "user-scoped-token"
    assert expires == "2026-05-08T21:00:00Z"
    host.mint_scoped_token.assert_called_once_with(
        repositories=["ambience"],
        permissions={"contents": "write", "metadata": "read"},
    )
    user_minter.mint_scoped_token.assert_called_once_with(
        repositories=["ambience"],
        permissions={"contents": "write", "metadata": "read"},
    )


def test_scoped_token_mismatch_fails_closed_without_super_admin_fallback() -> None:
    pool, _ = _pool()
    caller = _caller(is_super_admin=False)
    client = _client(pool)

    user_minter = pool.for_caller(caller)
    user_minter.mint_scoped_token = MagicMock(  # type: ignore[method-assign]
        return_value=("user-scoped-token", "2026-05-08T21:00:00Z")
    )

    def fake_get(url, *, headers, params, timeout):
        req = httpx.Request("GET", url)
        assert headers["Authorization"].split()[1] == "user-scoped-token"
        return httpx.Response(
            200,
            json={"repositories": [{"full_name": "romaine-life/ambience"}]},
            request=req,
        )

    token = CALLER.set(caller)
    try:
        with patch("mcp_github.github_client.httpx.get", side_effect=fake_get):
            with pytest.raises(ScopedTokenRepoMismatch) as exc_info:
                client.mint_scoped_token(
                    repositories=["ambience"],
                    permissions={"contents": "read", "metadata": "read"},
                    repos_full=[("nelsong6", "ambience")],
                )
    finally:
        CALLER.reset(token)

    assert exc_info.value.missing == ["nelsong6/ambience"]
