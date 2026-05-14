"""Caller identity + source-IP extraction (#57 stage 3)."""

from __future__ import annotations

import json
import sys
from pathlib import Path
from unittest.mock import patch

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from mcp_github.caller import (  # noqa: E402
    CallerIdentity,
    CallerResolutionError,
    CallerResolver,
    extract_source_pod_ip,
)


# ---------------------------------------------------------------------------
# CallerIdentity.from_dict
# ---------------------------------------------------------------------------


def test_caller_identity_from_dict_full_payload() -> None:
    body = {
        "email": "Alice@Example.test",
        "installation_id": 12345,
        "is_host": False,
        "host_email": "host@example.test",
        "pod_name": "session-abc",
    }
    caller = CallerIdentity.from_dict(body)
    # Email is normalised to lowercase so the minter cache + comparisons
    # downstream don't have to re-normalise.
    assert caller.email == "alice@example.test"
    assert caller.installation_id == 12345
    assert caller.is_host is False


def test_caller_identity_from_dict_null_installation() -> None:
    caller = CallerIdentity.from_dict(
        {"email": "carol@example.test", "installation_id": None, "is_host": False}
    )
    assert caller.installation_id is None


def test_caller_identity_from_dict_host_flag() -> None:
    caller = CallerIdentity.from_dict(
        {"email": "host@example.test", "installation_id": 1, "is_host": True}
    )
    assert caller.is_host is True
    assert caller.is_super_admin is False


def test_caller_identity_from_dict_super_admin_flag() -> None:
    caller = CallerIdentity.from_dict(
        {
            "email": "admin@example.test",
            "installation_id": 1,
            "is_host": False,
            "is_super_admin": True,
        }
    )
    assert caller.is_super_admin is True


# ---------------------------------------------------------------------------
# extract_source_pod_ip
# ---------------------------------------------------------------------------


def test_extract_source_pod_ip_picks_last_xff_hop() -> None:
    # X-Forwarded-For grows right when each proxy appends. Our
    # kube-rbac-proxy is the front; its peer (the session pod) is the
    # last hop appended. Trust the last entry.
    assert extract_source_pod_ip("10.244.1.94", peer_ip="127.0.0.1") == "10.244.1.94"


def test_extract_source_pod_ip_takes_last_when_multiple_hops() -> None:
    assert (
        extract_source_pod_ip("8.8.8.8, 10.244.1.94", peer_ip="127.0.0.1")
        == "10.244.1.94"
    )


def test_extract_source_pod_ip_strips_whitespace() -> None:
    assert (
        extract_source_pod_ip("8.8.8.8 ,   10.244.1.94  ", peer_ip="127.0.0.1")
        == "10.244.1.94"
    )


def test_extract_source_pod_ip_falls_back_to_peer_when_no_header() -> None:
    assert extract_source_pod_ip(None, peer_ip="10.244.1.50") == "10.244.1.50"


def test_extract_source_pod_ip_returns_none_when_nothing() -> None:
    assert extract_source_pod_ip(None, peer_ip=None) is None


def test_extract_source_pod_ip_falls_back_when_xff_empty_string() -> None:
    assert extract_source_pod_ip("", peer_ip="10.244.1.50") == "10.244.1.50"


# ---------------------------------------------------------------------------
# CallerResolver
# ---------------------------------------------------------------------------


class _FakeAsyncClient:
    """Minimal async-context-manager stub for httpx.AsyncClient.

    Records every outbound call so the test can assert the SA token was
    forwarded as Bearer auth.
    """

    def __init__(
        self,
        response_status: int | list[int],
        response_body: dict | list[dict | None] | None = None,
    ) -> None:
        statuses = response_status if isinstance(response_status, list) else [response_status]
        bodies = response_body if isinstance(response_body, list) else [response_body]
        self._responses = [
            (status, bodies[i] if i < len(bodies) else None)
            for i, status in enumerate(statuses)
        ]
        self.calls: list[dict] = []

    async def __aenter__(self):
        return self

    async def __aexit__(self, *_a):
        return False

    async def get(self, url, params=None, headers=None):  # noqa: D401
        self.calls.append({"url": url, "params": params, "headers": headers})
        status, body = self._responses[min(len(self.calls) - 1, len(self._responses) - 1)]
        return _FakeResponse(status, body)


class _FakeResponse:
    def __init__(self, status_code: int, body: dict | None) -> None:
        self.status_code = status_code
        self._body = body or {}
        self.text = json.dumps(self._body) if body else ""

    def json(self) -> dict:
        return self._body


@pytest.fixture
def sa_token_file(tmp_path: Path) -> Path:
    p = tmp_path / "token"
    p.write_text("fake-sa-token\n")
    return p


def test_resolver_returns_identity_on_200(sa_token_file: Path) -> None:
    fake = _FakeAsyncClient(
        200,
        {
            "email": "alice@example.test",
            "installation_id": 999,
            "is_host": False,
        },
    )
    resolver = CallerResolver(
        orchestrator_url="http://orchestrator", sa_token_path=str(sa_token_file)
    )

    with patch("mcp_github.caller.httpx.AsyncClient", return_value=fake):
        import asyncio

        caller = asyncio.run(resolver.resolve("10.0.0.1"))

    assert caller is not None
    assert caller.email == "alice@example.test"
    assert caller.installation_id == 999

    # Bearer SA token forwarded as orchestrator auth.
    assert fake.calls[0]["headers"]["Authorization"] == "Bearer fake-sa-token"


def test_resolver_raises_on_404(sa_token_file: Path) -> None:
    fake = _FakeAsyncClient(404, {"detail": "no session pod with IP"})
    resolver = CallerResolver(
        orchestrator_url="http://orchestrator", sa_token_path=str(sa_token_file)
    )

    with patch("mcp_github.caller.httpx.AsyncClient", return_value=fake):
        import asyncio

        with pytest.raises(CallerResolutionError, match="no session pod"):
            asyncio.run(resolver.resolve("10.0.0.99"))


def test_resolver_falls_back_to_next_orchestrator_on_404(sa_token_file: Path) -> None:
    fake = _FakeAsyncClient(
        [404, 200],
        [
            {"detail": "no session pod with IP"},
            {"email": "slot@example.test", "installation_id": 456, "is_host": False},
        ],
    )
    resolver = CallerResolver(
        orchestrator_urls=("http://main", "http://slot-3"),
        sa_token_path=str(sa_token_file),
    )

    with patch("mcp_github.caller.httpx.AsyncClient", return_value=fake):
        import asyncio

        caller = asyncio.run(resolver.resolve("10.0.0.99"))

    assert caller.email == "slot@example.test"
    assert [call["url"] for call in fake.calls] == [
        "http://main/api/internal/resolve-caller",
        "http://slot-3/api/internal/resolve-caller",
    ]


def test_resolver_retries_with_fallback_token_on_401(
    sa_token_file: Path, tmp_path: Path
) -> None:
    fallback = tmp_path / "default-token"
    fallback.write_text("fallback-token\n")
    fake = _FakeAsyncClient(
        [401, 200],
        [
            {"detail": "ServiceAccount token not authenticated"},
            {"email": "slot@example.test", "installation_id": 456, "is_host": False},
        ],
    )
    resolver = CallerResolver(
        orchestrator_url="http://slot-3",
        sa_token_path=str(sa_token_file),
        fallback_sa_token_path=str(fallback),
    )

    with patch("mcp_github.caller.httpx.AsyncClient", return_value=fake):
        import asyncio

        caller = asyncio.run(resolver.resolve("10.0.0.99"))

    assert caller.email == "slot@example.test"
    assert [call["headers"]["Authorization"] for call in fake.calls] == [
        "Bearer fake-sa-token",
        "Bearer fallback-token",
    ]


def test_resolver_caches_responses(sa_token_file: Path) -> None:
    fake = _FakeAsyncClient(
        200,
        {"email": "alice@example.test", "installation_id": 123, "is_host": False},
    )
    resolver = CallerResolver(
        orchestrator_url="http://orchestrator",
        sa_token_path=str(sa_token_file),
        cache_ttl_seconds=300,
    )

    import asyncio

    with patch("mcp_github.caller.httpx.AsyncClient", return_value=fake):
        first = asyncio.run(resolver.resolve("10.0.0.1"))
        second = asyncio.run(resolver.resolve("10.0.0.1"))

    assert first == second
    assert len(fake.calls) == 1, "second resolve should have hit the cache, not refetched"


def test_resolver_raises_when_sa_token_unreadable(tmp_path: Path) -> None:
    resolver = CallerResolver(
        orchestrator_url="http://orchestrator",
        sa_token_path=str(tmp_path / "does-not-exist"),
        fallback_sa_token_path=str(tmp_path / "fallback-does-not-exist"),
    )

    import asyncio

    with pytest.raises(CallerResolutionError, match="could not read SA token"):
        asyncio.run(resolver.resolve("10.0.0.1"))


def test_resolver_raises_when_pod_ip_blank(sa_token_file: Path) -> None:
    resolver = CallerResolver(
        orchestrator_url="http://orchestrator", sa_token_path=str(sa_token_file)
    )

    import asyncio

    with pytest.raises(CallerResolutionError, match="missing source pod IP"):
        asyncio.run(resolver.resolve(""))
