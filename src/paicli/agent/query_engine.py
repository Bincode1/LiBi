from __future__ import annotations

import asyncio
from contextlib import aclosing

from paicli.agent.agent import Agent
from paicli.config import PaiCliConfig
from paicli.llm.base import LlmClient
from paicli.prompt import PromptAssembler
from paicli.session import SessionStore
from paicli.tools.registry import ToolRegistry
from paicli.types import Message, QueryResult, Usage


class QueryEngine:
    def __init__(
        self,
        *,
        llm_client: LlmClient,
        tool_registry: ToolRegistry,
        config: PaiCliConfig,
        cwd: str,
        approval_callback=None,
        session_store: SessionStore | None = None,
        session_id: str | None = None,
    ):
        self.llm_client = llm_client
        self.tool_registry = tool_registry
        self.config = config
        self.cwd = cwd
        self.approval_callback = approval_callback
        self.session_store = session_store
        self.session_id = (
            session_store.create_session(session_id) if session_store is not None else session_id
        )
        self.system_prompt = PromptAssembler(
            config=config,
            cwd=cwd,
            tool_names=tool_registry.list_names(),
            model=llm_client.model_name,
            provider=llm_client.provider_name,
        ).build_static()
        self._last_agent: Agent | None = None

    async def ask(self, message: str, history: list[Message] | None = None):
        agent = Agent(
            llm_client=self.llm_client,
            tool_registry=self.tool_registry,
            system_prompt=self.system_prompt,
            cwd=self.cwd,
            config=self.config,
            approval_callback=self.approval_callback,
            session_store=self.session_store,
            session_id=self.session_id,
        )
        self._last_agent = agent
        agent.history = list(history or [])
        try:
            async for event in agent.run(message):
                yield event
        finally:
            self.session_store = agent.session_store
            self.session_id = agent.session_id

    async def plan(self, message: str):
        agent = Agent(
            llm_client=self.llm_client,
            tool_registry=self.tool_registry,
            config=self.config,
            cwd=self.cwd,
            approval_callback=self.approval_callback,
            mode="plan",
            session_store=self.session_store,
            session_id=self.session_id,
        )
        self._last_agent = agent
        self.session_store = agent.session_store
        self.session_id = agent.session_id
        async for event in agent.run(message):
            yield event
        self.session_store = agent.session_store
        self.session_id = agent.session_id

    async def team(self, message: str, *, worker_mode: str = "react"):
        agent = Agent(
            llm_client=self.llm_client,
            tool_registry=self.tool_registry,
            config=self.config,
            cwd=self.cwd,
            approval_callback=self.approval_callback,
            mode="team",
            team_worker_mode=worker_mode,
            session_store=self.session_store,
            session_id=self.session_id,
        )
        self._last_agent = agent
        self.session_store = agent.session_store
        self.session_id = agent.session_id
        async for event in agent.run(message):
            yield event
        self.session_store = agent.session_store
        self.session_id = agent.session_id

    async def ask_complete_async(
        self,
        message: str,
        history: list[Message] | None = None,
    ) -> QueryResult:
        return await self._complete_from_events(self.ask(message, history))

    async def plan_complete_async(self, message: str) -> QueryResult:
        return await self._complete_from_events(self.plan(message))

    async def team_complete_async(self, message: str, *, worker_mode: str = "react") -> QueryResult:
        return await self._complete_from_events(self.team(message, worker_mode=worker_mode))

    def ask_complete(self, message: str, history: list[Message] | None = None) -> QueryResult:
        return asyncio.run(self.ask_complete_async(message, history))

    def plan_complete(self, message: str) -> QueryResult:
        return asyncio.run(self.plan_complete_async(message))

    def team_complete(self, message: str, *, worker_mode: str = "react") -> QueryResult:
        return asyncio.run(self.team_complete_async(message, worker_mode=worker_mode))

    async def _complete_from_events(self, events) -> QueryResult:
        text = ""
        tokens = 0
        turns = 0
        usage = Usage()
        cost = {}
        async with aclosing(events):
            async for event in events:
                if event.get("type") == "text_delta":
                    text += str(event.get("text") or "")
                elif event.get("type") == "error":
                    raise event["error"]
                elif event.get("type") == "done":
                    tokens += int(event.get("total_tokens") or 0)
                    turns += int(event.get("total_turns") or 0)
                    usage = usage + Usage.from_mapping(event.get("usage") or {})
                    cost = dict(event.get("cost") or cost)
        return QueryResult(text=text, total_tokens=tokens, turns=turns, usage=usage, cost=cost)
