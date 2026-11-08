from __future__ import annotations

from copy import deepcopy
from dataclasses import replace

from libi.tools.base import Tool


class ToolRegistry:
    def __init__(self):
        self._tools: dict[str, Tool] = {}

    def register(self, tool: Tool) -> None:
        self._tools[tool.name] = tool

    def register_all(self, tools: list[Tool]) -> None:
        for tool in tools:
            self.register(tool)

    def get(self, name: str) -> Tool | None:
        return self._tools.get(name)

    def list_names(self) -> list[str]:
        return sorted(self._tools)

    def definitions(self) -> list[dict]:
        return [self._tools[name].definition() for name in self.list_names()]

    def clone(self, *, allowed_names: set[str] | None = None) -> ToolRegistry:
        cloned = ToolRegistry()
        cloned._tools.update(
            {
                name: replace(
                    tool,
                    parameters=deepcopy(tool.parameters),
                    required_keys=list(tool.required_keys),
                )
                for name, tool in self._tools.items()
                if allowed_names is None or name in allowed_names
            }
        )
        return cloned
