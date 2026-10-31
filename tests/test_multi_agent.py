from __future__ import annotations

import asyncio
from typing import Any

import pytest

from paicli.agent.orchestrator import (
    AgentMessage,
    AgentMessageType,
    AgentOrchestrator,
    AgentRole,
    ExecutionStep,
    StepStatus,
    SubAgent,
)
from paicli.config import load_config
from paicli.tools import ToolRegistry, get_builtin_tools


def test_orchestrator_parses_steps_and_review_output(tmp_path, monkeypatch):
    monkeypatch.setenv("HOME", str(tmp_path / "home"))
    orchestrator = _orchestrator(tmp_path, FakeTeamClient())

    steps = orchestrator.parse_plan(
        """
        {"steps": [
          {"id": "a", "description": "A", "type": "ANALYSIS", "dependencies": []},
          {"id": "b", "description": "B", "type": "COMMAND", "dependencies": ["a"]}
        ]}
        """
    )

    assert [step.id for step in steps] == ["step_1", "step_2"]
    assert steps[1].dependencies == ["step_1"]
    assert orchestrator.parse_review_approval('{"approved": true, "issues": []}')
    assert not orchestrator.parse_review_approval("执行结果未通过审查")
    assert "缺少验证" in orchestrator.parse_review_issues(
        '{"approved": false, "issues": ["缺少验证"]}'
    )


@pytest.mark.parametrize(
    "plan, message",
    [
        ('{"steps":[null]}', "must be an object"),
        ('{"steps":[{"id":"a"},{"id":"a"}]}', "duplicate step id"),
        ('{"steps":[{"id":"a","dependencies":["missing"]}]}', "unknown dependency"),
        ('{"steps":[{"id":"a","dependencies":["a"]}]}', "cannot depend on itself"),
        (
            '{"steps":[{"id":"a","dependencies":["b"]},'
            '{"id":"b","dependencies":["a"]}]}',
            "dependency cycle",
        ),
        (
            '{"steps":[{"id":"a"},{"id":"b","dependencies":["a","a"]}]}',
            "repeats dependency",
        ),
    ],
)
def test_orchestrator_rejects_invalid_task_graph(tmp_path, monkeypatch, plan, message):
    monkeypatch.setenv("HOME", str(tmp_path / "home"))
    orchestrator = _orchestrator(tmp_path, FakeTeamClient())
    with pytest.raises(ValueError, match=message):
        orchestrator.parse_plan(plan)


def test_orchestrator_runs_independent_workers_in_parallel(tmp_path, monkeypatch):
    monkeypatch.setenv("HOME", str(tmp_path / "home"))
    client = ParallelTeamClient()
    orchestrator = _orchestrator(tmp_path, client)

    async def run():
        text = ""
        done = None
        events = []
        async for event in orchestrator.run("并行完成 A 和 B"):
            events.append(event)
            if event.get("type") == "text_delta":
                text += str(event.get("text") or "")
            elif event.get("type") == "done":
                done = event
            elif event.get("type") == "error":
                raise event["error"]
        return text, done, events

    result, done, events = asyncio.run(run())

    assert "Multi-Agent task completed" in result
    assert "Task A result" in result
    assert "Task B result" in result
    assert client.peak_concurrency == 2
    assert any(
        event.get("type") == "team_text_delta" and event.get("agent", "").startswith("worker-")
        for event in events
    )
    assert any(
        event.get("type") == "team_status" and event.get("phase") == "review" for event in events
    )
    assert events[-1]["type"] == "done"
    assert done is not None
    assert done["total_turns"] == 5


def test_orchestrator_streams_worker_events_before_worker_finishes(tmp_path, monkeypatch):
    monkeypatch.setenv("HOME", str(tmp_path / "home"))

    class StreamingTeamClient(FakeTeamClient):
        def __init__(self):
            self.release_worker = asyncio.Event()

        async def chat(self, messages, tools, *, system_prompt):  # noqa: ARG002
            body = _message_text(messages[-1].content)
            if "Create an execution plan" in body:
                yield {
                    "type": "text_delta",
                    "text": '{"steps":[{"id":"a","description":"Inspect files",'
                    '"type":"ANALYSIS","dependencies":[]}]}',
                }
                yield {"type": "message_end", "stop_reason": "end_turn"}
                return
            if "Original task" in body:
                yield {"type": "text_delta", "text": '{"approved":true,"issues":[]}'}
                yield {"type": "message_end", "stop_reason": "end_turn"}
                return
            yield {"type": "thinking_delta", "thinking": "worker started"}
            await self.release_worker.wait()
            yield {"type": "text_delta", "text": "inspection complete"}
            yield {"type": "message_end", "stop_reason": "end_turn"}

    client = StreamingTeamClient()
    orchestrator = _orchestrator(tmp_path, client)

    async def run():
        events = orchestrator.run("Inspect files")
        try:
            while True:
                event = await asyncio.wait_for(anext(events), timeout=1)
                if event.get("type") == "team_status" and event.get("message", "").startswith(
                    "Executing:"
                ):
                    break

            event = await asyncio.wait_for(anext(events), timeout=1)
            while event.get("type") == "context_compressed":
                event = await asyncio.wait_for(anext(events), timeout=1)
            assert event["type"] == "thinking_delta"
            assert event["thinking"] == "worker started"
            assert event["agent"] == "worker-1"
            assert event["task_id"] == "step_1"
        finally:
            client.release_worker.set()
            await events.aclose()

    asyncio.run(run())


def test_orchestrator_parses_nested_plan_worker_mode(tmp_path, monkeypatch):
    monkeypatch.setenv("HOME", str(tmp_path / "home"))
    orchestrator = _orchestrator(tmp_path, FakeTeamClient())

    steps = orchestrator.parse_plan(
        '{"steps":[{"id":"a","description":"complex","type":"ANALYSIS",'
        '"mode":"plan","dependencies":[]}]}'
    )

    assert steps[0].mode == "plan"
    assert (
        orchestrator.workers[0].skill_context_buffer
        is not orchestrator.workers[1].skill_context_buffer
    )


def test_subagent_can_delegate_its_task_to_plan_execute(tmp_path, monkeypatch):
    monkeypatch.setenv("HOME", str(tmp_path / "home"))
    config = load_config(project_root=tmp_path)
    registry = ToolRegistry()
    worker = SubAgent(
        name="worker-plan",
        role=AgentRole.WORKER,
        llm_client=FakeTeamClient(),
        tool_registry=registry,
        config=config,
        cwd=str(tmp_path),
    )

    async def fake_plan_run(self, message):  # noqa: ARG001
        yield {"type": "text_delta", "text": "nested plan completed"}
        yield {
            "type": "done",
            "usage": {"input_tokens": 10, "output_tokens": 5},
            "total_tokens": 15,
            "total_turns": 3,
        }

    monkeypatch.setattr("paicli.agent.plan_execute.PlanExecuteAgent.run", fake_plan_run)

    result = asyncio.run(worker.execute(AgentMessage.task("test", "complex task"), mode="plan"))

    assert result.type == AgentMessageType.RESULT
    assert result.content == "nested plan completed"
    assert result.usage.total_tokens == 15
    assert result.turns == 3


def test_subagent_forwards_react_events_without_forwarding_internal_text(tmp_path, monkeypatch):
    monkeypatch.setenv("HOME", str(tmp_path / "home"))
    worker = SubAgent(
        name="worker-1",
        role=AgentRole.WORKER,
        llm_client=FakeTeamClient(),
        tool_registry=ToolRegistry(),
        config=load_config(project_root=tmp_path),
        cwd=str(tmp_path),
    )

    async def fake_query(**kwargs):  # noqa: ARG001
        yield {"type": "text_delta", "text": "internal draft"}
        yield {"type": "thinking_delta", "thinking": "reasoning"}
        yield {"type": "tool_call", "name": "list_dir", "input": {"path": "."}}
        yield {"type": "tool_result", "name": "list_dir", "result": "README.md"}
        yield {"type": "turn_complete", "turn": 1, "stop_reason": "end_turn"}
        yield {"type": "done", "total_turns": 1, "usage": {}}

    monkeypatch.setattr("paicli.agent.orchestrator.query", fake_query)
    forwarded = []

    result = asyncio.run(
        worker.execute(AgentMessage.task("test", "inspect files"), on_event=forwarded.append)
    )

    assert result.content == "internal draft"
    assert [event["type"] for event in forwarded] == [
        "text_delta",
        "thinking_delta",
        "tool_call",
        "tool_result",
        "turn_complete",
    ]


def test_subagent_plan_depth_is_bounded(tmp_path, monkeypatch):
    monkeypatch.setenv("HOME", str(tmp_path / "home"))
    worker = SubAgent(
        name="worker-plan",
        role=AgentRole.WORKER,
        llm_client=FakeTeamClient(),
        tool_registry=ToolRegistry(),
        config=load_config(project_root=tmp_path),
        cwd=str(tmp_path),
        max_plan_depth=1,
    )

    result = asyncio.run(
        worker.execute(AgentMessage.task("test", "recursive task"), mode="plan", plan_depth=1)
    )

    assert result.type == AgentMessageType.ERROR
    assert "depth limit" in result.content


def test_reviewer_rejection_after_retry_marks_step_failed(tmp_path, monkeypatch):
    monkeypatch.setenv("HOME", str(tmp_path / "home"))
    orchestrator = _orchestrator(tmp_path, FakeTeamClient())
    orchestrator.max_retries_per_step = 1
    steps = [ExecutionStep("step_1", "implement", "ANALYSIS", [])]

    class Worker:
        async def execute(self, task, context, *, mode):  # noqa: ARG002
            return AgentMessage.result("worker", AgentRole.WORKER, "unverified result")

    class Reviewer:
        async def review(self, original_task, execution_result):  # noqa: ARG002
            return AgentMessage.result(
                "reviewer",
                AgentRole.REVIEWER,
                '{"approved":false,"issues":["missing verification"]}',
            )

        def clear_history(self):
            return None

    asyncio.run(orchestrator._run_step(steps[0], steps, {}, Worker(), Reviewer()))

    assert steps[0].status == StepStatus.FAILED
    assert "missing verification" in steps[0].result


class FakeTeamClient:
    model_name = "fake-model"
    provider_name = "fake-provider"
    max_context_window = 1000

    async def chat(self, messages, tools, *, system_prompt):  # noqa: ARG002
        yield {"type": "text_delta", "text": "{}"}
        yield {"type": "message_end", "stop_reason": "end_turn"}


class ParallelTeamClient(FakeTeamClient):
    def __init__(self):
        self.current_concurrency = 0
        self.peak_concurrency = 0
        self.ready = asyncio.Event()

    async def chat(self, messages, tools, *, system_prompt):  # noqa: ARG002
        body = _message_text(messages[-1].content)
        if "Original task" in body:
            yield {"type": "text_delta", "text": '{"approved": true, "issues": []}'}
            yield {"type": "message_end", "stop_reason": "end_turn"}
            return
        if "Create an execution plan" in body:
            yield {
                "type": "text_delta",
                "text": (
                    '{"summary":"parallel","steps":['
                    '{"id":"a","description":"Task A","type":"ANALYSIS","dependencies":[]},'
                    '{"id":"b","description":"Task B","type":"ANALYSIS","dependencies":[]}'
                    "]}"
                ),
            }
            yield {"type": "message_end", "stop_reason": "end_turn"}
            return
        if "Task A" in body or "Task B" in body:
            self.current_concurrency += 1
            self.peak_concurrency = max(self.peak_concurrency, self.current_concurrency)
            if self.current_concurrency == 2:
                self.ready.set()
            await asyncio.wait_for(self.ready.wait(), timeout=2)
            self.current_concurrency -= 1
            text = "Task A result" if "Task A" in body else "Task B result"
            yield {"type": "text_delta", "text": text}
            yield {"type": "message_end", "stop_reason": "end_turn"}
            return
        yield {"type": "text_delta", "text": "fallback"}
        yield {"type": "message_end", "stop_reason": "end_turn"}


def _orchestrator(tmp_path, client):
    registry = ToolRegistry()
    registry.register_all(get_builtin_tools())
    config = load_config(project_root=tmp_path)
    config.policy.hitl_mode = "never"
    return AgentOrchestrator(
        llm_client=client,
        tool_registry=registry,
        config=config,
        cwd=str(tmp_path),
    )


def _message_text(content: Any) -> str:
    if isinstance(content, str):
        return content
    return str(content)
