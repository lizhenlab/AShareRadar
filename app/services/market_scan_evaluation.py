from __future__ import annotations

from collections import defaultdict
from collections.abc import Iterable, Iterator, Sequence
from contextlib import contextmanager
from dataclasses import dataclass
import math
from pathlib import Path
import sqlite3
from statistics import fmean, median
from typing import Literal, cast

from app.utils.clock import utc_now


EVALUATION_SCHEMA_VERSION = "market-scan-forward-evaluation-v1"
DEFAULT_TOP_SIZES = (20, 50, 100)
DEFAULT_HORIZONS = (1, 3, 5, 10, 20)
EvaluationStatus = Literal["ok", "insufficient_data"]


@dataclass(frozen=True)
class EvaluationConfig:
    top_sizes: tuple[int, ...] = DEFAULT_TOP_SIZES
    horizons: tuple[int, ...] = DEFAULT_HORIZONS
    minimum_sample_size: int = 30
    complete_day_coverage: float = 0.95

    def __post_init__(self) -> None:
        if not self.top_sizes or any(value <= 0 for value in self.top_sizes):
            raise ValueError("top_sizes 必须是正整数")
        if not self.horizons or any(value <= 0 for value in self.horizons):
            raise ValueError("horizons 必须是正整数")
        if self.minimum_sample_size <= 0:
            raise ValueError("minimum_sample_size 必须大于 0")
        if not 0 < self.complete_day_coverage <= 1:
            raise ValueError("complete_day_coverage 必须在 (0, 1] 范围内")


@dataclass(frozen=True)
class _Observation:
    run_id: int
    mode: str
    scope: str
    rule_version: str
    symbol: str
    market: str
    rank: int
    quality_bucket: str
    regime: str
    returns: dict[int, float]
    adverse: dict[int, float]


@dataclass(frozen=True)
class _RunSnapshot:
    id: int
    mode: str
    scope: str
    rule_version: str
    quote_date: str
    observations: tuple[_Observation, ...]
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
    observations = tuple(item for snapshot in snapshots for item in snapshot.observations)
    cohorts = _cohort_metrics(observations, settings)
    monotonicity = _monotonicity_metrics(observations, settings)
    stability = _stability_metrics(snapshots, settings)
    eligible_runs = sum(bool(snapshot.observations) for snapshot in snapshots)
    status: EvaluationStatus = "ok" if any(item["status"] == "ok" for item in cohorts) else "insufficient_data"
    return {
        "schema_version": EVALUATION_SCHEMA_VERSION,
        "generated_at": utc_now().isoformat(timespec="seconds").replace("+00:00", "Z"),
        "status": status,
        "config": {
            "top_sizes": list(settings.top_sizes),
            "horizons": list(settings.horizons),
            "minimum_sample_size": settings.minimum_sample_size,
            "complete_day_coverage": settings.complete_day_coverage,
        },
        "source": {
            "database": _portable_database_label(path),
            "published_run_count": len(runs),
            "eligible_run_count": eligible_runs,
            "observation_count": len(observations),
            "read_only": True,
            "ranking_source": "persisted_market_scan_result",
            "forward_price_source": "persisted_qfq_kline_daily",
        },
        "runs": [_run_summary(snapshot, settings) for snapshot in snapshots],
        "cohorts": cohorts,
        "monotonicity": monotonicity,
        "stability": stability,
        "limitations": [
            "只读取已发布批次及之后实际持久化的完整交易日数据，不重算历史排名。",
            "收益与单调性 cohort 按 mode、scope、rule_version 隔离，不跨规则版本汇总。",
            "同一 mode、scope、rule_version、quote_date 只保留最后发布的快照，避免重复扫描放大样本。",
            "样本不足的切片标记 insufficient_data，不据此宣称策略有效。",
            "市场环境由扫描快照当日全市场涨跌幅均值分层，不使用未来信息。",
            "报告不会自动修改生产评分权重；规则调整必须创建新的 rule_version。",
        ],
    }


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


def _evaluate_run(
    conn: sqlite3.Connection,
    run: sqlite3.Row,
    config: EvaluationConfig,
) -> _RunSnapshot | None:
    result_rows = conn.execute(
        """
        SELECT symbol, market, rank, price, change_pct, data_quality_score,
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
    bars = _forward_bars(conn, int(run["id"]), quote_date)
    eligible_dates = _eligible_trading_dates(bars, len(result_rows), config)
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
        observations=observations,
        eligible_dates=eligible_dates,
    )


def _forward_bars(
    conn: sqlite3.Connection,
    run_id: int,
    quote_date: str,
) -> dict[str, tuple[sqlite3.Row, ...]]:
    rows = conn.execute(
        """
        SELECT k.symbol, k.date, k.close, k.low
        FROM kline_daily AS k
        JOIN market_scan_result AS r
          ON r.run_id = ? AND r.symbol = k.symbol AND r.status = 'success'
        WHERE k.date > ?
          AND k.adjustment_mode = CASE
              WHEN r.adjustment_mode IN ('qfq', 'hfq', 'none') THEN r.adjustment_mode
              ELSE 'qfq'
          END
        ORDER BY k.date ASC, k.symbol ASC
        """,
        (run_id, quote_date),
    ).fetchall()
    grouped: dict[str, list[sqlite3.Row]] = defaultdict(list)
    for row in rows:
        grouped[str(row["symbol"])].append(row)
    return {symbol: tuple(items) for symbol, items in grouped.items()}


def _eligible_trading_dates(
    bars: dict[str, tuple[sqlite3.Row, ...]],
    snapshot_count: int,
    config: EvaluationConfig,
) -> tuple[str, ...]:
    counts: dict[str, int] = defaultdict(int)
    for symbol_rows in bars.values():
        for date in {str(row["date"]) for row in symbol_rows}:
            counts[date] += 1
    required = max(1, math.ceil(snapshot_count * config.complete_day_coverage))
    return tuple(sorted(date for date, count in counts.items() if count >= required))[: max(config.horizons)]


def _observation_from_rows(
    run: sqlite3.Row,
    result: sqlite3.Row,
    bars: tuple[sqlite3.Row, ...],
    eligible_dates: tuple[str, ...],
    regime: str,
    config: EvaluationConfig,
) -> _Observation | None:
    entry = float(result["price"])
    by_date = {str(row["date"]): row for row in bars}
    returns: dict[int, float] = {}
    adverse: dict[int, float] = {}
    lows: list[float] = []
    for index, date in enumerate(eligible_dates, start=1):
        row = by_date.get(date)
        if row is None:
            continue
        lows.append(float(row["low"]))
        if index in config.horizons:
            returns[index] = float(row["close"]) / entry - 1
            adverse[index] = min(lows) / entry - 1
    if not returns:
        return None
    return _Observation(
        run_id=int(run["id"]),
        mode=str(run["mode"] or "official"),
        scope=str(run["scope"]),
        rule_version=str(run["rule_version"]),
        symbol=str(result["symbol"]),
        market=str(result["market"]),
        rank=int(result["rank"]),
        quality_bucket=_quality_bucket(result["data_quality_score"]),
        regime=regime,
        returns=returns,
        adverse=adverse,
    )


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
    for market in ("SH", "SZ", "BJ"):
        cohorts.append(({**contract, "market": market}, tuple(item for item in rows if item.market == market)))
    for regime in ("strong", "neutral", "weak"):
        cohorts.append(({**contract, "regime": regime}, tuple(item for item in rows if item.regime == regime)))
    for quality in ("unknown", "low", "medium", "high"):
        cohorts.append(({**contract, "quality": quality}, tuple(item for item in rows if item.quality_bucket == quality)))
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
    status: EvaluationStatus = "ok" if len(values) >= config.minimum_sample_size else "insufficient_data"
    record: dict[str, object] = {
        "dimensions": dimensions,
        "top_n": top_n,
        "horizon_trading_days": horizon,
        "status": status,
        "sample_size": len(values),
    }
    if not values:
        return record
    returns = [value for _item, value in values]
    adverse = [item.adverse[horizon] for item, _value in values if horizon in item.adverse]
    benchmark_by_run = _benchmark_returns(benchmark_rows, horizon)
    excess = [value - benchmark_by_run[item.run_id] for item, value in values if item.run_id in benchmark_by_run]
    record.update(
        {
            "average_return": fmean(returns),
            "median_return": median(returns),
            "positive_return_rate": sum(value > 0 for value in returns) / len(returns),
            "equal_weight_market_return": fmean(benchmark_by_run.values()) if benchmark_by_run else None,
            "equal_weight_market_excess_return": fmean(excess) if excess else None,
            "maximum_adverse_excursion": min(adverse) if adverse else None,
        }
    )
    return record


def _benchmark_returns(rows: Iterable[_Observation], horizon: int) -> dict[int, float]:
    grouped: dict[int, list[float]] = defaultdict(list)
    for item in rows:
        if horizon in item.returns:
            grouped[item.run_id].append(item.returns[horizon])
    return {run_id: fmean(values) for run_id, values in grouped.items() if values}


def _monotonicity_metrics(
    observations: tuple[_Observation, ...],
    config: EvaluationConfig,
) -> list[dict[str, object]]:
    records: list[dict[str, object]] = []
    contracts = sorted({(item.mode, item.scope, item.rule_version) for item in observations})
    for mode, scope, rule_version in contracts:
        contract_rows = tuple(
            item
            for item in observations
            if (item.mode, item.scope, item.rule_version) == (mode, scope, rule_version)
        )
        records.extend(
            _contract_monotonicity(
                mode,
                scope,
                rule_version,
                contract_rows,
                config,
            )
        )
    return records


def _contract_monotonicity(
    mode: str,
    scope: str,
    rule_version: str,
    rows: tuple[_Observation, ...],
    config: EvaluationConfig,
) -> list[dict[str, object]]:
    return [
        _monotonicity_record(mode, scope, rule_version, rows, horizon, config.minimum_sample_size)
        for horizon in config.horizons
    ]


def _monotonicity_record(
    mode: str,
    scope: str,
    rule_version: str,
    rows: tuple[_Observation, ...],
    horizon: int,
    minimum_sample_size: int,
) -> dict[str, object]:
    summaries = [
        _band_return_summary("1-20", rows, horizon, 1, 20),
        _band_return_summary("21-50", rows, horizon, 21, 50),
        _band_return_summary("51-100", rows, horizon, 51, 100),
    ]
    enough = _bands_have_minimum_samples(summaries, minimum_sample_size)
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
    values = [
        item.returns[horizon]
        for item in rows
        if minimum_rank <= item.rank <= maximum_rank and horizon in item.returns
    ]
    return {
        "band": label,
        "sample_size": len(values),
        "average_return": fmean(values) if values else None,
    }


def _bands_have_minimum_samples(
    summaries: list[dict[str, object]],
    minimum_sample_size: int,
) -> bool:
    return all(cast(int, item["sample_size"]) >= minimum_sample_size for item in summaries)


def _band_means_descend(summaries: list[dict[str, object]]) -> bool:
    means = [cast(float, item["average_return"]) for item in summaries]
    return all(left >= right for left, right in zip(means, means[1:], strict=False))


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
                previous_ranks = {item.symbol: item.rank for item in previous.observations if item.rank <= top_n}
                current_ranks = {item.symbol: item.rank for item in current.observations if item.rank <= top_n}
                common = sorted(previous_ranks.keys() & current_ranks.keys())
                denominator = max(1, min(len(previous_ranks), len(current_ranks), top_n))
                overlap = len(common) / denominator
                records.append(
                    {
                        "mode": mode,
                        "previous_run_id": previous.id,
                        "current_run_id": current.id,
                        "top_n": top_n,
                        "status": "ok" if comparable and denominator >= config.minimum_sample_size else "insufficient_data",
                        "comparable": comparable,
                        "overlap_rate": overlap if comparable else None,
                        "turnover_rate": 1 - overlap if comparable else None,
                        "rank_stability": _spearman_rank_stability(previous_ranks, current_ranks, common) if comparable else None,
                    }
                )
    return records


def _spearman_rank_stability(
    previous: dict[str, int],
    current: dict[str, int],
    common: list[str],
) -> float | None:
    if len(common) < 2:
        return None
    squared_differences = sum((previous[symbol] - current[symbol]) ** 2 for symbol in common)
    count = len(common)
    return max(-1.0, min(1.0, 1 - (6 * squared_differences) / (count * (count**2 - 1))))


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


def _run_summary(snapshot: _RunSnapshot, config: EvaluationConfig) -> dict[str, object]:
    available_horizons = sorted(
        {horizon for item in snapshot.observations for horizon in item.returns if horizon in config.horizons}
    )
    return {
        "run_id": snapshot.id,
        "mode": snapshot.mode,
        "scope": snapshot.scope,
        "rule_version": snapshot.rule_version,
        "quote_date": snapshot.quote_date,
        "eligible_trading_day_count": len(snapshot.eligible_dates),
        "observation_count": len(snapshot.observations),
        "available_horizons": available_horizons,
    }


__all__ = [
    "DEFAULT_HORIZONS",
    "DEFAULT_TOP_SIZES",
    "EVALUATION_SCHEMA_VERSION",
    "EvaluationConfig",
    "evaluate_market_scan_rankings",
]
