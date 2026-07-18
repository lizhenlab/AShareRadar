from __future__ import annotations

from datetime import datetime

from app.models.reviews import AdviceReviewPlan
from app.services.research_replay import (
    completed_daily_bar_cutoff,
    evaluate_advice_forward_window,
)
from tests.factories import make_kline


def _plan(
    *,
    snapshot_market_time: str = "2026-05-10 10:00:00",
    horizon_days: int = 3,
) -> AdviceReviewPlan:
    return AdviceReviewPlan(
        id=1,
        advice_id=1,
        symbol="600519.SH",
        snapshot_market_time=snapshot_market_time,
        snapshot_price=100,
        snapshot_adjustment_mode="qfq",
        snapshot_anchor_date="2026-05-08",
        snapshot_anchor_close=100,
        snapshot_data_version="snapshot-qfq-v1",
        snapshot_contract_version="daily-kline.v1",
        hypothesis="趋势延续",
        trigger_condition="价格确认",
        invalidation_condition="跌破止损",
        target_price=110,
        stop_price=95,
        horizon_days=horizon_days,
        evidence_refs=[],
        revision=1,
        created_at="2026-05-10 10:00:01",
        updated_at="2026-05-10 10:00:01",
    )


def _evaluate(plan: AdviceReviewPlan, rows, as_of: datetime):
    return evaluate_advice_forward_window(
        plan,
        rows,
        as_of=as_of,
        evaluated_at=as_of.strftime("%Y-%m-%d %H:%M:%S"),
    )


def test_missing_trading_day_cannot_be_replaced_by_a_later_barrier() -> None:
    rows = [
        make_kline(date="2026-05-08", close=100),
        make_kline(date="2026-05-11", close=101, high=102, low=99),
        make_kline(date="2026-05-13", close=111, high=112, low=100),
        make_kline(date="2026-05-14", close=103, high=104, low=100),
    ]

    draft = _evaluate(_plan(), rows, datetime(2026, 5, 14, 16))

    assert (draft.status, draft.conclusion) == ("insufficient", "insufficient_data")
    assert draft.available_forward_days == 1
    assert draft.forward_end_date == "2026-05-11"
    assert draft.target_hit is False


def test_barrier_after_a_missing_first_day_is_not_treated_as_certain() -> None:
    rows = [
        make_kline(date="2026-05-08", close=100),
        make_kline(date="2026-05-12", close=111, high=112, low=100),
    ]

    draft = _evaluate(_plan(), rows, datetime(2026, 5, 12, 16))

    assert (draft.status, draft.conclusion) == ("insufficient", "insufficient_data")
    assert draft.available_forward_days == 0
    assert draft.target_hit is False


def test_daily_bar_becomes_visible_only_at_publish_boundary() -> None:
    at_publish = datetime(2026, 5, 11, 15, 15)
    rows = [
        make_kline(date="2026-05-08", close=100),
        make_kline(date="2026-05-11", close=111, high=112, low=100),
    ]

    after = _evaluate(_plan(), rows, at_publish)

    for before_publish in (
        datetime(2026, 5, 11, 14, 59),
        datetime(2026, 5, 11, 15, 0),
        datetime(2026, 5, 11, 15, 14, 59),
    ):
        before = _evaluate(_plan(), rows, before_publish)
        assert completed_daily_bar_cutoff(before_publish).isoformat() == "2026-05-10"
        assert (before.status, before.conclusion) == ("pending", "pending")
    assert completed_daily_bar_cutoff(at_publish).isoformat() == "2026-05-11"
    assert (after.status, after.conclusion) == ("evaluated", "target_hit")


def test_cross_contract_evaluation_is_rejected_without_an_explicit_converter() -> None:
    rows = [
        make_kline(date="2026-05-08", close=100).model_copy(update={"contract_version": "daily-kline.v2"}),
        make_kline(date="2026-05-11", close=111, high=112, low=100).model_copy(update={"contract_version": "daily-kline.v2"}),
    ]

    draft = _evaluate(_plan(), rows, datetime(2026, 5, 11, 16))

    assert (draft.status, draft.conclusion) == ("insufficient", "insufficient_data")
    assert draft.evaluation_contract_version == "unknown"
    assert draft.target_hit is False


def test_weekend_without_an_expected_bar_remains_pending() -> None:
    plan = _plan(snapshot_market_time="2026-05-08 16:00:00")

    draft = _evaluate(plan, [], datetime(2026, 5, 10, 16))

    assert (draft.status, draft.conclusion) == ("pending", "pending")
    assert draft.available_forward_days == 0
