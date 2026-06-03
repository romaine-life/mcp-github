from __future__ import annotations

import logging
import uuid
from dataclasses import dataclass
from typing import Any

import httpx

from .caller import current_caller
from .metrics import record_audit_append

log = logging.getLogger(__name__)


@dataclass(frozen=True)
class ControlActionInvocation:
    invocation_id: str
    session_id: str
    tank_operator_url: str


class ControlActionAuditor:
    def __init__(self, tank_operator_url: str) -> None:
        self._tank_operator_url = tank_operator_url.rstrip("/")

    def start(
        self,
        *,
        source_tool: str,
        action: str,
        target_kind: str,
        target_ref: str,
        repo_owner: str,
        repo_name: str,
        pr_number: int,
        payload: dict[str, Any] | None = None,
    ) -> ControlActionInvocation:
        caller = current_caller()
        if caller is None or not caller.session_id or not caller.service_bearer:
            raise RuntimeError("control action audit requires authenticated session caller")
        invocation = ControlActionInvocation(
            invocation_id=f"ctrl_{uuid.uuid4().hex}",
            session_id=caller.session_id,
            tank_operator_url=self._url_for_scope(caller.session_scope),
        )
        self._append(
            caller.service_bearer,
            tank_operator_url=invocation.tank_operator_url,
            session_id=caller.session_id,
            body={
                "event_id": f"{invocation.invocation_id}_started",
                "invocation_id": invocation.invocation_id,
                "source_service": "mcp-github",
                "source_tool": source_tool,
                "action": action,
                "status": "started",
                "target_kind": target_kind,
                "target_ref": target_ref,
                "repo_owner": repo_owner,
                "repo_name": repo_name,
                "pr_number": pr_number,
                "payload": payload or {},
            },
        )
        return invocation

    def finish(
        self,
        invocation: ControlActionInvocation,
        *,
        source_tool: str,
        action: str,
        target_kind: str,
        target_ref: str,
        repo_owner: str,
        repo_name: str,
        pr_number: int,
        status: str,
        result_sha: str = "",
        error: str = "",
        payload: dict[str, Any] | None = None,
    ) -> None:
        caller = current_caller()
        if caller is None or not caller.service_bearer:
            raise RuntimeError("control action audit requires authenticated session caller")
        self._append(
            caller.service_bearer,
            tank_operator_url=invocation.tank_operator_url,
            session_id=invocation.session_id,
            body={
                "event_id": f"{invocation.invocation_id}_{status}",
                "invocation_id": invocation.invocation_id,
                "source_service": "mcp-github",
                "source_tool": source_tool,
                "action": action,
                "status": status,
                "target_kind": target_kind,
                "target_ref": target_ref,
                "repo_owner": repo_owner,
                "repo_name": repo_name,
                "pr_number": pr_number,
                "result_sha": result_sha,
                "error": error[:1200],
                "payload": payload or {},
            },
        )

    def _url_for_scope(self, session_scope: str) -> str:
        scope = session_scope.strip()
        if scope in {"", "tank", "default", "tank-operator"}:
            return self._tank_operator_url
        return f"http://tank-operator.{scope}.svc:80"

    def _append(self, bearer: str, *, tank_operator_url: str, session_id: str, body: dict[str, Any]) -> None:
        url = f"{tank_operator_url}/api/internal/sessions/{session_id}/control-actions"
        try:
            r = httpx.post(
                url,
                json=body,
                headers={"Authorization": f"Bearer {bearer}"},
                timeout=10.0,
            )
            r.raise_for_status()
            record_audit_append(str(body.get("status") or ""), "ok")
        except Exception:
            record_audit_append(str(body.get("status") or ""), "audit_failed")
            log.exception(
                "control action audit append failed",
                extra={
                    "source_service": body.get("source_service"),
                    "source_tool": body.get("source_tool"),
                    "action": body.get("action"),
                    "status": body.get("status"),
                    "target_ref": body.get("target_ref"),
                },
            )
            raise
