"""Paired descriptive comparisons of frozen strategy-template selections."""

from __future__ import annotations

from collections.abc import Iterable, Mapping, Sequence
from collections import Counter, defaultdict
from dataclasses import asdict, dataclass, field
from datetime import date, datetime
from math import fsum, isclose, isfinite

from app.services.trading_calendar import TradingCalendarCoverageError, latest_expected_daily_kline_date, next_trade_dates


STRATEGY_TEMPLATE_TRACKING_SCHEMA_VERSION = "strategy-template-tracking-v2"
STRATEGY_TEMPLATE_TRACKING_IDS = ("bounded_medium_trend", "medium_momentum", "low_volatility_trend")
STRATEGY_TEMPLATE_TRACKING_BASELINE = "bounded_medium_trend"


@dataclass(frozen=True)
class StrategyTrackingPosition:
    symbol: str
    name: str
    industry: str | None
    target_weight: float
    signal_price: float
    forward_return: float | None = None
    outcome_reason: str | None = None


@dataclass(frozen=True)
class StrategyTrackingSelection:
    template_id: str
    status: str
    evaluated_count: int
    eligible_count: int
    positions: tuple[StrategyTrackingPosition, ...]
    target_invested_weight: float
    residual_cash_cny: float
    estimated_round_trip_cost_cny: float
    execution_fingerprint: str
    result_digest: str
    outcome_status: str
    weighted_gross_return: float | None
    available_outcome_count: int = 0
    rejection_counts: Mapping[str, int] = field(default_factory=dict)
    missing_reason_counts: Mapping[str, int] = field(default_factory=dict)
    reasons: tuple[str, ...] = ()


@dataclass(frozen=True)
class StrategyTrackingSession:
    run_id: int
    signal_date: str
    target_date: str | None
    rule_version: str
    score_spec_hash: str
    snapshot_digest: str
    published_at: str
    selections: tuple[StrategyTrackingSelection, ...]


def build_strategy_template_tracking_report(
    sessions: Sequence[StrategyTrackingSession],
    *,
    templates: Sequence[Mapping[str, object]],
    as_of: str,
    completed_date: str,
    horizon: int,
    notional_cash_cny: float,
    source: Mapping[str, object],
    run_failures: Sequence[Mapping[str, object]] = (),
    excluded_runs: Sequence[Mapping[str, object]] = (),
) -> dict[str, object]:
    """Aggregate only same-contract, same-date pairs; no NAV or promotion."""
    validate_strategy_tracking_inputs(sessions, templates, horizon, notional_cash_cny)
    _validate_report_dates(sessions, as_of, completed_date, horizon)
    grouped: dict[tuple[str, str], list[StrategyTrackingSession]] = defaultdict(list)
    for session in sessions:
        grouped[(session.rule_version, session.score_spec_hash)].append(session)
    cohorts = [_cohort(rows, rule, digest) for (rule, digest), rows in sorted(grouped.items())]
    paired = any(item["paired_session_count"] for cohort in cohorts for item in cohort["comparisons"])
    return {
        "schema_version": STRATEGY_TEMPLATE_TRACKING_SCHEMA_VERSION,
        "generated_at": as_of, "as_of_completed_date": completed_date,
        "status": "descriptive_only" if paired and not run_failures else "insufficient_data",
        "horizon_sessions": horizon, "notional_cash_cny": notional_cash_cny,
        "baseline_template_id": STRATEGY_TEMPLATE_TRACKING_BASELINE,
        "template_ids": list(STRATEGY_TEMPLATE_TRACKING_IDS),
        "templates": [dict(item) for item in templates],
        "selection_policy": "earliest-published-full-market-official-per-score-contract-session",
        "evaluation_kind": "retrospective-template-diagnostic",
        "return_target": "frozen-signal-price-to-fixed-D+H-close-weighted-gross-return",
        "return_unit": "decimal_fraction", "costs_deducted": False,
        "continuous_portfolio_simulation": False, "statistical_validation": "not_performed",
        "promotion_eligible": False, "prediction_accuracy_validated": False,
        "source": dict(source), "run_failures": [dict(item) for item in run_failures],
        "excluded_runs": [dict(item) for item in excluded_runs],
        "source_session_count": len(sessions), "cohorts": cohorts,
        "limitations": _report_limitations(),
    }


def validate_strategy_tracking_inputs(
    sessions: Sequence[StrategyTrackingSession], templates: Sequence[Mapping[str, object]],
    horizon: int, notional_cash_cny: float,
) -> None:
    if isinstance(horizon, bool) or not isinstance(horizon, int) or horizon not in (1, 5, 10, 20):
        raise ValueError("unsupported strategy tracking horizon")
    if not _finite(notional_cash_cny) or not 10_000 <= notional_cash_cny <= 1_000_000_000:
        raise ValueError("invalid strategy tracking notional")
    if Counter(item.get("template_id") for item in templates) != Counter(STRATEGY_TEMPLATE_TRACKING_IDS):
        raise ValueError("tracking requires the fixed three-template comparison")
    identities: set[tuple[str, str, str]] = set()
    run_ids: set[int] = set()
    for session in sessions:
        key = (session.rule_version, session.score_spec_hash, session.signal_date)
        if key in identities or session.run_id in run_ids:
            raise ValueError("duplicate frozen session in strategy tracking")
        identities.add(key)
        run_ids.add(session.run_id)
        _validate_session(session)


def _validate_session(session: StrategyTrackingSession) -> None:
    date.fromisoformat(session.signal_date)
    if session.target_date is not None and date.fromisoformat(session.target_date) <= date.fromisoformat(session.signal_date):
        raise ValueError("target date must follow the signal")
    for digest in (session.score_spec_hash, session.snapshot_digest):
        if len(digest) != 64 or any(char not in "0123456789abcdef" for char in digest):
            raise ValueError("unverified contract or snapshot identity")
    if Counter(item.template_id for item in session.selections) != Counter(STRATEGY_TEMPLATE_TRACKING_IDS):
        raise ValueError("session must include all comparison templates")
    for selection in session.selections:
        _validate_selection(selection)


def _validate_report_dates(sessions: Sequence[StrategyTrackingSession], as_of: str, completed_date: str, horizon: int) -> None:
    observed = datetime.fromisoformat(as_of)
    if observed.tzinfo is None or observed.utcoffset() is None:
        raise ValueError("report observation time requires a timezone")
    completed = date.fromisoformat(completed_date)
    if latest_expected_daily_kline_date(observed, allow_auto_refresh=False) != completed:
        raise ValueError("completed-date cutoff does not match observation time")
    for session in sessions:
        signal = date.fromisoformat(session.signal_date)
        if signal > completed:
            raise ValueError("signal date is not a completed session")
        try:
            expected = next_trade_dates(signal, horizon, allow_auto_refresh=False)[-1].isoformat()
        except TradingCalendarCoverageError:
            expected = None
        if session.target_date != expected:
            raise ValueError("target date does not match the fixed horizon")
        if session.target_date is None or session.target_date > completed_date:
            if any(item.outcome_status == "available" for item in session.selections):
                raise ValueError("immature outcome cannot be available")


def _validate_selection(selection: StrategyTrackingSelection) -> None:
    if selection.status not in ("ready", "no_trade", "blocked"):
        raise ValueError("unknown selection status")
    if selection.outcome_status not in ("available", "pending", "missing", "no_selection", "calendar_unavailable", "blocked"):
        raise ValueError("unknown outcome status")
    if not _finite(selection.target_invested_weight) or not 0 <= selection.target_invested_weight <= 1:
        raise ValueError("invalid invested weight")
    for amount in (selection.residual_cash_cny, selection.estimated_round_trip_cost_cny):
        if not _finite(amount) or amount < 0:
            raise ValueError("invalid cash or cost estimate")
    if len({item.symbol for item in selection.positions}) != len(selection.positions):
        raise ValueError("duplicate selected symbol")
    for item in selection.positions:
        _validate_position(item)
    if not isclose(sum(item.target_weight for item in selection.positions), selection.target_invested_weight, abs_tol=1e-7):
        raise ValueError("frozen weights disagree with invested weight")
    _validate_selection_outcome(selection)


def _validate_position(item: StrategyTrackingPosition) -> None:
    if not _finite(item.target_weight) or not 0 < item.target_weight <= 1:
        raise ValueError("invalid frozen position weight")
    if not _finite(item.signal_price) or item.signal_price <= 0:
        raise ValueError("invalid frozen signal price")
    if item.forward_return is not None and (not _finite(item.forward_return) or item.forward_return < -1):
        raise ValueError("invalid forward return")


def _validate_selection_outcome(selection: StrategyTrackingSelection) -> None:
    available = sum(item.forward_return is not None for item in selection.positions)
    if selection.available_outcome_count != available:
        raise ValueError("outcome coverage does not match selected members")
    if selection.outcome_status != "available":
        if selection.weighted_gross_return is not None:
            raise ValueError("unavailable outcome must not become zero or a partial mean")
        return
    if selection.status != "ready" or not selection.positions or available != len(selection.positions):
        raise ValueError("available outcome requires a complete nonempty frozen basket")
    value = selection.weighted_gross_return
    expected = sum(item.target_weight * float(item.forward_return or 0) for item in selection.positions)
    if value is None or not _finite(value) or not isclose(value, expected, rel_tol=1e-9, abs_tol=1e-12):
        raise ValueError("weighted return must use original weights without redistribution")


def _finite(value: object) -> bool:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return False
    try:
        return isfinite(value)
    except OverflowError:
        return False


def _mean(values: Iterable[float]) -> float:
    rows = tuple(values)
    scale = max(abs(value) for value in rows)
    return scale * (fsum(value / scale for value in rows) / len(rows)) if scale else 0.0


def _cohort(rows: list[StrategyTrackingSession], rule: str, digest: str) -> dict:
    ordered = sorted(rows, key=lambda item: (item.signal_date, item.run_id))
    comparisons = [_comparison(ordered, template) for template in STRATEGY_TEMPLATE_TRACKING_IDS[1:]]
    return {
        "rule_version": rule, "score_spec_hash": digest, "session_count": len(rows),
        "template_summaries": [_template_summary(ordered, template) for template in STRATEGY_TEMPLATE_TRACKING_IDS],
        "comparisons": comparisons,
        "sessions": [_session_payload(item) for item in ordered],
    }


def _session_payload(session: StrategyTrackingSession) -> dict[str, object]:
    payload = asdict(session)
    selections = []
    for item in session.selections:
        value = asdict(item)
        exposure: dict[str, float] = defaultdict(float)
        for position in item.positions:
            exposure[position.industry or "行业未知"] += position.target_weight
        value.update(selected_count=len(item.positions),
                     unallocated_weight=max(0.0, 1.0 - item.target_invested_weight),
                     industry_weights=dict(sorted(exposure.items())))
        selections.append(value)
    payload["selections"] = selections
    return payload


def _template_summary(rows: Sequence[StrategyTrackingSession], template_id: str) -> dict[str, object]:
    selections = [next(item for item in row.selections if item.template_id == template_id) for row in rows]
    counts = Counter(item.outcome_status for item in selections)
    return {
        "template_id": template_id, "session_count": len(rows), "outcome_counts": dict(sorted(counts.items())),
        "mean_selected_count": _mean(len(item.positions) for item in selections),
        "mean_target_invested_weight": _mean(item.target_invested_weight for item in selections),
        "mean_estimated_round_trip_cost_cny": _mean(item.estimated_round_trip_cost_cny for item in selections),
    }


def _comparison(rows: Sequence[StrategyTrackingSession], template_id: str) -> dict[str, object]:
    paired: list[tuple[StrategyTrackingSelection, StrategyTrackingSelection]] = []
    exclusions: Counter[str] = Counter()
    dates: list[str] = []
    overlap: list[int] = []
    for row in rows:
        by_id = {item.template_id: item for item in row.selections}
        candidate, baseline = by_id[template_id], by_id[STRATEGY_TEMPLATE_TRACKING_BASELINE]
        overlap.append(len({item.symbol for item in candidate.positions} & {item.symbol for item in baseline.positions}))
        if candidate.outcome_status == baseline.outcome_status == "available":
            paired.append((candidate, baseline))
            dates.append(row.signal_date)
        else:
            exclusions[f"candidate:{candidate.outcome_status}|baseline:{baseline.outcome_status}"] += 1
    candidate_values = [float(item.weighted_gross_return) for item, _ in paired if item.weighted_gross_return is not None]
    baseline_values = [float(item.weighted_gross_return) for _, item in paired if item.weighted_gross_return is not None]
    differences = [left - right for left, right in zip(candidate_values, baseline_values, strict=True)]
    return {
        "template_id": template_id, "baseline_template_id": STRATEGY_TEMPLATE_TRACKING_BASELINE,
        "paired_session_count": len(paired), "paired_dates": dates,
        "excluded_pair_counts": dict(sorted(exclusions.items())),
        "candidate_average_return": _mean(candidate_values) if paired else None,
        "baseline_average_return": _mean(baseline_values) if paired else None,
        "candidate_minus_baseline_return": _mean(differences) if paired else None,
        "mean_selected_overlap_count": _mean(overlap),
        "confidence_interval_95": None, "statistical_validation": "not_performed",
    }


def _report_limitations() -> list[str]:
    return [
        "当前模板回看已发生的历史，仅属描述性比较，不是预先登记的样本外检验或收益优势证明。",
        "按冻结信号价格至固定D+H收盘观察毛收益；信号收盘后才发布，不能据此假设在该收盘价成交。",
        "目标权重在读取未来价格前固定；入选股任何结果缺失都使该批次不可比较，不补位、不重新归一。",
        "未配置权重按零现金收益计算诊断；无入选股仍记为无选择，不虚构为收益为零的成功样本。",
        "各评分合同分别比较，双方使用同一信号日和固定期限，各配对日期等权，重扫不增加样本。",
        "毛收益未扣成本；展示的往返成本只是信号价格下的独立草案估算，不是已实现交易成本。",
        "未模拟成交、连续现金账本、持仓延续或调仓；不能将重叠批次的收益相乘为净值。",
        "缺失、待到期及封存失败均单列；不生成胜率、Sharpe、统计置信区间或自动晋级结论。",
    ]
