"""Cross-process single-writer file lock, independent of the fingerprint matcher.

The installer and the fingerprint store both need a single-writer lock. Keeping
it here means the standalone Hook runtime does not have to import the scanner /
matcher module just to write a file safely.
"""
from __future__ import annotations

import threading
from pathlib import Path

try:  # POSIX
    import fcntl  # type: ignore

    _POSIX = True
except ImportError:  # pragma: no cover
    _POSIX = False

try:  # Windows
    import msvcrt  # type: ignore

    _WINDOWS = not _POSIX
except ImportError:  # pragma: no cover
    _WINDOWS = False

_THREAD_LOCK = threading.Lock()


class _FileLock:
    """跨进程单写者锁: 锁文件 + fcntl/msvcrt 排他锁。

    与进程内 _THREAD_LOCK 叠加: 线程锁防同进程竞争, 文件锁防跨进程竞争。
    锁文件残留无害(下次进入正常加锁), 不做删除避免解锁竞态。
    """

    def __init__(self, path: Path) -> None:
        self._lock_path = Path(str(path) + ".lock")

    def __enter__(self) -> "_FileLock":
        self._lock_path.parent.mkdir(parents=True, exist_ok=True)
        self._fh = open(self._lock_path, "a+b")
        if _POSIX:
            fcntl.flock(self._fh.fileno(), fcntl.LOCK_EX)
        elif _WINDOWS:
            if self._fh.seek(0, 2) == 0:
                self._fh.write(b'0'); self._fh.flush()
            self._fh.seek(0)
            msvcrt.locking(self._fh.fileno(), msvcrt.LK_LOCK, 1)
        return self

    def __exit__(self, *exc) -> None:
        try:
            if _POSIX:
                fcntl.flock(self._fh.fileno(), fcntl.LOCK_UN)
            elif _WINDOWS:
                self._fh.seek(0)
                msvcrt.locking(self._fh.fileno(), msvcrt.LK_UNLCK, 1)
        finally:
            self._fh.close()


__all__ = ["_FileLock", "_THREAD_LOCK"]
