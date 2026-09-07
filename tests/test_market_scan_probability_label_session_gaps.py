"""A missing previous exchange session must not borrow an older close."""

from __future__ import annotations

from datetime import date

import pytest

from app.models.market import Kline
from app.services.market_scan_probability_labels import ProbabilityLabelConfig, build_probability_label_outcomes
from app.services.trading_calendar import next_trade_dates


def _bar(day: str, price: float) -> Kline:
    return Kline(
        date=day, open=price, close=price, high=price, low=price, volume=10000,
        adjustment_mode="qfq", as_of=day, data_version="test-v1",
    )


def _outcome(rows: list[Kline], dates: tuple[str, ...], horizon: int):
    return build_probability_label_outcomes(
        symbol="600001.SH", market="SH", list_date="2020-01-02", is_st=False,
        quote_date="2026-01-05", amount=1e9, rows=rows, eligible_dates=dates,
        config=ProbabilityLabelConfig(horizons=(horizon,)),
    )[horizon]


@pytest.mark.parametrize("horizon", [2, 5, 20])
def test_missing_pre_exit_session_cannot_produce_a_positive_label(horizon):
    dates = tuple(day.isoformat() for day in next_trade_dates(date(2026, 1, 5), horizon + 1))
    rows = [_bar("2026-01-05", 100)] + [_bar(day, 100) for day in dates]
    rows[-2] = _bar(dates[-2], 120)
    rows[-1] = _bar(dates[-1], 108)
    locked = _outcome(rows, dates, horizon)
    assert locked.status == "unfilled"
    assert locked.reason == "locked_limit_down"
    missing_previous = _outcome(rows[:-2] + rows[-1:], dates, horizon)
    assert missing_previous.status == "data_unavailable"
    assert missing_previous.reason == "exit_or_previous_bar_missing"
    assert missing_previous.label is None
    assert missing_previous.net_return is None
    assert missing_previous.exit_date == dates[-1]


def test_missing_signal_session_cannot_borrow_a_stale_entry_limit_reference():
    dates = ("2026-01-06", "2026-01-07")
    rows = [_bar("2025-12-31", 80), _bar(dates[0], 100), _bar(dates[1], 105)]
    outcome = _outcome(rows, dates, 1)
    assert outcome.status == "data_unavailable"
    assert outcome.reason == "entry_or_previous_bar_missing"
    assert outcome.label is None


def test_non_session_extra_bar_cannot_replace_the_exact_exit_reference():
    dates = ("2026-01-06", "2026-01-07", "2026-01-08", "2026-01-09", "2026-01-12")
    rows = [_bar("2026-01-05", 100)] + [_bar(day, 100) for day in dates[:-1]]
    rows.extend([_bar("2026-01-11", 120), _bar(dates[-1], 108)])
    outcome = _outcome(rows, dates, 4)
    assert outcome.status == "modelled"
    assert outcome.label == 1
