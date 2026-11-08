from __future__ import annotations

import asyncio
import json
import os
import shutil
import stat
import subprocess
from contextlib import contextmanager
from pathlib import Path

import pytest

from libi.agent import Agent
from libi.agent.subagents import make_subagent_tool
from libi.snapshot import SnapshotService
from libi.snapshot._filetree import Directory
from libi.tools import ToolRegistry, get_builtin_tools
from libi.tools.base import ToolContext, ToolResult
from libi.tools.executor import ToolExecutor


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
    dotenv = project / ".env"
    dotenv.write_text("KEY=original", encoding="utf-8")
    service = SnapshotService(project)
    first = service.create("pre-turn")
    assert not (first.path / ".git").exists()
    assert not (first.path / ".env").exists()
    (first.path / ".env").write_text("KEY=stale", encoding="utf-8")
    dotenv.write_text("KEY=current", encoding="utf-8")
    local_dotenv = project / ".env.local"
    local_dotenv.write_text("KEY=local", encoding="utf-8")
    (project / "note.txt").unlink()
    (project / "new.txt").write_text("new", encoding="utf-8")
    (project / ".git/config").write_text("current", encoding="utf-8")
    restored = service.restore("1")
    assert restored.id == first.id
    assert (project / "note.txt").read_text() == "before"
    assert not (project / "new.txt").exists()
    assert (project / ".git/config").read_text() == "current"
    assert dotenv.read_text(encoding="utf-8") == "KEY=current"
    assert local_dotenv.read_text(encoding="utf-8") == "KEY=local"
    assert service.list()[0].phase == "pre-restore"


@pytest.mark.parametrize("layout", ["standard", "linked", "separate", "parent", "case-alias"])
def test_restore_preserves_real_git_metadata(tmp_path, layout):
    git = shutil.which("git")
    if git is None:
        pytest.skip("Git is unavailable")
    project = tmp_path / "project"
    project.mkdir()

    def run(*args):
        return subprocess.check_output(
            [
                git,
                "-C",
                str(project),
                "-c",
                "core.autocrlf=false",
                "-c",
                "commit.gpgsign=false",
                "-c",
                "user.name=Snapshot Test",
                "-c",
                "user.email=snapshot@example.invalid",
                *args,
            ],
            stderr=subprocess.PIPE,
        )

    workspace = project / "subdir" if layout == "parent" else project
    workspace.mkdir(exist_ok=True)
    if layout == "case-alias":
        (workspace / "Repo-State").mkdir()
        if not (workspace / "repo-state").is_dir():
            pytest.skip("requires a case-insensitive filesystem")
    options = (
        ["--separate-git-dir", str(workspace / "repo-state")]
        if layout in {"separate", "parent", "case-alias"}
        else []
    )
    run("init", "-q", *options)
    project = workspace
    (project / "note.txt").write_bytes(b"first\n")
    run("add", "note.txt")
    run("commit", "-qm", "first")
    if layout == "linked":
        linked = tmp_path / "linked"
        run("worktree", "add", "--detach", str(linked))
        project = linked

    note = project / "note.txt"
    service = SnapshotService(project)
    saved = service.create("original")
    note.write_bytes(b"second\n")
    run("add", "note.txt")
    run("commit", "-qm", "second")
    head = run("rev-parse", "HEAD")
    note.write_bytes(b"staged\n")
    run("add", "note.txt")
    (project / "extra.txt").write_text("extra", encoding="utf-8")

    service.restore(saved.id)

    assert note.read_bytes() == b"first\n"
    assert not (project / "extra.txt").exists()
    assert run("rev-parse", "HEAD") == head
    assert run("show", ":./note.txt") == b"staged\n"
    assert not (saved.path / "repo-state").exists()
    assert not (saved.path / "Repo-State").exists()


@pytest.mark.skipif(os.name == "nt", reason="POSIX directory permissions")
def test_snapshot_preserves_private_directory_mode(tmp_path):
    private = tmp_path / "private"
    private.mkdir(mode=0o700)
    (private / "note.txt").write_text("private", encoding="utf-8")
    service = SnapshotService(tmp_path)
    saved = service.create("original")
    assert stat.S_IMODE((saved.path / "private").stat().st_mode) == 0o700
    shutil.rmtree(private)
    service.restore(saved.id)
    assert stat.S_IMODE(private.stat().st_mode) == 0o700


def test_restore_handles_case_only_rename(tmp_path):
    note = tmp_path / "Note.txt"
    note.write_text("snapshot", encoding="utf-8")
    service = SnapshotService(tmp_path)
    saved = service.create("original")
    middle = tmp_path / "temporary.txt"
    note.rename(middle)
    middle.rename(tmp_path / "note.txt")

    service.restore(saved.id)

    assert [entry.name for entry in tmp_path.iterdir()] == ["Note.txt"]
    assert note.read_text(encoding="utf-8") == "snapshot"


def test_restore_reconciles_file_directory_conflicts(tmp_path):
    old_dir = tmp_path / "directory"
    old_dir.mkdir()
    (old_dir / "note").write_bytes(b"nested")
    old_file = tmp_path / "file"
    old_file.write_bytes(b"file")
    service = SnapshotService(tmp_path)
    saved = service.create("original")
    shutil.rmtree(old_dir)
    old_dir.write_bytes(b"temporary")
    old_file.unlink()
    old_file.mkdir()
    (old_file / "extra").write_bytes(b"temporary")

    service.restore(saved.id)

    assert (old_dir / "note").read_bytes() == b"nested"
    assert old_file.read_bytes() == b"file"


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


def test_create_holds_lock_until_the_snapshot_is_published(tmp_path, monkeypatch):
    (tmp_path / "note").write_bytes(b"before")
    service = SnapshotService(tmp_path)
    concurrent = SnapshotService(tmp_path)
    copy_tree = service._copy_tree

    def copy_and_try_clean(*args):
        copy_tree(*args)
        with pytest.raises(OSError, match="another snapshot operation"):
            concurrent.clean()

    monkeypatch.setattr(service, "_copy_tree", copy_and_try_clean)
    record = service.create("original")

    assert concurrent.list() == [record]
    assert (record.path / "note").read_bytes() == b"before"


@pytest.mark.parametrize("workspace_is_home", [True, False])
def test_snapshot_in_home(tmp_path_factory, monkeypatch, workspace_is_home):
    parent = tmp_path_factory.mktemp("s")
    home = parent / "h"
    home.mkdir()
    monkeypatch.setattr(Path, "home", lambda: home)
    workspace = home if workspace_is_home else parent
    storage = home / ".libi" / "snapshots"
    foreign = storage / "other" / "keep.txt"
    foreign.parent.mkdir(parents=True)
    foreign.write_text("keep", encoding="utf-8")
    note = workspace / "note.txt"
    note.write_text("original", encoding="utf-8")
    settings = home / ".libi" / "s.txt"
    settings.write_text("settings", encoding="utf-8")
    service = SnapshotService(workspace)
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
    source = service.create("original").path
    displaced = tmp_path / "outside"
    source.rename(displaced)
    if invalid_source == "outside":
        _link_directory(source, displaced)
    elif invalid_source == "file":
        source.write_text("invalid", encoding="utf-8")
    note.write_text("current", encoding="utf-8")

    try:
        with pytest.raises(ValueError, match="snapshot source"):
            service.restore("1")
    finally:
        if invalid_source == "outside":
            source.rmdir() if os.name == "nt" else source.unlink()

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


def _link_directory(link, target):
    if os.name == "nt":
        result = subprocess.run(
            ["cmd", "/c", "mklink", "/J", str(link), str(target)],
            check=False,
            capture_output=True,
        )
        if result.returncode:
            pytest.skip("directory junctions are unavailable")
    else:
        link.symlink_to(target, target_is_directory=True)


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
    _link_directory(link, outside)

    with pytest.raises(ValueError, match="protected link"):
        service.restore(original.id)

    newer = service.create("current")
    assert not (newer.path / link.name).exists()
    service.restore(newer.id)
    assert (link / "note.txt").read_text(encoding="utf-8") == "outside"
    assert (outside / "note.txt").read_text(encoding="utf-8") == "outside"


@pytest.mark.parametrize("swap_after", ["checkpoint", "copy-check"])
def test_restore_never_writes_through_a_concurrent_link_swap(tmp_path, monkeypatch, swap_after):
    project = tmp_path / "project"
    nested = project / "nested"
    nested.mkdir(parents=True)
    (nested / "note.txt").write_text("snapshot", encoding="utf-8")
    outside = tmp_path / "outside"
    outside.mkdir()
    victim = outside / "note.txt"
    victim.write_text("outside", encoding="utf-8")
    service = SnapshotService(project)
    saved = service.create("original")
    original_create = service._create
    original_directory = Directory.directory
    armed = False
    swapped = False

    def swap():
        nonlocal swapped
        nested.rename(tmp_path / "moved")
        _link_directory(nested, outside)
        swapped = True

    def create(storage, source, phase, protected):
        nonlocal armed
        checkpoint = original_create(storage, source, phase, protected)
        if phase == "pre-restore":
            if swap_after == "checkpoint":
                swap()
            else:
                armed = True
        return checkpoint

    def directory(self, name, **options):
        nonlocal armed
        if armed and name == nested.name:
            armed = False
            swap()
        return original_directory(self, name, **options)

    monkeypatch.setattr(service, "_create", create)
    monkeypatch.setattr(Directory, "directory", directory)
    try:
        with pytest.raises((OSError, ValueError)):
            service.restore(saved.id)
        assert swapped
        assert victim.read_text(encoding="utf-8") == "outside"
        assert (tmp_path / "moved" / "note.txt").read_text(encoding="utf-8") == "snapshot"
    finally:
        if swapped:
            if os.name == "nt":
                nested.rmdir()
            else:
                nested.unlink()


def test_restore_rejects_a_workspace_root_replaced_before_construction(tmp_path):
    project = tmp_path / "project"
    project.mkdir()
    (project / "note.txt").write_text("snapshot", encoding="utf-8")
    SnapshotService(project).create("original")
    outside = tmp_path / "outside"
    outside.mkdir()
    victim = outside / "note.txt"
    victim.write_text("outside snapshot", encoding="utf-8")
    SnapshotService(outside).create("original")
    victim.write_text("outside current", encoding="utf-8")
    project.rename(tmp_path / "moved")
    _link_directory(project, outside)
    try:
        with pytest.raises((OSError, ValueError)):
            SnapshotService(project).restore("1")
        assert victim.read_text(encoding="utf-8") == "outside current"
    finally:
        if os.name == "nt":
            project.rmdir()
        else:
            project.unlink()


@pytest.mark.skipif(os.name == "nt", reason="Windows locks open directories against rename")
def test_restore_rejects_a_directory_moved_after_opening(tmp_path, monkeypatch):
    project = tmp_path / "project"
    nested = project / "nested"
    nested.mkdir(parents=True)
    (nested / "note").write_bytes(b"snapshot")
    service = SnapshotService(project)
    saved = service.create("original")
    (nested / "note").write_bytes(b"current")
    moved = tmp_path / "moved"
    checkpoint = service._create
    copy_file = shutil.copyfileobj
    armed = False

    def create(*args):
        nonlocal armed
        record = checkpoint(*args)
        armed = True
        return record

    def copy(source, target):
        nonlocal armed
        if armed:
            armed = False
            nested.rename(moved)
            nested.mkdir()
            (nested / "note").write_bytes(b"replacement")
        copy_file(source, target)

    monkeypatch.setattr(service, "_create", create)
    monkeypatch.setattr(shutil, "copyfileobj", copy)

    with pytest.raises(OSError, match="snapshot directory moved"):
        service.restore(saved.id)

    assert (moved / "note").read_bytes() == b"current"
    assert (nested / "note").read_bytes() == b"replacement"
    assert sorted(item.name for item in moved.iterdir()) == ["note"]


def test_restore_does_not_write_into_a_replaced_parent(tmp_path, monkeypatch):
    project = tmp_path / "project"
    nested = project / "nested"
    nested.mkdir(parents=True)
    note = nested / "note"
    note.write_bytes(b"snapshot")
    service = SnapshotService(project)
    saved = service.create("original")
    note.unlink()
    checkpoint = service._create

    def create(*args):
        record = checkpoint(*args)
        nested.rename(tmp_path / "moved")
        nested.mkdir()
        return record

    monkeypatch.setattr(service, "_create", create)

    with pytest.raises(OSError, match="workspace directory changed"):
        service.restore(saved.id)

    assert list(nested.iterdir()) == []


@pytest.mark.skipif(os.name != "nt", reason="Windows case-insensitive protected paths")
def test_restore_ignores_protected_names_in_modified_snapshots(tmp_path):
    project = tmp_path / "project"
    config = project / ".GIT" / "config"
    config.parent.mkdir(parents=True)
    config.write_text("live config", encoding="utf-8")
    service = SnapshotService(project)
    saved = service.create("original")
    injected = saved.path / ".GIT"
    assert not injected.exists()
    injected.mkdir()
    (injected / "config").write_text("injected snapshot config", encoding="utf-8")

    service.restore(saved.id)

    assert config.read_text(encoding="utf-8") == "live config"


def test_restore_replaces_hard_link_entry_without_writing_to_its_target(tmp_path):
    project = tmp_path / "project"
    project.mkdir()
    note = project / "note.txt"
    note.write_text("snapshot", encoding="utf-8")
    service = SnapshotService(project)
    saved = service.create("original")
    outside = tmp_path / "outside.txt"
    outside.write_text("outside", encoding="utf-8")
    note.unlink()
    os.link(outside, note)

    service.restore(saved.id)

    assert note.read_text(encoding="utf-8") == "snapshot"
    assert outside.read_text(encoding="utf-8") == "outside"
    assert not note.samefile(outside)


def test_restore_handles_two_hard_links_in_the_workspace(tmp_path):
    first = tmp_path / "first.txt"
    second = tmp_path / "second.txt"
    first.write_bytes(b"saved")
    os.link(first, second)
    service = SnapshotService(tmp_path)
    saved = service.create("original")
    first.write_bytes(b"live")

    service.restore(saved.id)

    assert first.read_bytes() == second.read_bytes() == b"saved"


def test_restore_preserves_read_only_files(tmp_path):
    note = tmp_path / "note.txt"
    note.write_text("snapshot", encoding="utf-8")
    note.chmod(stat.S_IREAD)
    service = SnapshotService(tmp_path)
    try:
        saved = service.create("original")
        note.chmod(stat.S_IREAD | stat.S_IWRITE)
        note.unlink()
        service.restore(saved.id)
        service.restore(saved.id)
        assert not note.stat().st_mode & stat.S_IWRITE
        assert note.read_text(encoding="utf-8") == "snapshot"
        assert service.clean() == 3
    finally:
        if note.exists():
            note.chmod(stat.S_IREAD | stat.S_IWRITE)


def test_restore_rejects_a_second_writer_in_the_same_workspace(tmp_path, monkeypatch):
    note = tmp_path / "note.txt"
    note.write_text("snapshot", encoding="utf-8")
    service = SnapshotService(tmp_path)
    saved = service.create("original")
    note.write_text("current", encoding="utf-8")
    original_create = service._create

    def create(storage, source, phase, protected):
        if phase == "pre-restore":
            with pytest.raises(OSError, match="another snapshot operation"):
                SnapshotService(tmp_path).restore(saved.id)
        return original_create(storage, source, phase, protected)

    monkeypatch.setattr(service, "_create", create)

    service.restore(saved.id)

    assert note.read_text(encoding="utf-8") == "snapshot"
    assert [snapshot.phase for snapshot in service.list()] == ["pre-restore", "original"]


def test_failed_restore_rolls_back_files_that_were_already_replaced(tmp_path, monkeypatch):
    for name in ("one.txt", "two.txt"):
        (tmp_path / name).write_text("snapshot", encoding="utf-8")
    service = SnapshotService(tmp_path)
    saved = service.create("original")
    for name in ("one.txt", "two.txt"):
        (tmp_path / name).write_text("current", encoding="utf-8")
    original_create = service._create
    original_copy = shutil.copyfileobj
    copies_until_error = None

    def create(storage, source, phase, protected):
        nonlocal copies_until_error
        checkpoint = original_create(storage, source, phase, protected)
        if phase == "pre-restore":
            copies_until_error = 2
        return checkpoint

    def copy(source, target):
        nonlocal copies_until_error
        if copies_until_error is not None:
            copies_until_error -= 1
            if not copies_until_error:
                copies_until_error = None
                (tmp_path / "external-edit.txt").write_text("new", encoding="utf-8")
                raise OSError("injected restore copy failure")
        original_copy(source, target)

    monkeypatch.setattr(service, "_create", create)
    monkeypatch.setattr(shutil, "copyfileobj", copy)

    with pytest.raises(OSError, match="injected restore copy failure"):
        service.restore(saved.id)

    assert all(
        (tmp_path / name).read_text(encoding="utf-8") == "current"
        for name in ("one.txt", "two.txt")
    )
    assert service.list()[0].phase == "pre-restore"
    assert (tmp_path / "external-edit.txt").read_text(encoding="utf-8") == "new"
    assert not list(tmp_path.glob(".libi-*.tmp"))


def test_rollback_preserves_external_edit_made_after_file_installation(tmp_path, monkeypatch):
    for name in ("one.txt", "two.txt"):
        (tmp_path / name).write_bytes(b"snapshot")
    service = SnapshotService(tmp_path)
    saved = service.create("original")
    for name in ("one.txt", "two.txt"):
        (tmp_path / name).write_bytes(b"current")
    checkpoint = service._create
    write = Directory.write
    restoring = False
    edited = None

    def create(*args):
        nonlocal restoring
        record = checkpoint(*args)
        restoring = True
        return record

    @contextmanager
    def edit_after_install(directory, name, *args):
        nonlocal edited
        if restoring and edited is not None and name != edited.name:
            raise OSError("injected restore failure")
        with write(directory, name, *args) as output:
            yield output
        if restoring and edited is None:
            edited = tmp_path / name
            edited.write_bytes(b"external editor")

    monkeypatch.setattr(service, "_create", create)
    monkeypatch.setattr(Directory, "write", edit_after_install)

    with pytest.raises(OSError, match="Recovery snapshot"):
        service.restore(saved.id)

    assert edited is not None
    assert edited.read_bytes() == b"external editor"
    assert service.list()[0].phase == "pre-restore"


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

    def fail_copy(*_args):
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
        async def delegate(self, tasks, _context):
            (payload,) = tasks
            (Path(payload["cwd"]) / "note.txt").write_text("changed", encoding="utf-8")
            return ToolResult("child finished")

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
