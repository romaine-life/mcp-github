from __future__ import annotations

from prometheus_client import Counter


control_action_total = Counter(
    "mcp_github_control_action_total",
    "Privileged GitHub control actions executed through the MCP server.",
    ["tool", "action", "status", "result"],
)

control_action_audit_append_total = Counter(
    "mcp_github_control_action_audit_append_total",
    "Control-action audit append attempts from mcp-github to Tank.",
    ["status", "result"],
)


def tool_label(raw: str) -> str:
    if raw in {"merge_pull_request", "mark_pull_request_ready_for_review"}:
        return raw
    return "other"


def action_label(raw: str) -> str:
    if raw in {"github.pull_request.merge", "github.pull_request.ready_for_review"}:
        return raw
    return "other"


def status_label(raw: str) -> str:
    if raw in {"started", "succeeded", "failed"}:
        return raw
    return "unknown"


def result_label(raw: str) -> str:
    if raw in {"ok", "audit_failed", "github_failed"}:
        return raw
    return "other"


def record_control_action(tool: str, action: str, status: str, result: str) -> None:
    control_action_total.labels(
        tool_label(tool),
        action_label(action),
        status_label(status),
        result_label(result),
    ).inc()


def record_audit_append(status: str, result: str) -> None:
    control_action_audit_append_total.labels(status_label(status), result_label(result)).inc()
