"""Cross-process file locks that work on Windows (msvcrt) and POSIX (fcntl).

    with file_lock(path_of_the_file_you_guard):
        read - modify - write

The lock lives in a sibling file ``<name>.lock`` (never deleted: deleting it would race other holders). It is advisory:
it only excludes other code that takes the same lock. Within one process, combine it with a threading lock (the OS
lock is per file handle; two threads opening their own handles still exclude each other on both platforms).
"""
from __future__ import annotations

import os
import time
from contextlib import contextmanager
from pathlib import Path

if os.name == "nt":
    import msvcrt
else:
    import fcntl


class LockTimeout(TimeoutError):
    pass


def _try_lock(fh) -> bool:
    try:
        if os.name == "nt":
            fh.seek(0)
            msvcrt.locking(fh.fileno(), msvcrt.LK_NBLCK, 1)
        else:
            fcntl.flock(fh.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        return True
    except OSError:
        return False


def _unlock(fh) -> None:
    try:
        if os.name == "nt":
            fh.seek(0)
            msvcrt.locking(fh.fileno(), msvcrt.LK_UNLCK, 1)
        else:
            fcntl.flock(fh.fileno(), fcntl.LOCK_UN)
    except OSError:
        pass


@contextmanager
def file_lock(path, timeout: float = 30.0, poll: float = 0.05):
    """Hold an exclusive lock on ``<path>.lock`` for the duration of the block. Raises LockTimeout after ``timeout``."""
    lock_path = Path(str(path) + ".lock")
    lock_path.parent.mkdir(parents=True, exist_ok=True)
    fh = open(lock_path, "a+b")
    try:
        t0 = time.monotonic()
        while not _try_lock(fh):
            if time.monotonic() - t0 > timeout:
                raise LockTimeout(f"could not lock {lock_path} within {timeout:.0f} s (another designer process?)")
            time.sleep(poll)
        try:
            yield
        finally:
            _unlock(fh)
    finally:
        fh.close()


def atomic_write_text(path, text: str, encoding: str = "utf-8") -> None:
    """Write ``text`` to a temp file in the same directory and os.replace() it over ``path`` (readers never see a
    half-written file). Retries briefly on Windows, where a reader holding the target open makes replace fail."""
    import tempfile
    p = Path(path)
    p.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp = tempfile.mkstemp(prefix=f".{p.name}.", suffix=".tmp", dir=str(p.parent))
    try:
        with os.fdopen(fd, "w", encoding=encoding, newline="\n") as fh:
            fh.write(text)
            fh.flush()
            os.fsync(fh.fileno())
        for attempt in range(20):
            try:
                os.replace(tmp, p)
                break
            except PermissionError:
                if os.name != "nt" or attempt == 19:
                    raise
                time.sleep(0.05)
    finally:
        if os.path.exists(tmp):
            os.remove(tmp)
