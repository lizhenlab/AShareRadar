"""The API acknowledgement survives a later list-read failure."""

from __future__ import annotations

from pathlib import Path
from typing import NoReturn

import pytest

from app.models.user_data import StockNoteInput
from app.services.cache import SQLiteCache
from tests.factories import make_quote
from tests.test_api_notes_routes import _client, _DataHubStub


@pytest.mark.parametrize("operation", ["add", "update", "remove"])
def test_note_acknowledgement_is_durable_before_failed_readback(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, operation: str,
) -> None:
    path = tmp_path / "notes.sqlite3"
    cache = SQLiteCache(path)
    quote = make_quote(timestamp="2026-07-17 15:00:00")
    initial = cache.create_stock_note(quote, StockNoteInput(symbol="600519", content="原笔记"))
    client = _client(_DataHubStub(cache=cache, quote=quote))
    if operation == "add":
        response = client.post("/api/stock/notes", json={"symbol": "600519", "content": "新增笔记"})
    elif operation == "update":
        response = client.patch(f"/api/stock/notes/{initial.id}", json={"expected_revision": initial.revision, "content": "修改笔记"})
    else:
        response = client.delete(f"/api/stock/notes/{initial.id}", params={"expected_revision": initial.revision})
    assert response.status_code == 200

    def failed_read(*_args: object, **_kwargs: object) -> NoReturn:
        raise RuntimeError("后续列表读取暂不可用")

    monkeypatch.setattr(cache, "stock_notes", failed_read)
    readback = client.get("/api/stock/notes?symbol=600519")
    assert readback.status_code == 503
    reopened = SQLiteCache(path).stock_notes("600519")
    if operation == "add":
        assert {item.content for item in reopened} == {"原笔记", "新增笔记"}
        assert next(item.id for item in reopened if item.content == "新增笔记") == response.json()["id"]
    elif operation == "update":
        assert [(item.id, item.content) for item in reopened] == [(initial.id, "修改笔记")]
    else:
        assert reopened == [] and response.json() == {"ok": True, "removed": True}
