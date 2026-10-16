"""Stable note history, including mutations between and during page reads."""

from pathlib import Path

import pytest

from app.models.user_data import StockNoteInput, StockNoteUpdate
from app.repositories import notes
from app.services.cache import SQLiteCache
from tests.factories import make_quote
from tests.test_api_notes_routes import _client, _DataHubStub


def _setup(tmp_path: Path, count: int = 5):
    cache = SQLiteCache(tmp_path / "notes.sqlite3")
    quote = make_quote()
    items = [cache.create_stock_note(quote, StockNoteInput(
        symbol="600519", content=f"观察 {index}", visible=index % 2 == 0,
        trade_date=f"2026-09-{index % 28 + 1:02d}",
    )) for index in range(count)]
    return cache, _client(_DataHubStub(cache=cache, quote=quote)), items


def test_history_reaches_every_note_once_in_the_existing_order(tmp_path: Path) -> None:
    cache, client, _items = _setup(tmp_path, 123)
    gathered = []
    snapshot = None
    for page in range(1, 4):
        params = {"symbol": "600519", "page": page, "page_size": 50}
        if snapshot:
            params["expected_snapshot"] = snapshot
        response = client.get("/api/stock/notes/page", params=params)
        assert response.status_code == 200
        assert response.headers["cache-control"] == "no-store"
        result = response.json()
        assert result["page"] == page and result["total"] == 123 and result["page_count"] == 3
        assert result["symbol"] == "600519.SH"
        assert len(result["items"]) == (50 if page < 3 else 23)
        snapshot = result["snapshot"]
        gathered.extend(result["items"])
    assert gathered == [item.model_dump(mode="json") for item in cache.stock_notes("600519", limit=500)]
    assert len({item["id"] for item in gathered}) == 123
    assert any(not item["visible"] for item in gathered)


@pytest.mark.parametrize("mutation", ["insert", "delete", "content", "trade_date", "visible"])
def test_changed_history_rejects_the_next_page_instead_of_skipping_records(tmp_path: Path, mutation: str) -> None:
    cache, client, items = _setup(tmp_path)
    first = cache.stock_note_page("600519", page_size=2)
    target = items[0]  # Outside page 1: even a change on a later page invalidates the collection.
    if mutation == "insert":
        cache.create_stock_note(make_quote(), StockNoteInput(symbol="600519", content="新笔记"))
    elif mutation == "delete":
        cache.delete_stock_note(target.id, expected_revision=target.revision)
    else:
        value = {"content": "已更新", "trade_date": "2026-10-15", "visible": False}[mutation]
        cache.update_stock_note(target.id, StockNoteUpdate.model_validate({"expected_revision": target.revision, mutation: value}))
    response = client.get("/api/stock/notes/page", params={
        "symbol": "600519", "page": 2, "page_size": 2, "expected_snapshot": first.snapshot,
    })
    assert response.status_code == 409
    assert response.headers["cache-control"] == "no-store"
    assert "刷新" in response.json()["detail"]
    refreshed = cache.stock_note_page("600519", page_size=2)
    assert refreshed.snapshot != first.snapshot


def test_history_count_items_and_digest_use_one_sqlite_snapshot(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    cache, _client_instance, _items = _setup(tmp_path)
    writer = SQLiteCache(cache.path)
    before = cache.stock_note_page("600519", page_size=2)
    original = notes.row_to_stock_note
    inserted = False

    def insert_during_mapping(row):
        nonlocal inserted
        if not inserted:
            inserted = True
            writer.create_stock_note(make_quote(), StockNoteInput(symbol="600519", content="并发新笔记"))
        return original(row)

    monkeypatch.setattr(notes, "row_to_stock_note", insert_during_mapping)
    during = cache.stock_note_page("600519", page_size=2)
    assert inserted and during == before
    after = cache.stock_note_page("600519", page_size=2)
    assert after.total == before.total + 1 and after.snapshot != before.snapshot


def test_empty_and_out_of_range_pages_are_explicit(tmp_path: Path) -> None:
    cache, client, _items = _setup(tmp_path, 0)
    empty = client.get("/api/stock/notes/page?symbol=600519").json()
    assert empty["items"] == [] and empty["total"] == 0 and empty["page_count"] == 0
    cache.create_stock_note(make_quote(), StockNoteInput(symbol="600519", content="唯一笔记"))
    outside = client.get("/api/stock/notes/page?symbol=600519&page=3&page_size=1").json()
    assert outside["page"] == 3 and outside["items"] == []
    assert outside["total"] == 1 and outside["page_count"] == 1


def test_history_identity_binds_symbol_page_size_and_all_stored_values(tmp_path: Path) -> None:
    cache, _client_instance, items = _setup(tmp_path)
    before = cache.stock_note_page("600519", page_size=2)
    assert cache.stock_note_page("SH600519", page_size=2).snapshot == before.snapshot
    assert cache.stock_note_page("600519", page_size=3).snapshot != before.snapshot
    with cache.note_repo._connect() as conn:
        conn.execute("UPDATE stock_note SET color = ? WHERE id = ?", ("custom", items[0].id))
    assert cache.stock_note_page("600519", page_size=2).snapshot != before.snapshot
    assert cache.stock_note_page("600001").snapshot != cache.stock_note_page("600002").snapshot


@pytest.mark.parametrize("query", ["page=0", "page_size=0", "page_size=101", "expected_snapshot=invalid", "symbol=bad"])
def test_history_rejects_invalid_query(tmp_path: Path, query: str) -> None:
    _cache, client, _items = _setup(tmp_path, 0)
    assert client.get(f"/api/stock/notes/page?{query}").status_code in {400, 422}


@pytest.mark.parametrize("page,page_size", [(0, 50), (1, 0), (1, 101)])
def test_repository_rejects_invalid_page_bounds(tmp_path: Path, page: int, page_size: int) -> None:
    cache, _client_instance, _items = _setup(tmp_path, 0)
    with pytest.raises(ValueError, match="分页"):
        cache.stock_note_page("600519", page=page, page_size=page_size)
