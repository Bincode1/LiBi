from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from libi.agent.subagent_events import subagent_role
from libi.session import SessionEvent, SessionState, SessionStore
from libi.web.citations import ResearchSource, source_list

SUBAGENT_STATE_EVENTS = (
    "agent/child_started",
    "agent/child_activated",
    "agent/child_delivery_created",
    "agent/child_completed",
    "agent/child_failed",
    "agent/child_orphaned",
)
_COORDINATION_RESULTS = {
    "list_agents": "已读取子 agent 状态。",
    "send_message": "已向子 agent 提交追加指令。",
    "interrupt_agent": "已处理对子 agent 的停止请求。",
}


@dataclass(slots=True)
class ChildActivity:
    session_id: str
    label: str
    turn_id: str | None
    step_id: str | None
    tool_call_id: str | None
    sequence: int
    started_at: str
    role: str | None = None
    queued: bool = False
    result_count: int = 0
    last_delivery: str | None = None
    delivered_at: str | None = None
    report: str | None = None
    terminal: str | None = None
    terminal_reason: str | None = None
    ended_at: str | None = None


class SubagentCatalog:
    """Incrementally project the parent's durable child lifecycle."""

    def __init__(self) -> None:
        self.children: dict[str, ChildActivity] = {}

    def advance(self, state: SessionState, events: list[SessionEvent]) -> None:
        turn_id, step_id = state.active_turn_id, state.active_step_id
        for event in events:
            kind, payload = event.event_type, event.payload
            if kind == "turn/started":
                turn_id, step_id = event.turn_id, None
            elif kind in ("turn/completed", "turn/canceled", "turn/failed"):
                turn_id, step_id = None, None
            elif kind == "step/started":
                step_id = event.step_id
            elif kind in ("step/completed", "step/failed"):
                step_id = None
            if kind not in SUBAGENT_STATE_EVENTS:
                continue
            child_id = payload.get("child_id")
            if not isinstance(child_id, str) or not child_id:
                continue
            if kind == "agent/child_started":
                child_session_id = payload.get("child_session_id")
                if not isinstance(child_session_id, str) or not child_session_id:
                    continue
                self.children.setdefault(
                    child_id,
                    ChildActivity(
                        session_id=child_session_id,
                        label=str(payload.get("description") or "子 agent")[:160],
                        turn_id=event.turn_id or turn_id,
                        step_id=event.step_id or step_id,
                        tool_call_id=event.tool_call_id,
                        sequence=event.sequence,
                        started_at=event.created_at,
                        role=subagent_role(payload.get("role")),
                        queued=payload.get("queued") is True,
                    ),
                )
            elif child := self.children.get(child_id):
                if kind == "agent/child_activated":
                    child.queued = False
                elif kind == "agent/child_delivery_created":
                    child.last_delivery = str(payload.get("result_status") or "")
                    if child.last_delivery in {"completed", "partial"} and isinstance(
                        payload.get("content"), str
                    ):
                        child.report = payload["content"]
                    sources = tuple(
                        source
                        for record in payload.get("sources", [])
                        if (source := ResearchSource.from_record(record)) is not None
                    )
                    if sources:
                        report = payload.get("content")
                        body = report if isinstance(report, str) else ""
                        child.report = f"{body}\n\n## 查阅来源\n\n{source_list(sources)}".lstrip()
                    if child.last_delivery == "completed":
                        child.result_count += 1
                        child.delivered_at = event.created_at
                else:
                    child.terminal = str(payload.get("status") or "unknown")
                    child.terminal_reason = payload.get("error_code")
                    child.ended_at = event.created_at

    def snapshot(
        self,
        store: SessionStore,
        parent_session_id: str,
        *,
        active_parent_turn: str | None,
        waiting_agents: set[str],
        only_session_id: str | None = None,
    ) -> list[dict[str, Any]]:
        if not self.children:
            return []
        sessions = {
            child.session_id: child
            for child in store.list_sessions(parent_session_id=parent_session_id, limit=None)
        }
        result = []
        for child_id, child in self.children.items():
            if only_session_id is not None and child.session_id != only_session_id:
                continue
            session = sessions.get(child.session_id)
            if session is None:
                continue
            owner_active = active_parent_turn is not None and child.turn_id == active_parent_turn
            idle_expired = child.terminal_reason == "CHILD_IDLE_EXPIRED" or (
                child.terminal == "timed_out"
                and child.last_delivery == "completed"
                and session.status == "completed"
            )
            status = (
                "completed" if idle_expired else _child_status(child, session.status, owner_active)
            )
            updated_at = child.ended_at or session.updated_at
            if not idle_expired and status in {"completed", "idle"} and child.delivered_at:
                updated_at = child.delivered_at
            tool_name = None
            if status == "running":
                if child_id in waiting_agents:
                    status = "approval"
                else:
                    turn_id, step_id = store.active_turn_step(child.session_id)
                    if turn_id and step_id:
                        tools = store.pending_tool_calls(child.session_id, turn_id, step_id)
                        if tools:
                            tool_name = str(store.redactor.clean(next(iter(tools.values()))))[:80]
            result.append(
                {
                    "id": child_id,
                    "session_id": child.session_id,
                    "label": child.label,
                    "role": child.role,
                    "parent_turn_id": child.turn_id,
                    "parent_step_id": child.step_id,
                    "tool_call_id": child.tool_call_id,
                    "sequence": child.sequence,
                    "status": status,
                    "result_count": child.result_count,
                    "started_at": child.started_at,
                    "updated_at": updated_at,
                    "closed": child.terminal is not None,
                    "tool_name": tool_name,
                    **({"close_reason": "idle_expired"} if idle_expired else {}),
                    **({"report": child.report} if only_session_id and child.report else {}),
                }
            )
        return result


def _child_status(child: ChildActivity, session_status: str, owner_active: bool) -> str:
    if child.terminal == "failed" and child.last_delivery == "partial":
        return "partial"
    if child.terminal == "orphaned":
        return "interrupted"
    if child.terminal in {"canceled", "disposed"}:
        if child.last_delivery == "completed" and session_status != "canceled":
            return "completed"
        if child.terminal == "disposed" and session_status == "completed":
            return "completed"
        return "canceled"
    if child.terminal is not None:
        return child.terminal
    if session_status in {"failed", "canceled"}:
        return session_status
    if session_status == "completed" and child.last_delivery == "completed":
        return "idle" if owner_active else "completed"
    if not owner_active:
        return "unknown"
    if child.queued and session_status == "created":
        return "queued"
    return "starting" if session_status == "created" else "running"


def conversation_event(event: SessionEvent, serialized: dict[str, Any]) -> dict[str, Any]:
    if event.event_type == "user/message" and event.source == "subagent-delivery":
        return {
            **serialized,
            "type": "agent/results_received",
            "payload": {"delivery_ids": event.payload.get("delivery_ids", [])},
        }
    if event.event_type == "tool/result":
        name = event.payload.get("name")
        if name in ("subagent", "send_message") and str(
            event.payload.get("content") or ""
        ).startswith("Child agent results:\n"):
            return {**serialized, "payload": {"name": name, "content": "已收到子 agent 的结果。"}}
        if (
            isinstance(name, str)
            and name in _COORDINATION_RESULTS
            and not event.payload.get("is_error")
        ):
            return {**serialized, "payload": {"name": name, "content": _COORDINATION_RESULTS[name]}}
    return serialized
