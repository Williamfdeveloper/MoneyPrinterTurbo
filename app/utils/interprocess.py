"""Cross-process coordination helpers for files shared by WebUI and API.

The WebUI and API can run as separate processes (or containers) on the same
data volume. In-memory locks do not protect them from each other, so shared
records use an OS file lock plus atomic replacement.
"""

import errno
import os
import tempfile
import threading
from contextlib import contextmanager
from pathlib import Path

if os.name == "nt":  # pragma: no cover - exercised only on Windows hosts
    import msvcrt
else:
    import fcntl

_thread_locks: dict[str, threading.RLock] = {}
_thread_locks_guard = threading.Lock()


def _thread_lock(path: str) -> threading.RLock:
    with _thread_locks_guard:
        lock = _thread_locks.get(path)
        if lock is None:
            lock = threading.RLock()
            _thread_locks[path] = lock
        return lock


@contextmanager
def file_lock(lock_path: str | os.PathLike):
    """Hold an exclusive lock on ``lock_path`` for this thread and process.

    The OS releases the lock when the process exits, so a crashed writer never
    leaves the queue permanently locked.
    """
    path = str(Path(lock_path).resolve())
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with _thread_lock(path):
        handle = open(path, "a+b")
        try:
            if os.name == "nt":  # pragma: no cover
                handle.seek(0)
                msvcrt.locking(handle.fileno(), msvcrt.LK_LOCK, 1)
            else:
                fcntl.flock(handle.fileno(), fcntl.LOCK_EX)
            try:
                yield
            finally:
                if os.name == "nt":  # pragma: no cover
                    handle.seek(0)
                    msvcrt.locking(handle.fileno(), msvcrt.LK_UNLCK, 1)
                else:
                    fcntl.flock(handle.fileno(), fcntl.LOCK_UN)
        finally:
            handle.close()


def write_text_atomic(target: str | os.PathLike, text: str) -> None:
    """Replace ``target`` with ``text`` without exposing a partial file."""
    target_path = Path(target)
    target_path.parent.mkdir(parents=True, exist_ok=True)
    fd, temp_name = tempfile.mkstemp(
        prefix=f".{target_path.name}.", suffix=".tmp", dir=target_path.parent
    )
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as temp_file:
            temp_file.write(text)
            temp_file.flush()
            os.fsync(temp_file.fileno())
        try:
            os.replace(temp_name, target_path)
        except OSError as exc:
            # A single-file bind mount cannot be replaced; rewrite it in place.
            if exc.errno != errno.EBUSY:
                raise
            with open(target_path, "w", encoding="utf-8") as target_file:
                target_file.write(text)
                target_file.flush()
                os.fsync(target_file.fileno())
    finally:
        if os.path.exists(temp_name):
            os.remove(temp_name)
