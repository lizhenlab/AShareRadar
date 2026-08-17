"""Deterministic, non-executing compiler for full-market strategy specifications."""

from __future__ import annotations

import hashlib
import json

from app.models.strategy_lab import (
    FilterValue,
    StrategyCompileResponse,
    StrategyCompiledExpression,
    StrategyExecutionPlan,
    StrategyHardFilter,
    StrategyObjectives,
    StrategySpecInput,
)
from app.services.strategy_metrics import STRATEGY_METRIC_BY_NAME


_BOARD_ORDER = ("sh_main", "star", "sz_main", "chinext", "beijing")
_BOARD_LABELS = {
    "sh_main": "上海A股（主板）",
    "star": "科创板",
    "sz_main": "深圳A股（主板）",
    "chinext": "创业板",
    "beijing": "北交所",
}
_OBJECTIVE_DIRECTIONS = {
    "alpha_1d": "maximize",
    "alpha_5d": "maximize",
    "alpha_20d": "maximize",
    "confidence": "maximize",
    "risk": "minimize",
    "tradability": "maximize",
}


def compile_strategy_spec(
    spec: StrategySpecInput,
    *,
    ambiguities: list[str] | None = None,
    unsupported_clauses: list[str] | None = None,
) -> StrategyCompileResponse:
    normalized = normalize_strategy_spec(spec)
    unsupported = list(dict.fromkeys(unsupported_clauses or []))
    compiled_filters = _compile_all_filters(normalized, unsupported)
    warnings = _compiler_warnings(normalized)
    ambiguity_values = list(dict.fromkeys(ambiguities or []))
    blocked_reasons = _blocked_reasons(unsupported, ambiguity_values)
    required_fields = sorted(
        {expression.source_field for expression in compiled_filters} | _objective_source_fields(normalized.objectives) | _portfolio_source_fields()
    )
    plan = _execution_plan(
        normalized,
        filters=compiled_filters,
        required_fields=required_fields,
        blocked_reasons=blocked_reasons,
    )
    return StrategyCompileResponse(
        normalized_spec=normalized,
        fingerprint=strategy_spec_fingerprint(normalized),
        execution_plan=plan,
        warnings=warnings,
        ambiguities=ambiguity_values,
        unsupported_clauses=unsupported,
    )


def _compile_all_filters(
    spec: StrategySpecInput,
    unsupported: list[str],
) -> list[StrategyCompiledExpression]:
    compiled: list[StrategyCompiledExpression] = []
    for item in [*_exclusion_filters(spec), *spec.hard_filters]:
        expression, reason = _compile_filter(item)
        if expression is None:
            unsupported.append(reason)
        else:
            compiled.append(expression)
    return compiled


def _compiler_warnings(spec: StrategySpecInput) -> list[str]:
    warnings: list[str] = []
    if spec.exclusions.min_data_quality_score != spec.evidence_policy.minimum_quality_score:
        warnings.append("排除规则与证据策略的数据质量阈值不同；执行时采用两者中更严格的阈值，原始配置保持不变")
    if spec.portfolio_constraints.stock_count > 50:
        warnings.append("候选数量超过50只，组合换手、费用和流动性约束需要重点审计")
    if spec.evidence_policy.allowed_sources:
        warnings.append("allowed_sources 是严格白名单；缺少来源标签的数据将不可执行")
    return warnings


def _blocked_reasons(unsupported: list[str], ambiguities: list[str]) -> list[str]:
    reasons: list[str] = []
    if unsupported:
        reasons.append("存在未支持条件，确认或删除前不能执行")
    if ambiguities:
        reasons.append("存在需要用户确认的歧义")
    return reasons


def _execution_plan(
    spec: StrategySpecInput,
    *,
    filters: list[StrategyCompiledExpression],
    required_fields: list[str],
    blocked_reasons: list[str],
) -> StrategyExecutionPlan:
    return StrategyExecutionPlan(
        executable=not blocked_reasons,
        blocked_reasons=blocked_reasons,
        board_labels=[_BOARD_LABELS[board] for board in spec.universe.boards],
        expressions=filters,
        required_fields=required_fields,
        objective_order=_objective_order(spec.objectives),
        portfolio_summary=_portfolio_summary(spec),
        execution_summary=_execution_summary(spec),
        estimated_universe="当前已上市A股池；实际数量在 dry-run 绑定冻结扫描批次后确定",
        estimated_work=(f"最多评估全市场冻结结果，应用 {len(filters)} 个确定性条件，" f"形成不超过 {spec.portfolio_constraints.stock_count} 只的研究组合草案"),
    )


def normalize_strategy_spec(spec: StrategySpecInput) -> StrategySpecInput:
    payload = spec.model_dump(mode="json")
    payload["universe"]["boards"] = sorted(
        payload["universe"]["boards"],
        key=_BOARD_ORDER.index,
    )
    payload["hard_filters"] = sorted(
        payload["hard_filters"],
        key=lambda item: (
            item["field"],
            item["operator"],
            item.get("period_sessions") or 0,
            _canonical_json(item["value"]),
        ),
    )
    for field in ("allowed_sources", "blocked_sources"):
        payload["evidence_policy"][field] = sorted(payload["evidence_policy"][field])
    payload["portfolio_constraints"]["custom_weights"] = dict(sorted(payload["portfolio_constraints"]["custom_weights"].items()))
    weights = payload["objectives"]
    total = sum(float(value) for value in weights.values())
    payload["objectives"] = {name: round(float(value) / total, 10) for name, value in weights.items()}
    return StrategySpecInput.model_validate(payload)


def strategy_spec_fingerprint(spec: StrategySpecInput) -> str:
    normalized = normalize_strategy_spec(spec)
    payload = normalized.model_dump(mode="json")
    payload.pop("name", None)
    payload.pop("description", None)
    return hashlib.sha256(_canonical_json(payload).encode("utf-8")).hexdigest()


def _compile_filter(item: StrategyHardFilter) -> tuple[StrategyCompiledExpression | None, str]:
    metric = STRATEGY_METRIC_BY_NAME.get(item.field)
    if metric is None:
        return None, f"未支持指标：{item.field}"
    if item.operator not in metric.allowed_operators:
        return None, f"指标 {item.field} 不支持运算符 {item.operator}"
    if metric.allowed_periods:
        if item.period_sessions not in metric.allowed_periods:
            return None, (f"指标 {item.field} 只支持周期 {metric.allowed_periods}，" f"收到 {item.period_sessions}")
    elif item.period_sessions is not None:
        return None, f"指标 {item.field} 不接受周期参数"
    if not _value_matches_kind(item.value, metric.kind):
        return None, f"指标 {item.field} 的值类型与 {metric.kind} 不匹配"
    source_field = metric.source_field
    if item.period_sessions is not None:
        source_field = source_field.format(period=item.period_sessions)
    return (
        StrategyCompiledExpression(
            field=item.field,
            source_field=source_field,
            operator=item.operator,
            value=item.value,
            period_sessions=item.period_sessions,
            display=_display_expression(metric.label, metric.unit, item.operator, item.value, item.period_sessions),
        ),
        "",
    )


def _value_matches_kind(value: FilterValue, kind: str) -> bool:
    values = value if isinstance(value, list) else [value]
    if kind == "number":
        return bool(values) and all(isinstance(item, (int, float)) and not isinstance(item, bool) for item in values)
    if kind == "boolean":
        return bool(values) and all(isinstance(item, bool) for item in values)
    return bool(values) and all(isinstance(item, str) for item in values)


def _exclusion_filters(spec: StrategySpecInput) -> list[StrategyHardFilter]:
    exclusions = spec.exclusions
    filters = [
        StrategyHardFilter(field="listing_days", operator="gte", value=exclusions.min_listing_days),
        StrategyHardFilter(field="history_sessions", operator="gte", value=exclusions.min_history_sessions),
        StrategyHardFilter(
            field="data_quality_score",
            operator="gte",
            value=max(exclusions.min_data_quality_score, spec.evidence_policy.minimum_quality_score),
        ),
    ]
    if exclusions.exclude_st:
        filters.append(StrategyHardFilter(field="is_st", operator="eq", value=False))
    if exclusions.exclude_new:
        filters.append(StrategyHardFilter(field="is_new", operator="eq", value=False))
    if exclusions.exclude_suspended:
        filters.append(StrategyHardFilter(field="suspended", operator="eq", value=False))
    if exclusions.min_amount_cny > 0:
        filters.append(StrategyHardFilter(field="amount", operator="gte", value=exclusions.min_amount_cny))
    return filters


def _objective_source_fields(objectives: StrategyObjectives) -> set[str]:
    fields = set()
    for name, value in objectives.model_dump().items():
        if float(value) <= 0:
            continue
        metric = STRATEGY_METRIC_BY_NAME[name]
        fields.add(metric.source_field)
    return fields


def _portfolio_source_fields() -> set[str]:
    return {
        "derived.listing_board",
        "market_scan_result.amount",
        "market_scan_result.industry",
        "market_scan_result.price",
        "market_scan_result.status",
    }


def _objective_order(objectives: StrategyObjectives) -> list[str]:
    values = objectives.model_dump()
    return [
        f"{name}:{_OBJECTIVE_DIRECTIONS[name]}:{float(weight):.4f}"
        for name, weight in sorted(values.items(), key=lambda item: (-float(item[1]), item[0]))
        if float(weight) > 0
    ]


def _portfolio_summary(spec: StrategySpecInput) -> list[str]:
    value = spec.portfolio_constraints
    return [
        f"目标持仓不超过 {value.stock_count} 只，权重方式 {value.weighting_method}",
        f"单股权重不超过 {value.max_stock_weight:.1%}",
        f"单行业不超过 {value.max_industry_positions} 只且权重不超过 {value.max_industry_weight:.1%}",
        f"单上市板块权重不超过 {value.max_board_weight:.1%}",
        f"单笔名义金额不超过日成交额的 {value.max_notional_share_of_daily_amount:.2%}",
    ]


def _execution_summary(spec: StrategySpecInput) -> list[str]:
    policy = spec.execution_policy
    return [
        f"持有 {spec.rebalance_policy.hold_sessions} 个交易日，调仓周期 {spec.rebalance_policy.rebalance_every_sessions} 日",
        "强制执行股票 T+1、停牌和板块涨跌停约束",
        f"成本情景 {policy.cost_profile}，佣金 {policy.commission_rate:.4%}，最低佣金 {policy.minimum_commission_cny:.2f} 元",
        f"买卖滑点分别为 {policy.buy_slippage_bps:.1f}/{policy.sell_slippage_bps:.1f} bps",
        "仅生成研究组合草案，不连接券商、不提交真实委托",
    ]


def _display_expression(
    label: str,
    unit: str,
    operator: str,
    value: FilterValue,
    period: int | None,
) -> str:
    period_text = f"最近{period}个交易日 " if period is not None else ""
    return f"{period_text}{label} {operator} {_display_value(value, unit)}"


def _display_value(value: FilterValue, unit: str) -> str:
    if isinstance(value, list):
        return "[" + ", ".join(_display_value(item, unit) for item in value) + "]"
    if unit == "CNY" and isinstance(value, (int, float)):
        numeric = float(value)
        if numeric >= 100_000_000:
            return f"{numeric / 100_000_000:g}亿元"
        if numeric >= 10_000:
            return f"{numeric / 10_000:g}万元"
        return f"{numeric:g}元"
    if unit == "percent" and isinstance(value, (int, float)):
        return f"{float(value):g}%"
    return str(value)


def _canonical_json(value: object) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


__all__ = ["compile_strategy_spec", "normalize_strategy_spec", "strategy_spec_fingerprint"]
