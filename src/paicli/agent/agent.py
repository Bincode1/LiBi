"""agent.py — Core terminal Agent inspired by Claude Code.

Integrates LLM API, parses natural-language instructions, schedules command
and file operations via the tool system, and generates streaming responses.

Supports two execution modes:
    - react  : Standard ReAct loop (thought → tool → observation → answer)
    - plan   : Plan-then-execute with a DAG of sub-tasks

Both modes share the same streaming event protocol so callers can render
progress incrementally.
"""

from __future__ import annotations

import asyncio
import hashlib
import json
import time
import uuid
from collections.abc import AsyncIterator, Callable
from contextlib import aclosing, suppress
from dataclasses import asdict, replace
from pathlib import Path
from typing import Any, Literal

from paicli.agent.events import OutputLimitError, collect_query_result
from paicli.agent.plan_execute import PlanExecuteAgent
from paicli.agent.subagents import (
    SUBAGENT_REPORT_TOOLS,
    SubagentManager,
    make_subagent_control_tools,
    make_subagent_tool,
)
from paicli.config import PaiCliConfig
from paicli.context import (
    CompactionClient,
    CompactionCoordinator,
    CompactionError,
    CompactionPlanner,
    CompactionPolicy,
    CompactionResult,
    estimate_message_tokens,
    estimate_text_tokens,
)
from paicli.image import parse_image_references
from paicli.image.processor import IMAGE_PATTERN
from paicli.llm.base import LlmClient, ProviderRequestError
from paicli.llm.pricing import calculate_costs
from paicli.prompt import PromptAssembler
from paicli.session import (
    ContextProjector,
    RequestBuilder,
    SessionEvent,
    SessionStore,
    default_runtime_db_path,
)
from paicli.session.models import ContextView
from paicli.skill import SkillContextBuffer, SkillRegistry
from paicli.snapshot import SnapshotService
from paicli.tools.base import ToolContext, ToolResult
from paicli.tools.call_parsing import (
    finalize_tool_calls,
    merge_tool_delta,
    tool_input,
    tool_name_by_id,
)
from paicli.tools.executor import ToolExecutor
from paicli.tools.registry import ToolRegistry
from paicli.types import Message, QueryResult, Usage
from paicli.web.budget import ResearchBudget
from paicli.web.citations import SUBMIT_RESEARCH_REPORT, CitationBook, report_output_definition

AgentMode = Literal["react", "plan"]


class Agent:
    """A terminal AI agent that connects an LLM to tools for task execution.

    Session events are the canonical conversation record; ContextProjector
    builds model-visible messages for each request.
    All ``run()`` methods yield the same streaming event protocol so that UI
    layers (REPL, CLI, or programmatic) can render progress uniformly.

    Typical usage::

        agent = Agent(
            llm_client=my_client,
            tool_registry=my_registry,
            config=my_config,
            cwd="/workspace",
        )
        async for event in agent.run("list all Python files"):
            if event["type"] == "text_delta":
                print(event["text"], end="")
    """

    def __init__(
        self,
        *,
        llm_client: LlmClient,
        tool_registry: ToolRegistry,
        config: PaiCliConfig,
        cwd: str,
        approval_callback: Callable | None = None,
        mode: AgentMode = "react",
        system_prompt: str | None = None,
        max_turns: int | None = None,
        session_store: SessionStore | None = None,
        session_id: str | None = None,
        subagent_manager: Any | None = None,
        agent_id: str = "root",
        event_sink: Callable[[dict[str, Any]], Any] | None = None,
        steer_inbox: asyncio.Queue[str] | None = None,
        compaction_client: CompactionClient | None = None,
        turn_snapshot: SnapshotService | None = None,
    ) -> None:
        self.llm_client = llm_client
        self.tool_registry = tool_registry
        self.config = config
        self.cwd = cwd
        self.approval_callback = approval_callback
        self.mode = mode
        self.max_turns = max_turns
        if mode not in {"react", "plan"}:
            raise ValueError("agent mode must be react or plan")
        self.session_store = session_store
        self.session_id = session_id
        if mode == "react" and subagent_manager is None:
            subagent_manager = SubagentManager(
                max_active=config.subagents.max_active,
                max_resident=config.subagents.max_resident,
                max_depth=config.subagents.max_depth,
                default_deadline=config.subagents.default_deadline,
                event_queue_size=config.subagents.observation_queue_size,
                drain_timeout=config.subagents.drain_timeout,
            )
        self.subagent_manager = subagent_manager
        self.agent_id = agent_id
        self.event_sink = event_sink
        self.steer_inbox = steer_inbox
        self.compaction_client = compaction_client
        self._turn_snapshot = turn_snapshot
        self.last_compaction: dict[str, Any] | None = None
        self._context_projector = ContextProjector()
        self._request_builder = RequestBuilder()

        # Build the base system prompt from personality profile and config.
        self.system_prompt = (
            system_prompt
            or PromptAssembler(
                config=config,
                cwd=cwd,
                tool_names=tool_registry.list_names(),
                model=llm_client.model_name,
                provider=llm_client.provider_name,
            ).build_static()
        )

        self.skill_context_buffer = SkillContextBuffer()

        # Accumulated usage / cost across all turns of the session.
        self.last_usage = Usage()
        self.last_cost: dict[str, Any] = {}

        # Validate configuration.
        self._validate_config()

    # ------------------------------------------------------------------
    # Public API — run the agent
    # ------------------------------------------------------------------

    async def run(
        self, message: str, *, turn_id: str | None = None
    ) -> AsyncIterator[dict[str, Any]]:
        """Execute *message* and yield streaming events.

        Events follow this protocol (``type`` field determines the shape):

        ``text_delta``
            {"type": "text_delta", "text": "..."}
        ``thinking_delta``
            {"type": "thinking_delta", "thinking": "..."}
        ``tool_call``
            {"type": "tool_call", "name": "...", "input": {...}}
        ``tool_result``
            {"type": "tool_result", "name": "...", "result": "...", "is_error": bool}
        ``usage``
            {"type": "usage", "usage": {...}}
        ``turn_complete``
            {"type": "turn_complete", "turn": int, "stop_reason": str}
        ``context_compressed``
            {"type": "context_compressed", "before_tokens": ..., "after_tokens": ...,
             "summarized_messages": int}
        ``error``
            {"type": "error", "error": Exception}
        ``done``
            {"type": "done", "total_turns": int, "total_tokens": int,
             "usage": {...}, "cost": {...}, "messages": [Message, ...]}
        """
        session_id = self._ensure_session()
        assert self.session_store is not None
        owns_turn = turn_id is None
        if turn_id is None:
            turn_id = f"turn_{uuid.uuid4().hex}"
            self.session_store.start_turn(session_id, turn_id, mode=self.mode, message=message)
        elif self.session_store.active_turn_step(session_id)[0] != turn_id:
            raise RuntimeError("The accepted turn is no longer active.")
        snapshot = None
        try:
            if self.mode == "react":
                snapshot = self._turn_snapshot or SnapshotService(self.cwd)
            stream = (
                self._run_react(message, snapshot, turn_id)
                if snapshot is not None
                else self._run_plan(message, turn_id)
            )
            async with aclosing(stream) as events:
                async for event in events:
                    if self.event_sink is not None:
                        await self.event_sink(event)
                    yield event
        except (asyncio.CancelledError, GeneratorExit):
            cleanup_error = None
            try:
                if self.subagent_manager is not None:
                    async with asyncio.timeout(self.config.subagents.drain_timeout):
                        await self.subagent_manager.cancel_for_parent(self.agent_id)
            except Exception as exc:
                cleanup_error = type(exc).__name__
            finally:
                self._close_canceled_turn(turn_id, cleanup_error)
            raise
        except Exception as exc:
            active_turn_id, step_id = self.session_store.active_turn_step(session_id)
            if active_turn_id == turn_id:
                failure: dict[str, Any] = {"reason": type(exc).__name__}
                if isinstance(exc, TimeoutError):
                    failure = {"reason": "deadline_exceeded"}
                elif isinstance(exc, CompactionError) and exc.provider_error is not None:
                    failure = {
                        "reason": "provider_error",
                        "provider_error": exc.provider_error,
                        "purpose": "compaction",
                    }
                if step_id:
                    self._close_pending_tools(
                        turn_id, step_id, "Tool result unknown after failure."
                    )
                    self._log("step/failed", failure, turn_id=turn_id, step_id=step_id)
                self._log("turn/failed", failure, turn_id=turn_id)
            raise
        finally:
            try:
                if snapshot is not None and self._turn_snapshot is None:
                    with suppress(Exception):
                        snapshot.finish_turn()
            finally:
                if owns_turn:
                    self.session_store.release_turn(session_id, turn_id)

    def _close_canceled_turn(self, turn_id: str, cleanup_error: str | None) -> None:
        assert self.session_store is not None and self.session_id is not None
        active_turn_id, step_id = self.session_store.active_turn_step(self.session_id)
        if active_turn_id != turn_id:
            return
        if step_id:
            self._close_pending_tools(turn_id, step_id, "Tool call canceled.")
        detail = {"reason": "cancelled"}
        if cleanup_error is not None:
            detail["cleanup_error"] = cleanup_error
        self._log("turn/canceled", detail, turn_id=turn_id)

    def _close_pending_tools(self, turn_id: str, step_id: str, content: str) -> None:
        assert self.session_store is not None and self.session_id is not None
        for call_id, name in self.session_store.pending_tool_calls(
            self.session_id, turn_id, step_id
        ).items():
            self._log(
                "tool/result",
                {"name": name, "content": content, "is_error": True},
                turn_id=turn_id,
                step_id=step_id,
                tool_call_id=call_id,
                source="tool-executor",
            )

    async def run_complete(self, message: str) -> QueryResult:
        """Run the agent and collect all events into a single result."""
        return await collect_query_result(self.run(message))

    async def compact(
        self,
        focus: str | None = None,
        *,
        trigger: str = "manual",
        force: bool | None = None,
        allow_active_turn: bool = False,
        input_context_hash: str | None = None,
    ) -> CompactionResult:
        """Compact the durable session history through one isolated LLM request.

        This is a maintenance operation: it never creates a fake turn or chat
        message.  The session lease fences model-visible writes while the
        provider request is in flight and the checkpoint commit is atomic.
        """
        if self.config.memory.compression_mode == "disabled":
            raise CompactionError("context compaction is disabled", code="disabled")
        session_id = self.session_id
        if not session_id or self.session_store is None:
            raise CompactionError("no active session", code="no_session")
        state = self.session_store.state(session_id)
        if state.active_turn_id and not allow_active_turn:
            raise CompactionError("session is busy", code="busy")

        events = self.session_store.events(session_id)
        view = self._context_projector.project_session(events, session_id=session_id)
        policy = self._compaction_policy()
        planner = CompactionPlanner(policy)
        should_force = trigger == "manual" if force is None else force
        plan = planner.plan(
            view.messages,
            sources=view.message_sources,
            trigger=trigger,
            force=should_force,
        )
        if not plan.has_selection:
            return CompactionResult(
                None,
                tuple(view.messages),
                tuple(view.message_sources),
                plan,
                f"compact_attempt_{uuid.uuid4().hex}",
                None,
            )

        attempt_id = f"compact_attempt_{uuid.uuid4().hex}"
        lease = self.session_store.acquire_compaction_lease(
            session_id,
            attempt_id=attempt_id,
            base_sequence=view.through_sequence,
            surface_hash=view.context_hash,
        )

        def write_event(event_type: str, payload: dict[str, Any]) -> Any:
            if event_type == "request/attempt_failed":
                return self.session_store.append(
                    session_id,
                    event_type,
                    payload,
                    turn_id=state.active_turn_id,
                    step_id=state.active_step_id,
                    source="compaction-coordinator",
                )
            if event_type == "compaction/started":
                return self.session_store.append(
                    session_id,
                    event_type,
                    payload,
                    source="compaction-coordinator",
                    lease_token=lease.owner_token,
                )
            if event_type == "compaction/failed":
                return self.session_store.fail_compaction(
                    lease.owner_token,
                    payload,
                    source="compaction-coordinator",
                )
            # The atomic checkpoint writer below is the single success event.
            return None

        def commit_checkpoint(payload: dict[str, Any]) -> Any:
            return self.session_store.commit_compaction_checkpoint(
                lease.owner_token,
                payload,
                source="compaction-coordinator",
            )

        coordinator = CompactionCoordinator(
            policy=policy,
            client=self.compaction_client or self.llm_client,
            provider=self.config.compaction.provider or self.llm_client.provider_name,
            model=self.config.compaction.model or self.llm_client.model_name,
            max_output_tokens=int(self.config.compaction.max_output_tokens),
            temperature=float(self.config.compaction.temperature),
            event_writer=write_event,
        )
        try:
            result = await coordinator.compact(
                view.messages,
                attempt_id=attempt_id,
                sources=view.message_sources,
                input_context_hash=input_context_hash or view.context_hash,
                projection_kind="session",
                through_sequence=view.through_sequence,
                trigger=trigger,
                focus=focus,
                force=should_force,
                checkpoint_writer=commit_checkpoint,
            )
            self.last_compaction = {
                "attempt_id": result.attempt_id,
                "compaction_id": result.compaction_id,
                "trigger": trigger,
                "before_tokens": result.plan.estimated_tokens_before,
                "after_tokens": result.plan.estimated_tokens_after,
                "usage": result.usage.to_dict() if result.usage else None,
            }
            return result
        except asyncio.CancelledError:
            with suppress(Exception):
                self.session_store.fail_compaction(
                    lease.owner_token,
                    {
                        "attempt_id": attempt_id,
                        "trigger": trigger,
                        "failure_code": "cancelled",
                        "retryable_by_user": True,
                        "cost_may_be_charged": True,
                    },
                )
            raise
        except Exception:
            with suppress(Exception):
                self.session_store.fail_compaction(
                    lease.owner_token,
                    {
                        "attempt_id": attempt_id,
                        "trigger": trigger,
                        "failure_code": "coordinator_error",
                        "retryable_by_user": True,
                        "cost_may_be_charged": False,
                    },
                )
            raise

    # ------------------------------------------------------------------
    # History management
    # ------------------------------------------------------------------

    def clear_history(self) -> None:
        """Start a fresh visible conversation while retaining its prior event log."""
        self.session_id = self._create_new_session()
        self.skill_context_buffer.clear()
        self.last_usage = Usage()
        self.last_cost = {}

    # ------------------------------------------------------------------
    # Mode runners
    # ------------------------------------------------------------------

    async def _run_react(
        self, message: str, snapshot: SnapshotService, turn_id: str
    ) -> AsyncIterator[dict[str, Any]]:
        """Standard ReAct loop (the default and most common mode)."""
        session_id = self.session_id
        store = self.session_store
        assert store is not None and session_id is not None
        original = message
        candidate_message = _prepend_skill_candidates(message, self.cwd, self.config)
        candidate_prefix, separator, request_message = candidate_message.partition(
            "\n\n---\nUser request:\n"
        )
        if separator:
            message = request_message
            self._log(
                "skill/context",
                {
                    "id": "skill-candidates",
                    "source": "SkillRegistry",
                    "content": candidate_prefix,
                    "visibility": "model",
                    "state": "candidate",
                },
                turn_id=turn_id,
                source="skill-registry",
            )
        else:
            message = candidate_message
        with_skill_context = _prepend_skill_context(message, self.skill_context_buffer)
        loaded_prefix, separator, request_message = with_skill_context.partition(
            "\n\n---\nUser request:\n"
        )
        if separator:
            message = request_message
            self._log(
                "skill/context",
                {
                    "id": "loaded-skill-context",
                    "source": "skill-context-buffer",
                    "content": loaded_prefix,
                    "visibility": "model",
                },
                turn_id=turn_id,
                source="skill-context-buffer",
            )
        else:
            message = with_skill_context
        tool_defs = self.tool_registry.definitions()
        if self.mode == "react":
            subagent_tools = [make_subagent_tool(), *make_subagent_control_tools()]
            allowlist = self.config.policy.subagent_tool_allowlist
            if allowlist is None or self.agent_id == "root":
                self.tool_registry.register_all(subagent_tools)
            else:
                self.tool_registry.register_all(
                    [tool for tool in subagent_tools if tool.name in set(allowlist)]
                )
            tool_defs = self.tool_registry.definitions()
        executor = ToolExecutor(
            self.tool_registry,
            audit_log_path=self.config.policy.audit_log_path,
        )
        subagent_config = self.config.subagents
        llm_policy = self._compaction_policy()
        try:
            input_limit = llm_policy.normal_input_budget
        except ValueError:
            input_limit = 0
        citations = CitationBook()

        def tool_result_payload(result: ToolResult, name: str, content: str) -> dict:
            payload = {"name": name, "content": content, "is_error": result.is_error}
            if result.sources:
                sources = []
                for source in result.sources:
                    record = asdict(source)
                    if name in SUBAGENT_REPORT_TOOLS and (source_id := citations.id_for(source)):
                        record["source_id"] = source_id
                    sources.append(record)
                payload["sources"] = sources
            return payload

        context = ToolContext(
            cwd=self.cwd,
            config=self.config,
            approval_callback=self.approval_callback,
            skill_context_buffer=self.skill_context_buffer,
            subagent_manager=self.subagent_manager,
            agent_id=self.agent_id,
            parent_agent_id=(
                self.subagent_manager.activations[self.agent_id].parent_id
                if self.subagent_manager is not None
                and self.agent_id in self.subagent_manager.activations
                else None
            ),
            session_store=store,
            session_id=session_id,
            turn_id=turn_id,
            llm_client=self.llm_client,
            tool_registry=self.tool_registry,
            citations=citations,
            research_budget=(
                ResearchBudget(
                    subagent_config.research_max_searches,
                    subagent_config.research_max_fetches,
                    subagent_config.research_max_chars,
                    tokens_left=input_limit // 4,
                    deadline=time.monotonic() + subagent_config.research_timeout,
                )
                if self.agent_id != "root"
                else None
            ),
            before_workspace_mutation=snapshot.checkpoint_turn,
            status_callback=(lambda event: self.subagent_manager.tool_status(self.agent_id, event))
            if self.subagent_manager is not None
            else None,
        )

        # Build the dynamic part of the system prompt (tool list, cwd, etc.).
        dynamic_prompt = PromptAssembler(
            config=self.config,
            cwd=self.cwd,
            tool_names=self.tool_registry.list_names(),
            model=self.llm_client.model_name,
            provider=self.llm_client.provider_name,
        ).build_dynamic(original)

        effective_system = f"{self.system_prompt}\n\n{dynamic_prompt}".strip()
        if context.research_budget is not None and (
            self.tool_registry.get("web_search") or self.tool_registry.get("web_fetch")
        ):
            effective_system += (
                "\n\nWeb research: start with 2–4 distinct queries when useful. "
                "Use saved excerpts and read more of a page only when needed. "
                "Summarize the available evidence when the context or time budget is reached. "
                "Return concise findings with source links and note any gaps."
            )
        self._log(
            "system/section",
            {
                "section_id": f"effective-system-{turn_id}",
                "kind": "base",
                "scope": "turn",
                "priority": 100,
                "content": effective_system,
                "source": "PromptAssembler",
                "version": hashlib.sha256(effective_system.encode("utf-8")).hexdigest(),
                "stable": False,
            },
            turn_id=turn_id,
            source="prompt-assembler",
        )

        total_usage = Usage()
        turn = 0
        finished = False
        manager = self.subagent_manager
        if manager is not None:
            manager.session_store = store
            manager.redactor = store.redactor
            manager.restore_catalog(self.agent_id, session_id)
            manager.recover_orphaned_children(self.agent_id, session_id)
        context_events: list[SessionEvent] = []
        context_cursor = 0
        context_secrets = store.redactor.sensitive_values
        previous_usage = 0
        previous_estimate = 0
        previous_checkpoint: str | None = None

        def estimate_input(view: ContextView) -> tuple[int, int]:
            estimated = llm_policy.estimate_request(
                view.messages, system_prompt=view.system_prompt, tools=view.tools
            ).tokens
            if previous_usage and previous_checkpoint == view.compaction_id:
                return estimated, max(0, previous_usage + estimated - previous_estimate)
            return estimated, estimated

        def refresh_context(step_id: str) -> list[SessionEvent]:
            nonlocal context_events, context_cursor, context_secrets
            if context_secrets != store.redactor.sensitive_values:
                # A newly configured secret also applies to older cached events.
                context_secrets = store.redactor.sensitive_values
                context_events = []
                context_cursor = 0
            incoming = store.events(session_id, after_sequence=context_cursor)
            if incoming:
                context_cursor = incoming[-1].sequence
                context_events = self._context_projector.working_events(
                    context_events + incoming, turn_id=turn_id, step_id=step_id
                )
            return context_events

        async def consume_steer(step_id: str) -> bool:
            if self.steer_inbox is None:
                return False
            consumed = False
            while True:
                try:
                    content = self.steer_inbox.get_nowait()
                except asyncio.QueueEmpty:
                    break
                self._log(
                    "user/message",
                    {"content": content},
                    turn_id=turn_id,
                    step_id=step_id,
                    source="subagent-steer",
                )
                consumed = True
            return consumed

        while self.max_turns is None or turn < self.max_turns:
            turn += 1
            text = ""
            thinking = ""
            stop_reason = "end_turn"
            turn_usage = Usage()
            tool_states: dict[int, dict[str, Any]] = {}
            step_id = f"step_{uuid.uuid4().hex}"
            citing = bool(citations.available)
            self._log(
                "step/started",
                {"mode": "react"},
                turn_id=turn_id,
                step_id=step_id,
            )
            self._log(
                "tool/schema_snapshot",
                {
                    "tools": sorted(
                        [*tool_defs, report_output_definition()],
                        key=lambda tool: tool["function"]["name"],
                    )
                    if citing
                    else tool_defs
                },
                turn_id=turn_id,
                step_id=step_id,
                source="tool-registry",
            )
            self._log(
                "runtime/context",
                {
                    "context_id": f"runtime_{step_id}",
                    "source": "workspace",
                    "scope": "step",
                    "version": "v1",
                    "visibility": "model",
                    "content": {
                        "workspace_name": Path(self.cwd).name or self.cwd,
                        "relative_path": ".",
                        "route_name": self.llm_client.provider_name,
                        "permission_mode": self.config.policy.hitl_mode,
                    },
                },
                turn_id=turn_id,
                step_id=step_id,
                source="runtime-context",
            )

            projection = {
                "scope": "session",
                "route": self.llm_client.provider_name,
                "adapter_capabilities": {
                    "provider": self.llm_client.provider_name,
                    "supports_images": bool(getattr(self.llm_client, "supports_images", False)),
                },
                "budget": self._budget_metadata(),
            }

            def project_current(
                step_id: str = step_id, projection: dict[str, Any] = projection
            ) -> ContextView:
                return self._context_projector.project(
                    refresh_context(step_id),
                    session_id=session_id,
                    turn_id=turn_id,
                    step_id=step_id,
                    **projection,
                )

            current_view = project_current()
            raw_estimate, input_tokens = estimate_input(current_view)
            pressure_limit = int(input_limit * llm_policy.threshold_ratio)
            if (
                pressure_limit
                and input_tokens >= pressure_limit
                and self.config.memory.compression_mode == "llm"
                and callable(getattr(self.compaction_client or self.llm_client, "complete", None))
            ):
                compression = await self.compact(
                    trigger="pressure",
                    force=True,
                    allow_active_turn=True,
                    input_context_hash=current_view.context_hash,
                )
                if compression.compaction_id:
                    yield _annotate_event(
                        {
                            "type": "context_compressed",
                            "before_tokens": compression.plan.estimated_tokens_before,
                            "after_tokens": compression.plan.estimated_tokens_after,
                            "summarized_messages": compression.plan.selected_message_count,
                            "compaction_id": compression.compaction_id,
                            "usage": compression.usage.to_dict() if compression.usage else None,
                        },
                        session_id,
                        turn_id,
                        step_id,
                    )
                current_view = project_current()
                raw_estimate, input_tokens = estimate_input(current_view)

            if context.research_budget is not None:
                context.research_budget.request_tokens_left = max(0, input_limit - input_tokens)

            # Budget pressure uses raw messages. Resolve image references only after compaction.
            if any(
                item.role == "user"
                and isinstance(item.content, str)
                and IMAGE_PATTERN.search(item.content)
                for item in current_view.messages
            ):
                current_view = self._context_projector.project(
                    refresh_context(step_id),
                    session_id=session_id,
                    turn_id=turn_id,
                    step_id=step_id,
                    message_transform=lambda item: _transform_message_images(item, self.cwd),
                    **projection,
                )
            request = self._request_builder.build(
                current_view,
                model=self.llm_client.model_name,
                provider=self.llm_client.provider_name,
                max_tokens=self.config.llm.max_tokens,
                temperature=self.config.llm.temperature,
            )
            context_tokens = (
                estimate_text_tokens(request.system_prompt)
                + sum(estimate_message_tokens(item) for item in request.messages)
                + estimate_text_tokens(json.dumps(list(request.tools), ensure_ascii=False))
            )
            role_counts: dict[str, int] = {}
            for item in request.messages:
                role_counts[item.role] = role_counts.get(item.role, 0) + 1
            context_summary = {
                "step_id": step_id,
                "message_count": len(request.messages),
                "role_counts": role_counts,
                "tool_count": len(request.tools),
                "estimated_input_tokens": context_tokens,
                "context_window": int(self.llm_client.max_context_window),
                "compaction_id": request.metadata["compaction_id"],
                "through_sequence": request.metadata["through_sequence"],
                "projection": projection,
            }
            yield _annotate_event(
                {"type": "context_summary", **context_summary},
                session_id,
                turn_id,
                step_id,
            )
            self._log(
                "context/summary",
                context_summary,
                turn_id=turn_id,
                step_id=step_id,
                source="context-projector",
            )
            self._log(
                "request/prepared",
                {
                    "request_id": request.request_id,
                    "context_hash": request.context_hash,
                    "projection_version": request.metadata["projection_version"],
                    "source_sequences": request.metadata["source_sequences"],
                    "compaction_id": request.metadata["compaction_id"],
                    "through_sequence": request.metadata["through_sequence"],
                    "projection": context_summary["projection"],
                    "model": request.model,
                    "provider": request.provider,
                    "max_tokens": request.max_tokens,
                    "temperature": request.temperature,
                    "tool_count": len(request.tools),
                    "context_summary": context_summary,
                },
                turn_id=turn_id,
                step_id=step_id,
                source="request-builder",
            )
            # Call the LLM.
            try:
                response_events = self.llm_client.chat(
                    list(request.messages),
                    list(request.tools),
                    system_prompt=request.system_prompt,
                )
                async for event in response_events:
                    event_type = event.get("type")
                    if event_type == "text_delta":
                        delta = str(event.get("text") or "")
                        text += delta
                        if not citing:
                            yield _annotate_event(
                                {"type": "text_delta", "text": delta}, session_id, turn_id, step_id
                            )
                    elif event_type == "thinking_delta":
                        delta = str(event.get("thinking") or "")
                        thinking += delta
                        yield _annotate_event(
                            {"type": "thinking_delta", "thinking": delta},
                            session_id,
                            turn_id,
                            step_id,
                        )
                    elif event_type == "tool_call_delta":
                        merge_tool_delta(tool_states, event["tool_call"])
                    elif event_type == "message_end":
                        stop_reason = str(event.get("stop_reason") or "end_turn")
                    elif event_type == "usage":
                        usage = Usage.from_mapping(event.get("usage") or {})
                        turn_usage = turn_usage + usage
                        yield _annotate_event(
                            {"type": "usage", "usage": usage.to_dict()},
                            session_id,
                            turn_id,
                            step_id,
                        )
                    elif event_type == "provider_attempt_failed":
                        self._log(
                            "request/attempt_failed",
                            {
                                **event["diagnostic"],
                                "request_id": request.request_id,
                                "provider": request.provider,
                                "model": request.model,
                            },
                            turn_id=turn_id,
                            step_id=step_id,
                            source="provider",
                        )
                    elif event_type == "error":
                        error = event["error"]
                        failure: dict[str, Any] = {"reason": "provider_error"}
                        if isinstance(error, ProviderRequestError):
                            failure["provider_error"] = error.details
                        self._log(
                            "request/failed",
                            {
                                "request_id": request.request_id,
                                "error_type": type(error).__name__,
                                **failure,
                            },
                            turn_id=turn_id,
                            step_id=step_id,
                        )
                        self._log(
                            "step/failed",
                            failure,
                            turn_id=turn_id,
                            step_id=step_id,
                        )
                        self._log(
                            "turn/failed",
                            failure,
                            turn_id=turn_id,
                        )
                        yield _annotate_event(
                            {"type": "error", "error": error}, session_id, turn_id, step_id
                        )
                        return
            except (asyncio.CancelledError, GeneratorExit):
                if close := getattr(response_events, "aclose", None):
                    await close()
                raise
            except Exception as exc:
                self._log(
                    "request/failed",
                    {"request_id": request.request_id, "error_type": type(exc).__name__},
                    turn_id=turn_id,
                    step_id=step_id,
                )
                self._log(
                    "step/failed",
                    {"reason": "provider_exception"},
                    turn_id=turn_id,
                    step_id=step_id,
                )
                self._log(
                    "turn/failed",
                    {"reason": "provider_exception"},
                    turn_id=turn_id,
                )
                raise

            total_usage = total_usage + turn_usage
            previous_usage = max(turn_usage.input_tokens, turn_usage.cache_context_tokens)
            previous_estimate = raw_estimate
            previous_checkpoint = current_view.compaction_id
            truncated = stop_reason == "max_tokens"
            tool_calls = [] if truncated else finalize_tool_calls(tool_states)
            if citing and not truncated:
                # The final output declaration is a response format. Only normal tools
                # reach the executor, and there is no follow-up request for a report.
                ordinary = [
                    call
                    for call in tool_calls
                    if call.get("function", {}).get("name") != SUBMIT_RESEARCH_REPORT
                ]
                if not ordinary:
                    submission = tool_input(tool_calls[0]) if tool_calls else {}
                    text = citations.render(submission, text)
                    stop_reason = "end_turn"
                tool_calls = ordinary
                if text:
                    yield _annotate_event(
                        {"type": "text_delta", "text": text}, session_id, turn_id, step_id
                    )
            for call in tool_calls:
                if store.redactor.clean(call["id"]) != call["id"]:
                    # Use one opaque ID for the call and its result before either is persisted.
                    call["id"] = f"call_{uuid.uuid4().hex}"
            assistant_msg = Message(
                role="assistant",
                content=text or "",
                tool_calls=tool_calls,
            )
            if thinking and not text:
                assistant_msg.content = ""
            self._log(
                "assistant/message",
                {"content": assistant_msg.content, "tool_calls": tool_calls},
                turn_id=turn_id,
                step_id=step_id,
                source="provider",
            )
            self._log(
                "request/completed",
                {"request_id": request.request_id, "usage": turn_usage.to_dict()},
                turn_id=turn_id,
                step_id=step_id,
                source="provider",
            )
            if truncated:
                failure = {"reason": "max_output_tokens"}
                self._log("step/failed", failure, turn_id=turn_id, step_id=step_id)
                self._log("turn/failed", failure, turn_id=turn_id)
                self.last_usage = total_usage
                yield _annotate_event(
                    {
                        "type": "error",
                        "error": OutputLimitError(store.redactor.clean(text)),
                    },
                    session_id,
                    turn_id,
                    step_id,
                )
                return
            yield _annotate_event(
                {"type": "turn_complete", "turn": turn, "stop_reason": stop_reason},
                session_id,
                turn_id,
                step_id,
            )

            steered = await consume_steer(step_id) if not tool_calls else False
            if steered and stop_reason != "tool_use" and not tool_calls:
                self._log(
                    "step/completed",
                    {"reason": "steered"},
                    turn_id=turn_id,
                    step_id=step_id,
                )
                continue

            # If the model didn't request any tools, we're done.
            if stop_reason != "tool_use" and not tool_calls:
                self._log(
                    "step/completed",
                    {},
                    turn_id=turn_id,
                    step_id=step_id,
                )
                if manager is not None:
                    manager.raise_background_errors()
                finished = True
                break

            # Run tools.
            for call in tool_calls:
                name = call.get("function", {}).get("name", "unknown")
                call_id = str(call.get("id") or "")
                self._log(
                    "tool/execution_started",
                    {"name": name},
                    turn_id=turn_id,
                    step_id=step_id,
                    tool_call_id=call_id,
                )
                yield _annotate_event(
                    {
                        "type": "tool_call",
                        "name": name,
                        "input": tool_input(call),
                        "tool_call_id": call_id,
                    },
                    session_id,
                    turn_id,
                    step_id,
                )

            completed_results: list[ToolResult] = []
            report_calls = sum(
                call.get("function", {}).get("name") in SUBAGENT_REPORT_TOOLS for call in tool_calls
            )
            report_budget = max(
                0,
                pressure_limit
                - max(input_tokens, turn_usage.input_tokens)
                - turn_usage.output_tokens,
            )
            execution = executor.execute_all(
                tool_calls,
                replace(
                    context,
                    step_id=step_id,
                    subagent_report_tokens=report_budget // max(1, report_calls),
                ),
                on_result=completed_results.append,
            )
            try:
                if report_calls and manager is not None and manager.event_callback is None:
                    # SDK consumers receive child progress while the tool result is pending.
                    tools_task = asyncio.create_task(execution)
                    progress_task = asyncio.create_task(manager.next_event(self.agent_id))
                    try:
                        while not tools_task.done():
                            done, _ = await asyncio.wait(
                                (tools_task, progress_task), return_when=asyncio.FIRST_COMPLETED
                            )
                            if progress_task in done:
                                yield _annotate_event(
                                    progress_task.result(), session_id, turn_id, step_id
                                )
                                progress_task = asyncio.create_task(
                                    manager.next_event(self.agent_id)
                                )
                        tool_results = tools_task.result()
                    finally:
                        for task in (tools_task, progress_task):
                            if not task.done():
                                task.cancel()
                        await asyncio.gather(tools_task, progress_task, return_exceptions=True)
                    if not progress_task.cancelled():
                        yield _annotate_event(progress_task.result(), session_id, turn_id, step_id)
                    for event in manager.drain_events(self.agent_id):
                        yield _annotate_event(event, session_id, turn_id, step_id)
                else:
                    tool_results = await execution
                    if report_calls and manager is not None:
                        manager.drain_events(self.agent_id)
            except (Exception, asyncio.CancelledError, GeneratorExit):
                for result in completed_results:
                    self._log(
                        "tool/result",
                        tool_result_payload(
                            result,
                            tool_name_by_id(tool_calls, result.tool_use_id or ""),
                            result.content,
                        ),
                        turn_id=turn_id,
                        step_id=step_id,
                        tool_call_id=result.tool_use_id,
                        source="tool-executor",
                    )
                raise
            loaded_skill_context = _drain_skill_context(self.skill_context_buffer)
            load_skill_ids = [
                str(call.get("id") or "")
                for call in tool_calls
                if str(call.get("function", {}).get("name") or "") == "load_skill"
            ]
            injection_target = load_skill_ids[-1] if load_skill_ids else ""
            injected = False

            for result in tool_results:
                tool_name = tool_name_by_id(tool_calls, result.tool_use_id or "")
                model_content = result.content
                if loaded_skill_context and result.tool_use_id == injection_target:
                    model_content = f"{model_content}\n\n{loaded_skill_context}"
                    injected = True
                self._log(
                    "tool/result",
                    tool_result_payload(result, tool_name, model_content),
                    turn_id=turn_id,
                    step_id=step_id,
                    tool_call_id=result.tool_use_id,
                    source="tool-executor",
                )

            if manager is not None:
                manager.raise_background_errors()
            for result in tool_results:
                yield _annotate_event(
                    {
                        "type": "tool_result",
                        "name": tool_name_by_id(tool_calls, result.tool_use_id or ""),
                        "result": result.content,
                        "is_error": result.is_error,
                        "tool_call_id": result.tool_use_id,
                    },
                    session_id,
                    turn_id,
                    step_id,
                )

            if loaded_skill_context:
                self._log(
                    "skill/context",
                    {
                        "id": "loaded-skill-context",
                        "source": "load_skill",
                        "content": loaded_skill_context,
                        "visibility": "internal" if injected else "model",
                    },
                    turn_id=turn_id,
                    step_id=step_id,
                    source="skill-loader",
                )

            self._log(
                "step/completed",
                {"tool_call_count": len(tool_calls)},
                turn_id=turn_id,
                step_id=step_id,
            )
            await consume_steer(step_id)

            if loaded_skill_context and not injected:
                # The model-visible skill context is projected from the event above.
                pass

        if self.max_turns is not None and not finished:
            message = (
                f"Agent reached its {self.max_turns}-step limit while work remained, "
                "before producing a final answer. Send a follow-up to continue."
            )
            self._log(
                "turn/failed",
                {"reason": "max_steps_reached", "steps": turn},
                turn_id=turn_id,
            )
            self.last_usage = total_usage
            yield _annotate_event(
                {"type": "error", "error": RuntimeError(message)},
                session_id,
                turn_id,
                step_id,
            )
            return

        final_events = refresh_context(step_id)
        completed_step = next(
            (
                event.step_id
                for event in reversed(final_events)
                if event.event_type == "step/completed" and event.turn_id == turn_id
            ),
            None,
        )
        if completed_step is None:
            raise RuntimeError("internal error: no completed step found for the finished turn")
        final_view = self._context_projector.project(
            final_events,
            session_id=session_id,
            turn_id=turn_id,
            step_id=completed_step,
        )
        self._log(
            "turn/completed",
            {"total_tokens": total_usage.total_tokens, "steps": turn},
            turn_id=turn_id,
        )
        self.last_usage = total_usage

        done_event: dict[str, Any] = {
            "type": "done",
            "total_turns": turn,
            "total_tokens": total_usage.total_tokens,
            "usage": total_usage.to_dict(),
            "messages": list(final_view.messages),
            "session_id": session_id,
            "turn_id": turn_id,
        }
        costs = calculate_costs(self.llm_client, total_usage)
        if costs:
            done_event["cost"] = costs
            self.last_cost = costs
        yield done_event

    def _ensure_session(self) -> str:
        if self.session_store is None:
            self.session_store = SessionStore(
                default_runtime_db_path(),
                workspace=self.cwd,
                sensitive_values=(self.config.llm.api_key,),
            )
        self.session_store.redactor.add_sensitive_values((self.config.llm.api_key,))
        if self.session_id is None:
            self.session_id = self.session_store.create_session()
        return self.session_id

    def _log(
        self,
        event_type: str,
        payload: dict[str, Any],
        *,
        session_id: str | None = None,
        turn_id: str | None = None,
        step_id: str | None = None,
        tool_call_id: str | None = None,
        source: str | None = None,
    ) -> None:
        """Append an event to the session store.

        ``session_id`` defaults to the agent's active session. No-op when there
        is no session store (e.g. stateless ``query()`` invocations).
        """
        if self.session_store is None:
            return
        sid = session_id or self.session_id
        if sid is None:
            return
        self.session_store.append(
            sid,
            event_type,
            payload,
            turn_id=turn_id,
            step_id=step_id,
            tool_call_id=tool_call_id,
            source=source or "agent",
        )

    def _compaction_policy(self) -> CompactionPolicy:
        return CompactionPolicy(
            context_window=int(self.llm_client.max_context_window),
            normal_max_output_tokens=int(self.config.llm.max_tokens),
            threshold_ratio=float(self.config.memory.compression_threshold),
            compression_target=float(self.config.memory.compression_target),
            reserve_tokens=int(self.config.memory.compression_reserve_tokens),
            min_recent_messages=int(self.config.memory.min_recent_messages),
            max_tool_result_chars_in_input=int(
                self.config.compaction.max_tool_result_chars_in_input
            ),
        )

    def _budget_metadata(self) -> dict[str, int]:
        return {
            "historical_tool_result_chars": 2_000,
            "context_window": int(self.llm_client.max_context_window),
            "max_output_tokens": int(self.config.llm.max_tokens),
            "reserve_tokens": int(self.config.memory.compression_reserve_tokens),
            "compression_threshold_micros": round(
                float(self.config.memory.compression_threshold) * 1_000_000
            ),
        }

    def _create_new_session(self) -> str:
        if self.session_store is None:
            self.session_store = SessionStore(
                default_runtime_db_path(),
                workspace=self.cwd,
                sensitive_values=(self.config.llm.api_key,),
            )
        return self.session_store.create_session()

    async def _run_plan(self, message: str, turn_id: str) -> AsyncIterator[dict[str, Any]]:
        """Plan-then-execute mode: the planner creates a DAG, then workers run it."""
        session_id = self._ensure_session()
        agent = PlanExecuteAgent(
            llm_client=self.llm_client,
            tool_registry=self.tool_registry,
            config=self.config,
            cwd=self.cwd,
            approval_callback=self.approval_callback,
            max_task_turns=self.max_turns,
            session_store=self.session_store,
            session_id=session_id,
        )
        async with aclosing(agent.run(message, turn_id=turn_id)) as events:
            async for event in events:
                if event.get("type") == "done":
                    self.last_usage = Usage.from_mapping(event.get("usage") or {})
                    self.last_cost = dict(event.get("cost") or {})
                yield event

    # ------------------------------------------------------------------
    # Configuration helpers
    # ------------------------------------------------------------------

    def _validate_config(self) -> None:
        """Raise early if the configuration is obviously wrong."""
        if self.max_turns is not None and self.max_turns <= 0:
            raise ValueError("max_turns must be positive when provided")
        if not self.llm_client.max_context_window:
            raise ValueError(
                "LLM context window is not configured. "
                "Set a PAICLI_CONTEXT_WINDOW environment variable or configure "
                "it in ~/.paicli/config.json."
            )


# ------------------------------------------------------------------
# ReAct event helpers
# ------------------------------------------------------------------


def _transform_message_images(message: Message, cwd: str) -> Message:
    content = message.content
    if message.role == "user" and isinstance(content, str):
        content = parse_image_references(content, cwd)
    return Message(
        role=message.role,
        content=content,
        name=message.name,
        tool_call_id=message.tool_call_id,
        tool_calls=[dict(call) for call in message.tool_calls],
    )


def _annotate_event(
    event: dict[str, Any], session_id: str, turn_id: str, step_id: str
) -> dict[str, Any]:
    return {
        **event,
        "session_id": session_id,
        "turn_id": turn_id,
        "step_id": step_id,
    }


def _prepend_skill_candidates(user_message: str, cwd: str, config: PaiCliConfig) -> str:
    if not config.features.skill:
        return user_message
    candidates = SkillRegistry(cwd).match(user_message, top_k=5)
    if not candidates:
        return user_message
    lines = [
        "Relevant skill candidates for this request:",
        "Call load_skill(name) before proceeding when a candidate applies.",
    ]
    for skill in candidates:
        description = " ".join(skill.description.split())
        if len(description) > 300:
            description = description[:297] + "..."
        tags = f" [tags: {', '.join(skill.tags)}]" if skill.tags else ""
        lines.append(f"- {skill.name}: {description}{tags}")
    candidate_text = "\n".join(lines)
    return f"{candidate_text}\n\n---\nUser request:\n{user_message}"


def _prepend_skill_context(user_message: str, skill_context_buffer: SkillContextBuffer) -> str:
    if not skill_context_buffer or skill_context_buffer.is_empty():
        return user_message
    drained = skill_context_buffer.drain()
    if not drained:
        return user_message
    return f"{drained}\n\n---\nUser request:\n{user_message}"


def _drain_skill_context(skill_context_buffer: SkillContextBuffer) -> str:
    if not skill_context_buffer or skill_context_buffer.is_empty():
        return ""
    return skill_context_buffer.drain()
