from __future__ import annotations

import asyncio
import json
import logging
import os
import re
import socket
import threading
import uuid
from collections import OrderedDict
from collections.abc import Iterator
from concurrent.futures import Future, ThreadPoolExecutor
from contextlib import aclosing, contextmanager, suppress
from copy import deepcopy
from dataclasses import asdict, dataclass, field, replace
from datetime import UTC, datetime, timedelta
from hmac import compare_digest
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from time import monotonic
from typing import Any
from urllib.parse import parse_qs

from libi.agent import QueryEngine
from libi.bootstrap import build_tool_registry
from libi.config import LiBiConfig
from libi.llm import create_llm_client
from libi.llm.base import ProviderRequestError
from libi.mcp.config import McpConfigConflict
from libi.mcp.management import McpManagement
from libi.policy.permission_mode import PermissionMode, PermissionModeController
from libi.runtime.conversation import SUBAGENT_STATE_EVENTS, SubagentCatalog, conversation_event
from libi.runtime.model_settings import (
    ModelSettingsBusy,
    WorkbenchModelSettings,
    contains_credential,
    model_discovery_error,
)
from libi.runtime.tasks import TASK_MODES, DurableTaskManager, TaskRecord
from libi.runtime.titles import generate_session_title
from libi.runtime.workspaces import WorkspaceError, browse_directory, resolve_directory
from libi.session import (
    SessionEvent,
    SessionState,
    SessionStore,
    TurnInProgressError,
    default_runtime_db_path,
)
from libi.tools.base import ToolDecision
from libi.types import Usage

_APPROVAL_TIMEOUT = 300.0
_TASK_LEASE_SECONDS = 300
_TASK_HEARTBEAT_INTERVAL = 30.0
_TASK_CONTROL_INTERVAL = 0.1
_MAX_REQUEST_BYTES = 1024 * 1024
_EVENT_PAGE_SIZE = 200
_FINISHED_RUN_LIMIT = 256
_FINISHED_RUN_TTL = 600.0
_APPROVAL_ROUTE = re.compile(r"/v1/sessions/([^/]+)/approvals/([^/]+)/decision")
_CANCEL_RUN_ROUTE = re.compile(r"/v1/sessions/([^/]+)/runs/([^/]+)/cancel")
_CHILD_STREAM_ROUTE = re.compile(r"/v1/sessions/([^/]+)/subagents/([^/]+)/events/stream")
_CHILD_PREVIEW_LIMIT = 65536
_STREAM_FRAME_INTERVAL = 0.03
_STREAM_HISTORY_INTERVAL = 1.0
_STREAM_HEARTBEAT_INTERVAL = 10.0
_CONVERSATION_EVENT_TYPES = (
    "session/created",
    "session/title_updated",
    "turn/started",
    "turn/completed",
    "turn/failed",
    "turn/canceled",
    "step/started",
    "user/message",
    "assistant/message",
    "assistant/text_delta",
    "tool/execution_started",
    "tool/result",
    "approval/started",
    "approval/approved",
    "approval/rejected",
    "approval/canceled",
    "request/completed",
    "turn/usage",
)
_PRIVATE_KEY_PREVIEW = re.compile(
    r"-----BEGIN [A-Z ]*PRIVATE KEY-----.*?(?:-----END [A-Z ]*PRIVATE KEY-----|\Z)", re.DOTALL
)


@dataclass(eq=False)
class _StreamWake:
    ready: threading.Event = field(default_factory=threading.Event)
    history_changed: bool = True


@dataclass
class _PendingApproval:
    session_id: str
    deadline: float
    agent_id: str
    decision: Future[ToolDecision] = field(default_factory=Future)


class TaskCanceledError(RuntimeError):
    pass


class TaskPlanFailed(RuntimeError):
    def __init__(self, partial_result: str):
        super().__init__("Plan execution failed")
        self.partial_result = partial_result


class RuntimeStoppingError(RuntimeError):
    pass


class RuntimeBindError(OSError):
    pass


class RequestBodyTooLarge(ValueError):
    pass


class RuntimeApiServer:
    """HTTP runtime backed directly by the canonical SessionStore event log."""

    def __init__(
        self,
        *,
        cwd: str,
        config: LiBiConfig,
        api_key: str,
        port: int | None = None,
        workers: int = 2,
        model_settings: WorkbenchModelSettings | None = None,
    ):
        self.cwd = str(Path(cwd).resolve())
        self.config = config
        self._model_settings = model_settings
        self._model_settings_lock = threading.Lock()
        self.api_key = api_key
        self.mcp = McpManagement(self.cwd, sensitive_values=(api_key, config.llm.api_key))
        self.port = config.runtime.port if port is None else port
        self.db_path = default_runtime_db_path()
        self.task_manager = DurableTaskManager(
            Path.home() / ".libi" / "tasks" / "tasks.db", scope=self.cwd
        )
        self.workers = workers
        self._stop = threading.Event()
        self._title_executor = ThreadPoolExecutor(max_workers=2, thread_name_prefix="libi-title")
        self._background_turns: dict[str, dict[str, Any]] = {}
        self._finished_runs: OrderedDict[str, float] = OrderedDict()
        self._background_turns_lock = threading.Condition(threading.Lock())
        self._active_turn_sessions: dict[str, str | None] = {}
        self._session_permissions: dict[str, PermissionMode] = {}
        self._pending_approvals: dict[str, _PendingApproval] = {}
        self._approval_lock = threading.Lock()
        self._assistant_previews: dict[tuple[str, str], tuple[str, str]] = {}
        self._assistant_previews_lock = threading.Lock()
        self._event_streams: dict[str, set[_StreamWake]] = {}
        self._event_streams_lock = threading.Lock()
        self.session_store = SessionStore(
            self.db_path,
            workspace=self.cwd,
            include_runtime_sessions=True,
            sensitive_values=(api_key, config.llm.api_key),
            on_append=lambda session_id: self._wake_event_streams(session_id, history_changed=True),
        )

    def serve_forever(self) -> None:
        with self.listen() as server:
            self.start_background_work()
            server.serve_forever()

    @contextmanager
    def listen(self) -> Iterator[ThreadingHTTPServer]:
        """Bind the HTTP server and clean up active work on exit."""
        outer = self

        class Handler(BaseHTTPRequestHandler):
            timeout = 10

            def do_POST(self) -> None:  # noqa: N802
                outer._handle(self)

            def do_GET(self) -> None:  # noqa: N802
                outer._handle(self)

            def log_message(self, _format: str, *args: Any) -> None:
                return

        with ThreadingHTTPServer(
            ("127.0.0.1", self.port), Handler, bind_and_activate=False
        ) as server:
            if os.name == "nt":
                server.allow_reuse_address = False
                server.socket.setsockopt(socket.SOL_SOCKET, socket.SO_EXCLUSIVEADDRUSE, 1)
            try:
                server.server_bind()
                server.server_activate()
            except OSError as exc:
                raise RuntimeBindError(
                    f"Cannot listen on 127.0.0.1:{self.port}: {exc.strerror or exc}."
                ) from exc
            print(
                f"LiBi Runtime API listening on http://127.0.0.1:{server.server_port}", flush=True
            )
            try:
                yield server
            finally:
                self._stop_and_drain_turns()

    def start_background_work(self) -> None:
        self.session_store.recover_interrupted_turns()
        self._start_workers()
        if self._mcp_auto_start:
            self.mcp.check_at_startup()

    @property
    def _mcp_auto_start(self) -> bool:
        return self.config.features.mcp and self.config.mcp.auto_start

    def _stop_and_drain_turns(self) -> None:
        with self._background_turns_lock:
            self._stop.set()
            active = tuple(self._active_turn_sessions.items())
        for session_id, run_id in active:
            if run_id is not None:
                self._cancel_background_turn(session_id, run_id)
        with self._background_turns_lock:
            self._background_turns_lock.wait_for(lambda: not self._active_turn_sessions)
        self._title_executor.shutdown(wait=True, cancel_futures=True)
        self.mcp.close()

    def work_forever(self) -> None:
        self._start_workers()
        print(f"LiBi task worker running with {self.workers} workers", flush=True)
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
                name=f"libi-task-{index}",
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
        except RequestBodyTooLarge as exc:
            request.close_connection = True
            _send_json(request, 413, {"error": str(exc)})
            _drain_rejected_request(request.connection)
            return
        except TimeoutError:
            request.close_connection = True
            _send_json(request, 408, {"error": "request body timed out"})
            return
        except ValueError as exc:
            _send_json(request, 400, {"error": str(exc)})
            return
        try:
            if self._model_settings is not None and path in {
                "/v1/settings/model",
                "/v1/settings/model/test",
            }:
                self._handle_model_settings(request, path, body, self._model_settings)
                return
            if path == "/v1/mcp/servers" or path.startswith("/v1/mcp/servers/"):
                self._handle_mcp(request, path, body)
                return
            if path.startswith("/v1/sessions/") and not self.session_store.session_exists(
                _session_id_from_path(path), root_only=True
            ):
                _send_json(request, 404, {"error": "session not found"})
                return
            if request.command == "POST" and path == "/v1/sessions":
                cwd = resolve_directory(body.get("cwd", self.cwd))
                session_id = self._create_session(cwd)
                _send_json(request, 200, {"id": session_id, "cwd": str(cwd)})
            elif request.command == "GET" and path == "/v1/sessions":
                _send_json(request, 200, {"sessions": self._list_sessions()})
            elif request.command == "GET" and path == "/v1/workspaces":
                query = parse_qs(request.path.partition("?")[2], keep_blank_values=True)
                _send_json(request, 200, browse_directory(query.get("path", [self.cwd])[0]))
            elif request.command == "GET" and path == "/v1/health":
                _send_json(request, 200, {"status": "ok", "cwd": self.cwd})
            elif request.command == "POST" and _matches_session_route(path, "/title"):
                title = self.session_store.set_session_title(
                    _session_id_from_path(path), body.get("title")
                )
                _send_json(request, 200, {"title": title})
            elif request.command in {"GET", "POST"} and _matches_session_route(
                path, "/permissions"
            ):
                session_id = _session_id_from_path(path)
                if request.command == "POST":
                    permission_mode = body.get("mode")
                    if permission_mode not in ("default", "auto"):
                        raise ValueError("permission mode must be default or auto")
                    self._set_permission_mode(session_id, permission_mode)
                else:
                    permission_mode = self._permission_mode(session_id)
                _send_json(request, 200, {"mode": permission_mode})
            elif request.command == "POST" and _matches_session_route(path, "/turns"):
                session_id = _session_id_from_path(path)
                message = str(body.get("message") or body.get("prompt") or "")
                if not message.strip():
                    _send_json(request, 400, {"error": "message is required"})
                    return
                mode = _mode_from_body(body)
                config = self._reserve_turn(session_id)
                try:
                    result = asyncio.run(
                        self._run_turn(session_id, message, mode=mode, config=config)
                    )
                finally:
                    with self._background_turns_lock:
                        self._release_turn_locked(session_id)
                _send_json(request, 200, result)
            elif request.command == "POST" and _matches_session_route(path, "/turns/async"):
                session_id = _session_id_from_path(path)
                message = str(body.get("message") or body.get("prompt") or "")
                if not message.strip():
                    _send_json(request, 400, {"error": "message is required"})
                    return
                mode = _mode_from_body(body)
                interactive_approval = body.get("interactive_approval", False)
                if not isinstance(interactive_approval, bool):
                    raise ValueError("interactive_approval must be a boolean")
                self._ensure_llm_key(self._config_snapshot())
                run_id, after_sequence = self._start_background_turn(
                    session_id, message, mode=mode, interactive_approval=interactive_approval
                )
                _send_json(
                    request,
                    202,
                    {
                        "run_id": run_id,
                        "turn_id": run_id,
                        "session_id": session_id,
                        "mode": mode,
                        "after_sequence": after_sequence,
                    },
                )
            elif request.command == "POST" and (match := _CANCEL_RUN_ROUTE.fullmatch(path)):
                status = self._cancel_background_turn(*match.groups())
                _send_json(request, status, {"cancel_requested": status == 202})
            elif request.command == "POST" and (match := _APPROVAL_ROUTE.fullmatch(path)):
                decision = body.get("decision")
                if decision not in ("approve", "deny"):
                    raise ValueError("decision must be approve or deny")
                status = self._decide_approval(*match.groups(), decision)
                _send_json(request, status, {"accepted": status == 200})
            elif request.command == "GET" and _matches_session_route(path, "/events.json"):
                session_id = _session_id_from_path(path)
                query_cursor = parse_qs(request.path.partition("?")[2]).get("cursor", ["0"])[0]
                _send_json(request, 200, self._events_json(session_id, _parse_cursor(query_cursor)))
            elif request.command == "GET" and (child_route := _CHILD_STREAM_ROUTE.fullmatch(path)):
                parent_id, child_id = child_route.groups()
                if not self.session_store.is_child_session(parent_id, child_id):
                    _send_json(request, 404, {"error": "child session not found"})
                    return
                self._send_event_stream(
                    request,
                    child_id,
                    after_sequence=_stream_cursor(request),
                    conversation=True,
                    root_session_id=parent_id,
                )
            elif request.command == "GET" and _matches_session_route(path, "/events/stream"):
                session_id = _session_id_from_path(path)
                query = parse_qs(request.path.partition("?")[2])
                self._send_event_stream(
                    request,
                    session_id,
                    after_sequence=_stream_cursor(request),
                    conversation=query.get("view", [""])[0] == "conversation",
                )
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
            elif (
                request.command == "POST"
                and path.startswith("/v1/tasks/")
                and path.endswith("/cancel")
            ):
                _send_json(request, 200, {"canceled": self.task_manager.cancel(path.split("/")[3])})
            else:
                _send_json(request, 404, {"error": "not found"})
        except TurnInProgressError:
            _send_json(request, 409, {"error": "session already has an active turn"})
        except RuntimeStoppingError:
            _send_json(request, 503, {"error": "Runtime is stopping"})
        except WorkspaceError as exc:
            _send_json(request, 400, {"workspace_error": str(exc)})
        except ValueError as exc:
            _send_json(request, 400, {"error": str(exc)})
        except Exception as exc:  # noqa: BLE001 - API boundary
            _send_json(request, 500, {"error": str(exc)})

    def _handle_model_settings(
        self,
        request: BaseHTTPRequestHandler,
        path: str,
        body: dict,
        settings: WorkbenchModelSettings,
    ) -> None:
        try:
            if request.command == "GET" and path == "/v1/settings/model":
                current = settings.load(self.config.llm)
                _send_json(request, 200, settings.public(current))
            elif request.command == "POST" and path == "/v1/settings/model":
                saved = settings.save(body, self.config.llm)
                _send_json(request, 200, settings.public(saved))
            elif request.command == "POST" and path == "/v1/settings/model/test":
                current = settings.load(self.config.llm)
                draft = settings.candidate(body, current)
                client = create_llm_client(draft)
                models = asyncio.run(client.list_models(strict=True, timeout=10.0))
                secrets = (draft.api_key, current.api_key, *self.mcp.sensitive_values)
                models = [
                    model
                    for model in models
                    if len(model) <= 256
                    and not any(contains_credential(model, secret) for secret in secrets)
                ]
                _send_json(request, 200, {"models": models})
            else:
                _send_json(request, 404, {"error": "not found"})
        except ModelSettingsBusy as exc:
            _send_json(request, 409, {"model_error": str(exc)})
        except ValueError as exc:
            _send_json(request, 400, {"model_error": str(exc)})
        except ProviderRequestError as exc:
            _send_json(request, 422, {"model_error": model_discovery_error(exc)})
        except OSError:
            _send_json(request, 500, {"model_error": "无法访问本地模型配置文件，请检查文件权限。"})
        except Exception as exc:  # noqa: BLE001 - keep provider diagnostics private
            logging.getLogger(__name__).warning(
                "Workbench model settings request failed: %s", type(exc).__name__
            )
            _send_json(request, 500, {"model_error": "处理模型配置时出错。"})

    def _handle_mcp(self, request: BaseHTTPRequestHandler, path: str, body: dict) -> None:
        base = "/v1/mcp/servers"
        try:
            if request.command == "GET" and path == base:
                result = self.mcp.list(auto_start=self._mcp_auto_start)
            elif request.command == "POST" and path.startswith(base + "/"):
                content_type = request.headers.get("Content-Type", "").partition(";")[0].strip()
                if content_type.lower() != "application/json":
                    raise ValueError("请使用 JSON 提交 MCP 配置。")
                action = path.removeprefix(base + "/")
                if action == "test":
                    result = asyncio.run(self.mcp.test(body))
                elif action == "preview":
                    result = self.mcp.preview(body.get("document"))
                elif action in {"add", "edit", "toggle", "remove", "import"}:
                    self.mcp.mutate(action, body, auto_start=self._mcp_auto_start)
                    result = {"saved": True}
                else:
                    _send_json(request, 404, {"error": "not found"})
                    return
            else:
                _send_json(request, 404, {"error": "not found"})
                return
            _send_json(request, 200, result)
        except McpConfigConflict as exc:
            _send_json(request, 409, {"mcp_error": str(exc)})
        except ValueError as exc:
            _send_json(request, 400, {"mcp_error": str(exc)})
        except OSError:
            _send_json(request, 500, {"mcp_error": "无法访问 MCP 配置文件。请检查文件权限。"})
        except Exception:
            _send_json(request, 500, {"mcp_error": "处理 MCP 配置时出错。"})

    async def _run_turn(
        self,
        session_id: str,
        message: str,
        *,
        mode: str = "react",
        interactive_approval: bool = False,
        config: LiBiConfig | None = None,
        turn_id: str | None = None,
    ) -> dict[str, Any]:
        mode = _validate_mode(mode)
        if config is None:
            config = self._config_snapshot()
        self._ensure_llm_key(config)
        owns_turn = turn_id is None
        if turn_id is None:
            turn_id = f"turn_{uuid.uuid4().hex}"
            self.session_store.start_turn(session_id, turn_id, mode=mode, message=message)
            self._schedule_session_title(session_id, turn_id, message, config)
        engine: QueryEngine | None = None
        text = ""
        total_tokens = 0
        usage_totals = Usage()
        plan_outcome = None
        preview_turns: dict[str, str] = {}
        canceled = False

        async def approve_tool(payload: dict[str, Any]) -> ToolDecision:
            return await self._request_approval(session_id, turn_id, payload)

        def record_usage(usage: Usage) -> None:
            nonlocal usage_totals
            usage_totals += usage
            self.session_store.append(
                session_id,
                "turn/usage",
                {
                    "total_tokens": usage_totals.total_tokens,
                    "cache_hit_tokens": usage_totals.cache_hit_tokens,
                    "cache_context_tokens": usage_totals.cache_context_tokens,
                },
                turn_id=turn_id,
                source="runtime",
            )

        def update_preview(event: dict[str, Any], *, child: bool = False) -> None:
            event_session_id = event.get("session_id") if child else session_id
            event_turn_id, step_id = event.get("turn_id"), event.get("step_id")
            delta = str(event.get("text") or "")
            if not (event_session_id and event_turn_id and step_id and delta):
                return
            if not child and event_turn_id != turn_id:
                return
            key = (event_session_id, event_turn_id)
            with self._assistant_previews_lock:
                previous_turn = preview_turns.get(event_session_id)
                if previous_turn and previous_turn != event_turn_id:
                    self._assistant_previews.pop((event_session_id, previous_turn), None)
                preview_turns[event_session_id] = event_turn_id
                previous_step, previous_text = self._assistant_previews.get(key, (None, ""))
                preview = previous_text + delta if previous_step == step_id else delta
                self._assistant_previews[key] = (
                    step_id,
                    preview[:_CHILD_PREVIEW_LIMIT] if child else preview,
                )
            self._wake_event_streams(event_session_id)

        try:
            cwd = self._turn_cwd(session_id)
            # Session selection does not extend the Runtime's trust for MCP configuration.
            registry, manager = await build_tool_registry(config=config, cwd=self.cwd)
            await self.mcp.remember_discovered(manager)
            engine = QueryEngine(
                llm_client=create_llm_client(config.llm, on_usage=record_usage),
                tool_registry=registry,
                config=config,
                cwd=cwd,
                session_store=self.session_store,
                session_id=session_id,
                approval_callback=approve_tool if interactive_approval else None,
                child_preview_callback=lambda event: update_preview(event, child=True),
            )
            events = _engine_events(engine, mode, message, turn_id=turn_id)
            async with aclosing(events):
                async for event in events:
                    event_type = str(event.get("type"))
                    if event_type == "text_delta":
                        text += str(event.get("text") or "")
                        update_preview(event)
                    elif event_type == "done":
                        total_tokens = int(event.get("total_tokens") or 0)
                        plan_outcome = event.get("plan_outcome")
                    elif event_type == "error":
                        raise event["error"]
        except asyncio.CancelledError:
            canceled = True
            self._close_turn(session_id, turn_id, "turn/canceled", {"mode": mode})
            raise
        except Exception as exc:
            self._close_turn(session_id, turn_id, "turn/failed", {"error_type": type(exc).__name__})
            raise
        finally:
            try:
                if engine is not None:
                    await engine.aclose()
            except Exception as exc:
                if not canceled:
                    raise
                logging.getLogger(__name__).warning(
                    "Canceled turn cleanup failed: %s", type(exc).__name__
                )
            finally:
                if owns_turn:
                    self.session_store.release_turn(session_id, turn_id)
                with self._assistant_previews_lock:
                    for identity, preview_turn in preview_turns.items():
                        self._assistant_previews.pop((identity, preview_turn), None)
                for identity in preview_turns:
                    self._wake_event_streams(identity)
        return {
            "session_id": session_id,
            "text": text,
            "mode": mode,
            "total_tokens": total_tokens,
            **({"plan_outcome": plan_outcome} if plan_outcome else {}),
        }

    def _start_background_turn(
        self, session_id: str, message: str, *, mode: str, interactive_approval: bool = False
    ) -> tuple[str, int]:
        run_id = f"turn_{uuid.uuid4().hex}"
        config = self._reserve_turn(session_id, run_id)
        try:
            after_sequence = self.session_store.start_turn(
                session_id, run_id, mode=mode, message=message
            )
        except Exception:
            with self._background_turns_lock:
                self._release_turn_locked(session_id)
            raise
        with self._background_turns_lock:
            record = self._background_turns[run_id] = {
                "run_id": run_id,
                "session_id": session_id,
                "mode": mode,
                "status": "running",
                "started_at": datetime.now(UTC).isoformat(),
            }

        async def run() -> dict[str, Any]:
            with self._background_turns_lock:
                record["task"] = asyncio.current_task()
                canceled = self._stop.is_set() or record["status"] == "canceling"
                if canceled:
                    record["status"] = "canceling"
            if canceled:
                self._close_turn(session_id, run_id, "turn/canceled", {"mode": mode})
                raise asyncio.CancelledError
            return await self._run_turn(
                session_id,
                message,
                mode=mode,
                interactive_approval=interactive_approval,
                config=config,
                turn_id=run_id,
            )

        def runner() -> None:
            outcome: dict[str, Any] = {"status": "failed", "error": "interrupted"}
            try:
                result = asyncio.run(run())
                status = (result.get("plan_outcome") or {}).get("status", "completed")
                outcome = {"status": status}
            except asyncio.CancelledError:
                outcome = {"status": "canceled"}
                self._close_turn(session_id, run_id, "turn/canceled", {"mode": mode})
            except Exception as exc:  # noqa: BLE001 - background task boundary
                outcome = {"status": "failed", "error": type(exc).__name__}
                self._close_turn(
                    session_id, run_id, "turn/failed", {"error_type": type(exc).__name__}
                )
            finally:
                self.session_store.release_turn(session_id, run_id)
                with self._background_turns_lock:
                    record.pop("task", None)
                    record.update(outcome, finished_at=datetime.now(UTC).isoformat())
                    self._finished_runs[run_id] = monotonic() + _FINISHED_RUN_TTL
                    self._prune_finished_runs_locked()
                    self._release_turn_locked(session_id)
                self._wake_event_streams(session_id)

        try:
            threading.Thread(target=runner, name=f"libi-turn-{run_id}", daemon=True).start()
        except RuntimeError as exc:
            try:
                self._close_turn(
                    session_id, run_id, "turn/failed", {"error_type": type(exc).__name__}
                )
            finally:
                self.session_store.release_turn(session_id, run_id)
                with self._background_turns_lock:
                    del self._background_turns[run_id]
                    self._release_turn_locked(session_id)
            raise
        self._schedule_session_title(session_id, run_id, message, config)
        self._wake_event_streams(session_id)
        return run_id, after_sequence

    def _schedule_session_title(
        self, session_id: str, turn_id: str, message: str, config: LiBiConfig
    ) -> None:
        if not config.session_title.enabled or not message.strip() or self._stop.is_set():
            return
        try:
            if self.session_store.claim_session_title(session_id, turn_id):
                prompt = self.session_store.sanitize_payload({"content": message})["content"]
                self._title_executor.submit(
                    self._generate_session_title, session_id, prompt, config
                )
        except Exception as exc:
            logging.getLogger(__name__).warning(
                "Session title scheduling failed: %s", type(exc).__name__
            )

    def _generate_session_title(self, session_id: str, message: str, config: LiBiConfig) -> None:
        try:
            title = asyncio.run(generate_session_title(config, message))
            if title:
                self.session_store.set_session_title(session_id, title, generated=True)
        except Exception as exc:
            logging.getLogger(__name__).warning(
                "Session title generation failed: %s", type(exc).__name__
            )

    def _reserve_turn(self, session_id: str, run_id: str | None = None) -> LiBiConfig:
        self._turn_cwd(session_id)
        with self._background_turns_lock:
            if self._stop.is_set():
                raise RuntimeStoppingError
            if session_id in self._active_turn_sessions:
                raise TurnInProgressError
            config = self._config_snapshot()
            PermissionModeController(config, self._session_permissions.get(session_id, "default"))
            self._active_turn_sessions[session_id] = run_id
            return config

    def _turn_cwd(self, session_id: str) -> str:
        cwd = self.session_store.session_cwd(session_id) or self.cwd
        resolved = str(resolve_directory(cwd))
        if os.path.normcase(resolved) != os.path.normcase(cwd):
            raise WorkspaceError("会话的工作目录位置已改变，请使用新目录创建会话。")
        return cwd

    def _release_turn_locked(self, session_id: str) -> None:
        del self._active_turn_sessions[session_id]
        self._background_turns_lock.notify_all()

    def _prune_finished_runs_locked(self) -> None:
        now = monotonic()
        while self._finished_runs:
            oldest = next(iter(self._finished_runs.values()))
            if oldest > now and len(self._finished_runs) <= _FINISHED_RUN_LIMIT:
                break
            run_id, _ = self._finished_runs.popitem(last=False)
            self._background_turns.pop(run_id, None)

    def _cancel_background_turn(self, session_id: str, run_id: str) -> int:
        with self._background_turns_lock:
            self._prune_finished_runs_locked()
            record = self._background_turns.get(run_id)
            if record is None or record["session_id"] != session_id:
                return 404
            if record["status"] == "canceling":
                return 202
            if record["status"] != "running":
                return 409
            task = record.get("task")
            if task is not None:
                try:
                    task.get_loop().call_soon_threadsafe(task.cancel)
                except RuntimeError:  # The event loop closed while the run was finishing.
                    return 409
            record["status"] = "canceling"
        self._wake_event_streams(session_id)
        return 202

    def _active_background_run(self, session_id: str) -> dict[str, str] | None:
        with self._background_turns_lock:
            run_id = self._active_turn_sessions.get(session_id)
            if run_id is None:
                return None
            record = self._background_turns.get(run_id)
            return {"run_id": run_id, "status": record["status"]} if record else None

    async def _request_approval(
        self, session_id: str, turn_id: str, request: dict[str, Any]
    ) -> ToolDecision:
        approval_id = f"approval_{uuid.uuid4().hex}"
        expires_at = datetime.now(UTC) + timedelta(seconds=_APPROVAL_TIMEOUT)
        pending = _PendingApproval(session_id, monotonic() + _APPROVAL_TIMEOUT, request["agent_id"])
        agent_session_id = request.get("session_id")
        agent_label = None
        if agent_session_id and agent_session_id != session_id:
            descriptors = self.session_store.events(
                agent_session_id, event_types=("agent/subagent_descriptor",)
            )
            if descriptors:
                agent_label = str(descriptors[-1].payload.get("description") or "")[:160]
        event_fields = {
            "turn_id": turn_id,
            "tool_call_id": request.get("tool_call_id"),
            "approval_id": approval_id,
            "source": "runtime-approval",
        }
        started = False
        decision = None
        with self._approval_lock:
            self._pending_approvals[approval_id] = pending
        try:
            self.session_store.append(
                session_id,
                "approval/started",
                {
                    "tool_name": request["tool_name"],
                    "input": request["input"],
                    "danger_level": request["danger_level"],
                    "agent_id": request["agent_id"],
                    "agent_session_id": agent_session_id,
                    "agent_label": agent_label,
                    "expires_at": expires_at.isoformat(),
                },
                **event_fields,
            )
            started = True
            await asyncio.wait(
                {asyncio.wrap_future(pending.decision)},
                timeout=max(0, pending.deadline - monotonic()),
            )
            with self._approval_lock:
                decision = pending.decision.result() if pending.decision.done() else None
        finally:
            with self._approval_lock:
                del self._pending_approvals[approval_id]
            if started:
                event_type = {"approve": "approval/approved", "deny": "approval/rejected"}
                self.session_store.append(
                    session_id, event_type.get(decision, "approval/canceled"), {}, **event_fields
                )
        return decision or "deny"

    def _decide_approval(self, session_id: str, approval_id: str, decision: ToolDecision) -> int:
        with self._approval_lock:
            pending = self._pending_approvals.get(approval_id)
            if pending is None or pending.session_id != session_id:
                return 404
            if pending.decision.done() or monotonic() >= pending.deadline:
                return 409
            pending.decision.set_result(decision)
            return 200

    def _permission_mode(self, session_id: str) -> PermissionMode:
        with self._background_turns_lock:
            return self._session_permissions.get(session_id, "default")

    def _set_permission_mode(self, session_id: str, mode: PermissionMode) -> None:
        with self._background_turns_lock:
            self._session_permissions[session_id] = mode
        self._wake_event_streams(session_id)

    def _create_session(self, cwd: Path | None = None) -> str:
        session_id = self.session_store.create_session(cwd=cwd)
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
                "title": item.title,
                "cwd": item.cwd or self.cwd,
            }
            for item in self.session_store.list_sessions(limit=100)
        ]

    def _events_json(self, session_id: str, after_sequence: int) -> dict[str, Any]:
        events = self.session_store.events(
            session_id, after_sequence=after_sequence, limit=_EVENT_PAGE_SIZE
        )
        state = self.session_store.state(session_id)
        cursor = events[-1].sequence if events else after_sequence
        return {
            "session_id": session_id,
            "cwd": self.session_store.session_cwd(session_id) or self.cwd,
            "events": [self._serialize_event(event) for event in events],
            "next_cursor": cursor,
            "has_more": state.last_sequence > cursor,
            "state": asdict(state),
            "active_run": self._active_background_run(session_id),
            "permission_mode": self._permission_mode(session_id),
            "assistant_preview": self._assistant_preview(state),
        }

    def _assistant_preview(self, state: SessionState) -> dict[str, Any] | None:
        if state.active_turn_id and state.active_step_id:
            with self._assistant_previews_lock:
                preview = self._assistant_previews.get((state.session_id, state.active_turn_id))
            if preview and preview[0] == state.active_step_id:
                payload = self.session_store.sanitize_payload(
                    {
                        "turn_id": state.active_turn_id,
                        "step_id": preview[0],
                        "text": _PRIVATE_KEY_PREVIEW.sub("[redacted]", preview[1]),
                    }
                )
                text = payload["text"]
                # A chunk can end midway through a credential that the next chunk completes.
                hidden = 0
                for secret in self.session_store.redactor.sensitive_values:
                    for length in range(min(len(secret) - 1, len(text)), hidden, -1):
                        if text.endswith(secret[:length]):
                            hidden = length
                            break
                if hidden:
                    payload["text"] = text[:-hidden]
                return payload if payload["text"] else None
        return None

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
            "approval_id": event.approval_id,
            "schema_version": event.schema_version,
            "source": event.source,
        }

    def _wake_event_streams(self, session_id: str, *, history_changed: bool = False) -> None:
        with self._event_streams_lock:
            for wake in self._event_streams.get(session_id, ()):
                wake.history_changed |= history_changed
                wake.ready.set()

    def _replay_conversation(
        self,
        state: SessionState,
        catalog: SubagentCatalog,
        cursor: int,
        *,
        include_messages: bool = True,
    ) -> tuple[list[SessionEvent], SessionState]:
        history, watermark = self.session_store.replay_events(
            state.session_id,
            after_sequence=state.last_sequence,
            visible_after_sequence=cursor,
            visible_types=_CONVERSATION_EVENT_TYPES if include_messages else (),
            state_event_types=SUBAGENT_STATE_EVENTS,
            page_size=_EVENT_PAGE_SIZE if include_messages else None,
        )
        catalog.advance(state, history)
        return history, replace(
            self.session_store.advance_state(state, history), last_sequence=watermark
        )

    def _send_event_stream(
        self,
        request: BaseHTTPRequestHandler,
        session_id: str,
        *,
        after_sequence: int,
        conversation: bool = False,
        root_session_id: str | None = None,
    ) -> None:
        cwd = self.session_store.session_cwd(session_id) or self.cwd
        parent_id = self.session_store.child_parent(session_id) if root_session_id else None
        parent_state = SessionState(parent_id, "created", 0) if parent_id else None
        parent_catalog = SubagentCatalog()
        child_summary = None
        wake = _StreamWake()
        with self._event_streams_lock:
            self._event_streams.setdefault(session_id, set()).add(wake)

        state = SessionState(session_id, "created", 0)
        catalog = SubagentCatalog()
        subagents: list[dict[str, Any]] = []
        owner = root_session_id or session_id
        last_view = None
        cursor = after_sequence
        history_deadline = 0.0
        last_write = 0.0
        started = False
        try:
            while not self._stop.is_set():
                with self._event_streams_lock:
                    history_changed = wake.history_changed
                    wake.history_changed = False
                    wake.ready.clear()
                events = []
                if history_changed or monotonic() >= history_deadline:
                    # Local commits wake the stream. This also catches writes from other processes.
                    if conversation:
                        history, state = self._replay_conversation(state, catalog, cursor)
                        with self._background_turns_lock:
                            owner_active = owner in self._active_turn_sessions
                        with self._approval_lock:
                            waiting_agents = {
                                approval.agent_id
                                for approval in self._pending_approvals.values()
                                if approval.session_id == owner
                            }
                        subagents = catalog.snapshot(
                            self.session_store,
                            session_id,
                            active_parent_turn=state.active_turn_id if owner_active else None,
                            waiting_agents=waiting_agents,
                        )
                        if parent_state is not None:
                            _, parent_state = self._replay_conversation(
                                parent_state, parent_catalog, 0, include_messages=False
                            )
                            summaries = parent_catalog.snapshot(
                                self.session_store,
                                parent_state.session_id,
                                active_parent_turn=parent_state.active_turn_id
                                if owner_active
                                else None,
                                waiting_agents=waiting_agents,
                                only_session_id=session_id,
                            )
                            child_summary = summaries[0] if summaries else None
                    else:
                        history = self.session_store.events(
                            session_id, after_sequence=state.last_sequence, limit=_EVENT_PAGE_SIZE
                        )
                        state = self.session_store.advance_state(state, history)
                    events = [
                        conversation_event(event, self._serialize_event(event))
                        if conversation
                        else self._serialize_event(event)
                        for event in history
                        if event.sequence > cursor
                        and (not conversation or event.event_type in _CONVERSATION_EVENT_TYPES)
                    ]
                    history_deadline = monotonic() + _STREAM_HISTORY_INTERVAL
                    if len(history) >= _EVENT_PAGE_SIZE or (
                        conversation and state.last_sequence >= cursor + _EVENT_PAGE_SIZE
                    ):
                        history_deadline = monotonic()
                preview = self._assistant_preview(state)
                active_run = self._active_background_run(session_id)
                permission_mode = self._permission_mode(session_id)
                view = (state, preview, active_run, permission_mode, subagents, child_summary)
                if not started:
                    request.close_connection = True
                    request.connection.settimeout(5)
                    request.send_response(200)
                    request.send_header("content-type", "text/event-stream")
                    request.send_header("cache-control", "no-cache, no-transform")
                    request.send_header("x-accel-buffering", "no")
                    request.send_header("connection", "close")
                    request.end_headers()
                    started = True
                if events or view != last_view:
                    if conversation:
                        cursor = max(cursor, state.last_sequence)
                    elif events:
                        cursor = events[-1]["sequence"]
                    data = json.dumps(
                        {
                            "session_id": session_id,
                            "cwd": cwd,
                            "events": events,
                            "next_cursor": cursor,
                            "state": asdict(state),
                            "active_run": active_run,
                            "permission_mode": permission_mode,
                            "assistant_preview": preview,
                            **({"subagents": subagents} if conversation else {}),
                            **({"subagent": child_summary} if parent_state is not None else {}),
                        },
                        ensure_ascii=False,
                    )
                    request.wfile.write(f"id: {cursor}\nevent: page\ndata: {data}\n\n".encode())
                    last_view = view
                    last_write = monotonic()
                elif monotonic() - last_write >= _STREAM_HEARTBEAT_INTERVAL:
                    request.wfile.write(b": heartbeat\n\n")
                    last_write = monotonic()
                deadline = min(history_deadline, last_write + _STREAM_HEARTBEAT_INTERVAL)
                wake.ready.wait(max(0, deadline - monotonic()))
                self._stop.wait(max(0, last_write + _STREAM_FRAME_INTERVAL - monotonic()))
        except (ConnectionError, TimeoutError):
            pass
        except Exception as exc:  # noqa: BLE001 - headers may already have been sent
            if not started:
                raise
            logging.getLogger(__name__).warning(
                "Runtime event stream ended: %s", type(exc).__name__
            )
        finally:
            with self._event_streams_lock:
                self._event_streams[session_id].remove(wake)
                if not self._event_streams[session_id]:
                    del self._event_streams[session_id]

    def _close_turn(
        self,
        session_id: str,
        turn_id: str,
        status: str,
        detail: dict[str, Any],
    ) -> None:
        if self.session_store.active_turn_step(session_id)[0] == turn_id:
            self.session_store.append(session_id, status, detail, turn_id=turn_id, source="runtime")

    def _worker_loop(self, worker_id: str) -> None:
        while not self._stop.is_set():
            task = self.task_manager.claim_next(worker_id, lease_seconds=_TASK_LEASE_SECONDS)
            if not task:
                self._stop.wait(0.5)
                continue
            try:
                result = asyncio.run(self._execute_claim(task))
                self.task_manager.complete(
                    task.id, result, worker_id=worker_id, attempt=task.attempts
                )
            except TaskCanceledError:
                pass
            except TaskPlanFailed as exc:
                self.task_manager.fail(
                    task.id,
                    str(exc),
                    worker_id=worker_id,
                    attempt=task.attempts,
                    partial_result=exc.partial_result,
                )
            except Exception as exc:  # noqa: BLE001
                self.task_manager.fail(
                    task.id, str(exc), worker_id=worker_id, attempt=task.attempts
                )
            finally:
                self.task_manager.finish_cancel(task)

    async def _execute_claim(self, task: TaskRecord) -> str:
        operation = asyncio.create_task(self._run_task(task))
        watcher = asyncio.create_task(self._watch_claim(task))
        try:
            done, _ = await asyncio.wait({operation, watcher}, return_when=asyncio.FIRST_COMPLETED)
            if operation in done:
                return operation.result()
            watcher.result()
            raise TaskCanceledError(task.id)
        finally:
            for pending in (operation, watcher):
                if not pending.done():
                    pending.cancel()
            await asyncio.gather(operation, watcher, return_exceptions=True)

    async def _watch_claim(self, task: TaskRecord) -> None:
        deadline = monotonic() + _TASK_HEARTBEAT_INTERVAL
        assert task.worker_id is not None
        while not self._stop.is_set():
            if not await asyncio.to_thread(self.task_manager.owns_active_claim, task):
                return
            if monotonic() >= deadline:
                renewed = await asyncio.to_thread(
                    self.task_manager.heartbeat,
                    task.id,
                    task.worker_id,
                    lease_seconds=_TASK_LEASE_SECONDS,
                    attempt=task.attempts,
                )
                if not renewed:
                    return
                deadline = monotonic() + _TASK_HEARTBEAT_INTERVAL
            await asyncio.sleep(_TASK_CONTROL_INTERVAL)

    async def _run_task(self, task: TaskRecord) -> str:
        if self.task_manager.is_canceled(task.id):
            raise TaskCanceledError(task.id)
        if task.mode not in TASK_MODES:
            raise ValueError(
                f"task mode {task.mode!r} is no longer supported; recreate it as react or plan"
            )
        config = self._config_snapshot()
        self._ensure_llm_key(config)
        registry, manager = await build_tool_registry(config=config, cwd=self.cwd)
        await self.mcp.remember_discovered(manager)
        engine = QueryEngine(
            llm_client=create_llm_client(config.llm),
            tool_registry=registry,
            config=config,
            cwd=self.cwd,
        )
        text = ""
        try:
            events = _engine_events(engine, task.mode, task.prompt)
            async with aclosing(events):
                async for event in events:
                    if event.get("type") == "text_delta":
                        text += str(event.get("text") or "")
                    elif event.get("type") == "error":
                        raise event["error"]
                    elif event.get("type") == "done":
                        outcome = event.get("plan_outcome")
                        if outcome and outcome["status"] == "failed":
                            raise TaskPlanFailed(text)
        finally:
            await engine.aclose()
        return text

    def _config_snapshot(self) -> LiBiConfig:
        config = deepcopy(self.config)
        if self._model_settings is not None:
            config.llm = self._model_settings.load(config.llm)
            key = config.llm.api_key
            with self._model_settings_lock:
                if key and key not in self.mcp.sensitive_values:
                    self.mcp.sensitive_values += (key,)
                    self.session_store.redactor.add_sensitive_values((key,))
        return config

    @staticmethod
    def _ensure_llm_key(config: LiBiConfig) -> None:
        if not config.llm.api_key:
            raise ValueError(
                "LIBI_API_KEY is not configured. Runtime turns/tasks need a working LLM key."
            )

    def _authorized(self, request: BaseHTTPRequestHandler) -> bool:
        auth = request.headers.get("authorization", "")
        token = request.headers.get("x-api-key", "")
        return bool(self.api_key) and (
            compare_digest(auth.encode(), f"Bearer {self.api_key}".encode())
            or compare_digest(token.encode(), self.api_key.encode())
        )


def _matches_session_route(path: str, suffix: str) -> bool:
    return re.fullmatch(r"/v1/sessions/[^/]+" + re.escape(suffix), path) is not None


def _stream_cursor(request: BaseHTTPRequestHandler) -> int:
    cursor = parse_qs(request.path.partition("?")[2]).get("cursor", ["0"])[0]
    return _parse_cursor(request.headers.get("last-event-id") or cursor)


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
    lengths = request.headers.get_all("content-length", [])
    if len(lengths) > 1 or request.headers.get("transfer-encoding"):
        raise ValueError("invalid request body framing")
    length = int(lengths[0]) if lengths else 0
    if length < 0:
        raise ValueError("content-length must be non-negative")
    if length > _MAX_REQUEST_BYTES:
        raise RequestBodyTooLarge("request body exceeds 1 MiB")
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


def _drain_rejected_request(connection: socket.socket) -> None:
    # Closing with unread bytes can reset TCP and discard the 413 response.
    # A short half-close drain gives sending clients time to receive it.
    with suppress(OSError):
        connection.shutdown(socket.SHUT_WR)
        deadline = monotonic() + 0.25
        remaining = 2 * _MAX_REQUEST_BYTES
        while remaining > 0 and monotonic() < deadline:
            connection.settimeout(max(0.001, deadline - monotonic()))
            chunk = connection.recv(min(64 * 1024, remaining))
            if not chunk:
                break
            remaining -= len(chunk)


def runtime_api_key(explicit: str | None = None) -> str:
    key = explicit or os.environ.get("LIBI_RUNTIME_API_KEY")
    if not key:
        raise ValueError("LIBI_RUNTIME_API_KEY is required for Runtime API")
    return key


def _mode_from_body(body: dict[str, Any]) -> str:
    return _validate_mode(str(body.get("mode") or "react"))


def _validate_mode(mode: str) -> str:
    value = mode.strip().lower()
    if value not in TASK_MODES:
        raise ValueError(f"mode must be one of: {', '.join(sorted(TASK_MODES))}")
    return value


def _engine_events(engine: QueryEngine, mode: str, message: str, *, turn_id: str | None = None):
    if mode == "react":
        return engine.ask(message, turn_id=turn_id) if turn_id else engine.ask(message)
    if mode == "plan":
        return engine.plan(message, turn_id=turn_id) if turn_id else engine.plan(message)
    raise ValueError(f"mode must be one of: {', '.join(sorted(TASK_MODES))}")
