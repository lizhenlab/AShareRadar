from __future__ import annotations

from collections import Counter, defaultdict
from collections.abc import Iterable, Iterator, Sequence
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import date, datetime
from functools import lru_cache
import hashlib
import json
import math
from pathlib import Path
import random
import sqlite3
from statistics import fmean, median, pstdev
from typing import Literal, cast

from app.models.market import Kline, KlineAdjustmentMode
from app.models.paper_trading import (
    CostProfileName,
    PaperCostProfile,
    PaperInstrumentMetadata,
    PaperTradeRuleProfile,
)
from app.services.paper_trading_costs import resolve_cost_profile, trade_costs
from app.services.paper_trading_rules import assess_daily_tradeability, resolve_trade_rule_profile
from app.services.market_scan_shadow_scoring import (
    SHADOW_SCORE_VARIANTS,
    ShadowScoreBatch,
    ShadowScoreInput,
    ShadowScoreVariant,
    market_scan_shadow_score_spec,
    score_shadow_market,
    stable_shadow_spec_hash,
)
from app.utils.clock import utc_now


EVALUATION_SCHEMA_VERSION = "market-scan-forward-evaluation-v2"
DEFAULT_TOP_SIZES = (20, 50, 100)
DEFAULT_HORIZONS = (1, 3, 5, 10, 20)
DEFAULT_MINIMUM_SESSION_COUNT = 20
DEFAULT_BOOTSTRAP_SAMPLES = 1_000
DEFAULT_EXECUTION_NOTIONAL = 100_000.0
DEFAULT_MAX_EXIT_DELAY_SESSIONS = 5
DEFAULT_MAX_DAILY_PARTICIPATION_RATE = 0.01
EvaluationStatus = Literal["ok", "insufficient_data"]
ExecutionStatus = Literal["modelled", "unfilled", "data_unavailable"]


@dataclass(frozen=True)
class EvaluationConfig:
    top_sizes: tuple[int, ...] = DEFAULT_TOP_SIZES
    horizons: tuple[int, ...] = DEFAULT_HORIZONS
    minimum_sample_size: int = 30
    minimum_session_count: int = DEFAULT_MINIMUM_SESSION_COUNT
    complete_day_coverage: float = 0.95
    bootstrap_samples: int = DEFAULT_BOOTSTRAP_SAMPLES
    cost_profile: CostProfileName = "base"
    execution_notional: float = DEFAULT_EXECUTION_NOTIONAL
    max_exit_delay_sessions: int = DEFAULT_MAX_EXIT_DELAY_SESSIONS
    max_daily_participation_rate: float = DEFAULT_MAX_DAILY_PARTICIPATION_RATE

    def __post_init__(self) -> None:
        _require_positive_sequence(self.top_sizes, "top_sizes")
        _require_positive_sequence(self.horizons, "horizons")
        _require_positive(self.minimum_sample_size, "minimum_sample_size")
        _require_positive(self.minimum_session_count, "minimum_session_count")
        _require_unit_interval(self.complete_day_coverage, "complete_day_coverage")
        _require_minimum(self.bootstrap_samples, 100, "bootstrap_samples")
        _require_positive(self.execution_notional, "execution_notional")
        _require_minimum(self.max_exit_delay_sessions, 0, "max_exit_delay_sessions")
        _require_unit_interval(self.max_daily_participation_rate, "max_daily_participation_rate")


def _require_positive_sequence(values: Sequence[int], label: str) -> None:
    if not values or any(value <= 0 for value in values):
        raise ValueError(f"{label} 必须是正整数")


def _require_positive(value: float, label: str) -> None:
    if value <= 0:
        raise ValueError(f"{label} 必须大于 0")


def _require_unit_interval(value: float, label: str) -> None:
    if not 0 < value <= 1:
        raise ValueError(f"{label} 必须在 (0, 1] 范围内")


def _require_minimum(value: int, minimum: int, label: str) -> None:
    if value < minimum:
        raise ValueError(f"{label} 不能小于 {minimum}")


@dataclass(frozen=True)
class _ExecutionOutcome:
    status: ExecutionStatus
    reason: str
    gross_return: float | None = None
    net_return: float | None = None
    cost_drag: float | None = None
    entry_date: str | None = None
    exit_date: str | None = None
    exit_delay_sessions: int = 0
    model_limited: bool = False


@dataclass(frozen=True)
class _ExecutionEntry:
    by_date: dict[str, sqlite3.Row]
    metadata: PaperInstrumentMetadata
    entry_date: str
    entry_price: float
    quantity: int
    buy_amount: float
    buy_cost: float
    entry_model_limited: bool
    cost_profile: PaperCostProfile


@dataclass(frozen=True)
class _Observation:
    run_id: int
    quote_date: str
    mode: str
    scope: str
    rule_version: str
    symbol: str
    market: str
    board: str
    segment: str
    liquidity_bucket: str
    scan_time_bucket: str
    rank: int
    raw_score: float
    quality_bucket: str
    regime: str
    returns: dict[int, float]
    adverse: dict[int, float]
    execution: dict[int, _ExecutionOutcome]


@dataclass(frozen=True)
class _RunSnapshot:
    id: int
    mode: str
    scope: str
    rule_version: str
    quote_date: str
    data_date: str
    observations: tuple[_Observation, ...]
    rankings: tuple[tuple[str, int], ...]
    eligible_dates: tuple[str, ...]


def evaluate_market_scan_rankings(
    database_path: Path,
    *,
    config: EvaluationConfig | None = None,
    mode: Literal["official", "intraday"] | None = None,
    run_ids: Sequence[int] | None = None,
) -> dict[str, object]:
    settings = config or EvaluationConfig()
    path = Path(database_path).resolve()
    with _readonly_connection(path) as conn:
        runs = _published_runs(conn, mode=mode, run_ids=run_ids)
        snapshots = tuple(
            snapshot
            for run in runs
            if (snapshot := _evaluate_run(conn, run, settings)) is not None
        )
    return _build_report(path, runs, snapshots, settings)


def evaluate_market_scan_shadow_rankings(
    database_path: Path,
    *,
    variant: ShadowScoreVariant = "v5_full",
    config: EvaluationConfig | None = None,
    mode: Literal["official", "intraday"] | None = None,
    run_ids: Sequence[int] | None = None,
) -> dict[str, object]:
    """Evaluate a candidate ranking reconstructed without changing production rows."""
    settings = config or EvaluationConfig()
    path = Path(database_path).resolve()
    with _readonly_connection(path) as conn:
        production_runs = _published_runs(conn, mode=mode, run_ids=run_ids)
        runs = _deduplicate_shadow_sessions(production_runs)
        evaluated = tuple(
            value
            for run in runs
            if (value := _evaluate_shadow_run(conn, run, settings, variant)) is not None
        )
    snapshots = tuple(item[0] for item in evaluated)
    batches = tuple(item[1] for item in evaluated)
    report = _build_report(
        path,
        runs,
        snapshots,
        settings,
        ranking_source="reconstructed-read-only-shadow-score",
    )
    spec = market_scan_shadow_score_spec(variant=variant)
    report["shadow"] = {
        "variant": variant,
        "spec": spec,
        "spec_hash": stable_shadow_spec_hash(spec),
        "production_mutation": False,
        "run_evidence": [
            {
                "run_id": snapshot.id,
                "candidate_id": batch.candidate_id,
                "scored_count": len(batch.results),
                "normalization": batch.normalization,
                "ranking_digest": _ranking_digest(batch),
            }
            for snapshot, batch in zip(snapshots, batches, strict=True)
        ],
        "reconstruction_limit": (
            "使用冻结扫描报价/元数据与当前只读数据库中 date<=data_date 的前复权日K；"
            "不写回生产榜单，但较晚供应商修订的历史K线无法被识别为原始快照。"
        ),
    }
    return report


def evaluate_market_scan_shadow_comparison(
    database_path: Path,
    *,
    config: EvaluationConfig | None = None,
    mode: Literal["official", "intraday"] | None = None,
    run_ids: Sequence[int] | None = None,
    variants: Sequence[ShadowScoreVariant] = SHADOW_SCORE_VARIANTS,
) -> dict[str, object]:
    settings = config or EvaluationConfig()
    normalized_variants = tuple(dict.fromkeys(variants))
    if not normalized_variants:
        raise ValueError("至少需要一个影子评分版本")
    production = evaluate_market_scan_rankings(
        database_path,
        config=settings,
        mode=mode,
        run_ids=run_ids,
    )
    candidates = {
        variant: evaluate_market_scan_shadow_rankings(
            database_path,
            variant=variant,
            config=settings,
            mode=mode,
            run_ids=run_ids,
        )
        for variant in normalized_variants
    }
    observed_sessions = max(
        [_maximum_contract_session_count(production)]
        + [_maximum_contract_session_count(report) for report in candidates.values()]
    )
    promotable = (
        production["status"] == "ok"
        and all(report["status"] == "ok" for report in candidates.values())
        and observed_sessions >= settings.minimum_session_count
    )
    return {
        "schema_version": "market-scan-shadow-comparison-v1",
        "generated_at": utc_now().isoformat(timespec="seconds").replace("+00:00", "Z"),
        "status": "eligible_for_human_review" if promotable else "insufficient_data",
        "production": production,
        "candidates": candidates,
        "promotion": {
            "automatic_promotion": False,
            "eligible_for_human_review": promotable,
            "required_independent_session_count": settings.minimum_session_count,
            "observed_independent_session_count": observed_sessions,
            "conclusion": (
                "候选评分已实现并可持续积累影子证据，但暂不晋级生产。"
                if not promotable
                else "样本门槛已满足，仅可进入人工晋级评审；不得自动替换生产评分。"
            ),
        },
    }


def _maximum_contract_session_count(report: dict[str, object]) -> int:
    cohorts = cast(list[dict[str, object]], report["cohorts"])
    return max(
        (
            int(str(item["independent_session_count"]))
            for item in cohorts
            if len(cast(dict[str, str], item["dimensions"])) == 3
        ),
        default=0,
    )


def _build_report(
    path: Path,
    runs: Sequence[sqlite3.Row],
    snapshots: tuple[_RunSnapshot, ...],
    settings: EvaluationConfig,
    *,
    ranking_source: str = "persisted_market_scan_result",
) -> dict[str, object]:
    observations = tuple(item for snapshot in snapshots for item in snapshot.observations)
    cohorts = _cohort_metrics(observations, settings)
    monotonicity = _monotonicity_metrics(observations, settings)
    deciles = _decile_metrics(observations, settings)
    rank_ic = _rank_ic_metrics(observations, settings)
    stability = _stability_metrics(snapshots, settings)
    eligible_runs = sum(bool(snapshot.observations) for snapshot in snapshots)
    status: EvaluationStatus = "ok" if any(item["status"] == "ok" for item in cohorts) else "insufficient_data"
    return {
        "schema_version": EVALUATION_SCHEMA_VERSION,
        "generated_at": utc_now().isoformat(timespec="seconds").replace("+00:00", "Z"),
        "status": status,
        "config": _report_config(settings),
        "source": _report_source(path, runs, snapshots, observations, eligible_runs, ranking_source),
        "runs": [_run_summary(snapshot, settings) for snapshot in snapshots],
        "cohorts": cohorts,
        "monotonicity": monotonicity,
        "deciles": deciles,
        "rank_ic": rank_ic,
        "stability": stability,
        "limitations": _report_limitations(),
    }


def _report_config(settings: EvaluationConfig) -> dict[str, object]:
    return {
        "top_sizes": list(settings.top_sizes),
        "horizons": list(settings.horizons),
        "minimum_sample_size": settings.minimum_sample_size,
        "minimum_session_count": settings.minimum_session_count,
        "complete_day_coverage": settings.complete_day_coverage,
        "bootstrap_samples": settings.bootstrap_samples,
        "execution_notional": settings.execution_notional,
        "max_exit_delay_sessions": settings.max_exit_delay_sessions,
        "max_daily_participation_rate": settings.max_daily_participation_rate,
        "cost_profile": resolve_cost_profile(settings.cost_profile).model_dump(mode="json"),
    }


def _report_source(
    path: Path,
    runs: Sequence[sqlite3.Row],
    snapshots: Sequence[_RunSnapshot],
    observations: Sequence[_Observation],
    eligible_runs: int,
    ranking_source: str,
) -> dict[str, object]:
    return {
        "database": _portable_database_label(path),
        "published_run_count": len(runs),
        "eligible_run_count": eligible_runs,
        "independent_session_count": len({item.quote_date for item in snapshots if item.observations}),
        "observation_count": len(observations),
        "read_only": True,
        "ranking_source": ranking_source,
        "forward_price_source": "persisted_qfq_kline_daily",
        "execution_model": "next-complete-session-open,T+1,next-sellable-open",
    }


def _report_limitations() -> list[str]:
    return [
        "只读取已发布批次及之后实际持久化的完整交易日数据，不重算历史生产排名。",
        "收益、IC与单调性 cohort 按 mode、scope、rule_version 隔离，不跨规则版本汇总。",
        "同一 mode、scope、rule_version、quote_date 只保留最后发布的快照，避免重复扫描放大样本。",
        "充分性同时要求股票观察数和独立交易日数；同一天的多只股票不视为独立时间样本。",
        "置信区间先按扫描交易日聚合，再以交易日为区块进行确定性 bootstrap。",
        "净收益是固定名义本金、下一完整交易日开盘入场、T+1后目标日或下一可卖日开盘退出的日K情景。",
        "日K无法复原盘口排队与盘中先后顺序，model_limited 与 unfilled 状态必须保留。",
        "市场环境由扫描快照当日全市场涨跌幅均值分层，不使用未来信息。",
        "报告不会自动修改生产评分权重；规则调整必须创建新的 rule_version。",
    ]


def _portable_database_label(path: Path) -> str:
    try:
        return path.relative_to(Path.cwd().resolve()).as_posix()
    except ValueError:
        return str(path)


@contextmanager
def _readonly_connection(path: Path) -> Iterator[sqlite3.Connection]:
    conn = sqlite3.connect(f"file:{path.as_posix()}?mode=ro", uri=True)
    try:
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA query_only = ON")
        yield conn
    finally:
        conn.close()


def _published_runs(
    conn: sqlite3.Connection,
    *,
    mode: str | None,
    run_ids: Sequence[int] | None,
) -> list[sqlite3.Row]:
    clauses = ["status IN ('success', 'degraded')"]
    parameters: list[object] = []
    if mode is not None:
        clauses.append("mode = ?")
        parameters.append(mode)
    normalized_ids = tuple(dict.fromkeys(int(value) for value in run_ids or () if int(value) > 0))
    if run_ids is not None:
        if not normalized_ids:
            return []
        clauses.append(f"id IN ({','.join('?' for _value in normalized_ids)})")
        parameters.extend(normalized_ids)
    rows = conn.execute(
        f"""
        SELECT id, mode, scope, rule_version, quote_date, data_date, as_of
        FROM market_scan_run
        WHERE {' AND '.join(clauses)}
        ORDER BY data_date ASC, as_of ASC, id ASC
        """,
        parameters,
    ).fetchall()
    by_session: dict[tuple[str, str, str, str], sqlite3.Row] = {}
    for row in rows:
        key = (
            str(row["mode"] or "official"),
            str(row["scope"]),
            str(row["rule_version"]),
            str(row["quote_date"] or row["data_date"]),
        )
        by_session[key] = row
    return list(by_session.values())


def _deduplicate_shadow_sessions(runs: Sequence[sqlite3.Row]) -> list[sqlite3.Row]:
    by_session: dict[tuple[str, str, str], sqlite3.Row] = {}
    for run in runs:
        key = (
            str(run["mode"] or "official"),
            str(run["scope"]),
            str(run["quote_date"] or run["data_date"]),
        )
        by_session[key] = run
    return list(by_session.values())


def _evaluate_shadow_run(
    conn: sqlite3.Connection,
    run: sqlite3.Row,
    config: EvaluationConfig,
    variant: ShadowScoreVariant,
) -> tuple[_RunSnapshot, ShadowScoreBatch] | None:
    result_rows = _shadow_result_rows(conn, int(run["id"]))
    if not result_rows:
        return None
    data_date = str(run["data_date"])
    quote_date = str(run["quote_date"] or run["data_date"])
    history = _shadow_history_bars(conn, int(run["id"]), data_date)
    inputs = _shadow_score_inputs(result_rows, history, quote_date, data_date)
    if not inputs:
        return None
    batch = score_shadow_market(inputs, variant=variant)
    forward = _forward_bars(conn, int(run["id"]), data_date)
    eligible_dates = _eligible_trading_dates(forward, len(batch.results), quote_date, config)
    snapshot = _shadow_snapshot(
        run,
        result_rows,
        batch,
        forward,
        eligible_dates,
        quote_date,
        data_date,
        config,
    )
    return snapshot, batch


def _shadow_result_rows(conn: sqlite3.Connection, run_id: int) -> list[sqlite3.Row]:
    return conn.execute(
        """
        SELECT symbol, market, rank, score, raw_score, price, change_pct, data_quality_score,
               amount, turnover_rate, volume_ratio, list_date, is_st, is_new,
               quote_fallback_used, kline_fallback_used, metadata_degraded,
               COALESCE(NULLIF(adjustment_mode, ''), 'qfq') AS adjustment_mode
        FROM market_scan_result
        WHERE run_id = ? AND status = 'success' AND rank IS NOT NULL AND price > 0
        ORDER BY rank ASC, symbol ASC
        """,
        (run_id,),
    ).fetchall()


def _shadow_score_inputs(
    result_rows: Sequence[sqlite3.Row],
    history: dict[str, tuple[Kline, ...]],
    quote_date: str,
    data_date: str,
) -> list[ShadowScoreInput]:
    inputs: list[ShadowScoreInput] = []
    for row in result_rows:
        symbol = str(row["symbol"])
        rows = history.get(symbol, ())
        if len(rows) < 60:
            continue
        inputs.append(_shadow_score_input(row, rows, quote_date, data_date))
    return inputs


def _shadow_score_input(
    row: sqlite3.Row,
    rows: tuple[Kline, ...],
    quote_date: str,
    data_date: str,
) -> ShadowScoreInput:
    return ShadowScoreInput(
        symbol=str(row["symbol"]), market=str(row["market"]), quote_date=quote_date,
        data_date=data_date, price=float(row["price"]), change_pct=float(row["change_pct"] or 0),
        turnover_rate=float(row["turnover_rate"]) if row["turnover_rate"] is not None else None,
        amount=float(row["amount"] or 0), volume_ratio=float(row["volume_ratio"] or 1),
        data_quality_score=int(row["data_quality_score"] or 0), rows=rows,
        list_date=str(row["list_date"]) if row["list_date"] else None,
        is_st=bool(row["is_st"]), is_new=bool(row["is_new"]),
        quote_fallback_used=bool(row["quote_fallback_used"]),
        kline_fallback_used=bool(row["kline_fallback_used"]), metadata_degraded=bool(row["metadata_degraded"]),
    )


def _shadow_snapshot(
    run: sqlite3.Row,
    result_rows: Sequence[sqlite3.Row],
    batch: ShadowScoreBatch,
    forward: dict[str, tuple[sqlite3.Row, ...]],
    eligible_dates: tuple[str, ...],
    quote_date: str,
    data_date: str,
    config: EvaluationConfig,
) -> _RunSnapshot:
    shadow_run = _shadow_run_proxy(run, batch.candidate_id, quote_date, data_date)
    rows_by_symbol = {str(row["symbol"]): row for row in result_rows}
    observations = _shadow_observations(
        shadow_run, rows_by_symbol, batch, forward, eligible_dates, _market_regime(result_rows), config,
    )
    return _RunSnapshot(
        id=int(run["id"]),
        mode=str(run["mode"] or "official"),
        scope=str(run["scope"]),
        rule_version=batch.candidate_id,
        quote_date=quote_date,
        data_date=data_date,
        observations=observations,
        rankings=tuple((item.symbol, item.rank) for item in batch.results),
        eligible_dates=eligible_dates,
    )


def _shadow_run_proxy(
    run: sqlite3.Row,
    candidate_id: str,
    quote_date: str,
    data_date: str,
) -> sqlite3.Row:
    value = {
        "id": run["id"], "mode": run["mode"], "scope": run["scope"],
        "rule_version": candidate_id, "quote_date": quote_date, "data_date": data_date,
        "as_of": run["as_of"],
    }
    return cast(sqlite3.Row, value)


def _shadow_observations(
    shadow_run: sqlite3.Row,
    rows_by_symbol: dict[str, sqlite3.Row],
    batch: ShadowScoreBatch,
    forward: dict[str, tuple[sqlite3.Row, ...]],
    eligible_dates: tuple[str, ...],
    regime: str,
    config: EvaluationConfig,
) -> tuple[_Observation, ...]:
    observations: list[_Observation] = []
    for scored in batch.results:
        persisted = rows_by_symbol[scored.symbol]
        proxy = {key: persisted[key] for key in persisted.keys()}
        proxy.update(rank=scored.rank, raw_score=scored.raw_score)
        observation = _observation_from_rows(
            shadow_run, cast(sqlite3.Row, proxy), forward.get(scored.symbol, ()),
            eligible_dates, regime, config,
        )
        if observation is not None:
            observations.append(observation)
    return tuple(observations)


def _shadow_history_bars(
    conn: sqlite3.Connection,
    run_id: int,
    data_date: str,
) -> dict[str, tuple[Kline, ...]]:
    rows = conn.execute(
        """
        SELECT k.symbol, k.date, k.open, k.close, k.high, k.low, k.volume,
               k.adjustment_mode, k.as_of, k.data_version, k.contract_version,
               k.source, k.fetched_at, k.fallback_used
        FROM kline_daily AS k
        JOIN market_scan_result AS r
          ON r.run_id = ? AND r.symbol = k.symbol AND r.status = 'success'
        WHERE k.date <= ? AND k.adjustment_mode = 'qfq'
        ORDER BY k.symbol ASC, k.date ASC
        """,
        (run_id, data_date),
    ).fetchall()
    grouped: dict[str, list[Kline]] = defaultdict(list)
    for row in rows:
        grouped[str(row["symbol"])].append(_to_kline(row))
    return {symbol: tuple(items) for symbol, items in grouped.items()}


def _ranking_digest(batch: ShadowScoreBatch) -> str:
    payload = [[item.symbol, item.rank, item.raw_score] for item in batch.results]
    return hashlib.sha256(json.dumps(payload, separators=(",", ":"), allow_nan=False).encode("utf-8")).hexdigest()


def _evaluate_run(
    conn: sqlite3.Connection,
    run: sqlite3.Row,
    config: EvaluationConfig,
) -> _RunSnapshot | None:
    result_rows = conn.execute(
        """
        SELECT symbol, market, rank, score, raw_score, price, change_pct, data_quality_score,
               amount, turnover_rate, list_date, is_st, is_new,
               COALESCE(NULLIF(adjustment_mode, ''), 'qfq') AS adjustment_mode
        FROM market_scan_result
        WHERE run_id = ? AND status = 'success' AND rank IS NOT NULL AND price > 0
        ORDER BY rank ASC, symbol ASC
        """,
        (run["id"],),
    ).fetchall()
    if not result_rows:
        return None
    quote_date = str(run["quote_date"] or run["data_date"])
    data_date = str(run["data_date"])
    bars = _forward_bars(conn, int(run["id"]), data_date)
    eligible_dates = _eligible_trading_dates(
        bars,
        len(result_rows),
        quote_date,
        config,
    )
    regime = _market_regime(result_rows)
    observations = tuple(
        observation
        for row in result_rows
        if (
            observation := _observation_from_rows(
                run,
                row,
                bars.get(str(row["symbol"]), ()),
                eligible_dates,
                regime,
                config,
            )
        ) is not None
    )
    return _RunSnapshot(
        id=int(run["id"]),
        mode=str(run["mode"] or "official"),
        scope=str(run["scope"]),
        rule_version=str(run["rule_version"]),
        quote_date=quote_date,
        data_date=data_date,
        observations=observations,
        rankings=tuple((str(row["symbol"]), int(row["rank"])) for row in result_rows),
        eligible_dates=eligible_dates,
    )


def _forward_bars(
    conn: sqlite3.Connection,
    run_id: int,
    data_date: str,
) -> dict[str, tuple[sqlite3.Row, ...]]:
    rows = conn.execute(
        """
        SELECT k.symbol, k.date, k.open, k.close, k.high, k.low, k.volume,
               k.adjustment_mode, k.as_of, k.data_version, k.contract_version,
               k.source, k.fetched_at, k.fallback_used
        FROM kline_daily AS k
        JOIN market_scan_result AS r
          ON r.run_id = ? AND r.symbol = k.symbol AND r.status = 'success'
        WHERE k.date >= ?
          AND k.adjustment_mode = CASE
              WHEN r.adjustment_mode IN ('qfq', 'hfq', 'none') THEN r.adjustment_mode
              ELSE 'qfq'
          END
        ORDER BY k.date ASC, k.symbol ASC
        """,
        (run_id, data_date),
    ).fetchall()
    grouped: dict[str, list[sqlite3.Row]] = defaultdict(list)
    for row in rows:
        grouped[str(row["symbol"])].append(row)
    return {symbol: tuple(items) for symbol, items in grouped.items()}


def _eligible_trading_dates(
    bars: dict[str, tuple[sqlite3.Row, ...]],
    snapshot_count: int,
    quote_date: str,
    config: EvaluationConfig,
) -> tuple[str, ...]:
    counts: dict[str, int] = defaultdict(int)
    for symbol_rows in bars.values():
        for row_date in {str(row["date"]) for row in symbol_rows if str(row["date"]) > quote_date}:
            counts[row_date] += 1
    required = max(1, math.ceil(snapshot_count * config.complete_day_coverage))
    limit = max(config.horizons) + config.max_exit_delay_sessions + 1
    return tuple(sorted(row_date for row_date, count in counts.items() if count >= required))[:limit]


def _observation_from_rows(
    run: sqlite3.Row,
    result: sqlite3.Row,
    bars: tuple[sqlite3.Row, ...],
    eligible_dates: tuple[str, ...],
    regime: str,
    config: EvaluationConfig,
) -> _Observation | None:
    entry = float(result["price"])
    returns, adverse = _forward_performance(bars, eligible_dates, entry, config.horizons)
    if not returns:
        return None
    quote_date = str(run["quote_date"] or run["data_date"])
    symbol = str(result["symbol"])
    market = str(result["market"])
    is_st = bool(result["is_st"])
    is_new = bool(result["is_new"])
    return _Observation(
        run_id=int(run["id"]),
        quote_date=quote_date,
        mode=str(run["mode"] or "official"),
        scope=str(run["scope"]),
        rule_version=str(run["rule_version"]),
        symbol=symbol,
        market=market,
        board=_board(symbol, market),
        segment="st" if is_st else "new" if is_new else "regular",
        liquidity_bucket=_liquidity_bucket(result["amount"]),
        scan_time_bucket=_scan_time_bucket(run["as_of"], str(run["mode"] or "official")),
        rank=int(result["rank"]),
        raw_score=_result_raw_score(result),
        quality_bucket=_quality_bucket(result["data_quality_score"]),
        regime=regime,
        returns=returns,
        adverse=adverse,
        execution=_execution_outcomes(
            symbol=symbol,
            market=market,
            list_date=result["list_date"],
            is_st=is_st,
            is_new=is_new,
            quote_date=quote_date,
            amount=float(result["amount"] or 0),
            bars=bars,
            eligible_dates=eligible_dates,
            config=config,
        ),
    )


def _forward_performance(
    bars: Sequence[sqlite3.Row],
    eligible_dates: Sequence[str],
    entry: float,
    horizons: Sequence[int],
) -> tuple[dict[int, float], dict[int, float]]:
    by_date = {str(row["date"]): row for row in bars}
    returns: dict[int, float] = {}
    adverse: dict[int, float] = {}
    lows: list[float] = []
    for index, row_date in enumerate(eligible_dates, start=1):
        row = by_date.get(row_date)
        if row is None:
            continue
        lows.append(float(row["low"]))
        if index in horizons:
            returns[index] = float(row["close"]) / entry - 1
            adverse[index] = min(lows) / entry - 1
    return returns, adverse


def _result_raw_score(result: sqlite3.Row) -> float:
    if result["raw_score"] is not None:
        return float(result["raw_score"])
    if result["score"] is not None:
        return float(result["score"])
    return -float(result["rank"])


def _execution_outcomes(
    *,
    symbol: str,
    market: str,
    list_date: object,
    is_st: bool,
    is_new: bool,
    quote_date: str,
    amount: float,
    bars: tuple[sqlite3.Row, ...],
    eligible_dates: tuple[str, ...],
    config: EvaluationConfig,
) -> dict[int, _ExecutionOutcome]:
    if not eligible_dates:
        return {}
    prepared = _prepare_execution_entry(
        symbol=symbol, market=market, list_date=list_date, is_st=is_st, is_new=is_new,
        quote_date=quote_date, amount=amount, bars=bars, eligible_dates=eligible_dates, config=config,
    )
    if isinstance(prepared, _ExecutionOutcome):
        return _repeat_outcome(config.horizons, prepared)
    outcomes: dict[int, _ExecutionOutcome] = {}
    for horizon in config.horizons:
        if horizon < len(eligible_dates):
            outcomes[horizon] = _execution_horizon(
                prepared, horizon, symbol, market, is_st, is_new, bars, eligible_dates, config,
            )
    return outcomes


def _prepare_execution_entry(
    *,
    symbol: str,
    market: str,
    list_date: object,
    is_st: bool,
    is_new: bool,
    quote_date: str,
    amount: float,
    bars: tuple[sqlite3.Row, ...],
    eligible_dates: tuple[str, ...],
    config: EvaluationConfig,
) -> _ExecutionEntry | _ExecutionOutcome:
    by_date = {str(row["date"]): row for row in bars}
    entry_date = eligible_dates[0]
    entry_row = by_date.get(entry_date)
    previous = _previous_row(bars, entry_date)
    if entry_row is None or previous is None:
        return _ExecutionOutcome("data_unavailable", "entry_or_previous_bar_missing")
    if amount <= 0 or config.execution_notional / amount > config.max_daily_participation_rate:
        return _ExecutionOutcome("unfilled", "daily_capacity_limit", entry_date=entry_date)
    metadata = _execution_metadata(symbol, market, list_date, is_st, quote_date)
    entry_bar = _to_kline(entry_row)
    try:
        entry_profile = _evaluation_trade_profile(
            symbol,
            market,
            date.fromisoformat(entry_date),
            metadata,
            is_st=is_st,
            is_new=is_new,
        )
        entry_tradeability = assess_daily_tradeability(
            entry_bar,
            previous_close=float(previous["close"]),
            profile=entry_profile,
        )
    except (KeyError, ValueError):
        return _ExecutionOutcome("data_unavailable", "entry_rule_unavailable", entry_date=entry_date)
    if not entry_tradeability.can_buy:
        return _ExecutionOutcome("unfilled", entry_tradeability.code, entry_date=entry_date)
    entry_price = float(entry_row["open"])
    quantity = _model_quantity(config.execution_notional, entry_price, entry_profile.min_buy_quantity, entry_profile.buy_quantity_step)
    if quantity <= 0:
        return _ExecutionOutcome("unfilled", "minimum_quantity_unaffordable", entry_date=entry_date)
    cost_profile = resolve_cost_profile(config.cost_profile)
    buy_amount = entry_price * quantity
    buy_cost = trade_costs(cost_profile, side="buy", gross_amount=buy_amount).total
    return _ExecutionEntry(
        by_date=by_date, metadata=metadata, entry_date=entry_date, entry_price=entry_price,
        quantity=quantity, buy_amount=buy_amount, buy_cost=buy_cost,
        entry_model_limited=entry_tradeability.model_limited or entry_profile.quality != "ok",
        cost_profile=cost_profile,
    )


def _execution_metadata(
    symbol: str,
    market: str,
    list_date: object,
    is_st: bool,
    quote_date: str,
) -> PaperInstrumentMetadata:
    return PaperInstrumentMetadata(
        symbol=symbol,
        market=market,
        list_date=str(list_date) if list_date else None,
        is_st=is_st,
        status_effective_date=quote_date,
        source="market_scan_result",
    )


def _repeat_outcome(horizons: Sequence[int], outcome: _ExecutionOutcome) -> dict[int, _ExecutionOutcome]:
    return {horizon: outcome for horizon in horizons}


def _execution_horizon(
    entry: _ExecutionEntry,
    horizon: int,
    symbol: str,
    market: str,
    is_st: bool,
    is_new: bool,
    bars: Sequence[sqlite3.Row],
    eligible_dates: Sequence[str],
    config: EvaluationConfig,
) -> _ExecutionOutcome:
    for delay in range(config.max_exit_delay_sessions + 1):
        exit_index = horizon + delay
        if exit_index >= len(eligible_dates):
            break
        exit_date = eligible_dates[exit_index]
        sellable = _sellable_exit(entry, symbol, market, is_st, is_new, bars, exit_date)
        if sellable is None:
            continue
        exit_price, exit_model_limited = sellable
        return _modelled_execution(entry, exit_date, exit_price, delay, exit_model_limited)
    return _ExecutionOutcome(
        "unfilled", "exit_not_sellable_within_delay", entry_date=entry.entry_date,
        exit_delay_sessions=config.max_exit_delay_sessions,
    )


def _sellable_exit(
    entry: _ExecutionEntry,
    symbol: str,
    market: str,
    is_st: bool,
    is_new: bool,
    bars: Sequence[sqlite3.Row],
    exit_date: str,
) -> tuple[float, bool] | None:
    exit_row = entry.by_date.get(exit_date)
    previous = _previous_row(bars, exit_date)
    if exit_row is None or previous is None:
        return None
    try:
        profile = _evaluation_trade_profile(
            symbol, market, date.fromisoformat(exit_date), entry.metadata, is_st=is_st, is_new=is_new,
        )
        tradeability = assess_daily_tradeability(
            _to_kline(exit_row), previous_close=float(previous["close"]), profile=profile,
        )
    except (KeyError, ValueError):
        return None
    if not tradeability.can_sell:
        return None
    model_limited = tradeability.model_limited or profile.quality != "ok"
    return float(exit_row["open"]), model_limited


def _modelled_execution(
    entry: _ExecutionEntry,
    exit_date: str,
    exit_price: float,
    delay: int,
    exit_model_limited: bool,
) -> _ExecutionOutcome:
    sell_amount = exit_price * entry.quantity
    sell_cost = trade_costs(entry.cost_profile, side="sell", gross_amount=sell_amount).total
    gross_return = exit_price / entry.entry_price - 1
    net_return = (sell_amount - sell_cost - entry.buy_amount - entry.buy_cost) / (entry.buy_amount + entry.buy_cost)
    return _ExecutionOutcome(
        status="modelled", reason="exit_delayed" if delay else "next_open_t1",
        gross_return=gross_return, net_return=net_return, cost_drag=gross_return - net_return,
        entry_date=entry.entry_date, exit_date=exit_date, exit_delay_sessions=delay,
        model_limited=entry.entry_model_limited or exit_model_limited,
    )


def _previous_row(rows: Sequence[sqlite3.Row], row_date: str) -> sqlite3.Row | None:
    candidates = [row for row in rows if str(row["date"]) < row_date]
    return candidates[-1] if candidates else None


def _to_kline(row: sqlite3.Row) -> Kline:
    return Kline(
        date=str(row["date"]),
        open=float(row["open"]),
        close=float(row["close"]),
        high=float(row["high"]),
        low=float(row["low"]),
        volume=float(row["volume"]),
        adjustment_mode=cast(KlineAdjustmentMode, str(row["adjustment_mode"])),
        as_of=row["as_of"],
        data_version=str(row["data_version"] or "unknown"),
        contract_version=str(row["contract_version"] or "daily-kline.v1"),
        source=row["source"],
        fetched_at=row["fetched_at"],
        fallback_used=bool(row["fallback_used"]),
    )


def _model_quantity(notional: float, price: float, minimum: int, step: int) -> int:
    if price <= 0 or step <= 0:
        return 0
    affordable = math.floor(notional / price)
    quantity = (affordable // step) * step
    return quantity if quantity >= minimum else 0


def _evaluation_trade_profile(
    symbol: str,
    market: str,
    trade_date: date,
    metadata: PaperInstrumentMetadata,
    *,
    is_st: bool,
    is_new: bool,
) -> PaperTradeRuleProfile:
    if is_st or is_new:
        return resolve_trade_rule_profile(symbol, trade_date, metadata)
    return _standard_evaluation_trade_profile(_board(symbol, market), trade_date.isoformat())


@lru_cache(maxsize=256)
def _standard_evaluation_trade_profile(board: str, trade_date: str) -> PaperTradeRuleProfile:
    canonical = {
        "SH_MAIN": ("600001.SH", "SH"),
        "STAR": ("688001.SH", "SH"),
        "SZ_MAIN": ("000001.SZ", "SZ"),
        "CHINEXT": ("300001.SZ", "SZ"),
        "BSE": ("920001.BJ", "BJ"),
    }[board]
    symbol, market = canonical
    metadata = PaperInstrumentMetadata(
        symbol=symbol,
        market=market,
        list_date="2022-01-04",
        is_st=False,
        status_effective_date=trade_date,
        source="market-scan-evaluation-standard-profile",
    )
    return resolve_trade_rule_profile(symbol, date.fromisoformat(trade_date), metadata)


def _cohort_metrics(
    observations: tuple[_Observation, ...],
    config: EvaluationConfig,
) -> list[dict[str, object]]:
    metrics: list[dict[str, object]] = []
    for dimensions, rows in _cohort_slices(observations):
        metrics.extend(_cohort_slice_metrics(dimensions, rows, config))
    return metrics


def _cohort_slices(
    observations: tuple[_Observation, ...],
) -> list[tuple[dict[str, str], tuple[_Observation, ...]]]:
    cohorts: list[tuple[dict[str, str], tuple[_Observation, ...]]] = []
    contracts = sorted({(item.mode, item.scope, item.rule_version) for item in observations})
    for mode, scope, rule_version in contracts:
        contract_rows = tuple(
            item
            for item in observations
            if (item.mode, item.scope, item.rule_version) == (mode, scope, rule_version)
        )
        contract = {"mode": mode, "scope": scope, "rule_version": rule_version}
        cohorts.extend(_contract_cohort_slices(contract, contract_rows))
    return cohorts


def _contract_cohort_slices(
    contract: dict[str, str],
    rows: tuple[_Observation, ...],
) -> list[tuple[dict[str, str], tuple[_Observation, ...]]]:
    cohorts = [(contract, rows)]
    dimensions = {
        "market": ("SH", "SZ", "BJ"),
        "board": ("SH_MAIN", "STAR", "SZ_MAIN", "CHINEXT", "BSE"),
        "regime": ("strong", "neutral", "weak"),
        "quality": ("unknown", "low", "medium", "high"),
        "segment": ("regular", "st", "new"),
        "liquidity": ("low", "medium", "high"),
        "scan_time": ("morning", "afternoon", "after_close", "unknown"),
    }
    attributes = {
        "market": "market",
        "board": "board",
        "regime": "regime",
        "quality": "quality_bucket",
        "segment": "segment",
        "liquidity": "liquidity_bucket",
        "scan_time": "scan_time_bucket",
    }
    for dimension, values in dimensions.items():
        attribute = attributes[dimension]
        for value in values:
            cohorts.append(
                (
                    {**contract, dimension: value},
                    tuple(item for item in rows if getattr(item, attribute) == value),
                )
            )
    return cohorts


def _cohort_slice_metrics(
    dimensions: dict[str, str],
    rows: tuple[_Observation, ...],
    config: EvaluationConfig,
) -> list[dict[str, object]]:
    metrics: list[dict[str, object]] = []
    for top_n in config.top_sizes:
        selected = tuple(item for item in rows if item.rank <= top_n)
        for horizon in config.horizons:
            metrics.append(_metric_record(dimensions, selected, rows, top_n, horizon, config))
    return metrics


def _metric_record(
    dimensions: dict[str, str],
    selected: tuple[_Observation, ...],
    benchmark_rows: tuple[_Observation, ...],
    top_n: int,
    horizon: int,
    config: EvaluationConfig,
) -> dict[str, object]:
    values = [(item, item.returns[horizon]) for item in selected if horizon in item.returns]
    grouped = _returns_by_run(values)
    independent_sessions = len(grouped)
    enough_samples = len(values) >= config.minimum_sample_size
    enough_sessions = independent_sessions >= config.minimum_session_count
    status: EvaluationStatus = "ok" if enough_samples and enough_sessions else "insufficient_data"
    record: dict[str, object] = {
        "dimensions": dimensions,
        "top_n": top_n,
        "horizon_trading_days": horizon,
        "status": status,
        "sample_size": len(values),
        "independent_session_count": independent_sessions,
        "insufficient_reasons": _insufficient_reasons(enough_samples, enough_sessions),
    }
    if not values:
        record["execution"] = _execution_summary(selected, horizon)
        return record
    record.update(_return_statistics(dimensions, selected, benchmark_rows, values, top_n, horizon, config))
    return record


def _return_statistics(
    dimensions: dict[str, str],
    selected: tuple[_Observation, ...],
    benchmark_rows: tuple[_Observation, ...],
    values: list[tuple[_Observation, float]],
    top_n: int,
    horizon: int,
    config: EvaluationConfig,
) -> dict[str, object]:
    grouped = _returns_by_run(values)
    returns = [value for _item, value in values]
    adverse = [item.adverse[horizon] for item, _value in values if horizon in item.adverse]
    benchmark_by_run = _benchmark_returns(benchmark_rows, horizon)
    daily_returns = {run_id: fmean(items) for run_id, items in grouped.items() if items}
    daily_excess = {
        run_id: value - benchmark_by_run[run_id]
        for run_id, value in daily_returns.items()
        if run_id in benchmark_by_run
    }
    daily_values = list(daily_returns.values())
    daily_excess_values = list(daily_excess.values())
    seed = json.dumps({"dimensions": dimensions, "top_n": top_n, "horizon": horizon}, sort_keys=True)
    return {
        "average_return": fmean(returns),
        "median_return": median(returns),
        "positive_return_rate": sum(value > 0 for value in returns) / len(returns),
        "session_average_return": fmean(daily_values),
        "session_median_return": median(daily_values),
        "session_positive_rate": sum(value > 0 for value in daily_values) / len(daily_values),
        "session_return_confidence_interval_95": _cluster_bootstrap_ci(
            daily_values, seed + ":return", config.bootstrap_samples,
        ),
        "equal_weight_market_return": fmean(benchmark_by_run.values()) if benchmark_by_run else None,
        "equal_weight_market_excess_return": fmean(daily_excess_values) if daily_excess_values else None,
        "session_excess_confidence_interval_95": _cluster_bootstrap_ci(
            daily_excess_values, seed + ":excess", config.bootstrap_samples,
        ),
        "session_maximum_drawdown": _compounded_maximum_drawdown(daily_values),
        "maximum_adverse_excursion": min(adverse) if adverse else None,
        "execution": _execution_summary(selected, horizon),
    }


def _compounded_maximum_drawdown(returns: Sequence[float]) -> float | None:
    if not returns:
        return None
    wealth = 1.0
    peak = 1.0
    worst = 0.0
    for value in returns:
        wealth *= 1 + value
        peak = max(peak, wealth)
        worst = min(worst, wealth / peak - 1)
    return worst


def _execution_summary(rows: Iterable[_Observation], horizon: int) -> dict[str, object]:
    materialized = tuple(rows)
    outcomes = [item.execution[horizon] for item in materialized if horizon in item.execution]
    statuses = Counter(item.status for item in outcomes)
    modelled = [item for item in outcomes if item.status == "modelled" and item.net_return is not None]
    by_run, cost_drag, delayed, model_limited = _execution_aggregates(materialized, horizon)
    daily_net = [fmean(values) for values in by_run.values() if values]
    return {
        "status_counts": dict(sorted(statuses.items())),
        "modelled_sample_size": len(modelled),
        "independent_session_count": len(daily_net),
        "average_net_return": fmean(daily_net) if daily_net else None,
        "median_net_return": median(daily_net) if daily_net else None,
        "average_cost_drag": fmean(cost_drag) if cost_drag else None,
        "delayed_exit_count": delayed,
        "model_limited_count": model_limited,
    }


def _execution_aggregates(
    rows: Sequence[_Observation],
    horizon: int,
) -> tuple[dict[int, list[float]], list[float], int, int]:
    by_run: dict[int, list[float]] = defaultdict(list)
    cost_drag: list[float] = []
    delayed = 0
    model_limited = 0
    for observation in rows:
        outcome = observation.execution.get(horizon)
        if outcome is None:
            continue
        if outcome.status == "modelled" and outcome.net_return is not None:
            by_run[observation.run_id].append(outcome.net_return)
        if outcome.cost_drag is not None:
            cost_drag.append(outcome.cost_drag)
        delayed += outcome.exit_delay_sessions > 0
        model_limited += outcome.model_limited
    return by_run, cost_drag, delayed, model_limited


def _returns_by_run(values: Iterable[tuple[_Observation, float]]) -> dict[int, list[float]]:
    grouped: dict[int, list[float]] = defaultdict(list)
    for item, value in values:
        grouped[item.run_id].append(value)
    return grouped


def _insufficient_reasons(enough_samples: bool, enough_sessions: bool) -> list[str]:
    reasons: list[str] = []
    if not enough_samples:
        reasons.append("minimum_sample_size")
    if not enough_sessions:
        reasons.append("minimum_session_count")
    return reasons


def _benchmark_returns(rows: Iterable[_Observation], horizon: int) -> dict[int, float]:
    grouped: dict[int, list[float]] = defaultdict(list)
    for item in rows:
        if horizon in item.returns:
            grouped[item.run_id].append(item.returns[horizon])
    return {run_id: fmean(values) for run_id, values in grouped.items() if values}


def _cluster_bootstrap_ci(values: Sequence[float], seed_text: str, samples: int) -> list[float] | None:
    if not values:
        return None
    if len(values) == 1:
        return [values[0], values[0]]
    seed = int.from_bytes(hashlib.sha256(seed_text.encode("utf-8")).digest()[:8], "big")
    generator = random.Random(seed)
    means = sorted(
        fmean(values[generator.randrange(len(values))] for _index in range(len(values)))
        for _sample in range(samples)
    )
    return [_percentile(means, 0.025), _percentile(means, 0.975)]


def _monotonicity_metrics(
    observations: tuple[_Observation, ...],
    config: EvaluationConfig,
) -> list[dict[str, object]]:
    records: list[dict[str, object]] = []
    for mode, scope, rule_version, rows in _contract_rows(observations):
        records.extend(
            _monotonicity_record(mode, scope, rule_version, rows, horizon, config)
            for horizon in config.horizons
        )
    return records


def _monotonicity_record(
    mode: str,
    scope: str,
    rule_version: str,
    rows: tuple[_Observation, ...],
    horizon: int,
    config: EvaluationConfig,
) -> dict[str, object]:
    summaries = [
        _band_return_summary("1-20", rows, horizon, 1, 20),
        _band_return_summary("21-50", rows, horizon, 21, 50),
        _band_return_summary("51-100", rows, horizon, 51, 100),
    ]
    enough = all(
        cast(int, item["sample_size"]) >= config.minimum_sample_size
        and cast(int, item["independent_session_count"]) >= config.minimum_session_count
        for item in summaries
    )
    monotonic = _band_means_descend(summaries) if enough else None
    return {
        "mode": mode,
        "scope": scope,
        "rule_version": rule_version,
        "horizon_trading_days": horizon,
        "status": "ok" if enough else "insufficient_data",
        "monotonic": monotonic,
        "bands": summaries,
    }


def _band_return_summary(
    label: str,
    rows: tuple[_Observation, ...],
    horizon: int,
    minimum_rank: int,
    maximum_rank: int,
) -> dict[str, object]:
    selected = [
        item
        for item in rows
        if minimum_rank <= item.rank <= maximum_rank and horizon in item.returns
    ]
    values = [item.returns[horizon] for item in selected]
    by_run = _returns_by_run((item, item.returns[horizon]) for item in selected)
    daily = [fmean(items) for items in by_run.values() if items]
    return {
        "band": label,
        "sample_size": len(values),
        "independent_session_count": len(daily),
        "average_return": fmean(daily) if daily else None,
    }


def _band_means_descend(summaries: list[dict[str, object]]) -> bool:
    means = [cast(float, item["average_return"]) for item in summaries]
    return all(left >= right for left, right in zip(means, means[1:], strict=False))


def _decile_metrics(
    observations: tuple[_Observation, ...],
    config: EvaluationConfig,
) -> list[dict[str, object]]:
    records: list[dict[str, object]] = []
    for mode, scope, rule_version, rows in _contract_rows(observations):
        run_sizes = Counter(item.run_id for item in rows)
        for horizon in config.horizons:
            records.append(_decile_record(mode, scope, rule_version, rows, run_sizes, horizon, config))
    return records


def _decile_record(
    mode: str,
    scope: str,
    rule_version: str,
    rows: tuple[_Observation, ...],
    run_sizes: Counter[int],
    horizon: int,
    config: EvaluationConfig,
) -> dict[str, object]:
    bands = [_decile_band(rows, run_sizes, horizon, decile) for decile in range(1, 11)]
    enough = all(_decile_band_sufficient(item, config) for item in bands)
    values = [cast(float, item["average_return"]) for item in bands] if enough else []
    return {
        "mode": mode,
        "scope": scope,
        "rule_version": rule_version,
        "horizon_trading_days": horizon,
        "status": "ok" if enough else "insufficient_data",
        "monotonic": _descending(values) if enough else None,
        "bands": bands,
    }


def _decile_band(
    rows: Sequence[_Observation],
    run_sizes: Counter[int],
    horizon: int,
    decile: int,
) -> dict[str, object]:
    selected = [
        item
        for item in rows
        if horizon in item.returns
        and min(10, math.ceil(item.rank / max(1, run_sizes[item.run_id]) * 10)) == decile
    ]
    by_run = _returns_by_run((item, item.returns[horizon]) for item in selected)
    daily = [fmean(values) for values in by_run.values() if values]
    return {
        "decile": decile,
        "sample_size": len(selected),
        "independent_session_count": len(daily),
        "average_return": fmean(daily) if daily else None,
    }


def _decile_band_sufficient(item: dict[str, object], config: EvaluationConfig) -> bool:
    return (
        cast(int, item["sample_size"]) >= config.minimum_sample_size
        and cast(int, item["independent_session_count"]) >= config.minimum_session_count
    )


def _descending(values: Sequence[float]) -> bool:
    return all(left >= right for left, right in zip(values, values[1:], strict=False))


def _rank_ic_metrics(
    observations: tuple[_Observation, ...],
    config: EvaluationConfig,
) -> list[dict[str, object]]:
    records: list[dict[str, object]] = []
    for mode, scope, rule_version, rows in _contract_rows(observations):
        for horizon in config.horizons:
            grouped: dict[int, list[_Observation]] = defaultdict(list)
            for item in rows:
                if horizon in item.returns:
                    grouped[item.run_id].append(item)
            daily_ic = [
                value
                for run_rows in grouped.values()
                if (value := _spearman([(item.raw_score, item.returns[horizon]) for item in run_rows])) is not None
            ]
            enough = len(daily_ic) >= config.minimum_session_count
            mean_ic = fmean(daily_ic) if daily_ic else None
            deviation = pstdev(daily_ic) if len(daily_ic) >= 2 else None
            records.append(
                {
                    "mode": mode,
                    "scope": scope,
                    "rule_version": rule_version,
                    "horizon_trading_days": horizon,
                    "status": "ok" if enough else "insufficient_data",
                    "independent_session_count": len(daily_ic),
                    "mean_rank_ic": mean_ic,
                    "icir": mean_ic / deviation if mean_ic is not None and deviation and deviation > 0 else None,
                    "confidence_interval_95": _cluster_bootstrap_ci(
                        daily_ic,
                        f"{mode}:{scope}:{rule_version}:{horizon}:ic",
                        config.bootstrap_samples,
                    ),
                }
            )
    return records


def _contract_rows(
    observations: tuple[_Observation, ...],
) -> list[tuple[str, str, str, tuple[_Observation, ...]]]:
    contracts = sorted({(item.mode, item.scope, item.rule_version) for item in observations})
    return [
        (
            mode,
            scope,
            rule_version,
            tuple(
                item
                for item in observations
                if (item.mode, item.scope, item.rule_version) == (mode, scope, rule_version)
            ),
        )
        for mode, scope, rule_version in contracts
    ]


def _stability_metrics(
    snapshots: tuple[_RunSnapshot, ...],
    config: EvaluationConfig,
) -> list[dict[str, object]]:
    records: list[dict[str, object]] = []
    by_mode: dict[str, list[_RunSnapshot]] = defaultdict(list)
    for snapshot in snapshots:
        by_mode[snapshot.mode].append(snapshot)
    for mode, mode_runs in by_mode.items():
        for previous, current in zip(mode_runs, mode_runs[1:], strict=False):
            comparable = previous.scope == current.scope and previous.rule_version == current.rule_version
            for top_n in config.top_sizes:
                records.append(_stability_record(mode, previous, current, top_n, comparable, config))
    return records


def _stability_record(
    mode: str,
    previous: _RunSnapshot,
    current: _RunSnapshot,
    top_n: int,
    comparable: bool,
    config: EvaluationConfig,
) -> dict[str, object]:
    previous_ranks = {symbol: rank for symbol, rank in previous.rankings if rank <= top_n}
    current_ranks = {symbol: rank for symbol, rank in current.rankings if rank <= top_n}
    common = sorted(previous_ranks.keys() & current_ranks.keys())
    denominator = min(len(previous_ranks), len(current_ranks), top_n)
    has_rankings = denominator > 0
    overlap = len(common) / denominator if has_rankings else None
    return {
        "mode": mode,
        "previous_run_id": previous.id,
        "current_run_id": current.id,
        "top_n": top_n,
        "status": "ok" if comparable and denominator >= config.minimum_sample_size else "insufficient_data",
        "comparable": comparable,
        "ranking_evidence_available": has_rankings,
        "overlap_rate": overlap if comparable else None,
        "turnover_rate": 1 - overlap if comparable and overlap is not None else None,
        "rank_stability": _spearman_rank_stability(previous_ranks, current_ranks, common) if comparable else None,
    }


def _spearman_rank_stability(
    previous: dict[str, int],
    current: dict[str, int],
    common: list[str],
) -> float | None:
    return _spearman([(float(previous[symbol]), float(current[symbol])) for symbol in common])


def _spearman(pairs: Sequence[tuple[float, float]]) -> float | None:
    if len(pairs) < 2:
        return None
    left = _midranks([item[0] for item in pairs])
    right = _midranks([item[1] for item in pairs])
    left_mean = fmean(left)
    right_mean = fmean(right)
    numerator = sum((x - left_mean) * (y - right_mean) for x, y in zip(left, right, strict=True))
    left_sum = sum((x - left_mean) ** 2 for x in left)
    right_sum = sum((y - right_mean) ** 2 for y in right)
    denominator = math.sqrt(left_sum * right_sum)
    return numerator / denominator if denominator > 0 else None


def _midranks(values: Sequence[float]) -> list[float]:
    ordered = sorted(enumerate(values), key=lambda item: (item[1], item[0]))
    ranks = [0.0] * len(values)
    index = 0
    while index < len(ordered):
        end = index + 1
        while end < len(ordered) and ordered[end][1] == ordered[index][1]:
            end += 1
        midrank = (index + end - 1) / 2 + 1
        for position in range(index, end):
            ranks[ordered[position][0]] = midrank
        index = end
    return ranks


def _market_regime(rows: Sequence[sqlite3.Row]) -> str:
    changes = [float(row["change_pct"]) for row in rows if row["change_pct"] is not None]
    average = fmean(changes) if changes else 0.0
    if average >= 1:
        return "strong"
    if average <= -1:
        return "weak"
    return "neutral"


def _quality_bucket(value: object) -> str:
    if value is None:
        return "unknown"
    score = int(str(value))
    if score >= 90:
        return "high"
    if score >= 80:
        return "medium"
    return "low"


def _liquidity_bucket(value: object) -> str:
    try:
        amount = float(str(value))
    except (TypeError, ValueError):
        return "low"
    if amount >= 1_000_000_000:
        return "high"
    if amount >= 100_000_000:
        return "medium"
    return "low"


def _scan_time_bucket(value: object, mode: str) -> str:
    text = str(value or "").replace("T", " ").replace("Z", "")
    try:
        parsed = datetime.fromisoformat(text)
    except ValueError:
        return "unknown"
    if mode == "official" or parsed.time() >= datetime.strptime("15:15", "%H:%M").time():
        return "after_close"
    if parsed.time() < datetime.strptime("11:30", "%H:%M").time():
        return "morning"
    return "afternoon"


def _board(symbol: str, market: str) -> str:
    code = symbol.split(".", 1)[0]
    if market == "BJ":
        return "BSE"
    if market == "SH" and code.startswith(("688", "689")):
        return "STAR"
    if market == "SZ" and code.startswith(("300", "301")):
        return "CHINEXT"
    return f"{market}_MAIN"


def _percentile(values: Sequence[float], probability: float) -> float:
    if not values:
        raise ValueError("percentile requires values")
    position = (len(values) - 1) * probability
    lower = math.floor(position)
    upper = math.ceil(position)
    if lower == upper:
        return values[lower]
    weight = position - lower
    return values[lower] * (1 - weight) + values[upper] * weight


def _run_summary(snapshot: _RunSnapshot, config: EvaluationConfig) -> dict[str, object]:
    available_horizons = sorted(
        {horizon for item in snapshot.observations for horizon in item.returns if horizon in config.horizons}
    )
    execution_horizons = sorted(
        {
            horizon
            for item in snapshot.observations
            for horizon, outcome in item.execution.items()
            if outcome.status == "modelled"
        }
    )
    return {
        "run_id": snapshot.id,
        "mode": snapshot.mode,
        "scope": snapshot.scope,
        "rule_version": snapshot.rule_version,
        "quote_date": snapshot.quote_date,
        "data_date": snapshot.data_date,
        "eligible_trading_day_count": len(snapshot.eligible_dates),
        "observation_count": len(snapshot.observations),
        "ranking_count": len(snapshot.rankings),
        "available_horizons": available_horizons,
        "execution_horizons": execution_horizons,
    }


__all__ = [
    "DEFAULT_HORIZONS",
    "DEFAULT_MINIMUM_SESSION_COUNT",
    "DEFAULT_TOP_SIZES",
    "EVALUATION_SCHEMA_VERSION",
    "EvaluationConfig",
    "evaluate_market_scan_rankings",
    "evaluate_market_scan_shadow_comparison",
    "evaluate_market_scan_shadow_rankings",
]
