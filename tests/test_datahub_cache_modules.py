from __future__ import annotations

from datetime import datetime, timedelta
from types import SimpleNamespace

import pytest

from app.services.data_quality_time import expected_quote_date, latest_expected_trade_date
from app.models.schemas import MinuteKline
from app.services.datahub_cache import (
    MINUTE_INTERVAL_ALIASES,
    _kline_cache_is_fresh,
    _minute_kline_cache_is_fresh,
    _normalize_minute_interval,
    _stock_pool_cache_is_fresh,
)
from tests.factories import make_kline


def test_minute_interval_alias_table_is_complete_and_explicit() -> None:
    assert MINUTE_INTERVAL_ALIASES == {
        "1": "1m",
        "1min": "1m",
        "1m": "1m",
        "3": "3m",
        "3min": "3m",
        "3m": "3m",
        "5": "5m",
        "5min": "5m",
        "5m": "5m",
        "10": "10m",
        "10min": "10m",
        "10m": "10m",
        "15": "15m",
        "15min": "15m",
        "15m": "15m",
        "30": "30m",
        "30min": "30m",
        "30m": "30m",
        "60": "60m",
        "60min": "60m",
        "60m": "60m",
        "1h": "60m",
    }


def test_minute_interval_normalization_accepts_aliases_case_and_empty_default() -> None:
    for raw, normalized in MINUTE_INTERVAL_ALIASES.items():
        assert _normalize_minute_interval(raw) == normalized

    assert _normalize_minute_interval(" 5MIN ") == "5m"
    assert _normalize_minute_interval("") == "5m"
    assert _normalize_minute_interval(None) == "5m"  # type: ignore[arg-type]


def test_minute_interval_normalization_rejects_unsupported_interval() -> None:
    with pytest.raises(ValueError, match="1m、3m、5m、10m、15m、30m、60m"):
        _normalize_minute_interval("2h")


def test_stock_pool_cache_freshness_rejects_invalid_windows_and_future_timestamps() -> None:
    fresh_cache = SimpleNamespace(
        latest_stock_at=datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
        stock_count=10,
    )
    future_cache = SimpleNamespace(
        latest_stock_at=(datetime.now() + timedelta(days=1)).strftime("%Y-%m-%d %H:%M:%S"),
        stock_count=10,
    )

    assert _stock_pool_cache_is_fresh(fresh_cache, max_age_seconds=60)
    assert not _stock_pool_cache_is_fresh(fresh_cache, max_age_seconds=0)
    assert not _stock_pool_cache_is_fresh(fresh_cache, max_age_seconds=-1)
    assert not _stock_pool_cache_is_fresh(future_cache, max_age_seconds=60 * 60 * 24 * 7)


def test_kline_cache_freshness_rejects_future_dates() -> None:
    current = datetime.now()
    expected = latest_expected_trade_date(current)
    allowed = expected_quote_date(current)
    future = allowed + timedelta(days=1)

    assert _kline_cache_is_fresh([make_kline(date=expected.isoformat())])
    assert _kline_cache_is_fresh([make_kline(date=allowed.isoformat())])
    assert not _kline_cache_is_fresh([make_kline(date=future.isoformat())])


def test_minute_kline_cache_freshness_checks_business_timestamp() -> None:
    trading_now = datetime(2026, 5, 13, 10, 20, 0)
    midday_now = datetime(2026, 5, 13, 12, 0, 0)
    after_close_now = datetime(2026, 5, 13, 16, 0, 0)

    assert not _minute_kline_cache_is_fresh([], "5m", now=trading_now)
    assert _minute_kline_cache_is_fresh([_minute_row("2026-05-13 10:15:00")], "5m", now=trading_now)
    assert not _minute_kline_cache_is_fresh([_minute_row("2026-05-13 09:30:00")], "5m", now=trading_now)
    assert not _minute_kline_cache_is_fresh([_minute_row("2026-05-12 10:15:00")], "5m", now=trading_now)
    assert not _minute_kline_cache_is_fresh([_minute_row("2026-05-13 10:30:00")], "5m", now=trading_now)
    assert _minute_kline_cache_is_fresh([_minute_row("2026-05-13 11:25:00")], "5m", now=midday_now)
    assert not _minute_kline_cache_is_fresh([_minute_row("2026-05-13 11:10:00")], "5m", now=midday_now)
    assert _minute_kline_cache_is_fresh([_minute_row("2026-05-13 14:55:00")], "5m", now=after_close_now)
    assert not _minute_kline_cache_is_fresh([_minute_row("2026-05-13 14:30:00")], "5m", now=after_close_now)
    assert not _minute_kline_cache_is_fresh([_minute_row("2026-05-13 16:30:00")], "5m", now=after_close_now)
    assert not _minute_kline_cache_is_fresh([_minute_row("2026-05-13 10:15:00")], "2h", now=trading_now)


def _minute_row(timestamp: str) -> MinuteKline:
    return MinuteKline(
        timestamp=timestamp,
        open=100.0,
        close=101.0,
        high=102.0,
        low=99.0,
        volume=1000.0,
        amount=101000.0,
        interval="5m",
    )
