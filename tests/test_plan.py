from __future__ import annotations

import asyncio
from typing import Any

import pytest

from libi.agent import PlanExecuteAgent
from libi.config import load_config
from libi.llm.base import ProviderRequestError
from libi.plan import ExecutionPlan, Planner, Task, TaskType
from libi.session import ContextProjector, SessionStore
from libi.tools import ToolRegistry, get_builtin_tools
from libi.tools.base import Tool, ToolResult, object_schema


def test_execution_plan_exposes_dag_batches():
    plan = ExecutionPlan(id="plan_1", goal="demo")
    task_1 = Task("task_1", "read a", TaskType.FILE_READ)
    task_2 = Task("task_2", "read b", TaskType.FILE_READ)
    task_3 = Task("task_3", "summarize", TaskType.ANALYSIS, ["task_1", "task_2"])

    plan.add_task(task_1)
    plan.add_task(task_2)
    plan.add_task(task_3)

    assert plan.execution_order() == ["task_1", "task_2", "task_3"]
    assert plan.execution_batches() == [[task_1, task_2], [task_3]]
    assert plan.executable_tasks() == [task_1, task_2]
    task_1.mark_completed("done")
    assert plan.executable_tasks() == [task_2]


def test_execution_plan_summary_uses_chinese_labels():
    plan = ExecutionPlan(id="plan_1", goal="检查项目")
    plan.summary = "先检查再汇总"
    plan.add_task(Task("task_1", "检查文件", TaskType.FILE_READ))

    summary = plan.summarize()

    assert "计划 plan_1：先检查再汇总" in summary
    assert "任务数：1" in summary
    assert "当前可执行：1" in summary


def test_planner_parses_tasks_and_dependencies():
    planner = Planner(FakeClient())

    plan = planner.parse_plan(
        "demo",
        """
        ```json
        {
          "summary": "demo plan",
          "tasks": [
            {"id": "a", "description": "A", "type": "COMMAND", "dependencies": []},
            {"id": "b", "description": "B", "type": "VERIFICATION", "dependencies": ["a"]}
          ]
        }
        ```
        """,
    )

    assert plan.summary == "demo plan"
    assert plan.get_task("task_2").dependencies == ["task_1"]
    assert plan.get_task("task_2").type == TaskType.VERIFICATION


@pytest.mark.parametrize("plan_json", ["not JSON", "{}", '{"tasks":[]}', '{"tasks":"bad"}'])
def test_planner_rejects_missing_or_invalid_task_lists(plan_json):
    with pytest.raises(ValueError):
        Planner(FakeClient()).parse_plan("goal", plan_json)


@pytest.mark.parametrize("dependencies", [["a"], ["b"]])
def test_planner_rejects_self_or_mutual_dependency_cycles(dependencies):
    import json

    raw = {
        "tasks": [
            {"id": "a", "dependencies": dependencies},
            {"id": "b", "dependencies": ["a"]},
        ]
    }
    with pytest.raises(ValueError, match="cyclic"):
        Planner(FakeClient()).parse_plan("goal", json.dumps(raw))


def test_failed_dependency_never_becomes_executable():
    plan = ExecutionPlan(id="failure", goal="goal")
    dependency = Task("a", "first")
    dependent = Task("b", "second", dependencies=["a"])
    plan.add_task(dependent)
    plan.add_task(dependency)
    assert dependency.dependents == ["b"]
    dependency.mark_failed("failed")
    assert plan.executable_tasks() == []
    assert plan.has_failed() and not plan.is_all_completed()
    assert plan.progress() == 0


def test_plan_execute_runs_independent_tasks_in_parallel(tmp_path):
    client = ParallelPlanClient()
    registry = ToolRegistry()
    registry.register_all(get_builtin_tools())
    config = load_config(project_root=tmp_path)
    config.policy.hitl_mode = "never"
    agent = PlanExecuteAgent(
        llm_client=client,
        tool_registry=registry,
        config=config,
        cwd=str(tmp_path),
        session_store=SessionStore(tmp_path / "runtime.db"),
    )

    async def run():
        text = ""
        events = []
        async for event in agent.run("先做 A 和 B，然后汇总"):
            events.append(event)
            if event.get("type") == "text_delta":
                text += str(event.get("text") or "")
            elif event.get("type") == "error":
                raise event["error"]
        return text, events

    result, events = asyncio.run(run())

    assert "正在规划任务：" in result
    assert "开始执行计划" in result
    assert "已完成 [task_1]" in result
    assert "已完成 [task_2]" in result
    assert "计划执行完成" in result
    assert "Planning task" not in result
    assert "Completed [" not in result
    assert client.task_system_prompts
    assert all(
        "所有进度说明、分析和最终结果都必须使用中文" in prompt
        for prompt in client.task_system_prompts
    )
    assert client.peak_concurrency == 2
    assert any(
        event.get("type") == "thinking_delta" and event.get("phase") == "planning"
        for event in events
    )
    assert {
        event.get("task_id")
        for event in events
        if event.get("type") == "thinking_delta" and event.get("phase") == "execution"
    } == {"task_1", "task_2"}
    assert {
        event.get("task_id") for event in events if event.get("type") == "plan_task_started"
    } == {"task_1", "task_2"}
    store = agent.session_store
    children = [item for item in store.list_sessions(include_children=True) if item.kind == "child"]
    assert len(children) == 2
    assert all(item.parent_session_id == agent.session_id for item in children)
    assert all(item.status == "completed" for item in children)
    assert len(store.list_sessions()) == 1
    for child in children:
        child_events = store.events(child.session_id)
        assert any(event.event_type == "request/prepared" for event in child_events)
        assert [
            m.role
            for m in ContextProjector()
            .project_session(child_events, session_id=child.session_id)
            .messages
        ] == ["user", "assistant"]
    parent_events = store.events(agent.session_id)
    assert sum(event.event_type == "turn/started" for event in parent_events) == 1
    assert sum(event.event_type == "agent/child_completed" for event in parent_events) == 2
    assert not any(event.event_type == "request/prepared" for event in parent_events)
    assert registry.get("subagent") is None


def test_plan_persists_provider_attempts_under_one_request(session_store, config, tmp_path):
    class PlannerClient:
        model_name = "plan-model"
        provider_name = "plan-provider"

        async def chat(self, messages, tools, *, system_prompt):
            for attempt, code in enumerate((503, 500), start=1):
                yield {
                    "type": "provider_attempt_failed",
                    "diagnostic": {
                        "attempt": attempt,
                        "stage": "response_headers",
                        "elapsed_ms": 35,
                        "error_type": "HTTPStatusError",
                        "retrying": attempt == 1,
                        "provider_error": {"kind": "http", "status_code": code},
                    },
                }
            yield {
                "type": "error",
                "error": ProviderRequestError("failed", kind="http", status_code=500),
            }

    agent = PlanExecuteAgent(
        llm_client=PlannerClient(),
        tool_registry=ToolRegistry(),
        config=config,
        cwd=str(tmp_path),
        session_store=session_store,
    )

    async def run():
        return [event async for event in agent.run("先检查 A，然后分析 B")]

    with pytest.raises(ProviderRequestError):
        asyncio.run(run())

    events = session_store.events(agent.session_id)
    attempts = [event for event in events if event.event_type == "request/attempt_failed"]
    assert [event.payload["attempt"] for event in attempts] == [1, 2]
    assert [event.payload["retrying"] for event in attempts] == [True, False]
    assert [event.payload["provider_error"]["status_code"] for event in attempts] == [503, 500]
    assert attempts[0].payload["request_id"] == attempts[1].payload["request_id"]
    assert attempts[0].payload["purpose"] == "planning"
    assert attempts[0].payload["provider"] == "plan-provider"
    assert attempts[0].payload["model"] == "plan-model"
    assert events[-1].event_type == "turn/failed"
    assert events[-1].payload["provider_error"] == {"kind": "http", "status_code": 500}


@pytest.mark.parametrize("fails", [False, True])
def test_plan_worker_records_tools_and_terminal_state(tmp_path, monkeypatch, fails):
    monkeypatch.setattr("libi.agent.agent.SnapshotService.create", lambda *args: None)
    monkeypatch.setattr("libi.agent.plan_execute.SnapshotService.create", lambda *args: None)
    config = load_config(project_root=tmp_path, env={})
    config.memory.compression_mode = "disabled"
    store = SessionStore(tmp_path / "runtime.db")
    registry = ToolRegistry()

    async def inspect(payload, context):
        assert context.session_store is store
        assert store.session_exists(context.session_id)
        return ToolResult("worker tool output")

    registry.register(Tool("inspect", "Inspect", object_schema({}), inspect))

    class WorkerClient:
        model_name = "fake"
        provider_name = "fake"
        max_context_window = 128_000

        async def chat(self, messages, tools, *, system_prompt):
            if not any(message.role == "tool" for message in messages):
                yield {
                    "type": "tool_call_delta",
                    "tool_call": {
                        "index": 0,
                        "id": "inspect-call",
                        "function": {"name": "inspect", "arguments": "{}"},
                    },
                }
                yield {"type": "message_end", "stop_reason": "tool_use"}
            elif fails:
                raise RuntimeError("worker provider failed")
            else:
                yield {"type": "text_delta", "text": "worker answer"}
                yield {"type": "message_end", "stop_reason": "end_turn"}

    class SingleTaskPlanner:
        async def stream_plan(self, message):
            plan = ExecutionPlan(id="one", goal=message)
            plan.add_task(Task("inspect", "inspect", TaskType.ANALYSIS))
            yield {"type": "plan_created", "plan": plan}

    agent = PlanExecuteAgent(
        llm_client=WorkerClient(),
        tool_registry=registry,
        config=config,
        cwd=str(tmp_path),
        session_store=store,
        planner=SingleTaskPlanner(),
    )

    async def run():
        return [event async for event in agent.run("inspect")]

    events = asyncio.run(run())
    child = next(
        item for item in store.list_sessions(include_children=True) if item.kind == "child"
    )
    assert child.status == ("failed" if fails else "completed")
    child_events = store.events(child.session_id)
    assert sum(event.event_type == "tool/result" for event in child_events) == 1
    parent = store.events(agent.session_id)
    assert any(
        event.event_type == ("agent/child_failed" if fails else "agent/child_completed")
        for event in parent
    )
    assert not any(event.event_type == "tool/result" for event in parent)
    assert events[-1]["type"] == "done"


class FakeClient:
    model_name = "fake-model"
    provider_name = "fake-provider"
    max_context_window = 1000

    async def chat(self, messages, tools, *, system_prompt):  # noqa: ARG002
        yield {"type": "text_delta", "text": "{}"}
        yield {"type": "message_end", "stop_reason": "end_turn"}


class ParallelPlanClient(FakeClient):
    def __init__(self):
        self.current_concurrency = 0
        self.peak_concurrency = 0
        self.ready = asyncio.Event()
        self.task_system_prompts: list[str] = []

    async def chat(self, messages, tools, *, system_prompt):  # noqa: ARG002
        body = _message_text(messages[-1].content)
        if "请为以下目标创建执行计划" in body:
            yield {"type": "thinking_delta", "thinking": "先拆分可并行任务"}
            yield {
                "type": "text_delta",
                "text": (
                    '{"summary":"parallel","tasks":['
                    '{"id":"a","description":"任务 A","type":"ANALYSIS","dependencies":[]},'
                    '{"id":"b","description":"任务 B","type":"ANALYSIS","dependencies":[]}'
                    "]}"
                ),
            }
            yield {"type": "message_end", "stop_reason": "end_turn"}
            return

        if "任务 A" in body or "任务 B" in body:
            self.task_system_prompts.append(system_prompt)
            self.current_concurrency += 1
            self.peak_concurrency = max(self.peak_concurrency, self.current_concurrency)
            if self.current_concurrency == 2:
                self.ready.set()
            await asyncio.wait_for(self.ready.wait(), timeout=2)
            self.current_concurrency -= 1
            text = "A 的结果" if "任务 A" in body else "B 的结果"
            yield {"type": "thinking_delta", "thinking": f"分析{text}"}
            yield {"type": "text_delta", "text": text}
            yield {"type": "message_end", "stop_reason": "end_turn"}
            return

        yield {"type": "text_delta", "text": "fallback"}
        yield {"type": "message_end", "stop_reason": "end_turn"}


def _message_text(content: Any) -> str:
    if isinstance(content, str):
        return content
    return str(content)
