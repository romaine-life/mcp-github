"""Installation-token lifecycle contract.

These tests pin the behaviour the 2026-06-10 burst exposed as missing:
the cached installation token treats GitHub's reported `expires_at` as
the authority clock, surfaces a 401 from a cached call by force-refreshing
and retrying exactly once (single-flighted), and verifies a freshly
minted scoped (clone) token is accepted by GitHub before handing it to
the caller.

The tests are deliberately at the contract level — they exercise the
public surface (``installation_token`` / ``force_refresh`` /
``mint_scoped_token`` / ``GitHubClient.get``) rather than internal
helpers, so refactoring the implementation cannot silently regress the
behaviour without breaking these tests.
"""

from __future__ import annotations

import sys
import threading
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest.mock import MagicMock, patch

import httpx
import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from mcp_github import auth as auth_mod  # noqa: E402
from mcp_github.auth import (  # noqa: E402
    GitHubAppTokenMinter,
    GitHubTokenNotReadyError,
    _parse_github_expiry,
    _REFRESH_SKEW_SECONDS,
)
from mcp_github.caller import CALLER, CallerIdentity  # noqa: E402
from mcp_github.github_client import GitHubClient  # noqa: E402
from mcp_github.minter_pool import MinterPool  # noqa: E402


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _iso(seconds_from_now: float) -> str:
    """ISO 8601 expiry string ``seconds_from_now`` in the future, UTC."""
    when = datetime.now(timezone.utc) + timedelta(seconds=seconds_from_now)
    return when.strftime("%Y-%m-%dT%H:%M:%SZ")


def _access_token_response(token: str, expires_in_seconds: float) -> httpx.Response:
    req = httpx.Request(
        "POST",
        "https://api.github.com/app/installations/x/access_tokens",
    )
    return httpx.Response(
        201,
        json={"token": token, "expires_at": _iso(expires_in_seconds)},
        request=req,
    )


def _minter() -> GitHubAppTokenMinter:
    return GitHubAppTokenMinter(
        app_id="app",
        installation_id="install",
        private_key="-----BEGIN FAKE-----",
    )


@pytest.fixture(autouse=True)
def _skip_jwt_signing(monkeypatch: pytest.MonkeyPatch) -> None:
    """Don't actually sign App JWTs in unit tests — none of these tests
    exercise the JWT signature path, and the fixture private key isn't a
    real RSA key."""
    monkeypatch.setattr(
        GitHubAppTokenMinter, "app_jwt", staticmethod(lambda app_id, private_key: "JWT")
    )


# ---------------------------------------------------------------------------
# expires_at parsing
# ---------------------------------------------------------------------------


def test_parse_expiry_handles_trailing_z() -> None:
    raw = "2026-06-10T17:01:42Z"
    ts = _parse_github_expiry(raw)
    # Round-trip back to a datetime in UTC and verify components.
    dt = datetime.fromtimestamp(ts, tz=timezone.utc)
    assert (dt.year, dt.month, dt.day, dt.hour, dt.minute, dt.second) == (
        2026, 6, 10, 17, 1, 42,
    )


def test_parse_expiry_handles_explicit_offset() -> None:
    raw = "2026-06-10T17:01:42+00:00"
    ts = _parse_github_expiry(raw)
    dt = datetime.fromtimestamp(ts, tz=timezone.utc)
    assert dt.hour == 17 and dt.minute == 1


def test_parse_expiry_rejects_empty() -> None:
    with pytest.raises(ValueError, match="expires_at is required"):
        _parse_github_expiry("")


def test_parse_expiry_rejects_naive_value() -> None:
    """A timezone-less ISO string is a contract violation we want to see
    — we'd rather fail loud at mint time than silently anchor the cache
    to local time."""
    with pytest.raises(ValueError, match="lacks timezone"):
        _parse_github_expiry("2026-06-10T17:01:42")


# ---------------------------------------------------------------------------
# installation_token: cache keyed on GitHub's expires_at
# ---------------------------------------------------------------------------


def test_installation_token_caches_on_github_expires_at() -> None:
    """Two calls with a token GitHub says is well in the future → one mint."""
    minter = _minter()
    responses = [_access_token_response("tok-A", expires_in_seconds=3600)]

    def fake_post(*args, **kwargs):
        return responses.pop(0)

    with patch("mcp_github.auth.httpx.post", side_effect=fake_post):
        assert minter.installation_token() == "tok-A"
        # Second call within the cache window: no upstream POST. (If
        # the implementation called POST again, responses would be
        # empty and pop() would raise IndexError.)
        assert minter.installation_token() == "tok-A"


def test_installation_token_refreshes_inside_skew(monkeypatch: pytest.MonkeyPatch) -> None:
    """A token whose GH-reported expiry is inside the skew window
    refreshes on the next call — and the new token is served. The
    locally-hardcoded 3300s TTL is not in play here; the skew is the
    only knob."""
    minter = _minter()
    # First mint: a token with expires_at JUST inside the skew window
    # so the next call must refresh.
    responses = [
        _access_token_response("tok-OLD", expires_in_seconds=_REFRESH_SKEW_SECONDS - 30),
        _access_token_response("tok-NEW", expires_in_seconds=3600),
    ]

    def fake_post(*args, **kwargs):
        return responses.pop(0)

    with patch("mcp_github.auth.httpx.post", side_effect=fake_post):
        assert minter.installation_token() == "tok-OLD"
        assert minter.installation_token() == "tok-NEW"


def test_installation_token_does_not_use_hardcoded_3300_ttl(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Regression guard: if GitHub returns a 25-minute expiry (well
    under the 1h we used to assume), the cache must respect that and
    refresh inside that window. The legacy ``time.time() + 3300`` would
    have kept the token cached for 50 minutes — masking GitHub's clock."""
    minter = _minter()
    responses = [
        _access_token_response("tok-25MIN", expires_in_seconds=25 * 60),
        _access_token_response("tok-NEW", expires_in_seconds=3600),
    ]
    with patch("mcp_github.auth.httpx.post", side_effect=lambda *a, **kw: responses.pop(0)):
        assert minter.installation_token() == "tok-25MIN"
        # Advance clock past GH's reported expiry minus the skew.
        real_time = auth_mod.time.time()
        monkeypatch.setattr(
            "mcp_github.auth.time.time",
            lambda: real_time + (25 * 60 - _REFRESH_SKEW_SECONDS + 1),
        )
        assert minter.installation_token() == "tok-NEW"


# ---------------------------------------------------------------------------
# force_refresh: single-flight on 401, race-safe vs concurrent winners
# ---------------------------------------------------------------------------


def test_force_refresh_mints_a_fresh_token() -> None:
    minter = _minter()
    responses = [
        _access_token_response("tok-A", expires_in_seconds=3600),
        _access_token_response("tok-B", expires_in_seconds=3600),
    ]
    with patch("mcp_github.auth.httpx.post", side_effect=lambda *a, **kw: responses.pop(0)):
        first = minter.installation_token()
        second = minter.force_refresh(stale=first)
    assert first == "tok-A"
    assert second == "tok-B"


def test_force_refresh_collapses_concurrent_stale_callers() -> None:
    """Two concurrent callers both observe a 401 on the same cached
    token and both call ``force_refresh(stale=tok-A)``. Exactly one
    ``/access_tokens`` POST should fire, and both callers should get
    the same new token."""
    minter = _minter()
    responses = [
        _access_token_response("tok-A", expires_in_seconds=3600),
        _access_token_response("tok-B", expires_in_seconds=3600),
    ]
    call_count = {"n": 0}

    def fake_post(*args, **kwargs):
        call_count["n"] += 1
        return responses.pop(0)

    with patch("mcp_github.auth.httpx.post", side_effect=fake_post):
        first = minter.installation_token()
        assert first == "tok-A"

        results: list[str] = []
        barrier = threading.Barrier(2)

        def refresh():
            barrier.wait()
            results.append(minter.force_refresh(stale=first))

        t1 = threading.Thread(target=refresh)
        t2 = threading.Thread(target=refresh)
        t1.start(); t2.start()
        t1.join(); t2.join()

    assert results == ["tok-B", "tok-B"]
    # Two installs of `tok-A`-then-`tok-B` were prepared. After the
    # initial install + one refresh, call_count should be 2 — both
    # concurrent refresh() calls produced one mint between them.
    assert call_count["n"] == 2, (
        "force_refresh must collapse concurrent stale-token callers into one mint"
    )


def test_force_refresh_noop_when_cache_already_advanced() -> None:
    """If the cache already advanced past ``stale``, force_refresh
    returns the cached newer token without minting again."""
    minter = _minter()
    responses = [
        _access_token_response("tok-A", expires_in_seconds=3600),
        _access_token_response("tok-B", expires_in_seconds=3600),
    ]
    with patch("mcp_github.auth.httpx.post", side_effect=lambda *a, **kw: responses.pop(0)):
        first = minter.installation_token()       # tok-A
        minter.force_refresh(stale=first)         # mints tok-B
        # A late caller who only ever saw tok-A asks for refresh again;
        # cache is already at tok-B. Should NOT mint a third token.
        out = minter.force_refresh(stale=first)
    assert out == "tok-B"
    assert responses == [], "exactly two mints happened, no third"


# ---------------------------------------------------------------------------
# Retry-on-401 in GitHubClient
# ---------------------------------------------------------------------------


def _pool_and_caller() -> tuple[MinterPool, GitHubAppTokenMinter, CallerIdentity]:
    host = _minter()
    pool = MinterPool(
        host_minter=host,
        tank_operator_app_id="user-app",
        tank_operator_private_key="user-key",
    )
    caller = CallerIdentity(email="host@example.test", installation_id=1, is_host=True)
    return pool, host, caller


def test_get_force_refreshes_and_retries_once_on_401() -> None:
    pool, host, caller = _pool_and_caller()
    # First installation_token() → "tok-stale". After force_refresh, →
    # "tok-fresh".
    tokens = iter(["tok-stale", "tok-fresh"])
    host.installation_token = MagicMock(side_effect=lambda: next(tokens))  # type: ignore[method-assign]
    host.force_refresh = MagicMock(return_value="tok-fresh")  # type: ignore[method-assign]
    client = GitHubClient(pool)

    seen_tokens: list[str] = []

    def fake_get(url, *, headers, params, timeout):
        tok = headers["Authorization"].split()[1]
        seen_tokens.append(tok)
        req = httpx.Request("GET", url)
        if tok == "tok-stale":
            return httpx.Response(401, text='{"message":"Requires authentication"}', request=req)
        return httpx.Response(200, json={"ok": True}, request=req)

    token = CALLER.set(caller)
    try:
        with patch("mcp_github.github_client.httpx.get", side_effect=fake_get):
            result = client.get("/repos/x/y", repo=("x", "y"))
    finally:
        CALLER.reset(token)

    assert result == {"ok": True}
    assert seen_tokens == ["tok-stale", "tok-fresh"]
    host.force_refresh.assert_called_once_with(stale="tok-stale")


def test_retry_uses_fresh_token_on_401_for_all_verbs() -> None:
    """The retry path must apply to every cached-token verb, not just GET."""
    pool, host, caller = _pool_and_caller()
    client = GitHubClient(pool)

    for verb, method in (
        ("PATCH", client.patch),
        ("DELETE", client.delete),
        ("POST", client.post),
        ("PUT", client.put),
    ):
        tokens = iter(["tok-stale", "tok-fresh"])
        host.installation_token = MagicMock(side_effect=lambda: next(tokens))  # type: ignore[method-assign]
        host.force_refresh = MagicMock(return_value="tok-fresh")  # type: ignore[method-assign]

        seen: list[str] = []

        def handler(*args, headers, **kw):
            # httpx.delete/patch/post/put pass (url,) positionally;
            # httpx.request passes (method, url) positionally. Tolerate both.
            url = args[-1]
            tok = headers["Authorization"].split()[1]
            seen.append(tok)
            req = httpx.Request(verb, url)
            if tok == "tok-stale":
                return httpx.Response(401, text='{"message":"x"}', request=req)
            return httpx.Response(200, json={"ok": True}, request=req)

        token = CALLER.set(caller)
        try:
            with (
                patch("mcp_github.github_client.httpx.patch", side_effect=handler),
                patch("mcp_github.github_client.httpx.delete", side_effect=handler),
                patch("mcp_github.github_client.httpx.post", side_effect=handler),
                patch("mcp_github.github_client.httpx.put", side_effect=handler),
                patch("mcp_github.github_client.httpx.request", side_effect=handler),
            ):
                result = method("/x", repo=("x", "y"))
        finally:
            CALLER.reset(token)
        assert result == {"ok": True}, f"verb {verb} should retry+succeed after 401"
        assert seen == ["tok-stale", "tok-fresh"], (
            f"verb {verb} should retry exactly once with the fresh token"
        )


def test_persistent_401_surfaces_after_one_retry() -> None:
    """A second 401 on the refreshed token surfaces as the response —
    we don't loop forever, and we don't mask real auth breakage."""
    pool, host, caller = _pool_and_caller()
    host.installation_token = MagicMock(return_value="tok-stale")  # type: ignore[method-assign]
    host.force_refresh = MagicMock(return_value="tok-fresh-but-also-bad")  # type: ignore[method-assign]
    client = GitHubClient(pool)

    call_count = {"n": 0}

    def fake_get(url, *, headers, params, timeout):
        call_count["n"] += 1
        req = httpx.Request("GET", url)
        return httpx.Response(401, text='{"message":"nope"}', request=req)

    token = CALLER.set(caller)
    try:
        with patch("mcp_github.github_client.httpx.get", side_effect=fake_get):
            with pytest.raises(httpx.HTTPStatusError) as exc:
                client.get("/repos/x/y", repo=("x", "y"))
    finally:
        CALLER.reset(token)

    assert exc.value.response.status_code == 401
    assert call_count["n"] == 2, "exactly one retry on 401, no loop"
    host.force_refresh.assert_called_once_with(stale="tok-stale")


def test_no_retry_when_first_response_is_not_401() -> None:
    """A 200/403/404/etc. on the first try must not trigger force_refresh."""
    pool, host, caller = _pool_and_caller()
    host.installation_token = MagicMock(return_value="tok-ok")  # type: ignore[method-assign]
    host.force_refresh = MagicMock(side_effect=AssertionError("should not refresh"))  # type: ignore[method-assign]
    client = GitHubClient(pool)

    def fake_get(url, *, headers, params, timeout):
        req = httpx.Request("GET", url)
        return httpx.Response(200, json={"ok": True}, request=req)

    token = CALLER.set(caller)
    try:
        with patch("mcp_github.github_client.httpx.get", side_effect=fake_get):
            result = client.get("/repos/x/y", repo=("x", "y"))
    finally:
        CALLER.reset(token)

    assert result == {"ok": True}
    host.force_refresh.assert_not_called()


def test_401_retry_composes_with_cross_install_fallback() -> None:
    """A 401 retry that resolves to a 403 'not accessible by integration'
    still triggers the existing cross-install fallback. The two recovery
    layers must compose without one masking the other."""
    pool, host = (
        MinterPool(
            host_minter=_minter(),
            tank_operator_app_id="user-app",
            tank_operator_private_key="user-key",
        ),
        None,
    )
    pool, _ = pool, host
    host = pool.host
    user_caller = CallerIdentity(
        email="alice@example.test",
        installation_id=42,
        is_host=False,
        is_super_admin=True,
    )
    user_minter = pool.for_caller(user_caller)
    user_minter.installation_token = MagicMock(return_value="user-stale")  # type: ignore[method-assign]
    user_minter.force_refresh = MagicMock(return_value="user-fresh")  # type: ignore[method-assign]
    host.installation_token = MagicMock(return_value="host-tok")  # type: ignore[method-assign]

    pool.host_for_owner = MagicMock(return_value=host)  # type: ignore[method-assign]

    client = GitHubClient(pool)
    seen: list[str] = []

    def fake_get(url, *, headers, params, timeout):
        tok = headers["Authorization"].split()[1]
        seen.append(tok)
        req = httpx.Request("GET", url)
        if tok == "user-stale":
            return httpx.Response(401, text='{"message":"x"}', request=req)
        if tok == "user-fresh":
            # After refresh the user install still can't see this repo
            # — that's a cross-install issue, not an auth issue.
            return httpx.Response(
                403,
                text='{"message":"Resource not accessible by integration"}',
                request=req,
            )
        return httpx.Response(200, json={"ok": True}, request=req)

    token = CALLER.set(user_caller)
    try:
        with patch("mcp_github.github_client.httpx.get", side_effect=fake_get):
            result = client.get("/repos/x/y", repo=("x", "y"))
    finally:
        CALLER.reset(token)

    assert result == {"ok": True}
    assert seen == ["user-stale", "user-fresh", "host-tok"], (
        "expected: 401 on stale → refresh+retry → 403 → host fallback succeeds"
    )


# ---------------------------------------------------------------------------
# mint_scoped_token: post-mint readiness probe
# ---------------------------------------------------------------------------


def test_mint_scoped_token_warmup_succeeds_on_first_probe() -> None:
    minter = _minter()
    post_calls: list[str] = []

    def fake_post(url, *, headers, json, timeout):
        post_calls.append(url)
        req = httpx.Request("POST", url)
        return httpx.Response(
            201,
            json={"token": "scoped-tok", "expires_at": _iso(3600)},
            request=req,
        )

    probe_calls: list[str] = []

    def fake_get(url, *, headers, params, timeout):
        probe_calls.append(headers["Authorization"].split()[1])
        req = httpx.Request("GET", url)
        return httpx.Response(200, json={"repositories": []}, request=req)

    with (
        patch("mcp_github.auth.httpx.post", side_effect=fake_post),
        patch("mcp_github.auth.httpx.get", side_effect=fake_get),
    ):
        token, expires_at = minter.mint_scoped_token(
            repositories=["foo"],
            permissions={"contents": "read", "metadata": "read"},
        )

    assert token == "scoped-tok"
    assert expires_at  # ISO string preserved verbatim
    assert probe_calls == ["scoped-tok"], "must probe the new token before returning"


def test_mint_scoped_token_warmup_retries_through_401_then_succeeds(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The exact case the 2026-06-10 incident hit on the `mint_clone_token`
    surface: GH returns 201 but the new token 401s on the first probe.
    Our warmup retries until GH stops 401'ing, *then* returns the token."""
    minter = _minter()
    monkeypatch.setattr("mcp_github.auth.time.sleep", lambda _s: None)

    def fake_post(url, *, headers, json, timeout):
        req = httpx.Request("POST", url)
        return httpx.Response(
            201,
            json={"token": "scoped-tok", "expires_at": _iso(3600)},
            request=req,
        )

    probe_responses = iter([
        httpx.Response(401, text='{"message":"x"}', request=httpx.Request("GET", "/x")),
        httpx.Response(401, text='{"message":"x"}', request=httpx.Request("GET", "/x")),
        httpx.Response(200, json={"repositories": []}, request=httpx.Request("GET", "/x")),
    ])

    def fake_get(*args, **kwargs):
        return next(probe_responses)

    with (
        patch("mcp_github.auth.httpx.post", side_effect=fake_post),
        patch("mcp_github.auth.httpx.get", side_effect=fake_get),
    ):
        token, _ = minter.mint_scoped_token(
            repositories=["foo"],
            permissions={"contents": "read", "metadata": "read"},
        )

    assert token == "scoped-tok"


def test_mint_scoped_token_raises_when_warmup_never_succeeds(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Persistent 401 on the probe → explicit GitHubTokenNotReadyError, not
    a likely-bad token. The caller is supposed to see this as a problem,
    not paper over it."""
    minter = _minter()
    monkeypatch.setattr("mcp_github.auth.time.sleep", lambda _s: None)

    def fake_post(url, *, headers, json, timeout):
        req = httpx.Request("POST", url)
        return httpx.Response(
            201,
            json={"token": "scoped-tok", "expires_at": _iso(3600)},
            request=req,
        )

    def fake_get(*args, **kwargs):
        return httpx.Response(401, text='{"message":"x"}', request=httpx.Request("GET", "/x"))

    with (
        patch("mcp_github.auth.httpx.post", side_effect=fake_post),
        patch("mcp_github.auth.httpx.get", side_effect=fake_get),
    ):
        with pytest.raises(GitHubTokenNotReadyError):
            minter.mint_scoped_token(
                repositories=["foo"],
                permissions={"contents": "read", "metadata": "read"},
            )


def test_mint_scoped_token_does_not_disturb_cached_installation_token() -> None:
    """Scoped mints are one-shot — they must not touch the long-lived
    cached installation token used by PATCH/DELETE/etc."""
    minter = _minter()
    cached_responses = [_access_token_response("install-tok", expires_in_seconds=3600)]

    def fake_post(url, *, headers, json=None, timeout=None):
        req = httpx.Request("POST", url)
        if json is None:
            # Installation-token mint (no body) → cached path.
            return cached_responses.pop(0)
        # Scoped mint.
        return httpx.Response(
            201,
            json={"token": "scoped-tok", "expires_at": _iso(3600)},
            request=req,
        )

    def fake_get(*args, **kwargs):
        return httpx.Response(200, json={"repositories": []}, request=httpx.Request("GET", "/x"))

    with (
        patch("mcp_github.auth.httpx.post", side_effect=fake_post),
        patch("mcp_github.auth.httpx.get", side_effect=fake_get),
    ):
        first = minter.installation_token()
        scoped, _ = minter.mint_scoped_token(
            repositories=["foo"],
            permissions={"contents": "read", "metadata": "read"},
        )
        second = minter.installation_token()

    assert first == "install-tok"
    assert second == "install-tok", (
        "scoped mint must not invalidate the long-lived cached token"
    )
    assert scoped == "scoped-tok"


# ---------------------------------------------------------------------------
# Migration guard: the legacy hardcoded TTL must not return.
# ---------------------------------------------------------------------------


def test_auth_module_does_not_reintroduce_hardcoded_ttl() -> None:
    """Per /workspace/.tank/docs/migration-policy.md: the old
    ``time.time() + 3300`` path is a deletion target, not a fallback.
    Fail if it ever shows back up in auth.py — the cache must derive
    TTL from GitHub's `expires_at`, never from a hardcoded constant."""
    auth_src = Path(auth_mod.__file__).read_text(encoding="utf-8")
    forbidden = (
        "time.time() + 3300",
        "time.time()+3300",
        "+ 3300",
        "+3300",
    )
    for needle in forbidden:
        assert needle not in auth_src, (
            f"legacy locally-computed TTL pattern reappeared in auth.py: {needle!r}"
        )
