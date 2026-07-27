from __future__ import annotations

from pathlib import Path
import sqlite3
from types import SimpleNamespace
from unittest.mock import patch

import pytest

from app.config import Settings
from app.db.schema import initialize_schema
from app.db.schema_migrations import AUDIT_TIMESTAMP_UTC_MIGRATION
from app.services.cache import SQLiteCache
from app.services.instance_guard import FileInstanceGuard
from app.services.runtime_coordinator import RUNTIME_LEADER_LOCK_SUFFIX


def _create_legacy_database(path: Path) -> None:
    with sqlite3.connect(path) as connection:
        initialize_schema(connection)
        connection.execute(
            "DELETE FROM schema_migration WHERE name = ?",
            (AUDIT_TIMESTAMP_UTC_MIGRATION,),
        )
        connection.execute(
            """
            INSERT INTO task_run (task_name, status, started_at)
            VALUES ('legacy', 'running', '2026-07-24 09:30:00')
            """
        )


def _legacy_value(path: Path) -> str:
    with sqlite3.connect(path) as connection:
        row = connection.execute(
            "SELECT started_at FROM task_run WHERE task_name = 'legacy'"
        ).fetchone()
    assert row is not None
    return str(row[0])


def test_existing_legacy_database_requires_explicit_settings(tmp_path: Path) -> None:
    path = tmp_path / "legacy.sqlite3"
    _create_legacy_database(path)

    with pytest.raises(ValueError, match="显式 Settings"):
        SQLiteCache(path)

    assert _legacy_value(path) == "2026-07-24 09:30:00"
    SQLiteCache(
        settings=Settings(
            cache_path=path,
            legacy_audit_timezone="America/Los_Angeles",
        )
    )
    assert _legacy_value(path) == "2026-07-24T16:30:00.000000Z"


def test_audit_migration_refuses_running_runtime(tmp_path: Path) -> None:
    path = tmp_path / "legacy.sqlite3"
    _create_legacy_database(path)
    guard = FileInstanceGuard(Path(f"{path}{RUNTIME_LEADER_LOCK_SUFFIX}"))
    assert guard.acquire()
    try:
        with pytest.raises(RuntimeError, match="完全停止服务"):
            SQLiteCache(settings=Settings(cache_path=path))
    finally:
        guard.release()

    assert _legacy_value(path) == "2026-07-24 09:30:00"


def test_audit_migration_checks_free_disk_space(tmp_path: Path) -> None:
    path = tmp_path / "legacy.sqlite3"
    _create_legacy_database(path)

    with patch(
        "app.services.cache.shutil.disk_usage",
        return_value=SimpleNamespace(free=0),
    ):
        with pytest.raises(RuntimeError, match="磁盘空间不足"):
            SQLiteCache(settings=Settings(cache_path=path))

    assert _legacy_value(path) == "2026-07-24 09:30:00"
