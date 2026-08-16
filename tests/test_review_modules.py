from __future__ import annotations

from datetime import date, datetime

from app.services.review import _review_events, build_individual_review
from app.services.trading_calendar import next_trade_dates
from tests.factories import make_kline, make_quote


def test_review_treats_non_positive_period_as_insufficient() -> None:
    review = build_individual_review(make_quote(price=108), _rows([100, 102, 104]), period_days=0)

    assert review.period_days == 0
    assert review.review_label == "数据不足"
    assert review.latest_close == 108


def test_review_rejects_malformed_kline_instead_of_changing_the_denominator() -> None:
    rows = _rows([100, 102, 104, 106])
    rows[2] = rows[2].model_copy(update={"close": 0, "low": 0})

    review = build_individual_review(make_quote(price=106), rows, period_days=10)

    assert review.period_days == 0
    assert review.return_pct == 0
    assert "执行证据不可用" in review.review_summary


def test_review_uses_only_last_period_after_filtering() -> None:
    rows = _rows([100, 101, 102, 103, 104])

    review = build_individual_review(make_quote(price=104), rows, period_days=3)

    assert review.period_days == 3
    assert review.return_pct == 1.96


def test_review_rejects_future_quote_or_decision_cutoff() -> None:
    rows = _rows([100, 101, 102])
    trusted_now = datetime(2026, 8, 1, 16)

    future_quote = build_individual_review(
        make_quote(price=102, timestamp="2099-01-01 15:00:00"),
        rows,
        now=trusted_now,
    )
    future_as_of = build_individual_review(
        make_quote(price=102, timestamp="2026-05-13 15:00:00"),
        rows,
        as_of=datetime(2099, 1, 1, 16),
        now=trusted_now,
    )

    assert future_quote.review_label == "数据不足"
    assert future_as_of.review_label == "数据不足"
    assert "截止时间不可验证" in future_quote.review_summary


def test_review_event_rules_keep_price_move_priority_over_amplitude() -> None:
    rows = [
        make_kline(date="2026-05-01", close=100, high=101, low=99, volume=1000),
        make_kline(date="2026-05-02", close=95, high=105, low=94, volume=2000),
    ]

    events = _review_events(rows)

    assert len(events) == 1
    assert events[0].title == "明显回撤日"
    assert events[0].level == "风险"


def test_review_volume_attack_requires_a_real_five_day_volume_surge() -> None:
    rows = [
        make_kline(date=f"2026-05-{day:02d}", close=100, high=101, low=99, volume=1000)
        for day in range(1, 6)
    ]

    confirmed = _review_events(
        [*rows, make_kline(date="2026-05-06", close=105, high=106, low=104, volume=1600)]
    )
    unconfirmed = _review_events(
        [*rows, make_kline(date="2026-05-06", close=105, high=106, low=104, volume=1200)]
    )

    assert confirmed[-1].title == "放量上攻日"
    assert "1.60 倍" in confirmed[-1].description
    assert unconfirmed[-1].title == "明显上涨日"
    assert "未达到放量确认" in unconfirmed[-1].description


def test_review_events_ignore_malformed_bars_and_keep_latest_limit() -> None:
    rows = [make_kline(date="2026-05-01", close=100, high=101, low=99, volume=1000)]
    rows.append(make_kline(date="2026-05-02", close=0, high=110, low=0, volume=1000))
    close = 100.0
    for index in range(10):
        close *= 1.05
        rows.append(make_kline(date=f"2026-05-{index + 3:02d}", close=close, high=close + 1, low=close - 1, volume=1000))

    events = _review_events(rows)

    assert len(events) == 8
    assert all(event.date != "2026-05-02" for event in events)
    assert events[-1].date == "2026-05-12"


def _rows(closes: list[float]):
    dates = next_trade_dates(date(2026, 2, 27), len(closes))
    return [
        make_kline(
            date=dates[index].isoformat(),
            close=close,
            high=close + 1,
            low=max(0.01, close - 1),
            volume=1000,
            replay_eligible=True,
        )
        for index, close in enumerate(closes)
    ]
