from __future__ import annotations

from datetime import date, datetime

import pytest

from app.models.system import CacheStats
from app.services import trading_calendar
from app.services.cache_freshness import assess_cache_freshness


def test_new_fetch_activity_does_not_hide_old_market_events() -> None:
    now = datetime(2026, 5, 13, 10, 30, 0)
    assessment = assess_cache_freshness(
        _stats(
            fetched_at="2026-05-13 10:29:30",
            quote_timestamp="2026-05-12 15:00:00",
            daily_date="2026-05-11",
            minute_timestamp="2026-05-12 15:00:00",
        ),
        now=now,
    )

    assert {key: item.status for key, item in assessment.fetch_activity.items()} == {
        "quote": "recent",
        "daily_kline": "recent",
        "minute_kline": "recent",
        "stock": "recent",
        "plate": "recent",
    }
    assert {key: item.status for key, item in assessment.market_freshness.items()} == {
        "quote": "stale",
        "daily_kline": "stale",
        "minute_kline": "stale",
        "stock": "fresh",
        "plate": "fresh",
    }
    assert {issue.semantic for issue in assessment.issues} == {"market_freshness"}


def test_legacy_fetch_timestamps_never_prove_market_freshness() -> None:
    now = datetime(2026, 5, 13, 10, 30, 0)
    fetched_at = "2026-05-13 10:29:30"
    stats = CacheStats(
        path=":memory:",
        quote_count=1,
        quote_history_count=0,
        kline_count=1,
        daily_kline_count=1,
        stock_count=1,
        plate_count=1,
        provider_count=0,
        latest_quote_at=fetched_at,
        latest_kline_at=fetched_at,
        latest_daily_kline_at=fetched_at,
        latest_stock_at=fetched_at,
        latest_plate_at=fetched_at,
    )

    assessment = assess_cache_freshness(stats, now=now)

    assert assessment.fetch_activity["quote"].status == "recent"
    assert assessment.fetch_activity["daily_kline"].status == "recent"
    assert assessment.market_freshness["quote"].status == "missing"
    assert assessment.market_freshness["daily_kline"].status == "missing"
    assert {issue.semantic for issue in assessment.issues} == {"market_freshness"}


def test_stock_pool_and_plate_cache_expiry_use_configured_thresholds() -> None:
    now = datetime(2026, 5, 13, 10, 30, 0)
    stats = _stats(
        fetched_at="2026-05-13 10:29:30",
        quote_timestamp="2026-05-13 10:29:00",
        daily_date="2026-05-12",
        minute_timestamp=None,
        minute_count=0,
    ).model_copy(
        update={
            "stock_count": 1,
            "plate_count": 1,
            "latest_stock_at": "2026-05-13 09:29:59",
            "latest_plate_at": "2026-05-13 10:19:59",
        }
    )

    assessment = assess_cache_freshness(
        stats,
        now=now,
        stock_pool_cache_seconds=60 * 60,
        plate_rank_cache_seconds=10 * 60,
    )

    assert assessment.fetch_activity["stock"].status == "idle"
    assert assessment.fetch_activity["plate"].status == "idle"
    assert assessment.market_freshness["stock"].status == "stale"
    assert assessment.market_freshness["plate"].status == "stale"
    assert [(issue.category, issue.status) for issue in assessment.issues][-2:] == [
        ("stock", "stale"),
        ("plate", "stale"),
    ]


def test_nonempty_metadata_cache_without_timestamp_is_not_reported_healthy() -> None:
    now = datetime(2026, 5, 13, 10, 30, 0)
    stats = _stats(
        fetched_at="2026-05-13 10:29:30",
        quote_timestamp="2026-05-13 10:29:00",
        daily_date="2026-05-12",
        minute_timestamp=None,
        minute_count=0,
    ).model_copy(update={"latest_stock_at": None, "latest_plate_at": None})

    assessment = assess_cache_freshness(stats, now=now)

    assert [(issue.category, issue.status) for issue in assessment.issues] == [
        ("stock", "missing"),
        ("plate", "missing"),
    ]


@pytest.mark.parametrize(
    ("now", "last_trade_date"),
    [
        (datetime(2026, 5, 16, 12, 0, 0), "2026-05-15"),
        (datetime(2026, 10, 5, 12, 0, 0), "2026-09-30"),
    ],
)
def test_last_valid_trade_day_is_market_fresh_during_weekend_or_holiday(
    now: datetime,
    last_trade_date: str,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    trade_days = {
        date(2026, 5, 15),
        date(2026, 9, 30),
        date(2026, 10, 9),
    }
    monkeypatch.setattr(trading_calendar, "_trade_days", lambda: trade_days)

    assessment = assess_cache_freshness(
        _stats(
            fetched_at=f"{last_trade_date} 15:01:00",
            quote_timestamp=f"{last_trade_date} 15:00:00",
            daily_date=last_trade_date,
            minute_timestamp=f"{last_trade_date} 15:00:00",
        ),
        now=now,
        stock_pool_cache_seconds=10 * 24 * 60 * 60,
        plate_rank_cache_seconds=10 * 24 * 60 * 60,
    )

    assert assessment.fetch_activity["quote"].status == "idle"
    assert {item.status for item in assessment.fetch_activity.values()} <= {"recent", "idle"}
    assert all(
        assessment.market_freshness[key].status == "fresh"
        for key in ("quote", "daily_kline", "minute_kline")
    )
    assert assessment.issues == ()


@pytest.mark.parametrize(
    ("now", "event_timestamp", "daily_date"),
    [
        (
            datetime(2026, 5, 13, 18, 0, 0),
            "2026-05-13 16:14:59",
            "2026-05-13",
        ),
        (
            datetime(2026, 5, 16, 12, 0, 0),
            "2026-05-15 16:14:59",
            "2026-05-15",
        ),
    ],
)
def test_after_hours_quote_timestamp_keeps_the_closing_snapshot_fresh(
    now: datetime,
    event_timestamp: str,
    daily_date: str,
) -> None:
    assessment = assess_cache_freshness(
        _stats(
            fetched_at=now.strftime("%Y-%m-%d %H:%M:%S"),
            quote_timestamp=event_timestamp,
            daily_date=daily_date,
            minute_timestamp=event_timestamp,
        ),
        now=now,
        stock_pool_cache_seconds=10 * 24 * 60 * 60,
        plate_rank_cache_seconds=10 * 24 * 60 * 60,
    )

    assert assessment.market_freshness["quote"].status == "fresh"
    assert assessment.market_freshness["minute_kline"].status == "fresh"


def test_future_and_dirty_market_events_are_reported_by_domain() -> None:
    now = datetime(2026, 5, 13, 10, 30, 0)
    assessment = assess_cache_freshness(
        _stats(
            fetched_at="2026-05-13 10:29:30",
            quote_timestamp="2026-05-14 10:00:00",
            daily_date="not-a-date",
            minute_timestamp="2026-05-13 10:31:00",
        ),
        now=now,
    )

    assert assessment.market_freshness["quote"].status == "future"
    assert assessment.market_freshness["daily_kline"].status == "invalid"
    assert assessment.market_freshness["minute_kline"].status == "future"
    assert [(issue.category, issue.semantic, issue.status) for issue in assessment.issues] == [
        ("quote", "market_freshness", "future"),
        ("kline", "market_freshness", "invalid"),
        ("minute", "market_freshness", "future"),
    ]


def test_checked_domains_only_include_optional_minute_domain_when_data_exists() -> None:
    now = datetime(2026, 5, 13, 10, 30, 0)
    without_minute = assess_cache_freshness(
        _stats(
            fetched_at="2026-05-13 10:29:30",
            quote_timestamp="2026-05-13 10:29:00",
            daily_date="2026-05-12",
            minute_timestamp=None,
            minute_count=0,
        ),
        now=now,
    )

    assert without_minute.checked_domains == (
        "报价市场时效",
        "日K市场时效",
        "股票池缓存时效",
        "行业背景缓存时效",
        "股票池可用性",
        "行业背景可用性",
    )
    assert "minute_kline" not in without_minute.market_freshness


@pytest.mark.parametrize(
    ("now", "event_timestamp", "daily_date", "expected_statuses"),
    [
        pytest.param(
            datetime(2026, 5, 13, 9, 30, 0),
            "2026-05-13 09:15:00",
            "2026-05-12",
            ("fresh", "fresh", "fresh"),
            id="open-accepts-auction-event",
        ),
        pytest.param(
            datetime(2026, 5, 13, 9, 30, 0),
            "2026-05-13 09:14:59",
            "2026-05-12",
            ("stale", "fresh", "stale"),
            id="open-rejects-pre-auction-event",
        ),
        pytest.param(
            datetime(2026, 5, 13, 11, 30, 0),
            "2026-05-13 11:15:00",
            "2026-05-12",
            ("fresh", "fresh", "fresh"),
            id="morning-close-is-still-live",
        ),
        pytest.param(
            datetime(2026, 5, 13, 11, 30, 1),
            "2026-05-13 11:15:00",
            "2026-05-12",
            ("stale", "fresh", "stale"),
            id="midday-requires-close-snapshot",
        ),
        pytest.param(
            datetime(2026, 5, 13, 11, 30, 1),
            "2026-05-13 11:25:00",
            "2026-05-12",
            ("fresh", "fresh", "fresh"),
            id="midday-accepts-close-snapshot",
        ),
        pytest.param(
            datetime(2026, 5, 13, 13, 0, 0),
            "2026-05-13 11:30:00",
            "2026-05-12",
            ("fresh", "fresh", "fresh"),
            id="afternoon-open-grace",
        ),
        pytest.param(
            datetime(2026, 5, 13, 13, 15, 0),
            "2026-05-13 11:30:00",
            "2026-05-12",
            ("fresh", "fresh", "fresh"),
            id="reopen-grace-end-exact",
        ),
        pytest.param(
            datetime(2026, 5, 13, 13, 15, 1),
            "2026-05-13 11:30:00",
            "2026-05-12",
            ("stale", "fresh", "stale"),
            id="after-reopen-grace-rejects-morning",
        ),
        pytest.param(
            datetime(2026, 5, 13, 13, 15, 1),
            "2026-05-13 13:15:00",
            "2026-05-12",
            ("fresh", "fresh", "fresh"),
            id="after-reopen-grace-accepts-live",
        ),
        pytest.param(
            datetime(2026, 5, 13, 15, 0, 0),
            "2026-05-13 14:55:00",
            "2026-05-12",
            ("fresh", "fresh", "fresh"),
            id="market-close-exact",
        ),
        pytest.param(
            datetime(2026, 5, 13, 15, 0, 1),
            "2026-05-13 14:55:00",
            "2026-05-12",
            ("fresh", "fresh", "fresh"),
            id="close-buffer-first-second",
        ),
        pytest.param(
            datetime(2026, 5, 13, 15, 0, 59),
            "2026-05-13 14:55:00",
            "2026-05-12",
            ("fresh", "fresh", "fresh"),
            id="reported-150059-conflict",
        ),
        pytest.param(
            datetime(2026, 5, 13, 15, 14, 59),
            "2026-05-13 14:55:00",
            "2026-05-12",
            ("fresh", "fresh", "fresh"),
            id="daily-buffer-last-second",
        ),
        pytest.param(
            datetime(2026, 5, 13, 15, 15, 0),
            "2026-05-13 14:55:00",
            "2026-05-12",
            ("fresh", "stale", "fresh"),
            id="daily-buffer-ended-needs-current-day",
        ),
        pytest.param(
            datetime(2026, 5, 13, 15, 15, 0),
            "2026-05-13 14:55:00",
            "2026-05-13",
            ("fresh", "fresh", "fresh"),
            id="daily-buffer-ended-current-day-present",
        ),
    ],
)
def test_market_freshness_uses_one_boundary_matrix_for_all_domains(
    now: datetime,
    event_timestamp: str,
    daily_date: str,
    expected_statuses: tuple[str, str, str],
) -> None:
    assessment = assess_cache_freshness(
        _stats(
            fetched_at=now.strftime("%Y-%m-%d %H:%M:%S"),
            quote_timestamp=event_timestamp,
            daily_date=daily_date,
            minute_timestamp=event_timestamp,
        ),
        now=now,
    )

    statuses = assessment.market_freshness
    assert (
        statuses["quote"].status,
        statuses["daily_kline"].status,
        statuses["minute_kline"].status,
    ) == expected_statuses


def _stats(
    *,
    fetched_at: str,
    quote_timestamp: str,
    daily_date: str,
    minute_timestamp: str | None,
    minute_count: int = 1,
) -> CacheStats:
    return CacheStats(
        path=":memory:",
        quote_count=1,
        quote_history_count=0,
        kline_count=1 + minute_count,
        daily_kline_count=1,
        minute_kline_count=minute_count,
        stock_count=1,
        plate_count=1,
        provider_count=0,
        latest_quote_at=fetched_at,
        latest_kline_at=fetched_at,
        latest_daily_kline_at=fetched_at,
        latest_minute_kline_at=fetched_at if minute_count else None,
        latest_quote_fetched_at=fetched_at,
        latest_daily_kline_fetched_at=fetched_at,
        latest_minute_kline_fetched_at=fetched_at if minute_count else None,
        latest_quote_timestamp=quote_timestamp,
        latest_daily_kline_date=daily_date,
        latest_minute_kline_timestamp=minute_timestamp,
        latest_stock_at=fetched_at,
        latest_plate_at=fetched_at,
    )
