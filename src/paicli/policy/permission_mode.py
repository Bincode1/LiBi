from __future__ import annotations

from dataclasses import dataclass
from typing import Literal

from paicli.config import PaiCliConfig

PermissionMode = Literal["default", "auto"]


@dataclass
class PermissionModeController:
    """Apply a permission mode to a config and remember its default policy."""

    config: PaiCliConfig
    mode: PermissionMode = "default"

    def __post_init__(self) -> None:
        self._defaults = (
            self.config.policy.hitl_mode,
            self.config.policy.path_guard_enabled,
            self.config.policy.command_guard_enabled,
        )
        self.set(self.mode)

    def set(self, mode: PermissionMode) -> PermissionMode:
        if mode not in ("default", "auto"):
            raise ValueError("permission mode must be default or auto")
        policy = self.config.policy
        policy.hitl_mode, policy.path_guard_enabled, policy.command_guard_enabled = (
            ("never", False, False) if mode == "auto" else self._defaults
        )
        self.mode = mode
        return self.mode

    def toggle(self) -> PermissionMode:
        return self.set("auto" if self.mode == "default" else "default")
