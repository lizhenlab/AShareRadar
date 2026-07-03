from __future__ import annotations

from app.services.review import _review_events, build_individual_review
from tests.factories import make_kline, make_quote


def test_review_treats_non_positive_period_as_insufficient() -> None:
    review = build_individual_review(make_quote(price=108), _rows([100, 102, 104]), period_days=0)

    assert review.period_days == 0
    assert review.review_label == "数据不足"
    assert review.latest_close == 108


def test_review_filters_malformed_klines_before_metrics() -> None:
    rows = _rows([100, 102, 104, 106])
    rows.insert(2, make_kline(date="2026-05-20", close=0, high=120, low=0, volume=5000))

    review = build_individual_review(make_quote(price=106), rows, period_days=10)

    assert review.period_days == 4
    assert review.return_pct == 6.0
    assert "4个有效交易日" in review.review_summary


def test_review_uses_only_last_period_after_filtering() -> None:
    rows = _rows([100, 101, 102, 103, 104])

    review = build_individual_review(make_quote(price=104), rows, period_days=3)

    assert review.period_days == 3
    assert review.return_pct == 1.96


def test_review_event_rules_keep_price_move_priority_over_amplitude() -> None:
    rows = [
        make_kline(date="2026-05-01", close=100, high=101, low=99, volume=1000),
        make_kline(date="2026-05-02", close=95, high=105, low=94, volume=2000),
    ]

    events = _review_events(rows)

    assert len(events) == 1
    assert events[0].title == "明显回撤日"
    assert events[0].level == "风险"


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
    return [
        make_kline(
            date=f"2026-05-{index + 1:02d}",
            close=close,
            high=close + 1,
            low=max(0.01, close - 1),
            volume=1000,
        )
        for index, close in enumerate(closes)
    ]
