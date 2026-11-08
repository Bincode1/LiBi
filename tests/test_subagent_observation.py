from __future__ import annotations

import asyncio
import io
import json
import threading
from datetime import datetime
from types import SimpleNamespace

import pytest
from rich.console import Console

from libi.agent import Agent
from libi.agent.subagent_events import ObservationBuffer, public_event
from libi.agent.subagents import (
    ActivationState,
    ProviderCapabilities,
    SubagentManager,
    SubagentResult,
)
from libi.config import LiBiConfig
from libi.policy.redaction import Redactor
from libi.render.plain import PlainRenderer
from libi.render.rich_renderer import RichRenderer
from libi.session import SessionStore
from libi.tools.base import Tool, ToolContext, ToolResult
from libi.tools.executor import ToolExecutor
from libi.tools.registry import ToolRegistry
from libi.types import Usage


def make_context(tmp_path):
    config = LiBiConfig()
    config.features.audit_log = False
    config.policy.hitl_mode = "never"
    store = SessionStore(tmp_path / "runtime.db")
    return ToolContext(
        cwd=str(tmp_path),
        config=config,
        session_store=store,
        session_id=store.create_session(),
        tool_registry=ToolRegistry(),
        llm_client=SimpleNamespace(
            model_name="fake", provider_name="fake", max_context_window=4096
        ),
        agent_id="root",
    )


async def until(predicate):
    async with asyncio.timeout(3):
        while not predicate():
            await asyncio.sleep(0.001)


def test_contract_filters_unknown_objects_and_secrets_without_repr():
    class PrivateObject:
        def __repr__(self):
            raise AssertionError("must not stringify provider objects")

    redactor = Redactor(("unit-private-value",))
    event = public_event(
        {
            "agent": "child",
            "parent_id": "root",
            "private": PrivateObject(),
            "event": {"type": "text_delta", "text": "unit-private-value", "raw": PrivateObject()},
        },
        redactor,
    )
    assert event["event"] == {"type": "text_delta", "text": "[redacted]"}
    assert "private" not in event
    assert datetime.fromisoformat(event["timestamp"]).utcoffset().total_seconds() == 0
    for inner in (
        {"type": "new_extension", "raw": PrivateObject()},
        {"type": "tool_result", "result": PrivateObject()},
    ):
        result = public_event({"event": inner}, redactor)
        assert result["event"]["type"] == "unknown"
        json.dumps(result)


def test_lifecycle_priority_coalescing_and_snapshot_after_overload():
    async def run():
        manager = SubagentManager(event_queue_size=32)
        manager._publish_event(
            {"agent": "one", "parent_id": "root", "event": {"type": "ended", "status": "completed"}}
        )
        for index in range(100):
            manager._publish_event(
                {
                    "agent": str(index),
                    "parent_id": "root",
                    "event": {"type": "text_delta", "text": "progress"},
                }
            )
        gap = await manager.next_event("root")
        assert gap["event"]["dropped"] > 0
        assert "snapshots" in gap["event"]
        events = manager.drain_events("root")
        assert events[0]["event"]["type"] == "ended"
        assert len(events) == 32
        buffer = ObservationBuffer(2)
        for text in ("hello", " world"):
            buffer.put_nowait(
                public_event(
                    {"agent": "a", "run_id": "r", "event": {"type": "text_delta", "text": text}},
                    Redactor(),
                )
            )
        assert buffer.qsize() == 1 and buffer.coalesced == 1
        assert buffer.get_nowait()["event"]["text"] == "hello world"

    asyncio.run(run())


def test_flush_observations_waits_for_callback_after_queue_item_is_removed():
    async def run():
        manager = SubagentManager()
        callbacks = []
        manager.event_callback = callbacks.append
        original_get = manager._callback_buffer.get
        removed = asyncio.Event()

        async def get_then_notify():
            item = await original_get()
            removed.set()
            return item

        manager._callback_buffer.get = get_then_notify
        manager._observer_task = asyncio.create_task(manager._observe_callbacks())
        try:
            # Let the observer park on an empty queue before offering the event.
            await asyncio.sleep(0)
            manager._publish_event(
                {
                    "agent": "child",
                    "parent_id": "root",
                    "event": {"type": "ended", "status": "completed"},
                }
            )
            await asyncio.wait_for(removed.wait(), timeout=1)
            await asyncio.wait_for(manager.flush_observations(), timeout=1)
            assert [event["event"]["type"] for event in callbacks] == ["ended"]
        finally:
            await manager.drain()

    asyncio.run(run())


def test_slow_callback_does_not_block_child_and_gap_recovers_state(tmp_path, monkeypatch):
    ctx = make_context(tmp_path)
    entered = threading.Event()
    release = threading.Event()
    captured = []

    def slow(event):
        entered.set()
        assert release.wait(3)
        captured.append(event)

    async def complete(self, message):
        await until(entered.is_set)
        for index in range(100):
            await self.event_sink(
                {"type": "tool_result", "name": "read", "tool_call_id": str(index), "result": "ok"}
            )
        return SimpleNamespace(text="findings")

    monkeypatch.setattr(Agent, "run_complete", complete)

    async def run():
        manager = SubagentManager(event_queue_size=32)
        manager.event_callback = slow
        try:
            await manager.start({"description": "review", "prompt": "inspect"}, ctx)
            child = next(iter(manager.activations.values()))
            await until(lambda: child.status == ActivationState.IDLE_WAITING)
            assert next(iter(manager.activations.values())).report is not None
            assert not release.is_set()
            assert manager._callback_buffer.qsize() <= 32
        finally:
            release.set()
        await manager.flush_observations()
        gaps = [e for e in captured if e["event"]["type"] == "observation_gap"]
        assert gaps and gaps[0]["event"]["snapshots"][0]["id"] == child.id
        await manager.drain()

    asyncio.run(run())


def test_each_run_completes_once_usage_accumulates_and_final_end_once(tmp_path, monkeypatch):
    ctx = make_context(tmp_path)
    first_started = asyncio.Event()
    release = asyncio.Event()
    calls = 0

    async def complete(self, message):
        nonlocal calls
        calls += 1
        if calls == 1:
            first_started.set()
            await release.wait()
        await self.event_sink({"type": "done", "messages": [object()]})
        return SimpleNamespace(text=message, usage=Usage(input_tokens=10, output_tokens=2))

    monkeypatch.setattr(Agent, "run_complete", complete)

    async def run():
        manager = SubagentManager()
        await manager.start({"description": "review", "prompt": "first"}, ctx)
        child = next(iter(manager.activations.values()))
        await first_started.wait()
        assert (await manager.queue_message(child.id, "second"))[0]
        release.set()
        await until(lambda: calls == 2 and child.status == ActivationState.IDLE_WAITING)
        await manager.interrupt(child.id)
        await manager.drain()
        events = manager.drain_events("root")
        completed = [e for e in events if e["event"]["type"] == "turn_completed"]
        assert len(completed) == 2 and len({e["run_id"] for e in completed}) == 2
        assert sum(e["event"]["type"] == "ended" for e in events) == 1
        sequences = [e["seq"] for e in events if "seq" in e]
        assert sequences == sorted(set(sequences))
        assert manager.describe_activation(child)["usage"]["input_tokens"] == 20
        assert child.current_phase == "ended"

    asyncio.run(run())


def test_parallel_tools_and_approval_are_real_live_states(tmp_path, monkeypatch):
    ctx = make_context(tmp_path)
    approval_gate = asyncio.Event()
    tool_gate = asyncio.Event()
    tool_entered = asyncio.Event()

    async def approve(request):
        await approval_gate.wait()
        return "approve"

    async def handler(payload, context):
        tool_entered.set()
        await tool_gate.wait()
        return ToolResult("read finished")

    registry = ToolRegistry()
    registry.register(Tool("read", "read", {}, handler))
    ctx.config.policy.hitl_mode = "always"

    async def execute(self, message):
        tool_context = ToolContext(
            cwd=ctx.cwd,
            config=ctx.config,
            approval_callback=approve,
            status_callback=lambda event: self.subagent_manager.tool_status(self.agent_id, event),
        )
        calls = [
            {"id": name, "function": {"name": "read", "arguments": "{}"}} for name in ("one", "two")
        ]
        await ToolExecutor(registry).execute_all(calls, tool_context)
        return SimpleNamespace(text="done")

    monkeypatch.setattr(Agent, "run_complete", execute)

    async def run():
        manager = SubagentManager()
        await manager.start({"description": "review", "prompt": "inspect"}, ctx)
        child = next(iter(manager.activations.values()))
        await until(lambda: len(child.waiting_tools) == 2)
        snapshot = manager.describe_activation(child)
        assert snapshot["phase"] == "approval"
        assert snapshot["waiting_reason"] == "approval"
        assert snapshot["pending_tool_ids"] == ["one", "two"]
        approval_gate.set()
        await tool_entered.wait()
        await until(lambda: not child.waiting_tools)
        assert child.current_phase == "tool" and len(child.pending_tools) == 2
        tool_gate.set()
        await until(lambda: child.status == ActivationState.IDLE_WAITING)
        assert not child.pending_tools and child.last_tool_result == "read finished"
        assert manager.describe_activation(child)["usage"] is None
        await manager.drain()

    asyncio.run(run())


def test_multiple_cleanup_failures_preserve_business_result_and_safe_diagnostics(
    tmp_path, monkeypatch
):
    ctx = make_context(tmp_path)

    class Provider:
        provider_name = "test"
        capabilities = ProviderCapabilities()

        async def run(self, *args):
            return SubagentResult("business result", "completed", "中" * 5000)

        async def interrupt(self, activation):
            activation.task.cancel()

        async def dispose(self, activation):
            raise OSError("unit-private-value")

    original = ctx.session_store.append

    def fail_terminal(sid, kind, payload, **kwargs):
        if kind == "agent/child_completed":
            raise OSError("unit-private-value")
        return original(sid, kind, payload, **kwargs)

    monkeypatch.setattr(ctx.session_store, "append", fail_terminal)

    async def run():
        manager = SubagentManager()
        manager.register_provider(Provider())
        await manager.start({"description": "test", "prompt": "test", "provider": "test"}, ctx)
        child = next(iter(manager.activations.values()))
        await until(lambda: child.settled)
        assert child.result == "business result"
        assert len(child.cleanup_diagnostics) == 2
        assert len(child.final_result.diagnostic.encode("utf-8")) <= 4096
        assert "unit-private-value" not in json.dumps(manager.describe_activation(child))
        assert child.report is not None
        with pytest.raises(RuntimeError, match="cleanup failures"):
            await manager.drain()

    asyncio.run(run())


def test_renderers_show_approval_and_snapshot_gap_without_llm(capsys):
    snapshot = {
        "id": "child",
        "description": "inspect",
        "state": "running",
        "phase": "approval",
        "waiting_reason": "approval",
        "elapsed_seconds": 12,
        "last_activity_seconds_ago": 3,
    }
    events = [
        {
            "type": "subagent_event",
            "agent": "child",
            "event": {"type": "status", "snapshot": {**snapshot, "description": "routine refresh"}},
        },
        {
            "type": "subagent_event",
            "agent": "child",
            "event": {"type": "phase_changed", "phase": "tool"},
        },
        {"type": "subagent_event", "agent": "child", "event": {"type": "idle"}},
        {"type": "subagent_event", "agent": "child", "event": {"type": "turn_completed"}},
        {
            "type": "subagent_event",
            "agent": "child",
            "event": {"type": "phase_changed", "phase": "approval", "waiting_reason": "approval"},
        },
        {
            "type": "subagent_event",
            "event": {"type": "observation_gap", "dropped": 5, "snapshots": [snapshot]},
        },
    ]
    output = io.StringIO()
    rich = RichRenderer(Console(file=output, width=180))
    plain = PlainRenderer()
    for event in events:
        rich.handle(event)
        plain.handle(event)
    assert "routine refresh" not in output.getvalue()
    assert "phase_changed" not in output.getvalue()
    assert "waiting for instructions" not in output.getvalue()
    assert "turn completed" not in output.getvalue()
    assert "waiting for approval" in output.getvalue()
    assert "last activity 3s ago" in output.getvalue()
    assert "waiting for approval" in capsys.readouterr().out


def test_local_activity_refresh_has_identity_without_model_calls(tmp_path, monkeypatch):
    ctx = make_context(tmp_path)
    captured = []
    model_runs = 0

    async def waiting(self, message):
        nonlocal model_runs
        model_runs += 1
        await asyncio.Event().wait()

    monkeypatch.setattr(Agent, "run_complete", waiting)

    async def run():
        manager = SubagentManager()
        manager.event_callback = captured.append
        await manager.start({"description": "inspect", "prompt": "wait"}, ctx)
        child = next(iter(manager.activations.values()))
        await until(lambda: any(e["event"]["type"] == "status" for e in captured))
        status = next(e for e in captured if e["event"]["type"] == "status")
        assert status["run_id"] == child.run_id
        assert status["seq"] > 0 and status["session_id"] == child.session_id
        assert status["event"]["snapshot"]["last_activity_seconds_ago"] >= 1
        assert model_runs == 1
        await manager.drain()

    asyncio.run(run())


def test_execution_failure_uses_stable_code_and_partial_error_stream_is_hidden(
    tmp_path, monkeypatch
):
    ctx = make_context(tmp_path)

    async def fail(self, message):
        await self.event_sink({"type": "text_delta", "text": "unit-private-prefix"})
        await self.event_sink({"type": "error", "error": ValueError("private provider detail")})
        raise ValueError("private provider detail")

    monkeypatch.setattr(Agent, "run_complete", fail)

    async def run():
        manager = SubagentManager()
        await manager.start({"description": "inspect", "prompt": "wait"}, ctx)
        child = next(iter(manager.activations.values()))
        await until(lambda: child.settled)
        assert child.error_code == "CHILD_EXECUTION_FAILED"
        assert child.diagnostic["exception_type"] == "ValueError"
        events = manager.drain_events("root")
        assert "unit-private-prefix" not in json.dumps(events)
        assert "private provider detail" not in json.dumps(events)
        await manager.drain()

    asyncio.run(run())
