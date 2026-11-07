from __future__ import annotations

from paicli.config import PaiCliConfig
from paicli.llm import create_llm_client
from paicli.llm.base import LlmClient
from paicli.mcp import McpClientManager
from paicli.session import SessionStore, default_runtime_db_path
from paicli.tools import ToolRegistry, get_builtin_tools


async def build_tool_registry(
    *,
    config: PaiCliConfig,
    cwd: str,
) -> tuple[ToolRegistry, McpClientManager | None]:
    registry = ToolRegistry()
    registry.register_all(get_builtin_tools())
    manager: McpClientManager | None = None
    if config.features.mcp and config.mcp.auto_start:
        manager = McpClientManager(cwd)
        registry.register_all(await manager.load_tools())
    return registry, manager


async def build_runtime(
    *,
    config: PaiCliConfig,
    cwd: str,
) -> tuple[LlmClient, ToolRegistry, McpClientManager | None, SessionStore]:
    """Assemble the shared runtime pieces used by every entry point.

    Returns the LLM client, tool registry (with MCP tools loaded), the MCP
    manager (if enabled), and a SessionStore pointing at the canonical runtime
    database. Callers that need an API-key redactor pass it via the store.
    """
    registry, manager = await build_tool_registry(config=config, cwd=cwd)
    client = create_llm_client(config.llm)
    session_store = SessionStore(
        default_runtime_db_path(),
        workspace=cwd,
        sensitive_values=(config.llm.api_key,),
    )
    return client, registry, manager, session_store
