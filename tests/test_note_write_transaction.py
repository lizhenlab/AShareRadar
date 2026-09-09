"""Note writes must roll back when their own readback or commit fails."""

from __future__ import annotations

import sqlite3
from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path
from typing import NoReturn

import pytest

from app.db.connection import SQLiteConnectionFactory
from app.models.user_data import StockNoteInput
from app.repositories import notes
from app.services.cache import SQLiteCache
from tests.factories import make_quote
from tests.test_api_notes_routes import _client, _DataHubStub


@pytest.mark.parametrize("operation", ["create", "update"])
@pytest.mark.parametrize("fault", ["select", "mapping", "commit"])
def test_note_write_failure_rolls_back_before_retry(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, operation: str, fault: str,
) -> None:
    path = tmp_path / "notes.sqlite3"
    cache = SQLiteCache(path)
    quote = make_quote(timestamp="2026-07-17 15:00:00")
    initial = cache.create_stock_note(quote, StockNoteInput(symbol="600519", content="原笔记")) if operation == "update" else None
    client = _client(_DataHubStub(cache=cache, quote=quote))
    before = _stored_rows(path)
    method = "PATCH" if initial else "POST"
    url = f"/api/stock/notes/{initial.id}" if initial else "/api/stock/notes"
    payload = {"expected_revision": initial.revision, "content": "保存的笔记"} if initial else {"symbol": "600519", "content": "保存的笔记"}

    with monkeypatch.context() as scoped:
        _install_storage_fault(scoped, fault)
        failed = client.request(method, url, json=payload)

    assert failed.status_code == 503
    assert _stored_rows(path) == before
    retried = client.request(method, url, json=payload)
    assert retried.status_code == 200
    reopened = SQLiteCache(path).stock_notes("600519")
    assert [(item.id, item.content) for item in reopened] == [(retried.json()["id"], "保存的笔记")]


def test_empty_note_patch_preserves_stored_state(tmp_path: Path) -> None:
    path = tmp_path / "notes.sqlite3"
    cache = SQLiteCache(path)
    quote = make_quote()
    note = cache.create_stock_note(quote, StockNoteInput(symbol="600519", content="原笔记"))
    client = _client(_DataHubStub(cache=cache, quote=quote))
    before = _stored_rows(path)

    response = client.patch(f"/api/stock/notes/{note.id}", json={"expected_revision": note.revision})

    assert response.status_code == 200
    assert response.json() == note.model_dump(mode="json")
    assert _stored_rows(path) == before


@pytest.mark.parametrize("payload", [{}, {"content": "不存在的笔记"}])
def test_note_patch_missing_record_remains_404(tmp_path: Path, payload: dict[str, str]) -> None:
    path = tmp_path / "notes.sqlite3"
    client = _client(_DataHubStub(cache=SQLiteCache(path), quote=make_quote()))

    response = client.patch("/api/stock/notes/999", json={"expected_revision": "a" * 64, **payload})

    assert response.status_code == 404
    assert response.json() == {"detail": "个股笔记不存在"}
    assert _stored_rows(path) == []


def _stored_rows(path: Path) -> list[tuple[object, ...]]:
    with sqlite3.connect(path) as conn:
        return conn.execute("SELECT * FROM stock_note ORDER BY id").fetchall()


def _install_storage_fault(monkeypatch: pytest.MonkeyPatch, fault: str) -> None:
    if fault == "mapping":
        def failed_mapping(_row: sqlite3.Row) -> NoReturn:
            raise RuntimeError("注入笔记返回模型构建失败")

        monkeypatch.setattr(notes, "row_to_stock_note", failed_mapping)
        return

    original = SQLiteConnectionFactory.connect

    @contextmanager
    def failing_connect(factory: SQLiteConnectionFactory) -> Iterator[sqlite3.Connection]:
        with original(factory) as conn:
            def authorize(action: int, arg1: str | None, *_args: object) -> int:
                denied = action == sqlite3.SQLITE_SELECT if fault == "select" else (
                    action == sqlite3.SQLITE_TRANSACTION and arg1 == "COMMIT"
                )
                return sqlite3.SQLITE_DENY if denied else sqlite3.SQLITE_OK

            conn.set_authorizer(authorize)
            yield conn

    monkeypatch.setattr(SQLiteConnectionFactory, "connect", failing_connect)
