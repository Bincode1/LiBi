from __future__ import annotations

import asyncio
from datetime import datetime

from paicli.agent import QueryEngine
from paicli.config import load_config
from paicli.llm import create_llm_client
from paicli.memory import MemoryManager
from paicli.prompt import PromptAssembler
from paicli.session import SessionStore
from paicli.tools import ToolRegistry


class CapturingClient:
    model_name = "fake-model"
    provider_name = "fake-provider"
    max_context_window = 100_000

    def __init__(self):
        self.system_prompts: list[str] = []

    async def chat(self, messages, tools, *, system_prompt):  # noqa: ARG002
        self.system_prompts.append(system_prompt)
        yield {"type": "text_delta", "text": "ok"}
        yield {"type": "message_end", "stop_reason": "end_turn"}


def test_static_prompt_is_stable_and_custom_project_instructions_are_loaded(tmp_path):
    custom = tmp_path / "rules.md"
    custom.write_text("Always run the focused test first.", encoding="utf-8")
    config = load_config(project_root=tmp_path)
    config.prompt.custom_prompt_paths = [str(custom)]
    assembler = PromptAssembler(config, str(tmp_path), ["read_file"], "model", "provider")

    first = assembler.build_static()
    second = assembler.build_static()

    assert first == second
    assert "Always run the focused test first" in first
    assert "Current time" not in first


def test_react_turn_keeps_the_previous_request_as_a_prefix(
    tmp_path, config, provider_stream, monkeypatch
):
    config.llm.api_key = "synthetic-llm-credential"
    config.features.memory = False
    config.features.skill = False
    batches, requests = provider_stream
    store = SessionStore(tmp_path / "runtime.db")
    session_id = store.create_session()
    engine = QueryEngine(
        llm_client=create_llm_client(config.llm),
        tool_registry=ToolRegistry(),
        config=config,
        cwd=str(tmp_path),
        session_store=store,
        session_id=session_id,
    )
    turns = [
        ("first question", "2030-01-02T03:04:05+00:00"),
        ("follow-up", "2030-01-02T03:04:21+00:00"),
    ]
    for question, timestamp in turns:
        monkeypatch.setattr("paicli.session.store._now", lambda value=timestamp: value)
        batches.append([{"choices": [{"delta": {"content": "ok"}, "finish_reason": "stop"}]}])
        asyncio.run(engine.ask_complete_async(question))

    first, second = requests
    assert "Current time:" not in first["messages"][0]["content"]
    assert first["tools"] == second["tools"]
    assert first["messages"] == second["messages"][: len(first["messages"])]
    for request, (question, timestamp) in zip(requests, turns, strict=True):
        latest = request["messages"][-1]
        local_time = datetime.fromisoformat(timestamp).astimezone().isoformat(timespec="seconds")
        assert latest["role"] == "user"
        assert latest["content"].startswith(question)
        assert local_time in latest["content"]
    messages = [event for event in store.events(session_id) if event.event_type == "user/message"]
    assert [event.payload["content"] for event in messages] == [question for question, _ in turns]


def test_dynamic_prompt_recall_is_rebuilt_for_each_request(tmp_path):
    config = load_config(project_root=tmp_path)
    client = CapturingClient()
    engine = QueryEngine(
        llm_client=client,
        tool_registry=ToolRegistry(),
        config=config,
        cwd=str(tmp_path),
    )

    asyncio.run(engine.ask_complete_async("第一次请求，不涉及测试偏好"))
    MemoryManager(config.memory.long_term_db_path, scope=str(tmp_path)).save(
        "用户偏好使用 uv run python -m pytest 执行测试",
        kind="preference",
        importance=0.9,
    )
    asyncio.run(engine.ask_complete_async("我偏好怎么执行测试？"))

    assert len(client.system_prompts) == 2
    assert "recalled-memory" in client.system_prompts[1]
    assert "uv run python -m pytest" in client.system_prompts[1]


def test_dynamic_memory_is_bounded_and_marked_untrusted(tmp_path):
    config = load_config(project_root=tmp_path)
    manager = MemoryManager(config.memory.long_term_db_path, scope=str(tmp_path))
    manager.save("项目测试统一使用 pytest", kind="constraint")
    assembler = PromptAssembler(config, str(tmp_path), [], "model", "provider")

    dynamic = assembler.build_dynamic("项目测试怎么运行")

    assert '<recalled-memory trust="untrusted-data">' in dynamic
    assert "项目测试统一使用 pytest" in dynamic
