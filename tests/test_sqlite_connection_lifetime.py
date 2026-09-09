"""The shared SQLite connection owns setup, rollback and close on every exit."""

from __future__ import annotations

from pathlib import Path
import sqlite3

import pytest

from app.db.connection import SQLiteConnectionFactory


class OperationAborted(BaseException):
    pass


@pytest.mark.parametrize("stage", ["function", "busy_timeout", "foreign_keys"])
@pytest.mark.parametrize("exception_type", [sqlite3.OperationalError, OperationAborted])
def test_setup_failure_closes_connection_before_propagating(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, stage: str, exception_type: type[BaseException],
) -> None:
    original = sqlite3.connect
    opened = []
    failure = exception_type("injected setup failure")

    class SetupFailureConnection(sqlite3.Connection):
        def create_function(self, *args, **kwargs):
            if stage == "function":
                raise failure
            return super().create_function(*args, **kwargs)

        def execute(self, sql, *args, **kwargs):
            if sql.startswith(f"PRAGMA {stage}"):
                raise failure
            return super().execute(sql, *args, **kwargs)

    def connect(*args, **kwargs):
        connection = original(*args, **kwargs, factory=SetupFailureConnection)
        opened.append(connection)
        return connection

    monkeypatch.setattr(sqlite3, "connect", connect)
    try:
        with pytest.raises(exception_type) as caught:
            with SQLiteConnectionFactory(tmp_path / "setup.sqlite3").connect():
                pytest.fail("failed setup must never yield its connection")
        assert caught.value is failure
        assert len(opened) == 1
        with pytest.raises(sqlite3.ProgrammingError, match="closed"):
            opened[0].execute("SELECT 1")
    finally:
        for connection in opened:
            connection.close()


def test_aborted_write_rolls_back_and_closes_before_next_writer(tmp_path: Path) -> None:
    factory = SQLiteConnectionFactory(tmp_path / "abort.sqlite3")
    with factory.connect() as connection:
        connection.execute("CREATE TABLE state (value TEXT)")
    failure = OperationAborted("cancelled by owner")
    with pytest.raises(OperationAborted) as caught:
        with factory.connect() as connection:
            connection.execute("INSERT INTO state VALUES ('uncommitted')")
            raise failure
    assert caught.value is failure
    with pytest.raises(sqlite3.ProgrammingError, match="closed"):
        connection.execute("SELECT 1")
    with factory.connect() as next_connection:
        next_connection.execute("BEGIN IMMEDIATE")
        assert next_connection.execute("SELECT * FROM state").fetchall() == []
        next_connection.execute("INSERT INTO state VALUES ('committed')")
    with factory.connect() as reader:
        assert reader.execute("SELECT value FROM state").fetchone()[0] == "committed"
