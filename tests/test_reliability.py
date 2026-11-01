from __future__ import annotations

import asyncio
import json
from types import SimpleNamespace

import pytest

from paicli.agent import Agent, QueryEngine
from paicli.agent.run_controller import RunController
from paicli.agent.subagents import ActivationState, SubagentManager
from paicli.config import PaiCliConfig
from paicli.policy.redaction import Redactor
from paicli.session import ContextProjector, SessionStore
from paicli.tools import ToolRegistry
from paicli.tools.approval import ApprovalBroker
from paicli.tools.base import ToolContext


class DelegatingClient:
    model_name = "fake"
    provider_name = "fake"
    max_context_window = 128_000

    def __init__(self):
        self.root_calls = 0
        self.result_counts = []

    async def chat(self, messages, tools, *, system_prompt):
        if messages[0].content == "inspect child":
            yield {"type": "text_delta", "text": "child findings"}
            yield {"type": "message_end", "stop_reason": "end_turn"}
            return
        self.root_calls += 1
        self.result_counts.append(sum("Child agent results:" in str(m.content) for m in messages))
        if self.root_calls == 1:
            yield {
                "type": "tool_call_delta",
                "tool_call": {
                    "index": 0,
                    "id": "spawn",
                    "function": {
                        "name": "subagent",
                        "arguments": json.dumps(
                            {
                                "description": "inspect",
                                "prompt": "inspect child",
                            }
                        ),
                    },
                },
            }
            yield {"type": "message_end", "stop_reason": "tool_use"}
        else:
            yield {"type": "text_delta", "text": "parent answer"}
            yield {"type": "message_end", "stop_reason": "end_turn"}


def context(tmp_path):
    config = PaiCliConfig()
    config.policy.hitl_mode = "never"
    config.features.memory = False
    config.features.skill = False
    store = SessionStore(tmp_path / "runtime.db")
    return ToolContext(
        cwd=str(tmp_path),
        config=config,
        session_store=store,
        session_id=store.create_session(),
        tool_registry=ToolRegistry(),
        llm_client=DelegatingClient(),
        agent_id="root",
    )


async def wait_idle(activation):
    async with asyncio.timeout(2):
        while activation.status != ActivationState.IDLE_WAITING:
            if activation.task.done():
                activation.task.result()
                raise AssertionError("child ended before idle")
            await asyncio.sleep(0)


def test_parent_natural_child_completion_delivers_once_and_sdk_closes(tmp_path, monkeypatch):
    monkeypatch.setattr("paicli.agent.agent.SnapshotService.create", lambda *args: None)
    ctx = context(tmp_path)

    async def run():
        async with QueryEngine(
            llm_client=ctx.llm_client,
            tool_registry=ctx.tool_registry,
            config=ctx.config,
            cwd=ctx.cwd,
            session_store=ctx.session_store,
        ) as engine:
            async with asyncio.timeout(3):
                await engine.ask_complete_async("delegate inspection")
            manager = engine._react_agent.subagent_manager
            assert ctx.llm_client.root_calls == 3
            assert ctx.llm_client.result_counts == [0, 0, 1]
            deliveries = [
                e
                for e in ctx.session_store.events(engine.session_id)
                if e.source == "subagent-delivery"
            ]
            assert len(deliveries) == 1
            assert len(deliveries[0].payload["delivery_ids"]) == 1
        assert not manager.active_for("root")
        assert all(a.settled for a in manager.activations.values())

    asyncio.run(run())


def test_continuation_keeps_unacknowledged_rounds_and_capacity(tmp_path, monkeypatch):
    ctx = context(tmp_path)

    async def complete(self, message):
        if message == "hold":
            await asyncio.Event().wait()
        return SimpleNamespace(text=message)

    monkeypatch.setattr(Agent, "run_complete", complete)

    async def run():
        manager = SubagentManager(max_active=1, max_resident=2)
        await manager.start({"description": "first", "prompt": "first"}, ctx)
        first = next(iter(manager.activations.values()))
        await wait_idle(first)
        await manager.start({"description": "second", "prompt": "hold"}, ctx)
        accepted, reason = await manager.queue_message(first.id, "next")
        assert not accepted and reason == "ACTIVATION_LIMIT_REACHED"
        second = list(manager.activations.values())[1]
        await manager.interrupt(second.id)
        assert (await manager.queue_message(first.id, "next"))[0]
        await wait_idle(first)
        controller = RunController(
            manager=manager,
            parent_id="root",
            parent_session_id=ctx.session_id,
            session_store=ctx.session_store,
        )
        assert controller.has_pending_children()
        deliveries = controller.collect_deliveries(defer_ack=True)
        rounds = [d for d in deliveries if d.child_id == first.id]
        assert [d.result for d in rounds] == ["first", "next"]
        assert len({d.delivery_id for d in rounds}) == 2
        controller.acknowledge_deliveries(deliveries)
        assert controller.collect_deliveries() == []
        await manager.drain()

    asyncio.run(run())


def test_restore_after_pending_check_and_acceptance_crash_window(tmp_path):
    ctx = context(tmp_path)
    store = ctx.session_store
    store.append(
        ctx.session_id,
        "agent/child_delivery_created",
        {
            "delivery_id": "delivery-test",
            "child_id": "child-test",
            "result_status": "completed",
            "content": "findings",
        },
    )
    manager = SubagentManager()
    controller = RunController(
        manager=manager, parent_id="root", parent_session_id=ctx.session_id, session_store=store
    )
    assert controller.has_pending_children()
    deliveries = controller.collect_deliveries(defer_ack=True)
    assert len(deliveries) == 1
    store.append(
        ctx.session_id,
        "user/message",
        {
            "content": "accepted findings",
            "delivery_ids": [deliveries[0].delivery_id],
        },
    )
    restarted = RunController(
        manager=SubagentManager(),
        parent_id="root",
        parent_session_id=ctx.session_id,
        session_store=store,
    )
    assert not restarted.has_pending_children()
    assert restarted.collect_deliveries() == []


def test_approval_failure_reaches_current_queued_and_future_requests():
    async def run():
        release = asyncio.Event()

        async def broken(_request):
            await release.wait()
            raise ValueError("private provider detail")

        broker = ApprovalBroker(broken)
        first = asyncio.create_task(broker.request({}))
        second = asyncio.create_task(broker.request({}))
        await asyncio.sleep(0)
        release.set()
        async with asyncio.timeout(1):
            results = await asyncio.gather(first, second, return_exceptions=True)
            assert all(isinstance(result, RuntimeError) for result in results)
            with pytest.raises(RuntimeError, match="presenter failed"):
                await broker.request({})
        await broker.close()

    asyncio.run(run())


def test_redaction_preserves_metrics_and_reads_old_rows_safely(tmp_path):
    redactor = Redactor(("unit-private-value",))
    value = redactor.clean(
        {
            "input_tokens": 12,
            "cached_tokens": 8,
            "max_tokens": 100,
            "accessToken": "unit-private-value",
            "detail": "unit-private-value",
        }
    )
    assert value["input_tokens"] == 12 and value["cached_tokens"] == 8
    assert value["max_tokens"] == 100
    assert "unit-private-value" not in json.dumps(value)
    store = SessionStore(tmp_path / "old.db")
    store.append("s", "user/message", {"content": "unit-private-value"})
    store.redactor.add_sensitive_values(("unit-private-value",))
    assert store.events("s")[0].payload["content"] == "[redacted]"
    # All connections have been closed; Windows can immediately rename the database.
    store.db_path.rename(tmp_path / "renamed.db")


def test_projection_literal_templates_and_request_cutoff(tmp_path):
    store = SessionStore(tmp_path / "runtime.db")
    sid = store.create_session()
    store.append(sid, "turn/started", {}, turn_id="t")
    store.append(sid, "user/message", {"content": "question"}, turn_id="t")
    store.append(
        sid,
        "system/section",
        {
            "section_id": "base",
            "kind": "base",
            "scope": "turn",
            "priority": 1,
            "content": "Use $HOME and ${VAR}",
            "source": "test",
            "version": "1",
        },
        turn_id="t",
    )
    cutoff = store.append(sid, "step/started", {}, turn_id="t", step_id="s").sequence
    projector = ContextProjector()
    kwargs = dict(session_id=sid, turn_id="t", step_id="s", through_sequence=cutoff)
    original = projector.project(store.events(sid), **kwargs)
    store.append(sid, "assistant/message", {"content": "future"}, turn_id="t", step_id="s2")
    replay = projector.project(store.events(sid), **kwargs)
    assert replay.context_hash == original.context_hash
    assert replay.system_prompt == "Use $HOME and ${VAR}"
    assert [m.content for m in replay.messages] == ["question"]
    store.append(
        sid,
        "system/section",
        {
            "section_id": "base",
            "kind": "base",
            "scope": "turn",
            "priority": 1,
            "content": "Route: $route",
            "source": "test",
            "version": "2",
            "template": True,
        },
        turn_id="t",
    )
    assert (
        projector.project(
            store.events(sid), session_id=sid, turn_id="t", step_id="s", route="local"
        ).system_prompt
        == "Route: local"
    )
    store.append(
        sid,
        "system/section",
        {
            "section_id": "base",
            "kind": "base",
            "scope": "turn",
            "priority": 1,
            "content": "$missing",
            "source": "test",
            "version": "3",
            "template": True,
        },
        turn_id="t",
    )
    with pytest.raises(ValueError, match="unresolved"):
        projector.project(store.events(sid), session_id=sid, turn_id="t", step_id="s")


def test_observation_queues_are_bounded_isolated_and_report_gaps():
    async def run():
        manager = SubagentManager(event_queue_size=32)
        for index in range(100):
            manager._publish_event(
                {
                    "type": "subagent_event",
                    "parent_id": "a",
                    "event": {"type": "tool_result", "result": str(index)},
                }
            )
        manager._publish_event(
            {
                "type": "subagent_event",
                "parent_id": "b",
                "event": {"type": "ended", "status": "completed"},
            }
        )
        assert manager._events["a"].qsize() == 32
        assert (await manager.next_event("b"))["event"]["type"] == "ended"
        gap = await manager.next_event("a")
        assert gap["event"]["type"] == "observation_gap"
        assert gap["event"]["dropped"] == 68
        assert len(manager.drain_events("a")) == 32

    asyncio.run(run())


def test_stream_redaction_across_every_split_and_exception_outputs(tmp_path, monkeypatch):
    ctx = context(tmp_path)
    secret = "unit-private-placeholder"
    ctx.session_store.redactor.add_sensitive_values((secret,))
    captured = []

    async def scripted(self, message):
        for split in range(len(secret) + 1):
            for channel, key in (("text_delta", "text"), ("thinking_delta", "thinking")):
                for part in (secret[:split], secret[split:]):
                    await self.event_sink({"type": channel, key: part})
            await self.event_sink({"type": "turn_complete"})
        raise ValueError(secret)

    monkeypatch.setattr(Agent, "run_complete", scripted)

    async def run():
        manager = SubagentManager()
        manager.event_callback = captured.append
        await manager.start({"description": "test", "prompt": "test"}, ctx)
        child = next(iter(manager.activations.values()))
        async with asyncio.timeout(3):
            while not child.settled:
                await asyncio.sleep(0)
        await manager.flush_observations()
        assert secret not in child.result
        assert secret not in json.dumps(captured)
        assert secret not in json.dumps(manager.drain_events("root"))
        assert secret not in json.dumps(
            [e.payload for e in ctx.session_store.events(ctx.session_id)]
        )
        assert any(e["event"]["type"] == "text_delta" for e in captured)
        await manager.drain()

    asyncio.run(run())


def test_observer_failure_keeps_delivery_and_persistence_failure_is_visible(tmp_path, monkeypatch):
    ctx = context(tmp_path)

    async def complete(self, message):
        return SimpleNamespace(text="findings")

    monkeypatch.setattr(Agent, "run_complete", complete)

    async def run():
        manager = SubagentManager()

        def broken(_event):
            raise ValueError("private observer detail")

        manager.event_callback = broken
        await manager.start({"description": "test", "prompt": "test"}, ctx)
        child = next(iter(manager.activations.values()))
        await wait_idle(child)
        controller = RunController(
            manager=manager,
            parent_id="root",
            parent_session_id=ctx.session_id,
            session_store=ctx.session_store,
        )
        deliveries = controller.collect_deliveries(defer_ack=True)
        assert len(deliveries) == 1
        controller.acknowledge_deliveries(deliveries)
        await manager.flush_observations()
        with pytest.raises(RuntimeError, match="observation callback failed"):
            manager.raise_background_errors()
        await manager.drain()

        manager = SubagentManager()
        original = ctx.session_store.append

        def fail_delivery(session_id, event_type, payload, **kwargs):
            if event_type == "agent/child_delivery_created":
                raise OSError("private database detail")
            return original(session_id, event_type, payload, **kwargs)

        monkeypatch.setattr(ctx.session_store, "append", fail_delivery)
        await manager.start({"description": "test", "prompt": "test"}, ctx)
        child = next(iter(manager.activations.values()))
        async with asyncio.timeout(2):
            while not child.settled:
                await asyncio.sleep(0)
        with pytest.raises(RuntimeError, match="persistence or cleanup failed"):
            manager.raise_background_errors()
        with pytest.raises(RuntimeError, match="cleanup failures"):
            await manager.drain()

    asyncio.run(run())


def test_recorded_request_can_be_reprojected_after_later_steps(tmp_path, monkeypatch):
    monkeypatch.setattr("paicli.agent.agent.SnapshotService.create", lambda *args: None)
    ctx = context(tmp_path)

    async def run():
        async with QueryEngine(
            llm_client=ctx.llm_client,
            tool_registry=ctx.tool_registry,
            config=ctx.config,
            cwd=ctx.cwd,
            session_store=ctx.session_store,
        ) as engine:
            await engine.ask_complete_async("delegate inspection")
            events = ctx.session_store.events(engine.session_id)
            for prepared in (e for e in events if e.event_type == "request/prepared"):
                view = ContextProjector().project(
                    events,
                    session_id=engine.session_id,
                    turn_id=prepared.turn_id,
                    step_id=prepared.step_id,
                    through_sequence=prepared.payload["through_sequence"],
                    **prepared.payload["projection"],
                )
                assert view.context_hash == prepared.payload["context_hash"]

    asyncio.run(run())


def test_stream_limit_and_two_children_do_not_share_buffers(tmp_path, monkeypatch):
    ctx = context(tmp_path)
    secret = "unit-private-placeholder"
    ctx.session_store.redactor.add_sensitive_values((secret,))
    captured = []

    async def scripted(self, message):
        await self.event_sink({"type": "text_delta", "text": secret[:8]})
        await asyncio.sleep(0)
        await self.event_sink({"type": "text_delta", "text": secret[8:]})
        await self.event_sink({"type": "turn_complete"})
        await self.event_sink({"type": "thinking_delta", "thinking": "x" * 65537})
        return SimpleNamespace(text="done")

    monkeypatch.setattr(Agent, "run_complete", scripted)

    async def run():
        manager = SubagentManager()
        manager.event_callback = captured.append
        for name in ("first", "second"):
            await manager.start({"description": name, "prompt": name}, ctx)
        for child in manager.activations.values():
            await wait_idle(child)
        await manager.flush_observations()
        text = [e for e in captured if e["event"]["type"] == "text_delta"]
        assert len(text) == 2 and len({e["agent"] for e in text}) == 2
        assert all(e["event"]["text"] == "[redacted]" for e in text)
        assert secret not in json.dumps(captured)
        assert sum("buffer limit" in str(e) for e in captured) == 2
        await manager.drain()

    asyncio.run(run())


def test_interrupted_partial_stream_is_hidden_and_plain_renderer_shows_status(
    tmp_path, monkeypatch, capsys
):
    from paicli.render.plain import PlainRenderer

    ctx = context(tmp_path)
    captured = []

    async def partial(self, message):
        await self.event_sink({"type": "text_delta", "text": "unit-private-prefix"})
        raise ValueError("provider failed")

    monkeypatch.setattr(Agent, "run_complete", partial)

    async def run():
        manager = SubagentManager()
        manager.event_callback = captured.append
        await manager.start({"description": "test", "prompt": "test"}, ctx)
        child = next(iter(manager.activations.values()))
        async with asyncio.timeout(2):
            while not child.settled:
                await asyncio.sleep(0)
        await manager.flush_observations()
        assert "unit-private-prefix" not in json.dumps(captured)
        renderer = PlainRenderer()
        for event in captured:
            renderer.handle(event)
        assert "ended" in capsys.readouterr().out
        await manager.drain()

    asyncio.run(run())


def test_runtime_turn_closes_idle_children(tmp_path, monkeypatch):
    from paicli.runtime.api import RuntimeApiServer

    ctx = context(tmp_path)
    ctx.config.llm.api_key = "unit-placeholder"
    engines = []

    def engine_factory(**kwargs):
        engine = QueryEngine(**kwargs)
        engines.append(engine)
        return engine

    async def registry(**kwargs):
        return ToolRegistry(), None

    monkeypatch.setenv("HOME", str(tmp_path / "home"))
    monkeypatch.setattr("paicli.agent.agent.SnapshotService.create", lambda *args: None)
    monkeypatch.setattr("paicli.runtime.api.QueryEngine", engine_factory)
    monkeypatch.setattr("paicli.runtime.api.build_tool_registry", registry)
    monkeypatch.setattr("paicli.runtime.api.create_llm_client", lambda config: ctx.llm_client)

    async def run():
        server = RuntimeApiServer(
            cwd=ctx.cwd, config=ctx.config, api_key="unit-runtime-placeholder"
        )
        session_id = server._create_session()
        async with asyncio.timeout(3):
            await server._run_turn(session_id, "delegate inspection")
        manager = engines[0]._last_agent.subagent_manager
        assert all(a.settled for a in manager.activations.values())
        assert engines[0]._react_agent is None

    asyncio.run(run())


def test_hanging_provider_interrupt_is_bounded_by_host_drain(tmp_path):
    from paicli.agent.subagents import ProviderCapabilities

    ctx = context(tmp_path)

    class HangingProvider:
        provider_name = "hanging"
        capabilities = ProviderCapabilities()

        async def run(self, manager, activation, payload, context):
            await asyncio.Event().wait()

        async def interrupt(self, activation):
            await asyncio.Event().wait()

        async def dispose(self, activation):
            return None

    async def run():
        manager = SubagentManager(drain_timeout=0.1)
        manager.register_provider(HangingProvider())
        await manager.start({"description": "test", "prompt": "test", "provider": "hanging"}, ctx)
        async with asyncio.timeout(1):
            with pytest.raises(TimeoutError, match="drain exceeded"):
                await manager.drain()
        child = next(iter(manager.activations.values()))
        assert child.error_code == "HOST_DRAIN_TIMEOUT"
        child.task.cancel()
        await asyncio.gather(child.task, return_exceptions=True)
        async with asyncio.timeout(1):
            while not child.settled:
                await asyncio.sleep(0)

    asyncio.run(run())
