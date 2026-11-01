from __future__ import annotations

import asyncio
from pathlib import Path

from paicli.agent import QueryEngine
from paicli.bootstrap import build_runtime
from paicli.config import load_config


def create_default_engine(cwd: str | None = None) -> QueryEngine:
    root = str(Path(cwd or ".").resolve())
    config = load_config(project_root=root)
    client, registry, _manager, session_store = asyncio.run(build_runtime(config=config, cwd=root))
    return QueryEngine(
        llm_client=client,
        tool_registry=registry,
        config=config,
        cwd=root,
        session_store=session_store,
    )


__all__ = ["QueryEngine", "create_default_engine"]
