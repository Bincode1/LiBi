from __future__ import annotations

from typing import Any


def format_status(snapshot: dict[str, Any]) -> str:
    identity = str(snapshot.get("id") or "subagent")
    label = str(snapshot.get("description") or "")
    state = str(snapshot.get("state") or "unknown")
    phase = str(snapshot.get("phase") or "unknown")
    waiting = snapshot.get("waiting_reason")
    tools = ", ".join((snapshot.get("pending_tools") or {}).values())
    elapsed = snapshot.get("elapsed_seconds", 0)
    activity = snapshot.get("last_activity_seconds_ago", 0)
    detail = f"waiting {waiting}" if waiting else tools or phase
    return (
        f"[{identity[:12]} | {label}] {state} / {detail} / {elapsed}s / "
        f"last activity {activity}s ago"
    )
