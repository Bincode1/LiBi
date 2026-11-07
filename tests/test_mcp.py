from __future__ import annotations

import asyncio
import json
import sys

import httpx
import pytest

from paicli.config import load_config
from paicli.entrypoints.repl import _count_mcp_servers
from paicli.mcp import McpClientManager
from paicli.mcp.server import _handle_request, serve_stdio
from paicli.tools.base import ToolContext


def test_mcp_tools_list(tmp_path):

    async def run():
        return await _handle_request(
            {"jsonrpc": "2.0", "id": 1, "method": "tools/list", "params": {}},
            str(tmp_path),
        )

    response = asyncio.run(run())
    tools = response["result"]["tools"]
    assert any(tool["name"] == "read_file" for tool in tools)
    names = {tool["name"] for tool in tools}
    assert {"glob", "grep", "bash"} <= names
    assert not {"glob_files", "grep_code", "execute_command"} & names


@pytest.mark.parametrize("method", ["initialize", "notifications/initialized", "unknown"])
def test_mcp_protocol_methods_preserve_request_id(tmp_path, method):
    response = asyncio.run(_handle_request({"id": 7, "method": method}, str(tmp_path)))
    assert response["id"] == 7 and response["jsonrpc"] == "2.0"
    assert ("error" in response) is (method == "unknown")


def test_mcp_tool_call_reads_workspace_and_reports_missing_tool(tmp_path):
    (tmp_path / "note.txt").write_text("hello", encoding="utf-8")
    response = asyncio.run(
        _handle_request(
            {
                "id": 1,
                "method": "tools/call",
                "params": {"name": "read_file", "arguments": {"path": "note.txt"}},
            },
            str(tmp_path),
        )
    )
    assert response["result"] == {
        "content": [{"type": "text", "text": "1: hello"}],
        "isError": False,
    }
    missing = asyncio.run(
        _handle_request(
            {"id": 2, "method": "tools/call", "params": {"name": "missing"}}, str(tmp_path)
        )
    )
    assert "not found" in missing["error"]["message"]


def test_mcp_stdio_handles_invalid_json_then_next_request_and_eof(tmp_path, monkeypatch, capsys):
    lines = iter(["{invalid", json.dumps({"jsonrpc": "2.0", "id": 9, "method": "tools/list"})])

    def read_line():
        try:
            return next(lines)
        except StopIteration:
            raise EOFError from None

    monkeypatch.setattr("builtins.input", read_line)
    asyncio.run(serve_stdio(cwd=str(tmp_path)))
    responses = [json.loads(line) for line in capsys.readouterr().out.splitlines()]
    assert len(responses) == 2
    assert "error" in responses[0]
    assert responses[1]["id"] == 9
    assert any(tool["name"] == "read_file" for tool in responses[1]["result"]["tools"])


@pytest.mark.integration
def test_mcp_client_registers_and_calls_stdio_tool(tmp_path, monkeypatch):
    monkeypatch.setenv("PAICLI_RUNTIME_API_KEY", "synthetic-mcp-parent-key")
    server = tmp_path / "fake_mcp_server.py"
    server.write_text(
        """
import os
from mcp.server.fastmcp import FastMCP

assert "PAICLI_RUNTIME_API_KEY" not in os.environ

mcp = FastMCP("fake")

@mcp.tool()
def echo(text: str) -> str:
    return "echo:" + text

if __name__ == "__main__":
    mcp.run(transport="stdio")
""".lstrip(),
        encoding="utf-8",
    )
    (tmp_path / ".paicli").mkdir()
    (tmp_path / ".paicli" / "mcp.json").write_text(
        json.dumps(
            {
                "mcpServers": {
                    "fake": {
                        "type": "stdio",
                        "command": sys.executable,
                        "args": [str(server)],
                    }
                }
            }
        ),
        encoding="utf-8",
    )

    assert McpClientManager(tmp_path).specs == {}
    monkeypatch.setenv("PAICLI_TRUST_PROJECT_MCP", "1")

    async def run():
        manager = McpClientManager(tmp_path)
        tools = await manager.load_tools()
        names = [tool.name for tool in tools]
        tool = next(item for item in tools if item.name == "mcp__fake__echo")
        config = load_config(project_root=tmp_path)
        config.policy.hitl_mode = "never"
        result = await tool.execute({"text": "ok"}, ToolContext(cwd=str(tmp_path), config=config))
        return names, result

    names, result = asyncio.run(run())
    assert "mcp__fake__echo" in names
    assert result.content == "echo:ok"


@pytest.mark.integration
def test_mcp_client_suppresses_stdio_server_stderr(tmp_path, capsys, monkeypatch):
    monkeypatch.setenv("PAICLI_TRUST_PROJECT_MCP", "1")
    server = tmp_path / "noisy_mcp_server.py"
    server.write_text(
        """
import sys
from mcp.server.fastmcp import FastMCP

sys.stderr.write("NOISY_MCP_STARTUP\\n")
sys.stderr.flush()

mcp = FastMCP("noisy")

@mcp.tool()
def echo(text: str) -> str:
    return text

if __name__ == "__main__":
    mcp.run(transport="stdio")
""".lstrip(),
        encoding="utf-8",
    )
    (tmp_path / ".paicli").mkdir()
    (tmp_path / ".paicli" / "mcp.json").write_text(
        json.dumps(
            {
                "mcpServers": {
                    "noisy": {
                        "type": "stdio",
                        "command": sys.executable,
                        "args": [str(server)],
                    }
                }
            }
        ),
        encoding="utf-8",
    )

    async def run():
        manager = McpClientManager(tmp_path)
        return await manager.load_tools()

    tools = asyncio.run(run())

    assert any(tool.name == "mcp__noisy__echo" for tool in tools)
    captured = capsys.readouterr()
    assert "NOISY_MCP_STARTUP" not in captured.err


@pytest.mark.parametrize(
    "bad_response",
    [
        {"code": 1000, "msg": "身份验证失败。", "success": False, "secret": "PRIVATE"},
        {"unexpected": "PRIVATE"},
        "invalid JSON PRIVATE",
    ],
)
def test_http_error_is_concise_and_other_servers_still_load(
    tmp_path,
    monkeypatch,
    caplog,
    bad_response,
):
    monkeypatch.setenv("PAICLI_TRUST_PROJECT_MCP", "1")
    (tmp_path / ".paicli").mkdir()
    (tmp_path / ".paicli" / "mcp.json").write_text(
        json.dumps(
            {
                "mcpServers": {
                    "bad": {"url": "https://mcp.test/bad", "timeout": 1},
                    "good": {"url": "https://mcp.test/good", "timeout": 1},
                },
            }
        )
    )

    def respond(request):
        if request.url.path == "/bad":
            if isinstance(bad_response, str):
                return httpx.Response(
                    200,
                    text=bad_response,
                    headers={"content-type": "application/json"},
                )
            return httpx.Response(200, json=bad_response)
        if request.method != "POST":
            return httpx.Response(405)
        message = json.loads(request.content)
        if "id" not in message:
            return httpx.Response(202, headers={"content-type": "application/json"})
        if message["method"] == "initialize":
            result = {
                "protocolVersion": message["params"]["protocolVersion"],
                "capabilities": {"tools": {}},
                "serverInfo": {"name": "good", "version": "1"},
            }
        else:
            assert message["method"] == "tools/list"
            result = {"tools": [{"name": "echo", "inputSchema": {"type": "object"}}]}
        return httpx.Response(200, json={"jsonrpc": "2.0", "id": message["id"], "result": result})

    original_client = httpx.AsyncClient

    def mock_client(**kwargs):
        return original_client(**kwargs, transport=httpx.MockTransport(respond))

    monkeypatch.setattr("paicli.mcp.client.httpx.AsyncClient", mock_client)

    async def run():
        manager = McpClientManager(tmp_path)
        tools = await manager.load_tools()
        return manager, tools

    manager, tools = asyncio.run(run())
    assert any(tool.name == "mcp__good__echo" for tool in tools)
    assert not any(tool.name.startswith("mcp__bad__") for tool in tools)
    assert set(manager.last_errors) == {"bad"}
    error = manager.last_errors["bad"]
    assert "check the MCP URL and credentials" in error
    if isinstance(bad_response, dict) and "code" in bad_response:
        assert "1000" in error
    assert "PRIVATE" not in error
    assert "PRIVATE" not in caplog.text
    assert "Error parsing JSON response" not in caplog.text
    assert _count_mcp_servers(manager) == 1
