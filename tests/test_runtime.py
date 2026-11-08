from __future__ import annotations

import subprocess
import sys

import pytest

from libi.config import load_config
from libi.runtime import TaskRecord
from libi.runtime.api import RuntimeApiServer, _engine_events
from libi.session import SessionStore


def test_runtime_loads_subagent_module_at_startup():
    subprocess.run(
        [
            sys.executable,
            "-B",
            "-c",
            "import sys; import libi.runtime; assert 'libi.agent.subagents' in sys.modules",
        ],
        check=True,
        timeout=15,
    )


def test_legacy_team_task_fails_explicitly(tmp_path):
    class TaskManager:
        def is_canceled(self, task_id):  # noqa: ARG002
            return False

    server = RuntimeApiServer.__new__(RuntimeApiServer)
    server.task_manager = TaskManager()
    task = TaskRecord(
        id="legacy-team-task",
        prompt="old task",
        status="running",
        created_at="",
        updated_at="",
        mode="team",
    )

    import asyncio

    with pytest.raises(ValueError, match="no longer supported"):
        asyncio.run(server._run_task(task))


def test_runtime_initialization_failure_closes_canonical_turn(tmp_path, monkeypatch):
    config = load_config(project_root=tmp_path)
    config.llm.api_key = "unit-placeholder"
    config.session_title.enabled = False
    server = RuntimeApiServer(
        cwd=str(tmp_path), config=config, api_key="unit-runtime-placeholder", workers=1
    )
    session_id = server._create_session()

    async def fail_registry(**_kwargs):
        raise RuntimeError("registry initialization failed")

    monkeypatch.setattr("libi.runtime.api.build_tool_registry", fail_registry)

    async def run():
        with pytest.raises(RuntimeError, match="registry initialization failed"):
            await server._run_turn(session_id, "hello")

    import asyncio

    asyncio.run(run())
    lifecycle = [
        event
        for event in server.session_store.events(session_id)
        if event.event_type in {"turn/started", "turn/failed", "turn/completed"}
    ]
    assert [event.event_type for event in lifecycle] == ["turn/started", "turn/failed"]
    state = server.session_store.state(session_id)
    assert state.status == "failed"
    assert state.active_turn_id is None


def test_runtime_session_events_are_not_merged_with_child_sessions(tmp_path, config):
    server = RuntimeApiServer(cwd=str(tmp_path), config=config, api_key="synthetic", workers=0)
    server.session_store = SessionStore(tmp_path / "runtime.db")
    server.session_store.create_session("root-session")
    server.session_store.create_session(
        "child-session", kind="child", parent_session_id="root-session"
    )
    parent_event = server.session_store.append(
        "root-session",
        "agent/child_started",
        {"child_id": "child-1", "child_session_id": "child-session"},
        source="subagent-manager",
    )
    server.session_store.append(
        "child-session",
        "tool/execution_started",
        {"name": "read_file"},
        source="tool-executor",
    )

    payload = server._events_json("root-session", 0)

    assert payload["session_id"] == "root-session"
    assert [event["id"] for event in payload["events"]] == [parent_event.event_id]
    assert [event["type"] for event in payload["events"]] == ["agent/child_started"]


@pytest.mark.parametrize("empty_page", [False, True])
def test_runtime_event_cursor_does_not_skip_concurrent_writes(
    tmp_path, config, monkeypatch, empty_page
):
    server = RuntimeApiServer(cwd=str(tmp_path), config=config, api_key="synthetic", workers=0)
    store = SessionStore(tmp_path / "runtime.db")
    server.session_store = store
    sid = store.create_session()
    first = store.append(sid, "user/message", {"content": "request"})
    original_events = store.events
    inserted = []

    def events_with_concurrent_write(*args, **kwargs):
        result = original_events(*args, **kwargs)
        if not inserted:
            inserted.append(store.append(sid, "assistant/message", {"content": "late answer"}))
        return result

    monkeypatch.setattr(store, "events", events_with_concurrent_write)
    page = server._events_json(sid, first.sequence if empty_page else 0)
    following = server._events_json(sid, page["next_cursor"])

    assert page["next_cursor"] == first.sequence
    assert [event["sequence"] for event in following["events"]] == [inserted[0].sequence]


def test_runtime_rejects_removed_mode_before_using_engine():
    with pytest.raises(ValueError, match="mode must be one of"):
        _engine_events(None, "team", "x")
