from __future__ import annotations

from datetime import date

import pytest

from app.services.analysis import build_analysis
from app.services.research_factor_calibration import _calibrate_factor, _calibration_sample_at, _factor_percentile
from app.services.research_factor_current import volume_confirmation_factor
from app.services.research_factor_execution_contract import factor_calibration_evidence_issue
from app.services.research_factor_specs import _factor_specs
from app.services.research_features import build_feature_snapshot
from app.services.research_volume_scoring import volume_confirmation_inputs
from app.services.stock_insights import build_stock_insight_bundle
from app.services.trading_calendar import next_trade_dates
from tests.factories import make_kline, make_quote


def _rows(count=60):
    return [make_kline(date=day.isoformat(), close=100, volume=1000, replay_eligible=True)
            .model_copy(update={"open": 100}) for day in next_trade_dates(date(2026, 1, 1), count)]


def _current(rows):
    last, previous = rows[-1], rows[-2]
    quote = make_quote(price=last.close, prev_close=previous.close, high=last.high, low=last.low,
                       timestamp=f"{last.date} 15:15:00").model_copy(update={"open": last.open, "volume": last.volume})
    analysis = build_analysis(quote, rows)
    feature = build_feature_snapshot(analysis, build_stock_insight_bundle(analysis))
    return volume_confirmation_factor(analysis, feature, _factor_specs(), {})


def test_split_on_signal_date_cannot_become_a_bearish_volume_calibration_sample():
    rows = _rows(48)
    for index in range(25, len(rows)):
        rows[index] = rows[index].model_copy(update={"open": 50, "close": 50, "high": 51, "low": 49})
    rows[25] = rows[25].model_copy(update={"corporate_action_status": "effective_event", "adjustment_factor": 2})
    spec = _factor_specs()["volume_confirmation"]

    assert factor_calibration_evidence_issue(rows) is None
    with pytest.raises(ValueError, match="20日正成交量窗口"):
        spec.evaluator(rows, 25)
    assert spec.trigger(rows, 25, 10) is False
    assert _calibration_sample_at(rows, spec, 10, 25) is None
    calibration = _calibrate_factor(rows, spec, 10)
    assert calibration.sample_count == 0
    assert calibration.participates_in_historical_aggregate is False


@pytest.mark.parametrize("offset", range(20))
@pytest.mark.parametrize("updates", [
    {"corporate_action_status": "effective_event", "adjustment_factor": 2},
    {"corporate_action_status": "unknown"},
    {"session_status": "unknown"},
    {"adjustment_mode": "none"},
])
def test_current_and_replay_refuse_unproven_price_or_volume_basis_at_any_window_position(offset, updates):
    rows = _rows()
    rows[-20 + offset] = rows[-20 + offset].model_copy(update=updates)
    spec = _factor_specs()["volume_confirmation"]

    current = _current(rows)
    assert current.participates_in_current_score is False
    assert current.score == 50 and current.percentile is None
    assert current.calibration_buckets == []
    with pytest.raises(ValueError, match="20日正成交量窗口"):
        spec.evaluator(rows, len(rows) - 1)


@pytest.mark.parametrize("kind", ["missing", "duplicate", "unsorted", "bad_date", "non_iso_date", "zero_volume", "invalid_bar"])
def test_historical_window_cannot_filter_or_refill_invalid_sessions(kind):
    rows = _rows()
    if kind == "missing":
        rows.pop(-6)
    elif kind == "duplicate":
        rows[-6] = rows[-7]
    elif kind == "unsorted":
        rows[-6], rows[-7] = rows[-7], rows[-6]
    elif kind == "non_iso_date":
        rows[-6] = rows[-6].model_copy(update={"date": rows[-6].date.replace("-", "")})
    else:
        updates = {"bad_date": {"date": "2026-02-30"}, "zero_volume": {"volume": 0}, "invalid_bar": {"high": 0}}
        rows[-6] = rows[-6].model_copy(update=updates[kind])
    spec = _factor_specs()["volume_confirmation"]
    with pytest.raises(ValueError, match="20日正成交量窗口"):
        spec.evaluator(rows, len(rows) - 1)
    assert spec.trigger(rows, len(rows) - 1, 50) is False


def test_company_action_only_affects_its_fixed_twenty_session_feature_windows():
    rows = _rows()
    rows[18] = rows[18].model_copy(update={"corporate_action_status": "effective_event", "adjustment_factor": 2})
    spec = _factor_specs()["volume_confirmation"]
    with pytest.raises(ValueError):
        spec.evaluator(rows, 37)
    assert spec.evaluator(rows, 38) == 50
    assert _current(rows).participates_in_current_score is True
    assert _calibrate_factor(rows, spec, 50).sample_count == 2


def test_unadmitted_action_windows_do_not_enter_percentile_history():
    rows = _rows(40)
    rows[18] = rows[18].model_copy(update={"corporate_action_status": "effective_event", "adjustment_factor": 2})
    spec = _factor_specs()["volume_confirmation"]
    assert _factor_percentile(rows[:38], spec.evaluator, 50) is None
    assert _factor_percentile(rows, spec.evaluator, 50) == 100


def test_clean_current_and_replay_scores_remain_equal_under_new_admission_version():
    rows = _rows()
    rows[-1] = rows[-1].model_copy(update={"open": 102, "close": 102, "high": 103, "low": 101, "volume": 4000})
    spec = _factor_specs()["volume_confirmation"]
    current = _current(rows)
    assert current.participates_in_current_score
    assert current.score == spec.evaluator(rows, len(rows) - 1) == 62
    assert current.score_rule_version == current.calibration.score_rule_version == "factor-volume-confirmation.v3"
    future = rows + [rows[-1].model_copy(update={"date": "bad", "volume": 0})]
    assert spec.evaluator(future, len(rows) - 1) == current.score


@pytest.mark.parametrize("count", [0, 1, 19, 21])
def test_shared_admission_requires_exactly_twenty_sessions(count):
    assert volume_confirmation_inputs(_rows(max(1, count))[:count]) is None


def test_current_basis_admission_does_not_invent_historical_pit_provenance():
    rows = _rows()
    rows[-6] = rows[-6].model_copy(update={"point_in_time": False})
    factor = _current(rows)
    assert factor.participates_in_current_score is True
    assert factor.score == 50
    assert factor.calibration.availability == "execution_evidence_unavailable"
    assert factor.calibration.sample_count == 0 and factor.percentile is None


def test_positive_volume_ratio_rounded_to_zero_is_unavailable_without_crashing_current_report():
    rows = _rows(20)
    rows = [row.model_copy(update={"volume": 100_000_000 if index < 15 else 100})
            for index, row in enumerate(rows)]
    factor = _current(rows)
    assert volume_confirmation_inputs(rows) is None
    assert factor.participates_in_current_score is False
    assert factor.score == 50 and factor.percentile is None
    assert factor.calibration_buckets == []
    assert factor.calibration.participates_in_historical_aggregate is False
    spec = _factor_specs()["volume_confirmation"]
    with pytest.raises(ValueError, match="20日正成交量窗口"):
        spec.evaluator(rows, len(rows) - 1)
    assert spec.trigger(rows, len(rows) - 1, 50) is False
