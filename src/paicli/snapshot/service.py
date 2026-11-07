from __future__ import annotations

import hashlib
import json
import shutil
import stat
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path

SKIP_DIRS = {".git", ".venv", "node_modules", "dist", "build", "target", "__pycache__"}


@dataclass(slots=True)
class SnapshotRecord:
    id: str
    phase: str
    created_at: str
    path: Path


class SnapshotService:
    def __init__(self, project_root: str | Path):
        self.project_root = Path(project_root).resolve()
        digest = hashlib.sha256(str(self.project_root).encode("utf-8")).hexdigest()[:16]
        storage = Path.home() / ".paicli" / "snapshots"
        self.root = storage / digest
        self._storage_paths = {storage, storage.resolve()}
        self.index_path = self.root / "index.jsonl"
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
        if any(self.project_root.is_relative_to(path) for path in self._storage_paths):
            raise ValueError("cannot snapshot a workspace inside snapshot storage")
        snapshot_id = f"{phase}_{datetime.now(UTC).strftime('%Y%m%d%H%M%S%f')}"
        target = self.root / snapshot_id
        target.mkdir(parents=True, exist_ok=True)
        self._copy_tree(self.project_root, target)
        record = SnapshotRecord(
            id=snapshot_id,
            phase=phase,
            created_at=datetime.now(UTC).isoformat(),
            path=target,
        )
        with self.index_path.open("a", encoding="utf-8") as handle:
            handle.write(
                json.dumps(
                    {
                        "id": record.id,
                        "phase": record.phase,
                        "created_at": record.created_at,
                        "path": str(record.path),
                    },
                    ensure_ascii=False,
                )
                + "\n"
            )
        return record

    def list(self, limit: int = 20) -> list[SnapshotRecord]:
        if not self.index_path.exists():
            return []
        records = []
        for line in self.index_path.read_text(encoding="utf-8").splitlines():
            try:
                item = json.loads(line)
            except json.JSONDecodeError:
                continue
            records.append(
                SnapshotRecord(
                    id=item["id"],
                    phase=item["phase"],
                    created_at=item["created_at"],
                    path=Path(item["path"]),
                )
            )
        return records[-limit:][::-1]

    def restore(self, snapshot_ref: str) -> SnapshotRecord:
        records = self.list(limit=200)
        record = None
        if snapshot_ref.isdigit():
            index = int(snapshot_ref) - 1
            if 0 <= index < len(records):
                record = records[index]
        else:
            record = next((item for item in records if item.id == snapshot_ref), None)
        if not record:
            raise ValueError(f"snapshot not found: {snapshot_ref}")
        source = record.path.resolve()
        if source.parent != self.root.resolve() or not source.is_dir():
            raise ValueError(f"snapshot source is missing or invalid: {record.id}")
        removals: list[tuple[Path, bool]] = []
        self._plan_removals(source, self.project_root, removals)
        self.create("pre-restore")
        for path, is_directory in removals:
            if is_directory:
                path.rmdir()
            else:
                path.unlink()
        self._copy_tree(source, self.project_root)
        return record

    def clean(self) -> int:
        count = len(self.list(limit=10_000))
        if self.root.exists():
            shutil.rmtree(self.root)
        self.root.mkdir(parents=True, exist_ok=True)
        return count

    def _copy_tree(self, source: Path, target: Path) -> None:
        def ignore(directory: str, names: list[str]) -> set[str]:
            current = Path(directory)
            relative = current.relative_to(source)
            return {
                name
                for name in names
                if self._skip(self.project_root / relative / name) or _is_link(current / name)
            }

        for item in source.iterdir():
            if self._skip(self.project_root / item.name) or _is_link(item):
                continue
            destination = target / item.name
            if item.is_dir():
                shutil.copytree(item, destination, ignore=ignore, dirs_exist_ok=True)
            elif item.is_file():
                shutil.copy2(item, destination)

    def _plan_removals(
        self, source: Path | None, target: Path, removals: list[tuple[Path, bool]]
    ) -> bool:
        """Plan deletions bottom-up, keeping directories that contain excluded paths."""
        preserves_children = False
        for item in target.iterdir():
            if self._skip(item):
                preserves_children = True
                continue
            saved = source / item.name if source is not None else None
            if saved is not None and _is_link(saved):
                saved = None
            if _is_link(item):
                if saved is not None and saved.exists():
                    raise ValueError(f"cannot restore over protected link: {item}")
                preserves_children = True
                continue
            if not item.is_dir():
                removals.append((item, False))
                continue
            saved_directory = saved if saved is not None and saved.is_dir() else None
            preserves_nested = self._plan_removals(saved_directory, item, removals)
            if preserves_nested:
                if saved is not None and saved.is_file():
                    raise ValueError(
                        f"cannot restore file over directory with protected paths: {item}"
                    )
                preserves_children = True
            elif saved_directory is None:
                removals.append((item, True))
        return preserves_children

    def _skip(self, path: Path) -> bool:
        return path.name in SKIP_DIRS or path in self._storage_paths


def _is_link(path: Path) -> bool:
    try:
        info = path.lstat()
    except FileNotFoundError:
        return False
    junction_tag = getattr(stat, "IO_REPARSE_TAG_MOUNT_POINT", -1)
    return stat.S_ISLNK(info.st_mode) or getattr(info, "st_reparse_tag", 0) == junction_tag
