from __future__ import annotations

from datetime import datetime
from pathlib import Path
import sqlite3

import pytest

from app.config import Settings
from app.db.schema_migrations import apply_compat_schema
from app.models.market import PlateItem
from app.services.cache import SQLiteCache
from app.services.datahub_cache_coverage import ShortResponseCoverage
from app.services.eastmoney_client import eastmoney_industry_plate_rank
from app.utils.audit_time import audit_now_text
from app.utils.clock import ASHARE_TIMEZONE


EVENT_TIME = "2026-09-14 10:15:00"
EVENT_SECONDS = int(datetime(2026, 9, 14, 10, 15, tzinfo=ASHARE_TIMEZONE).timestamp())


def _provider_payload(event_time: object) -> dict[str, object]:
    return {"rc": 0, "data": {"total": 1, "diff": [{
        "f3": 1.2, "f12": "BK1036", "f14": "半导体", "f124": event_time,
    }]}}


def _provider_rows(monkeypatch, event_time: object, stamp: str) -> list[PlateItem]:
    def fetch(_url, params, **_kwargs):
        assert "f124" in params["fields"].split(",")
        assert "f12" in params["fields"].split(",")
        return _provider_payload(event_time)

    monkeypatch.setattr("app.services.eastmoney_client._bounded_eastmoney_json", fetch)
    monkeypatch.setattr("app.services.eastmoney_client.audit_now_text", lambda: stamp)
    return eastmoney_industry_plate_rank(limit=100)


@pytest.mark.parametrize("seconds", [EVENT_SECONDS, float(EVENT_SECONDS), str(EVENT_SECONDS)])
def test_plate_provider_keeps_index_identity_and_separate_event_time(monkeypatch, seconds):
    stamp = "2026-09-14T02:16:00.123456Z"
    [row] = _provider_rows(monkeypatch, seconds, stamp)

    assert row.symbol == "BK1036"
    assert row.quote_timestamp == EVENT_TIME
    assert row.updated_at == stamp
    assert row.source == "AKShare·东方财富直连"


@pytest.mark.parametrize("value", [
    None, "", "-", "invalid", "2026-09-14 10:15:00", 0, -1, True, False,
    float("nan"), float("inf"), float("-inf"), EVENT_SECONDS * 1000, 10 ** 400, [], {},
])
def test_plate_missing_or_invalid_seconds_never_use_acquisition_time(monkeypatch, value):
    stamp = "2026-09-14T02:16:00.123456Z"
    [row] = _provider_rows(monkeypatch, value, stamp)

    assert row.quote_timestamp is None
    assert row.updated_at == stamp
    assert row.symbol == "BK1036"


def test_plate_missing_field_is_not_backfilled_with_read_time(monkeypatch):
    payload = _provider_payload(None)
    del payload["data"]["diff"][0]["f124"]
    monkeypatch.setattr("app.services.eastmoney_client._bounded_eastmoney_json", lambda *_a, **_k: payload)

    [row] = eastmoney_industry_plate_rank()

    assert row.quote_timestamp is None
    assert row.updated_at


def test_plate_sqlite_roundtrip_preserves_event_identity_and_short_cache_coverage(tmp_path, monkeypatch):
    stamp = audit_now_text()
    rows = _provider_rows(monkeypatch, EVENT_SECONDS, stamp)
    settings = Settings(cache_path=tmp_path / "new.sqlite3")
    cache = SQLiteCache(settings=settings)
    coverage = ShortResponseCoverage()
    provider = object()
    chain = (("akshare", provider),)
    coverage.remember("plate", rows, 100, chain, ttl_seconds=300)

    cache.save_plate_rank(rows)
    restored = SQLiteCache(settings=settings).get_plate_rank(max_age_seconds=300, limit=100)

    assert restored == rows
    assert restored[0].quote_timestamp == EVENT_TIME
    assert restored[0].updated_at == stamp
    assert coverage.covers("plate", restored, 100, chain)


def _legacy_plate_database(path: Path, stamp: str) -> Settings:
    settings = Settings(cache_path=path)
    SQLiteCache(settings=settings)
    with sqlite3.connect(path) as conn:
        conn.execute("ALTER TABLE plate_rank DROP COLUMN symbol")
        conn.execute("ALTER TABLE plate_rank DROP COLUMN quote_timestamp")
        conn.execute(
            "INSERT INTO plate_rank (rank,name,change_pct,source,updated_at) VALUES (1,?,?,?,?)",
            ("半导体", 1.2, "旧行情源", stamp),
        )
    return settings


def test_legacy_plate_migration_keeps_missing_identity_and_event_as_null(tmp_path):
    stamp = audit_now_text()
    settings = _legacy_plate_database(tmp_path / "legacy.sqlite3", stamp)

    cache = SQLiteCache(settings=settings)
    [row] = cache.get_plate_rank(max_age_seconds=300)
    [again] = SQLiteCache(settings=settings).get_plate_rank(max_age_seconds=300)

    assert row == again
    assert row.name == "半导体"
    assert row.change_pct == 1.2
    assert row.symbol is None
    assert row.quote_timestamp is None
    assert row.updated_at == stamp


def test_plate_column_migration_rolls_back_partial_addition(tmp_path, monkeypatch):
    path = tmp_path / "rollback.sqlite3"
    stamp = audit_now_text()
    _legacy_plate_database(path, stamp)
    import app.db.schema_migrations as migrations

    original = migrations.ensure_column

    def fail_after_symbol(conn, table, column, definition):
        if table == "plate_rank" and column == "quote_timestamp":
            raise sqlite3.OperationalError("synthetic column failure")
        original(conn, table, column, definition)

    monkeypatch.setattr(migrations, "ensure_column", fail_after_symbol)
    with sqlite3.connect(path) as conn:
        with pytest.raises(sqlite3.OperationalError, match="synthetic column failure"):
            apply_compat_schema(conn)
        columns = {row[1] for row in conn.execute("PRAGMA table_info(plate_rank)")}
        assert "symbol" not in columns
        assert "quote_timestamp" not in columns
        assert conn.execute("SELECT updated_at FROM plate_rank").fetchone()[0] == stamp
