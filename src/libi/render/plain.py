from __future__ import annotations

import sys
from typing import Any

from libi.render.subagent_status import format_status


class PlainRenderer:
    def __init__(self, *, print_events: bool = True):
        self.print_events = print_events

    def handle(self, event: dict[str, Any]) -> None:
        event_type = event.get("type")
        if event_type == "subagent_event" and self.print_events:
            inner = event.get("event") or {}
            agent = str(event.get("agent") or "subagent")
            kind = str(inner.get("type") or "event")
            content = inner.get("text", inner.get("thinking", inner.get("result", "")))
            if kind == "status":
                return
            if kind in {"idle", "turn_completed"}:
                return
            if kind == "phase_changed":
                if inner.get("waiting_reason") != "approval":
                    return
                content = "waiting for approval"
            if kind == "observation_gap":
                content = f"{inner.get('dropped', 0)} events omitted"
            elif kind == "ended":
                content = inner.get("status", "unknown")
                if inner.get("cleanup_error"):
                    content = f"{content}; cleanup failed: {inner['cleanup_error']}"
            elif kind == "error":
                content = inner.get("error", "Child failed")
            sys.stdout.write(f"\n[{agent}:{kind}] {content}\n")
            if kind == "observation_gap":
                for snapshot in inner.get("snapshots", []):
                    sys.stdout.write(format_status(snapshot) + "\n")
            sys.stdout.flush()
            return
        if event_type == "text_delta":
            sys.stdout.write(str(event.get("text") or ""))
            sys.stdout.flush()
        elif self.print_events and event_type == "tool_call":
            sys.stdout.write(f"\n[tool] {event.get('name')} {event.get('input')}\n")
            sys.stdout.flush()
        elif self.print_events and event_type == "tool_result":
            marker = "error" if event.get("is_error") else "result"
            sys.stdout.write(f"[tool:{marker}] {event.get('name')}: {event.get('result')}\n")
            sys.stdout.flush()

    def newline(self) -> None:
        sys.stdout.write("\n")
        sys.stdout.flush()
