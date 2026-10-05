from __future__ import annotations

from datetime import date

import pytest

from app.models.analysis import AnalysisResult, FeatureSnapshot
from app.services.analysis import build_analysis
from app.services.research_factor_current import trend_momentum_factor, volume_confirmation_factor
from app.services.research_factor_specs import _factor_specs
from app.services.research_features import build_feature_snapshot
from app.services.stock_insights import build_stock_insight_bundle
from app.services.trading_calendar import TradingCalendarCoverageError, next_trade_dates
from tests.factories import make_kline, make_quote


def _inputs() -> tuple[AnalysisResult, FeatureSnapshot]:
    dates = next_trade_dates(date(2026, 1, 1), 80)
    rows = [make_kline(date=day.isoformat(), close=100 + index * 0.1, volume=1000, replay_eligible=True)
            for index, day in enumerate(dates)]
    rows[-1] = make_kline(date=rows[-1].date, close=rows[-2].close * 1.02, volume=4000, replay_eligible=True)
    current, previous = rows[-1], rows[-2]
    quote = make_quote(price=current.close, prev_close=previous.close, high=current.high, low=current.low,
                       change_pct=2, turnover_rate=4, timestamp=f"{current.date} 15:15:00").model_copy(
                           update={"open": current.open, "volume": current.volume})
    analysis = build_analysis(quote, rows)
    return analysis, build_feature_snapshot(analysis, build_stock_insight_bundle(analysis))


def _volume(analysis: AnalysisResult, feature: FeatureSnapshot):
    return volume_confirmation_factor(analysis, feature, _factor_specs(), {})


def _assert_unavailable(analysis: AnalysisResult, feature: FeatureSnapshot) -> None:
    factor = _volume(analysis, feature)
    assert factor.score == 50 and factor.data_nature == "unavailable"
    assert not factor.participates_in_current_score
    assert factor.percentile is None and factor.calibration_buckets == []
    assert factor.calibration is not None and not factor.calibration.participates_in_historical_aggregate


def test_live_trend_keeps_current_value_without_unreplayable_historical_evidence(monkeypatch: pytest.MonkeyPatch) -> None:
    analysis, feature = _inputs()
    monkeypatch.setattr("app.services.research_factor_scoring._factor_percentile", lambda *_: pytest.fail("unreplayable percentile"))
    factor = trend_momentum_factor(analysis, feature, _factor_specs(), {})

    assert factor.score == feature.trend_score and factor.participates_in_current_score
    assert factor.percentile is None and factor.calibration_buckets == []
    assert factor.score_rule_version == "factor-current-trend.v1"
    assert factor.calibration is not None
    assert factor.calibration.sample_count == 0 and not factor.calibration.participates_in_historical_aggregate
    assert factor.calibration.availability == "execution_evidence_unavailable"
    assert "换手" in (factor.methodology or "") and "时点" in (factor.calibration.unavailable_reason or "")


@pytest.mark.parametrize("mode", ["intraday", "preopen"])
def test_volume_cannot_pair_prior_completed_volume_with_a_live_return(mode: str) -> None:
    analysis, feature = _inputs()
    _assert_unavailable(analysis.model_copy(update={"research_mode": mode}), feature)


@pytest.mark.parametrize("clock", ["10:00:00", "14:59:59", "", "bad"])
def test_volume_requires_explicit_after_close_quote_time(clock: str) -> None:
    analysis, feature = _inputs()
    timestamp = f"{analysis.klines[-1].date} {clock}".strip()
    _assert_unavailable(analysis.model_copy(update={"quote": analysis.quote.model_copy(update={"timestamp": timestamp})}), feature)


@pytest.mark.parametrize("changes", [{"price": 100}, {"prev_close": 100}, {"price": float("inf")}, {"prev_close": 0}])
def test_volume_requires_matching_finite_quote_and_bar_prices(changes: dict) -> None:
    analysis, feature = _inputs()
    _assert_unavailable(analysis.model_copy(update={"quote": analysis.quote.model_copy(update=changes)}), feature)


@pytest.mark.parametrize("change", ["prior_day", "future_day", "duplicate", "unsorted", "invalid_date", "missing_volume", "invalid_bar", "short"])
def test_volume_rejects_incomplete_or_misaligned_windows_without_filtering(change: str) -> None:
    analysis, feature = _inputs()
    rows = list(analysis.klines)
    if change == "prior_day":
        rows = rows[:-1]
    elif change == "future_day":
        rows[-1] = rows[-1].model_copy(update={"date": "2099-01-01"})
    elif change == "duplicate":
        rows[-6] = rows[-6].model_copy(update={"date": rows[-7].date})
    elif change == "unsorted":
        rows[-6], rows[-7] = rows[-7], rows[-6]
    elif change == "invalid_date":
        rows[-6] = rows[-6].model_copy(update={"date": "2026-02-30"})
    elif change == "missing_volume":
        rows[-6] = rows[-6].model_copy(update={"volume": 0})
    elif change == "invalid_bar":
        rows[-6] = rows[-6].model_copy(update={"high": 0})
    else:
        rows = rows[-19:]
    _assert_unavailable(analysis.model_copy(update={"klines": rows}), feature)


def test_volume_feature_missing_flag_remains_unavailable() -> None:
    analysis, feature = _inputs()
    _assert_unavailable(analysis, feature.model_copy(update={"volume_ratio_available": False}))


def test_current_volume_rebuilds_bar_return_and_ratio_for_exact_replay() -> None:
    analysis, feature = _inputs()
    changed = analysis.model_copy(update={"quote": analysis.quote.model_copy(update={"change_pct": -90})})
    factor = _volume(changed, feature.model_copy(update={"volume_ratio": 0.01, "change_pct": -90}))
    spec = _factor_specs()["volume_confirmation"]

    assert factor.participates_in_current_score
    assert factor.score == spec.evaluator(analysis.klines, len(analysis.klines) - 1) == 63
    assert "1.39倍" in factor.value and "2.00%" in factor.value
    assert factor.score_rule_version == factor.calibration.score_rule_version == "factor-volume-confirmation.v3"


@pytest.mark.parametrize("suffix", [" 15:00:00", "T07:00:00Z", "T15:00:00+08:00"])
def test_local_and_offset_close_times_share_the_same_admission(suffix: str) -> None:
    analysis, feature = _inputs()
    quote = analysis.quote.model_copy(update={"timestamp": analysis.klines[-1].date + suffix})
    assert _volume(analysis.model_copy(update={"quote": quote}), feature).participates_in_current_score


def test_floating_price_noise_is_admitted_without_changing_bar_based_score() -> None:
    analysis, feature = _inputs()
    quote = analysis.quote.model_copy(update={"price": analysis.quote.price + 1e-10, "prev_close": analysis.quote.prev_close - 1e-10})
    adjusted = _volume(analysis.model_copy(update={"quote": quote}), feature)
    assert adjusted.participates_in_current_score
    assert adjusted.score == _volume(analysis, feature).score


def test_finite_prices_with_overflowing_return_are_unavailable() -> None:
    analysis, feature = _inputs()
    rows = list(analysis.klines)
    rows[-2] = make_kline(date=rows[-2].date, close=1, high=2, low=0.1, replay_eligible=True)
    rows[-1] = rows[-1].model_copy(update={"close": 1e308, "high": 1e308})
    quote = analysis.quote.model_copy(update={"price": 1e308, "prev_close": 1})
    _assert_unavailable(analysis.model_copy(update={"quote": quote, "klines": rows}), feature)


def test_raw_bar_outside_the_twenty_row_window_is_not_backfilled_for_invalid_recent_bar() -> None:
    analysis, feature = _inputs()
    rows = list(analysis.klines)
    rows[-20] = rows[-20].model_copy(update={"volume": float("inf")})
    _assert_unavailable(analysis.model_copy(update={"klines": rows}), feature)


def test_upstream_invalid_bar_filtering_does_not_hide_a_missing_volume_session() -> None:
    analysis, _feature = _inputs()
    raw = list(analysis.klines)
    raw[-6] = raw[-6].model_copy(update={"high": 0})
    filtered = build_analysis(analysis.quote, raw)
    feature = build_feature_snapshot(filtered, build_stock_insight_bundle(filtered))

    assert len(filtered.klines) == len(raw) - 1
    assert feature.volume_ratio_available
    _assert_unavailable(filtered, feature)


def test_missing_middle_session_cannot_be_replaced_by_an_older_bar() -> None:
    analysis, feature = _inputs()
    rows = list(analysis.klines)
    rows.pop(-6)
    assert len(rows) >= 20
    _assert_unavailable(analysis.model_copy(update={"klines": rows}), feature)


def test_calendar_out_of_coverage_makes_current_volume_unavailable(monkeypatch: pytest.MonkeyPatch) -> None:
    analysis, feature = _inputs()

    def unavailable_calendar(*_args):
        raise TradingCalendarCoverageError("test calendar does not cover window")

    monkeypatch.setattr("app.services.research_volume_scoring.trading_dates_between", unavailable_calendar)
    _assert_unavailable(analysis, feature)
