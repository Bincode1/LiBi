from __future__ import annotations

import json
import threading
from typing import Any

from rich import box
from rich.align import Align
from rich.console import Console
from rich.live import Live
from rich.markdown import Markdown
from rich.panel import Panel
from rich.table import Table
from rich.text import Text

from paicli.render.subagent_status import format_status


class RichRenderer:
    def __init__(
        self,
        console: Console | None = None,
        *,
        live_markdown: bool = False,
        context_window: int | None = None,
    ):
        self.console = console or Console()
        self._render_lock = threading.RLock()
        self._buffer: list[str] = []
        self._thinking_buffer: list[str] = []
        self._thinking_scope: str | None = None
        self._team_output_scope: str | None = None
        self._live_markdown = live_markdown
        self._live: Live | None = None
        self._thinking_live: Live | None = None
        self._context_window = context_window or 0
        self._input_tokens = 0
        self._output_tokens = 0
        self._cache_hit_tokens = 0
        self._cache_miss_tokens = 0
        self._cache_write_tokens = 0
        self._cache_context_tokens = 0
        self._run_saw_usage_event = False
        self._last_input_tokens = 0
        self._last_turns = 0
        self._last_total_tokens = 0
        self._last_context_ratio = 0.0
        self._last_has_usage = False

    def set_context_window(self, context_window: int | None) -> None:
        self._context_window = context_window or self._context_window

    def start_run(self) -> None:
        self._buffer.clear()
        self._thinking_buffer.clear()
        self._thinking_scope = None
        self._team_output_scope = None
        self._stop_live_markdown()
        self._stop_live_thinking()
        self._input_tokens = 0
        self._output_tokens = 0
        self._last_input_tokens = 0
        self._run_saw_usage_event = False

    def reset_session_stats(self) -> None:
        self._input_tokens = 0
        self._output_tokens = 0
        self._cache_hit_tokens = 0
        self._cache_miss_tokens = 0
        self._cache_write_tokens = 0
        self._cache_context_tokens = 0
        self._last_input_tokens = 0
        self._last_turns = 0
        self._last_total_tokens = 0
        self._last_context_ratio = 0.0
        self._last_has_usage = False

    def toolbar_status(self) -> dict[str, Any]:
        return {
            "turns": self._last_turns,
            "input_tokens": self._input_tokens,
            "output_tokens": self._output_tokens,
            "cache_hit_tokens": self._cache_hit_tokens,
            "cache_miss_tokens": self._cache_miss_tokens,
            "cache_write_tokens": self._cache_write_tokens,
            "cache_context_tokens": self._cache_context_tokens,
            "cache_hit_rate": self._cache_hit_rate(),
            "total_tokens": self._last_total_tokens,
            "context_ratio": self._last_context_ratio,
            "has_usage": self._last_has_usage,
        }

    def banner(
        self,
        *,
        model: str,
        provider: str,
        cwd: str,
        tools: int,
        version: str = "0.1.0",
        api_key_configured: bool = False,
        mcp_servers: int = 0,
        skills: int = 0,
        agents_files: int = 0,
        hitl_mode: str = "auto",
    ) -> None:
        top = Table.grid(expand=True)
        top.add_column(ratio=1)
        top.add_column(ratio=2)
        top.add_row(
            self._identity_panel(version=version, api_key_configured=api_key_configured),
            self._release_panel(version=version),
        )

        _ = model, provider, cwd, tools, mcp_servers, skills, agents_files, hitl_mode

        self.console.print()
        self.console.print(top)
        self.console.print(Align.right(Text("? for shortcuts", style="dim")))
        self.console.rule(style="grey23")
        self.console.print()

    def handle(self, event: dict[str, Any]) -> None:
        with self._render_lock:
            self._handle(event)

    def _handle(self, event: dict[str, Any]) -> None:
        event_type = event.get("type")
        if event_type == "text_delta":
            self._flush_thinking()
            text = str(event.get("text") or "")
            self._buffer.append(text)
            self._update_live_markdown()
        elif event_type == "team_text_delta":
            self._flush_thinking()
            scope = _team_scope(event)
            if scope != self._team_output_scope:
                if self._team_output_scope is not None:
                    self.console.print()
                self.console.print(Text(f"[{_team_title(event)}]", style="bold #22d3ee"))
                self._team_output_scope = scope
            self.console.print(Text(str(event.get("text") or ""), style="#e5e7eb"), end="")
        elif event_type == "team_status":
            self._finish_team_output()
            self._flush_thinking()
            self._flush_markdown(title="Team")
            message = str(event.get("message") or "")
            style = "bold red" if event.get("error") else "bold #22d3ee"
            self.console.print(Text(f"[{_team_title(event)}] {message}", style=style))
        elif event_type == "subagent_event":
            self._handle_subagent_event(event)
        elif event_type == "thinking_delta":
            scope = _thinking_scope(event)
            if self._thinking_buffer and scope != self._thinking_scope:
                self._flush_thinking()
            self._thinking_scope = scope
            thinking = str(event.get("thinking") or "")
            self._thinking_buffer.append(thinking)
            self._update_live_thinking()
        elif event_type == "plan_status":
            self._flush_thinking()
            self._flush_markdown(title="Plan")
        elif event_type == "plan_task_started":
            self._flush_thinking()
            self._flush_markdown(title="Plan")
            task_id = str(event.get("task_id") or "task")
            description = str(event.get("task_description") or "")
            self.console.print(
                _output_panel(
                    Text(description, style="#e5e7eb"),
                    title=Text(f"Running {task_id}", style="bold #22d3ee"),
                    border_style="#0891b2",
                )
            )
        elif event_type == "usage":
            self._run_saw_usage_event = True
            self._record_usage(event.get("usage") or {})
        elif event_type == "context_summary":
            self._flush_thinking()
            self._flush_markdown(title="Assistant Output")
            self._print_context_summary(event)
        elif event_type == "turn_complete":
            if event.get("agent"):
                self._finish_team_output()
                return
            stop_reason = str(event.get("stop_reason") or "end_turn")
            title = "Assistant Output" if stop_reason == "tool_use" else "Final Output"
            self._flush_thinking()
            self._flush_markdown(title=title)
        elif event_type == "tool_call":
            if event.get("agent"):
                self._finish_team_output()
            self._flush_thinking()
            self._flush_markdown(title="Assistant Output")
            self._print_tool_call(event)
        elif event_type == "tool_result":
            if event.get("agent"):
                self._finish_team_output()
            self._flush_thinking()
            self._flush_markdown(title="Assistant Output")
            self._print_tool_result(event)
        elif event_type == "error":
            self._flush_thinking()
            self._flush_markdown(title="Assistant Output")
            self.console.print(f"[red]Error:[/red] {event.get('error')}")
        elif event_type == "done":
            if not self._run_saw_usage_event:
                self._record_usage(event.get("usage") or {})
            self._flush_thinking()
            self._finish_team_output()
            self._flush_markdown(title="Final Output")
            self._record_run_summary(event)

    def markdown(self, text: str) -> None:
        self.console.print(Markdown(text))

    def newline(self) -> None:
        self._flush_thinking()
        self._finish_team_output()
        self._flush_markdown(title="Final Output")
        self.console.print()

    def _finish_team_output(self) -> None:
        if self._team_output_scope is None:
            return
        self.console.print()
        self._team_output_scope = None

    def _flush_markdown(self, *, title: str) -> None:
        if not self._buffer:
            return
        text = "".join(self._buffer)
        self._buffer.clear()
        self._stop_live_markdown()
        if text.strip():
            self.console.print(
                _output_panel(
                    Markdown(text),
                    title=Text(title, style="bold #a8ff60"),
                    border_style="#3f3f46",
                )
            )

    def _update_live_markdown(self) -> None:
        if not self._live_markdown or not self.console.is_terminal:
            return
        text = "".join(self._buffer)
        if not text.strip():
            return
        renderable = _output_panel(
            Markdown(text),
            title=Text("Assistant Output", style="bold #a8ff60"),
            border_style="#3f3f46",
        )
        if self._live is None:
            self._live = Live(
                renderable,
                console=self.console,
                refresh_per_second=12,
                transient=True,
                vertical_overflow="visible",
            )
            self._live.start(refresh=True)
            return
        self._live.update(renderable, refresh=True)

    def _stop_live_markdown(self) -> None:
        if self._live is None:
            return
        self._live.stop()
        self._live = None

    def _flush_thinking(self) -> None:
        if not self._thinking_buffer:
            return
        text = "".join(self._thinking_buffer)
        self._thinking_buffer.clear()
        scope = self._thinking_scope
        self._thinking_scope = None
        self._stop_live_thinking()
        if text.strip():
            self.console.print(
                _output_panel(
                    Text(text, style="dim"),
                    title=Text(_thinking_title(scope), style="bold #c084fc"),
                    border_style="#6d28d9",
                )
            )

    def _update_live_thinking(self) -> None:
        if not self.console.is_terminal:
            return
        text = "".join(self._thinking_buffer)
        if not text.strip():
            return
        renderable = _output_panel(
            Text(text, style="dim"),
            title=Text(_thinking_title(self._thinking_scope), style="bold #c084fc"),
            border_style="#6d28d9",
        )
        if self._thinking_live is None:
            self._thinking_live = Live(
                renderable,
                console=self.console,
                refresh_per_second=12,
                transient=True,
                vertical_overflow="visible",
            )
            self._thinking_live.start(refresh=True)
            return
        self._thinking_live.update(renderable, refresh=True)

    def _stop_live_thinking(self) -> None:
        if self._thinking_live is None:
            return
        self._thinking_live.stop()
        self._thinking_live = None

    def _record_usage(self, usage: dict[str, Any]) -> None:
        input_tokens = int(usage.get("input_tokens") or 0)
        output_tokens = int(usage.get("output_tokens") or 0)
        self._input_tokens += input_tokens
        self._output_tokens += output_tokens
        self._cache_hit_tokens += int(usage.get("cache_hit_tokens") or 0)
        self._cache_miss_tokens += int(usage.get("cache_miss_tokens") or 0)
        self._cache_write_tokens += int(usage.get("cache_write_tokens") or 0)
        cache_context = usage.get("cache_context_tokens")
        if cache_context is None:
            cache_context = int(usage.get("input_tokens") or 0) or (
                int(usage.get("cache_hit_tokens") or 0)
                + int(usage.get("cache_miss_tokens") or 0)
                + int(usage.get("cache_write_tokens") or 0)
            )
        self._cache_context_tokens += int(cache_context or 0)
        if input_tokens:
            self._last_input_tokens = input_tokens

    def _cache_hit_rate(self) -> float | None:
        if not self._cache_context_tokens:
            return None
        return min(1.0, self._cache_hit_tokens / self._cache_context_tokens)

    def _print_tool_call(self, event: dict[str, Any]) -> None:
        name = str(event.get("name") or "unknown")
        payload = event.get("input") or {}
        body = Table.grid(padding=(0, 1))
        body.add_column(style="dim", no_wrap=True)
        body.add_column()
        body.add_row("name", Text(name, style="bold #facc15"))
        body.add_row("input", Text(_format_payload(payload), style="#e5e7eb"))
        self.console.print(
            _output_panel(
                body,
                title=Text(_scoped_title("Tool Use", event), style="bold #facc15"),
                border_style="#facc15",
            )
        )

    def _print_context_summary(self, event: dict[str, Any]) -> None:
        roles = event.get("role_counts") or {}
        role_text = ", ".join(f"{name} {count}" for name, count in sorted(roles.items()))
        tokens = int(event.get("estimated_input_tokens") or 0)
        window = int(event.get("context_window") or 0)
        ratio = f"{tokens / window:.0%}" if window else "unknown"
        details = (
            f"step {event.get('step_id', '-')}: {event.get('message_count', 0)} messages"
            f" ({role_text or 'empty'}), {event.get('tool_count', 0)} tools, "
            f"~{tokens:,} input tokens / {ratio} of context window"
        )
        if event.get("compaction_id"):
            details += "; compacted history"
        self.console.print(Text(f"Context · {details}", style="dim"))

    def _print_tool_result(self, event: dict[str, Any]) -> None:
        is_error = bool(event.get("is_error"))
        name = str(event.get("name") or "unknown")
        result = str(event.get("result") or "")
        if len(result) > 1200:
            result = result[:1200] + "\n... [truncated]"
        title_style = "bold #ff4d5a" if is_error else "bold #22c55e"
        border_style = "#ff4d5a" if is_error else "#22c55e"
        status = "error" if is_error else "ok"
        self.console.print(
            _output_panel(
                result or "(empty result)",
                title=Text(
                    _scoped_title(f"Tool Result · {name} · {status}", event),
                    style=title_style,
                ),
                border_style=border_style,
            )
        )

    def _handle_subagent_event(self, event: dict[str, Any]) -> None:
        agent = str(event.get("agent") or "subagent")
        inner = event.get("event") or {}
        kind = str(inner.get("type") or event.get("kind") or "event")
        if kind == "status":
            if self.console.is_terminal:
                self.console.print(Text(format_status(inner.get("snapshot") or {}), style="dim"))
            return
        if kind == "phase_changed":
            wait = inner.get("waiting_reason")
            phase = "waiting for approval" if wait == "approval" else inner.get("phase", "unknown")
            self.console.print(Text(f"[{agent}] {phase}", style="dim"))
            return
        if kind == "ended":
            status = str(inner.get("status") or "unknown")
            cleanup = inner.get("cleanup_error")
            detail = f"; cleanup failed: {cleanup}" if cleanup else ""
            self.console.print(
                Text(
                    f"[{agent}] ended: {status}{detail}",
                    style="bold red" if cleanup else "bold #22c55e",
                )
            )
            return
        if kind == "observation_gap":
            self.console.print(
                Text(
                    f"[{agent}] observation gap: {inner.get('dropped', 0)} events omitted",
                    style="yellow",
                )
            )
            for snapshot in inner.get("snapshots", []):
                self.console.print(Text(format_status(snapshot), style="dim"))
            return
        if kind == "turn_completed":
            self.console.print(Text(f"[{agent}] turn completed", style="dim"))
            return
        if kind == "idle":
            self.console.print(Text(f"[{agent}] waiting for instructions", style="dim"))
            return
        if kind == "text_delta":
            self.console.print(
                Text(f"[{agent}] {inner.get('text') or ''}", style="#c7f9cc"), end=""
            )
            return
        if kind == "thinking_delta":
            self.console.print(
                Text(f"[{agent} thinking] {inner.get('thinking') or ''}", style="dim"), end=""
            )
            return
        if kind == "tool_call":
            self._print_tool_call({**inner, "agent": agent})
            return
        if kind == "tool_result":
            self._print_tool_result({**inner, "agent": agent})
            return
        if kind == "done":
            self.console.print(Text(f"[{agent}] completed", style="bold #22c55e"))
            return
        if kind == "error":
            self.console.print(Text(f"[{agent}] error: {inner.get('error')}", style="bold red"))
            return
        self.console.print(Text(f"[{agent}] {kind}", style="dim"))

    def _record_run_summary(self, event: dict[str, Any]) -> None:
        total_tokens = int(event.get("total_tokens") or self._input_tokens + self._output_tokens)
        turns = int(event.get("total_turns") or 0)
        has_usage = total_tokens > 0 or self._input_tokens > 0 or self._output_tokens > 0
        context_ratio = (
            self._last_input_tokens / self._context_window if self._context_window > 0 else 0
        )
        self._last_turns = turns
        self._last_total_tokens = total_tokens
        self._last_context_ratio = context_ratio
        self._last_has_usage = has_usage

    def _identity_panel(self, *, version: str, api_key_configured: bool) -> Table:
        logo = Text("\n".join(_PI_LOGO), style="bold #a8ff60")
        identity = Text()
        identity.append("PaiCLI ", style="bold white")
        identity.append(f"v{version}", style="dim")
        identity.append("\n\n")
        if api_key_configured:
            identity.append("Signed in ", style="bold white")
            identity.append("API Key", style="dim")
        else:
            identity.append("Missing ", style="bold red")
            identity.append("API Key", style="dim")

        grid = Table.grid(padding=(0, 2))
        grid.add_column(no_wrap=True)
        grid.add_column()
        grid.add_row(logo, Align.center(identity, vertical="middle"))
        return grid

    def _release_panel(self, *, version: str) -> Panel:
        notes = Text()
        for line in [
            "π logo home layout for the interactive CLI",
            "MCP, skills, tools, and workspace status at a glance",
            "Use /help for commands and /config for runtime settings",
        ]:
            notes.append("- ", style="dim")
            notes.append(line, style="dim")
            notes.append("\n")
        notes.append("/help", style="purple")
        notes.append(" for more", style="dim")
        return Panel(
            notes,
            title=Text(f"What's new (v{version})", style="bold green"),
            border_style="grey37",
            box=box.ROUNDED,
            padding=(0, 2),
        )


_PI_LOGO = (
    "████████████",
    "  ██    ██  ",
    "  ██    ██  ",
    "  ██    ██  ",
    "  ██    ██  ",
    "  ██    ██  ",
)


def _format_payload(payload: Any) -> str:
    try:
        return json.dumps(payload, ensure_ascii=False, indent=2)
    except TypeError:
        return str(payload)


def _thinking_scope(event: dict[str, Any]) -> str | None:
    agent = str(event.get("agent") or "").strip()
    phase = str(event.get("phase") or "").strip()
    task_id = str(event.get("task_id") or "").strip()
    if agent:
        return " · ".join(value for value in (phase, agent, task_id) if value)
    if task_id:
        return task_id
    if event.get("phase") == "planning":
        return "planning"
    return None


def _thinking_title(scope: str | None) -> str:
    if scope == "planning":
        return "Thinking · planning"
    if scope:
        return f"Thinking · {scope}"
    return "Thinking"


def _team_scope(event: dict[str, Any]) -> str:
    return ":".join(str(event.get(key) or "") for key in ("phase", "agent", "task_id"))


def _team_title(event: dict[str, Any]) -> str:
    parts = [
        str(event.get("phase") or "team").capitalize(),
        str(event.get("agent") or "").strip(),
        str(event.get("task_id") or "").strip(),
    ]
    return " · ".join(part for part in parts if part)


def _scoped_title(title: str, event: dict[str, Any]) -> str:
    agent = str(event.get("agent") or "").strip()
    phase = str(event.get("phase") or "").strip()
    task_id = str(event.get("task_id") or "").strip()
    scope = " · ".join(value for value in (phase, agent, task_id) if value)
    return f"{title} · {scope}" if scope else title


def _output_panel(renderable: Any, *, title: Text, border_style: str) -> Panel:
    return Panel(
        renderable,
        title=title,
        border_style=border_style,
        box=box.ROUNDED,
        expand=True,
    )
