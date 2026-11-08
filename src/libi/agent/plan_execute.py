from __future__ import annotations

import asyncio
import logging
import uuid
from collections.abc import AsyncIterator
from contextlib import aclosing, suppress
from copy import deepcopy
from dataclasses import dataclass
from typing import Any

from libi.attachments import AttachmentStore
from libi.config import LiBiConfig
from libi.llm.base import LlmClient, ProviderRequestError
from libi.llm.pricing import calculate_costs
from libi.plan import ExecutionPlan, Planner, Task, TaskStatus
from libi.prompt import PromptAssembler
from libi.session import ContextProjector, SessionStore, default_runtime_db_path
from libi.snapshot import SnapshotService
from libi.tools.registry import ToolRegistry
from libi.types import Message, Usage


@dataclass(slots=True)
class TaskRunResult:
    task: Task
    text: str
    usage: Usage
    turns: int
    session_id: str
    error: Exception | None = None

    @property
    def tokens(self) -> int:
        return self.usage.total_tokens


class PlanExecuteAgent:
    def __init__(
        self,
        *,
        llm_client: LlmClient,
        tool_registry: ToolRegistry,
        config: LiBiConfig,
        cwd: str,
        approval_callback=None,
        planner: Planner | None = None,
        max_task_turns: int | None = None,
        session_store: SessionStore | None = None,
        session_id: str | None = None,
    ):
        self.llm_client = llm_client
        self.tool_registry = tool_registry
        self.config = config
        self.cwd = cwd
        self.approval_callback = approval_callback
        self.planner = planner or Planner(llm_client)
        self.max_task_turns = max_task_turns
        self.session_store = session_store
        self.session_id = session_id
        self._attachments: list[dict[str, Any]] = []

    async def run(
        self, message: str, *, turn_id: str | None = None
    ) -> AsyncIterator[dict[str, Any]]:
        if self.session_store is None:
            self.session_store = SessionStore(
                default_runtime_db_path(),
                workspace=self.cwd,
                sensitive_values=(self.config.llm.api_key,),
            )
        store = self.session_store
        store.redactor.add_sensitive_values((self.config.llm.api_key,))
        if self.session_id is None:
            self.session_id = store.create_session()
        session_id = self.session_id
        owns_turn = turn_id is None
        if turn_id is None:
            turn_id = f"turn_{uuid.uuid4().hex}"
            store.start_turn(session_id, turn_id, mode="plan", message=message)
        elif store.active_turn_step(session_id)[0] != turn_id:
            raise RuntimeError("The accepted turn is no longer active.")
        step_id = f"step_{uuid.uuid4().hex}"
        plan_request_id = f"req_{uuid.uuid4().hex}"
        answer: list[str] = []
        try:
            self._attachments = next(
                (
                    event.payload["attachments"]
                    for event in reversed(store.events(session_id, event_types=("user/message",)))
                    if event.payload.get("attachments")
                ),
                [],
            )
            store.append(
                session_id, "step/started", {"mode": "plan"}, turn_id=turn_id, step_id=step_id
            )
            async with aclosing(self._run(message)) as events:
                async for event in events:
                    kind = event.get("type")
                    if kind == "text_delta":
                        answer.append(str(event.get("text") or ""))
                    elif kind == "provider_attempt_failed":
                        store.append(
                            session_id,
                            "request/attempt_failed",
                            {
                                **event["diagnostic"],
                                "request_id": plan_request_id,
                                "purpose": "planning",
                                "provider": event["provider"],
                                "model": event["model"],
                            },
                            turn_id=turn_id,
                            step_id=step_id,
                            source="planner",
                        )
                        continue
                    elif kind in {"plan_task_started", "plan_task_done", "plan_task_failed"}:
                        event_type = {
                            "plan_task_started": "agent/child_started",
                            "plan_task_done": "agent/child_completed",
                            "plan_task_failed": "agent/child_failed",
                        }[kind]
                        store.append(
                            session_id,
                            event_type,
                            {
                                "task_id": event["task_id"],
                                "child_session_id": event["child_session_id"],
                                "mode": "plan",
                            },
                            turn_id=turn_id,
                            step_id=step_id,
                        )
                    elif kind == "error":
                        raise event["error"]
                    elif kind == "done":
                        outcome = event["plan_outcome"]
                        terminal = outcome["status"]
                        store.append(
                            session_id,
                            "assistant/message",
                            {"content": "".join(answer)},
                            turn_id=turn_id,
                            step_id=step_id,
                            source="plan-summary",
                        )
                        store.append(
                            session_id, f"step/{terminal}", {}, turn_id=turn_id, step_id=step_id
                        )
                        store.append(
                            session_id,
                            f"turn/{terminal}",
                            {
                                "mode": "plan",
                                "total_tokens": event["total_tokens"],
                                "plan_outcome": outcome,
                            },
                            turn_id=turn_id,
                        )
                        event = {
                            **event,
                            "messages": list(
                                ContextProjector()
                                .project_session(store.events(session_id), session_id=session_id)
                                .messages
                            ),
                        }
                    yield {
                        **event,
                        "session_id": session_id,
                        "turn_id": turn_id,
                        "step_id": step_id,
                    }
        except BaseException as exc:
            if store.active_turn_step(session_id)[0] != turn_id:
                raise
            status = (
                "turn/canceled"
                if isinstance(exc, asyncio.CancelledError | GeneratorExit)
                else "turn/failed"
            )
            failure: dict[str, Any] = {"reason": type(exc).__name__}
            if isinstance(exc, ProviderRequestError):
                failure = {"reason": "provider_error", "provider_error": exc.details}
            store.append(
                session_id,
                "step/failed",
                failure,
                turn_id=turn_id,
                step_id=step_id,
            )
            store.append(session_id, status, failure, turn_id=turn_id)
            raise
        finally:
            self._attachments = []
            if owns_turn:
                store.release_turn(session_id, turn_id)

    async def _run(self, message: str) -> AsyncIterator[dict[str, Any]]:
        snapshot = SnapshotService(self.cwd)
        total_usage = Usage()
        total_turns = 0
        final_text = ""
        try:
            goal = message or "请分析本轮附件。"
            yield {"type": "text_delta", "text": f"正在规划任务：{goal}\n\n"}
            yield {"type": "plan_status", "phase": "planning"}
            plan: ExecutionPlan | None = None
            if self._attachments:
                assert self.session_store is not None
                store = AttachmentStore(self.session_store.db_path)
                parts = store.prompt_parts(
                    self._attachments,
                    selected={(ref["session_id"], ref["id"]) for ref in self._attachments},
                    supports_images=bool(getattr(self.llm_client, "supports_images", False)),
                    clean_text=self.session_store.redactor.clean,
                )
                planning = self.planner.stream_plan(goal, attachment_parts=parts)
            else:
                planning = self.planner.stream_plan(goal)
            async for event in planning:
                if event.get("type") == "plan_created":
                    plan = event["plan"]
                    continue
                if event.get("type") == "usage":
                    total_usage = total_usage + Usage.from_mapping(event.get("usage") or {})
                yield event
            if plan is None:
                raise ValueError("planner did not produce an execution plan")
            yield {"type": "text_delta", "text": plan.summarize() + "\n\n"}
            async with aclosing(self._execute_plan(plan, snapshot)) as events:
                async for event in events:
                    if event.get("type") == "usage":
                        total_usage = total_usage + Usage.from_mapping(event.get("usage") or {})
                    elif event.get("type") == "plan_task_done":
                        total_turns += int(event.get("turns") or 0)
                    elif event.get("type") == "text_delta":
                        final_text += str(event.get("text") or "")
                    yield event
        except Exception as exc:  # noqa: BLE001
            yield {"type": "error", "error": exc}
            return
        finally:
            with suppress(Exception):
                snapshot.finish_turn()
        done: dict[str, Any] = {
            "type": "done",
            "plan_outcome": {
                "status": plan.status.value.lower(),
                "failed_task_ids": [
                    task.id for task in plan.tasks.values() if task.status == TaskStatus.FAILED
                ],
                "skipped_task_ids": [
                    task.id for task in plan.tasks.values() if task.status == TaskStatus.SKIPPED
                ],
            },
            "total_turns": total_turns,
            "total_tokens": total_usage.total_tokens,
            "usage": total_usage.to_dict(),
            "messages": [
                Message(role="user", content=message),
                Message(role="assistant", content=final_text),
            ],
        }
        costs = calculate_costs(self.llm_client, total_usage)
        if costs:
            done["cost"] = costs
        yield done

    async def _execute_plan(
        self, plan: ExecutionPlan, snapshot: SnapshotService
    ) -> AsyncIterator[dict[str, Any]]:
        yield {"type": "text_delta", "text": "开始执行计划……\n\n"}
        yield {"type": "plan_status", "phase": "execution"}
        plan.mark_started()
        while True:
            executable = _executable_tasks_in_order(plan)
            if not executable:
                break
            if len(executable) == 1:
                result: TaskRunResult | None = None
                async with aclosing(
                    self._execute_task_stream(plan, executable[0], snapshot)
                ) as events:
                    async for event in events:
                        if event.get("type") == "plan_task_result":
                            result = event["result"]
                        else:
                            yield event
                if result is None:
                    raise RuntimeError(f"task {executable[0].id} did not produce a result")
                async for event in self._apply_task_result(result):
                    yield event
                continue
            yield {
                "type": "text_delta",
                "text": (f"正在并行执行：{', '.join(task.id for task in executable)}\n\n"),
            }
            queue: asyncio.Queue[dict[str, Any]] = asyncio.Queue()
            runners = [
                asyncio.create_task(self._pump_task_events(plan, task, queue, snapshot))
                for task in executable
            ]
            results: dict[str, TaskRunResult] = {}
            finished = 0
            try:
                while finished < len(runners):
                    event = await queue.get()
                    event_type = event.get("type")
                    if event_type == "plan_task_stream_done":
                        finished += 1
                    elif event_type == "plan_task_result":
                        result = event["result"]
                        results[result.task.id] = result
                    else:
                        yield event
            finally:
                if finished < len(runners):
                    for runner in runners:
                        if not runner.done():
                            runner.cancel()
                await asyncio.gather(*runners, return_exceptions=True)

            for task in executable:
                result = results.get(task.id)
                if result is None:
                    raise RuntimeError(f"task {task.id} did not produce a result")
                async for event in self._apply_task_result(result):
                    yield event

        for task in plan.tasks.values():
            if task.status == TaskStatus.PENDING:
                task.mark_skipped()
        if plan.has_failed():
            plan.mark_failed()
            yield {"type": "text_delta", "text": "计划部分完成，但有任务执行失败。\n\n"}
        elif plan.is_all_completed():
            plan.mark_completed()
            yield {"type": "text_delta", "text": _build_plan_result(plan)}
        else:
            plan.mark_failed()
            yield {
                "type": "text_delta",
                "text": "计划已停滞，因为任务依赖未满足。\n\n",
            }

    async def _apply_task_result(self, result: TaskRunResult) -> AsyncIterator[dict[str, Any]]:
        if result.error:
            result.task.mark_failed(str(result.error))
            yield {
                "type": "plan_task_failed",
                "task_id": result.task.id,
                "child_session_id": result.session_id,
            }
            yield {"type": "text_delta", "text": f"失败 [{result.task.id}]：{result.error}\n\n"}
            return
        result.task.mark_completed(result.text)
        yield {
            "type": "text_delta",
            "text": f"已完成 [{result.task.id}]：{_preview(result.text)}\n\n",
        }
        yield {
            "type": "usage",
            "usage": result.usage.to_dict(),
        }
        yield {
            "type": "plan_task_done",
            "turns": result.turns,
            "tokens": result.tokens,
            "task_id": result.task.id,
            "child_session_id": result.session_id,
        }

    async def _pump_task_events(
        self,
        plan: ExecutionPlan,
        task: Task,
        queue: asyncio.Queue[dict[str, Any]],
        snapshot: SnapshotService,
    ) -> None:
        try:
            async for event in self._execute_task_stream(plan, task, snapshot):
                await queue.put(event)
        finally:
            await queue.put({"type": "plan_task_stream_done", "task_id": task.id})

    async def _execute_task_stream(
        self,
        plan: ExecutionPlan,
        task: Task,
        snapshot: SnapshotService,
    ) -> AsyncIterator[dict[str, Any]]:
        from libi.agent.agent import Agent

        assert self.session_store is not None and self.session_id is not None
        child_session_id = self.session_store.create_session(
            kind="child", parent_session_id=self.session_id
        )
        worker = Agent(
            llm_client=self.llm_client,
            tool_registry=self.tool_registry.clone(),
            system_prompt=self._task_system_prompt(plan, task),
            cwd=self.cwd,
            config=deepcopy(self.config),
            approval_callback=self.approval_callback,
            max_turns=self.max_task_turns,
            session_store=self.session_store,
            session_id=child_session_id,
            turn_snapshot=snapshot,
        )
        task.mark_started()
        yield {
            "type": "plan_task_started",
            "task_id": task.id,
            "task_description": task.description,
            "child_session_id": child_session_id,
        }
        text = ""
        tool_results: list[str] = []
        usage = Usage()
        turns = 0
        canceled = False
        try:
            async with aclosing(
                worker.run(_task_context(plan, task), attachments=self._attachments)
            ) as events:
                async for event in events:
                    if event.get("type") == "text_delta":
                        text += str(event.get("text") or "")
                    elif event.get("type") == "tool_result":
                        content = str(event.get("result") or "")
                        if content:
                            tool_results.append(content)
                        yield _with_task_context(event, task)
                    elif event.get("type") in {"thinking_delta", "tool_call", "context_compressed"}:
                        yield _with_task_context(event, task)
                    elif event.get("type") == "done":
                        turns += int(event.get("total_turns") or 0)
                        usage = usage + Usage.from_mapping(event.get("usage") or {})
                    elif event.get("type") == "error":
                        raise event["error"]
            result_text = text.strip() or "\n".join(tool_results).strip()
            yield {
                "type": "plan_task_result",
                "result": TaskRunResult(
                    task=task,
                    text=result_text,
                    usage=usage,
                    turns=turns,
                    session_id=child_session_id,
                ),
            }
        except (asyncio.CancelledError, GeneratorExit):
            canceled = True
            raise
        except Exception as exc:  # noqa: BLE001
            yield {
                "type": "plan_task_result",
                "result": TaskRunResult(
                    task=task,
                    text="",
                    usage=usage,
                    turns=turns,
                    session_id=child_session_id,
                    error=exc,
                ),
            }
        finally:
            if worker.subagent_manager is not None:
                try:
                    await worker.subagent_manager.drain()
                except Exception as exc:
                    if not canceled:
                        raise
                    logging.getLogger(__name__).warning(
                        "Canceled plan task cleanup failed: %s", type(exc).__name__
                    )

    def _task_system_prompt(self, plan: ExecutionPlan, task: Task) -> str:
        base = PromptAssembler(
            config=self.config,
            cwd=self.cwd,
            tool_names=self.tool_registry.list_names(),
            model=self.llm_client.model_name,
            provider=self.llm_client.provider_name,
        ).build_static()
        return (
            base
            + "\n\n你正在执行 Plan-and-Execute DAG 中的一个任务。\n"
            + f"任务 id：{task.id}\n任务类型：{task.type.value}\n"
            + "请具体完成任务，并在需要时使用工具。"
            + _task_language_instruction(plan.goal)
        )


def _with_task_context(event: dict[str, Any], task: Task) -> dict[str, Any]:
    return {
        **event,
        "phase": "execution",
        "task_id": task.id,
        "task_description": task.description,
    }


def _executable_tasks_in_order(plan: ExecutionPlan) -> list[Task]:
    executable_ids = {task.id for task in plan.executable_tasks()}
    return [plan.tasks[task_id] for task_id in plan.execution_order() if task_id in executable_ids]


def _task_context(plan: ExecutionPlan, task: Task) -> str:
    lines = [
        f"目标：{plan.goal}",
        f"当前任务 [{task.id}]：{task.description}",
        "",
        "已完成的依赖任务结果：",
    ]
    for dep_id in task.dependencies:
        dep = plan.get_task(dep_id)
        if dep and dep.status == TaskStatus.COMPLETED:
            lines.append(f"- [{dep.id}] {dep.description}: {_preview(dep.result, 800)}")
    return "\n".join(lines)


def _build_plan_result(plan: ExecutionPlan) -> str:
    status_labels = {
        TaskStatus.PENDING: "待执行",
        TaskStatus.RUNNING: "执行中",
        TaskStatus.COMPLETED: "已完成",
        TaskStatus.FAILED: "失败",
        TaskStatus.SKIPPED: "已跳过",
    }
    lines = ["计划执行完成。", "", "任务摘要："]
    for task in plan.all_tasks():
        lines.append(f"- [{task.id}] {status_labels[task.status]}：{task.description}")
        if task.result:
            lines.append(f"  结果：{_preview(task.result)}")
    return "\n".join(lines) + "\n"


def _task_language_instruction(goal: str) -> str:
    if any("\u4e00" <= char <= "\u9fff" for char in goal):
        return "\n用户目标包含中文；所有进度说明、分析和最终结果都必须使用中文。"
    return "\nUse the same language as the user's goal for all progress and results."


def _preview(text: str, max_len: int = 160) -> str:
    value = (text or "").replace("\r\n", "\n").strip()
    if len(value) <= max_len:
        return value
    return value[: max_len - 3] + "..."
