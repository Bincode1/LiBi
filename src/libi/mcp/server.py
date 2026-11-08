from __future__ import annotations

import os
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from hmac import compare_digest
from pathlib import Path
from typing import Any

import uvicorn
from mcp import types
from mcp.server.lowlevel import Server
from mcp.server.stdio import stdio_server
from mcp.server.streamable_http_manager import StreamableHTTPSessionManager
from mcp.server.transport_security import TransportSecuritySettings
from starlette.applications import Starlette
from starlette.requests import HTTPConnection
from starlette.responses import Response
from starlette.routing import Route

from libi.config import load_config
from libi.policy.redaction import Redactor
from libi.tools import ToolRegistry, get_builtin_tools
from libi.tools.base import ToolContext
from libi.tools.executor import ToolExecutor


def _build_registry() -> ToolRegistry:
    registry = ToolRegistry()
    registry.register_all(get_builtin_tools(session_tools=False))
    return registry


def _build_server(cwd: str, *, transport_key: str = "") -> Server:
    server = Server("libi")
    registry = _build_registry()
    config = load_config(project_root=cwd)
    # An MCP client cannot present a LiBi approval prompt.
    config.policy.hitl_mode = "auto"
    config.policy.path_guard_enabled = True
    config.policy.command_guard_enabled = True
    config.features.audit_log = True
    secrets = (config.llm.api_key, transport_key)
    executor = ToolExecutor(
        registry, audit_log_path=config.policy.audit_log_path, sensitive_values=secrets
    )
    context = ToolContext(cwd=cwd, config=config)
    redactor = Redactor(secrets)

    @server.list_tools()
    async def list_tools() -> list[types.Tool]:
        return [
            types.Tool(name=tool.name, description=tool.description, inputSchema=tool.parameters)
            for name in registry.list_names()
            if (tool := registry.get(name)) is not None
        ]

    @server.call_tool()
    async def call_tool(name: str, arguments: dict[str, Any]) -> types.CallToolResult:
        (result,) = await executor.execute_all([{"name": name, "arguments": arguments}], context)
        return types.CallToolResult(
            content=[types.TextContent(type="text", text=redactor.clean(result.content))],
            isError=result.is_error,
        )

    return server


async def serve_stdio(cwd: str | None = None) -> None:
    server = _build_server(str(Path(cwd or ".").resolve()))
    async with stdio_server() as (read, write):
        await server.run(read, write, server.create_initialization_options())


def _http_app(cwd: str, port: int, api_key: str) -> Starlette:
    hosts = [f"127.0.0.1:{port}", f"localhost:{port}"]
    sessions = StreamableHTTPSessionManager(
        app=_build_server(cwd, transport_key=api_key),
        stateless=True,
        json_response=True,
        security_settings=TransportSecuritySettings(
            allowed_hosts=hosts,
            allowed_origins=[f"http://{host}" for host in hosts],
        ),
    )
    bearer = f"Bearer {api_key}".encode()

    class Endpoint:
        async def __call__(self, scope, receive, send):
            headers = HTTPConnection(scope).headers
            token = headers.get("authorization", "").encode()
            if not compare_digest(token, bearer):
                await Response("Unauthorized", status_code=401)(scope, receive, send)
                return
            await sessions.handle_request(scope, receive, send)

    @asynccontextmanager
    async def lifespan(_app: Starlette) -> AsyncIterator[None]:
        async with sessions.run():
            yield

    return Starlette(
        routes=[Route(path, Endpoint()) for path in ("/mcp", "/")],
        lifespan=lifespan,
    )


def serve_http(port: int = 3000, cwd: str | None = None) -> None:
    key = os.environ.get("LIBI_MCP_API_KEY")
    if not key:
        raise ValueError("LIBI_MCP_API_KEY is required for MCP HTTP")
    app = _http_app(str(Path(cwd or ".").resolve()), port, key)
    uvicorn.run(app, host="127.0.0.1", port=port, proxy_headers=False)
