"""Strategies consume frozen quantitative features only after PIT replay."""

from copy import deepcopy
import math

from pydantic import ValidationError
import pytest

from app.artifacts.io import canonical_json_text, sha256_hex
from app.models.strategy_execution import StrategyExecutionRequest
from app.models.strategy_lab import StrategyHardFilter, StrategySpecInput
from app.services.strategy_compiler import compile_strategy_spec
from app.services.strategy_metrics import strategy_metric_registry
from app.services.strategy_portfolio import build_portfolio_draft
from tests.test_market_scan_scoring import _as_v4_score_details
from tests.test_strategy_automation_atomic_completion import _isolated_environment


METRIC_CASES = [
    ("skip5_return_pct", 20, "skip5_return_20d_pct"),
    ("skip5_return_pct", 55, "skip5_return_55d_pct"),
    ("atr_pct", 20, "atr20_pct"),
    ("downside_volatility_pct", 20, "downside_volatility_20d_pct"),
    ("max_drawdown_pct", 60, "max_drawdown_60d_pct"),
]


@pytest.fixture(scope="module")
def frozen_strategy(tmp_path_factory):
    with _isolated_environment(tmp_path_factory.mktemp("quant-metric-evidence")) as (cache, service, strategy_id, run_id):
        strategy = cache.domain_services.strategy_lab.get(strategy_id)
        frozen = service.repository.frozen_scan(run_id=run_id, data_date=None, mode="official")
        yield strategy, frozen.run, frozen.items[0]


def _candidate(frozen_strategy, metric, period, operator, value, *, item=None, verified_required=True):
    strategy, run, original = frozen_strategy
    spec = strategy.spec.model_copy(update={
        "hard_filters": [StrategyHardFilter(field=metric, period_sessions=period, operator=operator, value=value)],
        "evidence_policy": strategy.spec.evidence_policy.model_copy(update={
            "require_verified_point_in_time_evidence": verified_required,
        }),
    })
    result = build_portfolio_draft(
        strategy.model_copy(update={"spec": spec}), run,
        [item or original], StrategyExecutionRequest(strategy_id=strategy.strategy_id),
    )
    return result.candidates[0]


def _dimensions(item):
    return item.score_details["components"]["score_dimensions"]


@pytest.mark.parametrize("metric,period,key", METRIC_CASES)
def test_new_metrics_have_explicit_percent_units_periods_and_sources(metric, period, key):
    registry = {item.name: item for item in strategy_metric_registry()}
    assert registry[metric].unit == "percent"
    assert registry[metric].allowed_periods == ([20, 55] if metric == "skip5_return_pct" else [period])
    assert registry[metric].direction == ("maximize" if metric == "skip5_return_pct" else "minimize")
    compiled = compile_strategy_spec(StrategySpecInput(
        name="明确因子窗口", hard_filters=[StrategyHardFilter(field=metric, period_sessions=period, operator="lte", value=5.0)],
    ))
    assert compiled.execution_plan.executable
    expression = next(item for item in compiled.execution_plan.expressions if item.field == metric)
    assert expression.source_field == f"score_details.components.score_dimensions.raw_features.{key}"
    if metric == "skip5_return_pct":
        assert expression.display.startswith(f"跳过最近5日后，此前{period}个交易日收益率")


@pytest.mark.parametrize("metric,period,key", METRIC_CASES)
@pytest.mark.parametrize("bad_period", [None, 1, 5, 21, 252])
def test_metric_window_cannot_silently_fall_back(metric, period, key, bad_period):
    del period, key
    compiled = compile_strategy_spec(StrategySpecInput(
        name="拒绝不存在窗口", hard_filters=[StrategyHardFilter(field=metric, period_sessions=bad_period, operator="gte", value=0.0)],
    ))
    assert not compiled.execution_plan.executable
    assert any("只支持周期" in reason for reason in compiled.unsupported_clauses)


@pytest.mark.parametrize("value", [math.inf, -math.inf, math.nan, [0.0, math.inf]])
def test_nonfinite_filter_thresholds_are_rejected_before_fingerprinting(value):
    with pytest.raises(ValidationError):
        StrategyHardFilter(field="atr_pct", period_sessions=20, operator="between" if isinstance(value, list) else "lte", value=value)


def test_reversed_interval_and_boolean_number_are_not_executable():
    for value, operator in [([4.0, 1.0], "between"), (True, "gte"), (10**400, "lte"), ([0, 10**400], "between")]:
        compiled = compile_strategy_spec(StrategySpecInput(
            name="无效区间", hard_filters=[StrategyHardFilter(field="atr_pct", period_sessions=20, operator=operator, value=value)],
        ))
        assert not compiled.execution_plan.executable


def test_quantitative_values_match_independent_bar_formulas(frozen_strategy):
    _strategy, _run, item = frozen_strategy
    raw = _dimensions(item)["raw_features"]
    bars = _dimensions(item)["point_in_time_evidence"]["payload"]["bar_contract_61"]
    closes = [row[2] for row in bars]
    assert raw["skip5_return_20d_pct"] == pytest.approx((closes[-6] / closes[-26] - 1) * 100)
    assert raw["skip5_return_55d_pct"] == pytest.approx((closes[-6] / closes[-61] - 1) * 100)
    ranges = [max(row[3] - row[4], abs(row[3] - previous[2]), abs(row[4] - previous[2]))
              for previous, row in zip(bars[-21:-1], bars[-20:], strict=True)]
    assert raw["atr20_pct"] == pytest.approx(sum(ranges) / 20 / item.price * 100)
    returns = [current / previous - 1 for previous, current in zip(closes[-21:-1], closes[-20:], strict=True)]
    assert raw["downside_volatility_20d_pct"] == pytest.approx(math.sqrt(sum(min(value, 0) ** 2 for value in returns) / 20) * 100)
    peak = closes[-60]
    max_loss = 0.0
    for value in closes[-60:]:
        peak = max(peak, value)
        max_loss = max(max_loss, 1 - value / peak)
    assert raw["max_drawdown_60d_pct"] == pytest.approx(max_loss * 100)
    assert raw["max_drawdown_60d_pct"] >= 0


@pytest.mark.parametrize("metric,period,key", METRIC_CASES)
def test_real_compilation_and_execution_honor_closed_filter_boundaries(frozen_strategy, metric, period, key):
    raw = _dimensions(frozen_strategy[2])["raw_features"][key]
    at_boundary = _candidate(frozen_strategy, metric, period, "lte", raw)
    outside = _candidate(frozen_strategy, metric, period, "lt", raw)
    assert at_boundary.evidence_verified
    assert not at_boundary.hard_filter_failures
    assert outside.status == "rejected"
    assert any("未通过" in message for message in outside.hard_filter_failures)


@pytest.mark.parametrize("metric,period,key", [*METRIC_CASES, ("return_pct", 20, "return_20d_pct")])
@pytest.mark.parametrize("mutation", ["outer", "missing", "null", "nan", "bool", "payload_resealed", "outer_and_payload_resealed"])
def test_unbound_missing_or_resealed_features_cannot_pass_even_with_pit_policy_disabled(frozen_strategy, metric, period, key, mutation):
    item = frozen_strategy[2].model_copy(deep=True)
    dimensions = _dimensions(item)
    raw = dimensions["raw_features"]
    evidence = dimensions["point_in_time_evidence"]
    expected = raw[key]
    if mutation == "outer":
        raw[key] = expected + 1000
    elif mutation == "missing":
        raw.pop(key)
    elif mutation == "null":
        raw[key] = None
    elif mutation == "nan":
        raw[key] = math.nan
    elif mutation == "bool":
        raw[key] = bool(expected)
    else:
        evidence["payload"]["features"][key] = expected + 1000
        if mutation == "outer_and_payload_resealed":
            raw[key] = expected + 1000
        evidence["payload_digest"] = sha256_hex(canonical_json_text(evidence["payload"]))
    candidate = _candidate(frozen_strategy, metric, period, "ne", -9999.0, item=item, verified_required=False)
    assert candidate.status == "rejected"
    assert any("当前 缺失" in message for message in candidate.hard_filter_failures)


@pytest.mark.parametrize("mutation", ["no_evidence", "future_bar", "wrong_symbol", "wrong_mode"])
def test_quant_filter_requires_binding_to_current_item_and_decision_context(frozen_strategy, mutation):
    item = frozen_strategy[2].model_copy(deep=True)
    dimensions = _dimensions(item)
    evidence = dimensions["point_in_time_evidence"]
    if mutation == "no_evidence":
        dimensions["point_in_time_evidence"] = {}
    else:
        payload = evidence["payload"]
        if mutation == "future_bar":
            payload["bar_contract_61"][-1][9] = "2027-01-01T00:00:00Z"
        elif mutation == "wrong_symbol":
            payload["symbol"] = "600099.SH"
        else:
            payload["mode"] = "intraday"
        evidence["payload_digest"] = sha256_hex(canonical_json_text(payload))
    candidate = _candidate(frozen_strategy, "atr_pct", 20, "lte", 1000.0, item=item, verified_required=False)
    assert not candidate.evidence_verified
    assert candidate.status == "rejected"


def test_current_features_are_read_only_and_do_not_modify_production_score_or_rank(frozen_strategy):
    original = frozen_strategy[2]
    before = deepcopy(original.model_dump())
    first = _candidate(frozen_strategy, "atr_pct", 20, "lte", 1000.0)
    repeated = _candidate(frozen_strategy, "atr_pct", 20, "lte", 1000.0)
    assert first == repeated
    assert first.original_rank == original.rank
    assert original.model_dump() == before


def test_legacy_conditional_downside_deviation_is_not_read_as_target_semideviation(frozen_strategy):
    item = frozen_strategy[2].model_copy(deep=True)
    details = _as_v4_score_details(item.score_details)
    final = details["components"]["final_score"]
    item = item.model_copy(update={"score_details": details, "score": final["score"], "raw_score": final["raw"]})
    atr = _candidate(frozen_strategy, "atr_pct", 20, "lte", 1000.0, item=item)
    downside = _candidate(frozen_strategy, "downside_volatility_pct", 20, "lte", 1000.0, item=item)
    assert atr.evidence_verified and not atr.hard_filter_failures
    assert downside.evidence_verified
    assert any("当前 缺失" in message for message in downside.hard_filter_failures)


@pytest.mark.parametrize("invalid", [10**400, -1, 101, math.nan, math.inf, True])
def test_invalid_risk_dimension_cannot_crash_or_produce_utility(frozen_strategy, invalid):
    item = frozen_strategy[2].model_copy(deep=True)
    _dimensions(item)["scores"]["risk"] = invalid
    candidate = _candidate(frozen_strategy, "atr_pct", 20, "lte", 1000.0, item=item, verified_required=False)
    assert not candidate.evidence_verified
    assert candidate.utility_score is None
    assert candidate.status == "rejected"


def test_resealed_huge_bar_value_is_rejected_without_overflow(frozen_strategy):
    item = frozen_strategy[2].model_copy(deep=True)
    evidence = _dimensions(item)["point_in_time_evidence"]
    evidence["payload"]["bar_contract_61"][-2][3] = 10**400
    evidence["payload_digest"] = sha256_hex(canonical_json_text(evidence["payload"]))
    candidate = _candidate(frozen_strategy, "atr_pct", 20, "lte", 1000.0, item=item)
    assert not candidate.evidence_verified
    assert candidate.status == "rejected"
