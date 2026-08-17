from __future__ import annotations

import sqlite3
import threading
from contextlib import AbstractContextManager
from pathlib import Path
from typing import Callable


class MarketScanRepositoryContext:
    """Type contract supplied at runtime by SQLiteRepository in the public facade."""

    _lock: threading.RLock
    _connect: Callable[[], AbstractContextManager[sqlite3.Connection]]
    _read_snapshot: Callable[[], AbstractContextManager[sqlite3.Connection]]
    _run_started_monotonic: dict[int, float]
    _path: Path


__all__ = ["MarketScanRepositoryContext"]
