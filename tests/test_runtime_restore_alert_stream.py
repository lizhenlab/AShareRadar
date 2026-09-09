from __future__ import annotations

from contextlib import closing
import hashlib
from pathlib import Path
import sqlite3

import pytest

from app.config import Settings
from app.db.alert_stream import (
    AlertStreamState,
    read_alert_stream_state,
    rotate_alert_stream_state,
)
import app.db.alert_stream_restore as stream_restore
from app.models.user_data import AlertRuleInput
from app.services.cache import SQLiteCache
import app.services.runtime_backup as backup_module
from app.services.runtime_backup import create_runtime_backup, restore_runtime_backup, verify_runtime_backup
from tests.factories import make_quote


def _cache(path: Path) -> SQLiteCache:
    return SQLiteCache(path, settings=Settings(cache_path=path, scheduler_enabled=False))


def _stream(path: Path) -> AlertStreamState:
    with closing(sqlite3.connect(path)) as conn:
        return read_alert_stream_state(conn)


def _append_events(cache: SQLiteCache, count: int) -> None:
    quote = make_quote()
    symbol = f"{quote.code}.{quote.market}"
    rule = cache.create_alert_rule(quote, AlertRuleInput(symbol=symbol, condition_type="price_above", threshold=1))
    with closing(sqlite3.connect(cache.path)) as conn, conn:
        conn.executemany(
            """
            INSERT INTO alert_event (
                rule_id, symbol, code, market, stock_name, name, condition_type, event_type,
                message, price, change_pct, threshold, created_at
            ) VALUES (?, ?, ?, ?, ?, 'synthetic alert', 'price_above', '触发', ?, ?, ?, 1, ?)
            """,
            [
                (rule.id, symbol, quote.code, quote.market, quote.name, f"synthetic {index}",
                 quote.price, quote.change_pct, "2026-09-09T01:00:00.000000Z")
                for index in range(count)
            ],
        )


def _backup_scenario(tmp_path: Path):
    cache = _cache(tmp_path / "runtime.sqlite3")
    _append_events(cache, 3)
    with closing(sqlite3.connect(cache.path)) as conn, conn:
        conn.execute("BEGIN IMMEDIATE")
        source_state = rotate_alert_stream_state(conn)
    backup = create_runtime_backup(cache.path, tmp_path / "backup")
    _append_events(cache, 2)
    with closing(sqlite3.connect(cache.path)) as conn, conn:
        conn.execute("BEGIN IMMEDIATE")
        current_state = rotate_alert_stream_state(conn)
    return cache, backup, source_state, current_state


def test_restore_rotates_stream_with_restored_data_without_modifying_backup_bytes(tmp_path, monkeypatch) -> None:
    cache, backup, source_state, current_state = _backup_scenario(tmp_path)
    database_bytes = Path(backup.database_path).read_bytes()
    manifest_bytes = Path(backup.manifest_path).read_bytes()
    original_rotate = backup_module.rotate_private_restore_stream
    staging_directories = []

    def require_private_staging(path):
        assert path.parent.stat().st_mode & 0o777 == 0o700
        staging_directories.append(path.parent)
        return original_rotate(path)

    monkeypatch.setattr(backup_module, "rotate_private_restore_stream", require_private_staging)

    result = restore_runtime_backup(Path(backup.backup_path), cache.path, service_stopped=True)

    state = _stream(cache.path)
    assert result.restored is True
    assert state.stream_id not in {source_state.stream_id, current_state.stream_id}
    assert state.baseline_event_id == 3
    with closing(sqlite3.connect(cache.path)) as conn:
        assert conn.execute("SELECT COUNT(*) FROM alert_event").fetchone()[0] == 3
    assert Path(backup.database_path).read_bytes() == database_bytes
    assert Path(backup.manifest_path).read_bytes() == manifest_bytes
    assert verify_runtime_backup(Path(backup.backup_path)).ok is True
    assert len(staging_directories) == 1
    assert not staging_directories[0].exists()
    reopened = _cache(cache.path)
    assert _stream(reopened.path) == state
    _append_events(reopened, 1)
    assert _stream(reopened.path) == state


def test_restore_legacy_backup_adds_only_new_stream_metadata(tmp_path) -> None:
    cache = _cache(tmp_path / "runtime.sqlite3")
    _append_events(cache, 4)
    with closing(sqlite3.connect(cache.path)) as conn, conn:
        conn.execute("DROP TABLE alert_stream_state")
        conn.execute("DELETE FROM alert_event")
    backup = create_runtime_backup(cache.path, tmp_path / "legacy-backup")
    original = Path(backup.database_path).read_bytes()

    result = restore_runtime_backup(Path(backup.backup_path), tmp_path / "new.sqlite3", service_stopped=True)

    assert result.restored is True
    assert _stream(tmp_path / "new.sqlite3").baseline_event_id == 4
    assert Path(backup.database_path).read_bytes() == original
    assert verify_runtime_backup(Path(backup.backup_path)).ok is True
    with closing(sqlite3.connect(tmp_path / "new.sqlite3")) as conn:
        assert conn.execute("SELECT COUNT(*) FROM alert_event").fetchone()[0] == 0


def test_restore_post_replace_failure_restores_exact_rollback_and_prior_stream(tmp_path, monkeypatch) -> None:
    cache, backup, _source_state, current_state = _backup_scenario(tmp_path)
    original_verify = backup_module._verify_database_against_manifest
    failed = False

    def fail_installed_once(path, manifest):
        nonlocal failed
        if path == cache.path and not failed:
            failed = True
            raise backup_module.RuntimeBackupError("injected installed verification failure")
        return original_verify(path, manifest)

    monkeypatch.setattr(backup_module, "_verify_database_against_manifest", fail_installed_once)
    rollback_path = tmp_path / "rollback"
    with pytest.raises(backup_module.RuntimeBackupError, match="injected installed"):
        restore_runtime_backup(Path(backup.backup_path), cache.path, service_stopped=True, rollback_destination=rollback_path)
    rollback = verify_runtime_backup(rollback_path)
    assert _stream(cache.path) == current_state
    assert cache.path.read_bytes() == Path(rollback.database_path).read_bytes()
    assert hashlib.sha256(cache.path.read_bytes()).hexdigest() == rollback.manifest.sha256
    with closing(sqlite3.connect(cache.path)) as conn:
        assert conn.execute("SELECT COUNT(*) FROM alert_event").fetchone()[0] == 5


@pytest.mark.parametrize("statement", [
    "UPDATE alert_event SET message = 'tampered' WHERE id = 1",
    "CREATE TABLE unrelated_mutation (id INTEGER)",
    "PRAGMA user_version = 123",
    "ATTACH DATABASE ':memory:' AS injected",
])
@pytest.mark.parametrize("legacy", [False, True])
def test_restore_metadata_transform_cannot_resign_unrelated_changes(tmp_path, monkeypatch, statement, legacy) -> None:
    cache, backup, _source_state, current_state = _backup_scenario(tmp_path)
    if legacy:
        with closing(sqlite3.connect(cache.path)) as conn, conn:
            conn.execute("DROP TABLE alert_stream_state")
        backup = create_runtime_backup(cache.path, tmp_path / "legacy-backup")
        _cache(cache.path)
        current_state = _stream(cache.path)
    original_rotate = stream_restore.rotate_alert_stream_state

    def attempt_unrelated_write(conn):
        state = original_rotate(conn)
        conn.execute(statement)
        return state

    monkeypatch.setattr(stream_restore, "rotate_alert_stream_state", attempt_unrelated_write)
    with pytest.raises(sqlite3.DatabaseError, match="not authorized"):
        restore_runtime_backup(Path(backup.backup_path), cache.path, service_stopped=True)
    assert _stream(cache.path) == current_state
    with closing(sqlite3.connect(cache.path)) as conn:
        assert conn.execute("SELECT COUNT(*) FROM alert_event").fetchone()[0] == 5
        assert conn.execute("SELECT message FROM alert_event WHERE id = 1").fetchone()[0] == "synthetic 0"


@pytest.mark.parametrize("mismatch", ["return_value", "unchanged_identity"])
def test_restore_requires_independent_readback_of_rotated_identity(tmp_path, monkeypatch, mismatch) -> None:
    cache, backup, _source_state, current_state = _backup_scenario(tmp_path)
    original_rotate = backup_module.rotate_private_restore_stream

    def incorrect_rotation(path):
        if mismatch == "unchanged_identity":
            return _stream(path)
        actual = original_rotate(path)
        return AlertStreamState("f" * 32, actual.baseline_event_id)

    monkeypatch.setattr(backup_module, "rotate_private_restore_stream", incorrect_rotation)
    with pytest.raises(backup_module.RuntimeBackupError, match="通知流"):
        restore_runtime_backup(Path(backup.backup_path), cache.path, service_stopped=True)
    assert _stream(cache.path) == current_state


@pytest.mark.parametrize("statement", [
    "PRAGMA user_version = 123",
    "CREATE TABLE unexpected_table (id INTEGER)",
    "DELETE FROM alert_event WHERE id = 1",
])
def test_derived_manifest_does_not_adopt_unexpected_database_facts(tmp_path, monkeypatch, statement) -> None:
    cache, backup, _source_state, current_state = _backup_scenario(tmp_path)
    original_rotate = backup_module.rotate_private_restore_stream

    def change_after_authorizer(path):
        state = original_rotate(path)
        # Simulate a broken staging step outside the authorized metadata transaction.
        with closing(sqlite3.connect(path)) as conn, conn:
            conn.execute(statement)
        return state

    monkeypatch.setattr(backup_module, "rotate_private_restore_stream", change_after_authorizer)
    with pytest.raises(backup_module.RuntimeBackupError, match="manifest"):
        restore_runtime_backup(Path(backup.backup_path), cache.path, service_stopped=True)
    assert _stream(cache.path) == current_state
    with closing(sqlite3.connect(cache.path)) as conn:
        assert conn.execute("SELECT COUNT(*) FROM alert_event").fetchone()[0] == 5
        assert conn.execute("SELECT message FROM alert_event WHERE id = 1").fetchone()[0] == "synthetic 0"


def test_restore_rejects_damaged_source_before_stream_change(tmp_path) -> None:
    cache, backup, _source_state, current_state = _backup_scenario(tmp_path)
    with closing(sqlite3.connect(backup.database_path)) as conn, conn:
        conn.execute("UPDATE alert_event SET message = 'same row count changed value' WHERE id = 1")
    with pytest.raises(backup_module.RuntimeBackupError, match="SHA-256"):
        restore_runtime_backup(Path(backup.backup_path), cache.path, service_stopped=True)
    assert _stream(cache.path) == current_state
