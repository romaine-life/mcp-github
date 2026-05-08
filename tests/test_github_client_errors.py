"""HTTP error surfacing in GitHubClient.

httpx's HTTPStatusError ``__str__`` is just the status line by default,
which means MCP clients only ever saw "Client error '405 Method Not
Allowed' for url ..." with the body GitHub returned (the *actual*
explanation — required check missing, branch out of date, validation
errors, etc.) silently dropped on the way out. ``_check`` re-raises
with the body inlined so the MCP transport carries the full message.
"""

from __future__ import annotations

import sys
from pathlib import Path

import httpx
import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from mcp_github.github_client import _check  # noqa: E402


def _response(
    status_code: int,
    body: str,
    *,
    method: str = "PUT",
    url: str = "https://api.github.com/repos/owner/name/pulls/1/merge",
) -> httpx.Response:
    request = httpx.Request(method, url)
    return httpx.Response(status_code=status_code, text=body, request=request)


def test_check_passthrough_on_2xx() -> None:
    """Success path: no exception, no allocation."""
    _check(_response(200, '{"ok": true}'))


def test_check_includes_body_on_405() -> None:
    """The motivating case: a 405 from /merge with the explanation in
    the body. ``__str__`` must surface the body so the operator can tell
    'self-merge blocked' from 'branch out of date' from 'required check
    missing' without inspecting the response object directly."""
    body = (
        '{"message":"Required status check \\"docker-build-check\\" '
        'has not succeeded.","documentation_url":"..."}'
    )
    with pytest.raises(httpx.HTTPStatusError) as exc:
        _check(_response(405, body))

    s = str(exc.value)
    assert "405" in s
    assert "Required status check" in s, s
    # Status code still reachable on the exception's response — pattern
    # callers in tools.py rely on this.
    assert exc.value.response.status_code == 405


def test_check_includes_method_and_url() -> None:
    with pytest.raises(httpx.HTTPStatusError) as exc:
        _check(_response(404, '{"message":"Not Found"}'))

    s = str(exc.value)
    assert "PUT" in s or "GET" in s  # method recorded
    assert "api.github.com" in s


def test_check_truncates_huge_bodies() -> None:
    """A pathological 10k HTML error page shouldn't blow up the MCP
    frame. Truncate with a marker so the operator knows there's more."""
    huge = "x" * 5000
    with pytest.raises(httpx.HTTPStatusError) as exc:
        _check(_response(500, huge))

    s = str(exc.value)
    assert "...(truncated)" in s
    assert len(s) < 2500  # cap (1200) + status preamble — comfortably under 2.5k


def test_check_handles_empty_body() -> None:
    """Some endpoints (DELETE, sometimes PUT) 4xx with an empty body.
    Don't synthesize a useless ': ' on the message."""
    with pytest.raises(httpx.HTTPStatusError) as exc:
        _check(_response(409, ""))

    s = str(exc.value)
    assert "409" in s
    assert not s.endswith(": "), f"should not append empty body marker: {s!r}"


def test_check_preserves_response_class_for_pattern_callers() -> None:
    """tools.py has helpers like ``_is_404(exc)`` that match
    ``isinstance(exc, httpx.HTTPStatusError)`` and read
    ``exc.response.status_code``. Make sure both still hold."""
    with pytest.raises(httpx.HTTPStatusError) as exc:
        _check(_response(404, '{"message":"Not Found"}'))
    assert isinstance(exc.value, httpx.HTTPStatusError)
    assert exc.value.response is not None
    assert exc.value.response.status_code == 404
