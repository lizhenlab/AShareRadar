from __future__ import annotations

from datetime import date

import pytest

from app.services.research_execution_model import MODELLED_ROUND_TRIP_FRICTION_PCT
from app.services.research_factor_calibration import _calibrate_factor, _calibration_buckets, _calibration_sample_at
from app.services.research_factor_execution_contract import factor_calibration_evidence_issue
from app.services.research_factor_specs import FactorSpec
from app.services.trading_calendar import next_trade_dates
from tests.factories import make_kline


SIGNAL_INDEX = 25


def _rows(count: int = 48):
    days = next_trade_dates(date(2026, 3, 31), count)
    return [
        make_kline(date=day.isoformat(), close=100, high=101, low=99, volume=1000, replay_eligible=True).model_copy(update={"open": 100})
        for day in days
    ]


def _spec(*, trigger_index: int = SIGNAL_INDEX):
    return FactorSpec(
        id="label_basis_test", name="价格基准测试", category="测试", weight=1,
        direction="正向", evaluator=lambda *_: 60,
        trigger=lambda _rows, index, _score: index == trigger_index,
    )


def _with_action(rows, action_index: int, *, price_halves: bool = False):
    rows = rows.copy()
    if price_halves:
        for index in range(action_index, len(rows)):
            rows[index] = rows[index].model_copy(update={"open": 50, "close": 50, "high": 51, "low": 49})
    rows[action_index] = rows[action_index].model_copy(update={"corporate_action_status": "effective_event", "adjustment_factor": 2})
    return rows


def test_daily_pit_qfq_and_an_adjustment_number_do_not_prove_cross_date_return_basis():
    rows = _with_action(_rows(), SIGNAL_INDEX + 3, price_halves=True)
    assert factor_calibration_evidence_issue(rows) is None
    # The previous arithmetic reported roughly -50% after a two-for-one action.
    # The contract does not identify a shared price anchor or how factor=2 was applied.
    assert _calibration_sample_at(rows, _spec(), 60, SIGNAL_INDEX) is None


@pytest.mark.parametrize("offset", range(1, 11))
def test_action_anywhere_from_entry_through_day_ten_rejects_the_entire_sample(offset):
    rows = _with_action(_rows(), SIGNAL_INDEX + offset)
    assert factor_calibration_evidence_issue(rows) is None
    assert _calibration_sample_at(rows, _spec(), 60, SIGNAL_INDEX) is None


def test_rejected_action_sample_is_excluded_from_public_calibration_and_every_bucket():
    rows = _with_action(_rows(), SIGNAL_INDEX + 7)
    calibration = _calibrate_factor(rows, _spec(), 60)
    assert calibration.sample_count == 0
    assert calibration.availability == "execution_evidence_unavailable"
    assert calibration.participates_in_historical_aggregate is False
    assert "公司行动" in calibration.unavailable_reason
    assert _calibration_buckets(rows, _spec(), 60) == []


@pytest.mark.parametrize("offset", [2, 3, 6, 9])
@pytest.mark.parametrize("updates", [
    {"high": float("nan")}, {"low": 0}, {"volume": 0}, {"point_in_time": False},
    {"date": ""}, {"as_of": None}, {"corporate_action_status": "unknown"},
])
def test_an_invalid_intermediate_session_cannot_be_skipped_to_form_returns(offset, updates):
    rows = _rows()
    rows[SIGNAL_INDEX + offset] = rows[SIGNAL_INDEX + offset].model_copy(update=updates)
    assert _calibration_sample_at(rows, _spec(), 60, SIGNAL_INDEX) is None


def test_missing_middle_session_cannot_shift_five_and_ten_day_targets():
    rows = _rows()
    rows.pop(SIGNAL_INDEX + 3)
    assert _calibration_sample_at(rows, _spec(), 60, SIGNAL_INDEX) is None


def test_duplicated_middle_session_is_not_a_distinct_target_day():
    rows = _rows()
    rows[SIGNAL_INDEX + 3] = rows[SIGNAL_INDEX + 2]
    assert _calibration_sample_at(rows, _spec(), 60, SIGNAL_INDEX) is None


def test_a_valid_suspended_middle_session_is_counted_without_using_carried_ohlc_as_a_touch():
    rows = _rows()
    rows[SIGNAL_INDEX + 3] = rows[SIGNAL_INDEX + 3].model_copy(update={
        "session_status": "suspended", "open_execution_status": "unavailable", "volume": 0,
        "open": 1, "close": 1, "high": 200, "low": 0.5,
    })
    rows[SIGNAL_INDEX + 5] = rows[SIGNAL_INDEX + 5].model_copy(update={"open": 110, "close": 110, "high": 111, "low": 109})
    rows[SIGNAL_INDEX + 10] = rows[SIGNAL_INDEX + 10].model_copy(update={"open": 120, "close": 120, "high": 121, "low": 119})
    assert factor_calibration_evidence_issue(rows) is None
    sample = _calibration_sample_at(rows, _spec(), 60, SIGNAL_INDEX)
    assert sample is not None
    assert sample.forward_5d == pytest.approx(10 - MODELLED_ROUND_TRIP_FRICTION_PCT)
    assert sample.forward_10d == pytest.approx(20 - MODELLED_ROUND_TRIP_FRICTION_PCT)
    assert sample.adverse_return == pytest.approx(-1 - MODELLED_ROUND_TRIP_FRICTION_PCT)


def test_action_before_entry_does_not_invalidate_a_later_unchanged_price_basis():
    rows = _with_action(_rows(), SIGNAL_INDEX)
    assert _calibration_sample_at(rows, _spec(), 60, SIGNAL_INDEX) is not None


def test_action_beyond_the_ten_day_target_does_not_change_a_completed_label():
    rows = _rows()
    baseline = _calibration_sample_at(rows, _spec(), 60, SIGNAL_INDEX)
    changed = _with_action(rows, SIGNAL_INDEX + 11, price_halves=True)
    assert _calibration_sample_at(changed, _spec(), 60, SIGNAL_INDEX) == baseline


def test_invalid_future_suffix_does_not_change_the_sample_or_fixed_horizon():
    rows = _rows()
    baseline = _calibration_sample_at(rows, _spec(), 60, SIGNAL_INDEX)
    changed = rows[: SIGNAL_INDEX + 11] + [rows[-1].model_copy(update={"date": "", "high": float("nan")})]
    assert _calibration_sample_at(changed, _spec(), 60, SIGNAL_INDEX) == baseline


def test_nonmatching_signal_remains_no_similar_sample_instead_of_an_action_claim():
    rows = _with_action(_rows(), SIGNAL_INDEX + 3)
    calibration = _calibrate_factor(rows, _spec(trigger_index=1000), 60)
    assert calibration.sample_count == 0
    assert calibration.availability == "no_similar_samples"


def test_clean_later_samples_remain_available_after_an_action_window_has_ended():
    rows = _with_action(_rows(count=65), SIGNAL_INDEX + 3)
    calibration = _calibrate_factor(rows, _spec(trigger_index=40), 60)
    assert calibration.sample_count == 1
    assert calibration.availability == "available"
    assert calibration.win_rate == 0
