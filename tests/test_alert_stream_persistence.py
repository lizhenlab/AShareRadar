from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor
from contextlib import closing
import sqlite3
from threading import Event

import pytest

from app.config import Settings
import app.db.alert_stream as stream_module
from app.db.alert_stream import (
    AlertStreamStateError,
    alert_event_high_water,
    ensure_alert_stream_state,
    read_alert_stream_state,
    rotate_alert_stream_state,
)
from app.services.cache import SQLiteCache
from app.services.runtime_backup import RuntimeBackupError, create_runtime_backup
from tests.test_runtime_restore_alert_stream import _append_events, _stream


def test_schema_initializes_stream_once_and_ordinary_reopen_preserves_it(tmp_path) -> None:
    path = tmp_path / "runtime.sqlite3"
    cache = SQLiteCache(path, settings=Settings(cache_path=path, scheduler_enabled=False))
    first = _stream(path)
    assert first.baseline_event_id == 0
    _append_events(cache, 3)
    with closing(sqlite3.connect(path)) as conn, conn:
        conn.execute("DELETE FROM alert_event")
    SQLiteCache(path, settings=cache.settings)
    assert _stream(path) == first
    with closing(sqlite3.connect(path)) as conn:
        assert alert_event_high_water(conn) == 3


def test_legacy_initialization_uses_retained_sequence_when_history_is_empty(tmp_path) -> None:
    path = tmp_path / "runtime.sqlite3"
    cache = SQLiteCache(path, settings=Settings(cache_path=path, scheduler_enabled=False))
    _append_events(cache, 4)
    with closing(sqlite3.connect(path)) as conn, conn:
        conn.execute("DROP TABLE alert_stream_state")
        conn.execute("DELETE FROM alert_event")
    SQLiteCache(path, settings=cache.settings)
    assert _stream(path).baseline_event_id == 4
    _append_events(cache, 1)
    with closing(sqlite3.connect(path)) as conn:
        assert conn.execute("SELECT id FROM alert_event").fetchone()[0] == 5
        assert alert_event_high_water(conn) == 5


@pytest.mark.parametrize("statement", [
    "DELETE FROM alert_stream_state",
    "UPDATE alert_stream_state SET singleton = 2",
    "INSERT INTO alert_stream_state VALUES (2, 'aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa', 0)",
    "UPDATE alert_stream_state SET stream_id = 'invalid'",
    "UPDATE alert_stream_state SET stream_id = 'AAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAA'",
    "UPDATE alert_stream_state SET stream_id = X'6161'",
    "UPDATE alert_stream_state SET baseline_event_id = -1",
    "UPDATE alert_stream_state SET baseline_event_id = 0.5",
    "UPDATE alert_stream_state SET baseline_event_id = 1",
    "CREATE INDEX unexpected_stream_index ON alert_stream_state(stream_id)",
    "CREATE TRIGGER unexpected_stream_trigger AFTER UPDATE ON alert_stream_state BEGIN SELECT 1; END",
])
def test_corrupt_existing_stream_is_rejected_without_reinitialization(tmp_path, statement) -> None:
    path = tmp_path / "runtime.sqlite3"
    settings = Settings(cache_path=path, scheduler_enabled=False)
    SQLiteCache(path, settings=settings)
    with closing(sqlite3.connect(path)) as conn, conn:
        conn.execute("PRAGMA ignore_check_constraints = ON")
        conn.execute(statement)
    with closing(sqlite3.connect(path)) as conn:
        before = conn.execute("SELECT * FROM alert_stream_state").fetchall()
        with pytest.raises(AlertStreamStateError):
            read_alert_stream_state(conn)
        with pytest.raises(AlertStreamStateError):
            ensure_alert_stream_state(conn)
        assert conn.execute("SELECT * FROM alert_stream_state").fetchall() == before
    with pytest.raises(AlertStreamStateError):
        SQLiteCache(path, settings=settings)
    with pytest.raises(RuntimeBackupError):
        create_runtime_backup(path, tmp_path / "rejected-backup")
    assert not (tmp_path / "rejected-backup").exists()


def test_metadata_table_with_weaker_declared_schema_is_rejected() -> None:
    with closing(sqlite3.connect(":memory:")) as conn:
        conn.execute("CREATE TABLE alert_stream_state (singleton INTEGER, stream_id TEXT, baseline_event_id INTEGER)")
        conn.execute("INSERT INTO alert_stream_state VALUES (1, 'aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa', 0)")
        with pytest.raises(AlertStreamStateError, match="结构"):
            ensure_alert_stream_state(conn)


def test_high_water_prefers_sequence_and_rejects_malformed_sequence() -> None:
    with closing(sqlite3.connect(":memory:")) as conn:
        conn.execute("CREATE TABLE alert_event (id INTEGER PRIMARY KEY AUTOINCREMENT)")
        conn.execute("INSERT INTO alert_event(id) VALUES (50)")
        conn.execute("DELETE FROM alert_event")
        assert alert_event_high_water(conn) == 50
        conn.execute("UPDATE sqlite_sequence SET seq = 'bad' WHERE name = 'alert_event'")
        with pytest.raises(AlertStreamStateError, match="高水位"):
            alert_event_high_water(conn)


def test_rotation_requires_an_explicit_transaction_and_rollback_keeps_identity() -> None:
    with closing(sqlite3.connect(":memory:")) as conn:
        original = ensure_alert_stream_state(conn)
        conn.commit()
        with pytest.raises(AlertStreamStateError, match="显式"):
            rotate_alert_stream_state(conn)
        conn.execute("BEGIN IMMEDIATE")
        rotated = rotate_alert_stream_state(conn)
        assert rotated.stream_id != original.stream_id
        conn.rollback()
        assert read_alert_stream_state(conn) == original


def test_two_initializers_share_one_committed_stream(tmp_path, monkeypatch) -> None:
    path = tmp_path / "legacy.sqlite3"
    with closing(sqlite3.connect(path)):
        pass
    entered, release, second_started, second_done = Event(), Event(), Event(), Event()
    original = stream_module._new_stream_id
    calls = []

    def paused_identity(previous=None):
        calls.append(True)
        entered.set()
        assert release.wait(timeout=5)
        return original(previous)

    def initialize(second=False):
        if second:
            second_started.set()
        with closing(sqlite3.connect(path, timeout=5)) as conn, conn:
            state = ensure_alert_stream_state(conn)
        if second:
            second_done.set()
        return state

    monkeypatch.setattr(stream_module, "_new_stream_id", paused_identity)
    with ThreadPoolExecutor(max_workers=2) as workers:
        first = workers.submit(initialize)
        try:
            assert entered.wait(timeout=5)
            second = workers.submit(initialize, True)
            assert second_started.wait(timeout=1)
            assert not second_done.wait(timeout=0.1)
        finally:
            release.set()
        assert first.result(timeout=5) == second.result(timeout=5)
    assert calls == [True]
