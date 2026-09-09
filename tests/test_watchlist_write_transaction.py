"""Self-owned watchlist acknowledgements and preserved fields are transactional."""

from __future__ import annotations

from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path
import sqlite3
import threading
from types import SimpleNamespace
from typing import NoReturn

import pytest

from app.config import Settings
from app.db.connection import SQLiteConnectionFactory
from app.db.schema import initialize_schema
from app.models.user_data import WatchlistUpdate
from app.repositories import watchlist
from app.repositories.watchlist import WatchlistRepository
from tests.factories import make_quote
from tests.test_api_watchlist_routes import _client, _DataHubStub


@pytest.fixture
def repository(tmp_path: Path) -> WatchlistRepository:
    path = tmp_path / "watchlist.sqlite3"
    with SQLiteConnectionFactory(path).connect() as connection:
        initialize_schema(connection)
    return WatchlistRepository(path, threading.RLock(), settings=Settings(cache_path=path))


@pytest.mark.parametrize("operation", ["save", "update", "mark_viewed", "increment"])
@pytest.mark.parametrize("fault", ["select", "mapping", "commit"])
def test_failed_watchlist_acknowledgement_rolls_back_before_retry(
    repository: WatchlistRepository, monkeypatch: pytest.MonkeyPatch, operation: str, fault: str,
) -> None:
    repository.save_item(make_quote(), note="original")
    before = _stored_rows(repository)
    with monkeypatch.context() as scoped:
        _install_storage_fault(scoped, fault)
        with pytest.raises((RuntimeError, sqlite3.DatabaseError)):
            _write(repository, operation)
    assert _stored_rows(repository) == before

    result = _write(repository, operation)
    assert result is not None
    reopened = WatchlistRepository(repository._path, threading.RLock(), settings=repository.settings)
    assert reopened.item("600519") == result
    if operation in {"save", "update"}:
        assert result.note == "replacement"
    elif operation == "increment":
        assert result.unread_change_count == 1
    else:
        assert result.last_viewed_at is not None


def test_watchlist_api_failure_does_not_persist_unacknowledged_edit(
    repository: WatchlistRepository, monkeypatch: pytest.MonkeyPatch,
) -> None:
    repository.save_item(make_quote(), note="original")
    cache = SimpleNamespace(update_watchlist_item=repository.update_item)
    client = _client(_DataHubStub(cache=cache))
    with monkeypatch.context() as scoped:
        _install_storage_fault(scoped, "select")
        response = client.patch("/api/watchlist/600519", json={"note": "replacement"})
    assert response.status_code == 503
    assert repository.item("600519").note == "original"
    retried = client.patch("/api/watchlist/600519", json={"note": "replacement"})
    assert retried.status_code == 200 and retried.json()["note"] == "replacement"


@pytest.mark.parametrize("fault", ["select", "mapping", "commit"])
def test_failed_first_save_does_not_leave_a_watchlist_entry(
    repository: WatchlistRepository, monkeypatch: pytest.MonkeyPatch, fault: str,
) -> None:
    with monkeypatch.context() as scoped:
        _install_storage_fault(scoped, fault)
        with pytest.raises((RuntimeError, sqlite3.DatabaseError)):
            repository.save_item(make_quote(), note="new entry")
    assert _stored_rows(repository) == []
    saved = repository.save_item(make_quote(), note="new entry")
    assert repository.items() == [saved]


def test_save_reserves_preserved_fields_before_reading_independent_database_writer(
    repository: WatchlistRepository, monkeypatch: pytest.MonkeyPatch,
) -> None:
    repository.save_item(make_quote(), note="original", pinned=False)
    original_read = watchlist._watchlist_row
    attempts = []

    def read_while_another_writer_attempts(connection, symbol):
        row = original_read(connection, symbol)
        with sqlite3.connect(repository._path, timeout=0) as independent:
            with pytest.raises(sqlite3.OperationalError, match="database is locked"):
                independent.execute("UPDATE watchlist SET pinned = 1 WHERE symbol = ?", (symbol,))
        attempts.append(symbol)
        return row

    with monkeypatch.context() as scoped:
        scoped.setattr(watchlist, "_watchlist_row", read_while_another_writer_attempts)
        first = repository.save_item(make_quote(), note="replacement")
    other = WatchlistRepository(repository._path, threading.RLock(), settings=repository.settings)
    second = other.update_item("600519", WatchlistUpdate(pinned=True))
    assert attempts == ["600519.SH"]
    assert first.note == "replacement" and first.pinned is False
    assert second.note == "replacement" and second.pinned is True


def test_empty_update_and_missing_mutations_keep_existing_contract(repository: WatchlistRepository) -> None:
    saved = repository.save_item(make_quote(), note="original")
    before = _stored_rows(repository)
    assert repository.update_item("600519", WatchlistUpdate()) == saved
    assert _stored_rows(repository) == before
    assert repository.update_item("000001", WatchlistUpdate(note="missing")) is None
    assert repository.mark_viewed("000001") is None
    assert repository.increment_unread_change_count("000001") is None


def _write(repository: WatchlistRepository, operation: str):
    if operation == "save":
        return repository.save_item(make_quote(), note="replacement")
    if operation == "update":
        return repository.update_item("600519", WatchlistUpdate(note="replacement"))
    if operation == "mark_viewed":
        return repository.mark_viewed("600519", clear_unread=False)
    return repository.increment_unread_change_count("600519")


def _stored_rows(repository: WatchlistRepository) -> list[tuple[object, ...]]:
    with sqlite3.connect(repository._path) as connection:
        return connection.execute("SELECT * FROM watchlist ORDER BY symbol").fetchall()


def _install_storage_fault(monkeypatch: pytest.MonkeyPatch, fault: str) -> None:
    if fault == "mapping":
        def fail_mapping(_row: sqlite3.Row) -> NoReturn:
            raise RuntimeError("injected watchlist acknowledgement mapping failure")

        monkeypatch.setattr(watchlist, "row_to_watchlist_item", fail_mapping)
        return
    original = SQLiteConnectionFactory.connect

    @contextmanager
    def failing_connect(factory: SQLiteConnectionFactory) -> Iterator[sqlite3.Connection]:
        with original(factory) as connection:
            written = False

            def authorize(action: int, argument: str | None, *_args: object) -> int:
                nonlocal written
                if action in {sqlite3.SQLITE_INSERT, sqlite3.SQLITE_UPDATE} and argument == "watchlist":
                    written = True
                denied = written and action == sqlite3.SQLITE_SELECT if fault == "select" else (
                    action == sqlite3.SQLITE_TRANSACTION and argument == "COMMIT"
                )
                return sqlite3.SQLITE_DENY if denied else sqlite3.SQLITE_OK

            connection.set_authorizer(authorize)
            yield connection

    monkeypatch.setattr(SQLiteConnectionFactory, "connect", failing_connect)
