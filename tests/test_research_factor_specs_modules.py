from __future__ import annotations

import math

import pytest

from app.services.research_factor_specs import (
    FactorSpec,
    REGISTERED_FACTOR_SPECS,
    _REGISTERED_FACTOR_SPEC_MAP,
    _chip_position_score_at,
    _chip_trigger,
    _factor_spec_map,
    _factor_specs,
    _fund_flow_trigger,
    _fund_flow_proxy_score_at,
    _moving_averages,
    _price_range,
    _risk_proxy_score_at,
    _score_context,
    _score_index_is_valid,
    _trend_trigger,
    _trend_proxy_score_at,
    _volume_proxy_score_at,
    _volume_ratio_at,
    _window_average_close,
)
from tests.factories import make_kline


def test_factor_specs_preserve_registered_order_and_metadata() -> None:
    specs = _factor_specs()

    assert list(specs) == [
        "trend_momentum",
        "volume_confirmation",
        "risk_pressure",
        "fund_flow_proxy",
        "chip_position",
        "leadership_strength",
    ]
    assert list(_REGISTERED_FACTOR_SPEC_MAP) == list(specs)
    assert tuple(specs.values()) == REGISTERED_FACTOR_SPECS
    assert all(key == spec.id for key, spec in specs.items())


def test_factor_specs_return_isolation_and_duplicate_validation() -> None:
    specs = _factor_specs()
    specs.pop("trend_momentum")

    assert "trend_momentum" in _factor_specs()

    duplicate = _factor_spec(id="trend_momentum", name="重复因子")
    with pytest.raises(ValueError, match="duplicate factor spec id: trend_momentum"):
        _factor_spec_map((REGISTERED_FACTOR_SPECS[0], duplicate))


def test_registered_factor_spec_map_is_immutable() -> None:
    with pytest.raises(TypeError):
        _REGISTERED_FACTOR_SPEC_MAP["trend_momentum"] = _factor_spec(name="改写因子")

    assert tuple(_REGISTERED_FACTOR_SPEC_MAP.values()) == REGISTERED_FACTOR_SPECS


def test_factor_specs_reject_invalid_registration_rows() -> None:
    with pytest.raises(ValueError, match="factor spec id must be a non-empty string"):
        _factor_spec_map((_factor_spec(id=" "),))

    with pytest.raises(ValueError, match="factor spec id must not contain leading or trailing whitespace"):
        _factor_spec_map((_factor_spec(id=" trend_momentum "),))

    with pytest.raises(ValueError, match="factor spec invalid_weight weight must be a positive finite number"):
        _factor_spec_map((_factor_spec(id="invalid_weight", weight=math.nan),))

    with pytest.raises(ValueError, match="factor spec invalid_evaluator evaluator must be callable"):
        _factor_spec_map((_factor_spec(id="invalid_evaluator", evaluator=None),))

    with pytest.raises(ValueError, match="factor spec invalid_trigger trigger must be callable"):
        _factor_spec_map((_factor_spec(id="invalid_trigger", trigger=None),))


def test_trend_proxy_score_preserves_strong_trend_components() -> None:
    rows = _rows([100 + index for index in range(40)])

    assert _trend_proxy_score_at(rows, 30) == 99


def test_trend_proxy_score_returns_neutral_for_malformed_current_bar() -> None:
    rows = _rows([100 + index for index in range(40)])
    rows[30] = make_kline(date="2026-06-01", close=130, high=0, low=129, volume=1000)

    assert _trend_proxy_score_at(rows, 30) == 50


def test_volume_proxy_score_uses_positive_volume_confirmation_rule() -> None:
    rows = _rows([100 + index for index in range(40)])
    rows[30] = make_kline(date="2026-06-01", close=132, high=133, low=131, volume=4000)

    assert _volume_proxy_score_at(rows, 30) == 70


def test_volume_proxy_score_returns_neutral_when_current_volume_is_missing() -> None:
    rows = _rows([100 + index for index in range(40)])
    rows[30] = make_kline(date="2026-06-01", close=130, high=131, low=129, volume=0)

    assert _volume_proxy_score_at(rows, 30) == 50
    assert _volume_ratio_at(rows, 30) == 1.0


def test_risk_proxy_score_returns_neutral_for_malformed_current_bar() -> None:
    rows = _rows([100 + index for index in range(40)])
    rows[30] = make_kline(date="2026-06-01", close=130, high=130, low=0, volume=1000)

    assert _risk_proxy_score_at(rows, 30) == 58


def test_chip_position_score_requires_positive_volume_cost_center() -> None:
    rows = _rows([100 + index for index in range(40)], volume=0)

    assert _chip_position_score_at(rows, 30) == 50


def test_position_and_flow_scores_require_valid_current_bar() -> None:
    rows = _rows([100 + index for index in range(40)])

    malformed_current = list(rows)
    malformed_current[30] = make_kline(date="2026-06-01", close=130, high=131, low=129, volume=1000).model_copy(
        update={"high": math.inf}
    )
    assert _chip_position_score_at(malformed_current, 30) == 50
    assert _fund_flow_proxy_score_at(malformed_current, 30) == 50

    zero_volume_current = list(rows)
    zero_volume_current[30] = make_kline(date="2026-06-01", close=130, high=131, low=129, volume=0)
    assert _fund_flow_proxy_score_at(zero_volume_current, 30) == 50


def test_window_average_close_ignores_non_positive_values() -> None:
    rows = [
        make_kline(date="2026-05-01", close=100),
        make_kline(date="2026-05-02", close=0, high=1, low=0),
        make_kline(date="2026-05-02", close=101).model_copy(update={"high": math.inf}),
        make_kline(date="2026-05-03", close=102),
    ]

    assert _window_average_close(rows, 3, 4) == 101


def test_score_context_builds_expected_metrics_from_valid_rows() -> None:
    rows = _rows([100 + index for index in range(40)])

    context = _score_context(rows, 30, min_index=20)

    assert context is not None
    assert context.current is rows[30]
    assert context.previous is rows[29]
    assert context.change_pct == _pct(rows[30].close, rows[29].close)
    assert context.ma5 == _window_average_close(rows, 30, 5)
    assert context.ma10 == _window_average_close(rows, 30, 10)
    assert context.ma20 == _window_average_close(rows, 30, 20)
    assert context.prev_ma5 == _window_average_close(rows, 25, 5)
    assert context.high_20 == max(item.high for item in rows[11:31])
    assert context.low_20 == min(item.low for item in rows[11:31])


def test_score_context_rejects_invalid_boundaries_and_rows() -> None:
    rows = _rows([100 + index for index in range(40)])

    assert _score_index_is_valid(rows, 19, min_index=20) is False
    assert _score_index_is_valid(rows, 40, min_index=20) is False
    assert _score_context(rows, 19, min_index=20) is None

    malformed_current = list(rows)
    malformed_current[30] = make_kline(date="2026-06-01", close=130, high=120, low=129, volume=1000)
    assert _score_context(malformed_current, 30, min_index=20) is None

    invalid_previous = list(rows)
    invalid_previous[29] = make_kline(date="2026-05-29", close=0, high=1, low=0, volume=1000)
    assert _score_context(invalid_previous, 30, min_index=20) is None

    invalid_previous_open = list(rows)
    invalid_previous_open[29] = make_kline(
        date="2026-05-29",
        close=129,
        high=130,
        low=128,
        volume=1000,
    ).model_copy(update={"open": 140})
    assert _score_context(invalid_previous_open, 30, min_index=20) is None

    invalid_open = list(rows)
    invalid_open[30] = make_kline(
        date="2026-06-01",
        close=130,
        high=131,
        low=129,
        volume=1000,
    ).model_copy(update={"open": 140})
    assert _score_context(invalid_open, 30, min_index=20) is None

    non_finite = list(rows)
    non_finite[30] = make_kline(
        date="2026-06-01",
        close=130,
        high=131,
        low=129,
        volume=1000,
    ).model_copy(update={"high": math.inf})
    assert _score_context(non_finite, 30, min_index=20) is None

    non_finite_window = list(rows)
    non_finite_window[28] = make_kline(
        date="2026-05-28",
        close=128,
        high=129,
        low=127,
        volume=1000,
    ).model_copy(update={"close": math.inf})
    assert _score_context(non_finite_window, 30, min_index=20) is None
    assert _window_average_close(non_finite_window, 30, 5) == _window_average_close(rows, 30, 5)
    assert _window_average_close(non_finite_window, 30, 5, min_count=5) == 0


def test_score_context_requires_full_metric_windows() -> None:
    rows = _rows([100 + index for index in range(40)])

    invalid_average_window = list(rows)
    invalid_average_window[28] = make_kline(
        date="2026-05-28",
        close=128,
        high=129,
        low=127,
        volume=1000,
    ).model_copy(update={"close": math.inf})
    assert _moving_averages(invalid_average_window, 30) is None
    assert _trend_proxy_score_at(invalid_average_window, 30) == 50
    assert _volume_proxy_score_at(invalid_average_window, 30) == 50
    assert _risk_proxy_score_at(invalid_average_window, 30) == 58

    invalid_range_window = list(rows)
    invalid_range_window[15] = make_kline(
        date="2026-05-15",
        close=115,
        high=116,
        low=114,
        volume=1000,
    ).model_copy(update={"high": math.inf})
    assert _price_range(invalid_range_window, 30) is None


def test_score_context_helpers_return_none_for_empty_metric_windows() -> None:
    assert _moving_averages([], 0) is None
    assert _price_range([], 0) is None


def test_metric_helpers_return_neutral_for_invalid_windows_and_indexes() -> None:
    rows = _rows([100 + index for index in range(10)])

    assert _window_average_close(rows, 4, 0) == 0
    assert _window_average_close(rows, len(rows), 5) == 0
    assert _moving_averages(rows, len(rows)) is None
    assert _price_range(rows, len(rows)) is None
    assert _volume_ratio_at(rows, 4, recent_window=0) == 1.0
    assert _volume_ratio_at(rows, 4, base_window=0) == 1.0


def test_triggers_reject_non_finite_current_scores_and_clamp_boundaries() -> None:
    rows = _rows([100 + index for index in range(40)])

    assert _trend_trigger(rows, 30, math.nan) is False
    assert _fund_flow_trigger(rows, 30, math.inf) is False
    assert _trend_trigger(rows, 30, 200) is True
    assert _fund_flow_proxy_score_at(rows, 30) == 92
    assert _fund_flow_trigger(rows, 30, 120) is True


def test_chip_trigger_tolerance_boundary_is_inclusive() -> None:
    rows = _rows([100 + index for index in range(40)])
    score = _chip_position_score_at(rows, 30)

    assert _chip_trigger(rows, 30, score + 12) is True
    assert _chip_trigger(rows, 30, score + 12.01) is False
    assert _chip_trigger(rows, 30, math.nan) is False


def _factor_spec(**overrides) -> FactorSpec:
    values = {
        "id": "test_factor",
        "name": "测试因子",
        "category": "测试",
        "weight": 1,
        "direction": "正向",
        "evaluator": lambda _rows, _index: 50,
        "trigger": lambda _rows, _index, _score: True,
    }
    values.update(overrides)
    return FactorSpec(**values)


def _rows(closes: list[float], *, volume: float = 1000):
    return [
        make_kline(
            date=f"2026-05-{(index % 28) + 1:02d}",
            close=close,
            high=close + 1,
            low=max(0, close - 1),
            volume=volume,
        )
        for index, close in enumerate(closes)
    ]


def _pct(current: float, previous: float) -> float:
    return (current - previous) / previous * 100
