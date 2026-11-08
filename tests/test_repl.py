from __future__ import annotations

import asyncio

import pytest

from libi.entrypoints.repl import start_repl
from libi.memory import MemoryManager
from libi.session import SessionStore, default_runtime_db_path

pytestmark = pytest.mark.integration


@pytest.fixture
def repl_input(monkeypatch):
    lines = []

    class ScriptedPrompt:
        def __init__(self, **_kwargs):
            pass

        async def prompt_async(self):
            if not lines:
                raise EOFError
            item = lines.pop(0)
            if isinstance(item, BaseException):
                raise item
            return item

    monkeypatch.setattr("libi.entrypoints.repl.PromptSession", ScriptedPrompt)
    return lines


@pytest.mark.parametrize("exit_signal", ["/quit", EOFError(), KeyboardInterrupt()])
def test_repl_runs_prompt_and_cleans_up_on_exit(
    tmp_path, config, provider_stream, repl_input, exit_signal, capsys
):
    config.llm.api_key = "synthetic-repl-credential"
    batches, requests = provider_stream
    batches.append([{"choices": [{"delta": {"content": "REPL answer"}, "finish_reason": "stop"}]}])
    repl_input.extend(["", "hello", exit_signal])
    asyncio.run(start_repl(str(tmp_path), config))
    assert len(requests) == 1
    sessions = SessionStore(default_runtime_db_path()).list_sessions()
    assert len(sessions) == 1 and sessions[0].status == "completed"
    output = capsys.readouterr().out
    assert "REPL answer" in output
    assert config.llm.api_key not in output


def test_repl_local_commands_work_without_provider_calls(
    tmp_path, config, repl_input, provider_stream, capsys
):
    (tmp_path / "app.py").write_text(
        "def searchable_function():\n    return 42\n", encoding="utf-8"
    )
    repl_input.extend(
        [
            "/help",
            "/tools",
            "/config",
            "/policy",
            "/context",
            "/usage",
            "/audit invalid",
            "/save durable project fact",
            "/memory search durable",
            "/memory stats",
            "/index",
            "/search searchable_function",
            "/skill list",
            "/mcp",
            "/task list",
            "/agents",
            "/snapshot list",
            "/restore",
            "/restore missing",
            "/plan",
            "/save",
            "/unknown",
            "/hitl auto",
            "/hitl default",
            "/memory clear",
            "/clear",
            "/exit",
        ]
    )
    asyncio.run(start_repl(str(tmp_path), config))
    assert provider_stream[1] == []
    assert MemoryManager(config.memory.long_term_db_path, scope=str(tmp_path)).list() == []
    output = capsys.readouterr().out
    for expected in [
        "Saved memory",
        "Indexed",
        "searchable_function",
        "Unknown command",
        "snapshot not found: missing",
        "Cleared",
    ]:
        assert expected in output


def test_repl_resumes_history_and_continues_same_session(
    tmp_path, config, repl_input, provider_stream, capsys
):
    config.llm.api_key = "synthetic-repl-credential"
    store = SessionStore(default_runtime_db_path(), workspace=tmp_path)
    sid = store.create_session()
    store.append(sid, "user/message", {"content": "old question"})
    store.append(sid, "assistant/message", {"content": "old answer"})
    batches, requests = provider_stream
    batches.append([{"choices": [{"delta": {"content": "continued"}, "finish_reason": "stop"}]}])
    repl_input.extend(["continue", "/exit"])
    asyncio.run(start_repl(str(tmp_path), config, session_id=sid))
    assert [message["role"] for message in requests[0]["messages"]] == [
        "system",
        "user",
        "assistant",
        "user",
    ]
    assert len(store.list_sessions()) == 1
    assert "Resuming session" in capsys.readouterr().out


def test_repl_provider_failure_leaves_input_loop_usable(
    tmp_path, config, repl_input, monkeypatch, capsys
):
    import httpx

    config.llm.api_key = "synthetic-repl-credential"

    async def fail(_transport, _request):
        return httpx.Response(503)

    monkeypatch.setattr(httpx.AsyncHTTPTransport, "handle_async_request", fail)
    repl_input.extend(["hello", "/help", "/exit"])
    asyncio.run(start_repl(str(tmp_path), config))
    assert "HTTP 503" in capsys.readouterr().out
    assert SessionStore(default_runtime_db_path()).list_sessions()[0].status == "failed"
