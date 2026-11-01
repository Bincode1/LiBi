"""Pure helpers for parsing streaming tool-call deltas from an LLM response.

These functions are stateless and shared by the agent ReAct loop. They operate on
the raw streaming protocol dicts emitted by ``LlmClient.chat``.
"""

from __future__ import annotations

import json
from typing import Any


def merge_tool_delta(tool_states: dict[int, dict[str, Any]], delta: dict[str, Any]) -> None:
    """Accumulate a streaming ``tool_call_delta`` into ``tool_states`` keyed by index."""
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


def finalize_tool_calls(tool_states: dict[int, dict[str, Any]]) -> list[dict[str, Any]]:
    """Return completed tool calls (those with a function name) in index order."""
    calls = []
    for index in sorted(tool_states):
        state = tool_states[index]
        if state["function"]["name"]:
            calls.append(state)
    return calls


def tool_input(call: dict[str, Any]) -> dict[str, Any]:
    """Parse the JSON arguments of a tool call, falling back to a raw wrapper."""
    raw = call.get("function", {}).get("arguments") or "{}"
    try:
        parsed = json.loads(raw)
    except json.JSONDecodeError:
        return {"raw": raw}
    return parsed if isinstance(parsed, dict) else {"value": parsed}


def tool_name_by_id(calls: list[dict[str, Any]], tool_call_id: str) -> str:
    """Look up a tool name by its call id; returns ``"unknown"`` when not found."""
    for call in calls:
        if call.get("id") == tool_call_id:
            return str(call.get("function", {}).get("name") or "unknown")
    return "unknown"
