"""Snapshot I/O anchored to open directories and single filesystem entries."""

from __future__ import annotations

import os
import stat
import sys
from collections.abc import Callable
from contextlib import ExitStack, contextmanager, suppress
from pathlib import Path

if os.name == "nt":
    from paicli.snapshot._windows import Directory, open_anchor
else:
    import fcntl

    def _mount_id(fd: int) -> str | None:
        if sys.platform != "linux":
            return None
        with suppress(FileNotFoundError), open(f"/proc/self/fdinfo/{fd}", encoding="ascii") as info:
            for line in info:
                if line.startswith("mnt_id:"):
                    return line.split()[1]
        return None

    def _file_version(info: os.stat_result) -> tuple[int, ...]:
        return (
            info.st_dev,
            info.st_ino,
            info.st_mode,
            info.st_size,
            info.st_mtime_ns,
            info.st_nlink,
            info.st_ctime_ns,
        )

    class Directory:
        def __init__(self, fd: int, parent: Directory | None = None, name: str = ""):
            self.fd = fd
            info = os.fstat(fd)
            self.identity = (info.st_dev, info.st_ino)
            self.mount_id = _mount_id(fd)
            self.parent = parent
            self.name = name

        def check_bound(self) -> None:
            if self.parent is None:
                return
            self.parent.check_bound()
            try:
                info = os.stat(self.name, dir_fd=self.parent.fd, follow_symlinks=False)
            except FileNotFoundError as exc:
                raise OSError(f"snapshot directory moved: {self.name}") from exc
            if not stat.S_ISDIR(info.st_mode) or (info.st_dev, info.st_ino) != self.identity:
                raise OSError(f"snapshot directory moved: {self.name}")

        def version(self, name: str) -> tuple[int, ...] | None:
            self.check_bound()
            try:
                info = os.stat(name, dir_fd=self.fd, follow_symlinks=False)
            except FileNotFoundError:
                return None
            if stat.S_ISDIR(info.st_mode):
                return (info.st_dev, info.st_ino)
            return _file_version(info)

        def mode(self) -> int:
            return stat.S_IMODE(os.fstat(self.fd).st_mode) & 0o777

        def set_mode(self, mode: int) -> None:
            self.check_bound()
            os.fchmod(self.fd, mode)

        def entries(self) -> dict[str, str]:
            self.check_bound()
            result = {}
            with os.scandir(self.fd) as entries:
                for entry in entries:
                    try:
                        info = entry.stat(follow_symlinks=False)
                    except FileNotFoundError:
                        continue
                    if info.st_dev != self.identity[0]:
                        kind = "link"
                    elif stat.S_ISDIR(info.st_mode):
                        kind = "dir"
                    elif stat.S_ISREG(info.st_mode):
                        kind = "file"
                    else:
                        kind = "link"
                    result[entry.name] = kind
            return result

        @contextmanager
        def directory(
            self, name: str, *, create: bool = False, allow_mount: bool = False, exclusive=False
        ):
            self.check_bound()
            if exclusive:
                os.mkdir(name, mode=0o700, dir_fd=self.fd)
            elif create:
                with suppress(FileExistsError):
                    os.mkdir(name, mode=0o700, dir_fd=self.fd)
            fd = os.open(name, os.O_DIRECTORY | os.O_NOFOLLOW, dir_fd=self.fd)
            try:
                child = Directory(fd, self, name)
                if not allow_mount and (
                    child.identity[0] != self.identity[0] or child.mount_id != self.mount_id
                ):
                    raise ValueError(f"protected mount point: {name}")
                child.check_bound()
                yield child
            finally:
                os.close(fd)

        @contextmanager
        def read(self, name: str):
            self.check_bound()
            flags = os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK
            fd = os.open(name, flags, dir_fd=self.fd)
            with os.fdopen(fd, "rb") as source:
                info = os.fstat(source.fileno())
                if not stat.S_ISREG(info.st_mode) or info.st_dev != self.identity[0]:
                    raise ValueError(f"not a regular snapshot file: {name}")
                yield source

        def remove(
            self, name: str, *, directory: bool = False, expected: tuple[int, ...] | None = None
        ) -> None:
            self.check_bound()
            if expected is not None and self.version(name) != expected:
                raise OSError(f"workspace entry changed during restore: {name}")
            info = os.stat(name, dir_fd=self.fd, follow_symlinks=False)
            expected_kind = stat.S_ISDIR if directory else stat.S_ISREG
            if info.st_dev != self.identity[0] or not expected_kind(info.st_mode):
                raise ValueError(f"protected link: {name}")
            operation = os.rmdir if directory else os.unlink
            operation(name, dir_fd=self.fd)

        @contextmanager
        def lock(self, name: str):
            self.check_bound()
            flags = os.O_RDWR | os.O_CREAT | os.O_NOFOLLOW | os.O_NONBLOCK
            fd = os.open(name, flags, mode=0o600, dir_fd=self.fd)
            try:
                if not stat.S_ISREG(os.fstat(fd).st_mode):
                    raise ValueError(f"invalid snapshot lock: {name}")
                try:
                    fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
                except BlockingIOError as exc:
                    raise OSError("another snapshot operation is running") from exc
                yield
            finally:
                os.close(fd)

        @contextmanager
        def write(
            self,
            name: str,
            temporary: str,
            metadata: os.stat_result,
            expected: tuple[int, ...] | None,
            record: Callable[[tuple[int, ...]], None],
        ):
            self.check_bound()
            flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW
            fd = os.open(temporary, flags, mode=0o600, dir_fd=self.fd)
            try:
                with os.fdopen(fd, "wb") as target:
                    yield target
                    target.flush()
                    os.fchmod(target.fileno(), stat.S_IMODE(metadata.st_mode) & 0o777)
                    os.utime(target.fileno(), ns=(metadata.st_atime_ns, metadata.st_mtime_ns))
                    before = _file_version(os.fstat(target.fileno()))
                    if self.version(name) != expected:
                        raise OSError(f"workspace entry changed during restore: {name}")
                    if expected is None:
                        os.link(temporary, name, src_dir_fd=self.fd, dst_dir_fd=self.fd)
                        os.unlink(temporary, dir_fd=self.fd)
                    else:
                        os.replace(temporary, name, src_dir_fd=self.fd, dst_dir_fd=self.fd)
                    # Installing the entry changes ctime, while its saved bytes and mode are fixed.
                    record((*before[:-1], os.fstat(target.fileno()).st_ctime_ns))
            finally:
                with suppress(FileNotFoundError):
                    os.unlink(temporary, dir_fd=self.fd)

    @contextmanager
    def open_anchor(path: Path):
        fd = os.open(path, os.O_DIRECTORY | os.O_NOFOLLOW)
        try:
            yield Directory(fd)
        finally:
            os.close(fd)


@contextmanager
def open_directory(path: Path, *, create: bool = False):
    if not path.is_absolute() or ".." in path.parts:
        raise ValueError(f"invalid snapshot path: {path}")
    with ExitStack() as stack:
        current = stack.enter_context(open_anchor(Path(path.anchor)))
        for name in path.parts[1:]:
            current = stack.enter_context(current.directory(name, create=create, allow_mount=True))
        yield current
