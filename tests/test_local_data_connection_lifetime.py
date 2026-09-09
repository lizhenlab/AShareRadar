from __future__ import annotations

from pathlib import Path
import sqlite3

import pytest

import app.services.user_data_portability as portability
from app.services.cache import SQLiteCache


class OperationAborted(BaseException):
    pass


@pytest.fixture
def local_database(tmp_path: Path) -> Path:
    path = tmp_path / "user-data.sqlite3"
    SQLiteCache(path)
    return path


@pytest.fixture
def tracked_connections(monkeypatch):
    opened = []
    real_connect = sqlite3.connect

    def connect(*args, **kwargs):
        connection = real_connect(*args, **kwargs)
        opened.append(connection)
        return connection

    monkeypatch.setattr(portability.sqlite3, "connect", connect)
    yield opened
    for connection in opened:
        connection.close()


def assert_connections_closed(connections):
    assert connections
    for connection in connections:
        with pytest.raises(sqlite3.ProgrammingError, match="closed"):
            connection.execute("SELECT 1")


@pytest.mark.parametrize("operation", ["export", "digest", "preview", "commit"])
def test_public_operations_close_connections_before_returning(
    local_database, tracked_connections, operation,
):
    bundle = portability.export_user_data(local_database)
    if operation == "export":
        assert bundle.row_counts["watchlist"] == 0
    elif operation == "digest":
        assert len(portability.user_data_state_digest(local_database)) == 64
    else:
        result = portability.import_user_data(
            local_database, bundle, dry_run=operation == "preview",
        )
        assert result.committed is (operation == "commit")
    assert_connections_closed(tracked_connections)


@pytest.mark.parametrize("operation", ["export", "digest", "preview", "commit"])
@pytest.mark.parametrize("stage", ["setup", "read"])
def test_public_operations_close_connections_when_database_access_fails(
    local_database, monkeypatch, operation, stage,
):
    bundle = portability.export_user_data(local_database)
    opened = []
    real_connect = sqlite3.connect

    class FailingConnection(sqlite3.Connection):
        def execute(self, sql, *args, **kwargs):
            if stage == "setup" and sql.startswith("PRAGMA busy_timeout"):
                raise sqlite3.OperationalError("injected setup failure")
            if stage == "read" and sql.startswith('SELECT * FROM "watchlist"'):
                raise sqlite3.OperationalError("injected read failure")
            return super().execute(sql, *args, **kwargs)

    def connect(*args, **kwargs):
        connection = real_connect(*args, **kwargs, factory=FailingConnection)
        opened.append(connection)
        return connection

    monkeypatch.setattr(portability.sqlite3, "connect", connect)
    try:
        with pytest.raises(sqlite3.OperationalError, match=f"injected {stage} failure"):
            if operation == "export":
                portability.export_user_data(local_database)
            elif operation == "digest":
                portability.user_data_state_digest(local_database)
            else:
                portability.import_user_data(local_database, bundle, dry_run=operation == "preview")
        assert_connections_closed(opened)
    finally:
        for connection in opened:
            connection.close()


@pytest.mark.parametrize("dry_run", [True, False])
def test_import_abort_rolls_back_and_closes_before_propagating(
    local_database, tracked_connections, dry_run,
):
    bundle = portability.export_user_data(local_database)
    aborted = OperationAborted("cancelled by owner")

    def abort_after_validation(_digest, _result):
        tracked_connections[-1].execute(
            """INSERT INTO watchlist (symbol, code, market, name, created_at, updated_at)
            VALUES ('600519.SH', '600519', 'SH', 'synthetic',
                    '2026-09-08T00:00:00.000000Z', '2026-09-08T00:00:00.000000Z')""",
        )
        raise aborted

    with pytest.raises(OperationAborted) as caught:
        portability.import_user_data(
            local_database, bundle, dry_run=dry_run,
            on_validated_state=abort_after_validation,
        )
    assert caught.value is aborted
    assert_connections_closed(tracked_connections)
    with portability._connect(local_database) as connection:
        connection.execute("BEGIN IMMEDIATE")
        assert connection.execute("SELECT COUNT(*) FROM watchlist").fetchone()[0] == 0
        connection.rollback()
