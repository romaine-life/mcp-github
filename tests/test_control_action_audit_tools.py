from __future__ import annotations

import sys
from pathlib import Path
from unittest.mock import MagicMock

import httpx
import pytest
from mcp.server.fastmcp import FastMCP

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from mcp_github.caller import CALLER, CallerIdentity  # noqa: E402
from mcp_github.control_audit import ControlActionAuditor  # noqa: E402
from mcp_github.tools import register_tools  # noqa: E402


def _get_tool(mcp: FastMCP, name: str):
    for tool in mcp._tool_manager._tools.values():
        if tool.name == name:
            return tool.fn
    raise KeyError(name)


class FakeInvocation:
    invocation_id = "ctrl_test"
    session_id = "63"


class FakeAuditor:
    def __init__(self, *, fail_start: bool = False) -> None:
        self.fail_start = fail_start
        self.started: list[dict] = []
        self.finished: list[dict] = []

    def start(self, **kwargs):
        if self.fail_start:
            raise RuntimeError("audit unavailable")
        self.started.append(kwargs)
        return FakeInvocation()

    def finish(self, invocation, **kwargs) -> None:
        self.finished.append(kwargs)


def _fake_pr():
    return {
        "html_url": "https://github.com/romaine-life/tank-operator/pull/857",
        "node_id": "PR_node_857",
        "draft": True,
        "state": "open",
        "head": {"sha": "head-sha"},
        "base": {"sha": "base-sha"},
    }


def test_merge_pull_request_records_started_and_succeeded() -> None:
    mcp = FastMCP("test-github-mcp")
    gh = MagicMock()
    auditor = FakeAuditor()
    gh.get.return_value = _fake_pr()
    gh.put.return_value = {"merged": True, "sha": "merge-sha", "message": "merged"}
    register_tools(mcp, gh, auditor)

    result = _get_tool(mcp, "merge_pull_request")("romaine-life", "tank-operator", 857, merge_method="squash")

    assert result["merged"] is True
    assert result["audit_terminal_recorded"] is True
    assert auditor.started[0]["source_tool"] == "merge_pull_request"
    assert auditor.started[0]["action"] == "github.pull_request.merge"
    assert auditor.started[0]["pr_number"] == 857
    assert auditor.finished[0]["status"] == "succeeded"
    assert auditor.finished[0]["result_sha"] == "merge-sha"


def test_merge_pull_request_fails_closed_when_audit_start_fails() -> None:
    mcp = FastMCP("test-github-mcp")
    gh = MagicMock()
    auditor = FakeAuditor(fail_start=True)
    gh.get.return_value = _fake_pr()
    register_tools(mcp, gh, auditor)

    with pytest.raises(RuntimeError, match="audit unavailable"):
        _get_tool(mcp, "merge_pull_request")("romaine-life", "tank-operator", 857)

    gh.put.assert_not_called()


def test_merge_pull_request_records_failed_when_github_rejects() -> None:
    mcp = FastMCP("test-github-mcp")
    gh = MagicMock()
    auditor = FakeAuditor()
    gh.get.return_value = _fake_pr()
    gh.put.side_effect = RuntimeError("still a draft")
    register_tools(mcp, gh, auditor)

    with pytest.raises(RuntimeError, match="still a draft"):
        _get_tool(mcp, "merge_pull_request")("romaine-life", "tank-operator", 857)

    assert auditor.started
    assert auditor.finished[0]["status"] == "failed"
    assert "still a draft" in auditor.finished[0]["error"]


def test_merge_pull_request_merges_when_expected_head_sha_matches() -> None:
    mcp = FastMCP("test-github-mcp")
    gh = MagicMock()
    auditor = FakeAuditor()
    gh.get.return_value = _fake_pr()
    gh.put.return_value = {"merged": True, "sha": "merge-sha", "message": "merged"}
    register_tools(mcp, gh, auditor)

    result = _get_tool(mcp, "merge_pull_request")(
        "romaine-life", "tank-operator", 857, expected_head_sha="head-sha"
    )

    assert result["merged"] is True
    gh.put.assert_called_once()
    assert auditor.finished[0]["status"] == "succeeded"


def test_merge_pull_request_refuses_when_expected_head_sha_moved() -> None:
    mcp = FastMCP("test-github-mcp")
    gh = MagicMock()
    auditor = FakeAuditor()
    gh.get.return_value = _fake_pr()
    register_tools(mcp, gh, auditor)

    with pytest.raises(ValueError, match="head SHA moved"):
        _get_tool(mcp, "merge_pull_request")(
            "romaine-life", "tank-operator", 857, expected_head_sha="stale-sha"
        )

    # Refused before any write or audit record.
    gh.put.assert_not_called()
    assert auditor.started == []


def test_merge_pull_request_skips_guard_when_expected_head_sha_absent() -> None:
    mcp = FastMCP("test-github-mcp")
    gh = MagicMock()
    auditor = FakeAuditor()
    gh.get.return_value = _fake_pr()
    gh.put.return_value = {"merged": True, "sha": "merge-sha", "message": "merged"}
    register_tools(mcp, gh, auditor)

    result = _get_tool(mcp, "merge_pull_request")("romaine-life", "tank-operator", 857)

    assert result["merged"] is True
    gh.put.assert_called_once()


def test_mark_ready_records_started_and_succeeded() -> None:
    mcp = FastMCP("test-github-mcp")
    gh = MagicMock()
    auditor = FakeAuditor()
    gh.get.return_value = _fake_pr()
    gh.graphql.return_value = {
        "markPullRequestReadyForReview": {
            "pullRequest": {"number": 857, "isDraft": False}
        }
    }
    register_tools(mcp, gh, auditor)

    result = _get_tool(mcp, "mark_pull_request_ready_for_review")("romaine-life", "tank-operator", 857)

    assert result["is_draft"] is False
    assert result["audit_terminal_recorded"] is True
    assert auditor.started[0]["source_tool"] == "mark_pull_request_ready_for_review"
    assert auditor.started[0]["action"] == "github.pull_request.ready_for_review"
    assert auditor.finished[0]["status"] == "succeeded"


def test_control_action_auditor_routes_slot_scopes_to_slot_orchestrator(monkeypatch) -> None:
    seen: list[str] = []

    def fake_post(url, *, json, headers, timeout):
        seen.append(url)
        assert headers["Authorization"] == "Bearer svc-token"
        assert json["status"] == "started"
        return httpx.Response(201, json={"ok": True}, request=httpx.Request("POST", url))

    monkeypatch.setattr("mcp_github.control_audit.httpx.post", fake_post)
    token = CALLER.set(CallerIdentity(
        email="owner@example.test",
        installation_id=1,
        is_host=False,
        session_scope="tank-operator-slot-3",
        session_id="47",
        service_bearer="svc-token",
    ))
    try:
        ControlActionAuditor("http://tank-operator.tank-operator.svc").start(
            source_tool="merge_pull_request",
            action="github.pull_request.merge",
            target_kind="github_pull_request",
            target_ref="https://github.com/romaine-life/tank-operator/pull/857",
            repo_owner="romaine-life",
            repo_name="tank-operator",
            pr_number=857,
        )
    finally:
        CALLER.reset(token)

    assert seen == [
        "http://tank-operator.tank-operator-slot-3.svc:80/api/internal/sessions/47/control-actions"
    ]
