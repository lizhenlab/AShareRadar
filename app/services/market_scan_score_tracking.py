"""Date-paired, frozen-score diagnostics; never a probability or promotion gate."""

from __future__ import annotations

from collections import defaultdict
from collections.abc import Mapping, Sequence
from contextlib import closing
from dataclasses import dataclass
from datetime import date, datetime, time
import math
from pathlib import Path
import re
import sqlite3
from statistics import fmean
from typing import cast

from app.db.market_scan_integrity import MarketScanSnapshotSealError, verify_market_scan_snapshot
from app.models.market_scan import MarketScanMode
from app.services.market_scan_evaluation_config import EvaluationConfig
from app.services.market_scan_evaluation_price_basis import forward_price_basis_status, valid_forward_price_bar
from app.services.market_scan_evaluation_source import frozen_result_raw_score, frozen_score_contract, portable_database_label
from app.services.market_scan_evaluation_statistics import moving_block_bootstrap_confidence_interval
from app.services.trading_calendar import (
    ASHARE_TIMEZONE, TradingCalendarCoverageError, latest_expected_daily_kline_date, next_trade_dates,
    trading_dates_between,
)
from app.utils.audit_time import audit_time_epoch
from app.utils.clock import utc_now


SCORE_TRACKING_SCHEMA_VERSION = "market-scan-score-tracking-v1"


@dataclass(frozen=True)
class ScoreTrackingMember:
    symbol: str
    raw_score: float
    returns: Mapping[int, float]
    point_in_time_verified: bool = False


@dataclass(frozen=True)
class ScoreTrackingSession:
    run_id: int
    signal_date: str
    mode: str
    scope: str
    rule_version: str
    score_spec_hash: str | None
    members: tuple[ScoreTrackingMember, ...]
    published_at: str | None = None
    snapshot_origin: str | None = None
    snapshot_digest: str | None = None
    sealed_at: str | None = None


def evaluate_market_scan_score_tracking(
    database_path: Path,
    *,
    as_of: datetime | None = None,
    config: EvaluationConfig | None = None,
    mode: MarketScanMode | None = None,
    run_ids: Sequence[int] | None = None,
) -> dict[str, object]:
    """Read bounded frozen scores and forward bars without execution/model fitting."""
    settings = config or EvaluationConfig(horizons=(1, 5, 20))
    now = utc_now()
    timestamp = as_of or now
    if timestamp.tzinfo is None or timestamp.utcoffset() is None:
        timestamp = timestamp.replace(tzinfo=ASHARE_TIMEZONE)
    if timestamp > now:
        raise ValueError("score tracking as_of cannot be in the future")
    completed = latest_expected_daily_kline_date(timestamp)
    path = Path(database_path).resolve()
    sessions: list[ScoreTrackingSession] = []
    failures: list[dict[str, object]] = []
    with closing(sqlite3.connect(f"{path.as_uri()}?mode=ro", uri=True)) as conn:
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA query_only = ON")
        conn.execute("BEGIN")
        runs = _score_tracking_runs(conn, mode, run_ids, timestamp)
        for run in runs:
            try:
                sessions.append(_score_tracking_snapshot(conn, run, completed, settings))
            except (MarketScanSnapshotSealError, ValueError, TypeError, KeyError, TradingCalendarCoverageError) as exc:
                failures.append({"run_id": int(run["id"]), "signal_date": str(run["quote_date"] or run["data_date"]),
                                 "reason": "frozen_snapshot_unavailable", "error_type": type(exc).__name__})
    report = build_score_tracking(sessions, as_of=timestamp, config=settings)
    report["source"] = {"database": portable_database_label(path), "read_only": True,
                        "selected_run_count": len(runs), "failed_run_count": len(failures),
                        "provider_calls": 0, "model_fitting_performed": False}
    report["run_failures"] = failures
    if failures:
        report["status"] = "insufficient_data"
        for cohort in cast(list[dict[str, object]], report["cohorts"]):
            cohort["status"] = "insufficient_data"
            cohort["confidence_interval_95"] = None
            cast(list[str], cohort["insufficient_reasons"]).append("failed_frozen_snapshots")
    return report


def _score_tracking_runs(
    conn: sqlite3.Connection, mode: MarketScanMode | None,
    run_ids: Sequence[int] | None, as_of: datetime,
) -> list[sqlite3.Row]:
    clauses, parameters = ["r.status IN ('success', 'degraded')"], cast(list[object], [])
    if mode is not None:
        clauses.append("r.mode = ?")
        parameters.append(mode)
    if run_ids is not None:
        ids = tuple(dict.fromkeys(int(value) for value in run_ids if int(value) > 0))
        if not ids:
            return []
        clauses.append(f"r.id IN ({','.join('?' for _value in ids)})")
        parameters.extend(ids)
    rows = conn.execute(
        f"""SELECT r.*, c.production_score_spec_hash AS declared_score_spec_hash
            FROM market_scan_run AS r LEFT JOIN market_scan_rule_contract AS c ON c.rule_version = r.rule_version
            WHERE {' AND '.join(clauses)}""", parameters,
    ).fetchall()
    by_session: dict[tuple[str, str, str, str, str], sqlite3.Row] = {}
    ordered = sorted(rows, key=lambda row: (audit_time_epoch(row["finished_at"]) or float("-inf"), int(row["id"])))
    for row in ordered:
        published = audit_time_epoch(row["finished_at"])
        if published is not None and published > as_of.timestamp():
            continue
        # The immutable rule registry binds one run rule to exactly one score hash.
        # Legacy runs without that binding are kept until their own evidence is read.
        identity = str(row["declared_score_spec_hash"] or f"unknown-run-{row['id']}")
        key = (str(row["mode"]), str(row["scope"]), str(row["rule_version"]), identity, str(row["quote_date"] or row["data_date"]))
        by_session.setdefault(key, row)
    return sorted(by_session.values(), key=lambda row: (str(row["quote_date"] or row["data_date"]), int(row["id"])))


def _score_tracking_snapshot(
    conn: sqlite3.Connection, run: sqlite3.Row, completed: date, config: EvaluationConfig,
) -> ScoreTrackingSession:
    digest = verify_market_scan_snapshot(conn, int(run["id"]))
    rows = conn.execute(
        "SELECT * FROM market_scan_result WHERE run_id = ? AND status = 'success' AND rank IS NOT NULL ORDER BY rank, symbol",
        (run["id"],),
    ).fetchall()
    contracts = {frozen_score_contract(row) for row in rows}
    contract = next(iter(contracts)) if len(contracts) == 1 else None
    declared = run["declared_score_spec_hash"]
    if declared is not None and (contract is None or contract[1] != declared):
        raise ValueError("frozen score contract does not match its registered run identity")
    signal = str(run["quote_date"] or run["data_date"])
    targets = _score_tracking_targets(date.fromisoformat(signal), config.horizons)
    cutoff = min(completed, targets[-1] if targets else date.fromisoformat(signal)).isoformat()
    bars = _score_tracking_bars(conn, run, cutoff)
    members = tuple(_score_tracking_member(run, row, bars.get(str(row["symbol"]), ()), targets, config) for row in rows)
    return ScoreTrackingSession(
        run_id=int(run["id"]), signal_date=signal, mode=str(run["mode"]), scope=str(run["scope"]),
        rule_version=str(run["rule_version"]), score_spec_hash=contract[1] if contract is not None else None,
        members=members, published_at=run["finished_at"], snapshot_origin=run["snapshot_seal_origin"], snapshot_digest=digest,
        sealed_at=run["snapshot_sealed_at"],
    )


def _score_tracking_targets(signal: date, horizons: Sequence[int]) -> tuple[date, ...]:
    for horizon in sorted(horizons, reverse=True):
        try:
            return next_trade_dates(signal, horizon)
        except TradingCalendarCoverageError:
            continue
    return ()


def _score_tracking_bars(
    conn: sqlite3.Connection, run: sqlite3.Row, cutoff: str,
) -> dict[str, tuple[sqlite3.Row, ...]]:
    rows = conn.execute(
        """SELECT k.* FROM kline_daily AS k JOIN market_scan_result AS r
           ON r.run_id = ? AND r.symbol = k.symbol AND r.status = 'success'
           WHERE k.date BETWEEN ? AND ? AND k.adjustment_mode = 'qfq'
           ORDER BY k.symbol, k.date""",
        (run["id"], run["data_date"], cutoff),
    ).fetchall()
    grouped: dict[str, list[sqlite3.Row]] = defaultdict(list)
    for row in rows:
        grouped[str(row["symbol"])].append(row)
    return {symbol: tuple(values) for symbol, values in grouped.items()}


def _score_tracking_member(
    run: sqlite3.Row, result: sqlite3.Row, bars: tuple[sqlite3.Row, ...],
    targets: Sequence[date], config: EvaluationConfig,
) -> ScoreTrackingMember:
    verified = forward_price_basis_status(run, result, bars) == "verified"
    returns = {
        horizon: value for horizon in config.horizons
        if verified and horizon <= len(targets)
        and (value := _score_tracking_return(run, result, bars, targets[:horizon])) is not None
    }
    return ScoreTrackingMember(
        symbol=str(result["symbol"]), raw_score=frozen_result_raw_score(result), returns=returns,
        point_in_time_verified=verified,
    )


def _score_tracking_return(
    run: sqlite3.Row, result: sqlite3.Row, bars: Sequence[sqlite3.Row], targets: Sequence[date],
) -> float | None:
    window = tuple(row for row in bars if str(row["date"]) <= targets[-1].isoformat())
    # The caller verifies the same frozen signal/overlap once per stock. Only
    # this horizon's forward prefix may affect its path and vintage admission.
    by_date = {str(row["date"]): row for row in window if valid_forward_price_bar(row)}
    path = [by_date.get(str(run["data_date"])), *(by_date.get(day.isoformat()) for day in targets)]
    if any(row is None for row in path):
        return None
    rows = cast(list[sqlite3.Row], path)
    if len({str(row["data_version"]) for row in rows}) != 1:
        return None
    if any(row["corporate_action_status"] == "effective_event" for row in rows if "corporate_action_status" in row.keys()):
        return None
    return float(rows[-1]["close"]) / float(result["price"]) - 1


def build_score_tracking(
    sessions: Sequence[ScoreTrackingSession],
    *,
    as_of: datetime,
    config: EvaluationConfig | None = None,
    selection_policy: str = "earliest-published-per-contract-session",
) -> dict[str, object]:
    """Freeze tails before inspecting labels and average only paired date means."""
    settings = config or EvaluationConfig(horizons=(1, 5, 20))
    if as_of.tzinfo is None or as_of.utcoffset() is None:
        raise ValueError("score tracking as_of requires an explicit timezone")
    completed = latest_expected_daily_kline_date(as_of)
    selected, excluded = _select_sessions(sessions, as_of)
    grouped: dict[tuple[str, str, str, str], list[ScoreTrackingSession]] = defaultdict(list)
    for session in selected:
        grouped[_contract_key(session)].append(session)
    cohorts = [
        _cohort_report(rows, horizon, completed, settings)
        for _key, rows in sorted(grouped.items()) for horizon in settings.horizons
    ]
    return {
        "schema_version": SCORE_TRACKING_SCHEMA_VERSION,
        "generated_at": as_of.isoformat(), "as_of_completed_date": completed.isoformat(),
        "status": "ok" if any(row["status"] == "ok" for row in cohorts) else "insufficient_data",
        "selection_policy": selection_policy, "score_semantics": "ordinal-not-probability",
        "return_target": "frozen-snapshot-price-to-fixed-D+H-close-gross-return",
        "return_unit": "decimal_fraction", "group_fraction": 0.20,
        "tie_policy": "include_all_boundary_ties;overlapping_tails_are_not_comparable",
        "promotion_eligible": False, "prediction_accuracy_validated": False,
        "source_session_count": len(sessions), "selected_session_count": len(selected),
        "excluded_sessions": excluded, "cohorts": cohorts,
        "limitations": [
            "规则分不是上涨概率；结果只描述已冻结榜单，不证明预测能力提高。",
            "按冻结原始分数选高低各20%，边界同分一并纳入；不按股票代码拆分同分。",
            "高低收益必须来自同一信号日，各日期等权；缺失成熟日保留并阻止置信区间。",
            "收益未扣成本，未模拟交易执行；不等于可实现组合收益或组合净值。",
            "本地封存与PIT摘要不能证明外部真实性；历史查看过的数据不成为新样本外验证。",
        ],
    }


def _contract_key(session: ScoreTrackingSession) -> tuple[str, str, str, str]:
    # Unknown identities must never accumulate into a seemingly valid cohort.
    identity = _verified_identity(session.score_spec_hash) or f"unverified-run-{session.run_id}"
    return session.mode, session.scope, session.rule_version, identity


def _verified_identity(value: str | None) -> str | None:
    return value if isinstance(value, str) and re.fullmatch(r"[0-9a-f]{64}", value) else None


def _select_sessions(
    sessions: Sequence[ScoreTrackingSession], as_of: datetime,
) -> tuple[list[ScoreTrackingSession], list[dict[str, object]]]:
    selected: dict[tuple[str, str, str, str, str], ScoreTrackingSession] = {}
    excluded: list[dict[str, object]] = []
    for session in sorted(sessions, key=_publication_order):
        stamp = audit_time_epoch(session.published_at)
        if stamp is not None and stamp > as_of.timestamp():
            excluded.append({"run_id": session.run_id, "reason": "published_after_as_of"})
            continue
        key = (*_contract_key(session), session.signal_date)
        if key in selected:
            excluded.append({"run_id": session.run_id, "reason": "same_contract_session_rescan"})
        else:
            selected[key] = session
    return sorted(selected.values(), key=lambda item: (item.signal_date, item.run_id)), excluded


def _publication_order(session: ScoreTrackingSession) -> tuple[float, int]:
    stamp = audit_time_epoch(session.published_at)
    return (stamp if stamp is not None else float("-inf"), session.run_id)


def _frozen_tails(
    members: Sequence[ScoreTrackingMember],
) -> tuple[tuple[ScoreTrackingMember, ...], tuple[ScoreTrackingMember, ...], str | None]:
    if len(members) < 2 or len({item.symbol for item in members}) != len(members):
        return (), (), "insufficient_or_duplicate_frozen_members"
    if any(not _finite(item.raw_score) or not 0 <= item.raw_score <= 100 for item in members):
        return (), (), "invalid_frozen_score"
    scores = sorted(item.raw_score for item in members)
    size = max(1, math.ceil(len(scores) * 0.20))
    low_cutoff, high_cutoff = scores[size - 1], scores[-size]
    if low_cutoff >= high_cutoff:
        return (), (), "score_tails_overlap_or_no_score_difference"
    low = tuple(item for item in members if item.raw_score <= low_cutoff)
    high = tuple(item for item in members if item.raw_score >= high_cutoff)
    return high, low, None


def _session_report(
    session: ScoreTrackingSession, horizon: int, completed: date, config: EvaluationConfig,
) -> dict[str, object]:
    high, low, group_reason = _frozen_tails(session.members)
    row: dict[str, object] = {
        "run_id": session.run_id, "signal_date": session.signal_date, "target_date": None,
        "snapshot_digest": session.snapshot_digest, "status": "calendar_unavailable", "reason": None,
        "high_frozen_count": len(high), "low_frozen_count": len(low),
        "high_available_count": 0, "low_available_count": 0,
        "high_coverage": None, "low_coverage": None,
        "high_return": None, "low_return": None, "spread": None,
        "prospective_recording_verified": False,
    }
    try:
        dates = next_trade_dates(date.fromisoformat(session.signal_date), horizon)
    except (TradingCalendarCoverageError, ValueError):
        row["reason"] = "trusted_calendar_target_unavailable"
        return row
    row["target_date"] = dates[-1].isoformat()
    row["prospective_recording_verified"] = _prospective_recording(session, dates[0])
    if dates[-1] > completed:
        row.update(status="pending", reason="target_session_not_completed")
    elif group_reason is not None:
        row.update(status="not_comparable", reason=group_reason)
    else:
        _paired_outcomes(row, high, low, horizon, config.complete_day_coverage)
    return row


def _prospective_recording(session: ScoreTrackingSession, entry_day: date) -> bool:
    published = audit_time_epoch(session.published_at)
    sealed = audit_time_epoch(session.sealed_at)
    entry = datetime.combine(entry_day, time(9, 30), tzinfo=ASHARE_TIMEZONE).timestamp()
    signal_close = datetime.combine(date.fromisoformat(session.signal_date), time(15), tzinfo=ASHARE_TIMEZONE).timestamp()
    return bool(
        session.mode == "official" and session.snapshot_origin == "publication"
        and session.snapshot_digest and _verified_identity(session.score_spec_hash) and session.members
        and published is not None and signal_close <= published < entry
        and sealed is not None and published <= sealed < entry
        and all(item.point_in_time_verified for item in session.members)
    )


def _paired_outcomes(
    row: dict[str, object], high: Sequence[ScoreTrackingMember], low: Sequence[ScoreTrackingMember],
    horizon: int, minimum_coverage: float,
) -> None:
    high_values, low_values = _available_returns(high, horizon), _available_returns(low, horizon)
    high_coverage, low_coverage = len(high_values) / len(high), len(low_values) / len(low)
    row.update(high_available_count=len(high_values), low_available_count=len(low_values),
               high_coverage=high_coverage, low_coverage=low_coverage)
    if not high_values or not low_values or min(high_coverage, low_coverage) < minimum_coverage:
        row.update(status="missing", reason="matured_group_outcome_coverage_below_minimum")
        return
    high_mean, low_mean = fmean(high_values), fmean(low_values)
    row.update(status="paired", high_return=high_mean, low_return=low_mean, spread=high_mean - low_mean)


def _available_returns(members: Sequence[ScoreTrackingMember], horizon: int) -> list[float]:
    return [
        float(cast(float, value)) for item in members
        if _finite(value := item.returns.get(horizon)) and cast(float, value) >= -1
    ]


def _finite(value: object) -> bool:
    if not isinstance(value, int | float) or isinstance(value, bool):
        return False
    try:
        return math.isfinite(value)
    except OverflowError:
        return False


def _cohort_report(
    sessions: Sequence[ScoreTrackingSession], horizon: int, completed: date, config: EvaluationConfig,
) -> dict[str, object]:
    first = sessions[0]
    rows = [_session_report(session, horizon, completed, config) for session in sessions]
    matured = [row for row in rows if row["status"] not in {"pending", "calendar_unavailable"}]
    paired = [row for row in matured if row["status"] == "paired"]
    series, gaps = _calendar_series(rows, horizon, completed)
    minimum = max(20, 2 * horizon, config.minimum_session_count)
    reasons = _insufficient_reasons(rows, paired, first, minimum, config)
    if gaps:
        reasons.append("unobserved_signal_session_gaps")
    confidence = moving_block_bootstrap_confidence_interval(
        series, samples=config.bootstrap_samples, block_length=horizon, minimum_count=minimum,
        seed_text=f"{_contract_key(first)}:{horizon}:paired-score-spread-v1",
    ) if not reasons else None
    return {
        "mode": first.mode, "scope": first.scope, "rule_version": first.rule_version,
        "score_spec_hash": first.score_spec_hash, "horizon_trading_days": horizon,
        "status": "ok" if not reasons else "insufficient_data",
        "expected_session_count": len(rows) + gaps, "mature_session_count": len(matured) + gaps,
        "pending_session_count": sum(row["status"] == "pending" for row in rows), "paired_session_count": len(paired),
        "calendar_unavailable_session_count": sum(row["status"] == "calendar_unavailable" for row in rows),
        "missing_session_count": len(matured) - len(paired) + gaps,
        "unobserved_session_count": gaps,
        "prospective_recording_session_count": sum(row["prospective_recording_verified"] is True for row in rows),
        "high_average_return": _paired_mean(paired, "high_return"),
        "low_average_return": _paired_mean(paired, "low_return"),
        "high_minus_low_return": _paired_mean(paired, "spread"),
        "confidence_interval_95": confidence, "minimum_required_sessions": minimum,
        "minimum_outcome_coverage": config.complete_day_coverage,
        "confidence_interval_inference": "ordered-circular-moving-date-block-bootstrap;missing-dates-preserved",
        "insufficient_reasons": reasons, "promotion_eligible": False,
        "prediction_accuracy_validated": False, "sessions": rows,
    }


def _paired_mean(rows: Sequence[Mapping[str, object]], key: str) -> float | None:
    return fmean(cast(float, row[key]) for row in rows) if rows else None


def _calendar_series(
    rows: Sequence[Mapping[str, object]], horizon: int, completed: date,
) -> tuple[list[float], int]:
    by_date = {str(row["signal_date"]): row for row in rows}
    try:
        dates = trading_dates_between(date.fromisoformat(min(by_date)), date.fromisoformat(max(by_date)))
        matured = [day for day in dates if next_trade_dates(day, horizon)[-1] <= completed]
    except (TradingCalendarCoverageError, ValueError):
        return [math.nan], 0
    series: list[float] = []
    gaps = 0
    for day in matured:
        row = by_date.get(day.isoformat())
        if row is None:
            gaps += 1
        series.append(cast(float, row["spread"]) if row is not None and row["status"] == "paired" else math.nan)
    return series, gaps


def _insufficient_reasons(
    rows: Sequence[Mapping[str, object]], paired: Sequence[Mapping[str, object]],
    session: ScoreTrackingSession, minimum: int, config: EvaluationConfig,
) -> list[str]:
    reasons: list[str] = []
    if len(paired) < minimum:
        reasons.append("minimum_paired_session_count")
    if any(row["status"] in {"missing", "not_comparable"} for row in rows):
        reasons.append("incomplete_matured_target_dates")
    if any(row["status"] == "calendar_unavailable" for row in rows):
        reasons.append("calendar_coverage_unavailable")
    if sum(cast(int, row["high_available_count"]) for row in paired) < config.minimum_sample_size:
        reasons.append("minimum_high_group_sample_size")
    if sum(cast(int, row["low_available_count"]) for row in paired) < config.minimum_sample_size:
        reasons.append("minimum_low_group_sample_size")
    if _verified_identity(session.score_spec_hash) is None:
        reasons.append("unverified_score_contract")
    return reasons
