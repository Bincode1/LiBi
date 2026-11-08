from __future__ import annotations

import asyncio
import json
import os
import shlex
import socket
import sys
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from queue import Queue

import httpx
import pytest

from paicli.runtime.api import RuntimeApiServer
from paicli.session import SessionStore, TurnInProgressError

pytestmark = pytest.mark.integration


@pytest.fixture
def runtime_http(tmp_path, config):
    config.llm.api_key = "synthetic-runtime-llm-credential"
    runtime = RuntimeApiServer(
        cwd=str(tmp_path), config=config, api_key="synthetic-runtime-credential", workers=0
    )

    class Handler(BaseHTTPRequestHandler):
        def do_GET(self):  # noqa: N802
            runtime._handle(self)

        def do_POST(self):  # noqa: N802
            runtime._handle(self)

        def log_message(self, *_args):
            pass

    server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    thread = threading.Thread(
        target=server.serve_forever, kwargs={"poll_interval": 0.01}, daemon=True
    )
    thread.start()
    try:
        with httpx.Client(
            base_url=f"http://127.0.0.1:{server.server_port}",
            headers={"x-api-key": runtime.api_key},
            timeout=5,
            trust_env=False,
        ) as client:
            yield runtime, client
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=5)
        assert not thread.is_alive()


@pytest.mark.parametrize("header", ["missing", "wrong", "bearer", "x-api-key"])
def test_http_authentication_precedes_request_parsing(runtime_http, header):
    runtime, client = runtime_http
    headers = {"x-api-key": ""}
    if header == "wrong":
        headers["x-api-key"] = "wrong"
    elif header == "bearer":
        headers["authorization"] = f"Bearer {runtime.api_key}"
    elif header == "x-api-key":
        headers["x-api-key"] = runtime.api_key
    response = client.post("/v1/sessions", content=b"invalid", headers=headers)
    assert response.status_code == (400 if header in {"bearer", "x-api-key"} else 401)
    assert runtime.session_store.list_sessions() == []


def test_truncated_model_reply_preserves_text_without_executing_tools(
    runtime_http, provider_stream, tmp_path
):
    runtime, client = runtime_http
    runtime.config.policy.hitl_mode = "never"
    batches, requests = provider_stream
    batches.append(
        [
            {
                "choices": [
                    {
                        "delta": {
                            "content": "partial answer",
                            "tool_calls": [
                                {
                                    "index": 0,
                                    "id": "call_truncated",
                                    "function": {
                                        "name": "write_file",
                                        "arguments": '{"path":"truncated.txt","content":"partial"}',
                                    },
                                }
                            ],
                        },
                        "finish_reason": "length",
                    }
                ]
            }
        ]
    )
    session_id = client.post("/v1/sessions").json()["id"]
    client.post(f"/v1/sessions/{session_id}/turns", json={"message": "write a file"})
    assert not (tmp_path / "truncated.txt").exists()
    assert len(requests) == 1
    events = runtime.session_store.events(session_id)
    message = next(event for event in events if event.event_type == "assistant/message")
    assert message.payload == {"content": "partial answer", "tool_calls": []}
    assert events[-1].event_type == "turn/failed"
    assert events[-1].payload["reason"] == "max_output_tokens"


def test_truncated_plan_is_rejected_even_when_its_json_parses(runtime_http, provider_stream):
    runtime, client = runtime_http
    batches, requests = provider_stream
    plan = json.dumps({"tasks": [{"id": "write", "description": "write to disk"}]})
    batches.append([{"choices": [{"delta": {"content": plan}, "finish_reason": "length"}]}])
    session_id = client.post("/v1/sessions").json()["id"]
    response = client.post(
        f"/v1/sessions/{session_id}/turns",
        json={"message": "Plan and implement a cross-module change with tests.", "mode": "plan"},
    )
    assert response.status_code == 500
    assert len(requests) == 1
    events = runtime.session_store.events(session_id)
    assert not any(event.event_type == "agent/child_started" for event in events)
    assert events[-1].payload["provider_error"]["code"] == "max_output_tokens"


def test_runtime_hides_other_workspaces_and_rejects_their_session_routes(runtime_http, tmp_path):
    runtime, client = runtime_http
    other = SessionStore(runtime.db_path, workspace=tmp_path / "other")
    session_id = other.create_session()
    other.append(session_id, "user/message", {"content": "other workspace"})
    assert client.get("/v1/sessions").json()["sessions"] == []
    base = f"/v1/sessions/{session_id}"
    assert client.get(f"{base}/events.json").status_code == 404
    assert client.get(f"{base}/events/stream").status_code == 404
    assert client.post(f"{base}/turns/async", json={"message": "continue"}).status_code == 404


def test_session_uses_its_selected_directory_for_tools_and_resume(
    runtime_http, provider_stream, tmp_path, monkeypatch
):
    runtime, client = runtime_http
    workspace = tmp_path / "另一个项目"
    workspace.mkdir()
    runtime.config.policy.hitl_mode = "never"
    runtime.config.features.mcp = True
    monkeypatch.setenv("PAICLI_TRUST_PROJECT_MCP", "1")
    for root, name in [(tmp_path, "startup"), (workspace, "selected")]:
        mcp_config = root / ".paicli" / "mcp.json"
        mcp_config.parent.mkdir(exist_ok=True)
        mcp_config.write_text(json.dumps({"mcpServers": {name: {"command": "echo"}}}))

    discovered = []

    async def load_tools(manager):
        discovered.extend(manager.specs)
        return []

    monkeypatch.setattr("paicli.bootstrap.McpClientManager.load_tools", load_tools)
    batches, _ = provider_stream
    write = {
        "index": 0,
        "id": "call_workspace",
        "function": {"name": "write_file", "arguments": '{"path":"note.txt","content":"ok"}'},
    }
    batches.extend(
        [
            [{"choices": [{"delta": {"tool_calls": [write]}, "finish_reason": "tool_calls"}]}],
            [{"choices": [{"delta": {"content": "written"}, "finish_reason": "stop"}]}],
        ]
    )

    created = client.post("/v1/sessions", json={"cwd": str(workspace)})
    assert created.status_code == 200
    assert created.json()["cwd"] == str(workspace)
    sid = created.json()["id"]
    assert client.post(f"/v1/sessions/{sid}/turns", json={"message": "write"}).status_code == 200
    assert discovered == ["startup"]
    assert (workspace / "note.txt").read_text() == "ok"
    assert not (tmp_path / "note.txt").exists()
    assert client.get(f"/v1/sessions/{sid}/events.json").json()["cwd"] == str(workspace)
    assert SessionStore(runtime.db_path, workspace=workspace).session_exists(sid, root_only=True)
    assert not SessionStore(runtime.db_path, workspace=tmp_path).session_exists(sid)

    child = runtime.session_store.create_session(kind="child", parent_session_id=sid)
    reopened = RuntimeApiServer(cwd=runtime.cwd, config=runtime.config, api_key=runtime.api_key)
    assert reopened._list_sessions()[0]["cwd"] == str(workspace)
    assert reopened.session_store.is_child_session(sid, child)
    assert reopened.session_store.session_cwd(child) == str(workspace)


def test_workspace_browsing_and_unavailable_session_directory(runtime_http, tmp_path):
    _, client = runtime_http
    workspace = tmp_path / "Project & 目录"
    workspace.mkdir()
    file_path = tmp_path / "file.txt"
    file_path.touch()
    response = client.get("/v1/workspaces", params={"path": str(tmp_path)})
    assert response.status_code == 200
    assert response.json()["directories"] == [{"name": workspace.name, "path": str(workspace)}]
    assert client.get("/v1/workspaces", params={"path": str(workspace)}).json()["parent"] == str(
        tmp_path
    )
    for invalid in ["relative", str(file_path), str(workspace / "missing"), 123]:
        response = client.post("/v1/sessions", json={"cwd": invalid})
        assert response.status_code == 400
        assert "workspace_error" in response.json()
    assert client.get("/v1/sessions").json()["sessions"] == []

    sid = client.post("/v1/sessions", json={"cwd": str(workspace)}).json()["id"]
    workspace.rmdir()
    response = client.post(f"/v1/sessions/{sid}/turns/async", json={"message": "continue"})
    assert response.status_code == 400
    assert "workspace_error" in response.json()
    events = client.get(f"/v1/sessions/{sid}/events.json").json()["events"]
    assert [event["type"] for event in events] == ["session/created"]


def test_async_receipt_is_durable_before_worker_starts(runtime_http, monkeypatch):
    runtime, client = runtime_http
    original_start = threading.Thread.start

    def defer_turn_thread(thread):
        if not thread.name.startswith("paicli-turn-"):
            original_start(thread)

    monkeypatch.setattr(threading.Thread, "start", defer_turn_thread)
    session_id = client.post("/v1/sessions").json()["id"]
    path = f"/v1/sessions/{session_id}/turns/async"
    response = client.post(path, json={"message": "persist me"})
    assert response.status_code == 202
    receipt = response.json()
    assert receipt["run_id"] == receipt["turn_id"]

    other_runtime = RuntimeApiServer(cwd=runtime.cwd, config=runtime.config, api_key="other")
    with pytest.raises(TurnInProgressError):
        other_runtime._start_background_turn(session_id, "duplicate", mode="react")
    events = other_runtime.session_store.events(
        session_id, after_sequence=receipt["after_sequence"]
    )
    assert [event.event_type for event in events] == ["turn/started", "user/message"]
    assert {event.turn_id for event in events} == {receipt["turn_id"]}
    assert events[-1].payload == {"content": "persist me"}
    other_runtime.session_store.recover_interrupted_turns()
    assert other_runtime.session_store.state(session_id).status == "running"
    runtime.session_store.release_turn(session_id, receipt["turn_id"])
    other_runtime.session_store.recover_interrupted_turns()
    assert other_runtime.session_store.state(session_id).status == "canceled"


def test_audit_failure_keeps_tool_result_and_allows_follow_up(
    runtime_http, provider_stream, tmp_path
):
    runtime, client = runtime_http
    runtime.config.policy.hitl_mode = "never"
    audit_dir = tmp_path / "unwritable-audit"
    audit_dir.mkdir()
    runtime.config.policy.audit_log_path = str(audit_dir)
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
                                        "id": "call_audit",
                                        "function": {
                                            "name": "write_file",
                                            "arguments": '{"path":"note.txt","content":"ok"}',
                                        },
                                    }
                                ]
                            },
                            "finish_reason": "tool_calls",
                        }
                    ]
                }
            ],
            [{"choices": [{"delta": {"content": "written"}, "finish_reason": "stop"}]}],
            [{"choices": [{"delta": {"content": "continued"}, "finish_reason": "stop"}]}],
        ]
    )
    session_id = client.post("/v1/sessions").json()["id"]
    path = f"/v1/sessions/{session_id}/turns"
    assert client.post(path, json={"message": "write"}).status_code == 200
    assert (tmp_path / "note.txt").read_text() == "ok"
    assert client.post(path, json={"message": "continue"}).status_code == 200
    assert any(message.get("tool_call_id") == "call_audit" for message in requests[-1]["messages"])


def test_http_sessions_events_and_sse_have_consistent_cursors(runtime_http):
    runtime, client = runtime_http
    assert client.get("/v1/health").json()["status"] == "ok"
    sid = client.post("/v1/sessions").json()["id"]
    runtime.session_store.append(sid, "user/message", {"content": "hello"}, turn_id="turn-1")
    answer = runtime.session_store.append(
        sid, "assistant/message", {"content": "answer"}, turn_id="turn-1"
    )
    runtime.session_store.create_session("child", kind="child", parent_session_id=sid)
    assert [item["id"] for item in client.get("/v1/sessions").json()["sessions"]] == [sid]
    page = client.get(f"/v1/sessions/{sid}/events.json?cursor=1").json()
    assert [item["type"] for item in page["events"]] == ["user/message", "assistant/message"]
    assert page["next_cursor"] == answer.sequence
    assert page["events"][-1]["turn_id"] == "turn-1"
    empty = client.get(f"/v1/sessions/{sid}/events.json?cursor={answer.sequence}").json()
    assert empty["events"] == []
    assert empty["next_cursor"] == answer.sequence
    assert client.get(f"/v1/sessions/{sid}/events").status_code == 404
    with client.stream("GET", f"/v1/sessions/{sid}/events/stream?cursor=2") as stream:
        replay = next(_event_stream_pages(stream))
    assert [event["sequence"] for event in replay["events"]] == [answer.sequence]


def test_http_restart_recovers_orphaned_turns_across_entry_points(
    tmp_path, config, provider_stream, monkeypatch
):
    config.llm.api_key = "synthetic-runtime-llm-credential"
    runtime = RuntimeApiServer(
        cwd=str(tmp_path), config=config, api_key="synthetic", port=0, workers=0
    )
    store = runtime.session_store
    for sid in ("interrupted", "other-listener", "cli", "continued-in-cli"):
        store.create_session(sid)

    def start(sid, turn_id, port=None):
        payload = {"mode": "react"}
        if port is not None:
            payload["runtime_port"] = port
        store.append(sid, "turn/started", payload, turn_id=turn_id)

    ready = Queue()

    class RestartedServer(ThreadingHTTPServer):
        def server_activate(self):
            super().server_activate()
            start("cli", "cli-turn")
            start("other-listener", "other-turn", self.server_port + 1)
            start("continued-in-cli", "old-runtime-turn", self.server_port)
            store.start_turn("continued-in-cli", "new-cli-turn", mode="react", message="Live")
            sid, tid, step = "interrupted", "old-turn", "tool-step"
            start(sid, tid, self.server_port)
            store.append(sid, "user/message", {"content": "Read and write"}, turn_id=tid)
            store.append(sid, "step/started", {}, turn_id=tid, step_id=step)
            store.append(
                sid,
                "assistant/message",
                {
                    "content": "",
                    "tool_calls": [
                        {
                            "id": "read-call",
                            "type": "function",
                            "function": {"name": "read_file", "arguments": "{}"},
                        },
                        {
                            "id": "write-call",
                            "type": "function",
                            "function": {"name": "write_file", "arguments": "{}"},
                        },
                    ],
                },
                turn_id=tid,
                step_id=step,
            )
            store.append(
                sid,
                "tool/result",
                {"content": "read completed", "name": "read_file"},
                turn_id=tid,
                step_id=step,
                tool_call_id="read-call",
            )
            store.append(
                sid,
                "approval/started",
                {"tool_name": "write_file"},
                turn_id=tid,
                tool_call_id="write-call",
                approval_id="pending-approval",
            )
            store.acquire_compaction_lease(
                sid,
                attempt_id="interrupted-compaction",
                base_sequence=store.last_sequence(sid),
                surface_hash="surface",
            )

        def serve_forever(self, poll_interval=0.01):
            ready.put(self)
            super().serve_forever(poll_interval=poll_interval)

    monkeypatch.setattr("paicli.runtime.api.ThreadingHTTPServer", RestartedServer)
    batches, requests = provider_stream
    batches.append([{"choices": [{"delta": {"content": "Resumed"}, "finish_reason": "stop"}]}])
    thread = threading.Thread(target=runtime.serve_forever, daemon=True)
    thread.start()
    server = ready.get(timeout=5)
    try:
        with httpx.Client(
            base_url=f"http://127.0.0.1:{server.server_port}",
            headers={"x-api-key": runtime.api_key},
            timeout=5,
            trust_env=False,
        ) as client:
            base = "/v1/sessions/interrupted"
            with client.stream(
                "GET", f"{base}/events/stream", params={"view": "conversation"}
            ) as stream:
                page = next(_event_stream_pages(stream))
            assert page["state"]["status"] == "canceled"
            assert page["active_run"] is None
            terminals = [event for event in page["events"] if event["type"] == "turn/canceled"]
            assert len(terminals) == 1
            assert terminals[0]["payload"]["reason"] == "executor_interrupted"
            approval = next(
                event for event in page["events"] if event["type"] == "approval/canceled"
            )
            assert approval["approval_id"] == "pending-approval"
            for sid in ("cli", "other-listener"):
                assert (
                    client.get(f"/v1/sessions/{sid}/events.json").json()["state"]["status"]
                    == "canceled"
                )
            live = client.get("/v1/sessions/continued-in-cli/events.json").json()
            assert live["state"]["status"] == "running"
            resumed = client.post(f"{base}/turns", json={"message": "Continue"})
            assert resumed.status_code == 200
            assert resumed.json()["text"] == "Resumed"
            results = [
                (message["tool_call_id"], message["content"])
                for message in requests[-1]["messages"]
                if message["role"] == "tool"
            ]
            assert results == [
                ("read-call", "read completed"),
                (
                    "write-call",
                    "Tool call interrupted before the executor settled. Result unknown.",
                ),
            ]
            page = client.get(f"{base}/events.json").json()
            latest_start = [event for event in page["events"] if event["type"] == "turn/started"][
                -1
            ]
            assert latest_start["payload"] == {"mode": "react"}
    finally:
        store.release_turn("continued-in-cli", "new-cli-turn")
        server.shutdown()
        server.server_close()
        thread.join(timeout=5)
        assert not thread.is_alive()


@pytest.mark.parametrize("background", [True, False])
def test_http_shutdown_keeps_listener_bound_until_accepted_turn_exits(
    tmp_path, config, monkeypatch, background
):
    config.llm.api_key = "synthetic-runtime-llm-credential"
    runtime = RuntimeApiServer(
        cwd=str(tmp_path), config=config, api_key="synthetic", port=0, workers=0
    )
    ready, started = Queue(), threading.Event()
    release, cancel_seen = threading.Event(), threading.Event()

    class CapturedServer(ThreadingHTTPServer):
        def serve_forever(self, poll_interval=0.01):
            ready.put(self)
            super().serve_forever(poll_interval=poll_interval)

    class GatedStream(httpx.AsyncByteStream):
        async def __aiter__(self):
            started.set()
            try:
                assert await asyncio.to_thread(release.wait, 5)
            except asyncio.CancelledError:
                cancel_seen.set()
                assert await asyncio.to_thread(release.wait, 5)
                raise
            yield b'data: {"choices":[{"delta":{"content":"Done"},"finish_reason":"stop"}]}\n\n'

    async def respond(_transport, _request):
        return httpx.Response(200, stream=GatedStream())

    monkeypatch.setattr("paicli.runtime.api.ThreadingHTTPServer", CapturedServer)
    monkeypatch.setattr(httpx.AsyncHTTPTransport, "handle_async_request", respond)
    thread = threading.Thread(target=runtime.serve_forever, daemon=True)
    thread.start()
    server = ready.get(timeout=5)
    try:
        with (
            httpx.Client(
                base_url=f"http://127.0.0.1:{server.server_port}",
                headers={"x-api-key": runtime.api_key},
                timeout=10,
                trust_env=False,
            ) as client,
            ThreadPoolExecutor(max_workers=1) as requests,
        ):
            sid = client.post("/v1/sessions").json()["id"]
            path = f"/v1/sessions/{sid}/turns" + ("/async" if background else "")
            result = requests.submit(client.post, path, json={"message": "Start"})
            try:
                if background:
                    assert result.result(timeout=5).status_code == 202
                assert started.wait(5)
                server.shutdown()
                assert runtime._stop.wait(5)
                if background:
                    assert cancel_seen.wait(3)
                with socket.socket() as replacement, pytest.raises(OSError):
                    replacement.bind(("127.0.0.1", server.server_port))
                assert thread.is_alive()
            finally:
                release.set()
            if not background:
                assert result.result(timeout=5).status_code == 200
        thread.join(timeout=5)
        assert not thread.is_alive()
        assert runtime.session_store.state(sid).status == (
            "canceled" if background else "completed"
        )
    finally:
        release.set()
        server.shutdown()
        thread.join(timeout=5)


@pytest.mark.parametrize("body", [b"invalid", b"[]", b"null", b"\xff"])
def test_http_rejects_invalid_json_without_side_effects(runtime_http, body):
    runtime, client = runtime_http
    response = client.post("/v1/sessions", content=body)
    assert response.status_code == 400
    assert runtime.session_store.list_sessions() == []


@pytest.mark.parametrize(
    ("path", "body"),
    [
        ("/v1/tasks", {}),
        ("/v1/tasks", {"prompt": " \n "}),
        ("/v1/tasks", {"prompt": "hello", "mode": "team"}),
        ("/v1/sessions/{session_id}/turns", {}),
        ("/v1/sessions/{session_id}/turns", {"message": "hello", "mode": "team"}),
        ("/v1/sessions/{session_id}/turns/async", {}),
        ("/v1/sessions/{session_id}/turns/async", {"message": "hello", "mode": "team"}),
    ],
)
def test_http_rejects_invalid_tasks_and_turns(runtime_http, path, body):
    runtime, client = runtime_http
    if "{session_id}" in path:
        path = path.format(session_id=runtime._create_session())
    assert client.post(path, json=body).status_code == 400
    assert runtime.task_manager.list() == []


@pytest.mark.parametrize("suffix", ["events.json", "events/stream"])
def test_http_rejects_invalid_event_cursor(runtime_http, suffix):
    runtime, client = runtime_http
    session_id = runtime._create_session()
    assert client.get(f"/v1/sessions/{session_id}/{suffix}?cursor=bad").status_code == 400


def test_http_task_lifecycle_and_unknown_routes(runtime_http):
    runtime, client = runtime_http
    response = client.post("/v1/tasks", json={"prompt": "work", "mode": " PLAN "})
    assert response.status_code == 200
    task = response.json()
    assert task["mode"] == "plan"
    assert client.get(f"/v1/tasks/{task['id']}").json()["status"] == "queued"
    assert len(client.get("/v1/tasks").json()["tasks"]) == 1
    assert client.post(f"/v1/tasks/{task['id']}/cancel").json() == {"canceled": True}
    assert client.post(f"/v1/tasks/{task['id']}/cancel").json() == {"canceled": False}
    assert runtime.task_manager.claim_next() is None
    assert client.get("/v1/tasks/missing").status_code == 404
    assert client.get("/unknown").status_code == 404


@pytest.mark.parametrize("mode", ["react", "plan"])
def test_http_turn_runs_real_engine_and_persists_terminal_state(
    runtime_http, provider_stream, mode
):
    runtime, client = runtime_http
    batches, _requests = provider_stream
    if mode == "plan":
        batches.append(
            [
                {
                    "choices": [
                        {
                            "delta": {
                                "content": json.dumps(
                                    {
                                        "tasks": [
                                            {"id": "answer", "description": "Answer the request"}
                                        ]
                                    }
                                )
                            },
                            "finish_reason": "stop",
                        }
                    ]
                }
            ]
        )
    batches.append(
        [
            {"choices": [{"delta": {"content": "runtime answer"}, "finish_reason": "stop"}]},
            {"choices": [], "usage": {"prompt_tokens": 10, "completion_tokens": 4}},
        ]
    )
    sid = client.post("/v1/sessions").json()["id"]
    response = client.post(f"/v1/sessions/{sid}/turns", json={"message": "hello", "mode": mode})
    assert response.status_code == 200, response.text
    assert "runtime answer" in response.json()["text"]
    assert response.json()["total_tokens"] == 14
    events = runtime.session_store.events(sid)
    assert sum(item.event_type == "turn/started" for item in events) == 1
    assert sum(item.event_type == "turn/completed" for item in events) == 1
    assert runtime.session_store.state(sid).active_turn_id is None


@pytest.mark.parametrize("mode", ["react", "plan"])
def test_http_engine_failure_returns_error_and_persists_failed_turn(
    runtime_http, monkeypatch, mode
):
    runtime, client = runtime_http
    provider_attempts = 0

    async def reject(_transport, _request):
        nonlocal provider_attempts
        provider_attempts += 1
        return httpx.Response(503, text="synthetic-private-provider-diagnostic")

    monkeypatch.setattr(httpx.AsyncHTTPTransport, "handle_async_request", reject)
    sid = client.post("/v1/sessions").json()["id"]
    response = client.post(f"/v1/sessions/{sid}/turns", json={"message": "hello", "mode": mode})
    assert response.status_code == 500
    assert "HTTP 503" in response.json()["error"]
    assert "synthetic-private" not in response.text
    assert runtime.session_store.state(sid).status == "failed"
    assert provider_attempts == 2
    page = client.get(f"/v1/sessions/{sid}/events.json")
    failed = next(event for event in page.json()["events"] if event["type"] == "turn/failed")
    assert failed["payload"].get("provider_error") == {"kind": "http", "status_code": 503}
    assert "synthetic-private" not in page.text


def test_http_plan_child_failure_reports_failed_and_skipped_tasks(runtime_http, provider_stream):
    runtime, client = runtime_http
    batches, _ = provider_stream
    plan = {
        "tasks": [
            {"id": "first", "description": "First operation"},
            {"id": "second", "description": "Dependent operation", "dependencies": ["first"]},
        ]
    }
    batches.append(
        [{"choices": [{"delta": {"content": json.dumps(plan)}, "finish_reason": "stop"}]}]
    )
    batches.append([{"error": {"message": "synthetic child error"}}])
    sid = runtime._create_session()
    response = client.post(f"/v1/sessions/{sid}/turns", json={"message": "work", "mode": "plan"})
    assert response.status_code == 200
    assert response.json()["plan_outcome"] == {
        "status": "failed",
        "failed_task_ids": ["task_1"],
        "skipped_task_ids": ["task_2"],
    }
    assert runtime.session_store.state(sid).status == "failed"


def test_event_pages_bound_history_without_skipping_events(runtime_http, monkeypatch):
    runtime, client = runtime_http
    monkeypatch.setattr("paicli.runtime.api._EVENT_PAGE_SIZE", 2)
    sid = runtime._create_session()
    for index in range(5):
        runtime.session_store.append(sid, "user/message", {"content": str(index)})
    base = f"/v1/sessions/{sid}"
    received = []
    cursor = 0
    for more in (True, True, False):
        page = client.get(f"{base}/events.json", params={"cursor": cursor}).json()
        received.extend(
            event["payload"]["content"]
            for event in page["events"]
            if event["type"] == "user/message"
        )
        cursor = page["next_cursor"]
        assert page["has_more"] is more
    assert received == ["0", "1", "2", "3", "4"]
    with client.stream("GET", f"{base}/events/stream", params={"view": "conversation"}) as stream:
        pages = _event_stream_pages(stream)
        assert [next(pages)["next_cursor"] for _ in range(3)] == [2, 4, 6]


def test_runtime_rejects_oversized_request_body(runtime_http):
    _runtime, client = runtime_http
    result = client.post("/v1/sessions", content=b"x" * (1024 * 1024 + 1))
    assert result.status_code == 413
    assert client.get("/v1/sessions").json()["sessions"] == []


def test_http_turn_recovers_from_one_transient_provider_failure(runtime_http, monkeypatch):
    _runtime, client = runtime_http
    provider_requests = []

    async def respond(_transport, request):
        provider_requests.append(json.loads(request.content))
        if len(provider_requests) == 1:
            return httpx.Response(
                503,
                headers={"x-request-id": "upstream-attempt-1"},
                text="synthetic-private-provider-diagnostic",
            )
        return httpx.Response(
            200,
            headers={"content-type": "text/event-stream"},
            text=(
                'data: {"choices":[{"delta":{"content":"hello back"},'
                '"finish_reason":"stop"}]}\n\ndata: [DONE]\n\n'
            ),
        )

    monkeypatch.setattr(httpx.AsyncHTTPTransport, "handle_async_request", respond)
    sid = client.post("/v1/sessions").json()["id"]
    response = client.post(f"/v1/sessions/{sid}/turns", json={"message": "hello"})
    assert response.status_code == 200, response.text
    assert response.json()["text"] == "hello back"
    assert len(provider_requests) == 2 and provider_requests[0] == provider_requests[1]
    page = client.get(f"/v1/sessions/{sid}/events.json").json()
    types = [event["type"] for event in page["events"]]
    assert types.count("user/message") == types.count("turn/completed") == 1
    assert "turn/failed" not in types
    prepared = next(event for event in page["events"] if event["type"] == "request/prepared")
    attempts = [
        event["payload"] for event in page["events"] if event["type"] == "request/attempt_failed"
    ]
    assert len(attempts) == 1
    attempt = attempts[0]
    assert attempt["request_id"] == prepared["payload"]["request_id"]
    assert attempt["attempt"] == 1 and attempt["retrying"] is True
    assert attempt["stage"] == "response_headers"
    assert attempt["elapsed_ms"] >= 0
    assert attempt["upstream_request_id"] == "upstream-attempt-1"
    assert attempt["provider_error"] == {"kind": "http", "status_code": 503}
    assert "synthetic-private-provider-diagnostic" not in json.dumps(page)


def test_http_provider_error_after_tool_delta_does_not_execute_tool(runtime_http, monkeypatch):
    runtime, client = runtime_http
    tool = {
        "index": 0,
        "id": "call_partial_write",
        "function": {
            "name": "write_file",
            "arguments": '{"path":"partial.txt","content":"unexpected"}',
        },
    }

    async def respond(_transport, _request):
        delta = json.dumps({"choices": [{"delta": {"tool_calls": [tool]}}]})
        return httpx.Response(
            200,
            text=(
                f"data: {delta}\n\n"
                'data: {"error":{"message":"synthetic-private-provider-diagnostic"}}\n\n'
                "data: [DONE]\n\n"
            ),
        )

    monkeypatch.setattr(httpx.AsyncHTTPTransport, "handle_async_request", respond)
    sid = client.post("/v1/sessions").json()["id"]
    assert client.post(f"/v1/sessions/{sid}/permissions", json={"mode": "auto"}).status_code == 200
    response = client.post(f"/v1/sessions/{sid}/turns", json={"message": "write"})
    assert response.status_code == 500
    assert not (Path(runtime.cwd) / "partial.txt").exists()
    page = client.get(f"/v1/sessions/{sid}/events.json")
    failed = next(event for event in page.json()["events"] if event["type"] == "turn/failed")
    assert failed["payload"]["provider_error"] == {
        "kind": "protocol",
        "code": "provider_error_frame",
    }
    assert "synthetic-private-provider-diagnostic" not in page.text


def test_http_async_turn_exposes_text_before_provider_finishes(runtime_http, monkeypatch):
    _runtime, client = runtime_http
    first_chunk_consumed = threading.Event()
    release_provider = threading.Event()

    class GatedStream(httpx.AsyncByteStream):
        async def __aiter__(self):
            yield b'data: {"choices":[{"delta":{"content":"First part"}}]}\n\n'
            first_chunk_consumed.set()
            assert await asyncio.to_thread(release_provider.wait, 5)
            yield (
                b'data: {"choices":[{"delta":{"content":" and final part."},'
                b'"finish_reason":"stop"}]}\n\ndata: [DONE]\n\n'
            )

    async def respond(_transport, _request):
        return httpx.Response(200, stream=GatedStream())

    monkeypatch.setattr(httpx.AsyncHTTPTransport, "handle_async_request", respond)
    sid = client.post("/v1/sessions").json()["id"]
    events_url = f"/v1/sessions/{sid}/events.json"
    last_sequence = client.get(events_url).json()["next_cursor"]
    try:
        response = client.post(f"/v1/sessions/{sid}/turns/async", json={"message": "hello"})
        assert response.status_code == 202
        assert response.json()["after_sequence"] == last_sequence
        assert first_chunk_consumed.wait(5)
        page = client.get(events_url).json()
        assert page["state"]["status"] == "running"
        assert page["assistant_preview"] == {
            "turn_id": page["state"]["active_turn_id"],
            "step_id": page["state"]["active_step_id"],
            "text": "First part",
        }
        cursor = page["next_cursor"]
        same_cursor_page = client.get(events_url, params={"cursor": cursor}).json()
        assert same_cursor_page["assistant_preview"] == page["assistant_preview"]
        assert same_cursor_page["events"] == []
        assert same_cursor_page["next_cursor"] == cursor
    finally:
        release_provider.set()

    deadline = time.monotonic() + 5
    while time.monotonic() < deadline:
        page = client.get(events_url, params={"cursor": cursor}).json()
        if any(event["type"] == "turn/completed" for event in page["events"]):
            break
        time.sleep(0.01)
    else:
        pytest.fail("The async turn did not complete after releasing the provider")
    assert page["assistant_preview"] is None
    assert [
        e["payload"]["content"] for e in page["events"] if e["type"] == "assistant/message"
    ] == ["First part and final part."]


@pytest.mark.parametrize(
    ("mode", "decision"),
    [("react", "approve"), ("plan", "deny"), ("react", "timeout"), ("react", None)],
)
def test_http_interactive_approval_controls_tool_execution(
    runtime_http, provider_stream, monkeypatch, mode, decision
):
    runtime, client = runtime_http
    batches, _requests = provider_stream
    if decision == "timeout":
        monkeypatch.setattr("paicli.runtime.api._APPROVAL_TIMEOUT", 0.05)
    if mode == "plan":
        plan = {"tasks": [{"id": "write", "description": "Write the requested file"}]}
        batches.append(
            [{"choices": [{"delta": {"content": json.dumps(plan)}, "finish_reason": "stop"}]}]
        )
    tool = {
        "index": 0,
        "id": "write-call",
        "function": {
            "name": "write_file",
            "arguments": json.dumps({"path": "result.txt", "content": runtime.config.llm.api_key}),
        },
    }
    batches.extend(
        [
            [{"choices": [{"delta": {"tool_calls": [tool]}, "finish_reason": "tool_calls"}]}],
            [{"choices": [{"delta": {"content": "Done"}, "finish_reason": "stop"}]}],
        ]
    )
    target = Path(runtime.cwd) / "result.txt"
    sid = client.post("/v1/sessions").json()["id"]
    body = {"message": "Write the requested file", "mode": mode}
    if decision is not None:
        body["interactive_approval"] = True
    response = client.post(f"/v1/sessions/{sid}/turns/async", json=body)
    assert response.status_code == 202
    events_url = f"/v1/sessions/{sid}/events.json"

    def wait_for(event_type):
        deadline = time.monotonic() + 3
        while time.monotonic() < deadline:
            events = client.get(events_url).json()["events"]
            for event in events:
                if event["type"] == event_type:
                    return event
            time.sleep(0.01)
        pytest.fail(f"No {event_type} event in {[event['type'] for event in events]}")

    if decision is None:
        wait_for("turn/completed")
        assert not target.exists()
        assert not any(
            event["type"] == "approval/started" for event in client.get(events_url).json()["events"]
        )
        return

    started = wait_for("approval/started")
    assert not target.exists()
    assert started["payload"]["input"] == {"path": "result.txt", "content": "[redacted]"}
    assert started["tool_call_id"] == "write-call"
    suffix = f"/approvals/{started['approval_id']}/decision"
    url = f"/v1/sessions/{sid}{suffix}"
    if decision == "timeout":
        resolved = wait_for("approval/canceled")
    else:
        for route in ("turns", "turns/async"):
            overlapping = client.post(f"/v1/sessions/{sid}/{route}", json={"message": "Overlap"})
            assert overlapping.status_code == 409
        other_sid = client.post("/v1/sessions").json()["id"]
        other_url = f"/v1/sessions/{other_sid}{suffix}"
        assert client.post(other_url, json={"decision": decision}).status_code == 404
        unauthorized = client.post(url, headers={"x-api-key": ""}, json={"decision": decision})
        assert unauthorized.status_code == 401
        assert client.post(url, json={"decision": "invalid"}).status_code == 400
        assert client.post(url, json={"decision": decision}).status_code == 200
        resolved = wait_for("approval/approved" if decision == "approve" else "approval/rejected")
    assert client.post(url, json={"decision": "approve"}).status_code in {404, 409}

    completed = wait_for("turn/completed")
    assert resolved["approval_id"] == started["approval_id"]
    assert completed["turn_id"] == resolved["turn_id"] == started["turn_id"]
    assert target.exists() == (decision == "approve")


@pytest.mark.parametrize(
    ("mode", "secret_id"), [("react", False), ("plan", False), ("react", True)]
)
def test_http_cancel_waiting_approval_then_continue_the_same_session(
    runtime_http, provider_stream, mode, secret_id
):
    runtime, client = runtime_http
    batches, requests = provider_stream
    read_call_id = runtime.config.llm.api_key if secret_id else "read-call"
    (Path(runtime.cwd) / "source.txt").write_text("Already read", encoding="utf-8")
    if mode == "plan":
        plan = {"tasks": [{"id": "write", "description": "Write the requested file"}]}
        batches.append(
            [{"choices": [{"delta": {"content": json.dumps(plan)}, "finish_reason": "stop"}]}]
        )
    calls = [
        {
            "index": index,
            "id": call_id,
            "function": {"name": name, "arguments": json.dumps(arguments)},
        }
        for index, (call_id, name, arguments) in enumerate(
            [
                (read_call_id, "read_file", {"path": "source.txt"}),
                ("write-call", "write_file", {"path": "result.txt", "content": "New"}),
            ]
        )
    ]
    batches.extend(
        [
            [{"choices": [{"delta": {"tool_calls": calls}, "finish_reason": "tool_calls"}]}],
            [{"choices": [{"delta": {"content": "Resumed"}, "finish_reason": "stop"}]}],
        ]
    )
    sid = client.post("/v1/sessions").json()["id"]
    other_sid = client.post("/v1/sessions").json()["id"]
    session_url = f"/v1/sessions/{sid}"
    response = client.post(
        f"{session_url}/turns/async",
        json={"message": "Read then write", "mode": mode, "interactive_approval": True},
    )
    assert response.status_code == 202
    run_id = response.json()["run_id"]

    page = _wait_for_session_page(
        client,
        session_url,
        lambda page: any(e["type"] == "approval/started" for e in page["events"]),
    )
    approval = next(e for e in page["events"] if e["type"] == "approval/started")
    cancel_url = f"{session_url}/runs/{run_id}/cancel"
    assert client.post(cancel_url, headers={"x-api-key": ""}).status_code == 401
    assert client.post(f"/v1/sessions/{other_sid}/runs/{run_id}/cancel").status_code == 404
    with client.stream(
        "GET", f"{session_url}/events/stream", params={"view": "conversation"}
    ) as stream:
        replay = next(_event_stream_pages(stream))
        assert client.post(cancel_url).status_code == 202
        assert replay["active_run"] == {"run_id": run_id, "status": "running"}

    page = _wait_for_session_page(client, session_url, _is_stopped)
    assert client.post(cancel_url).status_code == 409
    approval_url = f"{session_url}/approvals/{approval['approval_id']}/decision"
    assert client.post(approval_url, json={"decision": "approve"}).status_code == 404
    assert not (Path(runtime.cwd) / "result.txt").exists()
    terminal = {"turn/canceled", "turn/completed", "turn/failed"}
    assert [e["type"] for e in page["events"] if e["type"] in terminal] == ["turn/canceled"]
    assert [e["type"] for e in page["events"] if e["approval_id"] == approval["approval_id"]] == [
        "approval/started",
        "approval/canceled",
    ]
    assert len(requests) == (1 if mode == "react" else 2)
    if secret_id:
        assert read_call_id not in json.dumps(page)

    resumed = client.post(f"{session_url}/turns", json={"message": "Continue"})
    assert resumed.status_code == 200
    assert resumed.json()["text"] == "Resumed"
    if mode == "react":
        tool_messages = {
            message["tool_call_id"]: message["content"]
            for message in requests[-1]["messages"]
            if message["role"] == "tool"
        }
        assert len(tool_messages) == 2
        read_id = next(
            call_id for call_id, content in tool_messages.items() if "Already read" in content
        )
        assert (read_id != read_call_id) is secret_id
        assert "canceled" in tool_messages["write-call"]


@pytest.mark.parametrize("child_cleanup_times_out", [False, True])
def test_http_cancel_interrupts_provider_stream_and_waits_for_cleanup(
    runtime_http, monkeypatch, child_cleanup_times_out
):
    runtime, client = runtime_http
    streaming = threading.Event()
    closing = threading.Event()
    release = threading.Event()
    if child_cleanup_times_out:
        runtime.config.subagents.drain_timeout = 0.1

        async def stuck_child_cleanup(_manager, _parent_id):
            await asyncio.Event().wait()

        monkeypatch.setattr(
            "paicli.agent.subagents.SubagentManager.cancel_for_parent", stuck_child_cleanup
        )

    class PendingStream(httpx.AsyncByteStream):
        async def __aiter__(self):
            yield b'data: {"choices":[{"delta":{"content":"partial response"}}]}\n\n'
            streaming.set()
            await asyncio.Event().wait()

        async def aclose(self):
            closing.set()
            assert await asyncio.to_thread(release.wait, 5)

    async def respond(_transport, _request):
        return httpx.Response(200, stream=PendingStream())

    monkeypatch.setattr(httpx.AsyncHTTPTransport, "handle_async_request", respond)
    sid = client.post("/v1/sessions").json()["id"]
    session_url = f"/v1/sessions/{sid}"
    response = client.post(f"{session_url}/turns/async", json={"message": "Stream a response"})
    assert response.status_code == 202
    run_id = response.json()["run_id"]
    cancel_url = f"{session_url}/runs/{run_id}/cancel"
    try:
        assert streaming.wait(3)
        assert client.post(cancel_url).status_code == 202
        assert closing.wait(3)
        page = client.get(f"{session_url}/events.json").json()
        assert page["active_run"] == {"run_id": run_id, "status": "canceling"}
        assert page["state"]["status"] == "running"
        assert client.post(cancel_url).status_code == 202
        assert client.post(f"{session_url}/turns", json={"message": "Overlap"}).status_code == 409
    finally:
        release.set()
    page = _wait_for_session_page(client, session_url, _is_stopped)
    assert page["assistant_preview"] is None
    terminal = next(event for event in page["events"] if event["type"] == "turn/canceled")
    assert terminal["payload"].get("cleanup_error") == (
        "TimeoutError" if child_cleanup_times_out else None
    )


def test_http_cancel_stops_a_running_command_child(runtime_http, provider_stream):
    runtime, client = runtime_http
    runtime.config.policy.hitl_mode = "never"
    batches, requests = provider_stream
    work = Path(runtime.cwd)
    marker = work / "child-finished"
    script = work / "run_child.py"
    script.write_text(
        'from pathlib import Path\nimport time\nPath("child-ready").touch()\n'
        'time.sleep(2)\nPath("child-finished").touch()\n',
        encoding="utf-8",
    )
    if os.name == "nt":
        command = "& '{}' '{}'".format(sys.executable.replace("'", "''"), script.name)
    else:
        command = f"{shlex.quote(sys.executable)} {script.name}; echo done"
    tool = {
        "index": 0,
        "id": "command-call",
        "function": {"name": "bash", "arguments": json.dumps({"command": command})},
    }
    batches.append(
        [{"choices": [{"delta": {"tool_calls": [tool]}, "finish_reason": "tool_calls"}]}]
    )
    sid = client.post("/v1/sessions").json()["id"]
    session_url = f"/v1/sessions/{sid}"
    response = client.post(f"{session_url}/turns/async", json={"message": "Run the command"})
    assert response.status_code == 202
    ready = work / "child-ready"
    deadline = time.monotonic() + 3
    while not ready.exists() and time.monotonic() < deadline:
        time.sleep(0.01)
    assert ready.exists()
    run_id = response.json()["run_id"]
    assert client.post(f"{session_url}/runs/{run_id}/cancel").status_code == 202
    _wait_for_session_page(client, session_url, _is_stopped)
    time.sleep(2)
    assert not marker.exists()
    assert len(requests) == 1


def _wait_for_session_page(client, session_url, predicate):
    deadline = time.monotonic() + 3
    while time.monotonic() < deadline:
        page = client.get(f"{session_url}/events.json").json()
        if predicate(page):
            return page
        time.sleep(0.01)
    pytest.fail("Expected Runtime state was not published")


def _is_stopped(page):
    return (
        page["state"]["status"] == "canceled"
        and not page["active_run"]
        and any(event["type"] == "turn/canceled" for event in page["events"])
    )


@pytest.mark.parametrize("outcome", ["auto", "manual", "error"])
def test_session_title_runs_once_without_blocking_or_overwriting_manual_name(
    runtime_http, monkeypatch, outcome
):
    runtime, client = runtime_http
    runtime.config.session_title.enabled = True
    runtime.config.session_title.model = "title-model" if outcome != "error" else ""
    runtime.config.llm.thinking_enabled = True
    title_started = threading.Event()
    release_title = threading.Event()
    title_requests = []

    async def respond(_transport, request):
        body = json.loads(request.content)
        system = body["messages"][0]["content"]
        if system.startswith("Write a short chat title"):
            title_requests.append(body)
            title_started.set()
            assert await asyncio.to_thread(release_title.wait, 5)
            if outcome == "error":
                return httpx.Response(503)
            text = "“检查目录结构”"
        else:
            assert body["thinking"] == {"type": "enabled"}
            text = "Main reply"
        chunk = {"choices": [{"delta": {"content": text}, "finish_reason": "stop"}]}
        return httpx.Response(200, content=f"data: {json.dumps(chunk)}\n\ndata: [DONE]\n\n")

    monkeypatch.setattr(httpx.AsyncHTTPTransport, "handle_async_request", respond)
    session_id = client.post("/v1/sessions").json()["id"]
    base = f"/v1/sessions/{session_id}"
    assert client.get("/v1/sessions").json()["sessions"][0]["title"] is None
    for route in ("turns", "turns/async"):
        assert client.post(f"{base}/{route}", json={"message": "  \t "}).status_code == 400
    try:
        first = client.post(f"{base}/turns/async", json={"message": "请检查这个目录"})
        assert first.status_code == 202
        assert title_started.wait(3)
        _wait_for_session_page(
            client,
            base,
            lambda page: page["state"]["status"] == "completed" and not page["active_run"],
        )
        second = client.post(f"{base}/turns", json={"message": "再检查一次"})
        assert second.json()["text"] == "Main reply"
        if outcome == "manual":
            assert client.post(f"{base}/title", json={"title": " "}).status_code == 400
            renamed = client.post(f"{base}/title", json={"title": "  我自己的名称  "})
            assert renamed.json() == {"title": "我自己的名称"}
    finally:
        release_title.set()
        runtime._title_executor.shutdown(wait=True)

    assert len(title_requests) == 1
    request = title_requests[0]
    assert request["model"] == ("title-model" if outcome != "error" else runtime.config.llm.model)
    assert request["thinking"] == {"type": "disabled"}
    assert "tools" not in request
    assert request["messages"][1:] == [{"role": "user", "content": "请检查这个目录"}]

    title = {"auto": "检查目录结构", "manual": "我自己的名称", "error": None}[outcome]
    page = client.get(f"{base}/events.json").json()
    assert page["state"]["title"] == title
    assert client.get("/v1/sessions").json()["sessions"][0]["title"] == title
    with client.stream(
        "GET",
        f"{base}/events/stream",
        params={"view": "conversation", "cursor": page["next_cursor"]},
    ) as opened:
        replay = next(_event_stream_pages(opened))
    assert replay["events"] == []
    assert replay["state"]["title"] == title

    reopened = SessionStore(runtime.db_path, workspace=runtime.cwd)
    assert not reopened.claim_session_title(session_id, first.json()["turn_id"])


def test_http_session_permission_mode_is_scoped_and_validated(runtime_http):
    runtime, client = runtime_http
    sid = client.post("/v1/sessions").json()["id"]
    other_sid = client.post("/v1/sessions").json()["id"]
    permissions = f"/v1/sessions/{sid}/permissions"
    assert client.get(permissions).json() == {"mode": "default"}
    assert (
        client.post(permissions, json={"mode": "auto"}, headers={"x-api-key": ""}).status_code
        == 401
    )
    assert client.post(permissions, json={"mode": "unknown"}).status_code == 400
    assert client.post("/v1/sessions/missing/permissions", json={"mode": "auto"}).status_code == 404
    assert client.post(permissions, json={"mode": "auto"}).json() == {"mode": "auto"}
    assert client.get(f"/v1/sessions/{sid}/events.json").json()["permission_mode"] == "auto"
    assert (
        client.get(f"/v1/sessions/{other_sid}/events.json").json()["permission_mode"] == "default"
    )
    assert runtime.config.policy.hitl_mode == "auto"
    assert runtime.config.policy.path_guard_enabled
    assert runtime.config.policy.command_guard_enabled
    restarted = RuntimeApiServer(
        cwd=runtime.cwd, config=runtime.config, api_key=runtime.api_key, workers=0
    )
    assert restarted._events_json(sid, 0)["permission_mode"] == "default"


@pytest.mark.parametrize("suffix", ["turns", "turns/async"])
def test_http_restore_default_keeps_the_current_full_access_turn(
    runtime_http, provider_stream, monkeypatch, suffix
):
    runtime, client = runtime_http
    batches, _requests = provider_stream
    work = Path(runtime.cwd)
    outside = work.parent / f"{work.name}-outside.txt"
    denied = work.parent / f"{work.name}-denied.txt"
    requested = threading.Event()
    release = threading.Event()
    respond = httpx.AsyncHTTPTransport.handle_async_request

    async def gate_first_request(transport, request):
        if not requested.is_set():
            requested.set()
            assert await asyncio.to_thread(release.wait, 5)
        return await respond(transport, request)

    monkeypatch.setattr(httpx.AsyncHTTPTransport, "handle_async_request", gate_first_request)
    _queue_tool_turn(
        batches, "react", [("outside-write", "write_file", {"path": str(outside), "content": "ok"})]
    )
    sid = client.post("/v1/sessions").json()["id"]
    base = f"/v1/sessions/{sid}"
    assert client.post(f"{base}/permissions", json={"mode": "auto"}).json() == {"mode": "auto"}

    with ThreadPoolExecutor(max_workers=1) as pool:
        request = pool.submit(client.post, f"{base}/{suffix}", json={"message": "Write outside"})
        try:
            assert requested.wait(3)
            assert client.post(f"{base}/permissions", json={"mode": "default"}).json() == {
                "mode": "default"
            }
        finally:
            release.set()
        assert request.result(timeout=5).status_code == (200 if suffix == "turns" else 202)

    _wait_for_session_page(
        client, base, lambda page: page["state"]["status"] == "completed" and not page["active_run"]
    )
    assert outside.read_text(encoding="utf-8") == "ok"

    _queue_tool_turn(
        batches, "react", [("default-write", "write_file", {"path": str(denied), "content": "no"})]
    )
    response = client.post(
        f"{base}/turns/async", json={"message": "Write in Default", "interactive_approval": True}
    )
    assert response.status_code == 202
    page = _wait_for_session_page(
        client,
        base,
        lambda page: any(event["type"] == "approval/started" for event in page["events"]),
    )
    approval = next(event for event in page["events"] if event["type"] == "approval/started")
    response = client.post(
        f"{base}/approvals/{approval['approval_id']}/decision", json={"decision": "approve"}
    )
    assert response.status_code == 200
    _wait_for_session_page(
        client, base, lambda page: page["state"]["status"] == "completed" and not page["active_run"]
    )
    assert not denied.exists()


def _queue_tool_turn(batches, mode, *call_batches):
    if mode == "plan":
        plan = json.dumps({"tasks": [{"id": "tool-task", "description": "Run the tools"}]})
        batches.append([{"choices": [{"delta": {"content": plan}, "finish_reason": "stop"}]}])
    for calls in call_batches:
        tool_calls = [
            {
                "index": index,
                "id": call_id,
                "function": {"name": name, "arguments": json.dumps(arguments)},
            }
            for index, (call_id, name, arguments) in enumerate(calls)
        ]
        batches.append(
            [{"choices": [{"delta": {"tool_calls": tool_calls}, "finish_reason": "tool_calls"}]}]
        )
    batches.append([{"choices": [{"delta": {"content": "Done"}, "finish_reason": "stop"}]}])


@pytest.mark.parametrize("mode", ["react", "plan"])
@pytest.mark.parametrize("decision", ["approve", "deny"])
def test_http_switch_full_access_applies_after_pending_default_turn(
    runtime_http, provider_stream, mode, decision
):
    runtime, client = runtime_http
    batches, _requests = provider_stream
    work = Path(runtime.cwd)
    pending_file = work / "pending.txt"
    later_file = work / "later.txt"
    outside = work.parent / f"{work.name}-outside.txt"
    command = "echo full-access-check"
    runtime.config.policy.command_blacklist = [command]

    # The same turn must also use Default for tools requested after the change.
    _queue_tool_turn(
        batches,
        mode,
        [("pending-write", "write_file", {"path": pending_file.name, "content": "ok"})],
        [("later-write", "write_file", {"path": later_file.name, "content": "no"})],
    )

    sid = client.post("/v1/sessions").json()["id"]
    base = f"/v1/sessions/{sid}"
    response = client.post(
        f"{base}/turns/async",
        json={"message": "Write in Default", "mode": mode, "interactive_approval": True},
    )
    assert response.status_code == 202
    page = _wait_for_session_page(
        client,
        base,
        lambda page: any(event["type"] == "approval/started" for event in page["events"]),
    )
    approval = next(event for event in page["events"] if event["type"] == "approval/started")
    with client.stream("GET", f"{base}/events/stream", params={"view": "conversation"}) as stream:
        frames = _event_stream_pages(stream)
        assert next(frames)["permission_mode"] == "default"
        assert client.post(f"{base}/permissions", json={"mode": "auto"}).json() == {"mode": "auto"}
        assert any(next(frames)["permission_mode"] == "auto" for _ in range(5))
    assert not pending_file.exists()
    response = client.post(
        f"{base}/approvals/{approval['approval_id']}/decision", json={"decision": decision}
    )
    assert response.status_code == 200

    page = _wait_for_session_page(
        client,
        base,
        lambda page: sum(event["type"] == "approval/started" for event in page["events"]) == 2,
    )
    later_approval = [event for event in page["events"] if event["type"] == "approval/started"][-1]
    assert later_approval["tool_call_id"] == "later-write"
    response = client.post(
        f"{base}/approvals/{later_approval['approval_id']}/decision", json={"decision": "deny"}
    )
    assert response.status_code == 200
    page = _wait_for_session_page(
        client, base, lambda page: page["state"]["status"] == "completed" and not page["active_run"]
    )
    assert pending_file.exists() == (decision == "approve")
    assert not later_file.exists()
    resolved = "approval/approved" if decision == "approve" else "approval/rejected"
    assert any(
        event["type"] == resolved and event["approval_id"] == approval["approval_id"]
        for event in page["events"]
    )

    _queue_tool_turn(
        batches,
        mode,
        [
            ("outside-write", "write_file", {"path": str(outside), "content": "allowed"}),
            ("command", "bash", {"command": command}),
        ],
    )
    response = client.post(f"{base}/turns", json={"message": "Write in Full Access", "mode": mode})
    assert response.status_code == 200, response.text
    assert outside.read_text(encoding="utf-8") == "allowed"
    if mode == "react":
        results = client.get(f"{base}/events.json").json()["events"]
        command_result = next(
            event
            for event in results
            if event["type"] == "tool/result" and event["tool_call_id"] == "command"
        )
        assert command_result["payload"]["is_error"] is False


def test_http_conversation_stream_skips_diagnostics_and_resumes_the_committed_cursor(runtime_http):
    runtime, client = runtime_http
    sid = client.post("/v1/sessions").json()["id"]
    store = runtime.session_store
    store.append(sid, "turn/started", {}, turn_id="turn-1")
    store.append(sid, "user/message", {"content": "hello"}, turn_id="turn-1")
    store.append(sid, "tool/schema_snapshot", {"schema": "large diagnostic"})
    store.append(sid, "step/started", {}, turn_id="turn-1", step_id="step-1")
    store.append(
        sid, "approval/started", {"command": "write"}, turn_id="turn-1", approval_id="approval-1"
    )
    tail = store.append(sid, "request/prepared", {"diagnostics": "large request"})
    path = f"/v1/sessions/{sid}/events/stream"

    with client.stream("GET", path, params={"view": "conversation"}) as response:
        page = next(_event_stream_pages(response))
        assert [event["type"] for event in page["events"]] == [
            "session/created",
            "turn/started",
            "user/message",
            "step/started",
            "approval/started",
        ]
        assert page["events"][-1]["approval_id"] == "approval-1"
        assert page["state"]["active_step_id"] == "step-1"
        assert page["next_cursor"] == page["state"]["last_sequence"] == tail.sequence

    with client.stream("GET", path) as response:
        page = next(_event_stream_pages(response))
        assert "tool/schema_snapshot" in [event["type"] for event in page["events"]]

    with client.stream(
        "GET", path, params={"view": "conversation", "cursor": tail.sequence}
    ) as response:
        frames = _event_stream_pages(response)
        resumed = next(frames)
        assert resumed["events"] == []
        assert resumed["state"]["active_turn_id"] == "turn-1"
        assert resumed["state"]["active_step_id"] == "step-1"
        hidden = store.append(sid, "request/prepared", {})
        assert next(frames)["next_cursor"] == hidden.sequence
        answer = store.append(sid, "assistant/message", {"content": "answer"}, turn_id="turn-1")
        next_page = next(frames)
        assert [event["sequence"] for event in next_page["events"]] == [answer.sequence]


def _event_stream_pages(response):
    assert response.status_code == 200
    assert response.headers["content-type"].startswith("text/event-stream")
    lines = response.iter_lines()
    for line in lines:
        if line == "event: page":
            data = next(lines)
            assert data.startswith("data: ")
            assert next(lines) == ""
            yield json.loads(data.removeprefix("data: "))


def test_http_subagent_overview_and_read_only_transcripts(runtime_http, monkeypatch, tmp_path):
    runtime, client = runtime_http
    runtime.config.features.skill = False
    release = threading.Event()

    def chunk(*, text=None, calls=None, reason=None):
        delta = {"content": text} if calls is None else {"tool_calls": calls}
        choice = {"delta": delta, "finish_reason": reason}
        return f"data: {json.dumps({'choices': [choice]})}\n\n".encode()

    def call(index, call_id, name, arguments):
        return {
            "index": index,
            "id": call_id,
            "function": {"name": name, "arguments": json.dumps(arguments)},
        }

    def response(**values):
        return httpx.Response(200, content=chunk(**values) + b"data: [DONE]\n\n")

    class SearchStream(httpx.AsyncByteStream):
        async def __aiter__(self):
            yield chunk(text="Search progress synthetic-runtime-llm-cred")
            assert await asyncio.to_thread(release.wait, 10)
            yield chunk(text="ential and result", reason="stop") + b"data: [DONE]\n\n"

    async def respond(_transport, request):
        messages = json.loads(request.content)["messages"]
        question = next(message["content"] for message in messages if message["role"] == "user")
        if question == "Search task":
            return httpx.Response(200, stream=SearchStream())
        if question == "Write task":
            if messages[-1]["role"] == "tool":
                return response(text="Write result", reason="stop")
            return response(
                calls=[
                    call(0, "child-write", "write_file", {"path": "child.txt", "content": "ok"})
                ],
                reason="tool_calls",
            )
        if messages[-1]["role"] == "tool":
            return response(text="Initial draft", reason="stop")
        if any("Child agent results:" in (message.get("content") or "") for message in messages):
            return response(text="Final synthesis", reason="stop")
        return response(
            calls=[
                call(
                    0,
                    "spawn-search",
                    "subagent",
                    {"description": "Search", "prompt": "Search task", "role": "researcher"},
                ),
                call(
                    1,
                    "spawn-write",
                    "subagent",
                    {"description": "Write", "prompt": "Write task", "role": "builder"},
                ),
            ],
            reason="tool_calls",
        )

    monkeypatch.setattr(httpx.AsyncHTTPTransport, "handle_async_request", respond)
    root_id = client.post("/v1/sessions").json()["id"]
    root = f"/v1/sessions/{root_id}"
    stream = f"{root}/events/stream?view=conversation"
    try:
        receipt = client.post(
            f"{root}/turns/async", json={"message": "My question", "interactive_approval": True}
        )
        assert receipt.status_code == 202
        raw = _wait_for_session_page(
            client, root, lambda page: any(e["type"] == "approval/started" for e in page["events"])
        )
        approval = next(e for e in raw["events"] if e["type"] == "approval/started")
        with client.stream("GET", stream) as opened:
            page = next(_event_stream_pages(opened))
        search, write = page["subagents"]
        assert [search["role"], write["role"]] == ["researcher", "builder"]
        assert [search["tool_call_id"], write["tool_call_id"]] == ["spawn-search", "spawn-write"]
        assert search["parent_turn_id"] == receipt.json()["turn_id"]
        assert search["parent_step_id"] == write["parent_step_id"]
        assert search["status"] == "running"
        assert write["status"] == "approval"
        assert approval["payload"]["agent_session_id"] == write["session_id"]
        assert all(e["payload"].get("content") != "Search task" for e in page["events"])

        child_stream = f"{root}/subagents/{search['session_id']}/events/stream"
        unrelated = client.post("/v1/sessions").json()["id"]
        assert client.get(child_stream.replace(root_id, unrelated, 1)).status_code == 404
        assert client.post(child_stream.replace("/events/stream", "/turns")).status_code == 404
        direct_child = f"/v1/sessions/{search['session_id']}"
        for suffix in ("turns", "turns/async"):
            assert (
                client.post(f"{direct_child}/{suffix}", json={"message": "extra"}).status_code
                == 404
            )
        assert client.get(f"{direct_child}/events.json").status_code == 404
        with client.stream("GET", child_stream) as opened:
            frames = _event_stream_pages(opened)
            child_page = next(frames)
            if child_page["assistant_preview"] is None:
                child_page = next(frames)
            assert child_page["assistant_preview"]["text"] == "Search progress "
            assert child_page["session_id"] == search["session_id"]
            assert child_page["subagent"]["status"] == "running"
            assert child_page["subagent"]["role"] == "researcher"

        assert (
            client.post(
                f"{root}/approvals/{approval['approval_id']}/decision", json={"decision": "approve"}
            ).status_code
            == 200
        )
        release.set()
        _wait_for_session_page(
            client,
            root,
            lambda page: page["state"]["status"] == "completed" and not page["active_run"],
        )
        assert (tmp_path / "child.txt").read_text(encoding="utf-8") == "ok"
        with client.stream("GET", stream) as opened:
            completed = next(_event_stream_pages(opened))
        assert [(child["status"], child["result_count"]) for child in completed["subagents"]] == [
            ("completed", 1),
            ("completed", 1),
        ]
        assert [
            e["payload"]["content"] for e in completed["events"] if e["type"] == "user/message"
        ] == ["My question"]
        markers = [e for e in completed["events"] if e["type"] == "agent/results_received"]
        assert len({i for e in markers for i in e["payload"]["delivery_ids"]}) == 2
        assert all("content" not in e["payload"] for e in markers)
        assert "Write result" not in json.dumps(completed)
        assert any(e["payload"].get("content") == "Final synthesis" for e in completed["events"])
        with client.stream(
            "GET", child_stream, params={"cursor": child_page["next_cursor"]}
        ) as opened:
            child_replay = next(_event_stream_pages(opened))
        assert child_replay["subagent"]["status"] == "completed"
        assert "Search progress" in child_replay["subagent"]["report"]
        assert child_replay["assistant_preview"] is None
        assert [
            e["payload"]["content"]
            for e in child_replay["events"]
            if e["type"] == "assistant/message"
        ] == ["Search progress [redacted] and result"]
        with client.stream(
            "GET", stream, params={"view": "conversation", "cursor": completed["next_cursor"]}
        ) as opened:
            resumed = next(_event_stream_pages(opened))
        assert resumed["events"] == []
        assert resumed["subagents"] == completed["subagents"]
    finally:
        release.set()


@pytest.mark.parametrize("params", [{}, {"view": "conversation"}])
def test_http_event_stream_pushes_preview_and_resumes_history(runtime_http, monkeypatch, params):
    _runtime, client = runtime_http
    release_next = threading.Event()
    release_finish = threading.Event()

    def chunk(text, reason=None):
        choice = {"delta": {"content": text}, "finish_reason": reason}
        return f"data: {json.dumps({'choices': [choice]})}\n\n".encode()

    class GatedStream(httpx.AsyncByteStream):
        async def __aiter__(self):
            yield chunk("First part synthetic-runtime-llm-cred")
            assert await asyncio.to_thread(release_next.wait, 5)
            yield chunk("ential and second part\n-----BEGIN PRIVATE KEY-----\nprivate-material")
            assert await asyncio.to_thread(release_finish.wait, 5)
            yield chunk("\n-----END PRIVATE KEY-----\nFinal part", "stop") + b"data: [DONE]\n\n"

    async def respond(_transport, _request):
        return httpx.Response(200, stream=GatedStream())

    monkeypatch.setattr(httpx.AsyncHTTPTransport, "handle_async_request", respond)
    sid = client.post("/v1/sessions").json()["id"]
    session_url = f"/v1/sessions/{sid}"

    try:
        with client.stream("GET", f"{session_url}/events/stream", params=params) as response:
            frames = _event_stream_pages(response)
            initial = next(frames)
            assert [event["type"] for event in initial["events"]] == ["session/created"]
            assert initial["assistant_preview"] is None
            assert (
                client.post(f"{session_url}/turns/async", json={"message": "hello"}).status_code
                == 202
            )
            first = next(page for page in frames if page["assistant_preview"])
            assert first["state"]["status"] == "running"
            assert first["assistant_preview"]["text"] == "First part "
            assert not any(event["type"] == "assistant/message" for event in first["events"])

        with client.stream(
            "GET", f"{session_url}/events/stream", params={**params, "cursor": first["next_cursor"]}
        ) as response:
            frames = _event_stream_pages(response)
            resumed = next(frames)
            assert resumed["events"] == []
            assert resumed["next_cursor"] == first["next_cursor"]
            assert resumed["assistant_preview"] == first["assistant_preview"]

            release_next.set()
            second = next(frames)
            assert second["events"] == []
            assert second["next_cursor"] == resumed["next_cursor"]
            assert (
                second["assistant_preview"]["text"]
                == "First part [redacted] and second part\n[redacted]"
            )

            release_finish.set()
            events = []
            for page in frames:
                events.extend(page["events"])
                if any(event["type"] == "turn/completed" for event in page["events"]):
                    assert page["assistant_preview"] is None
                    assert page["state"]["status"] == "completed"
                    break
            else:
                pytest.fail("SSE closed before the terminal event")
            assert [
                e["payload"]["content"] for e in events if e["type"] == "assistant/message"
            ] == ["First part [redacted] and second part\n[redacted]\nFinal part"]
    finally:
        release_next.set()
        release_finish.set()


@pytest.mark.parametrize("failure", [False, True])
def test_background_turn_records_completion_or_failure(tmp_path, config, monkeypatch, failure):
    runtime = RuntimeApiServer(cwd=str(tmp_path), config=config, api_key="synthetic", workers=0)
    completed = threading.Event()
    threads = []
    original_thread = threading.Thread

    def capture_thread(*args, **kwargs):
        thread = original_thread(*args, **kwargs)
        threads.append(thread)
        return thread

    async def run_turn(session_id, message, *, mode, interactive_approval, config, turn_id):
        assert interactive_approval is False
        assert config.policy.path_guard_enabled
        completed.set()
        if failure:
            runtime.session_store.append(session_id, "turn/failed", {}, turn_id=turn_id)
            raise RuntimeError("provider failed")
        runtime.session_store.append(session_id, "turn/completed", {}, turn_id=turn_id)
        return {"session_id": session_id, "text": message, "mode": mode}

    monkeypatch.setattr(runtime, "_run_turn", run_turn)
    monkeypatch.setattr("paicli.runtime.api.threading.Thread", capture_thread)
    session_id = runtime._create_session()
    run_id, _after_sequence = runtime._start_background_turn(session_id, "hello", mode="react")
    assert completed.wait(timeout=5)
    threads[0].join(timeout=5)
    assert not threads[0].is_alive()
    record = runtime._background_turns[run_id]
    assert record["status"] == ("failed" if failure else "completed")
    assert "finished_at" in record
    if failure:
        assert record["error"] == "RuntimeError"
    assert "result" not in record
