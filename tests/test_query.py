from __future__ import annotations

import asyncio
from typing import Any

from paicli.agent import QueryEngine
from paicli.config import load_config
from paicli.context import CompactionResponse
from paicli.session import ContextProjector, SessionStore
from paicli.skill import SkillRegistry
from paicli.tools import ToolRegistry, get_builtin_tools


class FakeClient:
    model_name = "fake-model"
    provider_name = "fake-provider"
    max_context_window = 1000

    def __init__(self):
        self.calls = 0

    async def chat(self, messages, tools, *, system_prompt):  # noqa: ARG002
        self.calls += 1
        if self.calls == 1:
            yield {
                "type": "tool_call_delta",
                "tool_call": {
                    "index": 0,
                    "id": "call_1",
                    "function": {"name": "read_file", "arguments": '{"path":"note.txt"}'},
                },
            }
            yield {"type": "message_end", "stop_reason": "tool_use"}
        else:
            tool_messages = [message for message in messages if message.role == "tool"]
            assert tool_messages
            assert "1: hello" in tool_messages[-1].content
            yield {"type": "text_delta", "text": "done"}
            yield {"type": "message_end", "stop_reason": "end_turn"}


class SkillLoadingClient:
    model_name = "fake-model"
    provider_name = "fake-provider"
    max_context_window = 1000

    def __init__(self):
        self.calls = 0

    async def chat(self, messages, tools, *, system_prompt):  # noqa: ARG002
        self.calls += 1
        if self.calls == 1:
            user_content = str(messages[-1].content)
            assert "Relevant skill candidates" in user_content
            assert "demo-skill" in user_content
            assert any(tool["function"]["name"] == "load_skill" for tool in tools)
            yield {
                "type": "tool_call_delta",
                "tool_call": {
                    "index": 0,
                    "id": "load_1",
                    "function": {"name": "load_skill", "arguments": '{"name":"demo-skill"}'},
                },
            }
            yield {"type": "message_end", "stop_reason": "tool_use"}
            return

        tool_messages = [message for message in messages if message.role == "tool"]
        assert tool_messages
        assert "## Loaded Skill: demo-skill" in str(tool_messages[-1].content)
        assert "Apply the demo workflow now." in str(tool_messages[-1].content)
        yield {"type": "text_delta", "text": "skill applied"}
        yield {"type": "message_end", "stop_reason": "end_turn"}


class UsageAndCompressionClient:
    model_name = "fake-model"
    provider_name = "fake-provider"
    max_context_window = 2_000

    def __init__(self):
        self.saw_summary = False

    async def complete(self, request):
        headings = (
            "Primary Request and Intent",
            "Constraints and Preferences",
            "Key Technical Concepts",
            "Files and Changes",
            "Decisions and Rationale",
            "Progress",
            "Pending Work and Next Steps",
            "Critical Context",
        )
        return CompactionResponse(
            text="\n".join(f"## {heading}\n(none)" for heading in headings)
            + "\n### Done\n(none)\n### In Progress\n(none)\n### Blocked\n(none)"
        )

    async def chat(self, messages, tools, *, system_prompt):  # noqa: ARG002
        self.saw_summary = any("compacted-summary" in str(message.content) for message in messages)
        yield {"type": "text_delta", "text": "compressed"}
        yield {"type": "message_end", "stop_reason": "end_turn"}
        yield {
            "type": "usage",
            "usage": {
                "input_tokens": 120,
                "output_tokens": 8,
                "prompt_cache_hit_tokens": 20,
                "prompt_cache_miss_tokens": 100,
            },
        }


def test_query_engine_executes_tool_and_replays_result(tmp_path):
    (tmp_path / "note.txt").write_text("hello\n", encoding="utf-8")
    config = load_config(project_root=tmp_path)
    registry = ToolRegistry()
    registry.register_all(get_builtin_tools())
    engine = QueryEngine(
        llm_client=FakeClient(),
        tool_registry=registry,
        config=config,
        cwd=str(tmp_path),
    )

    async def run() -> Any:
        return await engine.ask_complete_async("read note")

    result = asyncio.run(run())
    assert result.text == "done"
    assert result.turns == 2
    tool_events = [
        event
        for event in engine.session_store.events(engine.session_id)
        if event.event_type in {"tool/execution_started", "tool/result"}
    ]
    assert [event.tool_call_id for event in tool_events] == ["call_1", "call_1"]


def test_load_skill_is_injected_in_the_same_query_next_model_turn(tmp_path):
    SkillRegistry(tmp_path).create(
        "demo-skill",
        description="Use for the demo workflow",
        body="Apply the demo workflow now.",
        tags=["demo"],
    )
    config = load_config(project_root=tmp_path)
    registry = ToolRegistry()
    registry.register_all(get_builtin_tools())
    client = SkillLoadingClient()
    engine = QueryEngine(
        llm_client=client,
        tool_registry=registry,
        config=config,
        cwd=str(tmp_path),
    )

    result = asyncio.run(engine.ask_complete_async("请使用 demo-skill 完成这个任务"))

    assert result.text == "skill applied"
    assert result.turns == 2
    assert client.calls == 2


def test_query_integrates_context_compression_and_detailed_usage(tmp_path):
    config = load_config(project_root=tmp_path)
    config.llm.max_tokens = 100
    config.memory.compression_reserve_tokens = 20
    config.memory.compression_threshold = 0.6
    config.memory.compression_mode = "llm"
    client = UsageAndCompressionClient()
    engine = QueryEngine(
        llm_client=client,
        tool_registry=ToolRegistry(),
        config=config,
        cwd=str(tmp_path),
    )
    store = SessionStore(tmp_path / "runtime.db")
    session_id = store.create_session()
    for index in range(8):
        store.append(session_id, "user/message", {"content": f"old request {index} " + "x" * 500})
        store.append(
            session_id, "assistant/message", {"content": f"old answer {index} " + "y" * 500}
        )
    engine.session_store = store
    engine.session_id = session_id
    result = asyncio.run(engine.ask_complete_async("new request"))

    assert result.text == "compressed"
    assert client.saw_summary
    assert result.total_tokens == 128
    assert result.usage.cache_hit_tokens == 20
    assert result.usage.cache_miss_tokens == 100
    events = store.events(session_id)
    checkpoint = next(event for event in events if event.event_type == "compaction/checkpoint")
    prepared = next(event for event in events if event.event_type == "request/prepared")
    replayed = ContextProjector().project(
        events,
        session_id=session_id,
        turn_id=prepared.turn_id,
        step_id=prepared.step_id,
        through_sequence=prepared.payload["through_sequence"],
        **prepared.payload["projection"],
    )
    assert prepared.payload["compaction_id"] == checkpoint.payload["compaction_id"]
    assert replayed.context_hash == prepared.payload["context_hash"]
