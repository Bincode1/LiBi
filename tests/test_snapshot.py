from __future__ import annotations

import asyncio
import json
import os
import shutil
import subprocess
from pathlib import Path

import pytest

from paicli.agent import Agent
from paicli.agent.subagents import make_subagent_tool
from paicli.snapshot import SnapshotService
from paicli.tools import ToolRegistry, get_builtin_tools
from paicli.tools.base import ToolContext, ToolResult
from paicli.tools.executor import ToolExecutor


def test_snapshot_restore(tmp_path):
    project = tmp_path / "project"
    project.mkdir()
    file_path = project / "note.txt"
    file_path.write_text("before", encoding="utf-8")

    service = SnapshotService(project)
    first = service.create("pre-turn")
    file_path.write_text("after", encoding="utf-8")

    restored = service.restore(first.id)

    assert restored.id == first.id
    assert file_path.read_text(encoding="utf-8") == "before"


def test_snapshot_restore_removes_new_files_and_preserves_ignored_directories(tmp_path):
    project = tmp_path / "project"
    project.mkdir()
    (project / "note.txt").write_text("before", encoding="utf-8")
    (project / ".git").mkdir()
    (project / ".git/config").write_text("original", encoding="utf-8")
    service = SnapshotService(project)
    first = service.create("pre-turn")
    assert not (first.path / ".git").exists()
    (project / "note.txt").unlink()
    (project / "new.txt").write_text("new", encoding="utf-8")
    (project / ".git/config").write_text("current", encoding="utf-8")
    restored = service.restore("1")
    assert restored.id == first.id
    assert (project / "note.txt").read_text() == "before"
    assert not (project / "new.txt").exists()
    assert (project / ".git/config").read_text() == "current"
    assert service.list()[0].phase == "pre-restore"


@pytest.mark.parametrize("reference", ["missing", "0", "99"])
def test_snapshot_missing_reference_does_not_change_workspace(tmp_path, reference):
    (tmp_path / "note.txt").write_text("untouched", encoding="utf-8")
    service = SnapshotService(tmp_path)
    with pytest.raises(ValueError, match="snapshot not found"):
        service.restore(reference)
    assert (tmp_path / "note.txt").read_text() == "untouched"
    assert service.list() == []


def test_snapshot_clean_is_idempotent_and_project_scoped(tmp_path):
    first, second = tmp_path / "first", tmp_path / "second"
    first.mkdir()
    second.mkdir()
    a, b = SnapshotService(first), SnapshotService(second)
    a.create("a")
    b.create("b")
    assert a.clean() == 1
    assert a.clean() == 0
    assert a.list() == []
    assert len(b.list()) == 1


@pytest.mark.parametrize("workspace_is_home", [True, False])
def test_snapshot_in_home(tmp_path_factory, monkeypatch, workspace_is_home):
    parent = tmp_path_factory.mktemp("s")
    home = parent / "h"
    home.mkdir()
    monkeypatch.setattr(Path, "home", lambda: home)
    workspace = home if workspace_is_home else parent
    storage = home / ".paicli" / "snapshots"
    foreign = storage / "other" / "keep.txt"
    foreign.parent.mkdir(parents=True)
    foreign.write_text("keep", encoding="utf-8")
    note = workspace / "note.txt"
    note.write_text("original", encoding="utf-8")
    settings = home / ".paicli" / "s.txt"
    settings.write_text("settings", encoding="utf-8")
    service = SnapshotService(workspace)
    copytree = shutil.copytree

    def guard_copytree(source, destination, *args, **kwargs):
        assert not Path(source).is_relative_to(storage), "copied the live snapshot store"
        return copytree(source, destination, *args, **kwargs)

    with monkeypatch.context() as guard:
        guard.setattr(shutil, "copytree", guard_copytree)
        original = service.create("original")

    assert not (original.path / storage.relative_to(workspace)).exists()
    assert (original.path / settings.relative_to(workspace)).exists()
    note.write_text("current", encoding="utf-8")
    service.restore(original.id)
    assert note.read_text(encoding="utf-8") == "original"
    assert foreign.read_text(encoding="utf-8") == "keep"
    assert [record.phase for record in service.list()] == ["pre-restore", "original"]


@pytest.mark.parametrize("invalid_source", ["missing", "outside", "file"])
def test_restore_rejects_invalid_source(tmp_path, invalid_source):
    project = tmp_path / "project"
    project.mkdir()
    note = project / "note.txt"
    note.write_text("original", encoding="utf-8")
    service = SnapshotService(project)
    service.create("original")
    entry = json.loads(service.index_path.read_text(encoding="utf-8"))
    source = service.root / "missing"
    if invalid_source == "outside":
        source = tmp_path / "outside"
        source.mkdir()
    elif invalid_source == "file":
        source = service.root / "not-a-directory"
        source.write_text("invalid", encoding="utf-8")
    entry["path"] = str(source)
    service.index_path.write_text(json.dumps(entry) + "\n", encoding="utf-8")
    note.write_text("current", encoding="utf-8")

    with pytest.raises(ValueError, match="snapshot source"):
        service.restore("1")

    assert note.read_text(encoding="utf-8") == "current"
    assert len(service.list()) == 1


def test_snapshot_restore_preserves_nested_ignored_directories(tmp_path):
    frontend = tmp_path / "frontend"
    dependency = frontend / "node_modules" / "package.js"
    dependency.parent.mkdir(parents=True)
    dependency.write_text("original dependency", encoding="utf-8")
    app = frontend / "app.py"
    app.write_text("original app", encoding="utf-8")
    service = SnapshotService(tmp_path)
    original = service.create("original")
    dependency.write_text("current dependency", encoding="utf-8")
    app.write_text("current app", encoding="utf-8")
    untracked = tmp_path / "untracked"
    nested_git = untracked / "nested" / ".git" / "config"
    nested_git.parent.mkdir(parents=True)
    nested_git.write_text("nested git", encoding="utf-8")
    ordinary = untracked / "discard.txt"
    ordinary.write_text("remove", encoding="utf-8")

    service.restore(original.id)

    assert app.read_text(encoding="utf-8") == "original app"
    assert dependency.read_text(encoding="utf-8") == "current dependency"
    assert nested_git.read_text(encoding="utf-8") == "nested git"
    assert not ordinary.exists()


def test_snapshot_restore_rejects_conflict_with_protected_descendant(tmp_path):
    path = tmp_path / "replace-me"
    path.write_text("original file", encoding="utf-8")
    note = tmp_path / "note.txt"
    note.write_text("original note", encoding="utf-8")
    service = SnapshotService(tmp_path)
    original = service.create("original")
    path.unlink()
    dependency = path / "node_modules" / "package.js"
    dependency.parent.mkdir(parents=True)
    dependency.write_text("current dependency", encoding="utf-8")
    note.write_text("current note", encoding="utf-8")

    with pytest.raises(ValueError, match="protected"):
        service.restore(original.id)

    assert note.read_text(encoding="utf-8") == "current note"
    assert dependency.read_text(encoding="utf-8") == "current dependency"
    assert len(service.list()) == 1


def test_restore_protects_links(tmp_path):
    project = tmp_path / "project"
    project.mkdir()
    link = project / "linked"
    link.mkdir()
    (link / "note.txt").write_text("original", encoding="utf-8")
    service = SnapshotService(project)
    original = service.create("original")
    outside = tmp_path / "outside"
    link.rename(outside)
    (outside / "note.txt").write_text("outside", encoding="utf-8")
    if os.name == "nt":
        result = subprocess.run(
            ["cmd", "/c", "mklink", "/J", str(link), str(outside)],
            check=False,
            capture_output=True,
        )
        if result.returncode:
            pytest.skip("directory junctions are unavailable")
    else:
        link.symlink_to(outside, target_is_directory=True)

    with pytest.raises(ValueError, match="protected link"):
        service.restore(original.id)

    newer = service.create("current")
    assert not (newer.path / link.name).exists()
    service.restore(newer.id)
    assert (link / "note.txt").read_text(encoding="utf-8") == "outside"
    assert (outside / "note.txt").read_text(encoding="utf-8") == "outside"


@pytest.mark.parametrize("action", ["hello", "read", "dry-run", "deny", "write"])
def test_agent_snapshots_only_before_admitted_mutation(tmp_path, session_store, config, action):
    project = tmp_path / "project"
    project.mkdir()
    path = project / "note.txt"
    path.write_text("before", encoding="utf-8")
    snapshots = SnapshotService(project)
    approvals = []

    class Client:
        provider_name = "test"
        model_name = "test"
        max_context_window = 128_000

        async def chat(self, messages, tools, *, system_prompt):
            if action == "hello" or any(message.role == "tool" for message in messages):
                yield {"type": "text_delta", "text": "done"}
                yield {"type": "message_end", "stop_reason": "end_turn"}
                return
            name, arguments = {
                "read": ("read_file", {}),
                "dry-run": (
                    "edit_file",
                    {"old_text": "before", "new_text": "after", "dry_run": True},
                ),
                "deny": ("write_file", {"content": "after"}),
                "write": ("write_file", {"content": "after"}),
            }[action]
            yield _tool_call(name, {"path": "note.txt", **arguments})
            yield {"type": "message_end", "stop_reason": "tool_use"}

    async def approve(details):
        approvals.append(details["tool_name"])
        assert snapshots.list() == []
        assert path.read_text(encoding="utf-8") == "before"
        return "deny" if action == "deny" else "approve"

    registry = ToolRegistry()
    registry.register_all(get_builtin_tools())
    agent = Agent(
        llm_client=Client(),
        tool_registry=registry,
        config=config,
        cwd=str(project),
        session_store=session_store,
        approval_callback=approve,
    )
    result = asyncio.run(agent.run_complete("exercise snapshots"))
    assert result.text == "done"
    assert len(approvals) == int(action in {"dry-run", "deny", "write"})
    records = snapshots.list()
    if action == "write":
        assert path.read_text(encoding="utf-8") == "after"
        assert [record.phase for record in records] == ["post-turn", "pre-turn"]
        assert (records[1].path / "note.txt").read_text(encoding="utf-8") == "before"
    else:
        assert path.read_text(encoding="utf-8") == "before"
        assert records == []


def test_plan_workers_share_one_workspace_checkpoint(tmp_path, session_store, config):
    project = tmp_path / "project"
    project.mkdir()
    for name in ("left.txt", "right.txt"):
        (project / name).write_text("before", encoding="utf-8")

    class Client:
        provider_name = "test"
        model_name = "test"
        max_context_window = 128_000

        async def chat(self, messages, tools, *, system_prompt):
            if not tools:
                yield {
                    "type": "text_delta",
                    "text": json.dumps(
                        {
                            "tasks": [
                                {"id": "left", "description": "write left", "type": "FILE_WRITE"},
                                {"id": "right", "description": "write right", "type": "FILE_WRITE"},
                            ]
                        }
                    ),
                }
            elif not any(message.role == "tool" for message in messages):
                name = "left.txt" if "write left" in messages[-1].content else "right.txt"
                yield _tool_call("write_file", {"path": name, "content": "after"})
                yield {"type": "message_end", "stop_reason": "tool_use"}
                return
            else:
                yield {"type": "text_delta", "text": "done"}
            yield {"type": "message_end", "stop_reason": "end_turn"}

    config.policy.hitl_mode = "never"
    registry = ToolRegistry()
    registry.register_all(get_builtin_tools())
    agent = Agent(
        llm_client=Client(),
        tool_registry=registry,
        config=config,
        cwd=str(project),
        session_store=session_store,
        mode="plan",
    )
    asyncio.run(agent.run_complete("write both files"))

    records = SnapshotService(project).list()
    assert [record.phase for record in records] == ["post-turn", "pre-turn"]
    for name in ("left.txt", "right.txt"):
        assert (records[1].path / name).read_text(encoding="utf-8") == "before"
        assert (records[0].path / name).read_text(encoding="utf-8") == "after"


def test_revert_tool_resolves_number_before_recording_undo(tmp_path, config):
    note = tmp_path / "note.txt"
    note.write_text("original", encoding="utf-8")
    snapshots = SnapshotService(tmp_path)
    original = snapshots.create("initial")
    note.write_text("current", encoding="utf-8")
    registry = ToolRegistry()
    registry.register_all(get_builtin_tools())
    context = ToolContext(
        cwd=str(tmp_path),
        config=config,
        approval_callback=lambda _request: "approve",
        before_workspace_mutation=snapshots.checkpoint_turn,
    )
    call = _tool_call("revert_turn", {"snapshot": "1"})["tool_call"]

    result = asyncio.run(ToolExecutor(registry).execute_all([call], context))[0]

    assert not result.is_error
    assert original.id in result.content
    assert note.read_text(encoding="utf-8") == "original"
    assert [record.phase for record in snapshots.list()] == ["pre-restore", "initial"]
    snapshots.restore("1")
    assert note.read_text(encoding="utf-8") == "current"


def test_snapshot_failure_stops_approved_write(tmp_path, config, monkeypatch):
    note = tmp_path / "note.txt"
    note.write_text("original", encoding="utf-8")
    snapshots = SnapshotService(tmp_path)
    registry = ToolRegistry()
    registry.register_all(get_builtin_tools())
    context = ToolContext(
        cwd=str(tmp_path),
        config=config,
        approval_callback=lambda _request: "approve",
        before_workspace_mutation=snapshots.checkpoint_turn,
    )

    def fail_copy(_self, _source, _target):
        raise OSError("snapshot unavailable")

    monkeypatch.setattr(SnapshotService, "_copy_tree", fail_copy)
    call = _tool_call("write_file", {"path": "note.txt", "content": "changed"})["tool_call"]

    result = asyncio.run(ToolExecutor(registry).execute_all([call], context))[0]

    assert result.is_error and "snapshot unavailable" in result.content
    assert note.read_text(encoding="utf-8") == "original"
    assert snapshots.list() == []


def test_parent_snapshot_covers_delegated_mutation(tmp_path, config):
    nested = tmp_path / "child"
    nested.mkdir()
    note = nested / "note.txt"
    note.write_text("original", encoding="utf-8")
    snapshots = SnapshotService(tmp_path)

    class SubagentManager:
        async def start(self, payload, _context):
            (Path(payload["cwd"]) / "note.txt").write_text("changed", encoding="utf-8")
            return ToolResult("child started")

    registry = ToolRegistry()
    registry.register(make_subagent_tool())
    context = ToolContext(
        cwd=str(tmp_path),
        config=config,
        subagent_manager=SubagentManager(),
        before_workspace_mutation=snapshots.checkpoint_turn,
    )
    call = _tool_call(
        "subagent", {"cwd": str(nested), "description": "edit note", "prompt": "edit note"}
    )["tool_call"]

    result = asyncio.run(ToolExecutor(registry).execute_all([call], context))[0]

    assert not result.is_error
    assert note.read_text(encoding="utf-8") == "changed"
    checkpoint = snapshots.list()[0]
    assert (checkpoint.path / "child" / "note.txt").read_text(encoding="utf-8") == "original"


def _tool_call(name, arguments):
    return {
        "type": "tool_call_delta",
        "tool_call": {
            "index": 0,
            "id": "tool-call",
            "function": {"name": name, "arguments": json.dumps(arguments)},
        },
    }
