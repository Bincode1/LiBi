from __future__ import annotations

import asyncio
import json
import os
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
from paicli.session import SessionStore, default_runtime_db_path


class TaskCanceledError(RuntimeError):
    pass


class RuntimeApiServer:
    """HTTP runtime backed directly by the canonical SessionStore event log."""

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
        self.db_path = default_runtime_db_path()
        self.task_manager = DurableTaskManager(
            Path.home() / ".paicli" / "tasks" / "tasks.db", scope=self.cwd
        )
        self.workers = workers
        self._stop = threading.Event()
        self._background_turns: dict[str, dict[str, Any]] = {}
        self._background_turns_lock = threading.Lock()
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
        try:
            body = _read_json(request)
        except ValueError as exc:
            _send_json(request, 400, {"error": str(exc)})
            return
        try:
            if request.command == "POST" and path == "/v1/sessions":
                session_id = self._create_session()
                _send_json(request, 200, {"id": session_id})
            elif request.command == "GET" and path == "/v1/sessions":
                _send_json(request, 200, {"sessions": self._list_sessions()})
            elif request.command == "GET" and path == "/v1/health":
                _send_json(request, 200, {"status": "ok", "cwd": self.cwd})
            elif request.command == "POST" and _matches_session_route(path, "/turns"):
                session_id = _session_id_from_path(path)
                message = str(body.get("message") or body.get("prompt") or "")
                if not message:
                    _send_json(request, 400, {"error": "message is required"})
                    return
                result = asyncio.run(
                    self._run_turn(session_id, message, mode=_mode_from_body(body))
                )
                _send_json(request, 200, result)
            elif request.command == "POST" and _matches_session_route(path, "/turns/async"):
                session_id = _session_id_from_path(path)
                message = str(body.get("message") or body.get("prompt") or "")
                if not message:
                    _send_json(request, 400, {"error": "message is required"})
                    return
                mode = _mode_from_body(body)
                run_id = self._start_background_turn(session_id, message, mode=mode)
                _send_json(
                    request,
                    202,
                    {"run_id": run_id, "session_id": session_id, "mode": mode},
                )
            elif request.command == "GET" and _matches_session_route(path, "/events.json"):
                session_id = _session_id_from_path(path)
                query_cursor = parse_qs(request.path.partition("?")[2]).get("cursor", ["0"])[0]
                _send_json(request, 200, self._events_json(session_id, _parse_cursor(query_cursor)))
            elif request.command == "GET" and _matches_session_route(path, "/events"):
                session_id = _session_id_from_path(path)
                query_cursor = parse_qs(request.path.partition("?")[2]).get("cursor", [None])[0]
                cursor = query_cursor or request.headers.get("last-event-id") or "0"
                self._send_events(request, session_id, after_sequence=_parse_cursor(cursor))
            elif request.command == "POST" and path == "/v1/tasks":
                prompt = str(body.get("message") or body.get("prompt") or "")
                if not prompt:
                    _send_json(request, 400, {"error": "message is required"})
                    return
                mode = _mode_from_body(body)
                task_id = self.task_manager.add(prompt, mode=mode)
                _send_json(request, 200, {"id": task_id, "status": "queued", "mode": mode})
            elif request.command == "GET" and path == "/v1/tasks":
                _send_json(request, 200, {"tasks": [asdict(t) for t in self.task_manager.list()]})
            elif request.command == "GET" and path.startswith("/v1/tasks/"):
                task = self.task_manager.get(path.split("/")[3])
                payload = asdict(task) if task else {"error": "not found"}
                _send_json(request, 200 if task else 404, payload)
            elif request.command == "POST" and path.startswith("/v1/tasks/") and path.endswith(
                "/cancel"
            ):
                _send_json(request, 200, {"canceled": self.task_manager.cancel(path.split("/")[3])})
            else:
                _send_json(request, 404, {"error": "not found"})
        except ValueError as exc:
            _send_json(request, 400, {"error": str(exc)})
        except Exception as exc:  # noqa: BLE001 - API boundary
            _send_json(request, 500, {"error": str(exc)})

    async def _run_turn(
        self, session_id: str, message: str, *, mode: str = "react"
    ) -> dict[str, Any]:
        self._ensure_llm_key()
        mode = _validate_mode(mode)
        if not self.session_store.session_exists(session_id):
            raise ValueError(f"session not found: {session_id}")
        turn_boundary = self.session_store.state(session_id).last_sequence
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
                session_id=session_id,
            )
            events = _engine_events(engine, mode, message)
            async with aclosing(events):
                async for event in events:
                    event_type = str(event.get("type"))
                    if event_type == "text_delta":
                        text += str(event.get("text") or "")
                    elif event_type == "done":
                        total_tokens = int(event.get("total_tokens") or 0)
                    elif event_type == "error":
                        raise event["error"]
        except asyncio.CancelledError:
            self._close_unowned_turn(
                session_id,
                mode=mode,
                status="turn/canceled",
                detail={"mode": mode},
                after_sequence=turn_boundary,
            )
            raise
        except Exception as exc:
            self._close_unowned_turn(
                session_id,
                mode=mode,
                status="turn/failed",
                detail={"error_type": type(exc).__name__},
                after_sequence=turn_boundary,
            )
            raise
        finally:
            if engine is not None:
                await engine.aclose()
        return {
            "session_id": session_id,
            "text": text,
            "mode": mode,
            "total_tokens": total_tokens,
        }

    def _start_background_turn(self, session_id: str, message: str, *, mode: str) -> str:
        run_id = f"run_{uuid.uuid4().hex}"
        with self._background_turns_lock:
            self._background_turns[run_id] = {
                "run_id": run_id,
                "session_id": session_id,
                "mode": mode,
                "status": "running",
                "started_at": datetime.now(UTC).isoformat(),
            }

        def runner() -> None:
            try:
                result = asyncio.run(self._run_turn(session_id, message, mode=mode))
                outcome = {"status": "completed", "result": result}
            except Exception as exc:  # noqa: BLE001 - background task boundary
                outcome = {"status": "failed", "error": type(exc).__name__}
            with self._background_turns_lock:
                record = self._background_turns.get(run_id)
                if record is not None:
                    record.update(outcome, finished_at=datetime.now(UTC).isoformat())

        threading.Thread(target=runner, name=f"paicli-turn-{run_id}", daemon=True).start()
        return run_id

    def _create_session(self) -> str:
        session_id = self.session_store.create_session()
        self.session_store.append(
            session_id, "session/created", {"id": session_id}, source="runtime"
        )
        return session_id

    def _list_sessions(self) -> list[dict[str, Any]]:
        return [
            {
                "id": item.session_id,
                "created_at": item.created_at,
                "updated_at": item.updated_at,
                "status": item.status,
                "last_sequence": item.last_sequence,
            }
            for item in self.session_store.list_sessions(limit=100)
        ]

    def _events_json(self, session_id: str, after_sequence: int) -> dict[str, Any]:
        events = self.session_store.events(session_id, after_sequence=after_sequence)
        state = self.session_store.state(session_id)
        return {
            "session_id": session_id,
            "events": [self._serialize_event(event) for event in events],
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

    def _send_events(
        self,
        request: BaseHTTPRequestHandler,
        session_id: str,
        *,
        after_sequence: int = 0,
    ) -> None:
        events = self.session_store.events(session_id, after_sequence=after_sequence)
        body = "".join(
            f"id: {event.sequence}\nevent: {event.event_type}\n"
            f"data: {json.dumps(event.payload, ensure_ascii=False)}\n\n"
            for event in events
        ).encode("utf-8")
        request.send_response(200)
        request.send_header("content-type", "text/event-stream")
        request.send_header("content-length", str(len(body)))
        request.end_headers()
        request.wfile.write(body)

    def _close_unowned_turn(
        self,
        session_id: str,
        *,
        mode: str,
        status: str,
        detail: dict[str, Any],
        after_sequence: int,
    ) -> None:
        events = self.session_store.events(session_id)
        if any(
            event.sequence > after_sequence and event.event_type == "turn/started"
            for event in events
        ):
            return
        state = self.session_store.state(session_id)
        turn_id = f"turn_{uuid.uuid4().hex}"
        if state.active_turn_id is not None:
            self.session_store.append(
                session_id,
                status,
                detail,
                turn_id=state.active_turn_id,
                source="runtime",
            )
            return
        self.session_store.append(
            session_id,
            "turn/started",
            {"mode": mode, "synthetic": True},
            turn_id=turn_id,
            source="runtime",
        )
        self.session_store.append(session_id, status, detail, turn_id=turn_id, source="runtime")

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
                pass
            except Exception as exc:  # noqa: BLE001
                self.task_manager.fail(task.id, str(exc), worker_id=worker_id)
            finally:
                heartbeat_stop.set()
                heartbeat.join(timeout=1)

    def _heartbeat_loop(self, task_id: str, worker_id: str, stop: threading.Event) -> None:
        while not stop.wait(30):
            if not self.task_manager.heartbeat(task_id, worker_id, lease_seconds=300):
                return

    async def _run_task(self, task: TaskRecord) -> str:
        if self.task_manager.is_canceled(task.id):
            raise TaskCanceledError(task.id)
        if task.mode not in TASK_MODES:
            raise ValueError(
                f"task mode {task.mode!r} is no longer supported; recreate it as react or plan"
            )
        self._ensure_llm_key()
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


def _matches_session_route(path: str, suffix: str) -> bool:
    return path.startswith("/v1/sessions/") and path.endswith(suffix)


def _session_id_from_path(path: str) -> str:
    parts = path.split("/")
    if len(parts) < 4 or not parts[3]:
        raise ValueError("session id is required")
    return parts[3]


def _parse_cursor(value: str | None) -> int:
    try:
        return max(0, int(value or "0"))
    except ValueError as exc:
        raise ValueError("cursor must be a non-negative integer") from exc


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


def _engine_events(engine: QueryEngine, mode: str, message: str):
    if mode == "react":
        return engine.ask(message)
    if mode == "plan":
        return engine.plan(message)
    raise ValueError(f"mode must be one of: {', '.join(sorted(TASK_MODES))}")
