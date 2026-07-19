from __future__ import annotations

import sqlite3
import threading
from contextlib import AbstractContextManager
from typing import Callable


class MarketScanRepositoryContext:
    """Type contract supplied at runtime by SQLiteRepository in the public facade."""

    _lock: threading.RLock
    _connect: Callable[[], AbstractContextManager[sqlite3.Connection]]


__all__ = ["MarketScanRepositoryContext"]
