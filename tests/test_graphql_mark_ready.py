"""GraphQL helper + mark-ready support.

The github MCP previously exposed no way to clear a pull request's draft
flag, so draft PRs opened by the /test workflow could not be merged via
`merge_pull_request` (GitHub rejects a draft merge with 405). GitHub only
offers `markPullRequestReadyForReview` over the GraphQL v4 API, so the
`GitHubClient.graphql` helper is the load-bearing new primitive; the
`mark_pull_request_ready_for_review` tool is a thin wrapper over it.

These tests cover the client helper directly (matching the suite's
client-level testing style):
- happy path: posts to /graphql, threads variables, returns `data`
- GraphQL-level errors (HTTP 200 + `errors[]`) are promoted to RuntimeError
- the per-caller minter + repo fallback plumbing is exercised (host caller)
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
from mcp_github.github_client import GitHubClient  # noqa: E402
from mcp_github.minter_pool import MinterPool  # noqa: E402


def _pool() -> tuple[MinterPool, GitHubAppTokenMinter]:
    host = GitHubAppTokenMinter("host-app", "host-install", "host-key")
    pool = MinterPool(
        host_minter=host,
        tank_operator_app_id="user-app",
        tank_operator_private_key="user-key",
    )
    return pool, host


def _host_caller() -> CallerIdentity:
    return CallerIdentity(
        email="alice@example.test",
        installation_id=1,
        is_host=True,
        is_super_admin=True,
    )


def test_graphql_posts_to_v4_threads_variables_and_returns_data() -> None:
    pool, host = _pool()
    caller = _host_caller()
    client = GitHubClient(pool)
    host.installation_token = MagicMock(return_value="host-tok")  # type: ignore[method-assign]

    captured: dict[str, object] = {}

    def fake_post(url, *, headers, json, timeout):
        captured["url"] = url
        captured["json"] = json
        captured["tok"] = headers["Authorization"].split()[1]
        req = httpx.Request("POST", url)
        return httpx.Response(
            200,
            json={
                "data": {
                    "markPullRequestReadyForReview": {
                        "pullRequest": {"number": 743, "isDraft": False}
                    }
                }
            },
            request=req,
        )

    token = CALLER.set(caller)
    try:
        with patch("mcp_github.github_client.httpx.post", side_effect=fake_post):
            data = client.graphql(
                "mutation($id:ID!){markPullRequestReadyForReview(input:{pullRequestId:$id}){pullRequest{number isDraft}}}",
                {"id": "PR_node_123"},
                repo=("nelsong6", "tank-operator"),
            )
    finally:
        CALLER.reset(token)

    assert captured["url"] == "https://api.github.com/graphql"
    assert captured["json"]["variables"] == {"id": "PR_node_123"}
    assert captured["tok"] == "host-tok"
    assert data["markPullRequestReadyForReview"]["pullRequest"]["isDraft"] is False


def test_graphql_promotes_errors_to_runtime_error() -> None:
    pool, host = _pool()
    caller = _host_caller()
    client = GitHubClient(pool)
    host.installation_token = MagicMock(return_value="host-tok")  # type: ignore[method-assign]

    def fake_post(url, *, headers, json, timeout):
        req = httpx.Request("POST", url)
        # GraphQL reports logical failures as HTTP 200 + errors[].
        return httpx.Response(
            200,
            json={"data": None, "errors": [{"message": "Could not resolve to a node."}]},
            request=req,
        )

    token = CALLER.set(caller)
    try:
        with patch("mcp_github.github_client.httpx.post", side_effect=fake_post):
            with pytest.raises(RuntimeError, match="GraphQL error"):
                client.graphql("mutation{__typename}", repo=("nelsong6", "tank-operator"))
    finally:
        CALLER.reset(token)


def test_graphql_defaults_empty_variables() -> None:
    pool, host = _pool()
    caller = _host_caller()
    client = GitHubClient(pool)
    host.installation_token = MagicMock(return_value="host-tok")  # type: ignore[method-assign]

    captured: dict[str, object] = {}

    def fake_post(url, *, headers, json, timeout):
        captured["json"] = json
        req = httpx.Request("POST", url)
        return httpx.Response(200, json={"data": {"ok": True}}, request=req)

    token = CALLER.set(caller)
    try:
        with patch("mcp_github.github_client.httpx.post", side_effect=fake_post):
            client.graphql("query{viewer{login}}", repo=("nelsong6", "tank-operator"))
    finally:
        CALLER.reset(token)

    assert captured["json"]["variables"] == {}
