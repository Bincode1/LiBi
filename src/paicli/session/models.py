from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from paicli.types import Message


@dataclass(slots=True, frozen=True)
class SessionEvent:
    event_id: str
    sequence: int
    session_id: str
    turn_id: str | None
    step_id: str | None
    tool_call_id: str | None
    approval_id: str | None
    event_type: str
    source: str
    payload: dict[str, Any]
    created_at: str
    schema_version: int = 1


@dataclass(slots=True, frozen=True)
class SessionState:
    session_id: str
    status: str
    last_sequence: int
    active_turn_id: str | None = None
    active_step_id: str | None = None
    latest_compaction_id: str | None = None


@dataclass(slots=True, frozen=True)
class PromptSection:
    section_id: str
    kind: str
    scope: str
    priority: int
    content: str
    source: str
    version: str
    stable: bool
    template: bool = False


@dataclass(slots=True, frozen=True)
class RuntimeContextSnapshot:
    context_id: str
    source: str
    scope: str
    version: str
    content: dict[str, Any]
    visibility: str


@dataclass(slots=True, frozen=True)
class ContextView:
    session_id: str
    turn_id: str
    step_id: str
    projection_version: str
    system_prompt: str
    messages: tuple[Message, ...]
    tools: tuple[dict[str, Any], ...]
    runtime_contexts: tuple[RuntimeContextSnapshot, ...]
    tool_history: tuple[dict[str, Any], ...]
    source_sequences: tuple[int, ...]
    compaction_id: str | None
    context_hash: str
    through_sequence: int = 0


@dataclass(slots=True, frozen=True)
class LlmRequest:
    request_id: str
    session_id: str
    turn_id: str
    step_id: str
    model: str
    provider: str
    system_prompt: str
    messages: tuple[Message, ...]
    tools: tuple[dict[str, Any], ...]
    max_tokens: int
    temperature: float
    metadata: dict[str, Any]
    context_hash: str
