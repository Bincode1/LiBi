"""agent.py — Core terminal Agent inspired by Claude Code.

Integrates LLM API, parses natural-language instructions, schedules command
and file operations via the tool system, and generates streaming responses.

Supports three execution modes:
    - react  : Standard ReAct loop (thought → tool → observation → answer)
    - plan   : Plan-then-execute with a DAG of sub-tasks
    - team   : Multi-agent orchestrator (planner → workers → reviewer)

All modes share the same streaming event protocol so callers can render
progress incrementally.
"""

from __future__ import annotations

import asyncio
import hashlib
import json
import uuid
from collections.abc import AsyncIterator, Callable
from contextlib import suppress
from pathlib import Path
from typing import Any, Literal

from paicli.agent.orchestrator import AgentOrchestrator
from paicli.agent.plan_execute import PlanExecuteAgent
from paicli.config import PaiCliConfig
from paicli.context import (
    ContextBudget,
    ContextWindowManager,
    estimate_message_tokens,
    estimate_text_tokens,
)
from paicli.image import parse_image_references
from paicli.llm.base import LlmClient
from paicli.prompt import PromptAssembler
from paicli.session import ContextProjector, RequestBuilder, SessionStore
from paicli.skill import SkillContextBuffer, SkillRegistry
from paicli.snapshot import SnapshotService
from paicli.tools.base import ToolContext
from paicli.tools.executor import ToolExecutor
from paicli.tools.registry import ToolRegistry
from paicli.types import Message, QueryResult, Usage

AgentMode = Literal["react", "plan", "team"]


class Agent:
    """A terminal AI agent that connects an LLM to tools for task execution.

    The Agent owns the conversation history, manages context compression, and
    delegates the actual LLM-tool interaction loop to mode-specific runners.
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
        max_plan_depth: int = 1,
        team_worker_mode: str = "react",
        session_store: SessionStore | None = None,
        session_id: str | None = None,
        subagent_manager: Any | None = None,
        agent_id: str = "root",
        event_sink: Callable[[dict[str, Any]], Any] | None = None,
        steer_inbox: asyncio.Queue[str] | None = None,
    ) -> None:
        self.llm_client = llm_client
        self.tool_registry = tool_registry
        self.config = config
        self.cwd = cwd
        self.approval_callback = approval_callback
        self.mode = mode
        self.max_turns = max_turns
        self.max_plan_depth = max_plan_depth
        self.team_worker_mode = team_worker_mode
        self._owns_session_store = session_store is None
        self.session_store = session_store
        self.session_id = session_id
        if mode == "react" and subagent_manager is None:
            from paicli.agent.subagents import SubagentManager

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

        # Conversation state.
        self.history: list[Message] = []
        self.skill_context_buffer = SkillContextBuffer()

        # Accumulated usage / cost across all turns of the session.
        self.last_usage = Usage()
        self.last_cost: dict[str, Any] = {}

        # Validate configuration.
        self._validate_config()

    # ------------------------------------------------------------------
    # Public API — run the agent
    # ------------------------------------------------------------------

    async def run(self, message: str) -> AsyncIterator[dict[str, Any]]:
        """Execute *message* and yield streaming events.

        Events follow this protocol (``type`` field determines the shape):

        ``text_delta``
            {"type": "text_delta", "text": "..."}
        ``team_text_delta``
            {"type": "team_text_delta", "text": "...", "phase": str, "agent": str}
        ``team_status``
            {"type": "team_status", "phase": str, "message": str, "agent": str}
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
        snapshot = SnapshotService(self.cwd)
        with suppress(Exception):
            snapshot.create("pre-turn")

        try:
            if self.mode == "plan":
                events = self._run_logged_mode(self._run_plan(message), message, "plan")
                async for event in events:
                    if self.event_sink is not None:
                        await self.event_sink(event)
                    yield event
            elif self.mode == "team":
                events = self._run_logged_mode(self._run_team(message), message, "team")
                async for event in events:
                    if self.event_sink is not None:
                        await self.event_sink(event)
                    yield event
            else:
                async for event in self._run_react(message):
                    if self.event_sink is not None:
                        await self.event_sink(event)
                    yield event
        except asyncio.CancelledError:
            if self.subagent_manager is not None:
                await self.subagent_manager.cancel_for_parent(self.agent_id)
            if self.session_store is not None and self.session_id is not None:
                state = self.session_store.state(self.session_id)
                if state.active_turn_id:
                    self.session_store.append(
                        self.session_id,
                        "turn/canceled",
                        {"reason": "cancelled"},
                        turn_id=state.active_turn_id,
                    )
            raise
        except TimeoutError:
            if self.session_store is not None and self.session_id is not None:
                self.session_store.append(
                    self.session_id,
                    "turn/failed",
                    {"reason": "deadline_exceeded"},
                )
            raise
        except Exception as exc:
            if self.session_store is not None and self.session_id is not None:
                state = self.session_store.state(self.session_id)
                if state.active_turn_id:
                    if state.active_step_id:
                        self.session_store.append(
                            self.session_id,
                            "step/failed",
                            {"reason": type(exc).__name__},
                            turn_id=state.active_turn_id,
                            step_id=state.active_step_id,
                        )
                    self.session_store.append(
                        self.session_id,
                        "turn/failed",
                        {"reason": type(exc).__name__},
                        turn_id=state.active_turn_id,
                    )
            raise
        finally:
            with suppress(Exception):
                snapshot.create("post-turn")

    async def run_complete(self, message: str) -> QueryResult:
        """Run the agent synchronously (collect all events) and return a result."""
        text = ""
        tokens = 0
        turns = 0
        usage = Usage()
        cost: dict[str, Any] = {}
        async for event in self.run(message):
            event_type = event.get("type")
            if event_type == "text_delta":
                text += str(event.get("text") or "")
            elif event_type == "error":
                raise event["error"]  # type: ignore[arg-type]
            elif event_type == "done":
                tokens = int(event.get("total_tokens") or 0)
                turns = int(event.get("total_turns") or 0)
                usage = Usage.from_mapping(event.get("usage") or {})
                cost = dict(event.get("cost") or {})
        return QueryResult(text=text, total_tokens=tokens, turns=turns, usage=usage, cost=cost)

    # ------------------------------------------------------------------
    # History management
    # ------------------------------------------------------------------

    def clear_history(self) -> None:
        """Start a fresh visible conversation while retaining its prior event log."""
        self.history = []
        self.session_id = self._create_new_session()
        self.skill_context_buffer.clear()
        self.last_usage = Usage()
        self.last_cost = {}

    # ------------------------------------------------------------------
    # Mode runners
    # ------------------------------------------------------------------

    async def _run_react(self, message: str) -> AsyncIterator[dict[str, Any]]:
        """Standard ReAct loop (the default and most common mode)."""
        turn_id = f"turn_{uuid.uuid4().hex}"
        self._import_legacy_history()
        session_id = self._ensure_session()
        store = self.session_store
        assert store is not None
        store.append(
            session_id,
            "turn/started",
            {"mode": "react"},
            turn_id=turn_id,
        )
        original = message
        candidate_message = _prepend_skill_candidates(message, self.cwd, self.config)
        candidate_prefix, separator, request_message = candidate_message.partition(
            "\n\n---\nUser request:\n"
        )
        if separator:
            message = request_message
            store.append(
                session_id,
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
            store.append(
                session_id,
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
        store.append(
            session_id,
            "user/message",
            {"content": original},
            turn_id=turn_id,
            source="cli-or-runtime",
        )
        # Prepend skill candidates that match the user message.

        tool_defs = self.tool_registry.definitions()
        if self.mode == "react":
            from paicli.agent.subagents import make_subagent_control_tools, make_subagent_tool

            subagent_tools = [make_subagent_tool(), *make_subagent_control_tools()]
            allowlist = self.config.policy.subagent_tool_allowlist
            if allowlist is None or self.agent_id == "root":
                self.tool_registry.register_all(subagent_tools)
            else:
                self.tool_registry.register_all(
                    [tool for tool in subagent_tools if tool.name in set(allowlist)]
                )
            tool_defs = self.tool_registry.definitions()
        executor = ToolExecutor(self.tool_registry)
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
            llm_client=self.llm_client,
            tool_registry=self.tool_registry,
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
        store.append(
            session_id,
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

        # Context-window manager for history compression.
        context_manager = ContextWindowManager(
            ContextBudget(
                context_window=self.llm_client.max_context_window,
                max_output_tokens=self.config.llm.max_tokens,
                compression_threshold=self.config.memory.compression_threshold,
                compression_target=self.config.memory.compression_target,
                reserve_tokens=self.config.memory.compression_reserve_tokens,
            ),
            max_history_messages=self.config.memory.max_conversation_history,
            min_recent_messages=self.config.memory.min_recent_messages,
            summary_max_chars=self.config.memory.summary_max_chars,
        )

        total_usage = Usage()
        turn = 0
        finished = False
        latest_messages: list[Message] = []
        from paicli.agent.run_controller import RunController

        run_controller = RunController(
            manager=self.subagent_manager,
            parent_id=self.agent_id,
            parent_session_id=session_id,
            session_store=store,
        )

        async def consume_steer(step_id: str) -> bool:
            if self.steer_inbox is None:
                return False
            consumed = False
            while True:
                try:
                    content = self.steer_inbox.get_nowait()
                except asyncio.QueueEmpty:
                    break
                store.append(
                    session_id,
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
            store.append(
                session_id,
                "step/started",
                {"mode": "react"},
                turn_id=turn_id,
                step_id=step_id,
            )
            store.append(
                session_id,
                "tool/schema_snapshot",
                {"tools": tool_defs},
                turn_id=turn_id,
                step_id=step_id,
                source="tool-registry",
            )
            store.append(
                session_id,
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

            current_events = store.events(session_id)
            current_view = self._context_projector.project(
                current_events,
                session_id=session_id,
                turn_id=turn_id,
                step_id=step_id,
                scope="session",
                route=self.llm_client.provider_name,
                adapter_capabilities={
                    "provider": self.llm_client.provider_name,
                    "supports_images": bool(getattr(self.llm_client, "supports_images", False)),
                },
                budget=self._budget_metadata(),
            )

            # Compress if needed.
            if self.config.features.context_compression:
                compression = context_manager.prepare(
                    list(current_view.messages),
                    system_prompt=current_view.system_prompt,
                    tool_definitions=list(current_view.tools),
                )
                if compression.compressed:
                    compacted_messages = [_message_payload(item) for item in compression.messages]
                    source_sequences = list(current_view.source_sequences)
                    store.append(
                        session_id,
                        "compaction/checkpoint",
                        {
                            "compaction_id": f"compact_{uuid.uuid4().hex}",
                            "from_sequence": min(source_sequences, default=0),
                            "through_sequence": max(source_sequences, default=0),
                            "source_sequences": source_sequences,
                            "messages": compacted_messages,
                            "strategy_version": "extractive-v1",
                            "estimated_tokens_before": compression.estimated_tokens_before,
                            "estimated_tokens_after": compression.estimated_tokens_after,
                        },
                        turn_id=turn_id,
                        step_id=step_id,
                        source="context-window-manager",
                    )
                    current_view = self._context_projector.project(
                        store.events(session_id),
                        session_id=session_id,
                        turn_id=turn_id,
                        step_id=step_id,
                        scope="session",
                        route=self.llm_client.provider_name,
                        adapter_capabilities={
                            "provider": self.llm_client.provider_name,
                            "supports_images": bool(
                                getattr(self.llm_client, "supports_images", False)
                            ),
                        },
                        budget=self._budget_metadata(),
                    )
                    yield {
                        "type": "context_compressed",
                        "before_tokens": compression.estimated_tokens_before,
                        "after_tokens": compression.estimated_tokens_after,
                        "summarized_messages": compression.summarized_messages,
                        "session_id": session_id,
                        "turn_id": turn_id,
                        "step_id": step_id,
                    }
            current_view = self._context_projector.project(
                store.events(session_id),
                session_id=session_id,
                turn_id=turn_id,
                step_id=step_id,
                scope="session",
                route=self.llm_client.provider_name,
                adapter_capabilities={
                    "provider": self.llm_client.provider_name,
                    "supports_images": bool(getattr(self.llm_client, "supports_images", False)),
                },
                message_transform=lambda item: _transform_message_images(item, self.cwd),
                budget=self._budget_metadata(),
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
                "projection": {
                    "scope": "session",
                    "route": self.llm_client.provider_name,
                    "adapter_capabilities": {
                        "provider": self.llm_client.provider_name,
                        "supports_images": bool(getattr(self.llm_client, "supports_images", False)),
                    },
                    "budget": self._budget_metadata(),
                },
            }
            yield _annotate_event(
                {"type": "context_summary", **context_summary},
                session_id,
                turn_id,
                step_id,
            )
            store.append(
                session_id,
                "context/summary",
                context_summary,
                turn_id=turn_id,
                step_id=step_id,
                source="context-projector",
            )
            store.append(
                session_id,
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
                        _merge_tool_delta(tool_states, event["tool_call"])
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
                    elif event_type == "error":
                        error = event["error"]
                        store.append(
                            session_id,
                            "request/failed",
                            {"request_id": request.request_id, "error_type": type(error).__name__},
                            turn_id=turn_id,
                            step_id=step_id,
                        )
                        store.append(
                            session_id,
                            "step/failed",
                            {"reason": "provider_error"},
                            turn_id=turn_id,
                            step_id=step_id,
                        )
                        store.append(
                            session_id,
                            "turn/failed",
                            {"reason": "provider_error"},
                            turn_id=turn_id,
                        )
                        yield _annotate_event(
                            {"type": "error", "error": error}, session_id, turn_id, step_id
                        )
                        return
            except Exception as exc:
                store.append(
                    session_id,
                    "request/failed",
                    {"request_id": request.request_id, "error_type": type(exc).__name__},
                    turn_id=turn_id,
                    step_id=step_id,
                )
                store.append(
                    session_id,
                    "step/failed",
                    {"reason": "provider_exception"},
                    turn_id=turn_id,
                    step_id=step_id,
                )
                store.append(
                    session_id,
                    "turn/failed",
                    {"reason": "provider_exception"},
                    turn_id=turn_id,
                )
                raise

            total_usage = total_usage + turn_usage
            tool_calls = _finalize_tool_calls(tool_states)
            assistant_msg = Message(
                role="assistant",
                content=text or "",
                tool_calls=tool_calls,
            )
            if thinking and not text:
                assistant_msg.content = ""
            store.append(
                session_id,
                "assistant/message",
                {"content": assistant_msg.content, "tool_calls": tool_calls},
                turn_id=turn_id,
                step_id=step_id,
                source="provider",
            )
            store.append(
                session_id,
                "request/completed",
                {"request_id": request.request_id, "usage": turn_usage.to_dict()},
                turn_id=turn_id,
                step_id=step_id,
                source="provider",
            )
            latest_messages = list(current_view.messages) + [assistant_msg]
            yield _annotate_event(
                {"type": "turn_complete", "turn": turn, "stop_reason": stop_reason},
                session_id,
                turn_id,
                step_id,
            )

            steered = await consume_steer(step_id) if not tool_calls else False
            if steered and stop_reason != "tool_use" and not tool_calls:
                store.append(
                    session_id,
                    "step/completed",
                    {"reason": "steered"},
                    turn_id=turn_id,
                    step_id=step_id,
                )
                continue

            # If the model didn't request any tools, we're done.
            if stop_reason != "tool_use" and not tool_calls:
                store.append(
                    session_id,
                    "step/completed",
                    {},
                    turn_id=turn_id,
                    step_id=step_id,
                )
                if (
                    self.subagent_manager is not None
                    and self.subagent_manager.event_callback is not None
                ):
                    run_controller.discard_observations()
                if run_controller.has_pending_children():
                    async for event in run_controller.observe_until_settled():
                        if self.subagent_manager.event_callback is None:
                            yield _annotate_event(event, session_id, turn_id, step_id)
                    deliveries = run_controller.collect_deliveries(defer_ack=True)
                    if deliveries:
                        notices = [run_controller.format_delivery(item) for item in deliveries]
                        store.append(
                            session_id,
                            "user/message",
                            {
                                "content": "Child agent results:\n" + "\n".join(notices),
                                "delivery_ids": [item.delivery_id for item in deliveries],
                            },
                            turn_id=turn_id,
                            source="subagent-delivery",
                        )
                        run_controller.acknowledge_deliveries(deliveries)
                        self.subagent_manager.raise_background_errors()
                        continue
                if self.subagent_manager is not None:
                    self.subagent_manager.raise_background_errors()
                finished = True
                break

            # Run tools.
            for call in tool_calls:
                name = call.get("function", {}).get("name", "unknown")
                call_id = str(call.get("id") or "")
                store.append(
                    session_id,
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
                        "input": _tool_input(call),
                        "tool_call_id": call_id,
                    },
                    session_id,
                    turn_id,
                    step_id,
                )

            tool_results = await executor.execute_all(tool_calls, context)
            loaded_skill_context = _drain_skill_context(self.skill_context_buffer)
            load_skill_ids = [
                str(call.get("id") or "")
                for call in tool_calls
                if str(call.get("function", {}).get("name") or "") == "load_skill"
            ]
            injection_target = load_skill_ids[-1] if load_skill_ids else ""
            injected = False

            for result in tool_results:
                tool_name = _tool_name_by_id(tool_calls, result.tool_use_id or "")
                yield _annotate_event(
                    {
                        "type": "tool_result",
                        "name": tool_name,
                        "result": result.content,
                        "is_error": result.is_error,
                        "tool_call_id": result.tool_use_id,
                    },
                    session_id,
                    turn_id,
                    step_id,
                )
                model_content = result.content
                if loaded_skill_context and result.tool_use_id == injection_target:
                    model_content = f"{model_content}\n\n{loaded_skill_context}"
                    injected = True
                store.append(
                    session_id,
                    "tool/result",
                    {"name": tool_name, "content": model_content, "is_error": result.is_error},
                    turn_id=turn_id,
                    step_id=step_id,
                    tool_call_id=result.tool_use_id,
                    source="tool-executor",
                )

            if loaded_skill_context:
                store.append(
                    session_id,
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

            store.append(
                session_id,
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
            store.append(
                session_id,
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

        # The event stream is canonical; this list remains a compatibility projection.
        store.append(
            session_id,
            "turn/completed",
            {"total_tokens": total_usage.total_tokens, "steps": turn},
            turn_id=turn_id,
        )
        final_events = store.events(session_id)
        if turn:
            completed_step = next(
                event.step_id
                for event in reversed(final_events)
                if event.event_type == "step/completed" and event.turn_id == turn_id
            )
            final_view = self._context_projector.project(
                final_events,
                session_id=session_id,
                turn_id=turn_id,
                step_id=completed_step,
            )
            self.history = list(final_view.messages)
        else:
            self.history = latest_messages
        self.last_usage = total_usage

        done_event: dict[str, Any] = {
            "type": "done",
            "total_turns": turn,
            "total_tokens": total_usage.total_tokens,
            "usage": total_usage.to_dict(),
            "messages": self.history,
            "session_id": session_id,
            "turn_id": turn_id,
        }
        costs = _calculate_costs(self.llm_client, total_usage)
        if costs:
            done_event["cost"] = costs
            self.last_cost = costs
        yield done_event

    def _import_legacy_history(self) -> None:
        session_id = self._ensure_session()
        store = self.session_store
        assert store is not None
        events = store.events(session_id)
        if any(
            event.event_type in {"user/message", "assistant/message", "tool/result"}
            for event in events
        ):
            return
        if not any(event.event_type == "session/created" for event in events):
            store.append(session_id, "session/created", {}, source="agent")
        for message in self.history:
            if message.role not in {"user", "assistant", "tool"}:
                continue
            payload = _message_payload(message)
            event_type = "tool/result" if message.role == "tool" else f"{message.role}/message"
            store.append(
                session_id,
                event_type,
                payload,
                tool_call_id=message.tool_call_id,
                source="legacy-history-adapter",
            )

    def _ensure_session(self) -> str:
        if self.session_store is None:
            self.session_store = SessionStore(
                Path.home() / ".paicli" / "runtime" / "runtime.db",
                sensitive_values=(self.config.llm.api_key,),
            )
        self.session_store.redactor.add_sensitive_values((self.config.llm.api_key,))
        if self.session_id is None:
            self.session_id = self.session_store.create_session()
        return self.session_id

    def _budget_metadata(self) -> dict[str, int]:
        return {
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
                Path.home() / ".paicli" / "runtime" / "runtime.db",
                sensitive_values=(self.config.llm.api_key,),
            )
        return self.session_store.create_session()

    async def _run_logged_mode(
        self,
        events: AsyncIterator[dict[str, Any]],
        message: str,
        mode: str,
    ) -> AsyncIterator[dict[str, Any]]:
        """Persist the parent request/result without importing child-agent internals."""
        session_id = self._ensure_session()
        store = self.session_store
        assert store is not None
        turn_id = f"turn_{uuid.uuid4().hex}"
        step_id = f"step_{uuid.uuid4().hex}"
        store.append(session_id, "turn/started", {"mode": mode}, turn_id=turn_id)
        store.append(
            session_id,
            "user/message",
            {"content": message},
            turn_id=turn_id,
            source="cli-or-runtime",
        )
        store.append(session_id, "step/started", {"mode": mode}, turn_id=turn_id, step_id=step_id)
        answer: list[str] = []
        total_tokens = 0
        try:
            async for event in events:
                event_type = event.get("type")
                if event_type == "text_delta":
                    answer.append(str(event.get("text") or ""))
                elif event_type == "plan_task_started":
                    store.append(
                        session_id,
                        "agent/child_started",
                        {"task_id": event.get("task_id"), "mode": mode},
                        turn_id=turn_id,
                        step_id=step_id,
                    )
                elif event_type == "plan_task_done":
                    store.append(
                        session_id,
                        "agent/child_completed",
                        {
                            "task_id": event.get("task_id"),
                            "turns": event.get("turns"),
                            "tokens": event.get("tokens"),
                        },
                        turn_id=turn_id,
                        step_id=step_id,
                    )
                elif event_type == "done":
                    total_tokens = int(event.get("total_tokens") or 0)
                elif event_type == "error":
                    store.append(
                        session_id,
                        "step/failed",
                        {"reason": type(event.get("error")).__name__},
                        turn_id=turn_id,
                        step_id=step_id,
                    )
                    store.append(
                        session_id,
                        "turn/failed",
                        {"reason": type(event.get("error")).__name__},
                        turn_id=turn_id,
                    )
                if event_type == "done":
                    content = "".join(answer)
                    store.append(
                        session_id,
                        "assistant/message",
                        {"content": content},
                        turn_id=turn_id,
                        step_id=step_id,
                        source=f"{mode}-summary",
                    )
                    store.append(
                        session_id,
                        "step/completed",
                        {"mode": mode},
                        turn_id=turn_id,
                        step_id=step_id,
                    )
                    store.append(
                        session_id,
                        "turn/completed",
                        {"mode": mode, "total_tokens": total_tokens},
                        turn_id=turn_id,
                    )
                    self.history = [
                        Message(role="user", content=message),
                        Message(role="assistant", content=content),
                    ]
                    event = {
                        **event,
                        "session_id": session_id,
                        "turn_id": turn_id,
                        "step_id": step_id,
                    }
                yield {**event, "session_id": session_id, "turn_id": turn_id, "step_id": step_id}
        except Exception as exc:
            state = store.state(session_id)
            if state.active_turn_id:
                if state.active_step_id:
                    store.append(
                        session_id,
                        "step/failed",
                        {"reason": type(exc).__name__},
                        turn_id=turn_id,
                        step_id=step_id,
                    )
                store.append(
                    session_id,
                    "turn/failed",
                    {"reason": type(exc).__name__},
                    turn_id=turn_id,
                )
            raise

    async def _run_plan(self, message: str) -> AsyncIterator[dict[str, Any]]:
        """Plan-then-execute mode: the planner creates a DAG, then workers run it."""
        agent = PlanExecuteAgent(
            llm_client=self.llm_client,
            tool_registry=self.tool_registry,
            config=self.config,
            cwd=self.cwd,
            approval_callback=self.approval_callback,
            max_task_turns=self.max_turns,
        )
        agent.history = list(self.history)

        async for event in agent.run(message):
            if event.get("type") == "done":
                self.history = list(event.get("messages") or [])
                self.last_usage = Usage.from_mapping(event.get("usage") or {})
                self.last_cost = dict(event.get("cost") or {})
            yield event

    async def _run_team(self, message: str) -> AsyncIterator[dict[str, Any]]:
        """Multi-agent team mode: planner → parallel workers → reviewer."""
        orchestrator = AgentOrchestrator(
            llm_client=self.llm_client,
            tool_registry=self.tool_registry,
            config=self.config,
            cwd=self.cwd,
            approval_callback=self.approval_callback,
            default_worker_mode=self.team_worker_mode,
        )
        async for event in orchestrator.run(message):
            if event.get("type") == "done":
                self.history = list(event.get("messages") or [])
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
# Shared helpers (also used by query.py)
# ------------------------------------------------------------------


def _merge_tool_delta(tool_states: dict[int, dict[str, Any]], delta: dict[str, Any]) -> None:
    index = int(delta.get("index") or 0)
    state = tool_states.setdefault(
        index,
        {
            "id": delta.get("id") or f"tool_{index}",
            "type": "function",
            "function": {"name": "", "arguments": ""},
        },
    )
    if delta.get("id"):
        state["id"] = delta["id"]
    function = delta.get("function") or {}
    if function.get("name"):
        state["function"]["name"] = function["name"]
    if function.get("arguments"):
        state["function"]["arguments"] += function["arguments"]


def _finalize_tool_calls(tool_states: dict[int, dict[str, Any]]) -> list[dict[str, Any]]:
    calls = []
    for index in sorted(tool_states):
        state = tool_states[index]
        if state["function"]["name"]:
            calls.append(state)
    return calls


def _tool_input(call: dict[str, Any]) -> dict[str, Any]:
    raw = call.get("function", {}).get("arguments") or "{}"
    try:
        parsed = json.loads(raw)
    except json.JSONDecodeError:
        return {"raw": raw}
    return parsed if isinstance(parsed, dict) else {"value": parsed}


def _tool_name_by_id(calls: list[dict[str, Any]], tool_call_id: str) -> str:
    for call in calls:
        if call.get("id") == tool_call_id:
            return str(call.get("function", {}).get("name") or "unknown")
    return "unknown"


def _message_payload(message: Message) -> dict[str, Any]:
    return {
        "role": message.role,
        "content": message.content,
        "name": message.name,
        "tool_call_id": message.tool_call_id,
        "tool_calls": message.tool_calls,
    }


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


def _calculate_costs(llm_client: LlmClient, usage: Usage) -> dict[str, Any]:
    calculator = getattr(llm_client, "calculate_cost", None)
    if not callable(calculator):
        return {}
    result: dict[str, Any] = {}
    for currency in ("usd", "cny"):
        try:
            breakdown = calculator(usage, currency=currency)
        except (KeyError, TypeError, ValueError):
            continue
        result[currency] = breakdown.to_dict()
    return result
