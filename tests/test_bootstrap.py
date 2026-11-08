from __future__ import annotations

import asyncio

import pytest

from libi.bootstrap import build_runtime
from libi.tools.base import Tool, ToolResult, object_schema

pytestmark = pytest.mark.integration


@pytest.mark.parametrize("enabled", [False, True])
def test_shared_bootstrap_registers_builtins_and_optional_mcp(
    tmp_path, config, monkeypatch, enabled, isolated_home
):
    calls = []

    async def echo(_payload, _context):
        return ToolResult("echo")

    class Manager:
        def __init__(self, cwd):
            calls.append(cwd)

        async def load_tools(self):
            return [Tool("mcp__test__echo", "Echo", object_schema({}), echo)]

    monkeypatch.setattr("libi.bootstrap.McpClientManager", Manager)
    config.features.mcp = enabled
    client, registry, manager, store = asyncio.run(build_runtime(config=config, cwd=str(tmp_path)))
    assert registry.get("read_file") is not None
    assert (registry.get("mcp__test__echo") is not None) is enabled
    assert (manager is not None) is enabled
    assert len(calls) == int(enabled)
    assert client.model_name == config.llm.model
    assert store.db_path.is_relative_to(isolated_home)


def test_mcp_auto_start_disabled(tmp_path, config, monkeypatch):
    def unexpected_manager(_cwd):
        pytest.fail("Disabled MCP auto-start must not discover servers")

    monkeypatch.setattr("libi.bootstrap.McpClientManager", unexpected_manager)
    config.features.mcp = True
    config.mcp.auto_start = False
    _, registry, manager, _ = asyncio.run(build_runtime(config=config, cwd=str(tmp_path)))
    assert manager is None
    assert registry.get("read_file") is not None
