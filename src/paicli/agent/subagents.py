from __future__ import annotations

import asyncio
import json
import sqlite3
import time
import uuid
from collections.abc import Callable
from contextlib import asynccontextmanager
from copy import deepcopy
from dataclasses import asdict, dataclass, field
from enum import StrEnum
from pathlib import Path
from typing import Any, Protocol

from paicli.agent.events import OutputLimitError
from paicli.agent.subagent_events import (
    SUBAGENT_ROLES,
    ObservationBuffer,
    public_event,
    subagent_role,
)
from paicli.context import CompactionError
from paicli.policy.redaction import Redactor
from paicli.tools.base import Tool, ToolContext, ToolResult, object_schema
from paicli.tools.call_parsing import tool_input


class ActivationState(StrEnum):
    STARTING = "starting"
    RUNNING = "running"
    STOPPING = "stopping"
    IDLE_WAITING = "idle_waiting"
    COMPLETED = "completed"
    FAILED = "failed"
    CANCELED = "canceled"
    TIMED_OUT = "timed_out"
    DISPOSED = "disposed"


_LIVE_STATES = {
    ActivationState.STARTING,
    ActivationState.RUNNING,
    ActivationState.STOPPING,
}
_RESIDENT_STATES = _LIVE_STATES | {ActivationState.IDLE_WAITING}
_TERMINAL_STATES = {
    ActivationState.COMPLETED,
    ActivationState.FAILED,
    ActivationState.CANCELED,
    ActivationState.TIMED_OUT,
    ActivationState.DISPOSED,
}
_ALLOWED_TRANSITIONS = {
    ActivationState.STARTING: {ActivationState.RUNNING, ActivationState.STOPPING},
    ActivationState.RUNNING: {
        ActivationState.IDLE_WAITING,
        ActivationState.STOPPING,
        *_TERMINAL_STATES,
    },
    ActivationState.IDLE_WAITING: {ActivationState.RUNNING, ActivationState.STOPPING},
    ActivationState.STOPPING: _TERMINAL_STATES,
}


@dataclass(frozen=True, slots=True)
class ProviderCapabilities:
    continuable: bool = False
    queue_idle: bool = False
    steer_running: bool = False
    supports_cancel: bool = False
    supports_child_options: bool = False
    supports_structured_output: bool = False
    supports_live_events: bool = False
    external_process: bool = False
    agent_options: bool = False
    agent_preset: bool = False
    output_schema: bool = False
    depth_limit: bool = False


@dataclass(frozen=True, slots=True)
class SubagentResult:
    output: str
    stop_reason: str
    diagnostic: str | None = None
    structured: Any | None = None


def _bounded_diagnostic(value: str | None, *, limit: int = 4096) -> str | None:
    if value is None:
        return None
    encoded = value.encode("utf-8")
    if len(encoded) <= limit:
        return value
    suffix = b"\n[diagnostic truncated]"
    prefix = encoded[: max(0, limit - len(suffix))]
    while prefix:
        try:
            text = prefix.decode("utf-8")
            break
        except UnicodeDecodeError:
            prefix = prefix[:-1]
    else:
        text = ""
    return text + suffix.decode("ascii")


class SubagentProvider(Protocol):
    """Provider seam for local continuable and external one-shot runtimes."""

    provider_name: str
    capabilities: ProviderCapabilities

    async def run(
        self,
        manager: SubagentManager,
        activation: Activation,
        payload: dict[str, Any],
        context: ToolContext,
    ) -> str | SubagentResult: ...

    async def interrupt(self, activation: Activation) -> None: ...

    async def dispose(self, activation: Activation) -> None: ...


class LocalAgentProvider:
    provider_name = "local"
    capabilities = ProviderCapabilities(
        continuable=True,
        queue_idle=True,
        steer_running=True,
        supports_cancel=True,
        supports_live_events=True,
    )

    async def run(
        self,
        manager: SubagentManager,
        activation: Activation,
        payload: dict[str, Any],
        context: ToolContext,
    ) -> str | SubagentResult:
        return await manager._execute_local(activation.id, activation.session_id, payload, context)

    async def interrupt(self, activation: Activation) -> None:
        if not activation.task.done() and not activation.task.cancelling():
            activation.task.cancel()

    async def dispose(self, activation: Activation) -> None:
        return None


class ProviderRegistry:
    """Explicit provider registration boundary used by admission."""

    def __init__(self) -> None:
        self._providers: dict[str, SubagentProvider] = {}

    def register(self, provider: SubagentProvider) -> None:
        name = str(provider.provider_name).strip()
        if not name:
            raise ValueError("provider_name cannot be empty")
        if name in self._providers:
            raise ValueError(f"provider already registered: {name}")
        self._providers[name] = provider

    def get(self, name: str) -> SubagentProvider | None:
        return self._providers.get(name)

    def names(self) -> list[str]:
        return sorted(self._providers)


@dataclass(slots=True)
class Activation:
    id: str
    parent_id: str
    session_id: str
    description: str
    task: asyncio.Task[str | SubagentResult]
    parent_session_id: str
    started_at: float = field(default_factory=time.monotonic)
    status: ActivationState = ActivationState.STARTING
    result: str = ""
    delivered: bool = False
    delivery_id: str | None = None
    timeout_task: asyncio.Task[None] | None = None
    deadline_seconds: float | None = None
    settled: bool = False
    error_code: str | None = None
    inbox: asyncio.Queue[str] = field(default_factory=asyncio.Queue)
    result_available: bool = False
    capabilities: ProviderCapabilities = field(default_factory=ProviderCapabilities)
    policy_snapshot: dict[str, Any] = field(default_factory=dict)
    last_activity_at: float = field(default_factory=time.monotonic)
    config_snapshot: Any | None = None
    provider: str = "unknown"
    current_phase: str = "starting"
    usage: dict[str, int] = field(default_factory=dict)
    cleanup_error: str | None = None
    provider_diagnostic: str | None = None
    final_result: SubagentResult | None = None
    provider_adapter: SubagentProvider | None = None
    provider_disposed: bool = False
    run_id: str = field(default_factory=lambda: f"run_{uuid.uuid4().hex}")
    event_sequence: int = 0
    stream_buffers: dict[str, str] = field(default_factory=dict)
    hidden_channels: set[str] = field(default_factory=set)
    completed_runs: set[str] = field(default_factory=set)
    delivery_run_id: str | None = None
    ended_published: bool = False
    waiting_tools: set[str] = field(default_factory=set)
    pending_tools: dict[str, str] = field(default_factory=dict)
    last_tool_result: str | None = None
    diagnostic: dict[str, Any] | None = None
    cleanup_diagnostics: list[dict[str, Any]] = field(default_factory=list)
    finished_at: float | None = None


@dataclass(slots=True)
class PersistedDelivery:
    delivery_id: str
    parent_id: str
    parent_session_id: str
    child_id: str
    status: str
    result: str
    error_code: str | None = None


class SubagentManager:
    """Owns delegated Agent tasks, their capacity slots, and parent notifications."""

    def __init__(
        self,
        *,
        max_active: int = 8,
        max_resident: int = 32,
        max_depth: int = 2,
        default_deadline: float | None = None,
        event_queue_size: int = 512,
        drain_timeout: float = 15.0,
    ) -> None:
        self.max_active = max_active
        self.max_resident = max(max_active, max_resident)
        self.max_depth = max_depth
        self.activations: dict[str, Activation] = {}
        self.deliveries: dict[str, PersistedDelivery] = {}
        self.recovered_children: dict[str, list[dict[str, Any]]] = {}
        self.catalogs: dict[str, list[dict[str, Any]]] = {}
        self.providers = ProviderRegistry()
        self.providers.register(LocalAgentProvider())
        self.depths: dict[str, int] = {"root": 0}
        self._condition = asyncio.Condition()
        self._child_locks: dict[str, asyncio.Lock] = {}
        self._closed = False
        self.session_store: Any | None = None
        self._settlers: set[asyncio.Task[None]] = set()
        self._events: dict[str, ObservationBuffer] = {}
        self._callback_buffer = ObservationBuffer(max(32, event_queue_size))
        self._observer_task: asyncio.Task[None] | None = None
        self._observer_stopping = False
        self._callback_busy = False
        self.redactor = Redactor()
        self.default_deadline = default_deadline
        self.event_queue_size = max(32, event_queue_size)
        self.drain_timeout = max(0.1, drain_timeout)
        self.event_callback: Any | None = None
        self.child_preview_callback: Callable[[dict[str, Any]], None] | None = None
        self.observation_error: Exception | None = None
        self.settlement_error: Exception | None = None

    @staticmethod
    def transition(activation: Activation, target: ActivationState) -> bool:
        """Apply one legal lifecycle edge; terminal activations cannot be revived."""
        current = activation.status
        if current == target:
            return False
        if current in _TERMINAL_STATES or target not in _ALLOWED_TRANSITIONS.get(current, set()):
            raise RuntimeError(f"illegal subagent transition: {current} -> {target}")
        activation.status = target
        return True

    def register_provider(self, provider: SubagentProvider) -> None:
        self.providers.register(provider)

    async def start(self, payload: dict[str, Any], context: ToolContext) -> ToolResult:
        parent_id = context.agent_id or "root"
        if self._closed:
            return ToolResult("PARENT_CLOSED: subagent manager is shutting down.", is_error=True)
        provider_name = str(payload.get("provider") or "local")
        provider = self.providers.get(provider_name)
        if provider is None:
            return ToolResult(
                "PROVIDER_UNAVAILABLE: requested subagent provider is not registered.",
                is_error=True,
            )
        if payload.get("continuation") and not provider.capabilities.continuable:
            return ToolResult("CONTINUATION_UNSUPPORTED: provider is one-shot.", is_error=True)
        capability_options = {
            "agent_options": "agent_options",
            "agent_preset": "agent_preset",
            "output_schema": "output_schema",
            "max_depth": "depth_limit",
        }
        for option, capability in capability_options.items():
            if option in payload and not getattr(provider.capabilities, capability):
                return ToolResult(
                    f"PROVIDER_OPTION_UNSUPPORTED: {option} is not supported by {provider_name}.",
                    is_error=True,
                )
        requested_cwd = Path(str(payload.get("cwd") or context.cwd)).expanduser().resolve()
        allowed_cwd = Path(context.cwd).expanduser().resolve()
        if not requested_cwd.is_dir():
            return ToolResult(
                "INVALID_CWD: subagent cwd does not exist or is not a directory.",
                is_error=True,
            )
        if requested_cwd != allowed_cwd and allowed_cwd not in requested_cwd.parents:
            return ToolResult(
                "INVALID_CWD: subagent cwd must stay inside the current workspace.",
                is_error=True,
            )
        if self.depths.get(parent_id, 0) >= self.max_depth:
            return ToolResult("DEPTH_LIMIT_REACHED: subagent depth limit reached.", is_error=True)
        deadline = payload.get("timeout", self.default_deadline)
        try:
            deadline_seconds = None if deadline is None else float(deadline)
        except (TypeError, ValueError):
            return ToolResult("INVALID_DEADLINE: timeout must be a positive number.", is_error=True)
        if deadline_seconds is not None and deadline_seconds <= 0:
            return ToolResult("INVALID_DEADLINE: timeout must be a positive number.", is_error=True)
        active_count = sum(item.status in _LIVE_STATES for item in self.activations.values())
        if active_count >= self.max_active:
            return ToolResult(
                "ACTIVATION_LIMIT_REACHED: active subagent capacity reached.",
                is_error=True,
            )
        resident_count = sum(item.status in _RESIDENT_STATES for item in self.activations.values())
        if resident_count >= self.max_resident:
            return ToolResult(
                "RESIDENT_ACTIVATION_LIMIT_REACHED: parked child capacity reached.",
                is_error=True,
            )
        store, parent_session_id = context.session_store, context.session_id
        if store is None or not parent_session_id:
            return ToolResult(
                "SESSION_PERSISTENCE_UNAVAILABLE: subagents require a session store.",
                is_error=True,
            )

        store.redactor.add_sensitive_values((context.config.llm.api_key,))
        agent_id = f"agent_{uuid.uuid4().hex[:12]}"
        description = store.redactor.clean(str(payload["description"]))
        depth = self.depths.get(parent_id, 0) + 1
        config_snapshot = deepcopy(context.config)
        policy_snapshot = {
            "hitl_mode": config_snapshot.policy.hitl_mode,
            "path_guard_enabled": config_snapshot.policy.path_guard_enabled,
            "command_guard_enabled": config_snapshot.policy.command_guard_enabled,
            "subagent_tool_allowlist": config_snapshot.policy.subagent_tool_allowlist,
            "cwd": str(requested_cwd),
        }
        role = subagent_role(payload.get("role"))
        descriptor = {
            "child_id": agent_id,
            "parent_id": parent_id,
            "description": description,
            "cwd": str(requested_cwd),
            "provider": provider_name,
            "depth": depth,
            "deadline_seconds": deadline_seconds,
            "capabilities": asdict(provider.capabilities),
            "policy_snapshot": policy_snapshot,
        }
        if role:
            descriptor["role"] = role
        try:
            session_id = store.start_child_session(
                parent_session_id,
                descriptor,
                turn_id=context.turn_id,
                step_id=context.step_id,
                tool_call_id=context.tool_call_id,
            )
        except (OSError, sqlite3.Error) as exc:
            return ToolResult(
                f"SESSION_PERSISTENCE_UNAVAILABLE: {type(exc).__name__}",
                is_error=True,
            )

        self.session_store = store
        self.redactor = store.redactor
        self.depths[agent_id] = depth
        activation: Activation

        async def execute_provider() -> str | SubagentResult:
            await asyncio.sleep(0)
            return await provider.run(self, activation, payload, context)

        task = asyncio.create_task(execute_provider())
        activation = Activation(
            agent_id,
            parent_id,
            session_id,
            description,
            task,
            parent_session_id,
            deadline_seconds=deadline_seconds,
            provider=provider_name,
            provider_adapter=provider,
            capabilities=provider.capabilities,
            policy_snapshot=policy_snapshot,
            config_snapshot=config_snapshot,
        )
        self.activations[agent_id] = activation
        self.transition(activation, ActivationState.RUNNING)
        self._publish_event(
            {
                "type": "subagent_event",
                "event": {
                    "type": "started",
                    "description": activation.description,
                },
                "agent": agent_id,
                "parent_id": parent_id,
                "agent_description": activation.description,
            }
        )
        task.add_done_callback(self._schedule_settlement(agent_id))
        if deadline_seconds is not None:
            activation.timeout_task = asyncio.create_task(
                self._deadline_watch(agent_id, deadline_seconds)
            )
        return ToolResult(
            f"Started subagent {agent_id} (state=running, deadline={deadline_seconds or 'none'})."
        )

    async def _deadline_watch(self, agent_id: str, deadline: float) -> None:
        await asyncio.sleep(deadline)
        async with self._child_lock(agent_id):
            activation = self.activations.get(agent_id)
            if activation is None or activation.status not in {
                ActivationState.STARTING,
                ActivationState.RUNNING,
                ActivationState.IDLE_WAITING,
            }:
                return
            activation.error_code = (
                "CHILD_IDLE_EXPIRED"
                if activation.status == ActivationState.IDLE_WAITING
                else "CHILD_DEADLINE_EXCEEDED"
            )
            self.transition(activation, ActivationState.STOPPING)
        await self._interrupt_provider(activation)
        await asyncio.gather(activation.task, return_exceptions=True)
        await self._wait_for_settlement(activation)

    def _schedule_settlement(self, agent_id: str):
        def schedule(done: asyncio.Task[str]) -> None:
            settlement = asyncio.create_task(self._settle(agent_id, done))
            self._settlers.add(settlement)
            settlement.add_done_callback(self._observe_settlement)

        return schedule

    def _observe_settlement(self, task: asyncio.Task[None]) -> None:
        self._settlers.discard(task)
        if task.cancelled():
            return
        error = task.exception()
        if error is not None:
            self.settlement_error = error

    def raise_background_errors(self, *, include_observation: bool = True) -> None:
        if self.settlement_error is not None:
            raise RuntimeError(
                "Subagent persistence or cleanup failed; inspect child status."
            ) from None
        if include_observation and self.observation_error is not None:
            self.observation_error = None
            raise RuntimeError("Subagent observation callback failed.") from None

    def _offer_event(self, event: dict[str, Any]) -> None:
        parent_id = str(event.get("parent_id") or "root")
        queue = self._events.setdefault(parent_id, ObservationBuffer(self.event_queue_size))
        queue.put_nowait(event)

    def _publish_event(self, event: dict[str, Any]) -> None:
        activation = self.activations.get(str(event.get("agent") or ""))
        inner = event.get("event")
        kind = inner.get("type") if isinstance(inner, dict) else None
        if activation is not None:
            if kind == "turn_completed":
                if activation.run_id in activation.completed_runs:
                    return
                activation.completed_runs.add(activation.run_id)
            if kind == "ended":
                if activation.ended_published:
                    return
                activation.ended_published = True
            activation.event_sequence += 1
            event = {
                **event,
                "run_id": activation.run_id,
                "seq": activation.event_sequence,
                "session_id": activation.session_id,
                "agent_description": activation.description,
            }
        event = public_event(event, self.redactor)
        self._offer_event(event)
        if self.event_callback is not None and not self._observer_stopping:
            self._callback_buffer.put_nowait(event)
            if self._observer_task is None or self._observer_task.done():
                self._observer_task = asyncio.create_task(self._observe_callbacks())

    def _gap_event(self, buffer: ObservationBuffer, parent_id: str | None = None):
        dropped, coalesced = buffer.take_counts()
        if not dropped:
            return None
        snapshots = [
            self.describe_activation(item)
            for item in self.activations.values()
            if parent_id is None or item.parent_id == parent_id
        ]
        return public_event(
            {
                "parent_id": parent_id or "root",
                "event": {
                    "type": "observation_gap",
                    "dropped": dropped,
                    "coalesced": coalesced,
                    "snapshots": snapshots,
                },
            },
            self.redactor,
        )

    async def _observe_callbacks(self) -> None:
        while not self._observer_stopping or not self._callback_buffer.empty():
            try:
                # Stay in this task so queue removal and the busy flag cannot be interleaved.
                async with asyncio.timeout(1.0):
                    event = await self._callback_buffer.get()
            except TimeoutError:
                for activation in self.activations.values():
                    if activation.status in _RESIDENT_STATES:
                        self._publish_event(
                            {
                                "agent": activation.id,
                                "parent_id": activation.parent_id,
                                "event": {
                                    "type": "status",
                                    "snapshot": self.describe_activation(activation),
                                },
                            }
                        )
                continue
            callback = self.event_callback
            if callback is None:
                continue
            try:
                self._callback_busy = True
                gap = self._gap_event(self._callback_buffer)
                for item in [gap, event] if gap else [event]:
                    # Terminal rendering may block; serialize it off the Agent event loop.
                    result = await asyncio.to_thread(callback, deepcopy(item))
                    if asyncio.iscoroutine(result):
                        await result
            except Exception as exc:
                self.observation_error = RuntimeError(
                    self.redactor.exception(exc, "Observation failed")
                )
                self.event_callback = None
                return
            finally:
                self._callback_busy = False

    async def flush_observations(self) -> None:
        while not self._callback_buffer.empty() or self._callback_busy:
            if self._observer_task is None or self._observer_task.done():
                break
            await asyncio.sleep(0.001)

    def _phase(self, activation: Activation, phase: str) -> None:
        if activation.current_phase == phase:
            return
        activation.current_phase = phase
        self._publish_event(
            {
                "agent": activation.id,
                "parent_id": activation.parent_id,
                "event": {
                    "type": "phase_changed",
                    "phase": phase,
                    "waiting_reason": "approval" if activation.waiting_tools else None,
                },
            }
        )

    async def tool_status(self, agent_id: str, event: dict[str, Any]) -> None:
        activation = self.activations.get(agent_id)
        if activation is None:
            return
        activation.last_activity_at = time.monotonic()
        call_id = event["tool_call_id"]
        kind = event["type"]
        if kind == "approval_waiting":
            activation.waiting_tools.add(call_id)
        elif kind == "approval_resolved":
            activation.waiting_tools.discard(call_id)
        elif kind == "tool_started":
            activation.pending_tools[call_id] = event["tool_name"]
        elif kind == "tool_finished":
            activation.pending_tools.pop(call_id, None)
            activation.waiting_tools.discard(call_id)
            activation.last_tool_result = self.redactor.clean(event.get("summary", ""))[:500]
        phase = (
            "approval"
            if activation.waiting_tools
            else "tool"
            if activation.pending_tools
            else "model"
        )
        activation.current_phase = phase
        self._publish_event(
            {
                "agent": activation.id,
                "parent_id": activation.parent_id,
                "event": {
                    "type": "phase_changed",
                    "phase": phase,
                    "tool_name": event["tool_name"],
                    "tool_call_id": call_id,
                    "waiting_reason": "approval" if activation.waiting_tools else None,
                },
            }
        )

    def _cleanup_failure(self, activation: Activation, exc: Exception, action: str) -> None:
        diagnostic = {
            "code": "CHILD_CLEANUP_FAILED",
            "category": "cleanup",
            "message": self.redactor.exception(exc, action),
            "exception_type": type(exc).__name__,
            "retryable": False,
        }
        activation.cleanup_diagnostics.append(diagnostic)
        activation.cleanup_error = _bounded_diagnostic(
            "; ".join(item["message"] for item in activation.cleanup_diagnostics)
        )
        self.settlement_error = RuntimeError(diagnostic["message"])

    def _flush_streams(self, activation: Activation) -> None:
        for channel, text in activation.stream_buffers.items():
            if channel in activation.hidden_channels:
                text = "[response hidden: public output buffer limit exceeded]"
            if text:
                key = "text" if channel == "text_delta" else "thinking"
                self._publish_event(
                    {
                        "type": "subagent_event",
                        "agent": activation.id,
                        "parent_id": activation.parent_id,
                        "agent_description": activation.description,
                        "event": {"type": channel, key: text},
                    }
                )
        activation.stream_buffers.clear()
        activation.hidden_channels.clear()

    def _create_delivery(self, activation: Activation, status: str, result: str) -> None:
        if self.session_store is None or not activation.parent_session_id:
            raise RuntimeError("Child results require parent session persistence.")
        # A run may publish its result while entering the parked state. Settlement
        # of the same run must not publish a second parent notice.
        if activation.delivery_run_id == activation.run_id:
            return
        delivery_id = f"delivery_{uuid.uuid4().hex}"
        result = self.redactor.clean(result)
        self.session_store.append(
            activation.parent_session_id,
            "agent/child_delivery_created",
            {
                "delivery_id": delivery_id,
                "child_id": activation.id,
                "run_id": activation.run_id,
                "status": "pending",
                "result_status": status,
                "error_code": activation.error_code,
                "content": result,
                "diagnostic": activation.diagnostic,
                "cleanup_diagnostics": activation.cleanup_diagnostics,
            },
            source="subagent-manager",
        )
        self.deliveries[delivery_id] = PersistedDelivery(
            delivery_id,
            activation.parent_id,
            activation.parent_session_id,
            activation.id,
            status,
            result,
            activation.error_code,
        )
        activation.delivery_id = delivery_id
        activation.delivery_run_id = activation.run_id
        activation.result_available = True
        activation.delivered = False

    def _recent_research_evidence(self, session_id: str) -> str:
        if self.session_store is None:
            return ""
        try:
            events = self.session_store.events(
                session_id, event_types=("turn/failed", "assistant/message", "tool/result")
            )
        except (OSError, sqlite3.Error):
            return ""
        if not events or events[-1].event_type != "turn/failed":
            return ""
        turn_id = events[-1].turn_id
        evidence = []
        sources = {}
        for event in events:
            if event.turn_id != turn_id:
                continue
            if event.event_type == "assistant/message":
                for call in event.payload.get("tool_calls", []):
                    if call.get("function", {}).get("name") == "web_fetch":
                        sources[call.get("id")] = tool_input(call).get("url", "")
            elif (
                event.event_type == "tool/result"
                and event.payload.get("name") in {"web_fetch", "web_search"}
                and not event.payload.get("is_error")
            ):
                content = str(event.payload.get("content") or "").strip()
                if content:
                    source = sources.get(event.tool_call_id)
                    label = f"Web evidence from {source}" if source else "Web evidence"
                    evidence.append(f"{label}:\n{content[:1500]}")
        return "\n\n".join(evidence[-4:])

    async def _execute_local(
        self,
        agent_id: str,
        session_id: str,
        payload: dict[str, Any],
        context: ToolContext,
    ) -> str:
        from paicli.agent.agent import Agent

        activation = self.activations[agent_id]

        async def forward(event: dict[str, Any]) -> None:
            activation.last_activity_at = time.monotonic()
            event_type = str(event.get("type") or "")
            if event_type in {"text_delta", "thinking_delta", "context_summary"}:
                self._phase(activation, "model")
            if event_type in {"text_delta", "thinking_delta"}:
                if event_type == "text_delta" and self.child_preview_callback is not None:
                    self.child_preview_callback(event)
                key = "text" if event_type == "text_delta" else "thinking"
                if event_type not in activation.hidden_channels:
                    text = activation.stream_buffers.get(event_type, "") + str(event.get(key) or "")
                    if len(text.encode("utf-8")) > 65536:
                        activation.hidden_channels.add(event_type)
                        text = ""
                    activation.stream_buffers[event_type] = text
                return
            if event_type == "error":
                activation.stream_buffers.clear()
                activation.hidden_channels.clear()
            elif event_type in {"turn_complete", "done"}:
                self._flush_streams(activation)
            # done contains the full raw history; public observation only needs its summary.
            if event_type == "done":
                return
            elif event_type == "error":
                event = {
                    "type": "error",
                    "error": self.redactor.exception(event["error"], "Child execution failed"),
                }
            self._publish_event(
                {
                    "type": "subagent_event",
                    "event": event,
                    "agent": agent_id,
                    "parent_id": activation.parent_id,
                    "agent_description": activation.description,
                }
            )

        child = Agent(
            llm_client=context.llm_client,
            tool_registry=context.tool_registry.clone(
                allowed_names=(
                    set(activation.policy_snapshot["subagent_tool_allowlist"])
                    if activation.policy_snapshot["subagent_tool_allowlist"] is not None
                    else None
                )
            ),
            config=activation.config_snapshot,
            cwd=activation.policy_snapshot["cwd"],
            approval_callback=context.approval_callback,
            session_store=context.session_store,
            session_id=session_id,
            subagent_manager=self,
            agent_id=agent_id,
            event_sink=forward,
            steer_inbox=activation.inbox,
        )
        prompt = str(payload["prompt"])
        while True:
            try:
                result = await child.run_complete(prompt)
            except BaseException:
                # A partial response may end inside a credential; never flush it raw.
                if activation.stream_buffers:
                    activation.stream_buffers.clear()
                    activation.hidden_channels.clear()
                    self._publish_event(
                        {
                            "type": "subagent_event",
                            "agent": agent_id,
                            "parent_id": activation.parent_id,
                            "event": {"type": "output_hidden", "reason": "response interrupted"},
                        }
                    )
                raise
            else:
                self._flush_streams(activation)
            activation.result = self.redactor.clean(result.text)
            usage = getattr(result, "usage", None)
            if usage is not None:
                for key, value in usage.to_dict().items():
                    activation.usage[key] = activation.usage.get(key, 0) + value
            activation.result_available = True
            activation.delivered = False
            if not activation.capabilities.continuable:
                return result.text
            async with self._child_lock(agent_id):
                self._create_delivery(activation, "completed", activation.result)
                if activation.inbox.empty():
                    self.transition(activation, ActivationState.IDLE_WAITING)
                    self._phase(activation, "idle")
            self._publish_event(
                {
                    "agent": activation.id,
                    "parent_id": activation.parent_id,
                    "event": {"type": "turn_completed", "result": activation.result},
                }
            )
            self._publish_event(
                {
                    "type": "subagent_event",
                    "event": {
                        "type": "idle"
                        if activation.status == ActivationState.IDLE_WAITING
                        else "status",
                        "snapshot": self.describe_activation(activation),
                    },
                    "agent": activation.id,
                    "parent_id": activation.parent_id,
                    "agent_description": activation.description,
                }
            )
            async with self._condition:
                self._condition.notify_all()
            prompt = await activation.inbox.get()
            async with self._child_lock(agent_id):
                if activation.status != ActivationState.RUNNING:
                    raise RuntimeError("Child continuation has no capacity reservation.")
                activation.run_id = f"run_{uuid.uuid4().hex}"
            self._phase(activation, "starting")
            activation.result_available = False
            self._publish_event(
                {
                    "type": "subagent_event",
                    "event": {"type": "continued"},
                    "agent": activation.id,
                    "parent_id": activation.parent_id,
                    "agent_description": activation.description,
                }
            )

    @asynccontextmanager
    async def _child_lock(self, agent_id: str):
        lock = self._child_locks.setdefault(agent_id, asyncio.Lock())
        async with lock:
            yield

    async def _settle(self, agent_id: str, task: asyncio.Task[str | SubagentResult]) -> None:
        try:
            async with self._child_lock(agent_id):
                await self._settle_locked(agent_id, task)
        except Exception as exc:
            self.settlement_error = exc
            activation = self.activations[agent_id]
            self._cleanup_failure(activation, exc, "Settlement failed")
            activation.settled = True
            async with self._condition:
                self._condition.notify_all()
            raise

    async def _settle_locked(
        self,
        agent_id: str,
        task: asyncio.Task[str | SubagentResult],
    ) -> None:
        activation = self.activations[agent_id]
        if activation.settled:
            return
        delivery_status = None
        try:
            result = task.result()
            if isinstance(result, SubagentResult):
                activation.final_result = SubagentResult(
                    output=self.redactor.clean(result.output),
                    stop_reason=result.stop_reason,
                    diagnostic=_bounded_diagnostic(self.redactor.clean(result.diagnostic)),
                    structured=self.redactor.clean(result.structured),
                )
                activation.result = self.redactor.clean(result.output)
                activation.provider_diagnostic = activation.final_result.diagnostic
                if activation.status == ActivationState.STOPPING:
                    self.transition(activation, self._stopped_state(activation))
                elif result.stop_reason == "completed":
                    self.transition(activation, ActivationState.COMPLETED)
                elif result.stop_reason == "aborted":
                    activation.error_code = "PROVIDER_ABORTED"
                    self.transition(activation, ActivationState.CANCELED)
                elif result.stop_reason == "timeout":
                    activation.error_code = "CHILD_DEADLINE_EXCEEDED"
                    self.transition(activation, ActivationState.TIMED_OUT)
                else:
                    activation.error_code = "PROVIDER_EXECUTION_FAILED"
                    self.transition(activation, ActivationState.FAILED)
                    activation.result = activation.result or "Provider execution failed."
            elif activation.status == ActivationState.STOPPING:
                activation.result = self.redactor.clean(result or "Activation stopped.")
                self.transition(activation, self._stopped_state(activation))
            else:
                activation.result = self.redactor.clean(result)
                self.transition(activation, ActivationState.COMPLETED)
        except asyncio.CancelledError:
            if activation.error_code == "CHILD_DEADLINE_EXCEEDED":
                activation.result = "Child deadline exceeded."
            elif activation.error_code == "CHILD_IDLE_EXPIRED":
                activation.result = "Child lifetime expired while waiting for instructions."
            else:
                activation.result = "Subagent canceled."
            if activation.status == ActivationState.STOPPING:
                self.transition(activation, self._stopped_state(activation))
            else:
                self.transition(activation, ActivationState.CANCELED)
        except TimeoutError:
            activation.error_code = "CHILD_DEADLINE_EXCEEDED"
            activation.result = "Child deadline exceeded."
            if activation.status != ActivationState.STOPPING:
                self.transition(activation, ActivationState.STOPPING)
            self.transition(activation, ActivationState.TIMED_OUT)
        except Exception as exc:
            partial = ""
            message = self.redactor.exception(exc, "Child execution failed")
            if activation.status == ActivationState.STOPPING:
                activation.error_code = activation.error_code or "CHILD_EXECUTION_FAILED"
            elif isinstance(exc, OutputLimitError):
                activation.error_code = "MAX_OUTPUT_TOKENS"
                message = str(exc)
                partial = exc.partial_text.strip()
            elif isinstance(exc, CompactionError):
                activation.error_code = "COMPACTION_FAILED"
                message = f"Context compaction failed ({exc.code})."
                partial = self._recent_research_evidence(activation.session_id)
            else:
                activation.error_code = "CHILD_EXECUTION_FAILED"
            activation.diagnostic = {
                "code": activation.error_code,
                "category": "execution",
                "message": message,
                "exception_type": type(exc).__name__,
                "retryable": False,
            }
            activation.result = message
            if activation.status == ActivationState.STOPPING:
                self.transition(activation, self._stopped_state(activation))
            else:
                self.transition(activation, ActivationState.FAILED)
                if partial:
                    delivery_status = "partial"
                    activation.result = f"{message}\n\nPartial results:\n{partial}"
        finally:
            if (
                activation.timeout_task is not None
                and activation.timeout_task is not asyncio.current_task()
            ):
                activation.timeout_task.cancel()
        if activation.provider_adapter is not None and not activation.provider_disposed:
            try:
                await activation.provider_adapter.dispose(activation)
                activation.provider_disposed = True
            except Exception as exc:
                activation.provider_disposed = True
                self._cleanup_failure(activation, exc, "Child cleanup failed")
        stop_reason = {
            ActivationState.COMPLETED: "completed",
            ActivationState.CANCELED: "aborted",
            ActivationState.DISPOSED: "aborted",
            ActivationState.TIMED_OUT: "timeout",
        }.get(activation.status, "error")
        if stop_reason != "completed" and activation.diagnostic is None:
            activation.diagnostic = {
                "code": activation.error_code or "CHILD_EXECUTION_FAILED",
                "category": "timeout"
                if stop_reason == "timeout"
                else "cancellation"
                if stop_reason == "aborted"
                else "execution",
                "message": activation.result,
                "exception_type": None,
                "retryable": False,
            }
            activation.diagnostic["message"] = _bounded_diagnostic(
                self.redactor.clean(activation.result)
            )
        if activation.final_result is None:
            activation.final_result = SubagentResult(
                output=activation.result,
                stop_reason=stop_reason,
                diagnostic=_bounded_diagnostic(
                    activation.cleanup_error or activation.provider_diagnostic
                ),
            )
        try:
            self._create_delivery(
                activation, delivery_status or str(activation.status), activation.result
            )
            self.session_store.append(
                activation.parent_session_id,
                "agent/child_completed"
                if activation.status == ActivationState.COMPLETED
                else "agent/child_failed",
                {
                    "child_id": activation.id,
                    "status": str(activation.status),
                    "error_code": activation.error_code,
                    "result": activation.result,
                    "duration_seconds": round(time.monotonic() - activation.started_at, 3),
                },
                source="subagent-manager",
            )
            if activation.status == ActivationState.COMPLETED:
                self._publish_event(
                    {
                        "agent": activation.id,
                        "parent_id": activation.parent_id,
                        "event": {"type": "turn_completed", "result": activation.result},
                    }
                )
        except Exception as exc:
            self._cleanup_failure(activation, exc, "Delivery persistence failed")
        activation.finished_at = time.monotonic()
        activation.pending_tools.clear()
        activation.waiting_tools.clear()
        activation.current_phase = "ended"
        self._publish_event(
            {
                "type": "subagent_event",
                "event": {
                    "type": "ended",
                    "status": str(activation.status),
                    "cleanup_error": activation.cleanup_error,
                    "diagnostic": activation.diagnostic,
                    "result": activation.result,
                    "error": activation.result,
                },
                "agent": activation.id,
                "parent_id": activation.parent_id,
                "agent_description": activation.description,
            }
        )
        activation.settled = True
        activation.result_available = True
        async with self._condition:
            self._condition.notify_all()

    def acknowledge_delivery(self, activation: Activation) -> None:
        if activation.delivered:
            return
        if activation.delivery_id and self.session_store and activation.parent_session_id:
            self.session_store.append(
                activation.parent_session_id,
                "agent/child_delivery_acknowledged",
                {"delivery_id": activation.delivery_id, "child_id": activation.id},
                source="run-controller",
            )
        if activation.delivery_id:
            self.deliveries.pop(activation.delivery_id, None)
        activation.delivered = True

    def restore_deliveries(self, parent_id: str, parent_session_id: str) -> list[PersistedDelivery]:
        """Rebuild unacknowledged child results from the append-only parent event log."""
        if self.session_store is None:
            raise RuntimeError("session store is unavailable for delivery recovery")
        events = self.session_store.events(
            parent_session_id,
            event_types=(
                "agent/child_completed",
                "agent/child_failed",
                "agent/child_delivery_created",
                "agent/child_delivery_acknowledged",
                "user/message",
            ),
        )
        created: dict[str, PersistedDelivery] = {}
        acknowledged: set[str] = set()
        for event in events:
            payload = event.payload
            if event.event_type in {"agent/child_completed", "agent/child_failed"}:
                delivery_id = str(payload.get("delivery_id") or "")
                if delivery_id:
                    created[delivery_id] = PersistedDelivery(
                        delivery_id=delivery_id,
                        parent_id=parent_id,
                        parent_session_id=parent_session_id,
                        child_id=str(payload.get("child_id") or "unknown"),
                        status=str(payload.get("status") or event.event_type.rsplit("_", 1)[-1]),
                        result=str(payload.get("result") or ""),
                        error_code=(
                            str(payload["error_code"]) if payload.get("error_code") else None
                        ),
                    )
            elif event.event_type == "agent/child_delivery_created":
                delivery_id = str(payload.get("delivery_id") or "")
                if delivery_id:
                    previous = created.get(delivery_id)
                    created[delivery_id] = PersistedDelivery(
                        delivery_id=delivery_id,
                        parent_id=parent_id,
                        parent_session_id=parent_session_id,
                        child_id=str(
                            payload.get("child_id")
                            or (previous.child_id if previous else "unknown")
                        ),
                        status=str(
                            payload.get("result_status")
                            or (previous.status if previous else "completed")
                        ),
                        result=str(payload.get("content") or (previous.result if previous else "")),
                        error_code=(
                            str(payload["error_code"])
                            if payload.get("error_code")
                            else previous.error_code
                            if previous
                            else None
                        ),
                    )
            elif event.event_type == "user/message":
                acknowledged.update(payload.get("delivery_ids", []))
            elif event.event_type == "agent/child_delivery_acknowledged":
                delivery_id = str(payload.get("delivery_id") or "")
                if delivery_id:
                    acknowledged.add(delivery_id)
        for delivery_id in acknowledged:
            self.deliveries.pop(delivery_id, None)
            for activation in self.activations.values():
                if activation.delivery_id == delivery_id:
                    activation.delivered = True
        for delivery_id, delivery in created.items():
            if delivery_id not in acknowledged:
                self.deliveries[delivery_id] = delivery
        return [
            item
            for item in self.deliveries.values()
            if item.parent_id == parent_id and item.parent_session_id == parent_session_id
        ]

    def restore_catalog(self, parent_id: str, parent_session_id: str) -> list[dict[str, Any]]:
        store = self.session_store
        if store is None:
            raise RuntimeError("session store is unavailable for catalog recovery")
        entries: dict[str, dict[str, Any]] = {}
        for event in store.events(parent_session_id, event_types=("agent/child_started",)):
            payload = event.payload
            child_id = str(payload.get("child_id") or "")
            if not child_id or child_id in entries:
                continue
            provider = payload.get("provider")
            child_session_id = payload.get("child_session_id")
            if (
                not provider
                and isinstance(child_session_id, str)
                and store.child_parent(child_session_id) == parent_session_id
            ):
                descriptors = store.events(
                    child_session_id, event_types=("agent/subagent_descriptor",)
                )
                if descriptors:
                    provider = descriptors[0].payload.get("provider")
            entries[child_id] = {
                "child_id": child_id,
                "child_session_id": child_session_id,
                "provider": provider or "unknown",
                "label": str(payload.get("description") or ""),
            }
        self.catalogs[parent_id] = list(entries.values())
        return [dict(item) for item in self.catalogs[parent_id]]

    def catalog_for(self, parent_id: str) -> list[dict[str, Any]]:
        return [dict(item) for item in self.catalogs.get(parent_id, [])]

    def recover_orphaned_children(self, parent_id: str, parent_session_id: str) -> list[str]:
        """Mark interrupted activations after restart without replaying their side effects."""
        if self.session_store is None:
            raise RuntimeError("session store is unavailable for child recovery")
        events = self.session_store.events(
            parent_session_id,
            event_types=(
                "agent/child_started",
                "agent/child_completed",
                "agent/child_failed",
                "agent/child_orphaned",
            ),
        )
        started: dict[str, dict[str, Any]] = {}
        terminal: set[str] = set()
        orphaned: set[str] = set()
        for event in events:
            payload = event.payload
            child_id = str(payload.get("child_id") or "")
            if not child_id:
                continue
            if event.event_type == "agent/child_started":
                started[child_id] = payload
            elif event.event_type in {
                "agent/child_completed",
                "agent/child_failed",
                "agent/child_orphaned",
            }:
                terminal.add(child_id)
                if event.event_type == "agent/child_orphaned":
                    orphaned.add(child_id)
                    self._record_recovered_child(parent_id, payload)
        recovered: list[str] = []
        for child_id, payload in started.items():
            if child_id in terminal or child_id in self.activations:
                continue
            child_session_id = str(payload.get("child_session_id") or "")
            if not child_session_id:
                raise ValueError(f"child {child_id} has no persisted session identity")
            descriptor_events = self.session_store.events(
                child_session_id, event_types=("agent/subagent_descriptor",)
            )
            external = any(
                item.event_type == "agent/subagent_descriptor"
                and bool(item.payload.get("capabilities", {}).get("external_process"))
                for item in descriptor_events
            )
            status = "unknown" if external else "orphaned"
            self.session_store.append(
                parent_session_id,
                "agent/child_orphaned",
                {
                    "child_id": child_id,
                    "child_session_id": child_session_id,
                    "status": status,
                    "replay_allowed": False,
                },
                source="subagent-recovery",
            )
            self._record_recovered_child(
                parent_id,
                {
                    "child_id": child_id,
                    "child_session_id": child_session_id,
                    "status": status,
                    "replay_allowed": False,
                },
            )
            recovered.append(child_id)
            orphaned.add(child_id)
        return sorted(orphaned)

    def _record_recovered_child(self, parent_id: str, payload: dict[str, Any]) -> None:
        children = self.recovered_children.setdefault(parent_id, [])
        child_id = str(payload.get("child_id") or "")
        if not child_id:
            raise ValueError("recovered child event is missing child_id")
        for existing in children:
            if existing["child_id"] == child_id:
                existing.update(payload)
                return
        children.append(dict(payload))

    def recovered_for(self, parent_id: str) -> list[dict[str, Any]]:
        return [dict(item) for item in self.recovered_children.get(parent_id, [])]

    def acknowledge_persisted_delivery(self, delivery: PersistedDelivery) -> None:
        if delivery.delivery_id not in self.deliveries:
            return
        if self.session_store is None:
            raise RuntimeError("session store is unavailable for delivery acknowledgement")
        self.session_store.append(
            delivery.parent_session_id,
            "agent/child_delivery_acknowledged",
            {"delivery_id": delivery.delivery_id, "child_id": delivery.child_id},
            source="run-controller",
        )
        self.deliveries.pop(delivery.delivery_id, None)
        activation = self.activations.get(delivery.child_id)
        if activation is not None and activation.delivery_id == delivery.delivery_id:
            activation.delivered = True

    @staticmethod
    def _capabilities_for(client: Any) -> ProviderCapabilities:
        capabilities = getattr(client, "subagent_capabilities", None)
        if capabilities is None:
            # The built-in LLM client is only a model transport; child execution is
            # still owned by the local Agent runtime and therefore continuable.
            if callable(getattr(client, "chat", None)):
                return ProviderCapabilities(
                    continuable=True,
                    queue_idle=True,
                    steer_running=True,
                    supports_cancel=True,
                    supports_live_events=True,
                )
            return ProviderCapabilities()
        if isinstance(capabilities, ProviderCapabilities):
            return capabilities
        if isinstance(capabilities, dict):
            allowed = ProviderCapabilities.__dataclass_fields__
            return ProviderCapabilities(
                **{key: bool(value) for key, value in capabilities.items() if key in allowed}
            )
        raise TypeError("provider subagent_capabilities must be a mapping or ProviderCapabilities")

    @staticmethod
    def describe_activation(activation: Activation) -> dict[str, Any]:
        elapsed = max(0.0, (activation.finished_at or time.monotonic()) - activation.started_at)
        deadline_remaining = (
            max(0.0, activation.deadline_seconds - elapsed)
            if activation.deadline_seconds is not None
            else None
        )
        return {
            "id": activation.id,
            "parent_id": activation.parent_id,
            "provider": activation.provider,
            "state": str(activation.status),
            "description": activation.description,
            "elapsed_seconds": round(elapsed, 1),
            "last_activity_seconds_ago": round(
                max(0.0, time.monotonic() - activation.last_activity_at), 1
            ),
            "deadline_remaining_seconds": (
                round(deadline_remaining, 1) if deadline_remaining is not None else None
            ),
            "phase": activation.current_phase,
            "run_id": activation.run_id,
            "event_sequence": activation.event_sequence,
            "cleanup_error": activation.cleanup_error,
            "cleanup_diagnostics": list(activation.cleanup_diagnostics),
            "diagnostic": activation.diagnostic,
            "waiting_reason": "approval"
            if activation.waiting_tools
            else "instructions"
            if activation.status == ActivationState.IDLE_WAITING
            else None,
            "pending_tool_ids": sorted(activation.pending_tools),
            "pending_tools": dict(activation.pending_tools),
            "last_tool_result": activation.last_tool_result,
            "capabilities": {
                "continuable": activation.capabilities.continuable,
                "queue_idle": activation.capabilities.queue_idle,
                "steer_running": activation.capabilities.steer_running,
                "supports_cancel": activation.capabilities.supports_cancel,
                "supports_child_options": activation.capabilities.supports_child_options,
                "supports_structured_output": activation.capabilities.supports_structured_output,
                "supports_live_events": activation.capabilities.supports_live_events,
                "external_process": activation.capabilities.external_process,
                "agent_options": activation.capabilities.agent_options,
                "agent_preset": activation.capabilities.agent_preset,
                "output_schema": activation.capabilities.output_schema,
                "depth_limit": activation.capabilities.depth_limit,
            },
            "usage": dict(activation.usage) if activation.usage else None,
            "error_code": activation.error_code,
        }

    @staticmethod
    def _stopped_state(activation: Activation) -> ActivationState:
        if activation.error_code == "CHILD_DEADLINE_EXCEEDED":
            return ActivationState.TIMED_OUT
        if activation.error_code in {"DISPOSED", "HOST_DRAIN", "CHILD_IDLE_EXPIRED"}:
            return ActivationState.DISPOSED
        return ActivationState.CANCELED

    async def next_event(self, parent_id: str) -> dict[str, Any] | None:
        queue = self._events.setdefault(parent_id, ObservationBuffer(self.event_queue_size))
        gap = self._gap_event(queue, parent_id)
        if gap:
            return gap
        if not self.active_for(parent_id) and queue.empty():
            return None
        return await queue.get()

    def drain_events(self, parent_id: str) -> list[dict[str, Any]]:
        queue = self._events.get(parent_id)
        if queue is None:
            return []
        events = []
        gap = self._gap_event(queue, parent_id)
        if gap:
            events.append(gap)
        while not queue.empty():
            events.append(queue.get_nowait())
        return events

    def active_for(self, parent_id: str) -> list[Activation]:
        return [
            a
            for a in self.activations.values()
            if a.parent_id == parent_id and a.status in _LIVE_STATES
        ]

    def completed_for(self, parent_id: str) -> list[Activation]:
        return [
            a
            for a in self.activations.values()
            if a.parent_id == parent_id and a.result_available and not a.delivered
        ]

    async def wait_for_parent(self, parent_id: str) -> list[Activation]:
        async with self._condition:
            await self._condition.wait_for(
                lambda: bool(self.completed_for(parent_id)) or not self.active_for(parent_id)
            )
        return self.completed_for(parent_id)

    async def interrupt(self, agent_id: str) -> bool:
        async with self._child_lock(agent_id):
            activation = self.activations.get(agent_id)
            if activation is None or activation.status not in {
                ActivationState.STARTING,
                ActivationState.RUNNING,
                ActivationState.IDLE_WAITING,
            }:
                return False
            if activation.status == ActivationState.IDLE_WAITING:
                # Parked continuable activations remain resident; interrupting
                # them must not create a second terminal settlement or delivery.
                return True
            self.transition(activation, ActivationState.STOPPING)
            activation.error_code = "INTERRUPTED"
        if activation.provider_adapter is None:
            raise RuntimeError(f"activation {agent_id} has no provider adapter")
        await self._interrupt_provider(activation)
        await asyncio.gather(activation.task, return_exceptions=True)
        await self._wait_for_settlement(activation)
        await self._dispose_provider(activation)
        return True

    async def _dispose_provider(self, activation: Activation) -> None:
        async with self._child_lock(activation.id):
            if activation.provider_disposed:
                return
            if activation.provider_adapter is None:
                raise RuntimeError(f"activation {activation.id} has no provider adapter")
            try:
                await activation.provider_adapter.dispose(activation)
            except Exception as exc:
                self._cleanup_failure(activation, exc, "Child cleanup failed")
                raise
            finally:
                activation.provider_disposed = True

    async def _interrupt_provider(self, activation: Activation) -> None:
        adapter = activation.provider_adapter
        if adapter is None:
            raise RuntimeError(f"activation {activation.id} has no provider adapter")
        try:
            await adapter.interrupt(activation)
        except Exception as exc:
            self._cleanup_failure(activation, exc, "Provider interruption failed")
            if not activation.task.done() and not activation.task.cancelling():
                activation.task.cancel()

    async def _wait_for_settlement(self, activation: Activation) -> None:
        await self._condition.acquire()
        try:
            await self._condition.wait_for(lambda: activation.settled)
        finally:
            self._condition.release()

    def descendants_for(self, parent_id: str) -> list[Activation]:
        result: list[Activation] = []
        pending = [parent_id]
        while pending:
            current = pending.pop()
            children = [item for item in self.activations.values() if item.parent_id == current]
            result.extend(children)
            pending.extend(item.id for item in children)
        return result

    def is_authorized_descendant(self, parent_id: str, child_id: str) -> bool:
        current = self.activations.get(child_id)
        seen: set[str] = set()
        while current is not None and current.id not in seen:
            if current.parent_id == parent_id:
                return True
            seen.add(current.id)
            current = self.activations.get(current.parent_id)
        return False

    async def dispose(self, agent_id: str) -> bool:
        targets = self.descendants_for(agent_id)
        targets.append(self.activations.get(agent_id))
        targets = [item for item in targets if item is not None]
        owned = [item for item in targets if item.status in _RESIDENT_STATES]
        if not owned:
            return False
        for activation in reversed(owned):
            async with self._child_lock(activation.id):
                if activation.status not in _RESIDENT_STATES:
                    continue
                self.transition(activation, ActivationState.STOPPING)
                activation.error_code = "DISPOSED"
            await self._interrupt_provider(activation)
        await asyncio.gather(*(item.task for item in owned), return_exceptions=True)
        await asyncio.gather(*(self._wait_for_settlement(item) for item in owned))
        await asyncio.gather(
            *(self._dispose_provider(item) for item in owned if item.provider_adapter is not None)
        )
        return True

    async def queue_message(self, agent_id: str, message: str) -> tuple[bool, str]:
        async with self._child_lock(agent_id):
            activation = self.activations.get(agent_id)
            if activation is None:
                return False, "UNKNOWN_AGENT"
            if activation.status not in {ActivationState.RUNNING, ActivationState.IDLE_WAITING}:
                return False, "ACTIVATION_CLOSING"
            if activation.inbox.qsize() >= 32:
                return False, "CHILD_INBOX_FULL"
            if activation.status == ActivationState.IDLE_WAITING:
                active = sum(a.status in _LIVE_STATES for a in self.activations.values())
                if active >= self.max_active:
                    return False, "ACTIVATION_LIMIT_REACHED"
                self.transition(activation, ActivationState.RUNNING)
            activation.inbox.put_nowait(message)
            return True, "accepted"

    async def cancel_for_parent(self, parent_id: str) -> None:
        await self.dispose_descendants(parent_id)

    async def dispose_descendants(self, parent_id: str) -> None:
        targets = self.descendants_for(parent_id)
        targets = [item for item in targets if item.status in _RESIDENT_STATES]
        if not targets:
            return
        for activation in reversed(targets):
            async with self._child_lock(activation.id):
                if activation.status in _RESIDENT_STATES:
                    self.transition(activation, ActivationState.STOPPING)
                    activation.error_code = "PARENT_CANCELED"
                else:
                    continue
                await self._interrupt_provider(activation)
        await asyncio.gather(*(item.task for item in targets), return_exceptions=True)
        await asyncio.gather(*(self._wait_for_settlement(item) for item in targets))
        await asyncio.gather(
            *(self._dispose_provider(item) for item in targets if item.provider_adapter is not None)
        )

    async def drain(self) -> None:
        try:
            async with asyncio.timeout(self.drain_timeout):
                try:
                    await self._drain()
                finally:
                    self._observer_stopping = True
                    await self.flush_observations()
                    if self.observation_error is not None:
                        self.observation_error = None
                        raise RuntimeError("Subagent observation callback failed.") from None
        except TimeoutError:
            for activation in self.activations.values():
                if not activation.settled:
                    activation.error_code = "HOST_DRAIN_TIMEOUT"
                    activation.cleanup_error = (
                        "Host drain deadline elapsed before cleanup finished."
                    )
            raise TimeoutError(
                f"Subagent drain exceeded {self.drain_timeout:.1f}s; cleanup remains active."
            ) from None

        finally:
            self._observer_stopping = True
            if self._observer_task is not None and not self._observer_task.done():
                self._observer_task.cancel()
                await asyncio.gather(self._observer_task, return_exceptions=True)

    async def _drain(self) -> None:
        self._closed = True
        targets = list(self.activations.values())
        for activation in reversed(targets):
            async with self._child_lock(activation.id):
                if activation.status in _RESIDENT_STATES:
                    self.transition(activation, ActivationState.STOPPING)
                    activation.error_code = "HOST_DRAIN"
                else:
                    continue
                await self._interrupt_provider(activation)
        if not targets:
            return
        started_at = time.monotonic()
        tasks = {item.task for item in targets}
        tasks.update(self._settlers)
        done, pending = await asyncio.wait(tasks, timeout=self.drain_timeout)
        if pending:
            for activation in targets:
                if activation.task in pending:
                    activation.error_code = "HOST_DRAIN_TIMEOUT"
                    activation.cleanup_error = "Host drain deadline elapsed before task cleanup."
            raise TimeoutError(
                f"Subagent drain exceeded {self.drain_timeout:.1f}s; cleanup remains active."
            )
        failures = [
            RuntimeError("Subagent settlement failed.")
            for task in done
            if task in self._settlers and not task.cancelled() and task.exception()
        ]
        if failures:
            self.settlement_error = failures[0]
            raise ExceptionGroup("Subagent drain encountered task failures", failures)
        remaining = max(0.0, self.drain_timeout - (time.monotonic() - started_at))
        try:
            await asyncio.wait_for(
                asyncio.gather(*(self._wait_for_settlement(item) for item in targets)),
                timeout=remaining,
            )
        except TimeoutError as exc:
            unsettled = [item for item in targets if not item.settled]
            for activation in unsettled:
                activation.error_code = "HOST_DRAIN_TIMEOUT"
                activation.cleanup_error = "Settlement did not finish before host drain deadline."
            raise TimeoutError(
                f"Subagent settlement exceeded {self.drain_timeout:.1f}s host drain deadline."
            ) from exc
        cleanup_results = await asyncio.gather(
            *(
                self._dispose_provider(item)
                for item in targets
                if item.provider_adapter is not None
            ),
            return_exceptions=True,
        )
        cleanup_failures = [
            "Provider cleanup failed."
            for result in cleanup_results
            if isinstance(result, Exception)
        ]
        cleanup_failures.extend(item.cleanup_error for item in targets if item.cleanup_error)
        if self.settlement_error is not None and not cleanup_failures:
            cleanup_failures.append("Subagent persistence or cleanup failed.")
        if cleanup_failures:
            raise RuntimeError(
                "Subagent drain completed with cleanup failures: "
                + "; ".join(self.redactor.clean(str(item)) for item in cleanup_failures)
            )


def make_subagent_tool():
    async def handler(payload: dict[str, Any], context: ToolContext) -> ToolResult:
        if context.subagent_manager is None:
            return ToolResult("Subagent runtime is unavailable.", is_error=True)
        return await context.subagent_manager.start(payload, context)

    return Tool(
        name="subagent",
        description=(
            "Delegate a self-contained task to a child agent. Returns immediately with its ID. "
            "Finished reports arrive automatically between model requests. "
            "While waiting, do independent work or end this response without tools so the runtime "
            "can wait for results. Do not poll or sleep for reports."
        ),
        parameters=object_schema(
            {
                "description": {"type": "string", "description": "Short task label"},
                "role": {
                    "type": "string",
                    "enum": list(SUBAGENT_ROLES),
                    "description": "Optional display role for this task. Choose the closest role.",
                },
                "prompt": {
                    "type": "string",
                    "description": "Complete, self-contained task instructions",
                },
                "cwd": {"type": "string", "description": "Optional working directory"},
                "timeout": {
                    "type": "number",
                    "description": "Optional child deadline in seconds; cancellation is propagated",
                },
            },
            required=["description", "prompt"],
        ),
        handler=handler,
        is_read_only=False,
        is_concurrency_safe=True,
        timeout=10.0,
        required_keys=["description", "prompt"],
    )


def make_subagent_control_tools():
    async def list_handler(_payload: dict[str, Any], context: ToolContext) -> ToolResult:
        if context.subagent_manager is None:
            return ToolResult("Subagent runtime is unavailable.", is_error=True)
        children = [
            activation
            for activation in context.subagent_manager.activations.values()
            if activation.parent_id == (context.agent_id or "root")
        ]
        if not children:
            return ToolResult("No subagents have been started.")
        lines = [
            json.dumps(context.subagent_manager.describe_activation(item), ensure_ascii=False)
            for item in children
        ]
        return ToolResult("\n".join(lines))

    async def interrupt_handler(payload: dict[str, Any], context: ToolContext) -> ToolResult:
        manager = context.subagent_manager
        if manager is None:
            return ToolResult("Subagent runtime is unavailable.", is_error=True)
        activation = manager.activations.get(str(payload["agent_id"]))
        if activation is None or not manager.is_authorized_descendant(
            context.agent_id or "root", activation.id
        ):
            return ToolResult("Unknown direct child agent.", is_error=True)
        stopped = await manager.interrupt(activation.id)
        return ToolResult(
            f"Interrupt requested for {activation.id}."
            if stopped
            else f"Agent {activation.id} is no longer running."
        )

    async def send_handler(payload: dict[str, Any], context: ToolContext) -> ToolResult:
        manager = context.subagent_manager
        if manager is None:
            return ToolResult("Subagent runtime is unavailable.", is_error=True)
        activation = manager.activations.get(str(payload["agent_id"]))
        if activation is None or not manager.is_authorized_descendant(
            context.agent_id or "root", activation.id
        ):
            return ToolResult("Unknown direct child agent.", is_error=True)
        if not activation.capabilities.continuable:
            return ToolResult("CONTINUATION_UNSUPPORTED: provider is one-shot.", is_error=True)
        if activation.status not in {ActivationState.RUNNING, ActivationState.IDLE_WAITING}:
            return ToolResult("Cannot send a message to a settled subagent.", is_error=True)
        if activation.status == ActivationState.RUNNING and not activation.capabilities.queue_idle:
            return ToolResult(
                "CONTINUATION_UNSUPPORTED: provider cannot queue follow-ups.", is_error=True
            )
        accepted, reason = await manager.queue_message(activation.id, str(payload["message"]))
        if not accepted:
            return ToolResult(f"{reason}: message was not accepted.", is_error=True)
        delivery = (
            "steered at next safe model boundary"
            if activation.status == ActivationState.RUNNING
            and activation.capabilities.steer_running
            else "queued for next turn"
            if activation.status == ActivationState.RUNNING
            else "waking"
        )
        return ToolResult(f"Message {delivery} for subagent {activation.id}.")

    return [
        Tool(
            name="list_agents",
            description="Inspect child status. Reports are delivered automatically.",
            parameters=object_schema({}),
            handler=list_handler,
        ),
        Tool(
            name="interrupt_agent",
            description="Cancel one of your running delegated child agents.",
            parameters=object_schema(
                {"agent_id": {"type": "string", "description": "Child agent ID"}},
                required=["agent_id"],
            ),
            handler=interrupt_handler,
            is_read_only=False,
            required_keys=["agent_id"],
            needs_workspace_checkpoint=False,
        ),
        Tool(
            name="send_message",
            description=(
                "Send a follow-up message to a delegated child when continuation is supported."
            ),
            parameters=object_schema(
                {
                    "agent_id": {"type": "string", "description": "Child agent ID"},
                    "message": {"type": "string", "description": "Follow-up message"},
                },
                required=["agent_id", "message"],
            ),
            handler=send_handler,
            is_read_only=False,
            required_keys=["agent_id", "message"],
        ),
    ]
