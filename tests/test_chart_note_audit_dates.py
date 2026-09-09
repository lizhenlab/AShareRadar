from __future__ import annotations

import asyncio
from pathlib import Path
from types import SimpleNamespace

from fastapi import FastAPI
from fastapi.testclient import TestClient
import pytest

from app.api.deps import get_datahub
from app.api.routes import notes
from app.models.analysis import StockEventItem
from app.models.user_data import StockNoteItem
from app.services import chart_marks, trading_calendar
from app.services.cache import SQLiteCache
from app.services.stock_event_sources import default_observation_event
from tests.factories import make_quote


def _note(*, created_at: str, trade_date: str | None = None, visible: bool = True) -> StockNoteItem:
    return StockNoteItem(
        id=1, revision="a" * 64, symbol="600519.SH", code="600519", market="sh", name="测试股票",
        note_type="观察", content="日期边界笔记", price=100, trade_date=trade_date,
        visible=visible, created_at=created_at, updated_at=created_at,
    )


@pytest.mark.parametrize(("created_at", "expected"), [
    ("2026-07-14T15:59:59.999999Z", "2026-07-14"),
    ("2026-07-14T16:00:00.000000Z", "2026-07-15"),
    ("2026-07-14T23:30:00-04:00", "2026-07-15"),
    ("2026-07-15T01:00:00+14:00", "2026-07-14"),
    ("2026-07-14T23:59:59", "2026-07-14"),
    ("2026-07-14 23:59:59", "2026-07-14"),
    ("2026/07/14 23:59:59", "2026-07-14"),
    ("2026-07-14", "2026-07-14"),
    ("2026/07/14", "2026-07-14"),
])
def test_note_without_trade_date_uses_shanghai_audit_date(created_at: str, expected: str) -> None:
    mark, = chart_marks._note_marks([_note(created_at=created_at)])

    assert mark.visible is True
    assert mark.kline_date == expected
    assert mark.date == created_at
    assert mark.price == 100
    assert mark.anchor_price_type == "manual"


@pytest.mark.parametrize("created_at", [
    "", "nan", "2026-02-30T16:00:00Z", "20260714T160000Z",
    "2026-W29-2T16:00:00Z", "2026-07-14X16:00:00Z",
    "2026-07-14T25:00:00Z", "2026-07-14T16:00:00+00:99",
    "0001-01-01T00:00:00+14:00", "9999-12-31T23:59:59Z",
])
def test_invalid_audit_dates_do_not_create_chart_locations(created_at: str) -> None:
    mark, = chart_marks._note_marks([_note(created_at=created_at)])

    assert mark.kline_date is None
    assert mark.visible is False


@pytest.mark.parametrize("trade_date", [
    "2026-07-13", "2026/07/13", "2026-07-13 23:59:59", "2026/07/13 23:59:59",
])
def test_explicit_trade_date_has_precedence_and_preserves_market_date(trade_date: str) -> None:
    mark, = chart_marks._note_marks([
        _note(created_at="2026-07-14T16:00:00Z", trade_date=trade_date),
    ])

    assert mark.visible is True
    assert mark.kline_date == "2026-07-13"
    assert mark.date == trade_date


@pytest.mark.parametrize("trade_date", ["bad-date", "nan", "20260713", "2026-07-13T16:00:00Z"])
def test_invalid_explicit_trade_date_does_not_fall_back_to_audit_date(trade_date: str) -> None:
    mark, = chart_marks._note_marks([
        _note(created_at="2026-07-14 10:00:00", trade_date=trade_date),
    ])

    assert mark.visible is False
    assert mark.kline_date is None


@pytest.mark.parametrize("trade_date", [None, "2026-07-13"])
def test_audit_date_conversion_does_not_reveal_hidden_notes(trade_date: str | None) -> None:
    mark, = chart_marks._note_marks([
        _note(created_at="2026-07-14T16:00:00Z", trade_date=trade_date, visible=False),
    ])

    assert mark.visible is False


@pytest.mark.parametrize(("date", "expected"), [
    ("2026-07-14", "2026-07-14"), ("2026/07/14 23:59:59", "2026-07-14"),
    ("2026-07-14T16:00:00Z", None), ("20260714", None),
])
def test_event_marks_keep_their_existing_market_date_contract(date: str, expected: str | None) -> None:
    mark = chart_marks._event_mark(
        date=date, label="合成事件", category="行业", level="观察", description="事件", source="测试",
    )

    assert mark.kline_date == expected
    assert mark.visible is (expected is not None)


@pytest.mark.parametrize("cleared_date", [None, "   "])
@pytest.mark.parametrize(("audit_time", "expected"), [
    ("2026-07-14T15:59:59.999999Z", "2026-07-14"),
    ("2026-07-14T16:00:00.000000Z", "2026-07-15"),
    ("2026-07-15T01:00:00+14:00", "2026-07-14"),
])
def test_clear_note_trade_date_keeps_public_chart_mark_on_audit_market_date(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
    cleared_date: str | None, audit_time: str, expected: str,
) -> None:
    monkeypatch.setattr(trading_calendar, "CALENDAR_PATH", tmp_path / "absent-calendar.json")
    monkeypatch.setattr("app.repositories.notes.now_text", lambda: audit_time)
    monkeypatch.setattr(chart_marks, "_load_default_chart_context", _empty_chart_context)
    cache = SQLiteCache(tmp_path / "synthetic.sqlite3")
    app = FastAPI()
    app.include_router(notes.router)
    app.dependency_overrides[get_datahub] = lambda: _NoteHub(cache)
    with TestClient(app) as client:
        created = client.post("/api/stock/notes", json={
            "symbol": "600519", "content": "清空日期仍显示", "price": 100,
            "trade_date": "2026-07-13", "visible": True,
        })
        assert created.status_code == 200
        note_id = created.json()["id"]
        changed = client.patch(f"/api/stock/notes/{note_id}", json={"expected_revision": created.json()["revision"], "trade_date": cleared_date})
        assert changed.status_code == 200
        assert changed.json()["trade_date"] is None
        assert changed.json()["visible"] is True
        response = client.get("/api/stock/chart-marks", params={"symbol": "600519"})
        assert response.status_code == 200
        mark, = response.json()["marks"]
        assert mark["kline_date"] == expected
        assert mark["date"] == audit_time
        assert mark["description"] == "清空日期仍显示"
        assert client.patch(f"/api/stock/notes/{note_id}", json={"expected_revision": changed.json()["revision"], "visible": False}).status_code == 200
        assert client.get("/api/stock/chart-marks", params={"symbol": "600519"}).json()["marks"] == []


class _NoteHub:
    def __init__(self, cache: SQLiteCache) -> None:
        self.cache = cache

    async def quote(self, _symbol: str):
        return make_quote(timestamp="2026-07-14 10:00:00", price=100)


async def _empty_chart_context(*_args):
    return SimpleNamespace(insights=SimpleNamespace(
        abnormal_events=SimpleNamespace(events=[]), events=SimpleNamespace(events=[]),
    ))


def test_default_no_event_notice_does_not_generate_a_chart_event() -> None:
    notice = default_observation_event(SimpleNamespace(quote=make_quote(timestamp="2026-07-14 10:00:00")))
    bundle = SimpleNamespace(abnormal_events=SimpleNamespace(events=[]), events=SimpleNamespace(events=[notice]))
    hub = SimpleNamespace(cache=SimpleNamespace(stock_notes=lambda *_args, **_kwargs: []))

    summary = asyncio.run(chart_marks.build_chart_marks_from_context(hub, "600519", bundle))

    assert summary.marks == []
    assert summary.categories == []


def test_availability_notices_do_not_hide_real_observation_events_behind_the_cap() -> None:
    notice = default_observation_event(SimpleNamespace(quote=make_quote(timestamp="2026-07-14 10:00:00")))
    event = StockEventItem(
        date="2026-07-14 10:00:00", title="真实观察事件", category="观察", level="观察",
        description="合成数据形成的真实观察", source="隔离测试",
    )
    bundle = SimpleNamespace(
        abnormal_events=SimpleNamespace(events=[]), events=SimpleNamespace(events=[notice] * 6 + [event]),
    )
    hub = SimpleNamespace(cache=SimpleNamespace(stock_notes=lambda *_args, **_kwargs: []))

    summary = asyncio.run(chart_marks.build_chart_marks_from_context(hub, "600519", bundle))

    assert [mark.label for mark in summary.marks] == ["真实观察事件"]
    assert summary.marks[0].kline_date == "2026-07-14"
    assert summary.categories == ["观察"]
