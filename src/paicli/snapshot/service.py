from __future__ import annotations

import hashlib
import os
import re
import shutil
import stat
import unicodedata
import uuid
from contextlib import ExitStack, contextmanager, suppress
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path

from paicli.snapshot._filetree import Directory, open_directory

SKIP_NAMES = {".git", ".venv", "node_modules", "dist", "build", "target", "__pycache__"}
_LOCK = ".snapshot.lock"
_RECORD = re.compile(r"(\d{20})_([0-9a-f]{12})_([a-z][a-z0-9-]*)")
_Parts = tuple[str, ...]
_Version = tuple[int, ...] | None


@dataclass(slots=True)
class SnapshotRecord:
    id: str
    phase: str
    created_at: str
    path: Path


@dataclass(frozen=True, slots=True)
class _Change:
    path: _Parts
    previous: str | None
    expected: _Version
    kind: str | None = None
    source: _Parts = ()


@dataclass(slots=True)
class _ProtectedPaths:
    paths: dict[str, set[Path]] = field(default_factory=dict)

    def add(self, path: Path) -> None:
        self.paths.setdefault(_name_key(str(path)), set()).add(path)

    def __contains__(self, path: Path) -> bool:
        for candidate in self.paths.get(_name_key(str(path)), ()):
            if path == candidate:
                return True
            with suppress(FileNotFoundError):
                if path.samefile(candidate):
                    return True
        return False


@dataclass(slots=True)
class _RestorePlan:
    protected: _ProtectedPaths
    changes: list[_Change] = field(default_factory=list)
    directories: dict[_Parts, _Version] = field(default_factory=dict)
    unlinked: dict[tuple[int, ...], int] = field(default_factory=dict)

    def matches(self, current: _Version, expected: _Version) -> bool:
        if current == expected:
            return True
        if current is None or expected is None or len(current) != 7 or len(expected) != 7:
            return False
        removed = self.unlinked.get(expected[:2], 0)
        # Removing a hardlink updates ctime and the link count on the remaining names.
        return (
            removed > 0 and current[:-2] == expected[:-2] and current[-2] == expected[-2] - removed
        )


class SnapshotService:
    def __init__(self, project_root: str | Path):
        self.project_root = Path(os.path.abspath(project_root))
        digest = hashlib.sha256(os.path.normcase(str(self.project_root)).encode()).hexdigest()[:16]
        self._storage_path = Path(os.path.abspath(Path.home())) / ".paicli" / "snapshots"
        self.root = self._storage_path / "v2" / digest
        self._turn_checkpointed = False

    def checkpoint_turn(self) -> None:
        if not self._turn_checkpointed:
            self.create("pre-turn")
            self._turn_checkpointed = True

    def finish_turn(self) -> None:
        if self._turn_checkpointed:
            self.create("post-turn")
            self._turn_checkpointed = False

    def create(self, phase: str) -> SnapshotRecord:
        if self.project_root.is_relative_to(self._storage_path):
            raise ValueError("cannot snapshot a workspace inside snapshot storage")
        with (
            open_directory(self.root, create=True) as storage,
            storage.lock(_LOCK),
            open_directory(self.project_root) as source,
        ):
            return self._create(storage, source, phase, self._protected_paths(source))

    def _create(
        self, storage: Directory, source: Directory, phase: str, protected: _ProtectedPaths
    ) -> SnapshotRecord:
        stamp = datetime.now(UTC).strftime("%Y%m%d%H%M%S%f")
        snapshot_id = f"{stamp}_{uuid.uuid4().hex[:12]}_{phase}"
        record = self._record(snapshot_id)
        if record is None:
            raise ValueError(f"invalid snapshot phase: {phase}")
        with storage.directory(snapshot_id, exclusive=True) as snapshot:
            try:
                with snapshot.directory("tree", exclusive=True) as tree:
                    self._copy_tree(source, tree, (), protected)
                # The ready entry publishes the tree only after the complete copy.
                with snapshot.directory("ready", exclusive=True):
                    pass
            except (OSError, ValueError, KeyboardInterrupt):
                with suppress(OSError, ValueError):
                    self._clear(snapshot)
                raise
        return record

    def list(self, limit: int = 20) -> list[SnapshotRecord]:
        with self._existing_storage() as storage:
            return self._records(storage)[:limit] if storage is not None else []

    def _record(self, name: str) -> SnapshotRecord | None:
        match = _RECORD.fullmatch(name)
        if match is None:
            return None
        stamp, _, phase = match.groups()
        try:
            created_at = datetime.strptime(stamp, "%Y%m%d%H%M%S%f").replace(tzinfo=UTC)
        except ValueError:
            return None
        return SnapshotRecord(name, phase, created_at.isoformat(), self.root / name / "tree")

    def _records(self, storage: Directory) -> list[SnapshotRecord]:
        records = []
        for name, kind in sorted(storage.entries().items(), reverse=True):
            record = self._record(name) if kind == "dir" else None
            if record is not None:
                with storage.directory(name) as snapshot:
                    if snapshot.entries().get("ready") == "dir":
                        records.append(record)
        return records

    def restore(self, snapshot_ref: str) -> SnapshotRecord:
        with self._existing_storage() as storage:
            if storage is None:
                raise ValueError(f"snapshot not found: {snapshot_ref}")
            return self._restore_locked(storage, snapshot_ref)

    def _restore_locked(self, storage: Directory, snapshot_ref: str) -> SnapshotRecord:
        records = self._records(storage)
        if snapshot_ref.isascii() and snapshot_ref.isdecimal():
            number = int(snapshot_ref)
            record = records[number - 1] if 0 < number <= len(records) else None
        else:
            record = next((item for item in records if item.id == snapshot_ref), None)
        if record is None:
            raise ValueError(f"snapshot not found: {snapshot_ref}")

        with ExitStack() as stack:
            try:
                snapshot = stack.enter_context(storage.directory(record.id))
                source = stack.enter_context(snapshot.directory("tree"))
            except (OSError, ValueError) as exc:
                raise ValueError(f"snapshot source is missing or invalid: {record.id}") from exc
            target = stack.enter_context(open_directory(self.project_root))
            protected = self._protected_paths(target)
            plan = _RestorePlan(protected)
            self._plan(source, target, (), (), plan)
            backup = self._create(storage, target, "pre-restore", protected)
            if self._protected_paths(target) != protected:
                raise OSError("Git metadata paths changed during restore")

            journal: list[tuple[_Change, _Version]] = []
            modes: list[tuple[_Parts, _Version, int]] = []
            try:
                for change in plan.changes:
                    installed = self._apply(source, target, change, plan, modes)
                    journal.append((change, installed))
                self._set_modes(target, plan.directories, modes)
            except (OSError, ValueError, KeyboardInterrupt) as exc:
                if journal:
                    self._rollback(storage, target, backup, plan, journal, exc)
                raise
        return record

    def clean(self) -> int:
        with self._existing_storage() as storage:
            if storage is None:
                return 0
            count = len(self._records(storage))
            self._clear(storage, skip=_LOCK)
            return count

    @contextmanager
    def _existing_storage(self):
        with ExitStack() as stack:
            try:
                storage = stack.enter_context(open_directory(self.root))
            except FileNotFoundError:
                yield None
                return
            with storage.lock(_LOCK):
                yield storage

    def _copy_tree(
        self, source: Directory, target: Directory, path: _Parts, protected: _ProtectedPaths
    ) -> None:
        for name, kind in source.entries().items():
            child = (*path, name)
            if kind == "link" or self._skip(child, protected):
                continue
            if kind == "dir":
                with (
                    source.directory(name) as original,
                    target.directory(name, exclusive=True) as copy,
                ):
                    self._copy_tree(original, copy, child, protected)
                    copy.set_mode(original.mode())
            else:
                self._copy_file(source, name, target, name, None)

    def _plan(
        self,
        source: Directory | None,
        target: Directory | None,
        source_path: _Parts,
        target_path: _Parts,
        plan: _RestorePlan,
    ) -> bool:
        wanted = {
            name: kind
            for name, kind in (source.entries() if source is not None else {}).items()
            if kind != "link" and not self._skip((*target_path, name), plan.protected)
        }
        live = target.entries() if target is not None else {}
        aliases: dict[str, list[str]] = {}
        for name in wanted:
            aliases.setdefault(_name_key(name), []).append(name)

        preserves_children = False
        for name, kind in live.items():
            path = (*target_path, name)
            if self._skip(path, plan.protected):
                preserves_children = True
                continue
            assert target is not None
            saved_name = name if name in wanted else None
            if saved_name is None and kind != "link":
                for alias in aliases.get(_name_key(name), ()):
                    if (
                        alias in wanted
                        and alias not in live
                        and (target.version(alias) == target.version(name))
                    ):
                        saved_name = alias
                        break
            saved_kind = wanted.pop(saved_name) if saved_name is not None else None
            if kind == "link":
                if saved_kind is not None:
                    raise ValueError(f"cannot restore over protected link: {'/'.join(path)}")
                preserves_children = True
                continue

            expected = target.version(name)
            if expected is None:
                raise OSError(f"workspace entry changed during restore: {'/'.join(path)}")
            if kind == "dir":
                plan.directories[path] = expected
            if saved_kind == kind == "dir":
                assert source is not None and saved_name is not None
                with source.directory(saved_name) as saved, target.directory(name) as current:
                    preserves_children |= self._plan(
                        saved, current, (*source_path, saved_name), path, plan
                    )
                continue
            if saved_kind == kind == "file" and saved_name == name:
                plan.changes.append(_Change(path, kind, expected, kind, (*source_path, name)))
                continue
            if kind == "dir":
                with target.directory(name) as current:
                    preserves_nested = self._plan(None, current, (), path, plan)
                if preserves_nested:
                    if saved_kind is not None:
                        raise ValueError(
                            f"cannot restore file over directory with protected paths: "
                            f"{'/'.join(path)}"
                        )
                    preserves_children = True
                    continue
            plan.changes.append(_Change(path, kind, expected))
            if saved_name is not None and saved_kind is not None:
                wanted[saved_name] = saved_kind

        for name, kind in wanted.items():
            path = (*target_path, name)
            saved_path = (*source_path, name)
            plan.changes.append(_Change(path, None, None, kind, saved_path))
            if kind == "dir":
                assert source is not None
                with source.directory(name) as saved:
                    self._plan(saved, None, saved_path, path, plan)
        return preserves_children

    def _apply(
        self,
        source: Directory,
        target: Directory,
        change: _Change,
        plan: _RestorePlan,
        modes: list[tuple[_Parts, _Version, int]],
    ) -> _Version:
        name = change.path[-1]
        with _descend(target, change.path[:-1], plan.directories) as parent:
            current = parent.version(name)
            if not plan.matches(current, change.expected):
                raise OSError(f"workspace entry changed during restore: {'/'.join(change.path)}")
            if change.kind is None:
                parent.remove(name, directory=change.previous == "dir", expected=current)
                plan.directories.pop(change.path, None)
                installed = None
            elif change.kind == "file":
                with _descend(source, change.source[:-1]) as original:
                    installed = self._copy_file(original, change.source[-1], parent, name, current)
            else:
                with (
                    _descend(source, change.source) as original,
                    parent.directory(name, exclusive=True),
                ):
                    mode = original.mode()
                installed = parent.version(name)
                plan.directories[change.path] = installed
                modes.append((change.path, installed, mode))
            if change.previous == "file" and current is not None and current[-2] > 1:
                identity = current[:2]
                plan.unlinked[identity] = plan.unlinked.get(identity, 0) + 1
            return installed

    @staticmethod
    def _copy_file(
        source: Directory, name: str, target: Directory, destination: str, expected: _Version
    ) -> tuple[int, ...]:
        version = source.version(name)
        installed: list[tuple[int, ...]] = []
        with source.read(name) as original:
            metadata = os.fstat(original.fileno())
            temporary = f".paicli-{uuid.uuid4().hex}.tmp"
            with target.write(destination, temporary, metadata, expected, installed.append) as copy:
                shutil.copyfileobj(original, copy)
                if source.version(name) != version:
                    raise OSError(f"snapshot source changed while copying: {name}")
        return installed[0]

    @staticmethod
    def _set_modes(
        target: Directory,
        directories: dict[_Parts, _Version],
        modes: list[tuple[_Parts, _Version, int]],
    ) -> None:
        for path, version, mode in sorted(modes, key=lambda item: len(item[0]), reverse=True):
            with _descend(target, path[:-1], directories) as parent:
                if parent.version(path[-1]) != version:
                    raise OSError(f"workspace directory changed during restore: {'/'.join(path)}")
                with parent.directory(path[-1]) as directory:
                    directory.set_mode(mode)

    def _rollback(
        self,
        storage: Directory,
        target: Directory,
        backup: SnapshotRecord,
        plan: _RestorePlan,
        journal: list[tuple[_Change, _Version]],
        failure: BaseException,
    ) -> None:
        modes: list[tuple[_Parts, _Version, int]] = []
        errors = []
        with storage.directory(backup.id) as snapshot, snapshot.directory("tree") as source:
            for change, installed in reversed(journal):
                undo = _Change(change.path, change.kind, installed, change.previous, change.path)
                try:
                    self._apply(source, target, undo, plan, modes)
                except (OSError, ValueError) as exc:
                    errors.append(str(exc))
            try:
                self._set_modes(target, plan.directories, modes)
            except (OSError, ValueError) as exc:
                errors.append(str(exc))
        if errors:
            raise OSError(
                f"Restore failed and rollback could not finish. Recovery snapshot: {backup.id}. "
                f"Restore error: {failure}. Rollback errors: {'; '.join(errors)}"
            ) from failure

    def _protected_paths(self, workspace: Directory) -> _ProtectedPaths:
        if any(part.casefold() == ".git" for part in self.project_root.parts):
            raise ValueError("workspace is inside a Git metadata directory")
        protected = _ProtectedPaths()
        protected.add(self._storage_path)
        for name in ("GIT_DIR", "GIT_COMMON_DIR"):
            if value := os.environ.get(name):
                self._protect_git_path(self.project_root / value, protected)
        for ancestor in self.project_root.parents:
            with suppress(FileNotFoundError):
                if stat.S_ISREG((ancestor / ".git").lstat().st_mode):
                    with open_directory(ancestor) as directory:
                        self._read_gitfile(directory, ancestor, ".git", protected)
        self._find_gitfiles(workspace, (), protected)
        return protected

    def _find_gitfiles(
        self, directory: Directory, path: _Parts, protected: _ProtectedPaths
    ) -> None:
        entries = directory.entries()
        for name, kind in entries.items():
            if kind == "file" and name.casefold() == ".git":
                self._read_gitfile(directory, self.project_root.joinpath(*path), name, protected)
        for name, kind in entries.items():
            child = (*path, name)
            if kind == "dir" and not self._skip(child, protected):
                with directory.directory(name) as nested:
                    self._find_gitfiles(nested, child, protected)

    def _read_gitfile(
        self, directory: Directory, path: Path, name: str, protected: _ProtectedPaths
    ) -> None:
        with directory.read(name) as gitfile:
            line = gitfile.readline(8192).rstrip(b"\r\n")
        if line.startswith(b"gitdir: "):
            value = os.fsdecode(line.removeprefix(b"gitdir: "))
            self._protect_git_path(path / value, protected)

    def _protect_git_path(self, path: Path, protected: _ProtectedPaths) -> None:
        path = Path(os.path.abspath(path))
        if self.project_root.is_relative_to(path):
            raise ValueError("workspace is inside a Git metadata directory")
        protected.add(path)
        resolved = path.resolve()
        if self.project_root.is_relative_to(resolved):
            raise ValueError("workspace is inside a Git metadata directory")
        protected.add(resolved)
        try:
            with open_directory(resolved) as git, git.read("commondir") as common:
                value = os.fsdecode(common.readline(8192).rstrip(b"\r\n"))
        except FileNotFoundError:
            return
        if value:
            common_path = Path(os.path.abspath(resolved / value)).resolve()
            if self.project_root.is_relative_to(common_path):
                raise ValueError("workspace is inside a Git metadata directory")
            protected.add(common_path)

    def _skip(self, parts: _Parts, protected: _ProtectedPaths) -> bool:
        path = self.project_root.joinpath(*parts)
        return path.name.casefold() in SKIP_NAMES or path in protected

    @classmethod
    def _clear(cls, directory: Directory, *, skip: str | None = None) -> None:
        for name, kind in directory.entries().items():
            if name == skip:
                continue
            if kind == "link":
                raise ValueError(f"protected link in snapshot storage: {name}")
            if kind == "dir":
                with directory.directory(name) as child:
                    child.set_mode(0o700)
                    cls._clear(child)
            directory.remove(name, directory=kind == "dir")


@contextmanager
def _descend(directory: Directory, path: _Parts, versions: dict[_Parts, _Version] | None = None):
    with ExitStack() as stack:
        for index, part in enumerate(path, start=1):
            parent = directory
            directory = stack.enter_context(parent.directory(part))
            if versions is not None and parent.version(part) != versions[path[:index]]:
                raise OSError(
                    f"workspace directory changed during restore: {'/'.join(path[:index])}"
                )
        yield directory


def _name_key(name: str) -> str:
    return unicodedata.normalize("NFC", name).casefold()
