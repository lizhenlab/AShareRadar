from __future__ import annotations

from datetime import date

import pytest

from app.models.analysis import DataQuality
from app.models.market import Kline
from app.services.analysis import build_analysis
from app.services.research_factor_calibration import _calibrate_factor, _factor_percentile
from app.services.research_factor_current import build_current_factors
from app.services.research_factor_execution_contract import factor_calibration_evidence_issue
from app.services.research_factor_specs import _factor_specs, _fund_flow_proxy_score_at, _fund_flow_trigger
from app.services.research_features import build_feature_snapshot
from app.services.stock_activity import build_fund_flow_analysis
from app.services.stock_insights import build_stock_insight_bundle
from app.services.trading_calendar import next_trade_dates
from tests.factories import make_kline, make_quote


def _rows(*, count: int = 60, change: float = 1, body: float = -0.5, ratio: float = 1) -> list[Kline]:
    days = next_trade_dates(date(2026, 3, 31), count)
    rows = []
    for index, day in enumerate(days):
        close = 100 * (1 + change / 100) ** index
        row = make_kline(date=day.isoformat(), close=close, volume=1000, replay_eligible=True)
        rows.append(row.model_copy(update={"open": close + body}))
    if rows:
        rows[-1] = rows[-1].model_copy(update={"volume": 1000 * ratio})
    return rows


def _analysis(rows: list[Kline]):
    current, previous = rows[-1], rows[-2]
    quote = make_quote(
        price=current.close, prev_close=previous.close, high=current.high, low=current.low,
        change_pct=(current.close - previous.close) / previous.close * 100,
        timestamp=f"{current.date} 15:15:00",
    ).model_copy(update={"open": current.open, "volume": current.volume})
    return build_analysis(
        quote, rows,
        data_quality=DataQuality(level="优秀", source="合成PIT", quote_time=quote.timestamp, kline_count=len(rows), score=100),
    )


def _public_factor(rows: list[Kline]):
    analysis = _analysis(rows)
    insights = build_stock_insight_bundle(analysis)
    feature = build_feature_snapshot(analysis, insights)
    return next(item for item in build_current_factors(analysis, insights, feature) if item.id == "fund_flow_proxy")


@pytest.mark.parametrize("change", [-3, -1, 0, 1, 3])
@pytest.mark.parametrize("body", [-0.5, 0, 0.5])
@pytest.mark.parametrize("ratio", [0.5, 1, 1.25, 3.1])
def test_same_completed_price_volume_evidence_has_identical_current_and_historical_score(change, body, ratio):
    rows = _rows(change=change, body=body, ratio=ratio)
    report = build_fund_flow_analysis(_analysis(rows))
    assert factor_calibration_evidence_issue(rows) is None
    assert report.available is True
    assert _fund_flow_proxy_score_at(rows, len(rows) - 1) == report.overall_score
    assert report.score_rule_version == "current-price-volume.v2"


def test_unchanged_close_and_volume_have_the_same_historical_percentile_regardless_of_candle_body():
    positive_body = _public_factor(_rows(change=0, body=-0.5))
    negative_body = _public_factor(_rows(change=0, body=0.5))
    assert positive_body.score == negative_body.score == 50
    assert positive_body.percentile == negative_body.percentile == 100
    assert positive_body.calibration.sample_count == negative_body.calibration.sample_count == 3


def test_monotonic_downward_price_history_is_found_as_current_negative_state():
    factor = _public_factor(_rows(change=-1, body=-0.5))
    assert factor.score == 45
    assert factor.calibration.availability == "available"
    assert factor.calibration.sample_count == 3
    assert factor.calibration.win_rate == 0


def test_historical_signal_score_and_trigger_ignore_all_future_rows():
    rows = _rows(count=36, change=-1, body=-0.5)
    index = len(rows) - 1
    future = [rows[-1].model_copy(update={"date": None, "close": float("nan"), "volume": 1e100})]
    before = _fund_flow_proxy_score_at(rows, index)
    assert before == 45
    assert _fund_flow_proxy_score_at(rows + future, index) == before
    assert _fund_flow_trigger(rows + future, index, before) is True


def test_historical_scoring_does_not_read_or_require_a_current_quote():
    rows = _rows(count=6, change=3, ratio=1.8)
    assert _fund_flow_proxy_score_at(rows, 5) == 69


@pytest.mark.parametrize("index", [-1, 0, 4, 6, 100])
def test_historical_score_rejects_incomplete_or_out_of_range_windows(index):
    with pytest.raises(ValueError, match="量价"):
        _fund_flow_proxy_score_at(_rows(count=6), index)
    assert _fund_flow_trigger(_rows(count=6), index, 50) is False


@pytest.mark.parametrize("offset", [-1, -2, -5, -6])
@pytest.mark.parametrize("volume", [None, 0, -1, float("nan"), float("inf")])
def test_historical_score_cannot_skip_invalid_volume_and_refill_from_older_rows(offset, volume):
    rows = _rows()
    rows[offset] = rows[offset].model_copy(update={"volume": volume})
    with pytest.raises(ValueError, match="量价"):
        _fund_flow_proxy_score_at(rows, len(rows) - 1)
    assert _fund_flow_trigger(rows, len(rows) - 1, 50) is False


@pytest.mark.parametrize("updates", [
    {"date": ""}, {"date": None}, {"date": "2026-02-30"}, {"point_in_time": False},
    {"as_of": "2026-12-31 15:15:00"}, {"as_of": None}, {"fallback_used": True},
    {"session_status": "unknown"}, {"execution_metadata_version": None},
    {"adjustment_mode": "none"}, {"high": float("nan")},
])
def test_historical_score_rejects_undated_unobserved_or_incomparable_rows(updates):
    rows = _rows()
    rows[-3] = rows[-3].model_copy(update=updates)
    with pytest.raises(ValueError, match="量价"):
        _fund_flow_proxy_score_at(rows, len(rows) - 1)


def test_historical_score_rejects_duplicate_or_missing_trading_sessions():
    rows = _rows()
    duplicate = rows.copy()
    duplicate[-3] = duplicate[-2]
    missing = rows[:-3] + rows[-2:]
    for changed in (duplicate, missing):
        with pytest.raises(ValueError, match="量价"):
            _fund_flow_proxy_score_at(changed, len(changed) - 1)


@pytest.mark.parametrize("offset", [-1, -2, -6])
def test_corporate_action_cannot_create_a_return_from_unproved_cross_date_price_basis(offset):
    rows = _rows()
    rows[offset] = rows[offset].model_copy(update={"corporate_action_status": "effective_event", "adjustment_factor": 2})
    assert factor_calibration_evidence_issue(rows) is None
    with pytest.raises(ValueError, match="量价"):
        _fund_flow_proxy_score_at(rows, len(rows) - 1)


def test_suspension_is_not_converted_to_neutral_similarity_sample():
    rows = _rows(count=36, change=0)
    rows[23] = rows[23].model_copy(update={"session_status": "suspended", "open_execution_status": "unavailable", "volume": 0})
    spec = _factor_specs()["fund_flow_proxy"]
    assert factor_calibration_evidence_issue(rows) is None
    assert _calibrate_factor(rows, spec, 50).sample_count == 0
    assert _fund_flow_trigger(rows, 25, 50) is False


def test_factor_registration_pins_the_current_and_historical_rule_version():
    assert _factor_specs()["fund_flow_proxy"].score_rule_version == "current-price-volume.v2"


def test_percentile_skips_invalid_historical_windows_instead_of_making_neutral_samples():
    rows = _rows(count=30, change=0)
    for index in (19, 24):
        rows[index] = rows[index].model_copy(update={"session_status": "suspended", "open_execution_status": "unavailable", "volume": 0})
    spec = _factor_specs()["fund_flow_proxy"]
    assert factor_calibration_evidence_issue(rows) is None
    assert _factor_percentile(rows, spec.evaluator, 50) is None
