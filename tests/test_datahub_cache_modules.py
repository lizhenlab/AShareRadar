from __future__ import annotations

from datetime import datetime, timedelta, timezone
from types import SimpleNamespace

import pytest

from app.models.schemas import MinuteKline
from app.models.system import CacheStats
from app.services.cache_freshness import assess_cache_freshness
from app.services.data_quality_kline import assess_kline_quality
from app.services.data_quality_time import expected_quote_date, latest_expected_daily_kline_date
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


@pytest.mark.parametrize(
    ("now", "kline_date", "expected_latest", "expected_status", "expected_days_behind"),
    [
        pytest.param(
            datetime(2026, 5, 13, 10, 0, 0),
            "2026-05-12",
            "2026-05-12",
            "fresh",
            0,
            id="morning-keeps-previous-daily-kline",
        ),
        pytest.param(
            datetime(2026, 5, 13, 12, 0, 0),
            "2026-05-12",
            "2026-05-12",
            "fresh",
            0,
            id="midday-keeps-previous-daily-kline",
        ),
        pytest.param(
            datetime(2026, 5, 13, 15, 0, 1),
            "2026-05-12",
            "2026-05-12",
            "fresh",
            0,
            id="close-buffer-first-second",
        ),
        pytest.param(
            datetime(2026, 5, 13, 15, 14, 59),
            "2026-05-12",
            "2026-05-12",
            "fresh",
            0,
            id="publish-buffer-last-second",
        ),
        pytest.param(
            datetime(2026, 5, 13, 15, 14, 59),
            "2026-05-13",
            "2026-05-12",
            "fresh",
            0,
            id="legal-current-day-before-publish",
        ),
        pytest.param(
            datetime(2026, 5, 13, 15, 15, 0),
            "2026-05-12",
            "2026-05-13",
            "stale",
            1,
            id="publish-time-requires-current-day",
        ),
        pytest.param(
            datetime(2026, 5, 13, 15, 15, 0),
            "2026-05-13",
            "2026-05-13",
            "fresh",
            0,
            id="publish-time-accepts-current-day",
        ),
        pytest.param(
            datetime(2026, 5, 16, 10, 0, 0),
            "2026-05-15",
            "2026-05-15",
            "fresh",
            0,
            id="weekend-keeps-last-trading-day",
        ),
        pytest.param(
            datetime(2026, 5, 13, 7, 14, 59, tzinfo=timezone.utc),
            "2026-05-12",
            "2026-05-12",
            "fresh",
            0,
            id="utc-now-is-converted-to-shanghai-before-publish",
        ),
        pytest.param(
            datetime(2026, 5, 13, 12, 0, 0),
            "2026-05-14",
            "2026-05-12",
            "future",
            None,
            id="future-date-remains-rejected",
        ),
    ],
)
def test_daily_kline_cache_quality_and_freshness_share_publish_boundary(
    now: datetime,
    kline_date: str,
    expected_latest: str,
    expected_status: str,
    expected_days_behind: int | None,
) -> None:
    klines = [make_kline(date=kline_date)]
    quality = assess_kline_quality(klines, now=now)
    freshness = assess_cache_freshness(_cache_stats(daily_date=kline_date), now=now)

    assert latest_expected_daily_kline_date(now).isoformat() == expected_latest
    assert quality.latest_expected_date == expected_latest
    assert quality.latest_allowed_date == expected_quote_date(now).isoformat()
    assert quality.days_behind_expected == expected_days_behind
    assert freshness.market_freshness["daily_kline"].status == expected_status
    assert _kline_cache_is_fresh(klines, now=now) is (expected_status == "fresh")


def test_minute_kline_cache_freshness_checks_business_timestamp() -> None:
    trading_now = datetime(2026, 5, 13, 10, 20, 0)
    midday_now = datetime(2026, 5, 13, 12, 0, 0)
    after_close_now = datetime(2026, 5, 13, 16, 0, 0)
    weekend_now = datetime(2026, 5, 16, 10, 0, 0)

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
    assert _minute_kline_cache_is_fresh([_minute_row("2026-05-15 14:55:00")], "5m", now=weekend_now)
    assert not _minute_kline_cache_is_fresh([_minute_row("2026-05-15 14:54:00")], "5m", now=weekend_now)
    assert not _minute_kline_cache_is_fresh([_minute_row("2026-05-13 10:15:00")], "2h", now=trading_now)


@pytest.mark.parametrize(
    ("now", "event_timestamp", "expected_status"),
    [
        pytest.param(datetime(2026, 5, 13, 9, 30, 0), "2026-05-13 09:15:00", "fresh", id="open-auction-event"),
        pytest.param(datetime(2026, 5, 13, 9, 30, 0), "2026-05-13 09:14:59", "stale", id="open-pre-auction-event"),
        pytest.param(datetime(2026, 5, 13, 11, 30, 0), "2026-05-13 11:15:00", "fresh", id="morning-end-exact"),
        pytest.param(datetime(2026, 5, 13, 11, 30, 1), "2026-05-13 11:24:59", "stale", id="midday-first-second"),
        pytest.param(datetime(2026, 5, 13, 11, 30, 1), "2026-05-13 11:25:00", "fresh", id="midday-close-snapshot"),
        pytest.param(datetime(2026, 5, 13, 13, 0, 0), "2026-05-13 11:24:59", "stale", id="reopen-before-snapshot-window"),
        pytest.param(datetime(2026, 5, 13, 13, 0, 0), "2026-05-13 11:25:00", "fresh", id="reopen-snapshot-lower-bound"),
        pytest.param(datetime(2026, 5, 13, 13, 15, 0), "2026-05-13 11:30:00", "fresh", id="reopen-grace-end-snapshot"),
        pytest.param(datetime(2026, 5, 13, 13, 15, 0), "2026-05-13 11:30:01", "stale", id="reopen-after-snapshot-window"),
        pytest.param(datetime(2026, 5, 13, 13, 15, 0), "2026-05-13 13:00:00", "fresh", id="reopen-live-interval-boundary"),
        pytest.param(datetime(2026, 5, 13, 13, 15, 1), "2026-05-13 11:30:00", "stale", id="after-grace-rejects-morning"),
        pytest.param(datetime(2026, 5, 13, 13, 15, 1), "2026-05-13 12:59:59", "stale", id="after-grace-requires-afternoon"),
        pytest.param(datetime(2026, 5, 13, 13, 15, 1), "2026-05-13 13:15:00", "fresh", id="after-grace-accepts-live"),
        pytest.param(datetime(2026, 5, 13, 15, 0, 0), "2026-05-13 14:45:00", "fresh", id="market-close-still-live"),
        pytest.param(datetime(2026, 5, 13, 15, 0, 1), "2026-05-13 14:54:59", "stale", id="close-buffer-requires-snapshot"),
        pytest.param(datetime(2026, 5, 13, 15, 0, 1), "2026-05-13 14:55:00", "fresh", id="close-buffer-accepts-snapshot"),
        pytest.param(datetime(2026, 5, 13, 13, 0, 0), "2026-05-13 13:00:01", "future", id="future-minute-event"),
    ],
)
def test_minute_cache_read_matches_market_freshness_phase_boundaries(
    now: datetime,
    event_timestamp: str,
    expected_status: str,
) -> None:
    rows = [_minute_row(event_timestamp)]
    freshness = assess_cache_freshness(_cache_stats(minute_timestamp=event_timestamp), now=now)

    assert freshness.market_freshness["minute_kline"].status == expected_status
    assert _minute_kline_cache_is_fresh(rows, "5m", now=now) is (expected_status == "fresh")


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


def _cache_stats(*, daily_date: str | None = None, minute_timestamp: str | None = None) -> CacheStats:
    minute_count = int(minute_timestamp is not None)
    daily_count = int(daily_date is not None)
    return CacheStats(
        path=":memory:",
        quote_count=0,
        quote_history_count=0,
        kline_count=daily_count + minute_count,
        daily_kline_count=daily_count,
        minute_kline_count=minute_count,
        stock_count=0,
        plate_count=1,
        provider_count=0,
        latest_daily_kline_date=daily_date,
        latest_minute_kline_timestamp=minute_timestamp,
    )
