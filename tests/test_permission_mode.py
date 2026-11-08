from __future__ import annotations

import asyncio

from libi.config import load_config
from libi.entrypoints.repl import PermissionModeController
from libi.tools.base import Tool, ToolContext, ToolResult, object_schema
from libi.tools.executor import ToolExecutor
from libi.tools.registry import ToolRegistry


def test_auto_mode_runs_approval_required_tool_without_callback(tmp_path):
    config = load_config(project_root=tmp_path)
    controller = PermissionModeController(config)
    executions: list[str] = []

    async def mutate(payload, _context):
        executions.append(str(payload["value"]))
        return ToolResult("done")

    registry = ToolRegistry()
    registry.register(
        Tool(
            name="mutate",
            description="Mutate test state",
            parameters=object_schema({"value": {"type": "string"}}, ["value"]),
            required_keys=["value"],
            handler=mutate,
            is_read_only=False,
            requires_approval=True,
        )
    )
    executor = ToolExecutor(registry)
    call = {"id": "call-1", "name": "mutate", "arguments": {"value": "ok"}}
    context = ToolContext(cwd=str(tmp_path), config=config)

    denied = asyncio.run(executor.execute_all([call], context))[0]
    assert denied.is_error
    assert executions == []

    controller.set("auto")
    approved = asyncio.run(executor.execute_all([call], context))[0]
    assert not approved.is_error
    assert executions == ["ok"]


def test_default_mode_restores_the_original_policy(tmp_path):
    config = load_config(project_root=tmp_path)
    config.policy.hitl_mode = "always"
    config.policy.path_guard_enabled = False
    config.policy.command_guard_enabled = True
    controller = PermissionModeController(config)

    assert controller.toggle() == "auto"
    assert config.policy.hitl_mode == "never"
    assert not config.policy.path_guard_enabled
    assert not config.policy.command_guard_enabled

    assert controller.toggle() == "default"
    assert config.policy.hitl_mode == "always"
    assert not config.policy.path_guard_enabled
    assert config.policy.command_guard_enabled
