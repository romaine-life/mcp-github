from __future__ import annotations

import sys
from pathlib import Path
from unittest.mock import MagicMock

from mcp.server.fastmcp import FastMCP

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from mcp_github.tools import register_tools  # noqa: E402


def _get_tool(mcp: FastMCP, name: str):
    for tool in mcp._tool_manager._tools.values():
        if tool.name == name:
            return tool.fn
    raise KeyError(name)


class FakeAuditor:
    """The comment-read tools never touch the auditor; a no-op stand-in suffices."""

    def start(self, **kwargs):  # pragma: no cover - defensive
        raise AssertionError("read tools must not call auditor.start")

    def finish(self, invocation, **kwargs):  # pragma: no cover - defensive
        raise AssertionError("read tools must not call auditor.finish")


def _register(gh):
    mcp = FastMCP("test-github-mcp")
    register_tools(mcp, gh, FakeAuditor())
    return mcp


def _issue_comment(cid: int, body: str = "hello"):
    return {
        "id": cid,
        "user": {"login": "octocat"},
        "created_at": "2026-06-01T00:00:00Z",
        "updated_at": "2026-06-02T00:00:00Z",
        "author_association": "MEMBER",
        "html_url": f"https://github.com/o/n/issues/5#issuecomment-{cid}",
        "body": body,
    }


def _review_comment(cid: int, body: str = "fix this"):
    return {
        "id": cid,
        "user": {"login": "reviewer"},
        "path": "src/foo.py",
        "line": 42,
        "original_line": 40,
        "commit_id": "abc123",
        "diff_hunk": "@@ -1 +1 @@\n-old\n+new",
        "in_reply_to_id": None,
        "created_at": "2026-06-01T00:00:00Z",
        "html_url": f"https://github.com/o/n/pull/5#discussion-{cid}",
        "body": body,
    }


def _review(rid: int, body: str = "looks good"):
    return {
        "id": rid,
        "user": {"login": "approver"},
        "state": "APPROVED",
        "submitted_at": "2026-06-03T00:00:00Z",
        "commit_id": "abc123",
        "author_association": "MEMBER",
        "html_url": f"https://github.com/o/n/pull/5#pullrequestreview-{rid}",
        "body": body,
    }


# --------------------------------------------------------------------------- #
# list_issue_comments
# --------------------------------------------------------------------------- #
def test_list_issue_comments_endpoint_params_and_shape():
    gh = MagicMock()
    gh.get.return_value = [_issue_comment(1), _issue_comment(2)]
    mcp = _register(gh)

    out = _get_tool(mcp, "list_issue_comments")(
        "romaine-life", "mcp-github", 5, since="2026-01-01T00:00:00Z", limit=10, page=2
    )

    gh.get.assert_called_once_with(
        "/repos/romaine-life/mcp-github/issues/5/comments",
        params={"since": "2026-01-01T00:00:00Z", "per_page": 10, "page": 2},
        repo=("romaine-life", "mcp-github"),
    )
    assert out["count"] == 2
    assert out["page"] == 2
    assert out["has_more"] is False
    first = out["comments"][0]
    assert first["user"] == "octocat"  # flattened to a string
    assert first["body"] == "hello"
    assert first["body_truncated"] is False
    assert first["author_association"] == "MEMBER"


def test_list_issue_comments_clamps_per_page_to_100():
    gh = MagicMock()
    gh.get.return_value = []
    mcp = _register(gh)

    _get_tool(mcp, "list_issue_comments")("o", "n", 5, limit=500)

    _, kwargs = gh.get.call_args
    assert kwargs["params"]["per_page"] == 100


def test_list_issue_comments_omits_since_when_none():
    gh = MagicMock()
    gh.get.return_value = []
    mcp = _register(gh)

    _get_tool(mcp, "list_issue_comments")("o", "n", 5)

    _, kwargs = gh.get.call_args
    assert "since" not in kwargs["params"]
    assert kwargs["params"]["page"] == 1


def test_list_issue_comments_max_chars_truncates():
    gh = MagicMock()
    gh.get.return_value = [_issue_comment(1, body="abcdefghij")]
    mcp = _register(gh)

    out = _get_tool(mcp, "list_issue_comments")("o", "n", 5, max_chars=4)

    assert out["comments"][0]["body"] == "abcd"
    assert out["comments"][0]["body_truncated"] is True


def test_list_issue_comments_has_more_when_full_page():
    gh = MagicMock()
    gh.get.return_value = [_issue_comment(i) for i in range(10)]
    mcp = _register(gh)

    out = _get_tool(mcp, "list_issue_comments")("o", "n", 5, limit=10)

    assert out["has_more"] is True
    assert out["count"] == 10


# --------------------------------------------------------------------------- #
# list_pull_request_review_comments
# --------------------------------------------------------------------------- #
def test_list_pull_request_review_comments_endpoint_and_rows():
    gh = MagicMock()
    gh.get.return_value = [_review_comment(11)]
    mcp = _register(gh)

    out = _get_tool(mcp, "list_pull_request_review_comments")(
        "romaine-life", "mcp-github", 7, since="2026-01-01T00:00:00Z", limit=5
    )

    gh.get.assert_called_once_with(
        "/repos/romaine-life/mcp-github/pulls/7/comments",
        params={"since": "2026-01-01T00:00:00Z", "per_page": 5, "page": 1},
        repo=("romaine-life", "mcp-github"),
    )
    row = out["comments"][0]
    assert row["path"] == "src/foo.py"
    assert row["diff_hunk"].startswith("@@")
    assert row["line"] == 42
    assert row["user"] == "reviewer"
    assert out["has_more"] is False


def test_review_comments_line_falls_back_to_original_line():
    gh = MagicMock()
    rc = _review_comment(11)
    rc["line"] = None
    gh.get.return_value = [rc]
    mcp = _register(gh)

    out = _get_tool(mcp, "list_pull_request_review_comments")("o", "n", 7)

    assert out["comments"][0]["line"] == 40


def test_review_comments_max_chars_and_has_more():
    gh = MagicMock()
    gh.get.return_value = [_review_comment(i, body="0123456789") for i in range(3)]
    mcp = _register(gh)

    out = _get_tool(mcp, "list_pull_request_review_comments")("o", "n", 7, limit=3, max_chars=3)

    assert out["has_more"] is True
    assert out["comments"][0]["body"] == "012"
    assert out["comments"][0]["body_truncated"] is True


# --------------------------------------------------------------------------- #
# list_pull_request_reviews
# --------------------------------------------------------------------------- #
def test_list_pull_request_reviews_endpoint_and_rows():
    gh = MagicMock()
    gh.get.return_value = [_review(21)]
    mcp = _register(gh)

    out = _get_tool(mcp, "list_pull_request_reviews")(
        "romaine-life", "mcp-github", 9, limit=15, page=3
    )

    gh.get.assert_called_once_with(
        "/repos/romaine-life/mcp-github/pulls/9/reviews",
        params={"per_page": 15, "page": 3},
        repo=("romaine-life", "mcp-github"),
    )
    row = out["reviews"][0]
    assert row["state"] == "APPROVED"
    assert row["user"] == "approver"
    assert out["page"] == 3
    assert out["has_more"] is False


def test_list_pull_request_reviews_clamps_and_has_more():
    gh = MagicMock()
    gh.get.return_value = [_review(i) for i in range(100)]
    mcp = _register(gh)

    out = _get_tool(mcp, "list_pull_request_reviews")("o", "n", 9, limit=999)

    _, kwargs = gh.get.call_args
    assert kwargs["params"]["per_page"] == 100
    assert out["has_more"] is True
    assert out["count"] == 100


def test_list_pull_request_reviews_max_chars_truncates():
    gh = MagicMock()
    gh.get.return_value = [_review(21, body="abcdefghij")]
    mcp = _register(gh)

    out = _get_tool(mcp, "list_pull_request_reviews")("o", "n", 9, max_chars=2)

    assert out["reviews"][0]["body"] == "ab"
    assert out["reviews"][0]["body_truncated"] is True
