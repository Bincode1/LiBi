from __future__ import annotations

import asyncio
import json
import sqlite3

import pytest

from paicli.agent import Agent, QueryEngine
from paicli.config import load_config
from paicli.session import ContextProjector, RequestBuilder, SessionStore
from paicli.tools import ToolRegistry


def test_session_store_appends_atomically_and_supports_cursor(tmp_path):
    store = SessionStore(tmp_path / "runtime.db")
    first = store.append("session-a", "turn/started", {}, turn_id="turn-a")
    second = store.append("session-a", "user/message", {"content": "hello"}, turn_id="turn-a")

    assert [event.sequence for event in store.events("session-a")] == [1, 2]
    assert store.events("session-a", after_sequence=first.sequence) == [second]
    assert store.state("session-a").active_turn_id == "turn-a"


def test_session_store_can_check_resume_target(tmp_path):
    store = SessionStore(tmp_path / "runtime.db")
    session_id = store.create_session("resume-me")

    assert store.session_exists(session_id)
    assert not store.session_exists("missing-session")
    assert not store.session_exists(" ")


def test_session_store_lists_sessions_by_recent_activity(tmp_path):
    store = SessionStore(tmp_path / "runtime.db")
    older = store.create_session("older")
    newer = store.create_session("newer")
    store.append(older, "user/message", {"content": "old question"})
    store.append(newer, "user/message", {"content": "new question"})

    sessions = store.list_sessions()

    assert [item.session_id for item in sessions] == [newer, older]
    assert sessions[0].status == "created"
    assert sessions[0].last_sequence == 1
    assert sessions[0].latest_user_message is None


def test_session_store_lists_root_sessions_without_loading_payloads(tmp_path, monkeypatch):
    store = SessionStore(tmp_path / "runtime.db")
    parent = store.create_session("parent")
    child = store.create_session("child", kind="child", parent_session_id=parent)
    store.append(
        parent,
        "agent/child_started",
        {"child_id": "agent-1", "child_session_id": child},
    )
    store.append(child, "user/message", {"content": "internal child task"})

    def fail_if_history_is_loaded(_session_id, *, after_sequence=0):
        raise AssertionError("session list must not load event history")

    monkeypatch.setattr(store, "events", fail_if_history_is_loaded)
    assert [item.session_id for item in store.list_sessions()] == [parent]
    assert [item.session_id for item in store.list_sessions(include_children=True)] == [
        child,
        parent,
    ]


def test_session_store_extends_existing_runtime_event_table(tmp_path):
    db = tmp_path / "runtime.db"
    with sqlite3.connect(db) as conn:
        conn.execute(
            "create table events (id integer primary key, session_id text, type text, "
            "payload text, created_at text)"
        )
        conn.execute("insert into events values (1, 'thread-a', 'thread.created', '{}', 'now')")

    store = SessionStore(db)
    event = store.append("thread-a", "session/created", {}, source="runtime")

    with sqlite3.connect(db) as conn:
        rows = conn.execute(
            "select type, sequence, schema_version from events order by id"
        ).fetchall()
    assert rows == [("thread.created", 1, 1), ("session/created", 2, 1)]
    migrated = store.events("thread-a")
    assert [item.event_type for item in migrated] == ["thread.created", "session/created"]
    assert migrated[-1] == event


def test_session_store_migration_preserves_order_with_mixed_events(tmp_path):
    db = tmp_path / "runtime.db"
    with sqlite3.connect(db) as conn:
        conn.execute(
            "create table events (id integer primary key, session_id text, type text, "
            "payload text, created_at text, sequence integer)"
        )
        conn.execute("insert into events values (1, 'thread-a', 'old.event', '{}', 'now', null)")
        conn.execute("insert into events values (2, 'thread-a', 'new.event', '{}', 'later', 1)")

    events = SessionStore(db).events("thread-a")

    assert [(event.event_type, event.sequence) for event in events] == [
        ("old.event", 1),
        ("new.event", 2),
    ]


def test_session_store_redacts_nested_secrets_and_rejects_non_json(tmp_path):
    store = SessionStore(tmp_path / "runtime.db", sensitive_values=("runtime-secret-value",))
    event = store.append(
        "session-a",
        "tool/result",
        {
            "content": "Bearer runtime-secret-value and api_key=inline-secret",
            "credentials": {"access_token": "another-secret"},
        },
        tool_call_id="call-1",
    )

    assert "runtime-secret-value" not in json.dumps(event.payload)
    assert "inline-secret" not in json.dumps(event.payload)
    assert event.payload["credentials"] == "[redacted]"
    with pytest.raises(TypeError):
        store.append("session-a", "request/prepared", {"bad": object()})


def test_context_projector_is_deterministic_and_hides_internal_events(tmp_path):
    store = SessionStore(tmp_path / "runtime.db")
    sid = store.create_session("session-a")
    store.append(sid, "turn/started", {}, turn_id="turn-a")
    store.append(
        sid,
        "system/section",
        {
            "section_id": "base",
            "kind": "base",
            "scope": "session",
            "priority": 10,
            "content": "Stable rules",
            "source": "test",
            "version": "v1",
        },
        turn_id="turn-a",
    )
    store.append(sid, "request/header", {"secret": "must-not-project"}, turn_id="turn-a")
    store.append(sid, "approval/started", {"private": "hidden"}, turn_id="turn-a")
    store.append(sid, "user/message", {"content": "hello"}, turn_id="turn-a")
    store.append(sid, "step/started", {}, turn_id="turn-a", step_id="step-a")
    store.append(
        sid,
        "tool/schema_snapshot",
        {"tools": [{"type": "function", "function": {"name": "z_tool"}}]},
        turn_id="turn-a",
        step_id="step-a",
    )

    projector = ContextProjector()
    kwargs = {"session_id": sid, "turn_id": "turn-a", "step_id": "step-a"}
    view = projector.project(store.events(sid), **kwargs)
    repeated = projector.project(store.events(sid), **kwargs)
    request = RequestBuilder().build(
        view, model="test-model", provider="test-provider", max_tokens=100, temperature=0.2
    )

    assert view.context_hash == repeated.context_hash
    assert view.system_prompt == "Stable rules"
    assert [message.content for message in view.messages] == ["hello"]
    assert len(request.messages) == 1
    assert request.tools[0]["function"]["name"] == "z_tool"


def test_context_projector_requires_tool_calls_to_match_results(tmp_path):
    store = SessionStore(tmp_path / "runtime.db")
    sid = store.create_session("session-a")
    store.append(sid, "turn/started", {}, turn_id="turn-a")
    store.append(sid, "user/message", {"content": "read"}, turn_id="turn-a")
    store.append(sid, "step/started", {}, turn_id="turn-a", step_id="step-a")
    store.append(
        sid,
        "assistant/message",
        {
            "content": "",
            "tool_calls": [{"id": "call-1", "function": {"name": "read_file", "arguments": "{}"}}],
        },
        turn_id="turn-a",
        step_id="step-a",
    )

    with pytest.raises(ValueError, match="missing tool results"):
        ContextProjector().project(
            store.events(sid), session_id=sid, turn_id="turn-a", step_id="step-a"
        )


def test_context_projector_does_not_duplicate_skill_context_in_checkpoint(tmp_path):
    store = SessionStore(tmp_path / "runtime.db")
    sid = store.create_session("session-a")
    store.append(sid, "turn/started", {}, turn_id="turn-a")
    store.append(sid, "user/message", {"content": "use the skill"}, turn_id="turn-a")
    store.append(
        sid,
        "skill/context",
        {"content": "Relevant skill facts", "visibility": "model"},
        turn_id="turn-a",
    )
    store.append(sid, "step/started", {}, turn_id="turn-a", step_id="step-a")
    store.append(
        sid,
        "compaction/checkpoint",
        {
            "messages": [
                {"role": "user", "content": "use the skill"},
                {
                    "role": "user",
                    "content": "[skill-context: skill; untrusted data]\nRelevant skill facts",
                },
            ]
        },
        turn_id="turn-a",
        step_id="step-a",
    )

    view = ContextProjector().project(
        store.events(sid), session_id=sid, turn_id="turn-a", step_id="step-a"
    )

    assert len(view.messages) == 2
    assert sum("Relevant skill facts" in str(item.content) for item in view.messages) == 1


class TwoStepClient:
    model_name = "fake-model"
    provider_name = "fake-provider"
    max_context_window = 1_000

    def __init__(self):
        self.calls = 0

    async def chat(self, messages, tools, *, system_prompt):  # noqa: ARG002
        self.calls += 1
        if self.calls == 1:
            yield {
                "type": "tool_call_delta",
                "tool_call": {
                    "index": 0,
                    "id": "call-1",
                    "function": {"name": "missing_tool", "arguments": "{}"},
                },
            }
            yield {"type": "message_end", "stop_reason": "tool_use"}
            return
        assert any(
            message.role == "tool" and message.tool_call_id == "call-1" for message in messages
        )
        yield {"type": "text_delta", "text": "done"}
        yield {"type": "message_end", "stop_reason": "end_turn"}


def test_react_session_event_log_is_source_for_next_model_step(tmp_path, monkeypatch):
    monkeypatch.setenv("HOME", str(tmp_path / "home"))
    config = load_config(project_root=tmp_path)
    config.llm.api_key = "test-only"
    client = TwoStepClient()
    store = SessionStore(tmp_path / "runtime.db", sensitive_values=(config.llm.api_key,))
    engine = QueryEngine(
        llm_client=client,
        tool_registry=ToolRegistry(),
        config=config,
        cwd=str(tmp_path),
        session_store=store,
    )

    result = asyncio.run(engine.ask_complete_async("run"))
    events = store.events(engine.session_id)
    event_types = [event.event_type for event in events]

    assert result.text == "done"
    assert event_types.count("request/prepared") == 2
    assert event_types.count("context/summary") == 2
    assert event_types.count("assistant/message") == 2
    assert event_types.count("tool/result") == 1
    prepared = [event for event in events if event.event_type == "request/prepared"]
    assert all("context_summary" in event.payload for event in prepared)
    assert event_types[-1] == "turn/completed"
    assert store.state(engine.session_id).status == "completed"
    assert all(
        event.turn_id == engine.session_id or event.session_id == engine.session_id
        for event in events
    )


def test_agent_continues_past_twenty_tool_calls_until_model_finishes(tmp_path, monkeypatch):
    class ToolCallingClient:
        model_name = "fake-model"
        provider_name = "fake-provider"
        max_context_window = 1_000

        def __init__(self):
            self.calls = 0

        async def chat(self, messages, tools, *, system_prompt):  # noqa: ARG002
            self.calls += 1
            if self.calls <= 21:
                yield {
                    "type": "tool_call_delta",
                    "tool_call": {
                        "index": 0,
                        "id": f"call-{self.calls}",
                        "function": {"name": "missing_tool", "arguments": "{}"},
                    },
                }
                yield {"type": "message_end", "stop_reason": "tool_use"}
            else:
                yield {"type": "text_delta", "text": "finished"}
                yield {"type": "message_end", "stop_reason": "end_turn"}

    monkeypatch.setenv("HOME", str(tmp_path / "home"))
    config = load_config(project_root=tmp_path)
    config.llm.api_key = "test-only"
    store = SessionStore(tmp_path / "runtime.db")
    agent = Agent(
        llm_client=ToolCallingClient(),
        tool_registry=ToolRegistry(),
        config=config,
        cwd=str(tmp_path),
        session_store=store,
    )

    async def run():
        return [event async for event in agent.run("do work")]

    events = asyncio.run(run())
    session_events = store.events(agent.session_id)

    assert events[-1]["type"] == "done"
    assert events[-1]["total_turns"] == 22
    assert events[-1]["messages"][-1].content == "finished"
    projected = agent._context_projector.project_session(
        session_events, session_id=agent.session_id
    )
    assert projected.messages[-1].content == "finished"
    assert not hasattr(agent, "history")
    assert store.state(agent.session_id).status == "completed"
    assert session_events[-1].event_type == "turn/completed"


def test_agent_reuses_session_until_clear_then_starts_new_session(tmp_path, monkeypatch):
    monkeypatch.setenv("HOME", str(tmp_path / "home"))
    config = load_config(project_root=tmp_path)
    config.llm.api_key = "test-only"

    class ReplyClient:
        model_name = "fake-model"
        provider_name = "fake-provider"
        max_context_window = 4_000

        async def chat(self, messages, tools, *, system_prompt):  # noqa: ARG002
            yield {"type": "text_delta", "text": "ok"}
            yield {"type": "message_end", "stop_reason": "end_turn"}

    store = SessionStore(tmp_path / "runtime.db")
    agent = Agent(
        llm_client=ReplyClient(),
        tool_registry=ToolRegistry(),
        config=config,
        cwd=str(tmp_path),
        session_store=store,
    )

    async def run_once(message: str):
        return [event async for event in agent.run(message)]

    asyncio.run(run_once("first"))
    first_session = agent.session_id
    asyncio.run(run_once("second"))
    assert agent.session_id == first_session
    assert sum(event.event_type == "user/message" for event in store.events(first_session)) == 2

    agent.clear_history()
    second_session = agent.session_id
    assert second_session != first_session
    assert store.events(first_session)
    assert store.events(second_session) == []

    asyncio.run(run_once("after clear"))
    assert agent.session_id == second_session
    assert [event.event_type for event in store.events(second_session)].count("user/message") == 1


def test_query_engine_resumes_from_session_and_replays_requests(tmp_path, monkeypatch):
    monkeypatch.setattr("paicli.agent.agent.SnapshotService.create", lambda *args: None)
    config = load_config(project_root=tmp_path, env={})
    config.memory.compression_mode = "disabled"
    config.features.skill = False

    class ReplyClient:
        model_name = "fake-model"
        provider_name = "fake-provider"
        max_context_window = 128_000

        def __init__(self):
            self.seen_messages = []

        async def chat(self, messages, tools, *, system_prompt):
            self.seen_messages.append(list(messages))
            yield {"type": "text_delta", "text": "ok"}
            yield {"type": "message_end", "stop_reason": "end_turn"}

    client = ReplyClient()
    store = SessionStore(tmp_path / "runtime.db")
    session_id = store.create_session()
    store.append(session_id, "user/message", {"content": "past request"})
    store.append(session_id, "assistant/message", {"content": "past answer"})
    engine = QueryEngine(
        llm_client=client,
        tool_registry=ToolRegistry(),
        config=config,
        cwd=str(tmp_path),
        session_store=store,
        session_id=session_id,
    )

    asyncio.run(engine.ask_complete_async("new"))
    asyncio.run(engine.ask_complete_async("follow-up"))

    assert [m.content for m in client.seen_messages[0]] == ["past request", "past answer", "new"]
    assert [m.content for m in client.seen_messages[1]][-3:] == ["new", "ok", "follow-up"]
    events = store.events(session_id)
    for prepared in (event for event in events if event.event_type == "request/prepared"):
        replay = ContextProjector().project(
            events,
            session_id=session_id,
            turn_id=prepared.turn_id,
            step_id=prepared.step_id,
            through_sequence=prepared.payload["through_sequence"],
            **prepared.payload["projection"],
        )
        assert replay.context_hash == prepared.payload["context_hash"]
