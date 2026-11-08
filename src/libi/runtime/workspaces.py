from __future__ import annotations

import os
from contextlib import suppress
from pathlib import Path


class WorkspaceError(ValueError):
    """A directory error whose message is safe to show in the workbench."""


def resolve_directory(value: object) -> Path:
    if not isinstance(value, str) or not value.strip():
        raise WorkspaceError("请输入文件夹的绝对路径。")
    try:
        path = Path(value).expanduser()
        if not path.is_absolute():
            raise WorkspaceError("请输入文件夹的绝对路径。")
        path = path.resolve(strict=True)
        if path.is_dir():
            return path
    except (FileNotFoundError, NotADirectoryError):
        pass
    except (OSError, RuntimeError):
        raise WorkspaceError("无法访问此文件夹，请检查路径和权限。") from None
    except ValueError as exc:
        if isinstance(exc, WorkspaceError):
            raise
        raise WorkspaceError("文件夹路径无效。") from None
    raise WorkspaceError("文件夹不存在，请选择其他目录。")


def browse_directory(value: str) -> dict:
    path = resolve_directory(value)
    try:
        directories = []
        with os.scandir(path) as entries:
            for entry in entries:
                with suppress(OSError):
                    if entry.is_dir():
                        directories.append({"name": entry.name, "path": entry.path})
    except OSError:
        raise WorkspaceError("无法读取此文件夹，请检查权限或选择其他目录。") from None
    return {
        "path": str(path),
        "parent": str(path.parent) if path.parent != path else None,
        "home": str(Path.home()),
        "roots": _drive_roots() if os.name == "nt" else [path.anchor],
        "directories": sorted(directories, key=lambda entry: entry["name"].casefold()),
    }


def _drive_roots() -> list[str]:
    if hasattr(os, "listdrives"):
        return os.listdrives()
    # os.listdrives was added in Python 3.12. Keep Windows browsing on 3.11.
    import ctypes
    from string import ascii_uppercase

    drive_mask = ctypes.windll.kernel32.GetLogicalDrives()
    return [
        f"{letter}:\\" for index, letter in enumerate(ascii_uppercase) if drive_mask & (1 << index)
    ]
