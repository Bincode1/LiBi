from __future__ import annotations

import errno
import os
from contextlib import ExitStack
from pathlib import Path

if os.name == "nt":
    import msvcrt
else:
    import fcntl


def try_file_lock(path: Path) -> ExitStack | None:
    """Acquire a non-blocking OS lock held until the returned context closes."""
    path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
    # Keep the inode stable across owners and contenders.
    with ExitStack() as lock:
        handle = lock.enter_context(path.open("a+b"))
        try:
            handle.seek(0)
            if os.name == "nt":
                msvcrt.locking(handle.fileno(), msvcrt.LK_NBLCK, 1)
                lock.callback(msvcrt.locking, handle.fileno(), msvcrt.LK_UNLCK, 1)
            else:
                fcntl.flock(handle, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError as exc:
            if exc.errno in {errno.EACCES, errno.EAGAIN}:
                return None
            raise
        return lock.pop_all()
