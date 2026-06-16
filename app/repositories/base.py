from __future__ import annotations

import sqlite3
import threading
from contextlib import AbstractContextManager
from pathlib import Path

from app.db.connection import SQLiteConnectionFactory


class SQLiteRepository:
    def __init__(self, path: Path, lock: threading.RLock) -> None:
        self._path = path
        self._connections = SQLiteConnectionFactory(path)
        self._lock = lock

    def _connect(self) -> AbstractContextManager[sqlite3.Connection]:
        return self._connections.connect()
