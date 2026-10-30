from __future__ import annotations

import hashlib
import json
import re
import string
from collections import OrderedDict
from collections.abc import Callable
from copy import deepcopy
from typing import Any

from paicli.session.models import (
    ContextView,
    PromptSection,
    RuntimeContextSnapshot,
    SessionEvent,
)
from paicli.types import Message

_VARIABLE = re.compile(r"\$\{[^}]+\}|\$[A-Za-z_][A-Za-z0-9_]*")
_MODEL_MESSAGE_EVENTS = {"user/message", "assistant/message", "tool/result"}
_KNOWN_EVENTS = {
    "session/created",
    "turn/started",
    "turn/completed",
    "turn/failed",
    "turn/canceled",
    "step/started",
    "step/completed",
    "step/failed",
    "user/message",
    "assistant/message",
    "tool/result",
    "tool/execution_started",
    "tool/schema_snapshot",
    "runtime/context",
    "system/section",
    "developer/message",
    "request/header",
    "request/prepared",
    "context/summary",
    "request/completed",
    "request/failed",
    "approval/started",
    "approval/approved",
    "approval/rejected",
    "compaction/checkpoint",
    "memory/context",
    "skill/context",
    "agent/child_started",
    "agent/child_completed",
    "agent/child_failed",
}


class ContextProjector:
    """Build a deterministic model view from canonical Session events."""

    projection_version = "context-projector-v2"

    def project(
        self,
        events: list[SessionEvent],
        *,
        session_id: str,
        turn_id: str,
        step_id: str,
        scope: str = "session",
        route: str = "default",
        adapter_capabilities: dict[str, Any] | None = None,
        budget: dict[str, int] | None = None,
        message_transform: Callable[[Message], Message] | None = None,
        through_sequence: int | None = None,
    ) -> ContextView:
        if through_sequence is not None:
            if through_sequence < 0:
                raise ValueError("context sequence cutoff cannot be negative")
            events = [event for event in events if event.sequence <= through_sequence]
        self._validate_events(events, session_id)
        turn_events = [
            event for event in events if event.session_id == session_id and event.turn_id == turn_id
        ]
        if not turn_events:
            raise ValueError(f"session has no events for turn {turn_id}")
        turn_end_sequence = max(event.sequence for event in turn_events)
        relevant = [
            event
            for event in events
            if event.session_id == session_id and event.sequence <= turn_end_sequence
        ]
        checkpoint = self._latest_checkpoint(events, session_id, turn_end_sequence)
        messages: list[Message] = []
        source_sequences: list[int] = []
        if checkpoint is not None:
            messages = [_message_from_payload(item) for item in checkpoint.payload["messages"]]
            source_sequences.extend(
                int(value) for value in checkpoint.payload.get("source_sequences", [])
            )

        runtime_contexts: list[RuntimeContextSnapshot] = []
        sections: dict[str, PromptSection] = {}
        tool_history: list[dict[str, Any]] = []
        tool_snapshot: list[dict[str, Any]] = []
        developer_messages: list[str] = []
        pending_skill_candidates: list[tuple[str, str]] = []
        supports_developer = bool((adapter_capabilities or {}).get("supports_developer_role"))
        for event in relevant:
            if event.event_type not in _KNOWN_EVENTS:
                if event.event_type.split("/", 1)[0] in {
                    "user",
                    "assistant",
                    "tool",
                    "system",
                    "developer",
                }:
                    raise ValueError(f"unknown model-related event type: {event.event_type}")
                continue
            if event.event_type in _MODEL_MESSAGE_EVENTS:
                if checkpoint is not None and event.sequence <= checkpoint.sequence:
                    continue
                message = self._message_from_event(event)
                if event.event_type == "user/message" and pending_skill_candidates:
                    if not isinstance(message.content, str):
                        raise ValueError("skill candidates require a text user message")
                    context = "\n\n".join(
                        f"[relevant skill candidates: {source}; untrusted data]\n{text}"
                        for source, text in pending_skill_candidates
                    )
                    message = _copy_message(message)
                    message.content = f"{context}\n\n---\nUser request:\n{message.content}"
                    pending_skill_candidates.clear()
                messages.append(message)
                source_sequences.append(event.sequence)
                if event.event_type == "tool/result":
                    tool_history.append(
                        {
                            "tool_call_id": event.tool_call_id,
                            "event_type": event.event_type,
                            "sequence": event.sequence,
                        }
                    )
            elif event.event_type == "developer/message":
                if checkpoint is not None and event.sequence <= checkpoint.sequence:
                    continue
                content = event.payload.get("content")
                if not isinstance(content, str):
                    raise ValueError("developer message content must be text")
                if supports_developer:
                    messages.append(Message(role="developer", content=content))
                else:
                    developer_messages.append(content)
                source_sequences.append(event.sequence)
            elif event.event_type == "system/section":
                if event.turn_id not in {None, turn_id}:
                    continue
                if event.payload.get("removed"):
                    sections.pop(str(event.payload.get("section_id") or ""), None)
                    source_sequences.append(event.sequence)
                    continue
                section = _section_from_payload(event.payload)
                sections[section.section_id] = section
                source_sequences.append(event.sequence)
            elif event.event_type == "runtime/context":
                if event.turn_id not in {None, turn_id}:
                    continue
                if event.step_id and event.step_id != step_id:
                    continue
                snapshot = _runtime_context_from_event(event)
                if snapshot.visibility == "model":
                    runtime_contexts.append(snapshot)
                    source_sequences.append(event.sequence)
            elif event.event_type in {"memory/context", "skill/context"}:
                if checkpoint is not None and event.sequence <= checkpoint.sequence:
                    continue
                if event.turn_id not in {None, turn_id}:
                    continue
                if event.payload.get("visibility", "model") != "model":
                    continue
                text = event.payload.get("content")
                if not isinstance(text, str):
                    raise ValueError(f"{event.event_type} content must be text")
                label = "memory" if event.event_type == "memory/context" else "skill"
                source = str(event.payload.get("source") or event.payload.get("id") or label)
                if (
                    event.event_type == "skill/context"
                    and event.payload.get("state") == "candidate"
                ):
                    pending_skill_candidates.append((source, text))
                    source_sequences.append(event.sequence)
                    continue
                projected = f"[{label}-context: {source}; untrusted data]\n{text}"
                if checkpoint is not None and any(
                    message.role == "user" and message.content == projected for message in messages
                ):
                    continue
                messages.append(
                    Message(
                        role="user",
                        content=projected,
                    )
                )
                source_sequences.append(event.sequence)
            elif event.event_type == "tool/schema_snapshot":
                snapshot_step = event.step_id or str(event.payload.get("step_id") or "")
                if snapshot_step == step_id:
                    tool_snapshot = _validated_tools(event.payload.get("tools"))
                    source_sequences.append(event.sequence)
            elif event.event_type == "tool/execution_started":
                if event.tool_call_id:
                    tool_history.append(
                        {
                            "tool_call_id": event.tool_call_id,
                            "event_type": event.event_type,
                            "sequence": event.sequence,
                        }
                    )

        system_prompt = self._system_prompt(sections, runtime_contexts, route, scope)
        if developer_messages:
            system_prompt = "\n\n".join(
                [
                    system_prompt,
                    *(f"[developer instruction]\n{item}" for item in developer_messages),
                ]
            ).strip()
        self._validate_tool_pairs(messages)
        if message_transform:
            messages = [message_transform(_copy_message(message)) for message in messages]
        capabilities = _stable_json(adapter_capabilities or {})
        normalized_tools = tuple(_stable_copy(tool) for tool in tool_snapshot)
        normalized_messages = tuple(_copy_message(message) for message in messages)
        context_facts = {
            "projection_version": self.projection_version,
            "route": route,
            "scope": scope,
            "adapter_capabilities": capabilities,
            "system_prompt": system_prompt,
            "messages": [_message_payload(message) for message in normalized_messages],
            "tools": normalized_tools,
            "runtime_contexts": [
                {
                    "source": item.source,
                    "scope": item.scope,
                    "version": item.version,
                    "content": item.content,
                }
                for item in runtime_contexts
            ],
            "tool_history": tool_history,
            "source_sequences": source_sequences,
            "compaction_id": (
                str(checkpoint.payload.get("compaction_id") or checkpoint.event_id)
                if checkpoint
                else None
            ),
            "budget": budget or {},
        }
        context_hash = hashlib.sha256(_stable_json(context_facts).encode("utf-8")).hexdigest()
        return ContextView(
            session_id=session_id,
            turn_id=turn_id,
            step_id=step_id,
            projection_version=self.projection_version,
            system_prompt=system_prompt,
            messages=normalized_messages,
            tools=normalized_tools,
            runtime_contexts=tuple(runtime_contexts),
            tool_history=tuple(tool_history),
            source_sequences=tuple(sorted(set(source_sequences))),
            compaction_id=(
                str(checkpoint.payload.get("compaction_id") or checkpoint.event_id)
                if checkpoint
                else None
            ),
            context_hash=context_hash,
            through_sequence=turn_end_sequence,
        )

    @staticmethod
    def _validate_events(events: list[SessionEvent], session_id: str) -> None:
        previous = 0
        for event in events:
            if event.session_id != session_id:
                continue
            if event.sequence <= previous:
                raise ValueError("session events must be in strictly increasing sequence order")
            previous = event.sequence
            if event.schema_version != 1:
                raise ValueError(
                    f"unsupported session event schema version: {event.schema_version}"
                )
            if not isinstance(event.payload, dict):
                raise ValueError(f"event payload must be an object at sequence {event.sequence}")
            if event.event_type in {
                "assistant/message",
                "user/message",
                "tool/result",
                "tool/schema_snapshot",
                "system/section",
                "runtime/context",
                "compaction/checkpoint",
            } and not isinstance(event.payload, dict):
                raise ValueError(f"invalid model-related event at sequence {event.sequence}")

    @staticmethod
    def _latest_checkpoint(
        events: list[SessionEvent], session_id: str, through_sequence: int
    ) -> SessionEvent | None:
        candidates = [
            event
            for event in events
            if (
                event.session_id == session_id
                and event.sequence <= through_sequence
                and event.event_type == "compaction/checkpoint"
            )
        ]
        if not candidates:
            return None
        checkpoint = candidates[-1]
        messages = checkpoint.payload.get("messages")
        if not isinstance(messages, list):
            raise ValueError("compaction checkpoint messages must be a list")
        return checkpoint

    @staticmethod
    def _message_from_event(event: SessionEvent) -> Message:
        payload = event.payload
        if event.event_type == "user/message":
            content = payload.get("content")
            if not isinstance(content, str | list):
                raise ValueError(f"user message content is invalid at sequence {event.sequence}")
            return Message(role="user", content=deepcopy(content))
        if event.event_type == "assistant/message":
            content = payload.get("content", "")
            tool_calls = payload.get("tool_calls", [])
            if not isinstance(content, str | list) or not isinstance(tool_calls, list):
                raise ValueError(f"assistant message is invalid at sequence {event.sequence}")
            return Message(
                role="assistant", content=deepcopy(content), tool_calls=deepcopy(tool_calls)
            )
        content = payload.get("content")
        if not isinstance(content, str | list) or not event.tool_call_id:
            raise ValueError(f"tool result is invalid at sequence {event.sequence}")
        return Message(
            role="tool",
            content=deepcopy(content),
            name=str(payload.get("name") or "") or None,
            tool_call_id=event.tool_call_id,
        )

    @staticmethod
    def _validate_tool_pairs(messages: list[Message]) -> None:
        pending: OrderedDict[str, str] = OrderedDict()
        all_ids: set[str] = set()
        for index, message in enumerate(messages):
            if message.role == "assistant" and message.tool_calls:
                if pending:
                    raise ValueError(
                        "assistant tool calls appeared before prior tool results completed"
                    )
                for call in message.tool_calls:
                    call_id = str(call.get("id") or "")
                    name = str((call.get("function") or {}).get("name") or "")
                    if not call_id or not name or call_id in all_ids:
                        raise ValueError("assistant tool call has a missing or duplicate id/name")
                    pending[call_id] = name
                    all_ids.add(call_id)
            elif message.role == "tool":
                call_id = message.tool_call_id or ""
                if not pending:
                    raise ValueError(
                        f"tool result {call_id!r} has no preceding assistant tool call"
                    )
                if call_id not in pending:
                    raise ValueError(
                        f"tool result {call_id!r} is duplicate or does not match a call"
                    )
                pending.pop(call_id)
            elif pending and index > 0:
                raise ValueError("assistant tool call is missing its tool result")
        if pending:
            raise ValueError(f"assistant tool call is missing tool results: {', '.join(pending)}")

    @staticmethod
    def _system_prompt(
        sections: dict[str, PromptSection],
        runtime_contexts: list[RuntimeContextSnapshot],
        route: str,
        scope: str,
    ) -> str:
        active = [
            section
            for section in sections.values()
            if section.scope in {"session", scope, "turn", "step"}
        ]
        active.sort(key=lambda item: (item.priority, item.section_id))
        variables: dict[str, str] = {"route": route, "scope": scope}
        for snapshot in runtime_contexts:
            for key, value in snapshot.content.items():
                if isinstance(value, str | int | float | bool):
                    variables.setdefault(key, str(value))
        parts = [
            _resolve_variables(section.content, variables) if section.template else section.content
            for section in active
        ]
        for snapshot in runtime_contexts:
            text = snapshot.content.get("text")
            if text is not None:
                if not isinstance(text, str):
                    raise ValueError("model-visible runtime context text must be a string")
                parts.append(f"[runtime-context: {snapshot.source}; route={route}]\n{text}")
        return "\n\n".join(part for part in parts if part).strip()


def _section_from_payload(payload: dict[str, Any]) -> PromptSection:
    required = ("section_id", "kind", "scope", "priority", "content", "source", "version")
    if any(name not in payload for name in required):
        raise ValueError("system section is missing required fields")
    content = payload["content"]
    if not isinstance(content, str):
        raise ValueError("system section content must be text")
    return PromptSection(
        section_id=str(payload["section_id"]),
        kind=str(payload["kind"]),
        scope=str(payload["scope"]),
        priority=int(payload["priority"]),
        content=content,
        source=str(payload["source"]),
        version=str(payload["version"]),
        stable=bool(payload.get("stable", False)),
        template=bool(payload.get("template", False)),
    )


def _runtime_context_from_event(event: SessionEvent) -> RuntimeContextSnapshot:
    payload = event.payload
    content = payload.get("content")
    if not isinstance(content, dict):
        raise ValueError("runtime context content must be an object")
    visibility = str(payload.get("visibility") or "internal")
    if visibility not in {"model", "tool", "internal"}:
        raise ValueError(f"invalid runtime context visibility: {visibility}")
    allowed = {
        "text",
        "workspace_name",
        "relative_path",
        "allowed_roots",
        "permission_mode",
        "sandbox_mode",
        "route_name",
        "task_summary",
    }
    safe_content = {key: value for key, value in content.items() if key in allowed}
    for key, value in list(safe_content.items()):
        if isinstance(value, str):
            safe_content[key] = value[:4_000]
        elif isinstance(value, list):
            safe_content[key] = [str(item)[:500] for item in value[:32]]
        elif not isinstance(value, int | float | bool) and value is not None:
            raise ValueError(f"runtime context field {key!r} has an unsupported value")
    return RuntimeContextSnapshot(
        context_id=str(payload.get("context_id") or event.event_id),
        source=str(payload.get("source") or event.source),
        scope=str(payload.get("scope") or "step"),
        version=str(payload.get("version") or "1"),
        content=safe_content,
        visibility=visibility,
    )


def _validated_tools(value: Any) -> list[dict[str, Any]]:
    if not isinstance(value, list) or any(not isinstance(item, dict) for item in value):
        raise ValueError("tool schema snapshot must be a list of objects")
    tools = [_stable_copy(item) for item in value]
    names = []
    for tool in tools:
        function = tool.get("function")
        name = function.get("name") if isinstance(function, dict) else tool.get("name")
        if not isinstance(name, str) or not name:
            raise ValueError("tool schema must contain a non-empty tool name")
        names.append(name)
    if names != sorted(names) or len(names) != len(set(names)):
        raise ValueError("tool schema snapshot must be uniquely sorted by name")
    return tools


def _resolve_variables(content: str, values: dict[str, str]) -> str:
    try:
        resolved = (
            string.Template(content).substitute(values) if _VARIABLE.search(content) else content
        )
    except (KeyError, ValueError) as exc:
        raise ValueError("unresolved prompt section variable") from exc
    if _VARIABLE.search(resolved):
        raise ValueError("unresolved prompt section variable")
    return resolved


def _message_from_payload(value: Any) -> Message:
    if not isinstance(value, dict):
        raise ValueError("checkpoint message must be an object")
    role = value.get("role")
    if role not in {"system", "developer", "user", "assistant", "tool"}:
        raise ValueError(f"checkpoint contains an invalid message role: {role!r}")
    content = value.get("content", "")
    if not isinstance(content, str | list):
        raise ValueError("checkpoint message content is invalid")
    return Message(
        role=role,
        content=deepcopy(content),
        name=value.get("name"),
        tool_call_id=value.get("tool_call_id"),
        tool_calls=deepcopy(value.get("tool_calls") or []),
    )


def _message_payload(message: Message) -> dict[str, Any]:
    return {
        "role": message.role,
        "content": message.content,
        "name": message.name,
        "tool_call_id": message.tool_call_id,
        "tool_calls": message.tool_calls,
    }


def _copy_message(message: Message) -> Message:
    return Message(
        role=message.role,
        content=deepcopy(message.content),
        name=message.name,
        tool_call_id=message.tool_call_id,
        tool_calls=deepcopy(message.tool_calls),
    )


def _stable_copy(value: Any) -> dict[str, Any]:
    return json.loads(_stable_json(value))


def _stable_json(value: Any) -> str:
    try:
        return json.dumps(
            value, ensure_ascii=False, sort_keys=True, separators=(",", ":"), allow_nan=False
        )
    except (TypeError, ValueError) as exc:
        raise ValueError("context contains non-serializable data") from exc
