from __future__ import annotations

import asyncio
import os
import shutil

import pytest

from paicli.config import load_config
from paicli.tools import ToolRegistry, get_builtin_tools
from paicli.tools.base import ToolContext
from paicli.tools.builtins import save_memory, search_memory
from paicli.tools.executor import ToolExecutor


def test_read_write_file_tool(tmp_path):
    config = load_config(project_root=tmp_path)
    config.policy.hitl_mode = "never"
    registry = ToolRegistry()
    registry.register_all(get_builtin_tools())
    context = ToolContext(cwd=str(tmp_path), config=config)

    async def run():
        write = registry.get("write_file")
        read = registry.get("read_file")
        assert write and read
        write_result = await write.execute(
            {"path": "hello.txt", "content": "hello\nworld\n"},
            context,
        )
        read_result = await read.execute({"path": "hello.txt"}, context)
        return write_result, read_result

    write_result, read_result = asyncio.run(run())
    assert not write_result.is_error
    assert "1: hello" in read_result.content
    assert "2: world" in read_result.content


@pytest.mark.parametrize("name", ["write_file", "edit_file"])
@pytest.mark.parametrize("decision", [None, "approve"])
def test_file_mutations_require_explicit_approval(tmp_path, name, decision):
    from paicli.config import PaiCliConfig

    config = PaiCliConfig()
    config.policy.audit_log_path = str(tmp_path / "audit.jsonl")
    target = tmp_path / "note.txt"
    target.write_text("original", encoding="utf-8")
    requests = []

    def approve(request):
        requests.append(request)
        return decision

    registry = ToolRegistry()
    registry.register_all(get_builtin_tools())
    context = ToolContext(
        cwd=str(tmp_path), config=config, approval_callback=approve if decision else None
    )
    payload = (
        {"path": "note.txt", "content": "updated"}
        if name == "write_file"
        else {"path": "note.txt", "old_text": "original", "new_text": "updated"}
    )
    call = {"id": "call-file", "name": name, "arguments": payload}
    result = asyncio.run(ToolExecutor(registry).execute_all([call], context))[0]

    assert result.is_error is (decision != "approve")
    assert target.read_text(encoding="utf-8") == (
        "updated" if decision == "approve" else "original"
    )
    assert len(requests) == (0 if decision is None else 1)
    assert registry.get(name).requires_approval


def test_memory_tools_save_metadata_and_recall_relevant_items(tmp_path):
    config = load_config(project_root=tmp_path)
    context = ToolContext(cwd=str(tmp_path), config=config)

    saved = asyncio.run(
        save_memory(
            {
                "content": "用户偏好用 uv 执行 Python 测试",
                "kind": "preference",
                "importance": 0.9,
            },
            context,
        )
    )
    recalled = asyncio.run(search_memory({"query": "怎么执行测试"}, context))

    assert not saved.is_error
    assert not recalled.is_error
    assert "uv" in recalled.content
    assert "preference" in recalled.content


@pytest.mark.integration
@pytest.mark.skipif(os.name != "nt", reason="PowerShell shell integration is Windows-specific")
@pytest.mark.parametrize(
    ("command", "expected_error", "expected_output"),
    [
        ("Write-Output 'PowerShell works'", False, "PowerShell works"),
        ("Get-Content -LiteralPath 'missing-file.txt'", True, "missing-file.txt"),
        ('cmd.exe /c "exit 7"', True, ""),
    ],
)
def test_bash_tool_uses_powershell_and_preserves_failures(
    tmp_path,
    command,
    expected_error,
    expected_output,
):
    if not (shutil.which("pwsh") or shutil.which("powershell.exe")):
        pytest.skip("PowerShell executable is unavailable")

    config = load_config(project_root=tmp_path)
    config.policy.hitl_mode = "never"
    context = ToolContext(cwd=str(tmp_path), config=config)
    tool = next(tool for tool in get_builtin_tools() if tool.name == "bash")

    result = asyncio.run(tool.execute({"command": command}, context))

    assert result.is_error is expected_error
    if expected_output:
        assert expected_output in result.content
