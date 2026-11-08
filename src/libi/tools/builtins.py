from __future__ import annotations

import asyncio
import hashlib
import os
import re
import shutil
import signal
import subprocess
from contextlib import suppress
from pathlib import Path
from typing import Any
from urllib.parse import urldefrag

from libi.memory import MemoryManager
from libi.policy import CommandGuard
from libi.rag import CodeIndex
from libi.session.models import WebDocument
from libi.skill import SkillRegistry
from libi.snapshot import SnapshotService
from libi.tools import file_ops as fops
from libi.tools.base import Tool, ToolContext, ToolResult, object_schema
from libi.tools.file_ops import FileOpResult
from libi.web import fetch_url, search_web
from libi.web.citations import ResearchSource
from libi.web.fetch import TRUNCATION_MARKER


def get_builtin_tools(*, session_tools: bool = True) -> list[Tool]:
    tools = [
        Tool(
            name="read_file",
            description=(
                "Read numbered lines from a workspace file. Read the range needed to understand "
                "the relevant code. Output is capped at 50 KiB of numbered text and includes "
                "a continuation offset when more lines remain."
            ),
            parameters=object_schema(
                {
                    "path": {"type": "string", "description": "Path to read"},
                    "offset": {"type": "number", "description": "Start line, 1-based"},
                    "limit": {"type": "number", "description": "Maximum lines; defaults to 500"},
                },
                ["path"],
            ),
            required_keys=["path"],
            handler=_read_file,
        ),
        Tool(
            name="write_file",
            description="Write a UTF-8 text file inside the current workspace.",
            parameters=object_schema(
                {
                    "path": {"type": "string", "description": "Path to write"},
                    "content": {"type": "string", "description": "File content"},
                    "append": {"type": "boolean", "description": "Append instead of overwrite"},
                },
                ["path", "content"],
            ),
            required_keys=["path", "content"],
            handler=_write_file,
            is_read_only=False,
            is_concurrency_safe=False,
            danger_level="medium",
            requires_approval=True,
        ),
        Tool(
            name="edit_file",
            description=(
                "Make line-based edits to a text file. Each edit replaces exact line sequences "
                "with new content. Returns a git-style diff showing the changes made."
            ),
            parameters=object_schema(
                {
                    "path": {"type": "string", "description": "File path to edit"},
                    "old_text": {
                        "type": "string",
                        "description": "Text to search for — must match exactly",
                    },
                    "new_text": {
                        "type": "string",
                        "description": "Text to replace with",
                    },
                    "dry_run": {
                        "type": "boolean",
                        "description": "Preview changes without modifying the file",
                    },
                },
                ["path", "old_text", "new_text"],
            ),
            required_keys=["path", "old_text", "new_text"],
            handler=_edit_file,
            is_read_only=False,
            is_concurrency_safe=False,
            danger_level="medium",
            requires_approval=True,
            needs_workspace_checkpoint=False,
        ),
        Tool(
            name="list_dir",
            description="List entries in a directory inside the current workspace.",
            parameters=object_schema(
                {"path": {"type": "string", "description": "Directory path"}},
                ["path"],
            ),
            required_keys=["path"],
            handler=_list_dir,
        ),
        Tool(
            name="glob",
            description="Find files by glob pattern inside the current workspace.",
            parameters=object_schema(
                {
                    "pattern": {"type": "string", "description": "Glob pattern"},
                    "limit": {"type": "number", "description": "Maximum results"},
                },
                ["pattern"],
            ),
            required_keys=["pattern"],
            handler=_glob_files,
        ),
        Tool(
            name="grep",
            description="Search text in workspace files.",
            parameters=object_schema(
                {
                    "pattern": {"type": "string", "description": "Regex or plain text pattern"},
                    "path": {"type": "string", "description": "Optional path to search"},
                    "regex": {"type": "boolean", "description": "Treat pattern as regex"},
                    "limit": {"type": "number", "description": "Maximum matches"},
                },
                ["pattern"],
            ),
            required_keys=["pattern"],
            handler=_grep,
        ),
        Tool(
            name="directory_tree",
            description=(
                "Get a recursive tree view of files and directories as indented text. "
                "Each entry shows the name and type. Files have no children, "
                "while directories always show their contents."
            ),
            parameters=object_schema(
                {
                    "path": {
                        "type": "string",
                        "description": "Directory path (default: workspace root)",
                    },
                    "max_depth": {
                        "type": "number",
                        "description": "Maximum recursion depth (default: 3)",
                    },
                    "exclude_patterns": {
                        "type": "array",
                        "items": {"type": "string"},
                        "description": "Directory names to exclude from the tree",
                    },
                },
            ),
            required_keys=[],
            handler=_directory_tree,
        ),
        Tool(
            name="get_file_info",
            description=(
                "Retrieve detailed metadata about a file or directory — size, "
                "modification time, permissions, and type."
            ),
            parameters=object_schema(
                {"path": {"type": "string", "description": "Path to inspect"}},
                ["path"],
            ),
            required_keys=["path"],
            handler=_get_file_info,
        ),
        Tool(
            name="bash",
            description=(
                "Execute a command in the current workspace. On Windows, commands run through "
                "PowerShell (pwsh or Windows PowerShell), so use PowerShell syntax such as "
                "Get-ChildItem, Get-Content, and $env:NAME. Other platforms use their system shell."
            ),
            parameters=object_schema(
                {
                    "command": {"type": "string", "description": "Shell command"},
                    "timeout": {"type": "number", "description": "Timeout seconds"},
                },
                ["command"],
            ),
            required_keys=["command"],
            handler=_bash,
            is_read_only=False,
            is_concurrency_safe=False,
            danger_level="high",
            requires_approval=True,
            needs_workspace_checkpoint=False,
        ),
        Tool(
            name="web_search",
            description=(
                "Search the web for titles, URLs, and snippets. "
                "For research, start with 2–4 queries from different angles."
            ),
            parameters=object_schema(
                {
                    "query": {"type": "string", "description": "Search query"},
                    "max_results": {
                        "type": "integer",
                        "minimum": 1,
                        "maximum": 20,
                        "description": "Number of results (default 5)",
                    },
                },
                ["query"],
            ),
            required_keys=["query"],
            handler=_web_search,
        ),
        Tool(
            name="web_fetch",
            description=(
                "Fetch a public HTTP/HTTPS page. In a session, returns an excerpt and a "
                "response_id for web_read. Repeat requests reuse the saved page."
            ),
            parameters=object_schema(
                {
                    "url": {"type": "string", "description": "URL to fetch"},
                    "max_length": {"type": "number", "description": "Maximum returned characters"},
                },
                ["url"],
            ),
            required_keys=["url"],
            handler=_web_fetch,
        ),
        Tool(
            name="web_read",
            description="Read a saved webpage from this session by offset or literal text.",
            parameters=object_schema(
                {
                    "response_id": {"type": "string", "description": "ID returned by web_fetch"},
                    "offset": {"type": "integer", "minimum": 0, "description": "Character offset"},
                    "limit": {"type": "integer", "minimum": 1, "maximum": 30000},
                    "query": {"type": "string", "maxLength": 256},
                },
                ["response_id"],
            ),
            required_keys=["response_id"],
            handler=_web_read,
        ),
        Tool(
            name="save_memory",
            description=(
                "Save an explicit or durable fact to long-term project memory. Use only for stable "
                "preferences, project constraints, user corrections, or reusable decisions; never "
                "store secrets, temporary task state, raw logs, or uncertain claims."
            ),
            parameters=object_schema(
                {
                    "content": {"type": "string", "description": "Durable fact to remember"},
                    "kind": {
                        "type": "string",
                        "enum": ["fact", "preference", "constraint", "correction", "decision"],
                    },
                    "importance": {"type": "number", "description": "Score from 0 to 1"},
                    "confidence": {"type": "number", "description": "Score from 0 to 1"},
                    "expires_at": {
                        "type": "string",
                        "description": "Optional ISO-8601 expiry for time-sensitive memory",
                    },
                },
                ["content"],
            ),
            required_keys=["content"],
            handler=_save_memory,
            is_read_only=False,
            is_concurrency_safe=False,
            danger_level="medium",
            needs_workspace_checkpoint=False,
        ),
        Tool(
            name="search_memory",
            description=(
                "Search relevance-ranked long-term project memory when prior preferences, "
                "corrections, constraints, or decisions may matter."
            ),
            parameters=object_schema(
                {
                    "query": {"type": "string", "description": "What to recall"},
                    "limit": {"type": "number", "description": "Maximum results"},
                    "kinds": {
                        "type": "array",
                        "items": {"type": "string"},
                        "description": "Optional memory kinds",
                    },
                },
                ["query"],
            ),
            required_keys=["query"],
            handler=_search_memory,
        ),
        Tool(
            name="load_skill",
            description="Load a named LiBi skill manual from user/project skill directories.",
            parameters=object_schema(
                {"name": {"type": "string", "description": "Skill name"}},
                ["name"],
            ),
            required_keys=["name"],
            handler=_load_skill,
        ),
        Tool(
            name="save_skill",
            description=(
                "Persist a reusable LiBi workflow as a project or user skill after user approval."
            ),
            parameters=object_schema(
                {
                    "name": {"type": "string", "description": "Lowercase skill slug"},
                    "description": {
                        "type": "string",
                        "description": "When this skill should be used",
                    },
                    "content": {"type": "string", "description": "Reusable skill instructions"},
                    "scope": {
                        "type": "string",
                        "enum": ["project", "user"],
                        "description": "Where to persist the skill",
                    },
                    "version": {"type": "string", "description": "Skill version"},
                    "tags": {
                        "type": "array",
                        "items": {"type": "string"},
                        "description": "Matching keywords",
                    },
                    "overwrite": {
                        "type": "boolean",
                        "description": "Update an existing skill instead of failing",
                    },
                },
                ["name", "description", "content"],
            ),
            required_keys=["name", "description", "content"],
            handler=_save_skill,
            is_read_only=False,
            is_concurrency_safe=False,
            danger_level="medium",
            requires_approval=True,
            needs_workspace_checkpoint=False,
        ),
        Tool(
            name="search_code",
            description="Search the local code index for semantically relevant lines.",
            parameters=object_schema(
                {
                    "query": {"type": "string", "description": "Search query"},
                    "limit": {"type": "number", "description": "Maximum matches"},
                },
                ["query"],
            ),
            required_keys=["query"],
            handler=_search_code,
        ),
        Tool(
            name="revert_turn",
            description="Restore the workspace to a previous LiBi side-history snapshot.",
            parameters=object_schema(
                {"snapshot": {"type": "string", "description": "Snapshot id or 1-based index"}},
                ["snapshot"],
            ),
            required_keys=["snapshot"],
            handler=_revert_turn,
            is_read_only=False,
            is_concurrency_safe=False,
            danger_level="high",
            requires_approval=True,
            needs_workspace_checkpoint=False,
        ),
    ]
    return tools if session_tools else [tool for tool in tools if tool.name != "web_read"]


# ---------------------------------------------------------------------------
# Handler: file operations (thin wrappers over file_ops)
# ---------------------------------------------------------------------------


async def _read_file(payload: dict[str, Any], context: ToolContext) -> ToolResult:
    result: FileOpResult = fops.read_file(
        context.cwd,
        str(payload["path"]),
        offset=int(payload.get("offset") or 1),
        limit=int(payload.get("limit") or 500),
        path_guard_enabled=context.config.policy.path_guard_enabled,
    )
    return _to_tool_result(result)


async def _write_file(payload: dict[str, Any], context: ToolContext) -> ToolResult:
    result: FileOpResult = fops.write_file(
        context.cwd,
        str(payload["path"]),
        str(payload["content"]),
        append=bool(payload.get("append")),
        path_guard_enabled=context.config.policy.path_guard_enabled,
    )
    return _to_tool_result(result)


async def _edit_file(payload: dict[str, Any], context: ToolContext) -> ToolResult:
    dry_run = bool(payload.get("dry_run"))
    if not dry_run and context.before_workspace_mutation is not None:
        context.before_workspace_mutation()
    result: FileOpResult = fops.edit_file(
        context.cwd,
        str(payload["path"]),
        str(payload["old_text"]),
        str(payload["new_text"]),
        path_guard_enabled=context.config.policy.path_guard_enabled,
        dry_run=dry_run,
    )
    return _to_tool_result(result)


async def _list_dir(payload: dict[str, Any], context: ToolContext) -> ToolResult:
    result: FileOpResult = fops.list_directory(
        context.cwd,
        str(payload["path"]),
        path_guard_enabled=context.config.policy.path_guard_enabled,
    )
    return _to_tool_result(result)


async def _glob_files(payload: dict[str, Any], _context: ToolContext) -> ToolResult:
    result: FileOpResult = fops.glob_files(
        _context.cwd,
        str(payload["pattern"]),
        limit=int(payload.get("limit") or 100),
    )
    return _to_tool_result(result)


async def _grep(payload: dict[str, Any], context: ToolContext) -> ToolResult:
    result: FileOpResult = fops.grep(
        context.cwd,
        str(payload["pattern"]),
        path=str(payload.get("path") or "."),
        limit=int(payload.get("limit") or 100),
        use_regex=bool(payload.get("regex", True)),
        path_guard_enabled=context.config.policy.path_guard_enabled,
    )
    return _to_tool_result(result)


async def _directory_tree(payload: dict[str, Any], context: ToolContext) -> ToolResult:
    result: FileOpResult = fops.directory_tree(
        context.cwd,
        str(payload.get("path", ".")),
        max_depth=int(payload.get("max_depth") or 3),
        path_guard_enabled=context.config.policy.path_guard_enabled,
        exclude_patterns=tuple(payload.get("exclude_patterns") or ()),
    )
    return _to_tool_result(result)


async def _get_file_info(payload: dict[str, Any], context: ToolContext) -> ToolResult:
    result: FileOpResult = fops.get_file_info(
        context.cwd,
        str(payload["path"]),
        path_guard_enabled=context.config.policy.path_guard_enabled,
    )
    return _to_tool_result(result)


# ---------------------------------------------------------------------------
# Handler: bash
# ---------------------------------------------------------------------------


async def _bash(payload: dict[str, Any], context: ToolContext) -> ToolResult:
    command = str(payload["command"])
    if context.config.policy.command_guard_enabled:
        CommandGuard(context.config.policy.command_blacklist).validate(command)
    timeout = float(payload.get("timeout") or context.config.tools.timeout)
    powershell = _powershell_invocation(command) if os.name == "nt" else None
    if os.name == "nt" and powershell is None:
        return ToolResult(
            "PowerShell was not found. Install PowerShell 7 (pwsh) or enable Windows PowerShell.",
            is_error=True,
        )
    if context.before_workspace_mutation is not None:
        context.before_workspace_mutation()
    if powershell is None:
        proc = await asyncio.create_subprocess_shell(
            command,
            cwd=context.cwd,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
            env=os.environ.copy(),
            start_new_session=True,
        )
    else:
        proc = await asyncio.create_subprocess_exec(
            *powershell,
            cwd=context.cwd,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
            env=os.environ.copy(),
        )
    assert proc.stdout is not None and proc.stderr is not None
    capture = asyncio.gather(
        _read_command_pipe(proc.stdout),
        _read_command_pipe(proc.stderr),
    )
    try:
        async with asyncio.timeout(timeout):
            (stdout, out_truncated), (stderr, err_truncated) = await asyncio.shield(capture)
            await proc.wait()
    except (asyncio.CancelledError, TimeoutError) as exc:
        try:
            # Keep draining pipes while the process tree exits.
            with suppress(TimeoutError):
                await asyncio.wait_for(_stop_command(proc), timeout=5)
        finally:
            capture.cancel()
            await asyncio.gather(capture, return_exceptions=True)
        if isinstance(exc, asyncio.CancelledError):
            raise
        return ToolResult(f"Command timed out after {timeout:.0f}s", is_error=True)
    combined = stdout + stderr
    output = combined[:20_000].decode("utf-8", errors="replace")
    if out_truncated or err_truncated or len(combined) > 20_000:
        output += "\n... [truncated]"
    return ToolResult(
        output or f"(exit {proc.returncode}, no output)",
        is_error=proc.returncode != 0,
    )


async def _read_command_pipe(stream: asyncio.StreamReader) -> tuple[bytearray, bool]:
    output = bytearray()
    truncated = False
    while chunk := await stream.read(8192):
        remaining = 20_000 - len(output)
        output.extend(chunk[:remaining])
        truncated |= len(chunk) > remaining
    return output, truncated


async def _stop_command(proc: asyncio.subprocess.Process) -> None:
    # Descendants can outlive the shell and hold its output pipes open.
    if os.name == "nt" and proc.returncode is None:
        system_root = os.environ.get("SYSTEMROOT", r"C:\Windows")
        with suppress(OSError):
            killer = await asyncio.create_subprocess_exec(
                os.path.join(system_root, "System32", "taskkill.exe"),
                "/PID",
                str(proc.pid),
                "/T",
                "/F",
                stdout=asyncio.subprocess.DEVNULL,
                stderr=asyncio.subprocess.DEVNULL,
                creationflags=subprocess.CREATE_NO_WINDOW,
            )
            await killer.wait()
    elif os.name != "nt":
        with suppress(ProcessLookupError):
            os.killpg(proc.pid, signal.SIGKILL)
    if proc.returncode is None:
        with suppress(ProcessLookupError):
            proc.kill()
    await proc.wait()


def _powershell_invocation(command: str) -> list[str] | None:
    executable = (
        shutil.which("pwsh") or shutil.which("powershell.exe") or shutil.which("powershell")
    )
    if executable is None:
        return None

    script = f"""
[Console]::OutputEncoding = [System.Text.UTF8Encoding]::new($false)
$OutputEncoding = [Console]::OutputEncoding
$ErrorActionPreference = 'Stop'
$global:LASTEXITCODE = 0
try {{
{command}
    $commandSucceeded = $?
    if (-not $commandSucceeded) {{
        if ($global:LASTEXITCODE -is [int] -and $global:LASTEXITCODE -ne 0) {{
            exit $global:LASTEXITCODE
        }}
        exit 1
    }}
}} catch {{
    [Console]::Error.WriteLine($_.ToString())
    exit 1
}}
"""
    return [
        executable,
        "-NoLogo",
        "-NoProfile",
        "-NonInteractive",
        "-Command",
        script,
    ]


# ---------------------------------------------------------------------------
# Handler: web
# ---------------------------------------------------------------------------


async def _web_search(payload: dict[str, Any], context: ToolContext) -> ToolResult:
    max_results = max(1, min(int(payload.get("max_results") or payload.get("maxResults") or 5), 20))
    budget = context.research_budget
    if budget is not None and not budget.reserve("search"):
        return ToolResult(
            "Research search budget exhausted. Use the available evidence to finish.",
            is_error=True,
        )
    try:
        async with asyncio.timeout(budget.remaining_seconds if budget is not None else None):
            results = await search_web(str(payload["query"]), max_results=max_results)
    except Exception as exc:  # noqa: BLE001
        return ToolResult(f"Search error: {exc}", is_error=True)
    if not results:
        return ToolResult(f'No search results found for "{payload["query"]}".')
    blocks = [
        f"{index}. {result.title[:256]}\n{result.url}\n{result.snippet[:500]}"
        for index, result in enumerate(results, start=1)
    ]
    content = "\n\n".join(blocks)
    visible = budget.take_text(content) if budget is not None else content
    sources = []
    offset = 0
    for result, block in zip(results, blocks, strict=True):
        if offset + len(block) > len(visible):
            break
        source = ResearchSource.from_record(
            {"title": result.title, "url": result.url, "excerpt": result.snippet}
        )
        if source is not None:
            sources.append(source)
        offset += len(block) + 2
    if len(visible) < len(content):
        visible += "\n[Research evidence budget reached. Summarize the available evidence.]"
    return ToolResult(
        visible, display_summary=f"Search: {len(results)} results", sources=tuple(sources)
    )


_WEB_INLINE_LIMIT = 30_000
_WEB_DOCUMENT_LIMIT = 200_000


async def _fetch_text(url: str, limit: int, context: ToolContext) -> str:
    budget = context.research_budget
    if budget is not None and not budget.reserve("fetch"):
        raise ValueError("Research fetch budget exhausted. Use the available evidence to finish.")
    async with asyncio.timeout(budget.remaining_seconds if budget is not None else None):
        return await fetch_url(url, max_length=limit)


async def _web_fetch(payload: dict[str, Any], context: ToolContext) -> ToolResult:
    url = str(payload["url"])
    store, session_id = context.session_store, context.session_id
    in_session = store is not None and session_id is not None
    requested = payload.get("max_length") or payload.get("maxLength")
    limit = max(1, min(int(requested or (4_000 if in_session else 10_000)), _WEB_INLINE_LIMIT))
    try:
        if not in_session:
            budget = context.research_budget
            if budget is not None and budget.chars_left is not None:
                limit = min(limit, max(1, budget.chars_left))
            content = await _fetch_text(url, limit, context)
            return ToolResult(
                budget.take_text(content) if budget else content,
                display_summary=f"Fetched {url}",
            )
        url = urldefrag(url).url
        cache_key = hashlib.sha256(f"{context.turn_id or ''}\n{url}".encode()).hexdigest()
        async with context.web_fetch_locks.setdefault(cache_key, asyncio.Lock()):
            cached = store.get_web_document(session_id, cache_key, by_cache_key=True)
            if cached is not None:
                return ToolResult(
                    f"Response ID: {cached.response_id}\nURL: {cached.url}\n"
                    "Page already saved. Use web_read for a different excerpt."
                )
            text = await _fetch_text(url, _WEB_DOCUMENT_LIMIT, context)
            document = store.save_web_document(
                session_id,
                cache_key,
                url,
                text.removesuffix(TRUNCATION_MARKER),
                max_chars=_WEB_DOCUMENT_LIMIT,
                truncated=text.endswith(TRUNCATION_MARKER),
            )
    except Exception as exc:  # noqa: BLE001
        return ToolResult(f"Fetch error: {exc}", is_error=True)
    return _web_excerpt(document, context, start=0, limit=limit)


async def _web_read(payload: dict[str, Any], context: ToolContext) -> ToolResult:
    if context.session_store is None or context.session_id is None:
        return ToolResult("Web reads require a LiBi session.", is_error=True)
    document = context.session_store.get_web_document(
        context.session_id, str(payload["response_id"])
    )
    if document is None:
        return ToolResult("No saved webpage with that response ID in this session.", is_error=True)
    offset = max(0, min(len(document.text), int(payload.get("offset") or 0)))
    limit = max(1, min(int(payload.get("limit") or 4_000), _WEB_INLINE_LIMIT))
    query = str(payload.get("query") or "").strip()
    if query:
        if len(query) > 256:
            return ToolResult("Search text must be 256 characters or shorter.", is_error=True)
        match = re.compile(re.escape(query), re.IGNORECASE).search(document.text, offset)
        if match is None:
            return ToolResult(f"No matching text in the saved page from offset {offset}.")
        offset = max(offset, match.start() - limit // 3)
    return _web_excerpt(document, context, start=offset, limit=limit)


def _web_excerpt(
    document: WebDocument, context: ToolContext, *, start: int, limit: int
) -> ToolResult:
    start = min(start, len(document.text))
    excerpt = document.text[start : start + limit]
    budget = context.research_budget
    text = budget.take_text(excerpt) if budget else excerpt
    end = start + len(text)
    header = f"Response ID: {document.response_id}\nURL: {document.url}\n"
    if len(text) < len(excerpt):
        header += "Research evidence budget reached. Summarize the available evidence.\n"
    elif end < len(document.text):
        header += f"Next offset: {end}\n"
    if document.truncated:
        header += "The stored page is limited to its fetched prefix.\n"
    title = re.search(r"(?m)^# (.+)$", document.text[:1024])
    source = ResearchSource.from_record(
        {
            "url": document.url,
            "title": title[1] if title else None,
            "excerpt": text,
            "response_id": document.response_id,
        }
    )
    return ToolResult(
        header + f"Excerpt {start}–{end}:\n\n" + text,
        display_summary=f"Web excerpt: {document.url}",
        sources=(source,) if source is not None else (),
    )


# ---------------------------------------------------------------------------
# Handler: memory
# ---------------------------------------------------------------------------


async def _save_memory(payload: dict[str, Any], context: ToolContext) -> ToolResult:
    if not context.config.features.memory or not context.config.memory.long_term_enabled:
        return ToolResult("Long-term memory is disabled.", is_error=True)
    if context.before_workspace_mutation is not None:
        path = Path(context.config.memory.long_term_db_path).expanduser().resolve()
        if path.is_relative_to(Path(context.cwd).resolve()):
            context.before_workspace_mutation()
    manager = MemoryManager(
        context.config.memory.long_term_db_path,
        scope=context.cwd,
        max_entries=context.config.memory.max_long_term_entries,
        max_content_length=context.config.memory.max_memory_chars,
    )
    memory_id = manager.save(
        str(payload["content"]),
        kind=str(payload.get("kind") or "fact"),
        source="agent",
        importance=float(payload.get("importance", 0.5)),
        confidence=float(payload.get("confidence", 1.0)),
        expires_at=payload.get("expires_at"),
    )
    return ToolResult(f"Saved memory #{memory_id}")


async def _search_memory(payload: dict[str, Any], context: ToolContext) -> ToolResult:
    if not context.config.features.memory or not context.config.memory.long_term_enabled:
        return ToolResult("Long-term memory is disabled.", is_error=True)
    raw_kinds = payload.get("kinds")
    if raw_kinds is not None and not isinstance(raw_kinds, list):
        return ToolResult("search_memory kinds must be an array of strings.", is_error=True)
    manager = MemoryManager(
        context.config.memory.long_term_db_path,
        scope=context.cwd,
        max_entries=context.config.memory.max_long_term_entries,
        max_content_length=context.config.memory.max_memory_chars,
    )
    rows = manager.recall(
        str(payload["query"]),
        limit=int(payload.get("limit") or context.config.memory.recall_limit),
        kinds=[str(kind) for kind in raw_kinds] if raw_kinds else None,
        min_score=context.config.memory.recall_min_score,
    )
    if not rows:
        return ToolResult("(no relevant long-term memory)")
    content = "\n".join(
        f"#{row.id} [{row.kind}, importance={row.importance:.2f}] {row.content}" for row in rows
    )
    return ToolResult(content, display_summary=f"Recalled {len(rows)} memories")


# Keep the original handler imports working for SDK users and existing tests.
save_memory = _save_memory
search_memory = _search_memory


# ---------------------------------------------------------------------------
# Handler: skill
# ---------------------------------------------------------------------------


async def _load_skill(payload: dict[str, Any], context: ToolContext) -> ToolResult:
    if not context.config.features.skill:
        return ToolResult("Skills are disabled.", is_error=True)
    skill = SkillRegistry(context.cwd).load(str(payload["name"]))
    if not skill:
        return ToolResult(f'Skill "{payload["name"]}" not found or disabled.', is_error=True)
    content = skill.body or skill.content
    if len(content) > 5_000:
        content = content[:5_000] + "\n... [truncated; use /skill show for the full skill]"
    if context.skill_context_buffer:
        context.skill_context_buffer.push(skill.name, content)
        return ToolResult(
            f'Loaded skill "{skill.name}" instructions for the next model turn.',
            display_summary=f"Loaded skill {skill.name}",
        )
    return ToolResult(content, display_summary=f"Loaded skill {skill.name}")


async def _save_skill(payload: dict[str, Any], context: ToolContext) -> ToolResult:
    if not context.config.features.skill:
        return ToolResult("Skills are disabled.", is_error=True)
    raw_tags = payload.get("tags") or []
    if not isinstance(raw_tags, list):
        return ToolResult("save_skill tags must be an array of strings.", is_error=True)
    tags = [str(tag).strip() for tag in raw_tags if str(tag).strip()]
    registry = SkillRegistry(context.cwd)
    scope = str(payload.get("scope") or "project")
    name = str(payload["name"])
    if scope == "project" and context.before_workspace_mutation is not None:
        context.before_workspace_mutation()
    try:
        if bool(payload.get("overwrite")):
            skill = registry.update(
                name,
                description=str(payload["description"]),
                body=str(payload["content"]),
                scope=scope,
                version=str(payload.get("version") or "1.0.0"),
                tags=tags,
            )
        else:
            skill = registry.create(
                name,
                description=str(payload["description"]),
                body=str(payload["content"]),
                scope=scope,
                version=str(payload.get("version") or "1.0.0"),
                tags=tags,
            )
    except (OSError, ValueError) as exc:
        return ToolResult(f"save_skill failed: {exc}", is_error=True)
    return ToolResult(
        f'Saved skill "{skill.name}" in {scope} scope at {skill.path}.',
        display_summary=f"Saved skill {skill.name}",
    )


load_skill = _load_skill


# ---------------------------------------------------------------------------
# Handler: code search
# ---------------------------------------------------------------------------


async def _search_code(payload: dict[str, Any], context: ToolContext) -> ToolResult:
    index = CodeIndex(context.cwd)
    results = index.search(str(payload["query"]), limit=int(payload.get("limit") or 20))
    if not results:
        return ToolResult("(no indexed matches; run /index first)")
    return ToolResult("\n".join(f"{item.path}:{item.line}: {item.snippet}" for item in results))


# ---------------------------------------------------------------------------
# Handler: snapshot revert
# ---------------------------------------------------------------------------


async def _revert_turn(payload: dict[str, Any], context: ToolContext) -> ToolResult:
    # Restore resolves the index before recording its own pre-restore checkpoint.
    record = SnapshotService(context.cwd).restore(str(payload["snapshot"]))
    return ToolResult(f"Restored snapshot {record.id}")


# ---------------------------------------------------------------------------
# Conversion helper
# ---------------------------------------------------------------------------


def _to_tool_result(result: FileOpResult) -> ToolResult:
    """Convert a FileOpResult to a ToolResult."""
    return ToolResult(
        content=result.content,
        is_error=result.is_error,
        display_summary=result.display_summary,
    )
