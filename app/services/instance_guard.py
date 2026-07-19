from __future__ import annotations

import fcntl
import os
from pathlib import Path
import threading
from typing import Protocol, TextIO


class InstanceGuard(Protocol):
    def acquire(self) -> bool: ...

    def release(self) -> None: ...


class FileInstanceGuard:
    def __init__(self, path: Path) -> None:
        self.path = Path(path)
        self._handle: TextIO | None = None
        self._state_lock = threading.Lock()

    def acquire(self) -> bool:
        with self._state_lock:
            if self._handle is not None:
                return True
            self.path.parent.mkdir(parents=True, exist_ok=True)
            handle = self.path.open("a+", encoding="utf-8")
            try:
                fcntl.flock(handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
            except BlockingIOError:
                handle.close()
                return False
            except BaseException:
                handle.close()
                raise
            try:
                handle.seek(0)
                handle.truncate()
                handle.write(str(os.getpid()))
                handle.flush()
            except BaseException:
                try:
                    fcntl.flock(handle.fileno(), fcntl.LOCK_UN)
                except OSError:
                    pass
                finally:
                    handle.close()
                raise
            self._handle = handle
            return True

    def release(self) -> None:
        with self._state_lock:
            handle = self._handle
            self._handle = None
            if handle is None:
                return
            try:
                fcntl.flock(handle.fileno(), fcntl.LOCK_UN)
            finally:
                handle.close()

    def held_by_other(self) -> bool:
        with self._state_lock:
            if self._handle is not None:
                return False
            self.path.parent.mkdir(parents=True, exist_ok=True)
            handle = self.path.open("a+", encoding="utf-8")
            try:
                try:
                    fcntl.flock(handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
                except BlockingIOError:
                    return True
                fcntl.flock(handle.fileno(), fcntl.LOCK_UN)
                return False
            finally:
                handle.close()


__all__ = ["FileInstanceGuard", "InstanceGuard"]
