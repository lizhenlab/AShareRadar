"""Read-only frozen-template selections, gross diagnostics and net comparisons."""

from __future__ import annotations

from collections import Counter, defaultdict
from collections.abc import Callable, Iterable, Mapping, Sequence
from contextlib import closing
from dataclasses import dataclass, replace
from datetime import date, datetime, time
import math
from pathlib import Path
import sqlite3
from typing import Literal, TypeVar, cast

from app.db.market_scan_integrity import MarketScanSnapshotSealError, require_publication_market_scan_snapshot
from app.models.market_scan import MARKET_SCAN_FULL_MARKET_SCOPE, MarketScanResultItem, MarketScanRun
from app.models.market_scan_snapshot import validate_frozen_full_market_snapshot
from app.models.market_strategy_templates import MarketStrategyTemplate
from app.models.strategy_execution import StrategyExecutionRequest
from app.models.strategy_lab import StrategySpec
from app.repositories.market_scan_mapping import result_from_row, run_from_row
from app.services.market_scan_evaluation_price_basis import forward_price_basis_status, valid_forward_price_bar
from app.services.market_scan_evaluation_source import frozen_score_contract, portable_database_label
from app.services.market_scan_official_execution import VerifiedOfficialExecutionSession
from app.services.market_scan_score_dimensions import verify_market_scan_point_in_time_evidence_context
from app.services.market_scan_research_experiment import validate_research_decision_time
from app.services.market_strategy_templates import market_strategy_template_catalog
from app.services.strategy_compiler import strategy_spec_fingerprint
from app.services.strategy_portfolio import PortfolioComputation, build_portfolio_draft
from app.services.strategy_template_net_returns import evaluate_strategy_template_net_returns
from app.services.strategy_template_selection import build_strategy_template_selection_report
from app.services.strategy_template_tracking_metrics import (
    STRATEGY_TEMPLATE_TRACKING_IDS, StrategyTrackingPosition, StrategyTrackingSelection,
    StrategyTrackingSession, build_strategy_template_tracking_report,
)
from app.services.trading_calendar import (
    ASHARE_TIMEZONE, TradingCalendarCoverageError, latest_expected_daily_kline_date, next_trade_dates,
    trading_dates_between,
)
from app.utils.audit_time import audit_time_epoch
from app.utils.clock import utc_now
from app.utils.market_time import market_datetime_epoch


MAX_STRATEGY_TRACKING_SESSIONS = 200
_MAX_SCAN_HEADERS = 10_000
_HORIZONS = (1, 5, 10, 20)
_AdmissionReason = Literal[
    "snapshot_seal_invalid", "frozen_snapshot_invalid", "signal_availability_invalid",
    "decision_time_invalid", "missing_frozen_universe", "score_contract_unregistered", "pit_feature_binding_invalid",
]
_T = TypeVar("_T")


class _TrackingAdmissionError(ValueError):
    def __init__(self, reason: _AdmissionReason) -> None:
        self.reason = reason
        super().__init__(reason)


def _admission_step(reason: _AdmissionReason, operation: Callable[[], _T]) -> _T:
    try:
        return operation()
    except (MarketScanSnapshotSealError, ValueError, TypeError, KeyError, OverflowError) as exc:
        raise _TrackingAdmissionError(reason) from exc


@dataclass(frozen=True)
class _TrackingContext:
    as_of: datetime
    completed: date
    horizon: int
    notional: float
    templates: Sequence[Mapping[str, object]]


def evaluate_strategy_template_tracking(
    database_path: Path, *, as_of: datetime | None = None, horizon: int = 10,
    notional_cash_cny: float = 1_000_000.0, run_ids: Sequence[int] | None = None,
    official_sessions: Sequence[VerifiedOfficialExecutionSession] = (),
    non_overlapping_signals: bool = False,
) -> dict[str, object]:
    """Freeze three independent drafts before reading any forward labels."""
    timestamp = _validated_inputs(as_of, horizon, notional_cash_cny, run_ids)
    if type(non_overlapping_signals) is not bool or (non_overlapping_signals and run_ids is not None):
        raise ValueError("fixed non-overlapping signals cannot be combined with explicit run IDs")
    _strategies, templates, contracts = strategy_template_tracking_contracts()
    context = _TrackingContext(timestamp, latest_expected_daily_kline_date(timestamp, allow_auto_refresh=False), horizon, notional_cash_cny, templates)
    path = Path(database_path).resolve()
    sessions: list[StrategyTrackingSession] = []
    failures: list[dict[str, object]] = []
    with closing(sqlite3.connect(f"{path.as_uri()}?mode=ro", uri=True)) as conn:
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA query_only = ON")
        conn.execute("BEGIN")
        runs, excluded = _tracking_runs(conn, timestamp, run_ids, signal_spacing=horizon + 1 if non_overlapping_signals else 1)
        for row in runs:
            try:
                sessions.append(_tracking_session(conn, row, context))
            except (MarketScanSnapshotSealError, ValueError, TypeError, KeyError, OverflowError) as exc:
                failures.append({
                    "run_id": int(row["id"]), "signal_date": str(row["data_date"]),
                    "rule_version": str(row["rule_version"]), "score_spec_hash": row["declared_score_spec_hash"],
                    "reason": exc.reason if isinstance(exc, _TrackingAdmissionError) else "frozen_snapshot_unavailable",
                    "error_type": type(exc.__cause__ or exc).__name__,
                })
    report = build_strategy_template_tracking_report(
        sessions, templates=templates, as_of=timestamp.isoformat(), completed_date=context.completed.isoformat(),
        horizon=horizon, notional_cash_cny=notional_cash_cny,
        source=_tracking_source(path, len(runs), len(failures), contracts, run_ids is not None),
        run_failures=failures, excluded_runs=excluded,
    )
    net_report = evaluate_strategy_template_net_returns(
        sessions, templates, as_of=timestamp, horizon=horizon,
        notional_cash_cny=notional_cash_cny, official_sessions=official_sessions,
    )
    report["net_comparison"] = net_report
    report["strategy_selection"] = build_strategy_template_selection_report(net_report, run_failures=failures)
    report["non_overlapping_signals"] = non_overlapping_signals
    return report


def _tracking_source(path: Path, selected: int, failed: int, contracts: dict[str, object], filtered: bool) -> dict[str, object]:
    return {
        "database": portable_database_label(path), "read_only": True, "single_read_transaction": True,
        "provider_calls": 0, "calendar_auto_refresh": False,
        "model_fitting_performed": False, "production_ranking_mutated": False,
        "strategy_or_execution_rows_written": 0, "selected_run_count": selected, "failed_run_count": failed,
        "maximum_session_count": MAX_STRATEGY_TRACKING_SESSIONS, "source_contracts": contracts,
        "selection_policy": "earliest-published-per-contract-session-no-failure-fallback",
        "run_id_filter_applied": filtered, "required_mode": "official", "required_scope": MARKET_SCAN_FULL_MARKET_SCOPE,
        "publication_policy": "original-publication-seal-before-next-session-open",
        "outcome_policy": "signal-close-to-fixed-session-close;complete-selected-positions;frozen-weights;unallocated-cash-zero",
        "corporate_action_policy": "all-path-bars-explicit-none",
        "gross_report_costs_deducted": False, "continuous_portfolio_simulated": False,
    }


def _validated_inputs(as_of: datetime | None, horizon: int, notional: float, run_ids: Sequence[int] | None) -> datetime:
    if isinstance(horizon, bool) or not isinstance(horizon, int) or horizon not in _HORIZONS:
        raise ValueError("策略对照只支持 1/5/10/20 个交易日")
    StrategyExecutionRequest(strategy_id=1, notional_cash_cny=notional)
    if run_ids is not None and (len(run_ids) > _MAX_SCAN_HEADERS or any(
        isinstance(value, bool) or not isinstance(value, int) or value <= 0 for value in run_ids
    )):
        raise ValueError("run_ids 必须是不超过10000项的正整数")
    now = utc_now()
    timestamp = as_of or now
    if timestamp.tzinfo is None or timestamp.utcoffset() is None:
        timestamp = timestamp.replace(tzinfo=ASHARE_TIMEZONE)
    if timestamp > now:
        raise ValueError("策略对照 as_of 不能晚于当前时间")
    return timestamp


def strategy_template_tracking_contracts() -> tuple[dict[str, StrategySpec], list[dict[str, object]], dict[str, object]]:
    """Return the current fixed family, complete template bytes and source contracts."""
    catalog = market_strategy_template_catalog()
    by_id = {item.template_id: item for item in catalog.templates}
    metadata: list[dict[str, object]] = []
    stamp = f"{catalog.as_of_date}T00:00:00+08:00"
    for template_id in STRATEGY_TEMPLATE_TRACKING_IDS:
        template = by_id[template_id]
        if template.strategy_spec is None or template.availability != "available_for_draft":
            raise ValueError("固定策略模板不可载入")
        fingerprint = strategy_spec_fingerprint(template.strategy_spec)
        metadata.append({**template.model_dump(mode="json"), "strategy_fingerprint": fingerprint})
    return _frozen_template_strategies(metadata, stamp), metadata, catalog.source_contracts.model_dump(mode="json")


def _frozen_template_strategies(templates: Sequence[Mapping[str, object]], stamp: str) -> dict[str, StrategySpec]:
    if len(templates) != len(STRATEGY_TEMPLATE_TRACKING_IDS):
        raise ValueError("固定策略模板数量不一致")
    verified: dict[str, MarketStrategyTemplate] = {}
    for item in templates:
        template = MarketStrategyTemplate.model_validate({key: value for key, value in item.items() if key != "strategy_fingerprint"})
        if template.strategy_spec is None or template.availability != "available_for_draft":
            raise ValueError("固定策略模板不可载入")
        if item.get("strategy_fingerprint") != strategy_spec_fingerprint(template.strategy_spec):
            raise ValueError("冻结策略模板指纹不一致")
        verified[template.template_id] = template
    if set(verified) != set(STRATEGY_TEMPLATE_TRACKING_IDS):
        raise ValueError("固定策略模板身份不一致")
    strategies: dict[str, StrategySpec] = {}
    for number, template_id in enumerate(STRATEGY_TEMPLATE_TRACKING_IDS, 1):
        template = verified[template_id]
        assert template.strategy_spec is not None
        strategies[template_id] = StrategySpec(
            strategy_id=number, strategy_version=template.version, revision=template.version,
            current_revision=template.version, archived=False, fingerprint=strategy_spec_fingerprint(template.strategy_spec), spec=template.strategy_spec,
            created_at=stamp, updated_at=stamp, version_created_at=stamp,
        )
    return strategies


def _tracking_runs(
    conn: sqlite3.Connection, as_of: datetime, run_ids: Sequence[int] | None,
    *, signal_spacing: int = 1,
) -> tuple[list[sqlite3.Row], list[dict[str, object]]]:
    clauses = ["r.status IN ('success', 'degraded')", "r.mode = 'official'", "r.scope = ?"]
    parameters: list[object] = [MARKET_SCAN_FULL_MARKET_SCOPE]
    if run_ids is not None:
        ids = tuple(dict.fromkeys(run_ids))
        if not ids:
            return [], []
        # Explicit IDs identify relevant cohorts, never license selecting a
        # later rescan after seeing its outcomes. Canonicalize globally first.
        clauses.append(f"(r.data_date,r.rule_version) IN (SELECT data_date,rule_version FROM market_scan_run WHERE id IN ({','.join('?' for _ in ids)}))")
        parameters.extend(ids)
    rows = conn.execute(
        f"""SELECT r.*, c.production_score_spec_hash AS declared_score_spec_hash
            FROM market_scan_run r LEFT JOIN market_scan_rule_contract c ON c.rule_version = r.rule_version
            WHERE {' AND '.join(clauses)} ORDER BY r.id LIMIT ?""", [*parameters, _MAX_SCAN_HEADERS + 1],
    ).fetchall()
    if len(rows) > _MAX_SCAN_HEADERS:
        raise ValueError("策略对照批次头超过10000条，请缩小run_ids范围")
    ordered = sorted(rows, key=lambda row: (audit_time_epoch(row["finished_at"]) or float("-inf"), int(row["id"])))
    chosen: dict[tuple[str, str, str], sqlite3.Row] = {}
    excluded: list[dict[str, object]] = []
    for row in ordered:
        published = audit_time_epoch(row["finished_at"])
        if published is not None and published > as_of.timestamp():
            excluded.append({"run_id": int(row["id"]), "reason": "published_after_as_of"})
            continue
        key = (str(row["data_date"]), str(row["rule_version"]), str(row["declared_score_spec_hash"] or "unregistered"))
        if key in chosen:
            excluded.append({"run_id": int(row["id"]), "reason": "same_contract_session_rescan"})
        else:
            chosen[key] = row
    scheduled = _scheduled_tracking_runs(list(chosen.values()), signal_spacing, excluded)
    if len(scheduled) > MAX_STRATEGY_TRACKING_SESSIONS:
        raise ValueError("策略对照最多接受200个日期/评分合同，请缩小run_ids范围")
    permitted = _requested_canonical_runs(scheduled, run_ids, excluded)
    return sorted(permitted, key=lambda row: (str(row["data_date"]), int(row["id"]))), excluded


def _scheduled_tracking_runs(rows: list[sqlite3.Row], spacing: int, excluded: list[dict[str, object]]) -> list[sqlite3.Row]:
    if spacing == 1:
        return rows
    grouped: dict[tuple[str, str], list[sqlite3.Row]] = defaultdict(list)
    for row in rows:
        grouped[(str(row["rule_version"]), str(row["declared_score_spec_hash"]))].append(row)
    scheduled: list[sqlite3.Row] = []
    for cohort in grouped.values():
        dates = [date.fromisoformat(str(row["data_date"])) for row in cohort]
        calendar = trading_dates_between(min(dates), max(dates), allow_auto_refresh=False)
        if not set(dates).issubset(calendar):
            raise ValueError("signal schedule contains a non-trading date")
        anchors = {day.isoformat() for day in calendar[::spacing]}
        for row in cohort:
            if str(row["data_date"]) in anchors:
                scheduled.append(row)
            else:
                excluded.append({"run_id": int(row["id"]), "reason": "non_overlapping_signal_schedule"})
    return scheduled


def _requested_canonical_runs(
    rows: Iterable[sqlite3.Row], run_ids: Sequence[int] | None, excluded: list[dict[str, object]],
) -> list[sqlite3.Row]:
    candidates = list(rows)
    if run_ids is None:
        return candidates
    allowed = set(run_ids)
    excluded.extend({"run_id": int(row["id"]), "reason": "canonical_run_not_requested"} for row in candidates if int(row["id"]) not in allowed)
    return [row for row in candidates if int(row["id"]) in allowed]


def freeze_strategy_template_session(
    conn: sqlite3.Connection, row: sqlite3.Row, *, as_of: datetime, horizon: int,
    notional_cash_cny: float, templates: Sequence[Mapping[str, object]],
) -> StrategyTrackingSession:
    """Admit and freeze a basket in the caller's read transaction, without forward reads.

    The caller owns canonical run selection and the prospective plan's cutoff.
    Only supplied, digest-verified templates are used; the live catalog is not read.
    """
    if not conn.in_transaction:
        raise ValueError("frozen strategy selection requires a stable read transaction")
    timestamp = _validated_inputs(as_of, horizon, notional_cash_cny, None)
    completed = latest_expected_daily_kline_date(timestamp, allow_auto_refresh=False)
    strategies = _frozen_template_strategies(templates, timestamp.isoformat())
    digest = _admission_step("snapshot_seal_invalid", lambda: require_publication_market_scan_snapshot(conn, int(row["id"])))
    run = _admission_step("frozen_snapshot_invalid", lambda: run_from_row(row))
    _admission_step("signal_availability_invalid", lambda: _require_signal_availability(run, timestamp, completed))
    result_rows = conn.execute("SELECT * FROM market_scan_result WHERE run_id = ? ORDER BY rank, symbol", (run.id,)).fetchall()
    items = _admission_step("frozen_snapshot_invalid", lambda: [result_from_row(item) for item in result_rows])
    score_hash = _require_tracking_members(run, row, result_rows, items)
    drafts = {
        template_id: build_portfolio_draft(strategy, run, items, StrategyExecutionRequest(
            strategy_id=strategy.strategy_id, revision=strategy.strategy_version, kind="historical_replay",
            run_id=run.id, notional_cash_cny=notional_cash_cny,
        )) for template_id, strategy in strategies.items()
    }
    targets = _tracking_target_dates(run.data_date, horizon)
    prices = {item.symbol: float(item.price or 0) for item in items}
    selections = tuple(_tracking_selection(template_id, draft, prices) for template_id, draft in drafts.items())
    return StrategyTrackingSession(
        run.id, run.data_date, targets[-1].isoformat() if targets else None,
        run.rule_version, score_hash, digest, str(run.finished_at), selections,
    )


def _tracking_target_dates(signal_date: str, horizon: int) -> tuple[date, ...]:
    try:
        return next_trade_dates(date.fromisoformat(signal_date), horizon, allow_auto_refresh=False)
    except TradingCalendarCoverageError:
        return ()


def _tracking_session(conn: sqlite3.Connection, row: sqlite3.Row, context: _TrackingContext) -> StrategyTrackingSession:
    frozen = freeze_strategy_template_session(
        conn, row, as_of=context.as_of, horizon=context.horizon, notional_cash_cny=context.notional, templates=context.templates,
    )
    selected = {position.symbol for selection in frozen.selections for position in selection.positions}
    result_rows = conn.execute(
        f"SELECT * FROM market_scan_result WHERE run_id=? AND symbol IN ({','.join('?' for _ in selected)})",
        [frozen.run_id, *sorted(selected)],
    ).fetchall() if selected else []
    targets = _tracking_target_dates(frozen.signal_date, context.horizon)
    forward = _forward_bars(conn, selected, frozen.signal_date, targets[-1].isoformat()) if targets and targets[-1] <= context.completed else {}
    outcomes = _position_outcomes(row, result_rows, selected, forward, targets, context)
    return replace(frozen, selections=tuple(_label_frozen_selection(selection, outcomes) for selection in frozen.selections))


def _label_frozen_selection(
    selection: StrategyTrackingSelection, outcomes: Mapping[str, tuple[float | None, str | None]],
) -> StrategyTrackingSelection:
    positions = tuple(replace(position, forward_return=outcomes[position.symbol][0], outcome_reason=outcomes[position.symbol][1])
                      for position in selection.positions)
    known = [position for position in positions if position.forward_return is not None]
    missing = Counter(position.outcome_reason for position in positions if position.outcome_reason is not None)
    status = _selection_outcome_status(positions, missing)
    weighted = math.fsum(position.target_weight * cast(float, position.forward_return) for position in known) if status == "available" else None
    return replace(selection, positions=positions, outcome_status=status, weighted_gross_return=weighted,
                   available_outcome_count=len(known), missing_reason_counts=dict(sorted(missing.items())))


def _require_tracking_members(
    run: MarketScanRun, row: sqlite3.Row, result_rows: Sequence[sqlite3.Row], items: Sequence[MarketScanResultItem],
) -> str:
    integrity = _admission_step("frozen_snapshot_invalid", lambda: validate_frozen_full_market_snapshot(run, items))
    if run.missing_count or any(item.status == "missing" for item in items):
        raise _TrackingAdmissionError("missing_frozen_universe")
    _admission_step("decision_time_invalid", lambda: validate_research_decision_time(run, items))
    _admission_step("score_contract_unregistered", lambda: _require_registered_contract(row, result_rows, integrity.production_score_spec_hash))
    _admission_step("pit_feature_binding_invalid", lambda: _require_point_in_time_features(run, items))
    return integrity.production_score_spec_hash


def _require_signal_availability(run: MarketScanRun, as_of: datetime, completed: date) -> None:
    signal = date.fromisoformat(run.data_date)
    if signal > completed:
        raise ValueError("信号交易日尚未按可信日历完成")
    next_session = next_trade_dates(signal, 1, allow_auto_refresh=False)[0]
    opens = datetime.combine(next_session, time(9, 30), ASHARE_TIMEZONE).timestamp()
    closes = datetime.combine(signal, time(15), ASHARE_TIMEZONE).timestamp()
    decision = market_datetime_epoch(run.as_of)
    published = audit_time_epoch(run.finished_at)
    sealed = audit_time_epoch(run.snapshot_sealed_at)
    if decision is None or published is None or sealed is None or not closes <= decision <= published <= sealed < opens:
        raise ValueError("信号没有在下一交易日开盘前完成决策、发布与原始封印")
    if sealed > as_of.timestamp():
        raise ValueError("信号封印晚于报告as_of")


def _require_registered_contract(row: sqlite3.Row, result_rows: Sequence[sqlite3.Row], score_hash: str) -> None:
    contracts = {frozen_score_contract(item) for item in result_rows if item["status"] == "success"}
    if len(contracts) != 1 or None in contracts:
        raise ValueError("冻结批次含未知或不一致评分合同")
    if row["declared_score_spec_hash"] != score_hash:
        raise ValueError("冻结批次评分合同与规则注册表不一致")


def _require_point_in_time_features(run: MarketScanRun, items: Sequence[MarketScanResultItem]) -> None:
    for item in items:
        if item.status != "success":
            continue
        components = item.score_details.get("components")
        dimensions = components.get("score_dimensions") if isinstance(components, dict) else None
        evidence = dimensions.get("point_in_time_evidence") if isinstance(dimensions, dict) else None
        if not isinstance(dimensions, dict) or not isinstance(evidence, dict):
            raise ValueError("冻结候选缺少时点证据结构")
        if not verify_market_scan_point_in_time_evidence_context(
            evidence, item=item, expected_data_date=run.data_date, expected_quote_date=run.quote_date,
            expected_as_of=run.as_of, expected_mode="official",
        ):
            raise ValueError("冻结候选的时点证据无法重算或绑定")
        features = cast(dict[str, object], evidence.get("payload", {})).get("features")
        raw = dimensions.get("raw_features")
        if not isinstance(raw, dict) or raw != features or any(isinstance(value, bool) for value in raw.values()):
            raise ValueError("冻结原始因子与时点证据不一致")


def _forward_bars(conn: sqlite3.Connection, symbols: set[str], signal: str, target: str) -> dict[str, list[sqlite3.Row]]:
    if not symbols:
        return {}
    rows = conn.execute(
        f"SELECT * FROM kline_daily WHERE symbol IN ({','.join('?' for _ in symbols)}) AND adjustment_mode = 'qfq' AND date BETWEEN ? AND ? ORDER BY symbol,date",
        [*sorted(symbols), signal, target],
    ).fetchall()
    grouped: dict[str, list[sqlite3.Row]] = defaultdict(list)
    for row in rows:
        grouped[str(row["symbol"])].append(row)
    return grouped


def _position_outcomes(
    run: sqlite3.Row, rows: Sequence[sqlite3.Row], selected: set[str], bars: Mapping[str, Sequence[sqlite3.Row]],
    targets: Sequence[date], context: _TrackingContext,
) -> dict[str, tuple[float | None, str | None]]:
    by_symbol = {str(row["symbol"]): row for row in rows}
    if not targets:
        return {symbol: (None, "calendar_unavailable") for symbol in selected}
    if targets[-1] > context.completed:
        return {symbol: (None, "target_session_not_completed") for symbol in selected}
    return {symbol: _forward_return(run, by_symbol[symbol], bars.get(symbol, ()), targets, context.as_of) for symbol in selected}


def _forward_return(
    run: sqlite3.Row, result: sqlite3.Row, bars: Sequence[sqlite3.Row], targets: Sequence[date], as_of: datetime,
) -> tuple[float | None, str | None]:
    basis = forward_price_basis_status(run, result, bars)
    if basis != "verified":
        return None, basis
    dates = [str(run["data_date"]), *(day.isoformat() for day in targets)]
    by_date = {str(row["date"]): row for row in bars}
    if len(by_date) != len(bars) or any(day not in by_date for day in dates):
        return None, "holding_path_bar_missing_or_duplicate"
    path = [by_date[day] for day in dates]
    reason = _forward_path_reason(path, as_of)
    if reason:
        return None, reason
    value = float(path[-1]["close"]) / float(result["price"]) - 1
    return (value, None) if math.isfinite(value) else (None, "nonfinite_forward_return")


def _forward_path_reason(path: Sequence[sqlite3.Row], as_of: datetime) -> str | None:
    if not all(valid_forward_price_bar(row) for row in path):
        return "forward_bar_contract_invalid"
    if len({str(row["data_version"]) for row in path}) != 1:
        return "mixed_forward_price_vintages"
    if any(str(row["corporate_action_status"]) == "effective_event" for row in path):
        return "corporate_action_effective_event"
    if any(str(row["corporate_action_status"]) != "none" for row in path):
        return "corporate_action_status_unknown"
    return _forward_observation_reason(path, as_of)


def _forward_observation_reason(path: Sequence[sqlite3.Row], as_of: datetime) -> str | None:
    fetched = [audit_time_epoch(row["fetched_at"]) for row in path]
    if any(value is None or value > as_of.timestamp() for value in fetched):
        return "forward_observation_after_as_of_or_unknown"
    for row, fetched_epoch in zip(path, fetched, strict=True):
        snapshot = market_datetime_epoch(str(row["as_of"] or ""))
        closes = datetime.combine(date.fromisoformat(str(row["date"])), time(15), ASHARE_TIMEZONE).timestamp()
        if snapshot is None or not closes <= snapshot <= cast(float, fetched_epoch):
            return "forward_snapshot_time_invalid"
    return None


def _tracking_selection(
    template_id: str, draft: PortfolioComputation, prices: Mapping[str, float],
) -> StrategyTrackingSelection:
    positions = tuple(StrategyTrackingPosition(
        row.symbol, row.name, row.industry, row.target_weight, prices[row.symbol], None, "forward_outcome_not_read",
    ) for row in draft.candidates if row.target_weight > 0)
    summary = draft.summary
    return StrategyTrackingSelection(
        template_id, summary.status, summary.evaluated_count, summary.eligible_count, positions,
        summary.target_invested_weight, summary.residual_cash_cny, summary.estimated_round_trip_cost_cny,
        draft.execution_fingerprint, draft.result_digest, "missing" if positions else "no_selection", None, 0,
        dict(sorted(Counter(reason.split("（当前")[0] for row in draft.candidates for reason in row.hard_filter_failures).items())),
        {"forward_outcome_not_read": len(positions)} if positions else {}, tuple(summary.no_trade_reasons),
    )


def _selection_outcome_status(positions: Sequence[StrategyTrackingPosition], missing: Mapping[str, int]) -> str:
    if not positions:
        return "no_selection"
    if "calendar_unavailable" in missing:
        return "calendar_unavailable"
    if "target_session_not_completed" in missing:
        return "pending"
    return "missing" if missing else "available"


__all__ = [
    "MAX_STRATEGY_TRACKING_SESSIONS", "evaluate_strategy_template_tracking",
    "freeze_strategy_template_session", "strategy_template_tracking_contracts",
]
