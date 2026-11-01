from __future__ import annotations

import io
import threading

import pytest

from paicli.config import load_config
from paicli.runtime import DurableTaskManager, TaskRecord
from paicli.runtime.api import RuntimeApiServer, _engine_events
from paicli.session import SessionStore


def test_durable_task_lifecycle(tmp_path):
    manager = DurableTaskManager(tmp_path / "tasks.db")
    task_id = manager.add("do work")

    task = manager.claim_next()
    assert task is not None
    assert task.id == task_id
    assert task.status == "running"

    assert manager.complete(task_id, "done")
    completed = manager.get(task_id)
    assert completed is not None
    assert completed.status == "completed"
    assert completed.result == "done"


def test_durable_task_cancel(tmp_path):
    manager = DurableTaskManager(tmp_path / "tasks.db")
    task_id = manager.add("do work")

    assert manager.cancel(task_id)
    assert manager.get(task_id).status == "canceled"  # type: ignore[union-attr]


def test_durable_task_claim_is_atomic_across_workers(tmp_path):
    manager = DurableTaskManager(tmp_path / "tasks.db")
    task_id = manager.add("do work", mode="plan")
    barrier = threading.Barrier(2)
    claimed = []

    def claim(worker_id: str) -> None:
        barrier.wait()
        claimed.append(manager.claim_next(worker_id))

    workers = [threading.Thread(target=claim, args=(f"worker-{index}",)) for index in range(2)]
    for worker in workers:
        worker.start()
    for worker in workers:
        worker.join()

    records = [record for record in claimed if record is not None]
    assert len(records) == 1
    assert records[0].id == task_id
    assert records[0].mode == "plan"
    assert records[0].attempts == 1


def test_cancel_running_task_cannot_be_overwritten_by_worker_completion(tmp_path):
    manager = DurableTaskManager(tmp_path / "tasks.db")
    task_id = manager.add("do work")
    task = manager.claim_next("worker-1")
    assert task is not None

    assert manager.cancel(task_id)
    assert not manager.complete(task_id, "late result", worker_id="worker-1")

    canceled = manager.get(task_id)
    assert canceled is not None
    assert canceled.status == "canceled"
    assert canceled.result is None


def test_expired_worker_lease_is_reclaimed(tmp_path):
    manager = DurableTaskManager(tmp_path / "tasks.db")
    task_id = manager.add("do work", mode="plan")
    first = manager.claim_next("worker-1", lease_seconds=-1)
    assert first is not None

    second = manager.claim_next("worker-2")

    assert second is not None
    assert second.id == task_id
    assert second.worker_id == "worker-2"
    assert second.attempts == 2


def test_task_mode_validation(tmp_path):
    manager = DurableTaskManager(tmp_path / "tasks.db")

    try:
        manager.add("bad", mode="team")
    except ValueError as exc:
        assert "task mode" in str(exc)
    else:  # pragma: no cover - explicit failure message
        raise AssertionError("invalid task mode was accepted")


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


def test_runtime_session_history_reads_canonical_events(tmp_path, monkeypatch):
    monkeypatch.setenv("HOME", str(tmp_path / "home"))
    server = RuntimeApiServer(
        cwd=str(tmp_path),
        config=load_config(project_root=tmp_path),
        api_key="test-key",
        workers=1,
    )
    session_id = server._create_session()
    server.session_store.append(session_id, "user/message", {"content": "first question"})
    server.session_store.append(session_id, "assistant/message", {"content": "first answer"})

    history = server.session_store.events(session_id)

    assert [event.event_type for event in history[-2:]] == ["user/message", "assistant/message"]


def test_runtime_initialization_failure_closes_canonical_turn(tmp_path, monkeypatch):
    monkeypatch.setenv("HOME", str(tmp_path / "home"))
    config = load_config(project_root=tmp_path)
    config.llm.api_key = "unit-placeholder"
    server = RuntimeApiServer(
        cwd=str(tmp_path), config=config, api_key="unit-runtime-placeholder", workers=1
    )
    session_id = server._create_session()

    async def fail_registry(**_kwargs):
        raise RuntimeError("registry initialization failed")

    monkeypatch.setattr("paicli.runtime.api.build_tool_registry", fail_registry)

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


def test_runtime_event_sse_resumes_after_cursor(tmp_path):
    class Request:
        def __init__(self):
            self.wfile = io.BytesIO()
            self.headers = {}
            self.status = None

        def send_response(self, status):
            self.status = status

        def send_header(self, _key, _value):
            pass

        def end_headers(self):
            pass

    server = RuntimeApiServer.__new__(RuntimeApiServer)
    server.session_store = SessionStore(tmp_path / "runtime.db")
    first = server.session_store.append("session-a", "turn/started", {}, source="runtime")
    second = server.session_store.append("session-a", "turn/completed", {}, source="runtime")
    request = Request()

    server._send_events(request, "session-a", after_sequence=first.sequence)

    body = request.wfile.getvalue().decode("utf-8")
    assert request.status == 200
    assert f"id: {second.sequence}\nevent: turn/completed" in body
    assert "turn/started" not in body


def test_runtime_events_accepts_last_event_id_header(tmp_path):
    class Request:
        command = "GET"
        path = "/v1/sessions/session-a/events"
        headers = {"last-event-id": "1", "content-length": "0"}

        def __init__(self):
            self.rfile = io.BytesIO()
            self.wfile = io.BytesIO()
            self.status = None

        def send_response(self, status):
            self.status = status

        def send_header(self, _key, _value):
            pass

        def end_headers(self):
            pass

    server = RuntimeApiServer.__new__(RuntimeApiServer)
    server.session_store = SessionStore(tmp_path / "runtime.db")
    server.session_store.append("session-a", "turn/started", {}, source="runtime")
    second = server.session_store.append("session-a", "turn/completed", {}, source="runtime")
    server._authorized = lambda _request: True
    request = Request()

    server._handle(request)

    body = request.wfile.getvalue().decode("utf-8")
    assert request.status == 200
    assert f"id: {second.sequence}\nevent: turn/completed" in body
    assert "turn/started" not in body


def test_runtime_session_events_are_not_merged_with_child_sessions(tmp_path):
    server = RuntimeApiServer.__new__(RuntimeApiServer)
    server.session_store = SessionStore(tmp_path / "runtime.db")
    server.session_store.create_session("root-session")
    server.session_store.create_session(
        "child-session", kind="child", parent_session_id="root-session"
    )
    server.session_store.append(
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

    assert not any(
        event.get("payload", {}).get("child_session_id") == "child-session"
        and event["type"] == "tool/execution_started"
        for event in payload["events"]
    )


def test_runtime_routes_react_and_plan_modes():
    class Engine:
        def ask(self, message, history=None):
            return ("react", message, history)

        def plan(self, message):
            return ("plan", message)

    engine = Engine()

    assert _engine_events(engine, "react", "x")[0] == "react"
    assert _engine_events(engine, "plan", "x")[0] == "plan"
    with pytest.raises(ValueError, match="mode must be one of"):
        _engine_events(engine, "team", "x")


def test_task_workers_do_not_consume_another_project_scope(tmp_path):
    db = tmp_path / "tasks.db"
    project_a = DurableTaskManager(db, scope=tmp_path / "a")
    project_b = DurableTaskManager(db, scope=tmp_path / "b")
    task_a = project_a.add("A")
    task_b = project_b.add("B")

    claimed_a = project_a.claim_next("worker-a")
    claimed_b = project_b.claim_next("worker-b")

    assert claimed_a is not None and claimed_a.id == task_a
    assert claimed_b is not None and claimed_b.id == task_b
    assert claimed_a.scope != claimed_b.scope
