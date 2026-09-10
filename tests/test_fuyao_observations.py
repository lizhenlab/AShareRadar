from __future__ import annotations

import asyncio
from datetime import date, datetime, timedelta

import pytest

from app.services.fuyao_fetch import _fetch_pool, fetch_sentiment
from app.services.fuyao_sectors import fetch_sectors
from app.services.fuyao_observations import envelope_data, finite_or_none, normalized_valuations, normalized_pool_rows, valuation_history_summary
import app.services.fuyao_observations as observations
import app.services.fuyao_fetch as fetch


def pool(page=1, size=2, total=3, symbols=None, **updates):
    pagination = {"page": page, "size": size, "total": total, "pages": (total + size - 1) // size, **updates}
    rows = [{"thscode": symbol, "price_change_ratio_pct": 10} for symbol in (symbols or [])]
    return {"code": 0, "data": {"item": rows, "pagination": pagination}}


class Requests:
    def __init__(self, replies):
        self.replies = iter(replies)
        self.calls = []

    async def request(self, path, params=None):
        self.calls.append((path, params))
        return next(self.replies)


def test_pool_requires_complete_matching_pages():
    client = Requests([pool(symbols=["600519.SH", "000001.SZ"]), pool(page=2, symbols=["920066.BJ"])])
    rows = asyncio.run(_fetch_pool(client, "limit-up-pool", 123))
    assert len(rows) == 3
    assert [params["page"] for _, params in client.calls] == [1, 2]


@pytest.mark.parametrize("bad", [pool(page=1, symbols=["920066.BJ"]), pool(page=2, total=4, symbols=["920066.BJ", "600001.SH"]),
    pool(page=2, symbols=["600519.SH"]), pool(page=2, symbols=[]), pool(page=2, pages=3, symbols=["920066.BJ"])])
def test_pool_rejects_repeated_changed_truncated_pages(bad):
    client = Requests([pool(symbols=["600519.SH", "000001.SZ"]), bad])
    with pytest.raises(ValueError):
        asyncio.run(_fetch_pool(client, "limit-up-pool", 123))


@pytest.mark.parametrize("pages", [0, 1])
def test_empty_pool_preserves_zero_coverage(pages):
    client = Requests([pool(total=0, pages=pages)])
    assert asyncio.run(_fetch_pool(client, "limit-up-pool", 123)) == []


@pytest.mark.parametrize("value", [None, [], "bad", {"code": False, "data": {}}, {"code": 0, "data": []}])
def test_malicious_envelope_shapes_are_explicit_validation_errors(value):
    with pytest.raises(ValueError):
        envelope_data(value)


@pytest.mark.parametrize("value", [True, float("nan"), float("inf"), "1.0", 10**1000])
def test_invalid_numbers_are_not_silently_null(value):
    with pytest.raises(ValueError):
        finite_or_none(value)


def test_valuation_identity_normalizes_case_and_never_invents_freshness():
    payload = {"code": 0, "data": {"timestamp": None, "item": [{"thscode": "600519.sh", "pe_ttm": -2}]}}
    row = normalized_valuations(payload, ["600519.sh"])[0]
    assert row["symbol"] == "600519.SH" and row["values"]["pe_ttm"] == -2
    assert row["batch_timestamp"] is None and row["individual_timestamp"] is None
    payload["data"]["timestamp"] = {"untrusted": "timestamp"}
    with pytest.raises(ValueError):
        normalized_valuations(payload, ["600519.SH"])


def test_observation_days_use_shanghai_trade_days_and_latest_timestamp(monkeypatch):
    now = datetime.fromisoformat("2026-09-11T15:00:00+08:00")
    monkeypatch.setattr(observations, "market_now", lambda: now)
    def calendar(day, *, allow_auto_refresh):
        assert allow_auto_refresh is False
        return day.weekday() < 5
    monkeypatch.setattr(observations, "is_trading_day", calendar)
    rows = [{"fetched_at": "2026-09-10T16:01:00Z", "payload": {"values": {"pe_ttm": 2}}},
            {"fetched_at": "2026-09-11T09:00:00+08:00", "payload": {"values": {"pe_ttm": -1}}},
            {"fetched_at": "2026-09-05T15:00:00+08:00", "payload": {"values": {"pe_ttm": 4}}}]
    summary = valuation_history_summary(rows * 40, {"values": {"pe_ttm": 10}})
    assert summary["fields"]["pe_ttm"] == {"sample_days": 0, "percentile": None}
    start = date(2026, 7, 1)
    days = [start + timedelta(days=offset) for offset in range(70) if (start + timedelta(days=offset)).weekday() < 5][:30]
    valid = [{"fetched_at": f"{day}T15:00:00+08:00", "payload": {"values": {"pe_ttm": 2}}} for day in days]
    assert valuation_history_summary(valid * 3, {"values": {"pe_ttm": 3}})["fields"]["pe_ttm"] == {"sample_days": 30, "percentile": 100}


def test_weekend_sentiment_uses_previous_session_and_skips_current_anomaly(monkeypatch):
    monkeypatch.setattr(fetch, "market_now", lambda: datetime.fromisoformat("2026-09-12T12:00:00+08:00"))
    monkeypatch.setattr(fetch, "expected_quote_date", lambda _now: date(2026, 9, 11))
    client = Requests([pool(total=0), pool(total=0), pool(total=0),
        {"code": 0, "data": {"trade_date": "2026-09-11", "stock_count": 0, "stock_items": []}}])
    result = asyncio.run(fetch_sentiment(client, ["600519.SH"]))
    assert result["trade_date"] == "2026-09-11" and result["anomaly_observation_date"] is None
    assert len(client.calls) == 4


def test_current_anomaly_crossing_midnight_is_not_mixed_into_prior_day(monkeypatch):
    times = iter([datetime.fromisoformat("2026-09-10T23:59:00+08:00"), datetime.fromisoformat("2026-09-11T00:01:00+08:00")])
    monkeypatch.setattr(fetch, "market_now", lambda: next(times))
    monkeypatch.setattr(fetch, "expected_quote_date", lambda _now: date(2026, 9, 10))
    client = Requests([pool(total=0), pool(total=0), pool(total=0),
        {"code": 0, "data": {"trade_date": "2026-09-10", "stock_count": 0, "stock_items": []}}])
    with pytest.raises(ValueError, match="跨越"):
        asyncio.run(fetch_sentiment(client, ["600519.SH"]))


def test_pool_boolean_pagination_and_bad_rows_are_rejected():
    with pytest.raises(ValueError):
        normalized_pool_rows(pool(total=True))
    value = pool(total=1, symbols=["600519.SH"])
    value["data"]["item"] = [None]
    with pytest.raises(ValueError):
        normalized_pool_rows(value)


@pytest.mark.parametrize("total", [True, 0, 2, "1"])
def test_valuation_total_must_match_returned_rows(total):
    payload = {"code": 0, "data": {"total": total, "item": [{"thscode": "600519.SH"}]}}
    with pytest.raises(ValueError, match="总量"):
        normalized_valuations(payload, ["600519.SH"])


def test_sectors_preserve_current_membership_without_manufactured_quote_time():
    client = Requests([
        {"code": 0, "data": {"item": [{"thscode": "881101.TI", "name": "行业"}]}},
        {"code": 0, "data": {"item": []}},
        {"code": 0, "data": {"timestamp": None, "item": [{"thscode": "881101.TI", "last_price": 200, "price_change_ratio_pct": 1}]}},
        {"code": 0, "data": {"item": [{"thscode": "600519.sh"}]}}
    ])
    result = asyncio.run(fetch_sectors(client, ["881101.ti"]))
    assert result["members"] == {"881101.TI": ["600519.SH"]}
    assert result["rows"][0]["individual_timestamp"] is None
    assert result["point_in_time"] is False


def test_empty_sector_catalog_is_not_published_as_complete_market():
    client = Requests([{"code": 0, "data": {"item": []}}, {"code": 0, "data": {"item": []}}])
    with pytest.raises(ValueError, match="为空"):
        asyncio.run(fetch_sectors(client, []))
