from __future__ import annotations

import os
import stat
from collections.abc import Collection, Iterator
from contextlib import ExitStack, contextmanager
from pathlib import Path


class PathPolicyError(ValueError):
    pass


class PathGuard:
    """Restrict file tools to the current workspace tree."""

    def __init__(self, root: str | Path):
        self.root = Path(root).resolve()

    def validate(self, value: str | Path) -> Path:
        candidate = Path(value)
        if not candidate.is_absolute():
            candidate = self.root / candidate
        resolved = candidate.resolve()
        try:
            resolved.relative_to(self.root)
        except ValueError as exc:
            raise PathPolicyError(f"path escapes workspace: {value}") from exc
        return resolved

    @contextmanager
    def open_read(self, value: str | Path):
        """Open a verified workspace file without following a concurrent link swap."""
        from libi.snapshot._filetree import open_directory

        resolved = self.validate(value)
        parts = resolved.relative_to(self.root).parts
        if not parts:
            raise PathPolicyError("workspace root is not a regular file")
        with ExitStack() as stack:
            directory = stack.enter_context(open_directory(self.root))
            for name in parts[:-1]:
                directory = stack.enter_context(directory.directory(name))
            yield stack.enter_context(directory.read(parts[-1]))


def scan_entry(path: Path) -> bool:
    """Skip links, junctions, mount reparse points and special files during discovery."""
    try:
        info = path.lstat()
    except OSError:
        return False
    return not getattr(info, "st_file_attributes", 0) & stat.FILE_ATTRIBUTE_REPARSE_POINT and (
        stat.S_ISREG(info.st_mode) or stat.S_ISDIR(info.st_mode)
    )


def scan_files(root: Path, *, exclude: Collection[str] = ()) -> Iterator[Path]:
    for directory, dirs, files in os.walk(root):
        base = Path(directory)
        dirs[:] = [name for name in dirs if name not in exclude and scan_entry(base / name)]
        for name in files:
            candidate = base / name
            if scan_entry(candidate):
                yield candidate
