from __future__ import annotations

from prometheus_client import Counter, Histogram


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


# --- token lifecycle observability ------------------------------------------
#
# The pre-2026-06 build of mcp-github surfaced upstream GitHub 401s straight
# to MCP callers. Today the call path:
#
# 1. caches installation tokens off GitHub's reported `expires_at`, refreshes
#    proactively with a 10-min skew, and force-refreshes the cache on a 401;
# 2. retries a 401'd cached-token request exactly once with the fresh token,
#    then surfaces the second result;
# 3. probes a freshly minted scoped (clone) token until GitHub accepts it,
#    then returns it to the caller — collapsing GitHub's eventual-consistency
#    window into the mint call rather than every downstream `gh api`.
#
# These counters make all three behaviors observable: refresh churn vs.
# 401-driven invalidation, retry success rate (and remaining real 401s),
# and the warmup-attempt distribution so we can tune the skew/budget as
# GitHub's window shifts.

token_refresh_total = Counter(
    "mcp_github_token_refresh_total",
    "GitHub App installation-token mints via `/access_tokens`, labelled by "
    "what triggered the mint (cold start, natural TTL refresh, or a "
    "force-refresh triggered by an upstream 401) and the outcome.",
    ["trigger", "result"],
)

call_401_retry_total = Counter(
    "mcp_github_call_401_retry_total",
    "Cached-token request retries triggered by an upstream 401. The retry "
    "force-refreshes the minter (single-flighted) and reissues the original "
    "request exactly once before surfacing the result.",
    ["verb", "result"],
)

token_warmup_total = Counter(
    "mcp_github_token_warmup_total",
    "Outcome of the post-mint readiness probe for scoped (clone) tokens. "
    "`ok` means GitHub accepted the new token within the retry budget; "
    "`not_ready` means it kept 401'ing and the mint surfaced "
    "GitHubTokenNotReadyError.",
    ["result"],
)

token_warmup_attempts = Histogram(
    "mcp_github_token_warmup_attempts",
    "Probe attempts taken before GitHub accepted a freshly minted scoped "
    "token. A persistent eventual-consistency window shows up here before "
    "it shows up as user-visible failures.",
    buckets=(1.0, 2.0, 3.0, 4.0, 5.0, 6.0),
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


_VERB_LABELS = {"GET", "POST", "PATCH", "PUT", "DELETE"}


def _verb_label(raw: str) -> str:
    upper = (raw or "").upper()
    if upper in _VERB_LABELS:
        return upper
    return "other"


_TRIGGER_LABELS = {"cold", "ttl", "force_401"}


def _trigger_label(raw: str) -> str:
    return raw if raw in _TRIGGER_LABELS else "other"


_REFRESH_RESULT_LABELS = {"ok", "failed"}


def _refresh_result_label(raw: str) -> str:
    return raw if raw in _REFRESH_RESULT_LABELS else "other"


_RETRY_RESULT_LABELS = {"ok", "failed"}


def _retry_result_label(raw: str) -> str:
    return raw if raw in _RETRY_RESULT_LABELS else "other"


_WARMUP_RESULT_LABELS = {"ok", "not_ready"}


def _warmup_result_label(raw: str) -> str:
    return raw if raw in _WARMUP_RESULT_LABELS else "other"


def record_control_action(tool: str, action: str, status: str, result: str) -> None:
    control_action_total.labels(
        tool_label(tool),
        action_label(action),
        status_label(status),
        result_label(result),
    ).inc()


def record_audit_append(status: str, result: str) -> None:
    control_action_audit_append_total.labels(status_label(status), result_label(result)).inc()


def record_token_refresh(*, trigger: str, result: str) -> None:
    token_refresh_total.labels(
        _trigger_label(trigger),
        _refresh_result_label(result),
    ).inc()


def record_call_401_retry(*, verb: str, result: str) -> None:
    call_401_retry_total.labels(
        _verb_label(verb),
        _retry_result_label(result),
    ).inc()


def record_token_warmup(*, result: str, attempts: int) -> None:
    token_warmup_total.labels(_warmup_result_label(result)).inc()
    token_warmup_attempts.observe(float(attempts))
