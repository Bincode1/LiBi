from __future__ import annotations

import asyncio
from contextlib import aclosing

from paicli.agent.agent import Agent
from paicli.agent.events import collect_query_result
from paicli.config import PaiCliConfig
from paicli.llm.base import LlmClient
from paicli.prompt import PromptAssembler
from paicli.session import SessionStore
from paicli.tools.registry import ToolRegistry
from paicli.types import QueryResult


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
        self._react_agent: Agent | None = None
        self._react_loop: asyncio.AbstractEventLoop | None = None
        self._running = False

    async def ask(self, message: str):
        if self._running:
            raise RuntimeError("QueryEngine already has an active request.")
        loop = asyncio.get_running_loop()
        if self._react_agent is not None and self._react_loop is not loop:
            manager = self._react_agent.subagent_manager
            if manager is not None and any(not a.settled for a in manager.activations.values()):
                raise RuntimeError("QueryEngine has resident children in another event loop.")
            self._react_agent = None
            self._react_loop = None
        if self._react_agent is None:
            self._react_agent = Agent(
                llm_client=self.llm_client,
                tool_registry=self.tool_registry,
                system_prompt=self.system_prompt,
                cwd=self.cwd,
                config=self.config,
                approval_callback=self.approval_callback,
                session_store=self.session_store,
                session_id=self.session_id,
            )
            self._react_loop = loop
        agent = self._react_agent
        self._last_agent = agent
        self._running = True
        try:
            async with aclosing(agent.run(message)) as events:
                async for event in events:
                    yield event
        finally:
            self._running = False
            self.session_store = agent.session_store
            self.session_id = agent.session_id

    async def aclose(self) -> None:
        if self._react_agent is not None:
            agent = self._react_agent
            if self._react_loop is not asyncio.get_running_loop():
                raise RuntimeError("Close QueryEngine in the event loop that owns it.")
            if agent.subagent_manager is not None:
                await agent.subagent_manager.drain()
            self._react_agent = None
            self._react_loop = None

    async def __aenter__(self) -> QueryEngine:
        return self

    async def __aexit__(self, exc_type, exc, traceback) -> None:
        await self.aclose()

    async def _complete_and_close(self, operation) -> QueryResult:
        try:
            return await operation
        finally:
            await self.aclose()

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
        async with aclosing(agent.run(message)) as events:
            async for event in events:
                yield event
        self.session_store = agent.session_store
        self.session_id = agent.session_id

    async def ask_complete_async(
        self,
        message: str,
    ) -> QueryResult:
        return await self._complete_from_events(self.ask(message))

    async def plan_complete_async(self, message: str) -> QueryResult:
        return await self._complete_from_events(self.plan(message))

    def ask_complete(self, message: str) -> QueryResult:
        return asyncio.run(self._complete_and_close(self.ask_complete_async(message)))

    def plan_complete(self, message: str) -> QueryResult:
        return asyncio.run(self._complete_and_close(self.plan_complete_async(message)))

    async def _complete_from_events(self, events) -> QueryResult:
        return await collect_query_result(events)
