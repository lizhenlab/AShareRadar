"""Pure portfolio-draft construction from immutable market-scan evidence."""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import date
import hashlib
import json
from math import floor, isfinite
from typing import Any, cast

from app.models.market_scan import MarketScanResultItem, MarketScanRun
from app.models.paper_trading import PaperInstrumentMetadata
from app.models.paper_trading_config import PAPER_TRADING_RULE_VERSION
from app.models.strategy_execution import (
    PortfolioCandidate,
    PortfolioDraftSummary,
    StrategyExecutionRequest,
)
from app.models.strategy_lab import StrategyCompiledExpression, StrategySpec
from app.services.market_scan_score_dimensions import verify_market_scan_point_in_time_evidence
from app.services.paper_trading_rules import resolve_trade_rule_profile
from app.services.strategy_compiler import compile_strategy_spec


_MISSING = object()
_BOARD_LABELS = {
    "sh_main": "上海A股（主板）",
    "star": "科创板",
    "sz_main": "深圳A股（主板）",
    "chinext": "创业板",
    "beijing": "北交所",
    "unknown": "未知板块",
}


@dataclass
class _WorkingCandidate:
    item: MarketScanResultItem
    board: str
    scores: dict[str, float] = field(default_factory=dict)
    utility: float | None = None
    contributions: dict[str, float] = field(default_factory=dict)
    evidence_verified: bool = False
    freshness: str = "不可验证"
    failures: list[str] = field(default_factory=list)
    changes: list[str] = field(default_factory=list)
    reasons: list[str] = field(default_factory=list)
    pareto: bool = False
    utility_rank: int | None = None
    rank_sensitivity: dict[str, int] = field(default_factory=dict)
    status: str = "rejected"
    weight: float = 0.0
    quantity: int = 0
    gross_amount: float = 0.0
    round_trip_cost: float = 0.0


@dataclass(frozen=True)
class PortfolioComputation:
    summary: PortfolioDraftSummary
    candidates: list[PortfolioCandidate]
    cost_rule_fingerprint: str
    execution_fingerprint: str
    result_digest: str


def build_portfolio_draft(
    strategy: StrategySpec,
    run: MarketScanRun,
    items: list[MarketScanResultItem],
    request: StrategyExecutionRequest,
) -> PortfolioComputation:
    compiled = compile_strategy_spec(strategy.spec)
    if not compiled.execution_plan.executable:
        raise ValueError("策略编译计划不可执行：" + "；".join(compiled.execution_plan.blocked_reasons))
    working = [
        _evaluate_candidate(strategy, run, item, compiled.execution_plan.expressions)
        for item in items
    ]
    _apply_execution_freshness(strategy, run, working)
    _apply_rebalance_hysteresis(strategy, request, working)
    eligible = [item for item in working if item.utility is not None and not item.failures]
    eligible.sort(key=lambda item: (-float(item.utility or 0), item.item.rank or 10**9, item.item.symbol))
    for index, item in enumerate(eligible, start=1):
        item.utility_rank = index
    _rank_sensitivity(strategy, eligible)
    _mark_pareto_front(eligible)
    _apply_portfolio_constraints(strategy, run, eligible, request.notional_cash_cny)
    _finalize_unselected_reasons(strategy, eligible)

    candidates = [_to_candidate(item) for item in working]
    candidates.sort(key=lambda item: (item.utility_rank is None, item.utility_rank or 10**9, item.original_rank or 10**9, item.symbol))
    summary = _portfolio_summary(strategy, candidates, request)
    cost_fingerprint = _cost_rule_fingerprint(strategy)
    execution_fingerprint = _execution_fingerprint(
        strategy,
        run,
        request,
        cost_rule_fingerprint=cost_fingerprint,
    )
    result_digest = _stable_digest(
        {
            "execution_fingerprint": execution_fingerprint,
            "summary": summary.model_dump(mode="json"),
            "candidates": [item.model_dump(mode="json") for item in candidates],
        }
    )
    return PortfolioComputation(
        summary=summary,
        candidates=candidates,
        cost_rule_fingerprint=cost_fingerprint,
        execution_fingerprint=execution_fingerprint,
        result_digest=result_digest,
    )


def strategy_board(code: str, market: str) -> str:
    clean = str(code).zfill(6)
    if market == "BJ":
        return "beijing"
    if market == "SH":
        return "star" if clean.startswith(("688", "689")) else "sh_main"
    if market == "SZ":
        return "chinext" if clean.startswith(("300", "301")) else "sz_main"
    return "unknown"


def _evaluate_candidate(
    strategy: StrategySpec,
    run: MarketScanRun,
    item: MarketScanResultItem,
    expressions: list[StrategyCompiledExpression],
) -> _WorkingCandidate:
    candidate = _WorkingCandidate(item=item, board=strategy_board(item.code, item.market))
    if item.status != "success":
        candidate.status = "unfilled" if _looks_untradeable(item) else "rejected"
        candidate.failures.append(item.reason or item.error or f"冻结扫描状态为 {item.status}")
        candidate.reasons.append("未进入生产冻结排名，原始榜单名次保持为空")
        return candidate
    if candidate.board not in strategy.spec.universe.boards:
        candidate.failures.append(f"上市板块 {_BOARD_LABELS[candidate.board]} 不在策略股票池")
        candidate.changes.append(f"将 {_BOARD_LABELS[candidate.board]} 加入策略股票池")

    dimensions, evidence = _dimensions_and_evidence(item)
    candidate.scores = dimensions
    candidate.evidence_verified = bool(evidence) and verify_market_scan_point_in_time_evidence(evidence)
    candidate.freshness = _evidence_freshness(run, item, evidence, candidate.evidence_verified)
    _apply_evidence_policy(strategy, item, candidate)

    for expression in expressions:
        actual = _source_value(expression.source_field, item, run, candidate.board)
        if not _matches(expression, actual):
            actual_text = "缺失" if actual is _MISSING else str(actual)
            candidate.failures.append(f"{expression.display} 未通过（当前 {actual_text}）")
            candidate.changes.extend(_minimum_changes(expression, actual))

    if not _complete_dimensions(dimensions):
        candidate.failures.append("冻结快照缺少完整的 Alpha/置信/风险/可交易性维度")
        candidate.changes.append("等待包含 score_dimensions 的新冻结扫描，不用当前规则补算历史")
        return candidate
    candidate.contributions = _marginal_contributions(strategy, dimensions)
    candidate.utility = round(sum(candidate.contributions.values()), 6)
    candidate.reasons.append("多目标效用基于冻结的独立评分维度计算，不修改生产原始排名")
    if not candidate.failures:
        candidate.reasons.append("已通过股票池、硬过滤和时点证据检查")
    return candidate


def _dimensions_and_evidence(item: MarketScanResultItem) -> tuple[dict[str, float], dict[str, object]]:
    components = _dict(item.score_details.get("components"))
    dimensions = _dict(components.get("score_dimensions"))
    scores = _dict(dimensions.get("scores"))
    values = {
        name: float(value)
        for name in ("alpha_1d", "alpha_5d", "alpha_20d", "confidence", "risk", "tradability")
        if (value := scores.get(name)) is not None
        and isinstance(value, (int, float))
        and not isinstance(value, bool)
        and isfinite(float(value))
    }
    return values, _dict(dimensions.get("point_in_time_evidence"))


def _apply_evidence_policy(
    strategy: StrategySpec,
    item: MarketScanResultItem,
    candidate: _WorkingCandidate,
) -> None:
    policy = strategy.spec.evidence_policy
    if policy.require_verified_point_in_time_evidence and not candidate.evidence_verified:
        candidate.failures.append("策略要求可验证时点证据，但冻结摘要缺失或校验失败")
        candidate.changes.append("等待产生可校验 point-in-time evidence 的新扫描")
    sources = [source for source in (item.quote_source, item.kline_source, item.metadata_source) if source]
    blocked = sorted(set(sources) & set(policy.blocked_sources))
    if blocked:
        candidate.failures.append("命中禁止证据来源：" + "、".join(blocked))
    if policy.allowed_sources:
        missing = [
            label
            for label, source in (
                ("行情", item.quote_source),
                ("日K", item.kline_source),
                ("元数据", item.metadata_source),
            )
            if not str(source or "").strip()
        ]
        if missing:
            candidate.failures.append("证据来源白名单要求完整来源标签，当前缺少：" + "、".join(missing))
        outside = sorted(set(sources) - set(policy.allowed_sources))
        if outside:
            candidate.failures.append("存在白名单之外的证据来源：" + "、".join(outside))


def _apply_execution_freshness(
    strategy: StrategySpec,
    run: MarketScanRun,
    candidates: list[_WorkingCandidate],
) -> None:
    # Freshness is evaluated at the frozen run's own decision time.  Using the
    # HTTP request wall clock here would make an immutable replay change result
    # and digest merely because it was opened on a later day.
    reference_date = _date_value(run.as_of[:10]) or date.fromisoformat(run.data_date)
    age_days = max(0, (reference_date - date.fromisoformat(run.data_date)).days)
    maximum = strategy.spec.evidence_policy.maximum_market_data_age_days
    if age_days <= maximum:
        return
    for candidate in candidates:
        if candidate.item.status != "success":
            continue
        candidate.failures.append(f"市场数据已过期：{age_days} 天，策略上限 {maximum} 天")
        candidate.changes.append("改用满足最大数据年龄约束的新冻结扫描")
        candidate.freshness = f"已过期 · {age_days} 天（上限 {maximum} 天）"


def _apply_rebalance_hysteresis(
    strategy: StrategySpec,
    request: StrategyExecutionRequest,
    candidates: list[_WorkingCandidate],
) -> None:
    policy = strategy.spec.rebalance_policy
    for candidate in candidates:
        if candidate.utility is None or candidate.failures:
            continue
        held = request.current_weights.get(candidate.item.symbol, 0.0) > 0
        threshold = policy.hold_utility_threshold if held else policy.buy_utility_threshold
        if candidate.utility + 1e-9 < threshold:
            action = "继续持有" if held else "新买入"
            candidate.failures.append(
                f"多目标效用 {candidate.utility:.4f} 未达到{action}阈值 {threshold:.4f}"
            )
            candidate.changes.append(
                f"多目标效用至少提高 {threshold - candidate.utility:.4f} 或调整{action}阈值"
            )
        elif held:
            candidate.reasons.append(
                f"当前持仓适用持有阈值 {policy.hold_utility_threshold:.4f}，保留买卖迟滞"
            )


def _evidence_freshness(
    run: MarketScanRun,
    item: MarketScanResultItem,
    evidence: dict[str, object],
    verified: bool,
) -> str:
    if not verified:
        return "摘要不可验证"
    payload = _dict(evidence.get("payload"))
    evidence_date = str(payload.get("data_date") or "")
    if evidence_date != run.data_date or item.data_date != run.data_date:
        return f"日期不一致：批次 {run.data_date} / 证据 {evidence_date or '--'}"
    return f"已冻结且摘要校验通过 · 数据日 {run.data_date}"


def _source_value(
    source_field: str,
    item: MarketScanResultItem,
    run: MarketScanRun,
    board: str,
) -> object:
    if source_field.startswith("market_scan_result."):
        return getattr(item, source_field.removeprefix("market_scan_result."), _MISSING)
    if source_field == "derived.listing_days_at_data_as_of":
        listed = _date_value(item.list_date)
        return _MISSING if listed is None else max(0, (date.fromisoformat(run.data_date) - listed).days)
    if source_field == "derived.suspended_at_data_as_of":
        return _looks_untradeable(item)
    if source_field == "derived.listing_board":
        return board
    if source_field.startswith("score_details."):
        value: object = item.score_details
        for key in source_field.split(".")[1:]:
            if not isinstance(value, dict) or key not in value:
                return _MISSING
            value = value[key]
        if source_field.endswith("bar_contract_61") and isinstance(value, list):
            return len(value)
        return value
    return _MISSING


def _matches(expression: StrategyCompiledExpression, actual: object) -> bool:
    if actual is _MISSING or actual is None:
        return False
    expected = expression.value
    operator = expression.operator
    try:
        if operator == "eq":
            return bool(actual == expected)
        if operator == "ne":
            return bool(actual != expected)
        if operator in {"gt", "gte", "lt", "lte"}:
            return _ordered_match(operator, actual, expected)
        if operator == "between" and isinstance(expected, list):
            comparable = cast(Any, actual)
            return bool(expected[0] <= comparable <= expected[1])
        if operator == "in" and isinstance(expected, list):
            return actual in expected
    except (TypeError, ValueError):
        return False
    return False


def _ordered_match(operator: str, actual: object, expected: object) -> bool:
    if operator == "gt":
        return bool(actual > expected)  # type: ignore[operator]
    if operator == "gte":
        return bool(actual >= expected)  # type: ignore[operator]
    if operator == "lt":
        return bool(actual < expected)  # type: ignore[operator]
    return bool(actual <= expected)  # type: ignore[operator]


def _minimum_changes(expression: StrategyCompiledExpression, actual: object) -> list[str]:
    if actual is _MISSING or actual is None:
        return [f"补齐 {expression.field} 的冻结时点数据"]
    expected = expression.value
    if isinstance(actual, (int, float)) and not isinstance(actual, bool):
        if expression.operator in {"gt", "gte"} and isinstance(expected, (int, float)):
            return [f"{expression.field} 至少提高 {max(0.0, float(expected) - float(actual)):.4g}"]
        if expression.operator in {"lt", "lte"} and isinstance(expected, (int, float)):
            return [f"{expression.field} 至少降低 {max(0.0, float(actual) - float(expected)):.4g}"]
    return [f"调整 {expression.field} 使其满足 {expression.display}"]


def _marginal_contributions(strategy: StrategySpec, scores: dict[str, float]) -> dict[str, float]:
    weights = strategy.spec.objectives.model_dump()
    contributions = {}
    for name, weight in weights.items():
        score = scores[name]
        effective = 100 - score if name == "risk" else score
        contributions[name] = round(float(weight) * effective, 6)
    return contributions


def _complete_dimensions(scores: dict[str, float]) -> bool:
    return set(scores) == {"alpha_1d", "alpha_5d", "alpha_20d", "confidence", "risk", "tradability"}


def _rank_sensitivity(strategy: StrategySpec, candidates: list[_WorkingCandidate]) -> None:
    """Record rank shifts from a local +/-10% one-weight-at-a-time perturbation."""
    if not candidates:
        return
    base_weights = {
        name: float(value)
        for name, value in strategy.spec.objectives.model_dump().items()
    }
    for name in base_weights:
        for direction, multiplier in (("+10%", 1.1), ("-10%", 0.9)):
            weights = dict(base_weights)
            weights[name] *= multiplier
            total = sum(weights.values())
            normalized = {key: value / total for key, value in weights.items()}
            ordered = sorted(
                candidates,
                key=lambda item: (
                    -_utility_from_weights(item.scores, normalized),
                    item.item.rank or 10**9,
                    item.item.symbol,
                ),
            )
            for rank, item in enumerate(ordered, start=1):
                item.rank_sensitivity[f"{name}:{direction}"] = rank - int(item.utility_rank or rank)


def _utility_from_weights(scores: dict[str, float], weights: dict[str, float]) -> float:
    return sum(
        weight * (100 - scores[name] if name == "risk" else scores[name])
        for name, weight in weights.items()
    )


def _mark_pareto_front(candidates: list[_WorkingCandidate]) -> None:
    frontier: list[_WorkingCandidate] = []
    for candidate in candidates:
        if any(_dominates(existing, candidate) for existing in frontier):
            continue
        frontier = [existing for existing in frontier if not _dominates(candidate, existing)]
        frontier.append(candidate)
    for candidate in frontier:
        candidate.pareto = True
        candidate.reasons.append("位于 Alpha/置信度/风险/可交易性的 Pareto 前沿")


def _dominates(left: _WorkingCandidate, right: _WorkingCandidate) -> bool:
    names = ("alpha_1d", "alpha_5d", "alpha_20d", "confidence", "tradability")
    no_worse = all(left.scores[name] >= right.scores[name] for name in names) and left.scores["risk"] <= right.scores["risk"]
    better = any(left.scores[name] > right.scores[name] for name in names) or left.scores["risk"] < right.scores["risk"]
    return no_worse and better


def _apply_portfolio_constraints(
    strategy: StrategySpec,
    run: MarketScanRun,
    candidates: list[_WorkingCandidate],
    notional: float,
) -> None:
    constraints = strategy.spec.portfolio_constraints
    target_weights = _target_weights(strategy, candidates)
    selected_count = 0
    industry_counts: dict[str, int] = {}
    industry_weights: dict[str, float] = {}
    board_weights: dict[str, float] = {}
    for candidate in candidates:
        requested_weight = target_weights.get(candidate.item.symbol, 0.0)
        if not _has_requested_weight(candidate, requested_weight, constraints.weighting_method):
            continue
        if selected_count >= constraints.stock_count:
            candidate.reasons.append(
                f"组合已达到 {constraints.stock_count} 只股票上限"
            )
            candidate.changes.append("提高组合股票数量上限或等待更高排名股票退出")
            continue
        industry = candidate.item.industry or "未知行业"
        if industry_counts.get(industry, 0) >= constraints.max_industry_positions:
            candidate.reasons.append(f"行业 {industry} 已达到 {constraints.max_industry_positions} 只上限")
            candidate.changes.append("提高行业持仓上限或等待同业更高排名股票退出")
            continue
        actual_weight = _allocate_candidate(
            strategy,
            run,
            candidate,
            requested_weight=requested_weight,
            notional=notional,
            industry=industry,
            industry_weight=industry_weights.get(industry, 0.0),
            board_weight=board_weights.get(candidate.board, 0.0),
        )
        if actual_weight is None:
            continue
        selected_count += 1
        industry_counts[industry] = industry_counts.get(industry, 0) + 1
        industry_weights[industry] = industry_weights.get(industry, 0.0) + actual_weight
        board_weights[candidate.board] = board_weights.get(candidate.board, 0.0) + actual_weight


def _has_requested_weight(
    candidate: _WorkingCandidate,
    requested_weight: float,
    weighting_method: str,
) -> bool:
    if requested_weight > 0:
        return True
    if weighting_method == "custom":
        candidate.reasons.append("自定义权重未包含该股票")
        candidate.changes.append("在 custom_weights 中显式配置该股票权重")
    return False


def _allocate_candidate(
    strategy: StrategySpec,
    run: MarketScanRun,
    candidate: _WorkingCandidate,
    *,
    requested_weight: float,
    notional: float,
    industry: str,
    industry_weight: float,
    board_weight: float,
) -> float | None:
    constraints = strategy.spec.portfolio_constraints
    capacity_weight = _capacity_weight(
        candidate.item,
        constraints.max_notional_share_of_daily_amount,
        notional,
    )
    target_weight = min(requested_weight, constraints.max_stock_weight, capacity_weight)
    if target_weight * notional < constraints.min_position_amount_cny:
        candidate.status = "unfilled"
        candidate.reasons.append("流动性容量不足以达到最小名义持仓金额")
        candidate.changes.append("提高允许的日成交额容量比例或降低最小持仓金额")
        return None
    if industry_weight + target_weight > constraints.max_industry_weight + 1e-9:
        candidate.reasons.append(f"行业 {industry} 权重将超过 {constraints.max_industry_weight:.1%}")
        return None
    if board_weight + target_weight > constraints.max_board_weight + 1e-9:
        candidate.reasons.append(f"{_BOARD_LABELS[candidate.board]}权重将超过 {constraints.max_board_weight:.1%}")
        return None
    if _locked_limit_up(candidate.item, run):
        candidate.status = "unfilled"
        candidate.reasons.append("冻结日K显示一字涨停，日线模型按无法买入处理")
        return None
    quantity = _target_quantity(candidate.item, run, target_weight * notional)
    if quantity <= 0:
        candidate.status = "unfilled"
        candidate.reasons.append("目标金额不足以满足该板块最小买入数量")
        candidate.changes.append("提高名义本金或降低目标股票数量")
        return None
    return _record_candidate_allocation(
        strategy,
        candidate,
        requested_weight=requested_weight,
        notional=notional,
        quantity=quantity,
    )


def _record_candidate_allocation(
    strategy: StrategySpec,
    candidate: _WorkingCandidate,
    *,
    requested_weight: float,
    notional: float,
    quantity: int,
) -> float:
    gross = quantity * float(candidate.item.price or 0)
    actual_weight = min(1.0, gross / notional)
    candidate.weight = round(actual_weight, 8)
    candidate.quantity = quantity
    candidate.gross_amount = round(gross, 2)
    candidate.round_trip_cost = round(_round_trip_cost(strategy, gross), 2)
    adjusted = actual_weight + 1e-6 < requested_weight
    candidate.status = "constraint_adjusted" if adjusted else "selected"
    candidate.reasons.append(
        "流动性容量或最小交易单位使目标权重低于基础权重"
        if adjusted
        else "进入受约束研究组合草案"
    )
    return actual_weight


def _target_weights(
    strategy: StrategySpec,
    candidates: list[_WorkingCandidate],
) -> dict[str, float]:
    constraints = strategy.spec.portfolio_constraints
    if constraints.weighting_method == "custom":
        return dict(constraints.custom_weights)
    pool = candidates[: constraints.stock_count]
    if not pool:
        return {}
    if constraints.weighting_method == "risk_adjusted":
        raw = {
            item.item.symbol: 1.0 / max(5.0, float(item.scores["risk"]))
            for item in pool
        }
    else:
        raw = {item.item.symbol: 1.0 for item in pool}
    return _normalize_capped_weights(raw, cap=constraints.max_stock_weight)


def _normalize_capped_weights(raw: dict[str, float], *, cap: float) -> dict[str, float]:
    remaining = 1.0
    active = dict(raw)
    weights: dict[str, float] = {}
    while active and remaining > 1e-12:
        total = sum(active.values())
        proposed = {
            symbol: remaining * value / total
            for symbol, value in active.items()
        }
        capped = [symbol for symbol, value in proposed.items() if value > cap + 1e-12]
        if not capped:
            weights.update(proposed)
            break
        for symbol in capped:
            weights[symbol] = cap
            remaining -= cap
            active.pop(symbol)
    return {symbol: round(weight, 10) for symbol, weight in weights.items()}


def _finalize_unselected_reasons(strategy: StrategySpec, eligible: list[_WorkingCandidate]) -> None:
    selected = [item for item in eligible if item.status in {"selected", "constraint_adjusted"}]
    cutoff = min((float(item.utility or 0) for item in selected), default=None)
    for candidate in eligible:
        if candidate.status != "rejected":
            candidate.reasons.append(
                f"生产原始排名 {candidate.item.rank or '--'}，多目标效用排名 {candidate.utility_rank or '--'}"
            )
            continue
        if not candidate.reasons:
            candidate.reasons.append(
                f"多目标效用排名超出 {strategy.spec.portfolio_constraints.stock_count} 只组合容量"
            )
        if cutoff is not None and candidate.utility is not None and candidate.utility < cutoff:
            candidate.changes.append(f"多目标效用至少提高 {cutoff - candidate.utility:.4f}")


def _capacity_weight(item: MarketScanResultItem, share: float, notional: float) -> float:
    amount = float(item.amount or 0)
    return max(0.0, min(1.0, amount * share / notional))


def _target_quantity(item: MarketScanResultItem, run: MarketScanRun, allocation: float) -> int:
    price = float(item.price or 0)
    if price <= 0:
        return 0
    metadata = PaperInstrumentMetadata(
        symbol=item.symbol,
        name=item.name,
        market=item.market,
        list_date=item.list_date,
        is_st=item.is_st,
        source=item.metadata_source,
        status_effective_date=run.data_date,
    )
    profile = resolve_trade_rule_profile(item.symbol, date.fromisoformat(run.data_date), metadata)
    maximum = floor(allocation / price)
    if maximum < profile.min_buy_quantity:
        return 0
    return profile.min_buy_quantity + floor(
        (maximum - profile.min_buy_quantity) / profile.buy_quantity_step
    ) * profile.buy_quantity_step


def _locked_limit_up(item: MarketScanResultItem, run: MarketScanRun) -> bool:
    _dimensions, evidence = _dimensions_and_evidence(item)
    payload = _dict(evidence.get("payload"))
    bars = payload.get("bar_contract_61")
    if not isinstance(bars, list) or len(bars) < 2:
        return False
    previous, current = bars[-2], bars[-1]
    if not isinstance(previous, list) or not isinstance(current, list) or len(previous) < 5 or len(current) < 5:
        return False
    try:
        previous_close = float(previous[2])
        open_price, close, high, low = map(float, current[1:5])
    except (TypeError, ValueError):
        return False
    if max(open_price, close, high, low) - min(open_price, close, high, low) > 1e-8:
        return False
    metadata = PaperInstrumentMetadata(
        symbol=item.symbol,
        name=item.name,
        market=item.market,
        list_date=item.list_date,
        is_st=item.is_st,
        source=item.metadata_source,
        status_effective_date=run.data_date,
    )
    profile = resolve_trade_rule_profile(item.symbol, date.fromisoformat(run.data_date), metadata)
    if profile.price_limit_pct is None or previous_close <= 0:
        return False
    return close >= previous_close * (1 + profile.price_limit_pct / 100) - 0.011


def _round_trip_cost(strategy: StrategySpec, gross: float) -> float:
    policy = strategy.spec.execution_policy
    buy_commission = max(gross * policy.commission_rate, policy.minimum_commission_cny)
    sell_commission = max(gross * policy.commission_rate, policy.minimum_commission_cny)
    transfer = gross * policy.transfer_fee_rate * 2
    stamp = gross * policy.sell_stamp_duty_rate
    slippage = gross * (policy.buy_slippage_bps + policy.sell_slippage_bps) / 10_000
    return buy_commission + sell_commission + transfer + stamp + slippage


def _to_candidate(value: _WorkingCandidate) -> PortfolioCandidate:
    scores = value.scores
    return PortfolioCandidate(
        symbol=value.item.symbol,
        code=value.item.code,
        name=value.item.name,
        board=value.board,
        board_label=_BOARD_LABELS[value.board],
        industry=value.item.industry,
        original_rank=value.item.rank,
        utility_rank=value.utility_rank,
        utility_score=value.utility,
        alpha_1d=scores.get("alpha_1d"),
        alpha_5d=scores.get("alpha_5d"),
        alpha_20d=scores.get("alpha_20d"),
        confidence=scores.get("confidence"),
        risk=scores.get("risk"),
        tradability=scores.get("tradability"),
        pareto_front=value.pareto,
        status=value.status,
        target_weight=value.weight,
        target_quantity=value.quantity,
        estimated_gross_amount_cny=value.gross_amount,
        estimated_round_trip_cost_cny=value.round_trip_cost,
        evidence_verified=value.evidence_verified,
        evidence_freshness=value.freshness,
        hard_filter_failures=value.failures,
        marginal_contributions=value.contributions,
        reasons=value.reasons,
        minimum_changes=list(dict.fromkeys(value.changes)),
        rank_sensitivity=value.rank_sensitivity,
        rank_change_reason=(
            f"生产原始排名 {value.item.rank or '--'} 保持不变；"
            f"策略多目标效用排名 {value.utility_rank or '--'}"
        ),
    )


def _portfolio_summary(
    strategy: StrategySpec,
    candidates: list[PortfolioCandidate],
    request: StrategyExecutionRequest,
) -> PortfolioDraftSummary:
    selected = _selected_candidates(candidates)
    no_trade_reasons = _no_trade_reasons(strategy, selected)
    turnover = _estimated_turnover(selected, request.current_weights)
    invested = min(1.0, _sum_candidate_field(selected, "target_weight"))
    gross = _sum_candidate_field(selected, "estimated_gross_amount_cny")
    costs = _sum_candidate_field(selected, "estimated_round_trip_cost_cny")
    no_trade = not selected
    return PortfolioDraftSummary(
        status="no_trade" if no_trade else "ready",
        no_trade=no_trade,
        no_trade_reasons=no_trade_reasons,
        evaluated_count=len(candidates),
        eligible_count=_eligible_candidate_count(candidates),
        selected_count=len(selected),
        rejected_count=_candidate_status_count(candidates, "rejected"),
        adjusted_count=_candidate_status_count(candidates, "constraint_adjusted"),
        unfilled_count=_candidate_status_count(candidates, "unfilled"),
        target_invested_weight=round(invested, 8),
        estimated_turnover=round(min(2.0, turnover), 8),
        estimated_round_trip_cost_cny=round(costs, 2),
        residual_cash_cny=round(max(0.0, request.notional_cash_cny - gross - costs), 2),
        evidence_verified_count=_verified_candidate_count(candidates),
        notes=[
            "组合草案不修改 full-market-score-v4 的生产原始排名。",
            f"组合权重方式为 {strategy.spec.portfolio_constraints.weighting_method}；买入/持有迟滞已参与确定性准入。",
            "Alpha、置信度、风险和可交易性是序数研究分，不是收益概率。",
            "日K模型无法证明真实盘口排队与成交，结果仅用于研究和模拟。",
            "策略是否有效仍受独立扫描日期、成本、暴露和PBO晋级门槛约束。",
        ],
    )


def _selected_candidates(candidates: list[PortfolioCandidate]) -> list[PortfolioCandidate]:
    return [
        item
        for item in candidates
        if item.status in {"selected", "constraint_adjusted"}
    ]


def _no_trade_reasons(
    strategy: StrategySpec,
    selected: list[PortfolioCandidate],
) -> list[str]:
    reasons: list[str] = []
    if not selected:
        reasons.append("没有候选同时通过硬过滤、时点证据、流动性和组合约束")
    if strategy.spec.evidence_policy.require_verified_point_in_time_evidence and not any(
        item.evidence_verified for item in selected
    ):
        reasons.append("没有入选候选具备可验证的冻结时点证据")
    return reasons


def _estimated_turnover(
    selected: list[PortfolioCandidate],
    current_weights: dict[str, float],
) -> float:
    target = {item.symbol: item.target_weight for item in selected}
    return sum(
        abs(target.get(symbol, 0.0) - current_weights.get(symbol, 0.0))
        for symbol in set(target) | set(current_weights)
    )


def _sum_candidate_field(candidates: list[PortfolioCandidate], field_name: str) -> float:
    return sum(float(getattr(item, field_name)) for item in candidates)


def _eligible_candidate_count(candidates: list[PortfolioCandidate]) -> int:
    return sum(
        item.utility_score is not None and not item.hard_filter_failures
        for item in candidates
    )


def _candidate_status_count(candidates: list[PortfolioCandidate], status: str) -> int:
    return sum(item.status == status for item in candidates)


def _verified_candidate_count(candidates: list[PortfolioCandidate]) -> int:
    return sum(item.evidence_verified for item in candidates)


def _cost_rule_fingerprint(strategy: StrategySpec) -> str:
    return _stable_digest(
        {
            "paper_trading_rule_version": PAPER_TRADING_RULE_VERSION,
            "execution_policy": strategy.spec.execution_policy.model_dump(mode="json"),
        }
    )


def _execution_fingerprint(
    strategy: StrategySpec,
    run: MarketScanRun,
    request: StrategyExecutionRequest,
    *,
    cost_rule_fingerprint: str,
) -> str:
    return _stable_digest(
        {
            "strategy_id": strategy.strategy_id,
            "strategy_version": strategy.strategy_version,
            "strategy_fingerprint": strategy.fingerprint,
            "market_scan_run_id": run.id,
            "rule_version": run.rule_version,
            "data_as_of": run.as_of,
            "data_date": run.data_date,
            "cost_rule_fingerprint": cost_rule_fingerprint,
            "execution_request": {
                "kind": request.kind,
                "mode": request.mode,
                "notional_cash_cny": request.notional_cash_cny,
                "current_weights": dict(sorted(request.current_weights.items())),
            },
        }
    )


def _looks_untradeable(item: MarketScanResultItem) -> bool:
    reason = f"{item.reason or ''} {item.error or ''}"
    return bool(item.amount == 0 or any(word in reason for word in ("停牌", "零成交", "一字")))


def _date_value(value: str | None) -> date | None:
    if not value:
        return None
    normalized = value.strip()
    if len(normalized) == 8 and normalized.isdigit():
        normalized = f"{normalized[:4]}-{normalized[4:6]}-{normalized[6:]}"
    try:
        return date.fromisoformat(normalized)
    except ValueError:
        return None


def _dict(value: object) -> dict[str, Any]:
    return value if isinstance(value, dict) else {}


def _stable_digest(value: object) -> str:
    encoded = json.dumps(
        value,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
        allow_nan=False,
    )
    return hashlib.sha256(encoded.encode("utf-8")).hexdigest()


__all__ = ["PortfolioComputation", "build_portfolio_draft", "strategy_board"]
