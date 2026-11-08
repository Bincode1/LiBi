from __future__ import annotations

import asyncio
import json
import os
import socket
import subprocess
import sys

import httpx
import pytest
from mcp import ClientSession, StdioServerParameters
from mcp.client.stdio import stdio_client
from mcp.client.streamable_http import streamable_http_client

from paicli.config import load_config
from paicli.entrypoints.repl import _count_mcp_servers
from paicli.mcp import McpClientManager
from paicli.tools.base import ToolContext


async def _check_public_mcp_session(session, cwd):
    initialized = await session.initialize()
    assert initialized.serverInfo.name == "paicli"
    tools = await session.list_tools()
    names = {tool.name for tool in tools.tools}
    assert {"read_file", "glob", "grep", "bash"} <= names
    assert not {"glob_files", "grep_code", "execute_command"} & names
    assert (await session.call_tool("missing-tool", {})).isError
    result = await session.call_tool("read_file", {"path": "note.txt"})
    assert not result.isError and result.content[0].text == "1: hello"
    denied = await session.call_tool("write_file", {"path": "denied.txt", "content": "probe"})
    assert denied.isError and "approval policy" in denied.content[0].text
    assert not (cwd / "denied.txt").exists()


@pytest.mark.integration
def test_public_mcp_stdio_uses_sdk_and_tool_policy(tmp_path, isolated_home):
    (tmp_path / "note.txt").write_text("hello", encoding="utf-8")

    async def run():
        params = StdioServerParameters(
            command=sys.executable,
            args=["-m", "paicli", "mcp", "serve", "--cwd", str(tmp_path)],
            env={"HOME": str(isolated_home), "USERPROFILE": str(isolated_home)},
        )
        async with stdio_client(params) as streams, ClientSession(*streams) as session:
            await _check_public_mcp_session(session, tmp_path)

    asyncio.run(run())


@pytest.mark.integration
def test_public_mcp_http_requires_auth_and_valid_origin(tmp_path):
    (tmp_path / "note.txt").write_text("hello", encoding="utf-8")
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        port = sock.getsockname()[1]
    url = f"http://127.0.0.1:{port}/mcp"
    credential = "synthetic-mcp-key"
    headers = {"authorization": f"Bearer {credential}"}
    proc = subprocess.Popen(
        [
            sys.executable,
            "-m",
            "paicli",
            "mcp",
            "serve",
            "--transport",
            "http",
            "--port",
            str(port),
            "--cwd",
            str(tmp_path),
        ],
        env=dict(os.environ, PAICLI_MCP_API_KEY=credential),
    )

    async def run():
        async with httpx.AsyncClient() as client:
            async with asyncio.timeout(5):
                while True:
                    try:
                        response = await client.post(url, content=b"{}")
                        break
                    except httpx.ConnectError:
                        await asyncio.sleep(0.05)
            assert response.status_code == 401
            response = await client.post(
                url, json={}, headers={**headers, "origin": "https://untrusted.example"}
            )
            assert response.status_code == 403
        async with (
            httpx.AsyncClient(headers=headers) as client,
            streamable_http_client(url, http_client=client) as (read, write, _),
            ClientSession(read, write) as session,
        ):
            await _check_public_mcp_session(session, tmp_path)

    try:
        asyncio.run(run())
    finally:
        proc.terminate()
        proc.wait(timeout=5)


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
