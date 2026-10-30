from __future__ import annotations

import asyncio

import pytest

from paicli.agent.subagents import ActivationState, ProviderCapabilities, SubagentManager
from paicli.config import load_config
from paicli.session import SessionStore
from paicli.tools.approval import ApprovalBroker
from paicli.tools.base import ToolContext
from paicli.tools.registry import ToolRegistry


class FakeClient:
    model_name = "fake-model"
    provider_name = "fake-provider"
    max_context_window = 4096
    subagent_capabilities = ProviderCapabilities(
        continuable=True,
        queue_idle=True,
        steer_running=True,
        supports_cancel=True,
        supports_live_events=True,
    )


def _context(tmp_path):
    config = load_config(project_root=tmp_path)
    config.llm.api_key = "test-key-not-a-secret"
    store = SessionStore(tmp_path / "runtime.db")
    session_id = store.create_session()
    return ToolContext(
        cwd=str(tmp_path),
        config=config,
        session_store=store,
        session_id=session_id,
        llm_client=FakeClient(),
        tool_registry=ToolRegistry(),
    )


def test_subagent_admission_enforces_capacity_and_cwd(tmp_path, monkeypatch):
    from paicli.agent.agent import Agent

    async def never_finishes(self, message):
        await asyncio.sleep(60)

    monkeypatch.setattr(Agent, "run_complete", never_finishes)
    manager = SubagentManager(max_active=1)
    context = _context(tmp_path)

    async def run():
        started = await manager.start({"description": "first", "prompt": "work"}, context)
        assert not started.is_error
        rejected = await manager.start({"description": "second", "prompt": "work"}, context)
        assert rejected.is_error
        assert "ACTIVATION_LIMIT_REACHED" in rejected.content
        outside = await manager.start(
            {
                "description": "outside",
                "prompt": "work",
                "cwd": str(tmp_path.parent),
            },
            context,
        )
        assert outside.is_error
        assert "INVALID_CWD" in outside.content
        await manager.drain()

    asyncio.run(run())


def test_subagent_deadline_cancels_and_settles_once(tmp_path, monkeypatch):
    from paicli.agent.agent import Agent

    async def never_finishes(self, message):
        await asyncio.sleep(60)

    monkeypatch.setattr(Agent, "run_complete", never_finishes)
    manager = SubagentManager(max_active=1)
    context = _context(tmp_path)

    async def run():
        started = await manager.start(
            {"description": "deadline", "prompt": "work", "timeout": 0.01}, context
        )
        assert not started.is_error
        activation = next(iter(manager.activations.values()))
        await asyncio.wait_for(asyncio.gather(activation.task, return_exceptions=True), timeout=1)
        await asyncio.gather(*tuple(manager._settlers))
        assert activation.status == ActivationState.TIMED_OUT
        assert activation.error_code == "CHILD_DEADLINE_EXCEEDED"
        assert activation.settled
        assert len(manager.completed_for("root")) == 1
        await manager.drain()

    asyncio.run(run())


def test_activation_state_machine_rejects_illegal_and_terminal_transitions(tmp_path, monkeypatch):
    from paicli.agent.agent import Agent

    async def never_finishes(self, message):
        await asyncio.sleep(60)

    monkeypatch.setattr(Agent, "run_complete", never_finishes)
    manager = SubagentManager()
    context = _context(tmp_path)

    async def run():
        started = await manager.start({"description": "state", "prompt": "work"}, context)
        assert not started.is_error
        activation = next(iter(manager.activations.values()))
        assert activation.status == ActivationState.RUNNING
        with pytest.raises(RuntimeError, match="illegal subagent transition"):
            manager.transition(activation, ActivationState.STARTING)
        assert await manager.interrupt(activation.id)
        assert activation.status == ActivationState.CANCELED
        with pytest.raises(RuntimeError, match="illegal subagent transition"):
            manager.transition(activation, ActivationState.RUNNING)
        await manager.drain()

    asyncio.run(run())


def test_subagent_child_registry_isolated_from_parent(tmp_path):
    registry = ToolRegistry()
    parent_names = registry.list_names()
    cloned = registry.clone()
    assert cloned is not registry
    assert cloned.list_names() == parent_names


def test_send_message_continues_a_parked_local_child(tmp_path, monkeypatch):
    from paicli.agent.agent import Agent
    from paicli.agent.subagents import make_subagent_control_tools

    calls = 0

    async def scripted(self, message):
        nonlocal calls
        calls += 1
        return SimpleResult(f"reply-{calls}:{message}")

    class SimpleResult:
        def __init__(self, text):
            self.text = text

    monkeypatch.setattr(Agent, "run_complete", scripted)
    manager = SubagentManager()
    context = _context(tmp_path)
    context.subagent_manager = manager
    context.agent_id = "root"

    async def run():
        started = await manager.start({"description": "continuable", "prompt": "first"}, context)
        assert not started.is_error
        activation = next(iter(manager.activations.values()))
        await asyncio.wait_for(manager._condition.acquire(), timeout=1)
        manager._condition.release()
        while activation.status != ActivationState.IDLE_WAITING:
            await asyncio.sleep(0)
        send_tool = next(
            tool for tool in make_subagent_control_tools() if tool.name == "send_message"
        )
        result = await send_tool.execute(
            {"agent_id": activation.id, "message": "follow-up"}, context
        )
        assert not result.is_error
        while calls < 2:
            await asyncio.sleep(0)
        await manager.interrupt(activation.id)
        assert calls == 2
        assert activation.status == ActivationState.CANCELED
        await manager.drain()

    asyncio.run(run())


def test_idle_children_do_not_consume_active_capacity(tmp_path, monkeypatch):
    from paicli.agent.agent import Agent

    async def completes(self, message):
        class Result:
            text = f"done:{message}"

        return Result()

    monkeypatch.setattr(Agent, "run_complete", completes)
    manager = SubagentManager(max_active=1, max_resident=2)
    context = _context(tmp_path)

    async def run():
        first = await manager.start({"description": "one", "prompt": "first"}, context)
        assert not first.is_error
        first_activation = next(iter(manager.activations.values()))
        while first_activation.status != ActivationState.IDLE_WAITING:
            await asyncio.sleep(0)
        second = await manager.start({"description": "two", "prompt": "second"}, context)
        assert not second.is_error
        second_activation = manager.activations[second.content.split()[2].rstrip(".")]
        while second_activation.status != ActivationState.IDLE_WAITING:
            await asyncio.sleep(0)
        third = await manager.start({"description": "three", "prompt": "third"}, context)
        assert third.is_error
        assert "RESIDENT_ACTIVATION_LIMIT_REACHED" in third.content
        await manager.drain()

    asyncio.run(run())


def test_dispose_cancels_activation_descendants(tmp_path, monkeypatch):
    from paicli.agent.agent import Agent

    async def never_finishes(self, message):
        await asyncio.sleep(60)

    monkeypatch.setattr(Agent, "run_complete", never_finishes)
    manager = SubagentManager(max_depth=3)
    context = _context(tmp_path)

    async def run():
        await manager.start({"description": "parent", "prompt": "work"}, context)
        parent = next(iter(manager.activations.values()))
        child_context = _context(tmp_path)
        child_context.subagent_manager = manager
        child_context.agent_id = parent.id
        await manager.start({"description": "child", "prompt": "work"}, child_context)
        child = manager.activations[max(manager.activations)]
        assert await manager.dispose(parent.id)
        assert parent.status == ActivationState.DISPOSED
        assert child.status == ActivationState.DISPOSED
        await manager.drain()

    asyncio.run(run())


def test_approval_broker_serializes_presenter_calls():
    active = 0
    peak = 0
    order = []

    async def presenter(request):
        nonlocal active, peak
        active += 1
        peak = max(peak, active)
        order.append(request["id"])
        await asyncio.sleep(0.01)
        active -= 1
        return "approve"

    broker = ApprovalBroker(presenter)

    async def run():
        results = await asyncio.gather(
            broker.request({"id": "first"}),
            broker.request({"id": "second"}),
        )
        assert results == ["approve", "approve"]
        assert peak == 1
        assert order == ["first", "second"]
        await broker.close()

    asyncio.run(run())


def test_send_message_rejects_cross_tree_child(tmp_path, monkeypatch):
    from paicli.agent.subagents import make_subagent_control_tools

    manager = SubagentManager()
    context = _context(tmp_path)
    context.subagent_manager = manager
    context.agent_id = "root"

    async def run():
        result = await next(
            tool for tool in make_subagent_control_tools() if tool.name == "send_message"
        ).execute({"agent_id": "agent_missing", "message": "hello"}, context)
        assert result.is_error
        assert "Unknown direct child" in result.content

    asyncio.run(run())


def test_send_message_to_running_child_is_consumed_at_safe_boundary(tmp_path, monkeypatch):
    from paicli.agent.agent import Agent
    from paicli.agent.subagents import make_subagent_control_tools

    calls = 0
    release_first = asyncio.Event()
    seen_prompts = []

    class Result:
        def __init__(self, text):
            self.text = text

    async def slow_first_turn(self, message):
        nonlocal calls
        calls += 1
        seen_prompts.append(message)
        if calls == 1:
            await release_first.wait()
        return Result(f"result-{calls}")

    monkeypatch.setattr(Agent, "run_complete", slow_first_turn)
    manager = SubagentManager()
    context = _context(tmp_path)
    context.subagent_manager = manager
    context.agent_id = "root"

    async def run():
        started = await manager.start({"description": "running", "prompt": "initial"}, context)
        assert not started.is_error
        activation = next(iter(manager.activations.values()))
        await asyncio.sleep(0)
        send = next(tool for tool in make_subagent_control_tools() if tool.name == "send_message")
        response = await send.execute(
            {"agent_id": activation.id, "message": "please include this"}, context
        )
        assert "steered" in response.content
        release_first.set()
        while calls < 2:
            await asyncio.sleep(0)
        assert "please include this" in seen_prompts[1]
        await manager.interrupt(activation.id)
        await manager.drain()

    asyncio.run(run())


def test_settlement_creates_and_acknowledges_durable_delivery(tmp_path, monkeypatch):
    from paicli.agent.agent import Agent
    from paicli.agent.run_controller import RunController

    async def completes(self, message):
        class Result:
            text = "child result"

        return Result()

    monkeypatch.setattr(Agent, "run_complete", completes)
    manager = SubagentManager()
    context = _context(tmp_path)

    async def run():
        started = await manager.start({"description": "delivery", "prompt": "work"}, context)
        assert not started.is_error
        activation = next(iter(manager.activations.values()))
        while activation.status != ActivationState.IDLE_WAITING:
            await asyncio.sleep(0)
        await manager.interrupt(activation.id)
        while not activation.settled:
            await asyncio.sleep(0)
        events = context.session_store.events(context.session_id)
        created = [event for event in events if event.event_type == "agent/child_delivery_created"]
        assert len(created) == 2
        assert created[-1].payload["delivery_id"] == activation.delivery_id
        assert created[0].payload["result_status"] == "completed"
        assert created[-1].payload["result_status"] == "canceled"
        controller = RunController(
            manager=manager,
            parent_id="root",
            parent_session_id=context.session_id,
            session_store=context.session_store,
        )
        deliveries = controller.collect_deliveries()
        assert len(deliveries) == 2
        assert deliveries[-1].delivery_id == activation.delivery_id
        assert controller.collect_deliveries() == []
        acked = [
            event
            for event in context.session_store.events(context.session_id)
            if event.event_type == "agent/child_delivery_acknowledged"
        ]
        assert len(acked) == 2
        await manager.drain()

    asyncio.run(run())


def test_pending_delivery_recovers_after_manager_restart(tmp_path):
    context = _context(tmp_path)
    parent_session_id = context.session_id
    delivery_id = "delivery_recovery_test"
    context.session_store.append(
        parent_session_id,
        "agent/child_delivery_created",
        {
            "delivery_id": delivery_id,
            "child_id": "agent_child",
            "status": "pending",
            "result_status": "completed",
            "content": "recovered result",
        },
        source="test",
    )
    manager = SubagentManager()
    from paicli.agent.run_controller import RunController

    controller = RunController(
        manager=manager,
        parent_id="root",
        parent_session_id=parent_session_id,
        session_store=context.session_store,
    )
    recovered = controller.collect_deliveries(defer_ack=True)
    assert len(recovered) == 1
    assert recovered[0].result == "recovered result"
    assert recovered[0].delivery_id == delivery_id
    controller.acknowledge_deliveries(recovered)
    assert controller.collect_deliveries() == []
    assert any(
        event.event_type == "agent/child_delivery_acknowledged"
        and event.payload["delivery_id"] == delivery_id
        for event in context.session_store.events(parent_session_id)
    )


def test_approval_broker_cancel_and_close_active_presenter():
    started = asyncio.Event()
    canceled = asyncio.Event()

    async def presenter(request):
        started.set()
        try:
            await asyncio.sleep(60)
        except asyncio.CancelledError:
            canceled.set()
            raise
        return "approve"

    broker = ApprovalBroker(presenter)

    async def run():
        request_task = asyncio.create_task(
            broker.request({"agent_id": "agent_child", "session_id": "session_child"})
        )
        await started.wait()
        metadata = broker.pending_requests()
        assert metadata[0]["agent_id"] == "agent_child"
        request_id = metadata[0]["request_id"]
        assert await broker.cancel(request_id)
        assert await request_task == "deny"
        await broker.close()
        assert canceled.is_set()
        assert broker.pending_requests() == []

    asyncio.run(run())


def test_provider_capabilities_default_to_one_shot_for_non_llm_adapter():
    class ExternalAdapter:
        provider_name = "external"

    assert SubagentManager._capabilities_for(ExternalAdapter()) == ProviderCapabilities()

    class DeclaredAdapter(ExternalAdapter):
        subagent_capabilities = {
            "supports_cancel": True,
            "external_process": True,
        }

    capabilities = SubagentManager._capabilities_for(DeclaredAdapter())
    assert capabilities.supports_cancel
    assert capabilities.external_process
    assert not capabilities.continuable


def test_one_shot_activation_does_not_enter_idle_waiting(tmp_path, monkeypatch):
    class OneShotProvider:
        provider_name = "external"
        capabilities = ProviderCapabilities(
            continuable=False, supports_cancel=True, external_process=True
        )

        def __init__(self):
            self.started = 0
            self.disposed = 0

        async def run(self, manager, activation, payload, context):
            self.started += 1
            from paicli.agent.subagents import SubagentResult

            return SubagentResult(output="one-shot", stop_reason="completed")

        async def interrupt(self, activation):
            activation.task.cancel()

        async def dispose(self, activation):
            self.disposed += 1

    context = _context(tmp_path)
    manager = SubagentManager()
    provider = OneShotProvider()
    manager.register_provider(provider)

    async def run():
        started = await manager.start(
            {"description": "one-shot", "prompt": "work", "provider": "external"}, context
        )
        assert not started.is_error
        activation = next(iter(manager.activations.values()))
        while not activation.settled:
            await asyncio.sleep(0)
        assert activation.status == ActivationState.COMPLETED
        assert activation.status != ActivationState.IDLE_WAITING
        assert activation.provider == "external"
        assert provider.started == 1
        assert provider.disposed == 1
        assert activation.provider_disposed
        await manager.drain()

    asyncio.run(run())


def test_external_provider_cleanup_failure_is_reported_separately(tmp_path):
    from paicli.agent.subagents import SubagentResult

    class ExternalProvider:
        provider_name = "external-failing-cleanup"
        capabilities = ProviderCapabilities(external_process=True)

        async def run(self, manager, activation, payload, context):
            return SubagentResult(output="business result", stop_reason="completed")

        async def interrupt(self, activation):
            activation.task.cancel()

        async def dispose(self, activation):
            raise OSError("provider cleanup failed")

    context = _context(tmp_path)
    manager = SubagentManager()
    manager.register_provider(ExternalProvider())

    async def run():
        started = await manager.start(
            {
                "description": "cleanup failure",
                "prompt": "work",
                "provider": "external-failing-cleanup",
            },
            context,
        )
        assert not started.is_error
        activation = next(iter(manager.activations.values()))
        while not activation.settled:
            await asyncio.sleep(0)
        assert activation.result == "business result"
        assert activation.final_result.output == "business result"
        assert activation.final_result.stop_reason == "completed"
        assert "OSError" in activation.cleanup_error
        assert "raw diagnostic withheld" in activation.cleanup_error
        with pytest.raises(RuntimeError, match="cleanup failures"):
            await manager.drain()

    asyncio.run(run())


def test_provider_capabilities_gate_requested_options(tmp_path):
    class MinimalProvider:
        provider_name = "minimal"
        capabilities = ProviderCapabilities(external_process=True)

        async def run(self, manager, activation, payload, context):
            return "unused"

        async def interrupt(self, activation):
            activation.task.cancel()

        async def dispose(self, activation):
            return None

    context = _context(tmp_path)
    manager = SubagentManager()
    manager.register_provider(MinimalProvider())

    async def run():
        result = await manager.start(
            {
                "description": "options",
                "prompt": "work",
                "provider": "minimal",
                "output_schema": {"type": "object"},
            },
            context,
        )
        assert result.is_error
        assert result.content.startswith("PROVIDER_OPTION_UNSUPPORTED")

    asyncio.run(run())


def test_external_provider_error_result_maps_to_failed_activation(tmp_path):
    from paicli.agent.subagents import SubagentResult

    class FailedProvider:
        provider_name = "external-error"
        capabilities = ProviderCapabilities(external_process=True)

        async def run(self, manager, activation, payload, context):
            return SubagentResult(
                output="partial output",
                stop_reason="error",
                diagnostic="diagnostic " + "x" * 5000,
            )

        async def interrupt(self, activation):
            activation.task.cancel()

        async def dispose(self, activation):
            return None

    context = _context(tmp_path)
    manager = SubagentManager()
    manager.register_provider(FailedProvider())

    async def run():
        await manager.start(
            {"description": "failed", "prompt": "work", "provider": "external-error"},
            context,
        )
        activation = next(iter(manager.activations.values()))
        while not activation.settled:
            await asyncio.sleep(0)
        assert activation.status == ActivationState.FAILED
        assert activation.result == "partial output"
        assert activation.final_result.stop_reason == "error"
        assert len(activation.final_result.diagnostic.encode("utf-8")) <= 4096
        assert activation.final_result.diagnostic.endswith("[diagnostic truncated]")
        await manager.drain()

    asyncio.run(run())


def test_requested_unregistered_provider_fails_closed(tmp_path):
    context = _context(tmp_path)
    manager = SubagentManager()

    async def run():
        result = await manager.start(
            {"description": "provider", "prompt": "work", "provider": "acp"}, context
        )
        assert result.is_error
        assert result.content.startswith("PROVIDER_UNAVAILABLE")

    asyncio.run(run())


def test_session_persistence_failure_rolls_back_activation(tmp_path, monkeypatch):
    context = _context(tmp_path)
    manager = SubagentManager()

    original = context.session_store.append
    calls = 0
    catalog_calls = 0

    def fail_catalog(session_id, event_type, payload, **kwargs):
        nonlocal calls, catalog_calls
        calls += 1
        if event_type == "agent/subagent_catalog":
            catalog_calls += 1
            raise OSError("store unavailable")
        return original(session_id, event_type, payload, **kwargs)

    monkeypatch.setattr(context.session_store, "append", fail_catalog)

    async def run():
        result = await manager.start({"description": "persist", "prompt": "work"}, context)
        assert result.is_error
        assert "SESSION_PERSISTENCE_UNAVAILABLE" in result.content
        assert manager.activations == {}
        assert calls == 3
        assert catalog_calls == 1

    asyncio.run(run())


def test_recovery_marks_started_child_orphaned_without_replay(tmp_path):
    context = _context(tmp_path)
    child_session = context.session_store.create_session()
    context.session_store.append(
        context.session_id,
        "agent/child_started",
        {"child_id": "agent_orphan", "child_session_id": child_session},
        source="test",
    )
    manager = SubagentManager()
    manager.session_store = context.session_store
    recovered = manager.recover_orphaned_children("root", context.session_id)
    assert recovered == ["agent_orphan"]
    assert manager.recovered_for("root")[0]["status"] == "orphaned"
    assert manager.recover_orphaned_children("root", context.session_id) == ["agent_orphan"]
    events = context.session_store.events(context.session_id)
    orphan = [event for event in events if event.event_type == "agent/child_orphaned"]
    assert orphan[0].payload["replay_allowed"] is False


def test_subagent_tool_allowlist_is_applied_to_child_registry(tmp_path, monkeypatch):
    from paicli.agent.agent import Agent
    from paicli.tools.base import Tool, ToolResult, object_schema

    async def hold(self, message):
        await asyncio.sleep(60)

    monkeypatch.setattr(Agent, "run_complete", hold)
    context = _context(tmp_path)
    allowed = Tool(
        name="allowed_tool",
        description="allowed",
        parameters=object_schema({}),
        handler=lambda payload, ctx: asyncio.sleep(0, result=ToolResult("ok")),
    )
    blocked = Tool(
        name="blocked_tool",
        description="blocked",
        parameters=object_schema({}),
        handler=lambda payload, ctx: asyncio.sleep(0, result=ToolResult("ok")),
    )
    context.tool_registry.register_all([allowed, blocked])
    context.config.policy.subagent_tool_allowlist = ["allowed_tool"]
    manager = SubagentManager()

    async def run():
        started = await manager.start({"description": "allowlist", "prompt": "work"}, context)
        assert not started.is_error
        activation = next(iter(manager.activations.values()))
        child = Agent(
            llm_client=context.llm_client,
            tool_registry=context.tool_registry.clone(
                allowed_names=set(activation.policy_snapshot["subagent_tool_allowlist"])
            ),
            config=activation.config_snapshot,
            cwd=str(tmp_path),
            session_store=context.session_store,
            session_id=activation.session_id,
            subagent_manager=manager,
            agent_id=activation.id,
        )
        assert child.tool_registry.list_names() == ["allowed_tool"]
        await manager.drain()

    asyncio.run(run())


def test_file_tools_enforce_child_cwd_boundary(tmp_path):
    from paicli.tools.builtins import _read_file

    outside = tmp_path.parent / f"{tmp_path.name}-outside.txt"
    outside.write_text("outside", encoding="utf-8")
    config = load_config(project_root=tmp_path)
    context = ToolContext(cwd=str(tmp_path), config=config)
    try:
        with pytest.raises(Exception, match="path escapes workspace"):
            asyncio.run(_read_file({"path": str(outside)}, context))
    finally:
        outside.unlink()


def test_deferred_ack_marks_activation_and_persists_ack(tmp_path, monkeypatch):
    from paicli.agent.agent import Agent
    from paicli.agent.run_controller import RunController

    async def complete(self, message):
        class Result:
            text = "ready"

        return Result()

    monkeypatch.setattr(Agent, "run_complete", complete)
    context = _context(tmp_path)
    manager = SubagentManager()

    async def run():
        await manager.start({"description": "ack", "prompt": "work"}, context)
        activation = next(iter(manager.activations.values()))
        while activation.status != ActivationState.IDLE_WAITING:
            await asyncio.sleep(0)
        await manager.interrupt(activation.id)
        while not activation.settled:
            await asyncio.sleep(0)
        controller = RunController(
            manager=manager,
            parent_id="root",
            parent_session_id=context.session_id,
            session_store=context.session_store,
        )
        deliveries = controller.collect_deliveries(defer_ack=True)
        assert deliveries
        controller.acknowledge_deliveries(deliveries)
        assert activation.delivered
        assert any(
            event.event_type == "agent/child_delivery_acknowledged"
            and event.payload["delivery_id"] == activation.delivery_id
            for event in context.session_store.events(context.session_id)
        )
        await manager.drain()

    asyncio.run(run())
