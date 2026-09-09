"""Explicit note annotations can use verified local identity without quote IO."""
from __future__ import annotations

from pathlib import Path
import sqlite3
from unittest.mock import patch

import pytest

from app.config import Settings
from app.models.market import Quote, StockInfo
from app.services.cache import SQLiteCache
from app.utils.audit_time import audit_now_text
from tests.factories import make_quote
from tests.test_api_notes_routes import _client


class NoteDataHub:
    def __init__(self, cache: SQLiteCache, quote: Quote | None = None):
        self.cache = cache
        self.value = quote
        self.quote_calls: list[str] = []

    async def quote(self, symbol: str) -> Quote:
        self.quote_calls.append(symbol)
        if self.value is None:
            raise RuntimeError("synthetic quote unavailable")
        return self.value


def _hub(tmp_path: Path, *, cached: bool = True, quote: Quote | None = None) -> NoteDataHub:
    with patch.dict("os.environ", {}, clear=True), patch("app.config_settings._SHELL_ENV_VALUES", {}):
        settings = Settings(cache_path=tmp_path / "notes.sqlite3", llm_enabled=False, llm_api_key=None)
    cache = SQLiteCache(settings=settings)
    if cached:
        cache.save_stock_pool([StockInfo(symbol="600519.SH", code="600519", market="SH",
            name="缓存茅台名称", industry="白酒", source="合成股票主数据", updated_at=audit_now_text())])
    return NoteDataHub(cache, quote)


def _explicit(**overrides):
    return {"symbol": "600519", "content": "离线记录", "price": 1288, "trade_date": "2026-09-08", **overrides}


@pytest.mark.parametrize("symbol", ["600519", "sh600519", "600519.SH"])
@pytest.mark.parametrize("trade_date,normalized", [("2026-09-08", "2026-09-08"), ("2026/09/08 13:20:00", "2026-09-08 13:20:00")])
def test_explicit_local_note_roundtrip_never_requests_a_quote(tmp_path, symbol, trade_date, normalized):
    hub = _hub(tmp_path)
    response = _client(hub).post("/api/stock/notes", json=_explicit(symbol=symbol, trade_date=trade_date))
    assert response.status_code == 200, response.text
    note = response.json()
    assert (note["symbol"], note["code"], note["market"], note["name"]) == ("600519.SH", "600519", "SH", "缓存茅台名称")
    assert note["price"] == 1288 and note["trade_date"] == normalized
    assert hub.quote_calls == []
    assert hub.cache.stock_notes("600519")[0].model_dump(mode="json") == note


@pytest.mark.parametrize("extra,expected_price,expected_date", [
    ({}, 1300, "2026-05-13 10:15:00"),
    ({"price": 1288}, 1288, "2026-05-13 10:15:00"),
    ({"trade_date": "2026-09-08"}, 1300, "2026-09-08"),
    ({"price": 1288, "trade_date": "   "}, 1288, "2026-05-13 10:15:00"),
])
def test_missing_note_defaults_still_come_from_original_quote_path(tmp_path, extra, expected_price, expected_date):
    hub = _hub(tmp_path, quote=make_quote(timestamp="2026-05-13 10:15:00"))
    response = _client(hub).post("/api/stock/notes", json={"symbol": "600519", "content": "原默认行为", **extra})
    assert response.status_code == 200, response.text
    assert response.json()["price"] == expected_price and response.json()["trade_date"] == expected_date
    assert response.json()["name"] == "贵州茅台"
    assert hub.quote_calls == ["600519"]


@pytest.mark.parametrize("extra", [{}, {"price": 1288}, {"trade_date": "2026-09-08"}, {"price": 1288, "trade_date": "  "}])
def test_missing_defaults_without_quote_remain_explicit_failure(tmp_path, extra):
    hub = _hub(tmp_path)
    response = _client(hub).post("/api/stock/notes", json={"symbol": "600519", "content": "不能虚构默认行情", **extra})
    assert response.status_code == 503 and hub.quote_calls == ["600519"]
    assert hub.cache.stock_notes("600519") == []


@pytest.mark.parametrize("field,value", [
    ("code", "600036"), ("market", "SZ"), ("symbol", "600519.SZ"),
    ("name", "   "), ("name", "未知"), ("name", "NaN"), ("source", ""),
])
def test_invalid_local_stock_metadata_never_authorizes_an_offline_note(tmp_path, field, value):
    hub = _hub(tmp_path)
    with sqlite3.connect(hub.cache.path) as conn:
        conn.execute(f"UPDATE stock_master SET {field} = ?", (value,))
    response = _client(hub).post("/api/stock/notes", json=_explicit())
    assert response.status_code == 503 and hub.quote_calls == ["600519"]
    assert hub.cache.stock_notes("600519") == []


def test_matching_name_for_another_code_does_not_confirm_stock_identity(tmp_path):
    hub = _hub(tmp_path, cached=False)
    hub.cache.save_stock_pool([StockInfo(symbol="600036.SH", code="600036", market="SH", name="600519",
        source="合成名称恰好包含代码", updated_at=audit_now_text())])
    response = _client(hub).post("/api/stock/notes", json=_explicit())
    assert response.status_code == 503 and hub.quote_calls == ["600519"]
    assert hub.cache.stock_notes("600519") == []


def test_conflicting_local_aliases_do_not_pick_an_identity_by_row_order(tmp_path):
    hub = _hub(tmp_path)
    with sqlite3.connect(hub.cache.path) as conn:
        conn.execute("INSERT INTO stock_master SELECT 'SH600519',code,market,'冲突名称',industry,list_date,source,updated_at FROM stock_master")
    response = _client(hub).post("/api/stock/notes", json=_explicit())
    assert response.status_code == 503 and hub.quote_calls == ["600519"]
    assert hub.cache.stock_notes("600519") == []


@pytest.mark.parametrize("live", [False, True])
def test_unknown_local_identity_preserves_original_quote_resolution(tmp_path, live):
    hub = _hub(tmp_path, cached=False, quote=make_quote() if live else None)
    response = _client(hub).post("/api/stock/notes", json=_explicit())
    assert response.status_code == (200 if live else 503)
    assert hub.quote_calls == ["600519"]
    if live:
        assert response.json()["name"] == "贵州茅台"
        assert response.json()["price"] == 1288 and response.json()["trade_date"] == "2026-09-08"
    else:
        assert hub.cache.stock_notes("600519") == []


@pytest.mark.parametrize("overrides,status", [
    ({"price": 0}, 400), ({"trade_date": "2026-02-31"}, 400),
    ({"content": "   "}, 400), ({"symbol": "bad"}, 400), ({"name": "用户伪造名称"}, 422),
])
def test_invalid_explicit_payload_is_not_written_or_repaired_from_a_quote(tmp_path, overrides, status):
    hub = _hub(tmp_path)
    response = _client(hub).post("/api/stock/notes", json=_explicit(**overrides))
    assert response.status_code == status, response.text
    assert hub.quote_calls == [] and hub.cache.stock_notes("600519") == []


def test_expired_local_metadata_preserves_quote_resolution(tmp_path):
    hub = _hub(tmp_path)
    with sqlite3.connect(hub.cache.path) as conn:
        conn.execute("UPDATE stock_master SET updated_at = '2000-01-01T00:00:00Z'")
    response = _client(hub).post("/api/stock/notes", json=_explicit())
    assert response.status_code == 503 and hub.quote_calls == ["600519"]
    assert hub.cache.stock_notes("600519") == []


def test_offline_note_insert_failure_rolls_back_and_retry_writes_once(tmp_path):
    hub = _hub(tmp_path)
    with sqlite3.connect(hub.cache.path) as conn:
        conn.execute("CREATE TRIGGER fail_note BEFORE INSERT ON stock_note BEGIN SELECT RAISE(ABORT, 'synthetic failed insert'); END")
    client = _client(hub)
    failed = client.post("/api/stock/notes", json=_explicit())
    assert failed.status_code == 503 and hub.quote_calls == []
    assert hub.cache.stock_notes("600519") == []
    with sqlite3.connect(hub.cache.path) as conn:
        conn.execute("DROP TRIGGER fail_note")
    retried = client.post("/api/stock/notes", json=_explicit())
    assert retried.status_code == 200 and hub.quote_calls == []
    assert [item.id for item in hub.cache.stock_notes("600519")] == [retried.json()["id"]]
