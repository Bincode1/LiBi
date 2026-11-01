"""Shared helpers for consuming the agent streaming event protocol."""

from __future__ import annotations

from collections.abc import AsyncIterator
from contextlib import aclosing
from typing import Any

from paicli.types import QueryResult, Usage


async def collect_query_result(events: AsyncIterator[dict[str, Any]]) -> QueryResult:
    """Consume an agent event stream and aggregate it into a ``QueryResult``.

    Multiple ``done`` events are summed so that nested loops (e.g. plan tasks
    that each emit ``done``) aggregate correctly. ``error`` events are raised.
    """
    text = ""
    tokens = 0
    turns = 0
    usage = Usage()
    cost: dict[str, Any] = {}
    async with aclosing(events):
        async for event in events:
            event_type = event.get("type")
            if event_type == "text_delta":
                text += str(event.get("text") or "")
            elif event_type == "error":
                raise event["error"]  # type: ignore[misc]
            elif event_type == "done":
                tokens += int(event.get("total_tokens") or 0)
                turns += int(event.get("total_turns") or 0)
                usage = usage + Usage.from_mapping(event.get("usage") or {})
                cost = dict(event.get("cost") or cost)
    return QueryResult(text=text, total_tokens=tokens, turns=turns, usage=usage, cost=cost)
