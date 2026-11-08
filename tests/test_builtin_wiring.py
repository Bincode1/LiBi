from __future__ import annotations

import asyncio
import shutil
import subprocess

import pytest

from libi.rag.code_index import CodeIndex
from libi.tools.executor import ToolExecutor


def test_file_discovery_tools_are_wired_through_executor(tmp_path, tool_context, tool_registry):
    (tmp_path / "note.txt").write_text("needle", encoding="utf-8")
    calls = [
        {"id": "list", "name": "list_dir", "arguments": {"path": "."}},
        {"id": "glob", "name": "glob", "arguments": {"pattern": "*.txt"}},
        {"id": "grep", "name": "grep", "arguments": {"pattern": "needle", "regex": False}},
        {"id": "tree", "name": "directory_tree", "arguments": {}},
        {"id": "info", "name": "get_file_info", "arguments": {"path": "note.txt"}},
    ]
    results = asyncio.run(ToolExecutor(tool_registry).execute_all(calls, tool_context))
    assert len(results) == 5
    assert all(not result.is_error for result in results), [result.content for result in results]
    assert all("note.txt" in result.content for result in results)


def test_git_commands_use_the_builtin_shell(tmp_path, tool_context, tool_registry):
    git = shutil.which("git")
    if git is None:
        pytest.skip("Git is unavailable")
    subprocess.run([git, "init", "-q", str(tmp_path)], check=True)
    (tmp_path / "note.txt").write_text("note", encoding="utf-8")
    tool_context.approval_callback = lambda _request: "approve"
    calls = [
        {"id": "status", "name": "bash", "arguments": {"command": "git status --porcelain"}},
        {"id": "error", "name": "bash", "arguments": {"command": "git rev-parse HEAD"}},
    ]

    status, error = asyncio.run(ToolExecutor(tool_registry).execute_all(calls, tool_context))

    assert not status.is_error and "?? note.txt" in status.content
    assert error.is_error and "HEAD" in error.content


@pytest.mark.parametrize("feature", ["memory", "skill"])
def test_disabled_features_reject_tools_without_side_effects(
    tmp_path, tool_context, tool_registry, feature
):
    setattr(tool_context.config.features, feature, False)
    tool_context.config.policy.hitl_mode = "never"
    calls = (
        [
            {"id": "save", "name": "save_memory", "arguments": {"content": "fact"}},
            {"id": "search", "name": "search_memory", "arguments": {"query": "fact"}},
        ]
        if feature == "memory"
        else [
            {"id": "load", "name": "load_skill", "arguments": {"name": "missing"}},
            {
                "id": "save",
                "name": "save_skill",
                "arguments": {"name": "new-skill", "description": "desc", "content": "body"},
            },
        ]
    )
    results = asyncio.run(ToolExecutor(tool_registry).execute_all(calls, tool_context))
    assert all(result.is_error and "disabled" in result.content for result in results)
    assert not (tmp_path / "memory.db").exists()
    assert not (tmp_path / ".libi/skills").exists()


def test_code_search_tool_uses_real_index(tmp_path, tool_context, tool_registry):
    (tmp_path / "app.py").write_text("needle = 42\n", encoding="utf-8")
    tool = tool_registry.get("search_code")
    empty = asyncio.run(tool.execute({"query": "needle"}, tool_context))
    assert "run /index first" in empty.content
    CodeIndex(tmp_path).rebuild()
    found = asyncio.run(tool.execute({"query": "needle"}, tool_context))
    assert not found.is_error and "app.py:1: needle = 42" in found.content


@pytest.mark.parametrize("fails", [False, True])
def test_web_tool_handlers_report_results_and_errors(
    tool_context, tool_registry, monkeypatch, fails
):
    from libi.web.search import SearchResult

    async def search(_query, **_kwargs):
        if fails:
            raise RuntimeError("search unavailable")
        return [SearchResult("Title", "https://example.test", "Snippet")]

    async def fetch(_url, **_kwargs):
        if fails:
            raise RuntimeError("fetch unavailable")
        return "page body"

    monkeypatch.setattr("libi.tools.builtins.search_web", search)
    monkeypatch.setattr("libi.tools.builtins.fetch_url", fetch)
    results = asyncio.run(
        ToolExecutor(tool_registry).execute_all(
            [
                {"id": "search", "name": "web_search", "arguments": {"query": "query"}},
                {"id": "fetch", "name": "web_fetch", "arguments": {"url": "https://example.test"}},
            ],
            tool_context,
        )
    )
    assert all(result.is_error is fails for result in results)
    by_id = {result.tool_use_id: result for result in results}
    if fails:
        assert "search unavailable" in by_id["search"].content
        assert "fetch unavailable" in by_id["fetch"].content
    else:
        assert "Title" in by_id["search"].content and "Snippet" in by_id["search"].content
        assert by_id["fetch"].content == "page body"
