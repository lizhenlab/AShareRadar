"""Read-only fixed-session D+1/D+2/D+3 range research for official scans.

The module intentionally keeps exploratory price-range outcomes separate from
executable strategy P&L.  Signal-day bars come only from verified point-in-time
scan evidence; future bars come from versioned qfq daily bars on fixed exchange
sessions and are never shifted for a suspended symbol.
"""

from __future__ import annotations

from collections import defaultdict
from collections.abc import Iterator, Mapping, Sequence
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import date
import hashlib
import json
import math
from pathlib import Path
import random
import sqlite3
from statistics import fmean, median
from typing import Final, Literal, cast

from app.db.market_scan_action_source import require_market_scan_action_source
from app.db.market_scan_integrity import MarketScanSnapshotSealError
from app.models.market import DAILY_KLINE_CONTRACT_VERSION, Kline
from app.models.paper_trading import CostProfileName
from app.repositories.market_scan_mapping import decode_result_payload
from app.services.market_scan_probability_artifact import load_probability_artifact
from app.services.market_scan_probability_labels import (
    ProbabilityLabelConfig,
    ProbabilityLabelOutcome,
    build_probability_label_outcomes,
    probability_label_contract,
)
from app.services.market_scan_score_dimensions import (
    MARKET_SCAN_EVIDENCE_CONTRACT_VERSION,
    verify_market_scan_point_in_time_evidence,
)
from app.services.market_scan_universe import FULL_MARKET_SCOPE
from app.services.trading_calendar import TradingCalendarCoverageError, next_trade_dates
from app.utils.clock import utc_now
from app.utils.market_time import market_datetime_epoch


FUTURE_RANGE_EVALUATION_SCHEMA_VERSION: Final[str] = "market-scan-future-range-evaluation-v1"
FUTURE_RANGE_REPORT_CONTRACT_VERSION: Final[str] = "market-scan-future-range-report-v1"
FUTURE_RANGE_RESEARCH_VERSION: Final[str] = "fixed-session-future-range-v1"
FUTURE_RANGE_CENTER_PROXY: Final[str] = "HLC3_proxy_not_VWAP"
FUTURE_RANGE_SESSION_OFFSETS: Final[tuple[int, ...]] = (1, 2, 3)
FUTURE_RANGE_TOP_SIZES: Final[tuple[int, ...]] = (20, 50, 100)
FUTURE_RANGE_METRICS: Final[tuple[str, ...]] = (
    "level_shift_low",
    "level_shift_hlc3_proxy",
    "level_shift_high",
    "mae",
    "mfe",
    "terminal_close_return",
    "net_return",
    "net_excess_return",
)
FutureRangeStatus = Literal["ok", "insufficient_data"]


class FutureRangeResearchError(ValueError):
    """Raised when evidence conflicts make future-range research unsafe."""


@dataclass(frozen=True)
class FutureRangeConfig:
    session_offsets: tuple[int, ...] = FUTURE_RANGE_SESSION_OFFSETS
    top_sizes: tuple[int, ...] = FUTURE_RANGE_TOP_SIZES
    minimum_sample_size: int = 30
    minimum_session_count: int = 20
    complete_run_coverage: float = 0.95
    bootstrap_samples: int = 1_000
    bootstrap_seed: int = 20_260_811
    validation_gap_sessions: int = 3
    bootstrap_block_sessions: int = 3
    cost_profile: CostProfileName = "base"
    execution_notional: float = 100_000.0
    max_daily_participation_rate: float = 0.01

    def __post_init__(self) -> None:
        if self.session_offsets != FUTURE_RANGE_SESSION_OFFSETS:
            raise ValueError("session_offsets 必须固定为 (1, 2, 3)")
        if self.top_sizes != FUTURE_RANGE_TOP_SIZES:
            raise ValueError("top_sizes 必须固定为 (20, 50, 100)")
        if self.minimum_sample_size <= 0 or self.minimum_session_count <= 0:
            raise ValueError("样本数与独立交易日门槛必须为正整数")
        if not 0 < self.complete_run_coverage <= 1:
            raise ValueError("complete_run_coverage 必须在 (0, 1] 范围内")
        if self.bootstrap_samples < 100:
            raise ValueError("bootstrap_samples 不能小于 100")
        if self.validation_gap_sessions < 3 or self.bootstrap_block_sessions < 3:
            raise ValueError("D+1/D+2/D+3 重叠标签要求至少3个交易日 gap/block")
        if self.execution_notional <= 0 or not 0 < self.max_daily_participation_rate <= 1:
            raise ValueError("执行名义金额或参与率无效")


@dataclass(frozen=True)
class _Run:
    run_id: int
    mode: str
    scope: str
    rule_version: str
    quote_date: str
    data_date: str
    as_of: str
    snapshot_digest: str = ""

    @property
    def cohort(self) -> tuple[str, str, str]:
        return self.mode, self.scope, self.rule_version

    def contract(self) -> dict[str, str]:
        return {"mode": self.mode, "scope": self.scope, "rule_version": self.rule_version}


@dataclass(frozen=True)
class _EvidenceBar:
    date: str
    open: float
    close: float
    high: float
    low: float
    volume: float
    adjustment_mode: str
    data_version: str
    contract_version: str
    as_of: str

    @property
    def hlc3(self) -> float:
        return (self.high + self.low + self.close) / 3

    def payload(self) -> dict[str, object]:
        return {
            "date": self.date,
            "open": _rounded(self.open),
            "high": _rounded(self.high),
            "low": _rounded(self.low),
            "close": _rounded(self.close),
            "volume": _rounded(self.volume, 4),
            "hlc3_proxy": _rounded(self.hlc3),
            "adjustment_mode": self.adjustment_mode,
            "data_version": self.data_version,
            "contract_version": self.contract_version,
        }


@dataclass(frozen=True)
class _EvidenceResult:
    bar: _EvidenceBar | None
    digest: str | None
    evidence_contract: str | None
    reason: str | None


@dataclass(frozen=True)
class _RecordContext:
    target_dates: tuple[str, ...]
    target_rows: Mapping[str, sqlite3.Row]
    maximum_bar_date: str | None
    calendar_error: str | None
    config: FutureRangeConfig


def evaluate_market_scan_future_range(
    database_path: str | Path,
    *,
    config: FutureRangeConfig | None = None,
    run_ids: Sequence[int] | None = None,
    probability_artifact_paths: Sequence[str | Path] = (),
    generated_at: str | None = None,
) -> dict[str, object]:
    """Evaluate canonical official cohorts without mutating SQLite or ranking rows.

    ``run_ids`` selects artifact/report targets.  Their cohort statistics always
    use every canonical official session currently available for the same
    ``(mode, scope, rule_version)`` contract.
    """
    settings = config or FutureRangeConfig()
    database = Path(database_path).expanduser().resolve()
    timestamp = generated_at or utc_now().isoformat()
    probability = _load_probability_context(probability_artifact_paths)
    with _readonly_connection(database) as conn:
        all_runs = _canonical_official_runs(conn)
        selected = _select_runs(all_runs, run_ids)
        selected_cohorts = {run.cohort for run in selected}
        context_runs = [run for run in all_runs if run.cohort in selected_cohorts]
        maximum_bar_date = _maximum_qfq_date(conn)
        evaluated = {
            run.run_id: _evaluate_run(conn, run, maximum_bar_date, probability, settings)
            for run in context_runs
        }
    reports = _run_reports(
        selected,
        context_runs,
        evaluated,
        settings,
        timestamp,
        database,
    )
    return {
        "evaluation_schema_version": FUTURE_RANGE_EVALUATION_SCHEMA_VERSION,
        "status": "ok" if reports and all(item["status"] == "ok" for item in reports) else "insufficient_data",
        "generated_at": timestamp,
        "source": {
            "database": _portable_database_label(database),
            "read_only": True,
            "query_only": True,
            "transaction_snapshot": True,
            "production_ranking_mutated": False,
            "selected_run_count": len(selected),
            "context_run_count": len(context_runs),
        },
        "reports": reports,
        "limitations": _base_limitations(),
    }


@contextmanager
def _readonly_connection(path: Path) -> Iterator[sqlite3.Connection]:
    try:
        conn = sqlite3.connect(f"file:{path.as_posix()}?mode=ro", uri=True)
    except sqlite3.Error as exc:
        raise FutureRangeResearchError(f"无法只读打开 SQLite：{path}") from exc
    try:
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA query_only = ON")
        conn.execute("BEGIN")
        yield conn
    finally:
        conn.close()


def _canonical_official_runs(conn: sqlite3.Connection) -> list[_Run]:
    rows = conn.execute(
        """
        SELECT id, mode, scope, rule_version, quote_date, data_date, as_of,
               snapshot_digest
        FROM market_scan_run
        WHERE status IN ('success', 'degraded') AND mode = 'official' AND scope = ?
        ORDER BY data_date ASC, as_of ASC, id ASC
        """,
        (FULL_MARKET_SCOPE,),
    ).fetchall()
    canonical: dict[tuple[str, str, str, str], _Run] = {}
    for row in rows:
        run = _run_from_row(row)
        canonical[(*run.cohort, run.quote_date)] = run
    return list(canonical.values())


def _run_from_row(row: sqlite3.Row) -> _Run:
    quote_date = str(row["quote_date"] or row["data_date"])
    return _Run(
        run_id=int(row["id"]),
        mode=str(row["mode"]),
        scope=str(row["scope"]),
        rule_version=str(row["rule_version"]),
        quote_date=quote_date,
        data_date=str(row["data_date"]),
        as_of=str(row["as_of"]),
        snapshot_digest=str(row["snapshot_digest"] or ""),
    )


def _select_runs(runs: Sequence[_Run], run_ids: Sequence[int] | None) -> list[_Run]:
    if run_ids is None:
        return list(runs)
    normalized = tuple(dict.fromkeys(int(value) for value in run_ids))
    if not normalized or any(value <= 0 for value in normalized):
        raise FutureRangeResearchError("run_ids 必须是非空正整数集合")
    selected = [run for run in runs if run.run_id in normalized]
    missing = sorted(set(normalized) - {run.run_id for run in selected})
    if missing:
        raise FutureRangeResearchError(f"请求批次不是 canonical official published run：{missing}")
    return selected


def _maximum_qfq_date(conn: sqlite3.Connection) -> str | None:
    row = conn.execute("SELECT MAX(date) FROM kline_daily WHERE adjustment_mode = 'qfq'").fetchone()
    return str(row[0]) if row is not None and row[0] else None


def _evaluate_run(
    conn: sqlite3.Connection,
    run: _Run,
    maximum_bar_date: str | None,
    probability: Mapping[tuple[int, str], dict[str, object]],
    config: FutureRangeConfig,
) -> dict[str, object]:
    try:
        require_market_scan_action_source(conn, run.run_id)
    except MarketScanSnapshotSealError as exc:
        return _snapshot_integrity_failure(run, exc)
    rows = _result_rows(conn, run.run_id)
    target_dates, calendar_error = _fixed_target_dates(run.data_date)
    targets = _target_rows(conn, run.run_id, (run.data_date, *target_dates))
    records: list[dict[str, object]] = []
    exclusions: list[dict[str, object]] = []
    for row in rows:
        evidence = _signal_day_evidence(row, run)
        if evidence.bar is None:
            exclusions.append(
                {
                    "run_id": run.run_id,
                    "symbol": str(row["symbol"]),
                    "reason": evidence.reason or "point_in_time_evidence_invalid",
                }
            )
            continue
        records.append(
            _future_range_record(
                row,
                run,
                evidence,
                _RecordContext(
                    target_dates, targets.get(str(row["symbol"]), {}),
                    maximum_bar_date, calendar_error, config,
                ),
                probability.get((run.run_id, str(row["symbol"]))),
            )
        )
    _attach_market_benchmarks(records)
    return {
        "run": run,
        "expected_result_count": len(rows),
        "records": records,
        "exclusions": exclusions,
        "target_dates": target_dates,
        "calendar_error": calendar_error,
    }


def _snapshot_integrity_failure(
    run: _Run,
    error: MarketScanSnapshotSealError,
) -> dict[str, object]:
    return {
        "run": run,
        "expected_result_count": 0,
        "records": [],
        "exclusions": [
            {
                "run_id": run.run_id,
                "symbol": "*",
                "reason": "market_scan_snapshot_integrity_failed",
                "detail": " ".join(str(error).split())[:240],
            }
        ],
        "target_dates": (),
        "calendar_error": "market_scan_snapshot_integrity_failed",
    }


def _result_rows(conn: sqlite3.Connection, run_id: int) -> list[sqlite3.Row]:
    return conn.execute(
        """
        SELECT run_id, symbol, code, market, name, industry, rank, score, raw_score,
               trend_score, price, change_pct, turnover_rate, amount, data_quality_score,
               list_date, is_st, is_new, quote_timestamp, adjustment_mode, metrics_json
        FROM market_scan_result
        WHERE run_id = ? AND status = 'success' AND rank IS NOT NULL
        ORDER BY rank ASC, symbol ASC
        """,
        (run_id,),
    ).fetchall()


def _fixed_target_dates(data_date: str) -> tuple[tuple[str, ...], str | None]:
    try:
        parsed = date.fromisoformat(data_date)
        dates = next_trade_dates(parsed, max(FUTURE_RANGE_SESSION_OFFSETS))
    except (ValueError, TradingCalendarCoverageError) as exc:
        return (), " ".join(str(exc).split())[:240]
    return tuple(item.isoformat() for item in dates), None


def _target_rows(
    conn: sqlite3.Connection,
    run_id: int,
    target_dates: Sequence[str],
) -> dict[str, dict[str, sqlite3.Row]]:
    if not target_dates:
        return {}
    placeholders = ",".join("?" for _ in target_dates)
    rows = conn.execute(
        f"""
        SELECT k.symbol, k.date, k.open, k.close, k.high, k.low, k.volume,
               k.adjustment_mode, k.as_of, k.data_version, k.contract_version,
               k.source, k.fetched_at, k.fallback_used
        FROM kline_daily AS k
        JOIN market_scan_result AS r ON r.run_id = ? AND r.symbol = k.symbol
        WHERE r.status = 'success' AND k.adjustment_mode = 'qfq'
          AND k.date IN ({placeholders})
        ORDER BY k.symbol ASC, k.date ASC
        """,
        (run_id, *target_dates),
    ).fetchall()
    grouped: dict[str, dict[str, sqlite3.Row]] = defaultdict(dict)
    for row in rows:
        symbol, row_date = str(row["symbol"]), str(row["date"])
        if row_date in grouped[symbol]:
            raise FutureRangeResearchError(f"目标日K身份冲突：{symbol} {row_date}")
        grouped[symbol][row_date] = row
    return dict(grouped)


def _signal_day_evidence(row: sqlite3.Row, run: _Run) -> _EvidenceResult:
    if str(row["adjustment_mode"] or "") != "qfq":
        return _invalid_evidence("scan_result_adjustment_mode_not_qfq")
    evidence = _decoded_point_in_time_evidence(row)
    if evidence is None:
        return _invalid_evidence("point_in_time_evidence_digest_invalid")
    return _validated_signal_evidence(evidence, row, run)


def _decoded_point_in_time_evidence(row: sqlite3.Row) -> Mapping[str, object] | None:
    try:
        _metrics, details = decode_result_payload(row["metrics_json"])
    except (TypeError, ValueError, json.JSONDecodeError):
        return None
    evidence = _nested_evidence(details)
    if evidence is None or not verify_market_scan_point_in_time_evidence(evidence):
        return None
    return evidence


def _validated_signal_evidence(
    evidence: Mapping[str, object],
    row: sqlite3.Row,
    run: _Run,
) -> _EvidenceResult:
    if evidence.get("contract_version") != MARKET_SCAN_EVIDENCE_CONTRACT_VERSION:
        return _invalid_evidence("point_in_time_evidence_contract_unsupported")
    payload = evidence.get("payload")
    if not isinstance(payload, Mapping) or not _evidence_identity_matches(payload, row, run):
        return _invalid_evidence("point_in_time_evidence_identity_conflict")
    contracts = payload.get("bar_contract_61")
    if not isinstance(contracts, list) or len(contracts) != 61:
        return _invalid_evidence("point_in_time_bar_contract_incomplete")
    bars = _evidence_bars(contracts)
    if bars is None:
        return _invalid_evidence("point_in_time_bar_contract_invalid")
    if not _valid_evidence_bar_series(bars, run.data_date, run.as_of):
        return _invalid_evidence("point_in_time_bar_version_or_qfq_conflict")
    digest = evidence.get("payload_digest")
    if not isinstance(digest, str) or len(digest) != 64:
        return _invalid_evidence("point_in_time_evidence_digest_missing")
    return _EvidenceResult(bars[-1], digest, str(evidence["contract_version"]), None)


def _evidence_bars(contracts: Sequence[object]) -> tuple[_EvidenceBar, ...] | None:
    try:
        return tuple(_bar_from_contract(item) for item in contracts)
    except (TypeError, ValueError):
        return None


def _nested_evidence(details: Mapping[str, object]) -> Mapping[str, object] | None:
    components = details.get("components")
    dimensions = components.get("score_dimensions") if isinstance(components, Mapping) else None
    evidence = dimensions.get("point_in_time_evidence") if isinstance(dimensions, Mapping) else None
    return cast(Mapping[str, object], evidence) if isinstance(evidence, Mapping) else None


def _evidence_identity_matches(payload: Mapping[str, object], row: sqlite3.Row, run: _Run) -> bool:
    text_fields = {
        "symbol": row["symbol"],
        "market": row["market"],
        "quote_date": run.quote_date,
        "data_date": run.data_date,
        "mode": "official",
    }
    if any(str(payload.get(name) or "") != str(expected or "") for name, expected in text_fields.items()):
        return False
    value = payload.get("quote_price")
    return _finite_number(value) and _finite_number(row["price"]) and math.isclose(
        float(cast(float, value)), float(row["price"]), rel_tol=0, abs_tol=1e-8
    )


def _bar_from_contract(value: object) -> _EvidenceBar:
    if not isinstance(value, list) or len(value) != 10:
        raise ValueError("bar_contract 必须包含10个字段")
    return _EvidenceBar(
        date=str(value[0]),
        open=_positive_float(value[1]),
        close=_positive_float(value[2]),
        high=_positive_float(value[3]),
        low=_positive_float(value[4]),
        volume=_non_negative_float(value[5]),
        adjustment_mode=str(value[6]),
        data_version=str(value[7]),
        contract_version=str(value[8]),
        as_of=str(value[9]),
    )


def _valid_evidence_bar_series(
    bars: Sequence[_EvidenceBar],
    data_date: str,
    run_as_of: str,
) -> bool:
    if not bars:
        return False
    return (
        _valid_evidence_bar_identity(bars, data_date)
        and _valid_evidence_bar_contracts(bars)
        and _valid_evidence_bar_times(bars, run_as_of)
    )


def _valid_evidence_bar_identity(
    bars: Sequence[_EvidenceBar],
    data_date: str,
) -> bool:
    dates = [bar.date for bar in bars]
    versions = {bar.data_version for bar in bars}
    return (
        dates == sorted(dates)
        and len(dates) == len(set(dates))
        and bars[-1].date == data_date
        and len(versions) == 1
        and all(version.strip() for version in versions)
    )


def _valid_evidence_bar_contracts(bars: Sequence[_EvidenceBar]) -> bool:
    return (
        all(_valid_ohlc(bar) for bar in bars)
        and all(bar.adjustment_mode == "qfq" for bar in bars)
        and all(bar.contract_version == DAILY_KLINE_CONTRACT_VERSION for bar in bars)
    )


def _valid_evidence_bar_times(
    bars: Sequence[_EvidenceBar],
    run_as_of: str,
) -> bool:
    decision_epoch = market_datetime_epoch(run_as_of)
    optional_snapshot_epochs = [_evidence_timestamp_epoch(bar.as_of) for bar in bars]
    optional_row_start_epochs = [
        market_datetime_epoch(f"{bar.date} 00:00:00") for bar in bars
    ]
    if (
        decision_epoch is None
        or any(epoch is None for epoch in optional_snapshot_epochs)
        or any(epoch is None for epoch in optional_row_start_epochs)
    ):
        return False
    snapshot_epochs = cast(list[float], optional_snapshot_epochs)
    row_start_epochs = cast(list[float], optional_row_start_epochs)
    return (
        all(
            row_start <= snapshot <= decision_epoch
            for snapshot, row_start in zip(snapshot_epochs, row_start_epochs, strict=True)
        )
        and snapshot_epochs == sorted(snapshot_epochs)
    )


def _evidence_timestamp_epoch(value: object) -> float | None:
    text = str(value or "").strip()
    if len(text) == 10:
        text = f"{text} 00:00:00"
    return market_datetime_epoch(text)


def _invalid_evidence(reason: str) -> _EvidenceResult:
    return _EvidenceResult(None, None, None, reason)


def _future_range_record(
    row: sqlite3.Row,
    run: _Run,
    evidence: _EvidenceResult,
    context: _RecordContext,
    probability: Mapping[str, object] | None,
) -> dict[str, object]:
    d_bar = cast(_EvidenceBar, evidence.bar)
    overlap_status = _target_overlap_status(d_bar, context.target_rows.get(run.data_date))
    bars: dict[str, _EvidenceBar] = {}
    invalid_targets: dict[str, str] = {}
    for target_date, target in context.target_rows.items():
        if target_date == run.data_date:
            continue
        parsed, error = _verified_target_bar(target, expected_date=target_date)
        if parsed is None:
            invalid_targets[target_date] = error or "target_bar_invalid"
        else:
            bars[target_date] = parsed
    if overlap_status != "verified":
        bars = {}
        invalid_targets = {value: overlap_status for value in context.target_dates}
    executions = _execution_outcomes(row, run, d_bar, context, overlap_status)
    offsets = [
        _offset_outcome(
            session_offset,
            d_bar,
            context.target_dates,
            bars,
            invalid_targets,
            context.maximum_bar_date,
            context.calendar_error,
            executions,
        )
        for session_offset in FUTURE_RANGE_SESSION_OFFSETS
    ]
    return {
        "run_id": run.run_id,
        "quote_date": run.quote_date,
        "symbol": str(row["symbol"]),
        "name": str(row["name"]),
        "market": str(row["market"]),
        "industry": str(row["industry"] or "行业未知"),
        "rank": int(row["rank"]),
        "score": _optional_float(row["score"]),
        "raw_score": _optional_float(row["raw_score"]),
        "trend_score": _optional_float(row["trend_score"]),
        "d_bar": d_bar.payload(),
        "source_evidence": {
            "status": "verified",
            "payload_digest": evidence.digest,
            "contract_version": evidence.evidence_contract,
            "target_adjustment_continuity": overlap_status,
        },
        "probability": _matched_probability(probability, evidence, run),
        "offsets": offsets,
    }


def _target_overlap_status(d_bar: _EvidenceBar, row: sqlite3.Row | None) -> str:
    if row is None:
        return "target_adjustment_overlap_missing"
    parsed, error = _verified_target_bar(row, expected_date=d_bar.date, allow_zero_volume=True)
    if parsed is None:
        return error or "target_adjustment_rebase_conflict"
    pairs = ((parsed.open, d_bar.open), (parsed.high, d_bar.high), (parsed.low, d_bar.low), (parsed.close, d_bar.close))
    if not all(math.isclose(left, right, rel_tol=0, abs_tol=1e-8) for left, right in pairs):
        return "target_adjustment_rebase_conflict"
    return "verified"


def _execution_outcomes(
    row: sqlite3.Row,
    run: _Run,
    d_bar: _EvidenceBar,
    context: _RecordContext,
    overlap_status: str,
) -> dict[int, dict[str, object]]:
    label_config = ProbabilityLabelConfig(
        horizons=(1, 2), cost_profile=context.config.cost_profile,
        execution_notional=context.config.execution_notional,
        max_daily_participation_rate=context.config.max_daily_participation_rate,
    )
    if overlap_status != "verified":
        return _with_execution_contract(
            _unavailable_execution_set(context.target_dates, overlap_status), label_config
        )
    rows = [_to_label_kline(d_bar)]
    for target_date in context.target_dates:
        raw = context.target_rows.get(target_date)
        if raw is None:
            continue
        parsed, _error = _verified_target_bar(raw, expected_date=target_date, allow_zero_volume=True)
        if parsed is not None:
            rows.append(_to_label_kline(parsed))
    try:
        outcomes = build_probability_label_outcomes(
            symbol=str(row["symbol"]), market=str(row["market"]),
            list_date=str(row["list_date"]) if row["list_date"] else None,
            is_st=bool(row["is_st"]), quote_date=run.quote_date,
            amount=float(row["amount"] or 0), rows=rows,
            eligible_dates=context.target_dates, config=label_config,
        )
    except (TypeError, ValueError):
        return _with_execution_contract(
            _unavailable_execution_set(context.target_dates, "execution_label_input_conflict"), label_config
        )
    return _with_execution_contract({
        1: _same_session_unexecutable(context.target_dates),
        2: _execution_payload(outcomes[1]),
        3: _execution_payload(outcomes[2]),
    }, label_config)


def _with_execution_contract(
    outcomes: dict[int, dict[str, object]],
    config: ProbabilityLabelConfig,
) -> dict[int, dict[str, object]]:
    contract = probability_label_contract(config)
    fields = {
        "label_version": contract["label_version"],
        "execution_model": contract["execution_model"],
        "cost_model_version": contract["cost_model_version"],
        "cost_profile_id": contract["cost_profile_id"],
    }
    for outcome in outcomes.values():
        outcome.update(fields)
    return outcomes


def _to_label_kline(bar: _EvidenceBar) -> Kline:
    return Kline(
        date=bar.date, open=bar.open, close=bar.close, high=bar.high, low=bar.low,
        volume=bar.volume, adjustment_mode="qfq", as_of=bar.date,
        data_version=bar.data_version, contract_version=bar.contract_version,
        source="future-range-fixed-session-evidence", from_cache=True,
    )


def _execution_payload(outcome: ProbabilityLabelOutcome) -> dict[str, object]:
    return {
        "status": outcome.status, "reason": outcome.reason,
        "gross_return": _optional_float(outcome.gross_return),
        "net_return": _optional_float(outcome.net_return),
        "cost_drag": _optional_float(outcome.cost_drag),
        "market_benchmark_net_return": None, "net_excess_return": None,
        "entry_date": outcome.entry_date, "exit_date": outcome.exit_date,
        "entry_price": _optional_float(outcome.entry_price),
        "exit_price": _optional_float(outcome.exit_price),
        "model_limited": outcome.model_limited,
        "daily_bar_model_limited": outcome.daily_bar_model_limited,
        "rule_profile_verified": outcome.rule_profile_verified,
    }


def _same_session_unexecutable(target_dates: Sequence[str]) -> dict[str, object]:
    outcome = ProbabilityLabelOutcome(
        horizon=0, status="data_unavailable", reason="A_share_T_plus_1_no_same_session_exit",
        entry_date=target_dates[0] if target_dates else None,
    )
    return _execution_payload(outcome)


def _unavailable_execution_set(
    target_dates: Sequence[str],
    reason: str,
) -> dict[int, dict[str, object]]:
    values = {
        offset: _execution_payload(
            ProbabilityLabelOutcome(
                horizon=max(0, offset - 1), status="data_unavailable", reason=reason,
                entry_date=target_dates[0] if target_dates else None,
                exit_date=target_dates[offset - 1] if len(target_dates) >= offset else None,
            )
        )
        for offset in FUTURE_RANGE_SESSION_OFFSETS
    }
    values[1] = _same_session_unexecutable(target_dates)
    return values


def _attach_market_benchmarks(records: Sequence[dict[str, object]]) -> None:
    for session_offset in (2, 3):
        modelled = [
            execution for record in records
            if (execution := cast(dict[str, object], _offset_for(record, session_offset)["execution"]))["status"] == "modelled"
            and _finite_number(execution.get("net_return"))
        ]
        if not modelled:
            continue
        benchmark = fmean(float(cast(float, item["net_return"])) for item in modelled)
        for execution in modelled:
            net_return = float(cast(float, execution["net_return"]))
            execution["market_benchmark_net_return"] = _rounded(benchmark)
            execution["net_excess_return"] = _rounded(net_return - benchmark)


def _matched_probability(
    probability: Mapping[str, object] | None,
    evidence: _EvidenceResult,
    run: _Run,
) -> dict[str, object]:
    if probability is None or probability.get("status") != "calibrated_shadow":
        return {"status": "not_available", "predictions": []}
    predictions = [
        dict(item)
        for item in cast(Sequence[Mapping[str, object]], probability.get("predictions") or ())
        if item.get("source_evidence_digest") == evidence.digest
        and item.get("quote_date") == run.quote_date
        and item.get("cohort_contract") == run.contract()
    ]
    return (
        {"status": "calibrated_shadow", "predictions": predictions}
        if predictions
        else {"status": "not_available", "predictions": []}
    )


def _verified_target_bar(
    row: sqlite3.Row,
    *,
    expected_date: str,
    allow_zero_volume: bool = False,
) -> tuple[_EvidenceBar | None, str | None]:
    if str(row["date"]) != expected_date:
        return None, "target_bar_date_conflict"
    try:
        bar = _EvidenceBar(
            date=str(row["date"]),
            open=_positive_float(row["open"]),
            close=_positive_float(row["close"]),
            high=_positive_float(row["high"]),
            low=_positive_float(row["low"]),
            volume=_non_negative_float(row["volume"]),
            adjustment_mode=str(row["adjustment_mode"]),
            data_version=str(row["data_version"]),
            contract_version=str(row["contract_version"]),
            as_of=str(row["as_of"]),
        )
    except (TypeError, ValueError):
        return None, "target_bar_nonfinite_or_nonpositive"
    if bar.adjustment_mode != "qfq" or bar.contract_version != DAILY_KLINE_CONTRACT_VERSION:
        return None, "target_bar_version_or_qfq_conflict"
    if not bar.data_version.strip() or not _valid_ohlc(bar):
        return None, "target_bar_version_or_ohlc_invalid"
    if bar.volume <= 0 and not allow_zero_volume:
        return None, "target_session_suspended_or_zero_volume"
    return bar, None


def _offset_outcome(
    session_offset: int,
    d_bar: _EvidenceBar,
    target_dates: Sequence[str],
    bars: Mapping[str, _EvidenceBar],
    invalid_targets: Mapping[str, str],
    maximum_bar_date: str | None,
    calendar_error: str | None,
    executions: Mapping[int, dict[str, object]],
) -> dict[str, object]:
    target_date = target_dates[session_offset - 1] if len(target_dates) >= session_offset else None
    target = bars.get(target_date) if target_date is not None else None
    status, reason = _fixed_session_status(
        target_date,
        target,
        invalid_targets,
        maximum_bar_date,
        calendar_error,
    )
    if target is None:
        return _empty_offset(session_offset, target_date, status, reason, executions[session_offset])
    d1_reference = _d1_open_outcome(session_offset, target_dates, bars)
    return {
        "session_offset": session_offset,
        "target_session_date": target_date,
        "fixed_session_status": status,
        "reason": reason,
        "target_bar": target.payload(),
        "target_bar_digest": _bar_digest(target),
        "level_shift": {
            "low": _return(target.low, d_bar.low),
            "hlc3_proxy": _return(target.hlc3, d_bar.hlc3),
            "high": _return(target.high, d_bar.high),
        },
        "d_close_reference": {
            "low": _return(target.low, d_bar.close),
            "hlc3_proxy": _return(target.hlc3, d_bar.close),
            "high": _return(target.high, d_bar.close),
            "close": _return(target.close, d_bar.close),
        },
        "d1_open_reference": d1_reference,
        "execution": executions[session_offset],
        "daily_bar_path_unknown": True,
        "interval_structure": _interval_structure(d_bar, target),
    }


def _fixed_session_status(
    target_date: str | None,
    target: _EvidenceBar | None,
    invalid_targets: Mapping[str, str],
    maximum_bar_date: str | None,
    calendar_error: str | None,
) -> tuple[str, str | None]:
    if target_date is None:
        return "not_mature", calendar_error or "trusted_calendar_future_coverage_unavailable"
    if target is not None:
        return "available", None
    if target_date in invalid_targets:
        return "unavailable", invalid_targets[target_date]
    if maximum_bar_date is None or target_date > maximum_bar_date:
        return "not_mature", "target_exchange_session_not_ingested_yet"
    return "unavailable", "fixed_target_session_bar_missing_no_forward_shift"


def _empty_offset(
    session_offset: int,
    target_date: str | None,
    status: str,
    reason: str | None,
    execution: Mapping[str, object],
) -> dict[str, object]:
    return {
        "session_offset": session_offset,
        "target_session_date": target_date,
        "fixed_session_status": status,
        "reason": reason,
        "target_bar": None,
        "target_bar_digest": None,
        "level_shift": None,
        "d_close_reference": None,
        "d1_open_reference": None,
        "execution": dict(execution),
        "daily_bar_path_unknown": None,
        "interval_structure": None,
    }


def _d1_open_outcome(
    session_offset: int,
    target_dates: Sequence[str],
    bars: Mapping[str, _EvidenceBar],
) -> dict[str, object]:
    required_dates = tuple(target_dates[:session_offset])
    if len(required_dates) != session_offset:
        return _unavailable_d1_reference("trusted_calendar_path_incomplete")
    missing = [value for value in required_dates if value not in bars]
    if missing:
        return _unavailable_d1_reference("fixed_path_bar_missing_no_forward_shift")
    path = [bars[value] for value in required_dates]
    entry = path[0].open
    target = path[-1]
    return {
        "status": "available",
        "reason": None,
        "entry_date": path[0].date,
        "entry_price": _rounded(entry),
        "specified_day": {
            "low": _return(target.low, entry),
            "hlc3_proxy": _return(target.hlc3, entry),
            "high": _return(target.high, entry),
            "close": _return(target.close, entry),
        },
        "cumulative_path": {
            "mae": _return(min(bar.low for bar in path), entry),
            "mfe": _return(max(bar.high for bar in path), entry),
            "terminal_close_return": _return(target.close, entry),
            "daily_bar_path_unknown": True,
        },
    }


def _unavailable_d1_reference(reason: str) -> dict[str, object]:
    return {
        "status": "unavailable",
        "reason": reason,
        "entry_date": None,
        "entry_price": None,
        "specified_day": None,
        "cumulative_path": None,
    }


def _interval_structure(d_bar: _EvidenceBar, target: _EvidenceBar) -> dict[str, object]:
    intersection = max(0.0, min(d_bar.high, target.high) - max(d_bar.low, target.low))
    union = max(d_bar.high, target.high) - min(d_bar.low, target.low)
    target_width = (target.high - target.low) / target.hlc3
    d_width = (d_bar.high - d_bar.low) / d_bar.hlc3
    return {
        "normalized_width": _rounded(target_width),
        "width_change": _rounded(target_width - d_width),
        "overlap_ratio": _rounded(intersection / union) if union > 0 else 1.0,
        "higher_high": target.high > d_bar.high,
        "higher_low": target.low > d_bar.low,
        "full_gap_up": target.low > d_bar.high,
        "full_gap_down": target.high < d_bar.low,
    }


def _run_reports(
    selected: Sequence[_Run],
    context_runs: Sequence[_Run],
    evaluated: Mapping[int, Mapping[str, object]],
    config: FutureRangeConfig,
    generated_at: str,
    database: Path,
) -> list[dict[str, object]]:
    by_cohort: dict[tuple[str, str, str], list[dict[str, object]]] = defaultdict(list)
    for run in context_runs:
        result = evaluated[run.run_id]
        by_cohort[run.cohort].extend(cast(Sequence[dict[str, object]], result["records"]))
    aggregates = {
        cohort: _cohort_aggregates(cohort, records, config)
        for cohort, records in by_cohort.items()
    }
    reports: list[dict[str, object]] = []
    for run in selected:
        result = evaluated[run.run_id]
        records = cast(list[dict[str, object]], result["records"])
        aggregate = aggregates[run.cohort]
        reports.append(
            _build_run_report(
                run, result, records, aggregate, config, generated_at, database
            )
        )
    return reports


def _build_run_report(
    run: _Run,
    result: Mapping[str, object],
    records: list[dict[str, object]],
    aggregate: Mapping[str, object],
    config: FutureRangeConfig,
    generated_at: str,
    database: Path,
) -> dict[str, object]:
    status, coverage = _report_status(result, records, aggregate, config)
    probability_meta = cast(Mapping[str, object], aggregate["probability_context"])
    return {
        "report_contract_version": FUTURE_RANGE_REPORT_CONTRACT_VERSION,
        "status": status,
        "generated_at": generated_at,
        "run": _run_payload(run),
        "config": _config_payload(config),
        "source": _source_payload(database, result, records, aggregate, coverage),
        "records": records,
        "groups": aggregate["groups"],
        "rank_ic": aggregate["rank_ic"],
        "monotonicity": aggregate["monotonicity"],
        "probability_context": dict(probability_meta),
        "limitations": _report_limitations(status, probability_meta),
    }


def _run_payload(run: _Run) -> dict[str, object]:
    return {
        "run_id": run.run_id, "mode": run.mode, "scope": run.scope,
        "rule_version": run.rule_version, "quote_date": run.quote_date,
        "data_date": run.data_date, "as_of": run.as_of,
        "snapshot_digest": run.snapshot_digest,
    }


def _config_payload(config: FutureRangeConfig) -> dict[str, object]:
    return {
        "research_version": FUTURE_RANGE_RESEARCH_VERSION,
        "session_offsets": list(config.session_offsets),
        "center_proxy": FUTURE_RANGE_CENTER_PROXY,
        "center_proxy_formula": "(high + low + close) / 3",
        "reference_prices": ["same_named_point", "d_close", "d1_open"],
        "top_sizes": list(config.top_sizes), "deciles": 10,
        "minimum_sample_size": config.minimum_sample_size,
        "minimum_session_count": config.minimum_session_count,
        "complete_run_coverage": config.complete_run_coverage,
        "bootstrap_samples": config.bootstrap_samples,
        "validation_gap_sessions": config.validation_gap_sessions,
        "validation_gap_semantics": "required_for_future_train_test_splits_not_descriptive_group_filter",
        "bootstrap_method": "ordered_moving_block_by_signal_date",
        "bootstrap_block_sessions": config.bootstrap_block_sessions,
        "fixed_session_no_suspension_shift": True,
        "execution_label_contract": probability_label_contract(
            ProbabilityLabelConfig(
                horizons=(1, 2), cost_profile=config.cost_profile,
                execution_notional=config.execution_notional,
                max_daily_participation_rate=config.max_daily_participation_rate,
            )
        ),
        "execution_capacity_basis": "frozen_signal_day_amount_proxy",
    }


def _source_payload(
    database: Path,
    result: Mapping[str, object],
    records: Sequence[Mapping[str, object]],
    aggregate: Mapping[str, object],
    coverage: Mapping[str, object],
) -> dict[str, object]:
    return {
        "database": _portable_database_label(database), "read_only": True, "query_only": True,
        "transaction_snapshot": True,
        "adjustment_mode": "qfq",
        "signal_bar_source": "verified_persisted_point_in_time_bar_contract_61",
        "target_bar_source": "persisted_qfq_kline_daily_fixed_exchange_session",
        "expected_result_count": cast(int, result["expected_result_count"]),
        "verified_record_count": len(records),
        "point_in_time_evidence_coverage": coverage["evidence"],
        "offset_coverage": coverage["offsets"],
        "context_canonical_run_count": aggregate["canonical_session_count"],
        "calendar_error": result["calendar_error"], "exclusions": result["exclusions"],
    }


def _cohort_aggregates(
    cohort: tuple[str, str, str],
    records: Sequence[dict[str, object]],
    config: FutureRangeConfig,
) -> dict[str, object]:
    groups = _group_reports(cohort, records, config)
    rank_ic = _rank_ic_reports(cohort, records, config)
    monotonicity = _monotonicity_reports(cohort, groups, config)
    probability_context = _probability_report_context(records, config)
    sessions = {str(record["quote_date"]) for record in records}
    return {
        "canonical_session_count": len(sessions),
        "groups": groups,
        "rank_ic": rank_ic,
        "monotonicity": monotonicity,
        "probability_context": probability_context,
    }


def _group_reports(
    cohort: tuple[str, str, str],
    records: Sequence[dict[str, object]],
    config: FutureRangeConfig,
) -> list[dict[str, object]]:
    definitions = [("all", "ALL"), *(("top_n", str(size)) for size in config.top_sizes)]
    definitions.extend(("decile", f"Q{value}") for value in range(1, 11))
    reports: list[dict[str, object]] = []
    for group_type, group_value in definitions:
        selected = _select_group(records, group_type, group_value)
        for session_offset in config.session_offsets:
            metrics = {
                metric: _metric_summary(selected, session_offset, metric, config, f"{cohort}:{group_type}:{group_value}:{session_offset}:{metric}")
                for metric in FUTURE_RANGE_METRICS
            }
            reports.append(
                {
                    "cohort": _cohort_contract(cohort),
                    "group_type": group_type,
                    "group_value": group_value,
                    "session_offset": session_offset,
                    "status": _combined_metric_status(metrics),
                    "sample_size": max((cast(int, value["sample_size"]) for value in metrics.values()), default=0),
                    "independent_session_count": max(
                        (cast(int, value["independent_session_count"]) for value in metrics.values()), default=0
                    ),
                    "metrics": metrics,
                }
            )
    return reports


def _select_group(
    records: Sequence[dict[str, object]],
    group_type: str,
    group_value: str,
) -> list[dict[str, object]]:
    if group_type == "all":
        return list(records)
    if group_type == "top_n":
        limit = int(group_value)
        return [record for record in records if cast(int, record["rank"]) <= limit]
    decile = int(group_value[1:])
    totals: dict[tuple[int, str], int] = defaultdict(int)
    for record in records:
        totals[(cast(int, record["run_id"]), str(record["quote_date"]))] += 1
    return [
        record
        for record in records
        if _rank_decile(cast(int, record["rank"]), totals[(cast(int, record["run_id"]), str(record["quote_date"]))]) == decile
    ]


def _rank_decile(rank: int, total: int) -> int:
    return max(1, min(10, 10 - ((rank - 1) * 10 // max(1, total))))


def _metric_summary(
    records: Sequence[dict[str, object]],
    session_offset: int,
    metric: str,
    config: FutureRangeConfig,
    seed_key: str,
) -> dict[str, object]:
    by_date: dict[str, list[float]] = defaultdict(list)
    for record in records:
        value = _record_metric(record, session_offset, metric)
        if value is not None:
            by_date[str(record["quote_date"])].append(value)
    values = [value for items in by_date.values() for value in items]
    session_means = [fmean(items) for _date, items in sorted(by_date.items())]
    session_positive_rates = [fmean(value > 0 for value in items) for _date, items in sorted(by_date.items())]
    status = _sample_status(len(values), len(session_means), config)
    return {
        "status": status,
        "sample_size": len(values),
        "independent_session_count": len(session_means),
        "mean": _rounded(fmean(session_means)) if session_means else None,
        "median": _rounded(float(median(values))) if values else None,
        "positive_rate": _rounded(fmean(session_positive_rates)) if session_positive_rates else None,
        "ci95": _bootstrap_ci(session_means, config, seed_key) if status == "ok" else None,
    }


def _record_metric(record: Mapping[str, object], session_offset: int, metric: str) -> float | None:
    offsets = cast(Sequence[Mapping[str, object]], record["offsets"])
    outcome = next((item for item in offsets if item.get("session_offset") == session_offset), None)
    if outcome is None or outcome.get("fixed_session_status") != "available":
        return None
    if metric.startswith("level_shift_"):
        values = outcome.get("level_shift")
        key = metric.removeprefix("level_shift_")
    elif metric in {"net_return", "net_excess_return"}:
        values = outcome.get("execution")
        key = metric
    else:
        reference = outcome.get("d1_open_reference")
        values = reference.get("cumulative_path") if isinstance(reference, Mapping) else None
        key = metric
    value = values.get(key) if isinstance(values, Mapping) else None
    return float(cast(float, value)) if _finite_number(value) else None


def _rank_ic_reports(
    cohort: tuple[str, str, str],
    records: Sequence[dict[str, object]],
    config: FutureRangeConfig,
) -> list[dict[str, object]]:
    reports: list[dict[str, object]] = []
    for session_offset in config.session_offsets:
        for metric in FUTURE_RANGE_METRICS:
            by_date: dict[str, list[tuple[float, float]]] = defaultdict(list)
            for record in records:
                score = record.get("trend_score")
                outcome = _record_metric(record, session_offset, metric)
                if _finite_number(score) and outcome is not None:
                    by_date[str(record["quote_date"])].append((float(cast(float, score)), outcome))
            session_ics = [
                coefficient
                for _date, pairs in sorted(by_date.items())
                if (coefficient := _spearman_pairs(pairs)) is not None
            ]
            observations = sum(len(items) for items in by_date.values())
            status = _sample_status(observations, len(session_ics), config)
            reports.append(
                {
                    "cohort": _cohort_contract(cohort),
                    "score_field": "trend_score",
                    "session_offset": session_offset,
                    "metric": metric,
                    "status": status,
                    "observation_count": observations,
                    "independent_session_count": len(session_ics),
                    "mean_rank_ic": _rounded(fmean(session_ics)) if session_ics else None,
                    "median_rank_ic": _rounded(float(median(session_ics))) if session_ics else None,
                    "ci95": (
                        _bootstrap_ci(session_ics, config, f"{cohort}:ic:{session_offset}:{metric}")
                        if status == "ok"
                        else None
                    ),
                }
            )
    return reports


def _monotonicity_reports(
    cohort: tuple[str, str, str],
    groups: Sequence[dict[str, object]],
    config: FutureRangeConfig,
) -> list[dict[str, object]]:
    reports: list[dict[str, object]] = []
    for session_offset in config.session_offsets:
        for metric in FUTURE_RANGE_METRICS:
            reports.append(_monotonicity_report(cohort, groups, config, session_offset, metric))
    return reports


def _monotonicity_report(
    cohort: tuple[str, str, str],
    groups: Sequence[dict[str, object]],
    config: FutureRangeConfig,
    session_offset: int,
    metric: str,
) -> dict[str, object]:
    values = _decile_metric_values(groups, session_offset, metric)
    complete = len(values) == 10
    sessions = min((item[3] for item in values), default=0)
    sufficient = complete and sessions >= config.minimum_session_count and all(item[4] == "ok" for item in values)
    status: FutureRangeStatus = "ok" if sufficient else "insufficient_data"
    coefficient = _spearman_pairs([(float(item[0]), item[1]) for item in values]) if complete else None
    passed = bool(coefficient is not None and coefficient >= 0.7 and values[-1][1] > values[0][1]) if sufficient else None
    return {
        "cohort": _cohort_contract(cohort), "session_offset": session_offset,
        "metric": metric, "status": status, "independent_session_count": sessions,
        "direction": "higher_score_higher_outcome",
        "decile_medians": [
            {"decile": f"Q{decile}", "value": _rounded(value), "sample_size": sample_size}
            for decile, value, sample_size, _sessions, _status in values
        ],
        "spearman": _rounded(coefficient) if coefficient is not None else None,
        "passed": passed,
    }


def _decile_metric_values(
    groups: Sequence[dict[str, object]],
    session_offset: int,
    metric: str,
) -> list[tuple[int, float, int, int, str]]:
    values: list[tuple[int, float, int, int, str]] = []
    for decile in range(1, 11):
        group = next(
            item for item in groups
            if item["group_type"] == "decile" and item["group_value"] == f"Q{decile}"
            and item["session_offset"] == session_offset
        )
        summary = cast(Mapping[str, object], cast(Mapping[str, object], group["metrics"])[metric])
        if _finite_number(summary.get("median")):
            values.append((
                decile, float(cast(float, summary["median"])), cast(int, summary["sample_size"]),
                cast(int, summary["independent_session_count"]), str(summary["status"]),
            ))
    return values


def _report_status(
    result: Mapping[str, object],
    records: Sequence[Mapping[str, object]],
    aggregate: Mapping[str, object],
    config: FutureRangeConfig,
) -> tuple[FutureRangeStatus, dict[str, object]]:
    expected = cast(int, result["expected_result_count"])
    evidence_coverage = len(records) / expected if expected > 0 else 0.0
    offset_coverage: dict[str, float] = {}
    for session_offset in config.session_offsets:
        available = sum(
            _offset_for(record, session_offset).get("fixed_session_status") == "available"
            for record in records
        )
        offset_coverage[str(session_offset)] = available / expected if expected > 0 else 0.0
    groups = cast(Sequence[Mapping[str, object]], aggregate["groups"])
    all_groups = [item for item in groups if item["group_type"] == "all"]
    evidence_ready = evidence_coverage >= config.complete_run_coverage
    offsets_ready = all(value >= config.complete_run_coverage for value in offset_coverage.values())
    sessions_ready = all(
        cast(Mapping[str, object], cast(Mapping[str, object], item["metrics"])["level_shift_hlc3_proxy"])["status"] == "ok"
        for item in all_groups
    )
    status: FutureRangeStatus = "ok" if evidence_ready and offsets_ready and sessions_ready else "insufficient_data"
    return status, {"evidence": _rounded(evidence_coverage), "offsets": offset_coverage}


def _offset_for(record: Mapping[str, object], session_offset: int) -> Mapping[str, object]:
    offsets = cast(Sequence[Mapping[str, object]], record["offsets"])
    return next(item for item in offsets if item["session_offset"] == session_offset)


def _sample_status(sample_size: int, session_count: int, config: FutureRangeConfig) -> FutureRangeStatus:
    return (
        "ok"
        if sample_size >= config.minimum_sample_size and session_count >= config.minimum_session_count
        else "insufficient_data"
    )


def _combined_metric_status(metrics: Mapping[str, Mapping[str, object]]) -> FutureRangeStatus:
    core = [item for name, item in metrics.items() if name not in {"net_return", "net_excess_return"}]
    return "ok" if core and all(item["status"] == "ok" for item in core) else "insufficient_data"


def _bootstrap_ci(values: Sequence[float], config: FutureRangeConfig, seed_key: str) -> list[float] | None:
    if not values:
        raise ValueError("bootstrap values 不能为空")
    block = config.bootstrap_block_sessions
    if len(values) < block:
        return None
    seed_digest = hashlib.sha256(seed_key.encode("utf-8")).digest()
    generator = random.Random(config.bootstrap_seed ^ int.from_bytes(seed_digest[:8], "big"))
    estimates = sorted(
        fmean(_moving_block_sample(values, block, generator))
        for _sample in range(config.bootstrap_samples)
    )
    lower = estimates[math.floor(0.025 * (len(estimates) - 1))]
    upper = estimates[math.ceil(0.975 * (len(estimates) - 1))]
    return [_rounded(lower), _rounded(upper)]


def _moving_block_sample(values: Sequence[float], block: int, generator: random.Random) -> list[float]:
    sampled: list[float] = []
    maximum_start = len(values) - block
    while len(sampled) < len(values):
        start = generator.randint(0, maximum_start)
        sampled.extend(values[start : start + block])
    return sampled[: len(values)]


def _spearman_pairs(pairs: Sequence[tuple[float, float]]) -> float | None:
    if len(pairs) < 2:
        return None
    left, right = zip(*pairs, strict=True)
    return _pearson(_average_ranks(left), _average_ranks(right))


def _average_ranks(values: Sequence[float]) -> tuple[float, ...]:
    ordered = sorted(enumerate(values), key=lambda item: (item[1], item[0]))
    ranks = [0.0] * len(values)
    cursor = 0
    while cursor < len(ordered):
        end = cursor + 1
        while end < len(ordered) and ordered[end][1] == ordered[cursor][1]:
            end += 1
        rank = (cursor + 1 + end) / 2
        for index in range(cursor, end):
            ranks[ordered[index][0]] = rank
        cursor = end
    return tuple(ranks)


def _pearson(left: Sequence[float], right: Sequence[float]) -> float | None:
    left_mean, right_mean = fmean(left), fmean(right)
    numerator = sum((a - left_mean) * (b - right_mean) for a, b in zip(left, right, strict=True))
    left_variance = sum((value - left_mean) ** 2 for value in left)
    right_variance = sum((value - right_mean) ** 2 for value in right)
    denominator = math.sqrt(left_variance * right_variance)
    return numerator / denominator if denominator > 0 else None


def _load_probability_context(
    paths: Sequence[str | Path],
) -> dict[tuple[int, str], dict[str, object]]:
    index: dict[tuple[int, str], dict[str, object]] = {}
    predictions: dict[tuple[int, str], list[dict[str, object]]] = defaultdict(list)
    for path in paths:
        artifact = load_probability_artifact(path)
        integrity = cast(Mapping[str, object], artifact["integrity"])
        payload = cast(Mapping[str, object], artifact["payload"])
        studies = {
            (item["run_id"], item["target"], item["horizon"]): item
            for item in cast(Sequence[Mapping[str, object]], payload["studies"])
        }
        for raw_record in cast(Sequence[Mapping[str, object]], payload["records"]):
            study = studies.get((raw_record.get("run_id"), raw_record.get("target"), raw_record.get("horizon")))
            parsed = _verified_probability_prediction(raw_record, integrity, study)
            if parsed is None:
                continue
            key, prediction = parsed
            identity = (str(prediction["target"]), cast(int, prediction["horizon"]))
            if any((item["target"], item["horizon"]) == identity for item in predictions[key]):
                raise FutureRangeResearchError(f"上涨概率 artifact 预测身份冲突：{key} {identity}")
            predictions[key].append(prediction)
    for key, values in predictions.items():
        index[key] = {
            "status": "calibrated_shadow",
            "predictions": sorted(values, key=lambda item: (cast(int, item["horizon"]), str(item["target"]))),
        }
    return index


def _verified_probability_prediction(
    record: Mapping[str, object],
    integrity: Mapping[str, object],
    study: Mapping[str, object] | None,
) -> tuple[tuple[int, str], dict[str, object]] | None:
    if study is None or record.get("status") != "calibrated_shadow" or not _finite_probability(record.get("probability")):
        return None
    details = record.get("details")
    if not isinstance(details, Mapping):
        return None
    quote_date = details.get("quote_date")
    cutoff = details.get("training_cutoff")
    fold_id = details.get("fold_id")
    versions = details.get("versions")
    metadata = study.get("metadata")
    cohort = metadata.get("cohort_contract") if isinstance(metadata, Mapping) else None
    if (
        not isinstance(quote_date, str)
        or not isinstance(cutoff, str)
        or cutoff >= quote_date
        or (fold_id is not None and (isinstance(fold_id, bool) or not isinstance(fold_id, int)))
        or not isinstance(versions, Mapping)
        or not isinstance(cohort, Mapping)
    ):
        return None
    run_id, symbol = record.get("run_id"), record.get("symbol")
    if isinstance(run_id, bool) or not isinstance(run_id, int) or not isinstance(symbol, str):
        return None
    prediction = {
        "target": str(record["target"]),
        "horizon": int(cast(int, record["horizon"])),
        "probability": _rounded(float(cast(float, record["probability"]))),
        "training_cutoff": cutoff,
        "quote_date": quote_date,
        "model_version": versions.get("model"),
        "calibrator_version": versions.get("calibrator"),
        "target_definition": details.get("target_definition"),
        "calibration_summary": details.get("calibration_summary"),
        "source_evidence_digest": details.get("source_evidence_digest"),
        "source_artifact_digest": integrity.get("integrity_digest"),
        "cohort_contract": dict(cohort),
    }
    return (run_id, symbol), prediction


def _probability_report_context(
    records: Sequence[dict[str, object]],
    config: FutureRangeConfig,
) -> dict[str, object]:
    linked = _linked_probability_predictions(records)
    flattened = [prediction for _record, prediction in linked]
    digests = sorted(
        {
            str(item["source_artifact_digest"])
            for item in flattened
            if isinstance(item.get("source_artifact_digest"), str)
        }
    )
    return {
        "status": "available" if flattened else "not_available",
        "source": "persisted_oos_calibrated_shadow_only",
        "prediction_count": len(flattened),
        "artifact_digests": digests,
        "original_target_calibration": _original_probability_calibration(flattened),
        "range_outcome_comparisons": _probability_range_comparisons(linked, config),
        "limitations": (
            ["probability_is_explanatory_only_not_recalibrated_against_range_outcomes"]
            if flattened
            else ["calibrated_shadow_artifact_not_supplied_or_not_available"]
        ),
    }


def _linked_probability_predictions(
    records: Sequence[dict[str, object]],
) -> list[tuple[dict[str, object], Mapping[str, object]]]:
    linked: list[tuple[dict[str, object], Mapping[str, object]]] = []
    for record in records:
        probability = record.get("probability")
        if not isinstance(probability, Mapping) or probability.get("status") != "calibrated_shadow":
            continue
        linked.extend(
            (record, item)
            for item in cast(Sequence[Mapping[str, object]], probability.get("predictions") or ())
        )
    return linked


def _original_probability_calibration(
    predictions: Sequence[Mapping[str, object]],
) -> list[dict[str, object]]:
    unique: dict[tuple[str, int, str], dict[str, object]] = {}
    for prediction in predictions:
        key = (
            str(prediction["target"]), cast(int, prediction["horizon"]),
            str(prediction.get("source_artifact_digest") or ""),
        )
        unique[key] = {
            "target": key[0], "horizon": key[1],
            "target_definition": prediction.get("target_definition"),
            "calibration_summary": prediction.get("calibration_summary"),
            "source_artifact_digest": key[2],
            "semantics": "original_binary_target_calibration_not_range_outcome_calibration",
        }
    return [unique[key] for key in sorted(unique)]


def _probability_range_comparisons(
    linked: Sequence[tuple[dict[str, object], Mapping[str, object]]],
    config: FutureRangeConfig,
) -> list[dict[str, object]]:
    identities = sorted({(str(item[1]["target"]), cast(int, item[1]["horizon"])) for item in linked})
    comparisons: list[dict[str, object]] = []
    for target, horizon in identities:
        selected = [item for item in linked if item[1]["target"] == target and item[1]["horizon"] == horizon]
        for session_offset in config.session_offsets:
            for metric in ("level_shift_hlc3_proxy", "mae", "mfe", "terminal_close_return", "net_return", "net_excess_return"):
                comparisons.append(
                    _probability_range_comparison(selected, target, horizon, session_offset, metric, config)
                )
    return comparisons


def _probability_range_comparison(
    linked: Sequence[tuple[dict[str, object], Mapping[str, object]]],
    target: str,
    horizon: int,
    session_offset: int,
    metric: str,
    config: FutureRangeConfig,
) -> dict[str, object]:
    bins = [
        _probability_bin(linked, lower, upper, session_offset, metric, config, f"{target}:{horizon}")
        for lower, upper in ((0.0, 0.2), (0.2, 0.4), (0.4, 0.6), (0.6, 0.8), (0.8, 1.0))
    ]
    return {
        "target": target, "probability_horizon": horizon,
        "session_offset": session_offset, "range_metric": metric,
        "status": "ok" if bins and all(item["status"] == "ok" for item in bins) else "insufficient_data",
        "semantics": "range_outcome_comparison_not_probability_recalibration",
        "bins": bins,
    }


def _probability_bin(
    linked: Sequence[tuple[dict[str, object], Mapping[str, object]]],
    lower: float,
    upper: float,
    session_offset: int,
    metric: str,
    config: FutureRangeConfig,
    seed_prefix: str,
) -> dict[str, object]:
    selected = [
        (record, prediction) for record, prediction in linked
        if lower <= float(cast(float, prediction["probability"])) <= upper
        and (upper == 1.0 or float(cast(float, prediction["probability"])) < upper)
    ]
    summary = _metric_summary(
        [record for record, _prediction in selected], session_offset, metric, config,
        f"{seed_prefix}:{lower}:{upper}:{session_offset}:{metric}",
    )
    probabilities = [float(cast(float, prediction["probability"])) for _record, prediction in selected]
    return {
        "lower": lower, "upper": upper, "upper_inclusive": upper == 1.0,
        "probability_mean": _rounded(fmean(probabilities)) if probabilities else None,
        **summary,
    }


def _cohort_contract(cohort: tuple[str, str, str]) -> dict[str, str]:
    mode, scope, rule_version = cohort
    return {"mode": mode, "scope": scope, "rule_version": rule_version}


def _bar_digest(bar: _EvidenceBar) -> str:
    encoded = json.dumps(bar.payload(), sort_keys=True, separators=(",", ":"), allow_nan=False)
    return hashlib.sha256(encoded.encode("utf-8")).hexdigest()


def _valid_ohlc(bar: _EvidenceBar) -> bool:
    return (
        min(bar.open, bar.close, bar.high, bar.low) > 0
        and bar.high >= max(bar.open, bar.close, bar.low)
        and bar.low <= min(bar.open, bar.close, bar.high)
    )


def _return(value: float, reference: float) -> float:
    return _rounded(value / reference - 1)


def _positive_float(value: object) -> float:
    parsed = _non_negative_float(value)
    if parsed <= 0:
        raise ValueError("价格必须为正数")
    return parsed


def _non_negative_float(value: object) -> float:
    if not _finite_number(value):
        raise ValueError("数值必须有限")
    parsed = float(cast(float, value))
    if parsed < 0:
        raise ValueError("数值不能为负")
    return parsed


def _optional_float(value: object) -> float | None:
    return _rounded(float(cast(float, value))) if _finite_number(value) else None


def _finite_number(value: object) -> bool:
    return not isinstance(value, bool) and isinstance(value, int | float) and math.isfinite(float(value))


def _finite_probability(value: object) -> bool:
    return _finite_number(value) and 0 <= float(cast(float, value)) <= 1


def _rounded(value: float, decimals: int = 10) -> float:
    if not math.isfinite(value):
        raise FutureRangeResearchError("未来区间研究产生非有限数值")
    return round(float(value), decimals)


def _portable_database_label(path: Path) -> str:
    try:
        return path.relative_to(Path.cwd().resolve()).as_posix()
    except ValueError:
        return str(path)


def _base_limitations() -> list[str]:
    return [
        "HLC3_is_a_typical_price_proxy_not_true_VWAP",
        "daily_OHLC_cannot_reconstruct_intraday_path_or_stop_target_order",
        "future_high_and_low_are_exploratory_excursions_not_simultaneously_realizable_returns",
        "fixed_exchange_session_is_not_shifted_when_a_symbol_is_suspended",
        "A_share_T_plus_1_constraints_require_separate_executable_PnL_evaluation",
        "d1_open_terminal_close_return_is_gross_exploratory_path_without_cost_or_benchmark_adjustment",
        "separate_execution_outcomes_include_cost_adjusted_net_return_and_equal_weight_market_net_excess",
        "execution_capacity_uses_signal_day_amount_proxy_because_future_daily_bars_lack_amount",
        "research_does_not_modify_production_ranking_or_probability_models",
    ]


def _report_limitations(status: FutureRangeStatus, probability: Mapping[str, object]) -> list[str]:
    values = _base_limitations()
    if status == "insufficient_data":
        values.append("independent_sessions_or_fixed_target_coverage_below_research_gate")
    values.extend(str(item) for item in cast(Sequence[object], probability["limitations"]))
    return list(dict.fromkeys(values))


__all__ = [
    "FUTURE_RANGE_CENTER_PROXY",
    "FUTURE_RANGE_EVALUATION_SCHEMA_VERSION",
    "FUTURE_RANGE_METRICS",
    "FUTURE_RANGE_REPORT_CONTRACT_VERSION",
    "FUTURE_RANGE_RESEARCH_VERSION",
    "FUTURE_RANGE_SESSION_OFFSETS",
    "FUTURE_RANGE_TOP_SIZES",
    "FutureRangeConfig",
    "FutureRangeResearchError",
    "evaluate_market_scan_future_range",
]
