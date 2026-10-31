from __future__ import annotations

import asyncio
import json
import os
import sqlite3
import threading
import uuid
from contextlib import aclosing
from dataclasses import asdict
from datetime import UTC, datetime
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any
from urllib.parse import parse_qs

from paicli.agent import QueryEngine
from paicli.bootstrap import build_tool_registry
from paicli.config import PaiCliConfig
from paicli.llm import create_llm_client
from paicli.runtime.tasks import TASK_MODES, DurableTaskManager, TaskRecord
from paicli.session import ContextProjector, SessionStore
from paicli.types import Message


class TaskCanceledError(RuntimeError):
    pass


class RuntimeApiServer:
    def __init__(
        self,
        *,
        cwd: str,
        config: PaiCliConfig,
        api_key: str,
        port: int = 8080,
        workers: int = 2,
    ):
        self.cwd = str(Path(cwd).resolve())
        self.config = config
        self.api_key = api_key
        self.port = port
        self.db_path = Path.home() / ".paicli" / "runtime" / "runtime.db"
        self.db_path.parent.mkdir(parents=True, exist_ok=True)
        self.task_manager = DurableTaskManager(
            Path.home() / ".paicli" / "tasks" / "tasks.db", scope=self.cwd
        )
        self.workers = workers
        self._stop = threading.Event()
        self._background_turns: dict[str, dict[str, Any]] = {}
        self._background_turns_lock = threading.Lock()
        self._ensure_schema()
        self.session_store = SessionStore(
            self.db_path, sensitive_values=(api_key, config.llm.api_key)
        )

    def serve_forever(self) -> None:
        self._start_workers()

        outer = self

        class Handler(BaseHTTPRequestHandler):
            def do_POST(self) -> None:  # noqa: N802
                outer._handle(self)

            def do_GET(self) -> None:  # noqa: N802
                outer._handle(self)

            def log_message(self, _format: str, *args: Any) -> None:
                return

        server = ThreadingHTTPServer(("127.0.0.1", self.port), Handler)
        print(f"PaiCLI Runtime API listening on http://127.0.0.1:{self.port}", flush=True)
        try:
            server.serve_forever()
        finally:
            self._stop.set()

    def work_forever(self) -> None:
        """Run only durable task workers, without exposing the HTTP API."""

        self._start_workers()
        print(f"PaiCLI task worker running with {self.workers} workers", flush=True)
        try:
            while not self._stop.wait(0.5):
                pass
        except KeyboardInterrupt:
            self._stop.set()

    def _start_workers(self) -> None:
        self.task_manager.requeue_expired()
        for index in range(self.workers):
            worker_id = f"{os.getpid()}-{index}"
            thread = threading.Thread(
                target=self._worker_loop,
                args=(worker_id,),
                name=f"paicli-task-{index}",
                daemon=True,
            )
            thread.start()

    def _handle(self, request: BaseHTTPRequestHandler) -> None:
        path = request.path.split("?", 1)[0]
        if not self._authorized(request):
            _send_json(request, 401, {"error": "unauthorized"})
            return
        method = request.command
        try:
            body = _read_json(request)
        except ValueError as exc:
            _send_json(request, 400, {"error": str(exc)})
            return
        try:
            if method == "POST" and path == "/v1/threads":
                thread_id = self._create_thread()
                _send_json(request, 200, {"id": thread_id})
            elif method == "GET" and path == "/v1/threads":
                _send_json(request, 200, {"threads": self._list_threads()})
            elif method == "GET" and path == "/v1/health":
                _send_json(request, 200, {"status": "ok", "cwd": self.cwd})
            elif method == "POST" and path.startswith("/v1/threads/") and path.endswith("/turns"):
                thread_id = path.split("/")[3]
                message = str(body.get("message") or body.get("prompt") or "")
                if not message:
                    _send_json(request, 400, {"error": "message is required"})
                    return
                mode = _mode_from_body(body)
                result = asyncio.run(self._run_turn(thread_id, message, mode=mode))
                _send_json(request, 200, result)
            elif (
                method == "POST"
                and path.startswith("/v1/threads/")
                and path.endswith("/turns/async")
            ):
                thread_id = path.split("/")[3]
                message = str(body.get("message") or body.get("prompt") or "")
                if not message:
                    _send_json(request, 400, {"error": "message is required"})
                    return
                mode = _mode_from_body(body)
                run_id = self._start_background_turn(thread_id, message, mode=mode)
                _send_json(request, 202, {"run_id": run_id, "thread_id": thread_id, "mode": mode})
            elif (
                method == "GET"
                and path.startswith("/v1/threads/")
                and path.endswith("/events.json")
            ):
                thread_id = path.split("/")[3]
                query_cursor = parse_qs(request.path.partition("?")[2]).get("cursor", ["0"])[0]
                try:
                    after_sequence = max(0, int(query_cursor))
                except ValueError as exc:
                    raise ValueError("cursor must be a non-negative integer") from exc
                _send_json(request, 200, self._events_json(thread_id, after_sequence))
            elif method == "GET" and path.startswith("/v1/threads/") and path.endswith("/events"):
                thread_id = path.split("/")[3]
                query_cursor = parse_qs(request.path.partition("?")[2]).get("cursor", [None])[0]
                cursor = query_cursor or request.headers.get("last-event-id") or "0"
                try:
                    after_sequence = max(0, int(cursor))
                except ValueError as exc:
                    raise ValueError("cursor must be a non-negative integer") from exc
                self._send_events(request, thread_id, after_sequence=after_sequence)
            elif method == "POST" and path == "/v1/tasks":
                prompt = str(body.get("message") or body.get("prompt") or "")
                if not prompt:
                    _send_json(request, 400, {"error": "message is required"})
                    return
                mode = _mode_from_body(body)
                task_id = self.task_manager.add(prompt, mode=mode)
                _send_json(request, 200, {"id": task_id, "status": "queued", "mode": mode})
            elif method == "GET" and path == "/v1/tasks":
                _send_json(request, 200, {"tasks": [asdict(t) for t in self.task_manager.list()]})
            elif method == "GET" and path.startswith("/v1/tasks/"):
                task = self.task_manager.get(path.split("/")[3])
                payload = asdict(task) if task else {"error": "not found"}
                _send_json(request, 200 if task else 404, payload)
            elif method == "POST" and path.startswith("/v1/tasks/") and path.endswith("/cancel"):
                task_id = path.split("/")[3]
                _send_json(request, 200, {"canceled": self.task_manager.cancel(task_id)})
            else:
                _send_json(request, 404, {"error": "not found"})
        except ValueError as exc:
            _send_json(request, 400, {"error": str(exc)})
        except Exception as exc:  # noqa: BLE001 - API boundary
            _send_json(request, 500, {"error": str(exc)})

    async def _run_turn(
        self, thread_id: str, message: str, *, mode: str = "react"
    ) -> dict[str, Any]:
        self._ensure_llm_key()
        mode = _validate_mode(mode)
        turn_id = f"turn_{uuid.uuid4().hex}"
        self._append_event(thread_id, "turn.started", {"mode": mode})
        runtime_boundary = self.session_store.events(thread_id)[-1].sequence
        engine: QueryEngine | None = None
        text = ""
        total_tokens = 0
        try:
            registry, _manager = await build_tool_registry(config=self.config, cwd=self.cwd)
            engine = QueryEngine(
                llm_client=create_llm_client(self.config.llm),
                tool_registry=registry,
                config=self.config,
                cwd=self.cwd,
                session_store=self.session_store,
                session_id=thread_id,
            )
            history = self._thread_history(thread_id) if mode == "react" else []
            events = _engine_events(engine, mode, message, history=history)
            async with aclosing(events):
                async for event in events:
                    event_type = str(event.get("type"))
                    if event_type == "text_delta":
                        text += str(event.get("text") or "")
                    elif event_type in {"tool_call", "tool_result", "error", "done"}:
                        safe_event = _runtime_event_payload(event)
                        self._append_event(thread_id, event_type, safe_event)
                        if event_type == "error":
                            self._append_event(
                                thread_id,
                                "turn.failed",
                                {"error_type": safe_event.get("error_type", "RuntimeError")},
                            )
                            raise RuntimeError(
                                str(safe_event.get("error_type") or "Runtime turn failed")
                            )
                        if event_type == "done":
                            total_tokens = int(event.get("total_tokens") or 0)
        except asyncio.CancelledError:
            self._close_unowned_turn(
                thread_id,
                turn_id,
                mode=mode,
                status="turn/canceled",
                detail={"mode": mode},
                after_sequence=runtime_boundary,
            )
            raise
        except Exception as exc:
            self._close_unowned_turn(
                thread_id,
                turn_id,
                mode=mode,
                status="turn/failed",
                detail={"error_type": type(exc).__name__},
                after_sequence=runtime_boundary,
            )
            raise
        if engine is not None:
            try:
                await engine.aclose()
            except Exception:
                raise
        self._append_event(
            thread_id,
            "turn.completed",
            {"mode": mode, "total_tokens": total_tokens},
        )
        self._sync_thread_messages(thread_id)
        return {
            "thread_id": thread_id,
            "text": text,
            "mode": mode,
            "total_tokens": total_tokens,
        }

    def _start_background_turn(self, thread_id: str, message: str, *, mode: str) -> str:
        run_id = f"run_{uuid.uuid4().hex}"
        with self._background_turns_lock:
            self._background_turns[run_id] = {
                "run_id": run_id,
                "thread_id": thread_id,
                "mode": mode,
                "status": "running",
                "started_at": datetime.now(UTC).isoformat(),
            }

        def runner() -> None:
            try:
                result = asyncio.run(self._run_turn(thread_id, message, mode=mode))
                outcome = {"status": "completed", "result": result}
            except Exception as exc:  # noqa: BLE001 - background task boundary
                outcome = {"status": "failed", "error": type(exc).__name__}
            with self._background_turns_lock:
                record = self._background_turns.get(run_id)
                if record is not None:
                    record.update(outcome, finished_at=datetime.now(UTC).isoformat())

        threading.Thread(target=runner, name=f"paicli-turn-{run_id}", daemon=True).start()
        return run_id

    def _list_threads(self) -> list[dict[str, Any]]:
        with self._connect() as conn:
            rows = conn.execute(
                "select id, created_at from sessions order by created_at desc"
            ).fetchall()
            child_rows = conn.execute(
                "select payload from events where type = 'agent/subagent_catalog'"
            ).fetchall()
        child_sessions = set()
        for (payload,) in child_rows:
            try:
                child_id = json.loads(payload).get("child_session_id")
            except (json.JSONDecodeError, AttributeError):
                continue
            if child_id:
                child_sessions.add(str(child_id))
        result = []
        for thread_id, created_at in rows:
            if thread_id in child_sessions:
                continue
            state = self.session_store.state(thread_id)
            events = self.session_store.events(thread_id)
            prompt = next(
                (
                    str(event.payload.get("content") or "")
                    for event in events
                    if event.event_type == "user/message"
                ),
                "",
            )
            result.append(
                {
                    "id": thread_id,
                    "created_at": created_at,
                    "status": state.status,
                    "last_sequence": state.last_sequence,
                    "prompt": prompt[:160],
                }
            )
        return result[:100]

    def _events_json(self, thread_id: str, after_sequence: int) -> dict[str, Any]:
        parent_events = self.session_store.events(thread_id, after_sequence=after_sequence)
        all_parent_events = self.session_store.events(thread_id)
        child_sessions = {
            str(event.payload.get("child_session_id")): str(event.payload.get("child_id"))
            for event in all_parent_events
            if event.event_type == "agent/subagent_catalog"
            and event.payload.get("child_session_id")
            and event.payload.get("child_id")
        }
        child_events: list[tuple[str, str, Any]] = []
        for child_session, agent_id in child_sessions.items():
            child_events.extend(
                (child_session, agent_id, event)
                for event in self.session_store.events(child_session)
            )
        state = self.session_store.state(thread_id)
        serialized = [self._serialize_event(event) for event in parent_events]
        serialized.extend(
            {
                **self._serialize_event(event),
                "sequence": 0,
                "agent_session_id": child_session,
                "agent_id": agent_id,
                "source": f"child:{child_session}",
            }
            for child_session, agent_id, event in child_events
        )
        serialized.sort(key=lambda item: (item["created_at"], item["sequence"], item["id"]))
        return {
            "thread_id": thread_id,
            "events": serialized,
            "next_cursor": state.last_sequence,
            "state": asdict(state),
        }

    @staticmethod
    def _serialize_event(event: Any) -> dict[str, Any]:
        return {
            "id": event.event_id,
            "sequence": event.sequence,
            "type": event.event_type,
            "payload": event.payload,
            "created_at": event.created_at,
            "turn_id": event.turn_id,
            "step_id": event.step_id,
            "tool_call_id": event.tool_call_id,
            "source": event.source,
        }

    def _close_unowned_turn(
        self,
        thread_id: str,
        turn_id: str,
        *,
        mode: str,
        status: str,
        detail: dict[str, Any],
        after_sequence: int,
    ) -> None:
        if any(
            event.event_type == "turn/started"
            for event in self.session_store.events(thread_id)
            if event.sequence > after_sequence
        ):
            return
        state = self.session_store.state(thread_id)
        if state.active_turn_id is not None:
            if state.active_turn_id == turn_id:
                self.session_store.append(
                    thread_id,
                    status,
                    detail,
                    turn_id=turn_id,
                    source="runtime-recovery",
                )
            return
        self.session_store.append(
            thread_id,
            "turn/started",
            {"mode": mode, "synthetic": True},
            turn_id=turn_id,
            source="runtime-recovery",
        )
        self.session_store.append(
            thread_id,
            status,
            detail,
            turn_id=turn_id,
            source="runtime-recovery",
        )

    def _worker_loop(self, worker_id: str) -> None:
        while not self._stop.is_set():
            task = self.task_manager.claim_next(worker_id, lease_seconds=300)
            if not task:
                self._stop.wait(0.5)
                continue
            heartbeat_stop = threading.Event()
            heartbeat = threading.Thread(
                target=self._heartbeat_loop,
                args=(task.id, worker_id, heartbeat_stop),
                name=f"paicli-heartbeat-{task.id}",
                daemon=True,
            )
            heartbeat.start()
            try:
                result = asyncio.run(self._run_task(task))
                self.task_manager.complete(task.id, result, worker_id=worker_id)
            except TaskCanceledError:
                # cancel() owns the terminal state; never overwrite it from the worker.
                pass
            except Exception as exc:  # noqa: BLE001
                self.task_manager.fail(task.id, str(exc), worker_id=worker_id)
            finally:
                heartbeat_stop.set()
                heartbeat.join(timeout=1)

    def _heartbeat_loop(
        self,
        task_id: str,
        worker_id: str,
        stop: threading.Event,
    ) -> None:
        while not stop.wait(30):
            if not self.task_manager.heartbeat(task_id, worker_id, lease_seconds=300):
                return

    async def _run_task(self, task: TaskRecord) -> str:
        self._ensure_llm_key()
        if self.task_manager.is_canceled(task.id):
            raise TaskCanceledError(task.id)
        registry, _manager = await build_tool_registry(config=self.config, cwd=self.cwd)
        engine = QueryEngine(
            llm_client=create_llm_client(self.config.llm),
            tool_registry=registry,
            config=self.config,
            cwd=self.cwd,
        )
        text = ""
        try:
            events = _engine_events(engine, task.mode, task.prompt)
            async with aclosing(events):
                async for event in events:
                    if self.task_manager.is_canceled(task.id):
                        raise TaskCanceledError(task.id)
                    if event.get("type") == "text_delta":
                        text += str(event.get("text") or "")
                    elif event.get("type") == "error":
                        raise event["error"]
        finally:
            await engine.aclose()
        return text

    def _ensure_llm_key(self) -> None:
        if not self.config.llm.api_key:
            raise ValueError(
                "PAICLI_API_KEY is not configured. Runtime turns/tasks need a working LLM key."
            )

    def _authorized(self, request: BaseHTTPRequestHandler) -> bool:
        auth = request.headers.get("authorization", "")
        token = request.headers.get("x-api-key", "")
        return auth == f"Bearer {self.api_key}" or token == self.api_key

    def _create_thread(self) -> str:
        thread_id = f"thread_{datetime.now(UTC).strftime('%Y%m%d%H%M%S%f')}"
        with self._connect() as conn:
            conn.execute(
                "insert into threads(id, created_at) values (?, ?)",
                (thread_id, datetime.now(UTC).isoformat()),
            )
        self._append_event(thread_id, "thread.created", {"id": thread_id})
        self.session_store.create_session(thread_id)
        self.session_store.append(thread_id, "session/created", {"id": thread_id}, source="runtime")
        return thread_id

    def _append_event(self, thread_id: str, event_type: str, payload: dict[str, Any]) -> None:
        safe_payload = self.session_store.sanitize_payload(payload)
        self.session_store.append(
            thread_id,
            f"runtime/{event_type}",
            safe_payload,
            source="runtime-compat",
        )

    def _append_thread_message(self, thread_id: str, role: str, content: str) -> None:
        safe_content = (
            str(self.session_store.sanitize_payload({"content": content})["content"])
            if hasattr(self, "session_store")
            else content
        )
        with self._connect() as conn:
            conn.execute(
                """
                insert into messages(thread_id, role, content, created_at)
                values (?, ?, ?, ?)
                """,
                (thread_id, role, safe_content, datetime.now(UTC).isoformat()),
            )

    def _sync_thread_messages(self, thread_id: str) -> None:
        history = self._thread_history(thread_id)
        serialized = [
            (
                thread_id,
                item.role,
                item.content if isinstance(item.content, str) else json.dumps(item.content),
                datetime.now(UTC).isoformat(),
            )
            for item in history
        ]
        with self._connect() as conn:
            conn.execute("begin immediate")
            conn.execute("delete from messages where thread_id = ?", (thread_id,))
            conn.executemany(
                "insert into messages(thread_id, role, content, created_at) values (?, ?, ?, ?)",
                serialized,
            )

    def _thread_history(self, thread_id: str, limit: int = 100) -> list[Message]:
        if hasattr(self, "session_store"):
            events = self.session_store.events(thread_id)
            turns = [
                event.turn_id
                for event in events
                if event.event_type == "user/message" and event.turn_id is not None
            ]
            if turns:
                turn_id = next(
                    (
                        event.turn_id
                        for event in reversed(events)
                        if event.event_type == "turn/completed" and event.turn_id
                    ),
                    None,
                )
                if turn_id is None:
                    return []
                steps = [
                    event.step_id
                    for event in events
                    if event.event_type == "step/completed"
                    and event.turn_id == turn_id
                    and event.step_id
                ]
                if steps:
                    view = ContextProjector().project(
                        events,
                        session_id=thread_id,
                        turn_id=turn_id,
                        step_id=steps[-1],
                    )
                    return list(view.messages[-limit:])
        with self._connect() as conn:
            rows = conn.execute(
                """
                select role, content
                from messages
                where thread_id = ?
                order by id desc
                limit ?
                """,
                (thread_id, limit),
            ).fetchall()
        return [Message(role=role, content=content) for role, content in reversed(rows)]

    def _send_events(
        self,
        request: BaseHTTPRequestHandler,
        thread_id: str,
        *,
        after_sequence: int = 0,
    ) -> None:
        with self._connect() as conn:
            rows = conn.execute(
                """
                select id, sequence, type, payload, created_at
                from events
                where thread_id = ? and (sequence > ? or ? = 0)
                order by id
                """,
                (thread_id, after_sequence, after_sequence),
            ).fetchall()
        body = "".join(
            f"id: {sequence if sequence is not None else event_id}\nevent: {event_type}\n"
            f"data: {payload}\n\n"
            for event_id, sequence, event_type, payload, _created_at in rows
        ).encode("utf-8")
        request.send_response(200)
        request.send_header("content-type", "text/event-stream")
        request.send_header("content-length", str(len(body)))
        request.end_headers()
        request.wfile.write(body)

    def _ensure_schema(self) -> None:
        with self._connect() as conn:
            conn.execute(
                """
                create table if not exists threads (
                    id text primary key,
                    created_at text not null
                )
                """
            )
            conn.execute(
                """
                create table if not exists messages (
                    id integer primary key autoincrement,
                    thread_id text not null,
                    role text not null,
                    content text not null,
                    created_at text not null
                )
                """
            )
            conn.execute(
                "create index if not exists idx_messages_thread on messages(thread_id, id)"
            )
            conn.execute(
                """
                create table if not exists events (
                    id integer primary key autoincrement,
                    thread_id text not null,
                    type text not null,
                    payload text not null,
                    created_at text not null
                )
                """
            )
            columns = {row[1] for row in conn.execute("pragma table_info(events)")}
            for column, column_type in {
                "event_id": "text",
                "sequence": "integer",
                "turn_id": "text",
                "step_id": "text",
                "tool_call_id": "text",
                "approval_id": "text",
                "source": "text",
                "schema_version": "integer",
            }.items():
                if column not in columns:
                    conn.execute(f"alter table events add column {column} {column_type}")

    def _connect(self) -> sqlite3.Connection:
        return sqlite3.connect(self.db_path)


def _read_json(request: BaseHTTPRequestHandler) -> dict[str, Any]:
    length = int(request.headers.get("content-length") or 0)
    if length == 0:
        return {}
    try:
        value = json.loads(request.rfile.read(length).decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise ValueError("malformed JSON body") from exc
    if not isinstance(value, dict):
        raise ValueError("JSON body must be an object")
    return value


def _send_json(request: BaseHTTPRequestHandler, status: int, payload: dict[str, Any]) -> None:
    body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
    request.send_response(status)
    request.send_header("content-type", "application/json")
    request.send_header("content-length", str(len(body)))
    request.end_headers()
    request.wfile.write(body)


def _jsonable(event: dict[str, Any]) -> dict[str, Any]:
    result = {}
    for key, value in event.items():
        if isinstance(value, (str, int, float, bool)) or value is None:
            result[key] = value
        else:
            result[key] = str(value)
    return result


def _runtime_event_payload(event: dict[str, Any]) -> dict[str, Any]:
    event_type = str(event.get("type") or "unknown")
    if event_type == "error":
        return {"type": event_type, "error_type": type(event.get("error")).__name__}
    if event_type == "done":
        return {
            key: event[key]
            for key in ("type", "total_turns", "total_tokens", "usage", "session_id", "turn_id")
            if key in event
        }
    if event_type == "tool_call":
        return {
            "type": event_type,
            "name": event.get("name"),
            "input": event.get("input", {}),
            "tool_call_id": event.get("tool_call_id"),
        }
    return {
        "type": event_type,
        "name": event.get("name"),
        "result": event.get("result", ""),
        "is_error": bool(event.get("is_error")),
        "tool_call_id": event.get("tool_call_id"),
    }


def runtime_api_key(explicit: str | None = None) -> str:
    key = explicit or os.environ.get("PAICLI_RUNTIME_API_KEY")
    if not key:
        raise ValueError("PAICLI_RUNTIME_API_KEY is required for Runtime API")
    return key


def _mode_from_body(body: dict[str, Any]) -> str:
    return _validate_mode(str(body.get("mode") or "react"))


def _validate_mode(mode: str) -> str:
    value = mode.strip().lower()
    if value not in TASK_MODES:
        raise ValueError(f"mode must be one of: {', '.join(sorted(TASK_MODES))}")
    return value


def _engine_events(engine: QueryEngine, mode: str, message: str, *, history=None):
    if mode == "plan":
        return engine.plan(message)
    if mode == "team":
        return engine.team(message)
    return engine.ask(message, history=history)
