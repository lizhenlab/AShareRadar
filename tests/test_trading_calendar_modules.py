from __future__ import annotations

import json
import logging
from datetime import date, datetime

import pytest

from app.services import trading_calendar


@pytest.fixture(autouse=True)
def _reset_trade_calendar_cache(monkeypatch: pytest.MonkeyPatch):
    monkeypatch.setenv("ASHARE_RADAR_TRADE_CALENDAR_AUTO_FETCH", "0")
    monkeypatch.setenv("TRADE_CALENDAR_AUTO_FETCH", "0")
    trading_calendar._trade_days.cache_clear()
    yield
    trading_calendar._trade_days.cache_clear()


def test_official_dates_apply_only_inside_cached_coverage(tmp_path, monkeypatch: pytest.MonkeyPatch) -> None:
    _use_calendar(
        tmp_path,
        monkeypatch,
        ["2025-12-30", "2025-12-31", "2026-01-05"],
    )

    assert trading_calendar.is_trading_day(date(2025, 12, 30))
    assert not trading_calendar.is_trading_day(date(2026, 1, 1))
    assert not trading_calendar.is_trading_day(date(2026, 1, 2))
    assert trading_calendar.is_trading_day(date(2026, 1, 5))
    assert trading_calendar.is_trading_day(date(2025, 12, 29))
    assert trading_calendar.is_trading_day(date(2026, 1, 6))


def test_weekends_remain_closed_outside_cached_coverage(tmp_path, monkeypatch: pytest.MonkeyPatch) -> None:
    _use_calendar(tmp_path, monkeypatch, ["2025-12-31"])

    assert not trading_calendar.is_trading_day(date(2026, 1, 3))
    assert not trading_calendar.is_trading_day(date(2026, 1, 4))
    assert trading_calendar.previous_trade_date(date(2026, 1, 4)) == date(2026, 1, 2)


def test_cross_year_expected_dates_do_not_retreat_to_old_cache(tmp_path, monkeypatch: pytest.MonkeyPatch) -> None:
    _use_calendar(tmp_path, monkeypatch, ["2025-12-30", "2025-12-31"])

    assert trading_calendar.latest_expected_trade_date(datetime(2026, 1, 5, 14, 0)) == date(2026, 1, 2)
    assert trading_calendar.latest_expected_trade_date(datetime(2026, 1, 5, 16, 0)) == date(2026, 1, 5)
    assert trading_calendar.expected_quote_date(datetime(2026, 1, 5, 9, 20)) == date(2026, 1, 5)


@pytest.mark.parametrize(
    ("now", "expected"),
    [
        pytest.param(
            datetime(2026, 5, 13, 9, 14, 59),
            trading_calendar.MarketSessionPhase.PRE_OPEN,
            id="before-auction",
        ),
        pytest.param(
            datetime(2026, 5, 13, 9, 15, 0),
            trading_calendar.MarketSessionPhase.CALL_AUCTION,
            id="auction-start",
        ),
        pytest.param(
            datetime(2026, 5, 13, 9, 30, 0),
            trading_calendar.MarketSessionPhase.MORNING,
            id="morning-open",
        ),
        pytest.param(
            datetime(2026, 5, 13, 11, 30, 0),
            trading_calendar.MarketSessionPhase.MORNING,
            id="morning-close-exact",
        ),
        pytest.param(
            datetime(2026, 5, 13, 11, 30, 1),
            trading_calendar.MarketSessionPhase.MIDDAY_BREAK,
            id="midday-one-second-later",
        ),
        pytest.param(
            datetime(2026, 5, 13, 13, 0, 0),
            trading_calendar.MarketSessionPhase.AFTERNOON_REOPEN_GRACE,
            id="afternoon-open",
        ),
        pytest.param(
            datetime(2026, 5, 13, 13, 15, 0),
            trading_calendar.MarketSessionPhase.AFTERNOON_REOPEN_GRACE,
            id="reopen-grace-end-exact",
        ),
        pytest.param(
            datetime(2026, 5, 13, 13, 15, 1),
            trading_calendar.MarketSessionPhase.AFTERNOON,
            id="after-reopen-grace",
        ),
        pytest.param(
            datetime(2026, 5, 13, 15, 0, 0),
            trading_calendar.MarketSessionPhase.AFTERNOON,
            id="market-close-exact",
        ),
        pytest.param(
            datetime(2026, 5, 13, 15, 0, 1),
            trading_calendar.MarketSessionPhase.CLOSE_PUBLISH_BUFFER,
            id="close-buffer-one-second-later",
        ),
        pytest.param(
            datetime(2026, 5, 13, 15, 14, 59),
            trading_calendar.MarketSessionPhase.CLOSE_PUBLISH_BUFFER,
            id="close-buffer-last-second",
        ),
        pytest.param(
            datetime(2026, 5, 13, 15, 15, 0),
            trading_calendar.MarketSessionPhase.AFTER_CLOSE,
            id="daily-publish-time",
        ),
    ],
)
def test_market_session_phase_has_explicit_second_boundaries(
    tmp_path,
    monkeypatch: pytest.MonkeyPatch,
    now: datetime,
    expected: trading_calendar.MarketSessionPhase,
) -> None:
    _use_calendar(
        tmp_path,
        monkeypatch,
        ["2026-05-11", "2026-05-12", "2026-05-13", "2026-05-14", "2026-05-15"],
    )

    assert trading_calendar.market_session_phase(now) is expected
    assert trading_calendar.is_trading_session(now) is (
        expected
        in {
            trading_calendar.MarketSessionPhase.MORNING,
            trading_calendar.MarketSessionPhase.AFTERNOON_REOPEN_GRACE,
            trading_calendar.MarketSessionPhase.AFTERNOON,
        }
    )
    assert trading_calendar.is_midday_break(now) is (expected is trading_calendar.MarketSessionPhase.MIDDAY_BREAK)
    assert trading_calendar.is_after_close(now) is (
        expected
        in {
            trading_calendar.MarketSessionPhase.CLOSE_PUBLISH_BUFFER,
            trading_calendar.MarketSessionPhase.AFTER_CLOSE,
        }
    )


@pytest.mark.parametrize(
    ("now", "expected"),
    [
        (datetime(2026, 5, 13, 15, 14, 59), date(2026, 5, 12)),
        (datetime(2026, 5, 13, 15, 15, 0), date(2026, 5, 13)),
        (datetime(2026, 5, 16, 12, 0, 0), date(2026, 5, 15)),
    ],
)
def test_latest_expected_daily_kline_date_uses_close_publish_buffer(
    tmp_path,
    monkeypatch: pytest.MonkeyPatch,
    now: datetime,
    expected: date,
) -> None:
    _use_calendar(
        tmp_path,
        monkeypatch,
        ["2026-05-11", "2026-05-12", "2026-05-13", "2026-05-14", "2026-05-15"],
    )

    assert trading_calendar.latest_expected_daily_kline_date(now) == expected


def test_trading_day_gap_combines_official_and_fallback_days(tmp_path, monkeypatch: pytest.MonkeyPatch) -> None:
    _use_calendar(tmp_path, monkeypatch, ["2025-12-31", "2026-01-05"])

    assert trading_calendar.trading_day_gap(date(2025, 12, 31), date(2026, 1, 7)) == 3


@pytest.mark.parametrize(
    "contents",
    [
        "{not-json",
        json.dumps({"updated_at": "2026-01-01 00:00:00", "trade_dates": []}),
        json.dumps({"updated_at": "2026-01-01 00:00:00", "trade_dates": ["bad-date"]}),
    ],
)
def test_damaged_or_empty_cache_falls_back_stably(tmp_path, monkeypatch: pytest.MonkeyPatch, contents: str) -> None:
    path = tmp_path / "trading_calendar.json"
    path.write_text(contents, encoding="utf-8")
    monkeypatch.setattr(trading_calendar, "CALENDAR_PATH", path)
    trading_calendar._trade_days.cache_clear()

    assert trading_calendar.is_trading_day(date(2026, 1, 5))
    assert not trading_calendar.is_trading_day(date(2026, 1, 4))
    assert trading_calendar.previous_trade_date(date(2026, 1, 4)) == date(2026, 1, 2)
    assert trading_calendar.calendar_source() == "工作日兜底"


def test_calendar_source_reports_expired_coverage_with_diagnostic_warning(
    tmp_path,
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
) -> None:
    _use_calendar(tmp_path, monkeypatch, ["2025-12-30", "2025-12-31"])

    class _CurrentDate(date):
        @classmethod
        def today(cls) -> date:
            return cls(2026, 1, 5)

    monkeypatch.setattr(trading_calendar, "date", _CurrentDate)
    with caplog.at_level(logging.WARNING, logger="app.services.trading_calendar"):
        source = trading_calendar.calendar_source()

    assert source == "工作日兜底"
    assert "未覆盖 2026-01-05" in caplog.text
    assert "覆盖 2025-12-30 至 2025-12-31" in caplog.text
    assert "更新于 2026-01-02 12:00:00" in caplog.text


def test_save_days_persists_coverage_bounds_and_updated_at(tmp_path, monkeypatch: pytest.MonkeyPatch) -> None:
    path = tmp_path / "trading_calendar.json"
    monkeypatch.setattr(trading_calendar, "CALENDAR_PATH", path)

    trading_calendar._save_days({date(2026, 1, 5), date(2025, 12, 31)})

    payload = json.loads(path.read_text(encoding="utf-8"))
    assert payload["min_date"] == "2025-12-31"
    assert payload["max_date"] == "2026-01-05"
    assert datetime.fromisoformat(payload["updated_at"])


def _use_calendar(tmp_path, monkeypatch: pytest.MonkeyPatch, trade_dates: list[str]) -> None:
    path = tmp_path / "trading_calendar.json"
    path.write_text(
        json.dumps(
            {
                "updated_at": "2026-01-02 12:00:00",
                "source": "test-calendar",
                "min_date": min(trade_dates),
                "max_date": max(trade_dates),
                "trade_dates": trade_dates,
            }
        ),
        encoding="utf-8",
    )
    monkeypatch.setattr(trading_calendar, "CALENDAR_PATH", path)
    trading_calendar._trade_days.cache_clear()
