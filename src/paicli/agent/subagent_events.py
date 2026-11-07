from __future__ import annotations

import asyncio
import json
from collections import deque
from datetime import UTC, datetime
from typing import Any

from paicli.policy.redaction import Redactor

SUBAGENT_ROLES = (
    "explorer",
    "researcher",
    "builder",
    "reviewer",
    "tester",
    "writer",
    "planner",
    "general",
)


def subagent_role(value: object) -> str | None:
    return value if isinstance(value, str) and value in SUBAGENT_ROLES else None


_FIELDS = {
    "started": {"description"},
    "continued": set(),
    "idle": {"result", "snapshot"},
    "turn_completed": {"result", "usage"},
    "ended": {"status", "result", "error", "cleanup_error", "diagnostic"},
    "phase_changed": {"phase", "tool_name", "tool_call_id", "waiting_reason"},
    "text_delta": {"text"},
    "thinking_delta": {"thinking"},
    "tool_call": {"name", "input", "tool_call_id"},
    "tool_result": {"name", "result", "is_error", "tool_call_id"},
    "usage": {"usage"},
    "error": {"error", "diagnostic"},
    "output_hidden": {"reason"},
    "status": {"snapshot"},
    "observation_gap": {"dropped", "coalesced", "snapshots"},
}
_LOW_PRIORITY = {"text_delta", "thinking_delta", "usage", "status", "phase_changed"}


def public_event(event: dict[str, Any], redactor: Redactor) -> dict[str, Any]:
    """Select the public contract before touching untrusted extension values."""
    inner = event.get("event")
    if (
        not isinstance(inner, dict)
        or not isinstance(inner.get("type"), str)
        or inner["type"] not in _FIELDS
    ):
        inner = {"type": "unknown", "summary": "Unsupported child event omitted."}
    else:
        kind = inner["type"]
        inner = {"type": kind, **{key: inner[key] for key in _FIELDS[kind] if key in inner}}
        for key in (
            "text",
            "thinking",
            "name",
            "tool_call_id",
            "result",
            "error",
            "phase",
            "tool_name",
            "waiting_reason",
            "status",
            "description",
            "reason",
        ):
            if key in inner and inner[key] is not None and not isinstance(inner[key], str):
                inner = {"type": "unknown", "summary": "Invalid child event fields omitted."}
                break
    result = {
        "type": "subagent_event",
        "version": 1,
        "timestamp": datetime.now(UTC).isoformat(),
        "event": inner,
    }
    for key in ("agent", "parent_id", "agent_description", "session_id", "run_id"):
        value = event.get(key)
        if isinstance(value, str):
            result[key] = value
    if isinstance(event.get("seq"), int):
        result["seq"] = event["seq"]
    try:
        cleaned = redactor.clean(result)
        json.dumps(cleaned, allow_nan=False)
        return cleaned
    except (TypeError, ValueError):
        result["event"] = {"type": "unknown", "summary": "Non-JSON child payload omitted."}
        return redactor.clean(result)


class ObservationBuffer:
    """A bounded display copy; authoritative state and delivery live elsewhere."""

    def __init__(self, size: int):
        self.size = size
        self.items: deque[dict[str, Any]] = deque()
        self.ready = asyncio.Event()
        self.dropped = 0
        self.coalesced = 0

    def qsize(self) -> int:
        return len(self.items)

    def empty(self) -> bool:
        return not self.items

    def put_nowait(self, event: dict[str, Any]) -> None:
        kind = event["event"]["type"]
        if self.items and kind in {"text_delta", "thinking_delta"}:
            last = self.items[-1]
            if (
                all(last.get(key) == event.get(key) for key in ("agent", "run_id"))
                and last["event"]["type"] == kind
            ):
                key = "text" if kind == "text_delta" else "thinking"
                combined = last["event"].get(key, "") + event["event"].get(key, "")
                if len(combined.encode("utf-8")) <= 65536:
                    self.items[-1] = {**event, "event": {"type": kind, key: combined}}
                    self.coalesced += 1
                    self.ready.set()
                    return
        if len(self.items) >= self.size:
            disposable = next(
                (i for i, item in enumerate(self.items) if item["event"]["type"] in _LOW_PRIORITY),
                None,
            )
            if disposable is None and kind in _LOW_PRIORITY:
                self.dropped += 1
                return
            if disposable is None:
                # Even lifecycle-only overload is bounded; a gap carries current snapshots.
                disposable = 0
            del self.items[disposable]
            self.dropped += 1
        self.items.append(event)
        self.ready.set()

    def get_nowait(self) -> dict[str, Any]:
        if not self.items:
            raise asyncio.QueueEmpty
        item = self.items.popleft()
        if not self.items:
            self.ready.clear()
        return item

    async def get(self) -> dict[str, Any]:
        while not self.items:
            await self.ready.wait()
        return self.get_nowait()

    def take_counts(self) -> tuple[int, int]:
        counts = self.dropped, self.coalesced
        self.dropped = self.coalesced = 0
        return counts
