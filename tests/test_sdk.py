from __future__ import annotations

import asyncio

import pytest

from libi.sdk import create_default_engine

pytestmark = pytest.mark.integration


def test_default_sdk_engine_runs_and_resumes_conversation(tmp_path, monkeypatch, provider_stream):
    monkeypatch.setenv("LIBI_MCP", "false")
    monkeypatch.setenv("LIBI_API_KEY", "synthetic-sdk-credential")
    batches, requests = provider_stream
    batches.extend(
        [
            [{"choices": [{"delta": {"content": "first"}, "finish_reason": "stop"}]}],
            [{"choices": [{"delta": {"content": "second"}, "finish_reason": "stop"}]}],
        ]
    )
    engine = create_default_engine(str(tmp_path))
    assert engine.ask_complete("first request").text == "first"
    assert engine.ask_complete("second request").text == "second"
    assert [message["role"] for message in requests[1]["messages"]] == [
        "system",
        "user",
        "assistant",
        "user",
    ]
    assert engine.session_store.state(engine.session_id).status == "completed"


def test_sdk_reassembles_streamed_tool_call_executes_and_replays_result(
    tmp_path, monkeypatch, provider_stream
):
    monkeypatch.setenv("LIBI_MCP", "false")
    monkeypatch.setenv("LIBI_API_KEY", "synthetic-sdk-credential")
    (tmp_path / "note.txt").write_text("tool chain content", encoding="utf-8")
    batches, requests = provider_stream
    batches.extend(
        [
            [
                {
                    "choices": [
                        {
                            "delta": {
                                "tool_calls": [
                                    {
                                        "index": 0,
                                        "id": "read-call",
                                        "type": "function",
                                        "function": {"name": "read_file", "arguments": '{"path":'},
                                    }
                                ]
                            }
                        }
                    ]
                },
                {
                    "choices": [
                        {
                            "delta": {
                                "tool_calls": [
                                    {
                                        "index": 0,
                                        "function": {"arguments": '"note.txt"}'},
                                    }
                                ]
                            },
                            "finish_reason": "tool_calls",
                        }
                    ]
                },
            ],
            [{"choices": [{"delta": {"content": "read complete"}, "finish_reason": "stop"}]}],
        ]
    )
    engine = create_default_engine(str(tmp_path))
    result = engine.ask_complete("read the note")
    assert result.text == "read complete" and result.turns == 2
    replayed = [message for message in requests[1]["messages"] if message["role"] == "tool"]
    assert replayed == [
        {"role": "tool", "tool_call_id": "read-call", "content": "1: tool chain content"}
    ]
    events = engine.session_store.events(engine.session_id)
    assert [event.tool_call_id for event in events if event.event_type == "tool/result"] == [
        "read-call"
    ]


def test_sdk_failure_propagates_and_closes_engine(tmp_path, monkeypatch):
    monkeypatch.setenv("LIBI_MCP", "false")
    engine = create_default_engine(str(tmp_path))
    closed = []
    original_close = engine.aclose

    async def close():
        await original_close()
        closed.append(True)

    monkeypatch.setattr(engine, "aclose", close)
    with pytest.raises(RuntimeError, match="not configured"):
        engine.ask_complete("hello")
    assert closed == [True]
    assert engine.session_store.state(engine.session_id).status == "failed"


def test_async_sdk_context_closes_after_caller_failure(
    tmp_path, config, session_store, monkeypatch
):
    from libi.agent import QueryEngine
    from libi.tools import ToolRegistry

    class Client:
        model_name = provider_name = "fake"
        max_context_window = 128_000

        async def chat(self, *_args, **_kwargs):
            yield {"type": "text_delta", "text": "answer"}
            yield {"type": "message_end", "stop_reason": "end_turn"}

    engine = QueryEngine(
        llm_client=Client(),
        tool_registry=ToolRegistry(),
        config=config,
        cwd=str(tmp_path),
        session_store=session_store,
    )
    closed = []
    original_close = engine.aclose

    async def close():
        await original_close()
        closed.append(True)

    monkeypatch.setattr(engine, "aclose", close)

    async def run():
        with pytest.raises(ValueError, match="caller failed"):
            async with engine:
                assert (await engine.ask_complete_async("hello")).text == "answer"
                raise ValueError("caller failed")
        assert closed == [True]
        assert session_store.state(engine.session_id).status == "completed"

    asyncio.run(run())
