from __future__ import annotations

import pytest

from paicli.policy.command_guard import CommandGuard, CommandPolicyError
from paicli.policy.path_guard import PathGuard, PathPolicyError


def test_path_guard_rejects_escape(tmp_path):
    guard = PathGuard(tmp_path)
    assert guard.validate("inside.txt") == tmp_path / "inside.txt"
    with pytest.raises(PathPolicyError):
        guard.validate("../outside.txt")


@pytest.mark.parametrize("command", ["rm -rf /", "shutdown", "reboot", "find / "])
def test_command_guard_rejects_destructive_command(command):
    with pytest.raises(CommandPolicyError):
        CommandGuard().validate(command)


def test_path_guard_accepts_normalized_internal_path_and_absolute_workspace(tmp_path):
    guard = PathGuard(tmp_path)
    assert guard.validate("sub/../inside.txt") == tmp_path / "inside.txt"
    assert guard.validate(tmp_path / "inside.txt") == tmp_path / "inside.txt"
    with pytest.raises(PathPolicyError):
        guard.validate(tmp_path.parent / "outside.txt")


def test_command_guard_custom_blacklist_and_safe_command():
    guard = CommandGuard(["forbidden-command"])
    guard.validate("git status --short")
    with pytest.raises(CommandPolicyError):
        guard.validate("  forbidden-command   argument ")
