from __future__ import annotations

from datetime import date
from types import SimpleNamespace

import pytest

from app.models.market import Kline
from app.services.analysis import build_analysis
from app.services.indicator_volume import recent_volume_ratio_if_available
from app.services.indicators import pct_change
from app.services.research_factor_calibration import _factor_percentile
from app.services.research_factor_current import build_current_factors
from app.services.research_factor_execution_contract import factor_calibration_evidence_issue
from app.services.research_factor_scoring import _volume_confirmation_score
from app.services.research_factor_specs import _factor_specs, _volume_proxy_score_at, _volume_trigger
from app.services.research_features import build_feature_snapshot
from app.services.stock_insights import build_stock_insight_bundle
from app.services.trading_calendar import next_trade_dates
from tests.factories import make_kline, make_quote


@pytest.mark.parametrize("change_pct", [-2.01, -2.0, -1.99, 0.0, 1.99, 2.0, 2.01])
@pytest.mark.parametrize(
    "raw_ratio, rounded_ratio",
    [
        (0.6949, 0.69), (0.6951, 0.70), (0.8449, 0.84), (0.8451, 0.85),
        (1.1949, 1.19), (1.1951, 1.20), (1.199, 1.20), (1.20, 1.20),
        (1.2049, 1.20), (1.2051, 1.21), (1.2549, 1.25), (1.2551, 1.26), (2.50, 2.50),
    ],
)
def test_historical_volume_score_matches_current_rounding_and_rule_boundaries(
    change_pct: float, raw_ratio: float, rounded_ratio: float,
) -> None:
    rows = _window_with_ratio(raw_ratio, change_pct=change_pct)

    assert recent_volume_ratio_if_available(rows) == rounded_ratio
    assert _volume_proxy_score_at(rows, len(rows) - 1) == _current_score(rows)


@pytest.mark.parametrize("change_pct, expected", [(2.0, 70), (-2.0, 34), (0.0, 56)])
def test_ratio_1199_uses_the_same_observed_120_rule_in_both_paths(change_pct: float, expected: int) -> None:
    rows = _window_with_ratio(1.199, change_pct=change_pct)

    assert _current_score(rows) == expected
    assert _volume_proxy_score_at(rows, len(rows) - 1) == expected


def test_historical_volume_score_starts_with_the_same_twenty_observed_sessions() -> None:
    rows = _window_with_ratio(1.2, change_pct=2.0)[-20:]

    assert _volume_proxy_score_at(rows, 19) == _current_score(rows) == 70
    with pytest.raises(ValueError, match="20日正成交量窗口"):
        _volume_proxy_score_at(rows, 18)
    assert _volume_trigger(rows, 18, 50) is False


def test_historical_volume_score_and_trigger_ignore_future_rows() -> None:
    rows = _window_with_ratio(1.199, change_pct=2.0)
    index = len(rows) - 1
    future = [make_kline(close=1, volume=1e15), make_kline(close=1e6, volume=0)]

    assert _volume_proxy_score_at(rows + future, index) == _volume_proxy_score_at(rows, index) == 70
    assert _volume_trigger(rows + future, index, 70) is True


def test_public_factor_calibration_uses_the_same_rounded_historical_state() -> None:
    # Every completed twenty-session window ending at index 25 or 35 has a
    # raw ratio of 1.199. The former rule scored the historical state as 56,
    # outside the current 70-point trigger's tolerance, and discarded it.
    recent_volume = 9000.0
    other_volume = recent_volume * (2 / 1.199 - 1)
    volumes = [recent_volume if 1 <= index % 10 <= 5 else other_volume for index in range(36)]
    rows = _pit_rows(volumes)

    factor = _public_volume_factor(rows)

    assert factor_calibration_evidence_issue(rows) is None
    assert factor.score == 70
    assert factor.calibration is not None
    assert factor.calibration.availability == "available"
    assert factor.calibration.sample_count == 1
    assert factor.calibration.participates_in_historical_aggregate is True
    assert factor.calibration_buckets == []  # The original 45-row bucket gate remains.


def test_public_factor_percentile_compares_scores_with_the_current_rule() -> None:
    volumes = [1000 * 1.05 ** index for index in range(60)] + [7000] * 15 + [9000] * 5
    rows = _pit_rows(volumes)
    factor = _public_volume_factor(rows)
    current_rule_history = [_current_score(rows[: index + 1]) for index in range(20, len(rows) - 1)]
    expected = round(sum(score <= factor.score for score in current_rule_history) / len(current_rule_history) * 100, 1)

    assert factor_calibration_evidence_issue(rows) is None
    assert factor.score == 70
    assert expected < 100
    assert factor.percentile == expected


def test_suspended_volume_windows_are_excluded_from_calibration_and_percentiles() -> None:
    rows = _pit_rows([1000] * 36)
    # A valid, explicitly suspended exchange session does not violate the PIT
    # contract, but it makes the declared positive-volume window unavailable.
    rows[18] = rows[18].model_copy(update={
        "volume": 0, "session_status": "suspended", "open_execution_status": "unavailable",
    })
    spec = _factor_specs()["volume_confirmation"]

    assert factor_calibration_evidence_issue(rows) is None
    with pytest.raises(ValueError, match="20日正成交量窗口"):
        spec.evaluator(rows, 25)
    assert spec.trigger(rows, 25, 50) is False
    assert _factor_percentile(rows, spec.evaluator, 50) is None
    factor = _public_volume_factor(rows)
    assert factor.participates_in_current_score is False
    assert factor.percentile is None
    assert factor.calibration is not None
    assert factor.calibration.participates_in_historical_aggregate is False


def test_public_calibration_still_rejects_missing_pit_evidence() -> None:
    rows = _pit_rows([7000] * 31 + [9000] * 5)
    rows[5] = rows[5].model_copy(update={"point_in_time": False})

    factor = _public_volume_factor(rows)

    assert factor.score == 70
    assert factor.participates_in_current_score is True
    assert factor.percentile is None
    assert factor.calibration is not None
    assert factor.calibration.availability == "execution_evidence_unavailable"
    assert factor.calibration.sample_count == 0


def test_public_calibration_resumes_only_after_suspension_leaves_the_volume_window() -> None:
    rows = _pit_rows([1000] * 60)
    rows[18] = rows[18].model_copy(update={
        "volume": 0, "session_status": "suspended", "open_execution_status": "unavailable",
    })

    factor = _public_volume_factor(rows)

    assert factor_calibration_evidence_issue(rows) is None
    assert factor.participates_in_current_score is True
    assert factor.score == 56
    assert factor.calibration is not None
    assert factor.calibration.availability == "available"
    # The first complete positive-volume window ends at index 38. Keeping the
    # existing ten-session non-overlap rule selects 38 and 48, never 25 or 35.
    assert factor.calibration.sample_count == 2


def _window_with_ratio(raw_ratio: float, *, change_pct: float) -> list[Kline]:
    recent_volume = 9000.0
    base_volume = (20 * recent_volume / raw_ratio - 5 * recent_volume) / 15
    rows = _pit_rows([base_volume] * 25 + [recent_volume] * 5)
    rows[-2] = rows[-2].model_copy(update={"open": 100, "close": 100, "high": 103, "low": 97})
    close = 100 + change_pct
    rows[-1] = rows[-1].model_copy(update={"open": 100, "close": close, "high": max(100, close) + 1, "low": min(100, close) - 1})
    return rows


def _pit_rows(volumes: list[float]) -> list[Kline]:
    dates = next_trade_dates(date(2026, 1, 1), len(volumes))
    return [
        make_kline(date=day.isoformat(), close=100 + index * 0.1, volume=volume, replay_eligible=True)
        for index, (day, volume) in enumerate(zip(dates, volumes, strict=True))
    ]


def _current_score(rows: list[Kline]) -> int:
    ratio = recent_volume_ratio_if_available(rows)
    feature = SimpleNamespace(volume_ratio=ratio, volume_ratio_available=ratio is not None)
    analysis = SimpleNamespace(quote=SimpleNamespace(change_pct=pct_change(rows[-1].close, rows[-2].close)))
    return _volume_confirmation_score(analysis, feature)


def _public_volume_factor(rows: list[Kline]):
    current, previous = rows[-1], rows[-2]
    quote = make_quote(
        price=current.close, prev_close=previous.close, high=current.high, low=current.low,
        change_pct=pct_change(current.close, previous.close), timestamp=f"{current.date} 15:15:00",
    ).model_copy(update={"open": current.open, "volume": current.volume, "amount": current.close * current.volume})
    analysis = build_analysis(quote, rows)
    insights = build_stock_insight_bundle(analysis)
    feature = build_feature_snapshot(analysis, insights)
    return next(factor for factor in build_current_factors(analysis, insights, feature) if factor.id == "volume_confirmation")
