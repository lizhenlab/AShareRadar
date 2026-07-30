#!/usr/bin/env python3
"""Benchmark the full-market daily-K preservation-cache read path.

The benchmark is deliberately provider-free and read-only for the supplied
database. It compares the legacy one-connection-per-symbol path with the
production batch-prefetch path. Cold-cache measurements use a temporary empty
SQLite database; warm-cache measurements use the supplied persisted daily K
lines. Historical scan durations are reported separately so a local cache win
cannot be misrepresented as an end-to-end provider win.
"""

from __future__ import annotations

import argparse
from collections.abc import Callable, Iterator, Sequence
from contextlib import contextmanager
import json
from pathlib import Path
import sqlite3
import statistics
import sys
from tempfile import TemporaryDirectory
import threading
from time import perf_counter
from typing import Any


ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from app.config import get_settings  # noqa: E402
from app.repositories.market_data import MarketDataRepository  # noqa: E402
from app.services.cache import SQLiteCache  # noqa: E402


DEFAULT_LIMIT = 260
DEFAULT_BATCH_SIZE = 50
DEFAULT_ITERATIONS = 3
PRESERVATION_MAX_AGE_SECONDS = 10**9


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="全市场扫描日 K 暖/冷缓存读取基准（不会调用 provider）",
    )
    parser.add_argument(
        "--database",
        type=Path,
        default=get_settings().cache_path,
        help="包含 kline_daily 的 SQLite 数据库；默认使用当前配置",
    )
    parser.add_argument("--symbols", type=int, default=0, help="0 表示数据库中的全部股票")
    parser.add_argument("--limit", type=int, default=DEFAULT_LIMIT)
    parser.add_argument("--batch-size", type=int, default=DEFAULT_BATCH_SIZE)
    parser.add_argument("--iterations", type=int, default=DEFAULT_ITERATIONS)
    parser.add_argument("--output", type=Path)
    return parser


def _validate_args(args: argparse.Namespace) -> None:
    if not args.database.is_file():
        raise ValueError(f"数据库不存在：{args.database}")
    for name in ("limit", "batch_size", "iterations"):
        if getattr(args, name) <= 0:
            raise ValueError(f"--{name.replace('_', '-')} 必须大于 0")
    if args.symbols < 0:
        raise ValueError("--symbols 不能小于 0")


@contextmanager
def _read_only_connection(path: Path) -> Iterator[sqlite3.Connection]:
    connection = sqlite3.connect(f"{path.resolve().as_uri()}?mode=ro", uri=True)
    try:
        connection.row_factory = sqlite3.Row
        connection.execute("PRAGMA query_only = ON")
        yield connection
    finally:
        connection.close()


def _symbols(path: Path, limit: int) -> list[str]:
    with _read_only_connection(path) as connection:
        rows = connection.execute(
            "SELECT DISTINCT symbol FROM kline_daily ORDER BY symbol"
        ).fetchall()
    symbols = [str(row["symbol"]) for row in rows]
    return symbols[:limit] if limit else symbols


def _historical_live_evidence(path: Path) -> dict[str, Any]:
    with _read_only_connection(path) as connection:
        columns = {str(row["name"]) for row in connection.execute("PRAGMA table_info(market_scan_run)")}
        if not {"duration_ms", "total_count", "processed_count"}.issubset(columns):
            return {"sample_count": 0, "median_seconds": None, "stage_medians_ms": {}}
        retry_filter = "AND retry_of_run_id IS NULL" if "retry_of_run_id" in columns else ""
        durations = _historical_durations(connection, retry_filter)
        stage_medians = _historical_stage_medians(connection, retry_filter) if "stage_metrics_json" in columns else {}
    return {
        "sample_count": len(durations),
        "median_seconds": (
            round(statistics.median(durations) / 1000, 3) if durations else None
        ),
        "stage_medians_ms": stage_medians,
    }


def _historical_durations(connection: sqlite3.Connection, retry_filter: str) -> list[int]:
    rows = connection.execute(
        f"""
        SELECT duration_ms FROM market_scan_run
        WHERE duration_ms IS NOT NULL AND total_count >= 4000
          AND processed_count = total_count {retry_filter}
        ORDER BY id DESC LIMIT 30
        """
    ).fetchall()
    return [int(row["duration_ms"]) for row in rows]


def _historical_stage_medians(
    connection: sqlite3.Connection,
    retry_filter: str,
) -> dict[str, float]:
    rows = connection.execute(
        f"""
        SELECT stage_metrics_json FROM market_scan_run
        WHERE duration_ms IS NOT NULL AND total_count >= 4000
          AND processed_count = total_count {retry_filter}
        ORDER BY id DESC LIMIT 30
        """
    ).fetchall()
    stage_values: dict[str, list[int]] = {}
    for row in rows:
        try:
            metrics = json.loads(str(row["stage_metrics_json"] or "{}"))
        except (TypeError, ValueError):
            continue
        for stage, metric in metrics.items():
            duration = metric.get("duration_ms") if isinstance(metric, dict) else None
            if isinstance(duration, int) and duration >= 0:
                stage_values.setdefault(stage, []).append(duration)
    return {stage: round(statistics.median(values), 3) for stage, values in stage_values.items()}


def _legacy_read(
    repository: MarketDataRepository,
    symbols: Sequence[str],
    *,
    limit: int,
) -> tuple[int, tuple[tuple[str, str | None, str | None], ...]]:
    rows_by_symbol = {
        symbol: repository.get_klines(
            symbol,
            limit,
            PRESERVATION_MAX_AGE_SECONDS,
        )
        for symbol in symbols
    }
    return _cache_signature(rows_by_symbol)


def _prefetched_read(
    repository: MarketDataRepository,
    symbols: Sequence[str],
    *,
    limit: int,
    batch_size: int,
) -> tuple[int, tuple[tuple[str, str | None, str | None], ...]]:
    rows_by_symbol = {}
    for offset in range(0, len(symbols), batch_size):
        rows_by_symbol.update(
            repository.get_klines_many(
                symbols[offset : offset + batch_size],
                limit,
                PRESERVATION_MAX_AGE_SECONDS,
            )
        )
    return _cache_signature(rows_by_symbol)


def _cache_signature(
    rows_by_symbol: dict[str, list[Any]],
) -> tuple[int, tuple[tuple[str, str | None, str | None], ...]]:
    row_count = sum(len(rows) for rows in rows_by_symbol.values())
    boundaries = tuple(
        (
            symbol,
            rows[0].date if rows else None,
            rows[-1].date if rows else None,
        )
        for symbol, rows in sorted(rows_by_symbol.items())
    )
    return row_count, boundaries


def _measure(
    operation: Callable[[], tuple[int, tuple[tuple[str, str | None, str | None], ...]]],
    iterations: int,
) -> tuple[list[float], tuple[int, tuple[tuple[str, str | None, str | None], ...]]]:
    samples: list[float] = []
    signature = operation()
    for _index in range(iterations):
        started = perf_counter()
        current = operation()
        samples.append(perf_counter() - started)
        if current != signature:
            raise RuntimeError("基准期间缓存读取结果不稳定")
    return samples, signature


def _comparison(
    repository: MarketDataRepository,
    symbols: Sequence[str],
    *,
    limit: int,
    batch_size: int,
    iterations: int,
) -> dict[str, Any]:
    legacy, legacy_signature = _measure(
        lambda: _legacy_read(repository, symbols, limit=limit),
        iterations,
    )
    prefetched, prefetched_signature = _measure(
        lambda: _prefetched_read(
            repository,
            symbols,
            limit=limit,
            batch_size=batch_size,
        ),
        iterations,
    )
    if legacy_signature != prefetched_signature:
        raise RuntimeError("逐股读取与批量预取结果不一致")
    legacy_median = statistics.median(legacy)
    prefetched_median = statistics.median(prefetched)
    return {
        "legacy_seconds": [round(value, 6) for value in legacy],
        "prefetched_seconds": [round(value, 6) for value in prefetched],
        "legacy_median_seconds": round(legacy_median, 6),
        "prefetched_median_seconds": round(prefetched_median, 6),
        "median_improvement_pct": round(
            (1 - prefetched_median / legacy_median) * 100,
            2,
        ),
        "row_count": legacy_signature[0],
        "equivalent": True,
    }


def run_benchmark(
    database: Path,
    *,
    symbol_limit: int,
    limit: int,
    batch_size: int,
    iterations: int,
) -> dict[str, Any]:
    database = database.resolve()
    symbols = _symbols(database, symbol_limit)
    if not symbols:
        raise ValueError("数据库没有可用于基准的日 K 股票")
    warm = _warm_comparison(database, symbols, limit, batch_size, iterations)
    cold = _cold_comparison(symbols, limit, batch_size, iterations)

    live = _historical_live_evidence(database)
    local_savings = warm["legacy_median_seconds"] - warm["prefetched_median_seconds"]
    live_median = live["median_seconds"]
    estimated_ceiling = (
        round(local_savings / live_median * 100, 2)
        if isinstance(live_median, (int, float)) and live_median > 0
        else None
    )
    return {
        "schema_version": 1,
        "benchmark": "full_market_daily_kline_preservation_cache",
        "provider_calls": 0,
        "read_only_source_database": True,
        "symbol_count": len(symbols),
        "kline_limit": limit,
        "batch_size": batch_size,
        "iterations": iterations,
        "cold_empty_cache": cold,
        "warm_persisted_cache": warm,
        "historical_live_scans": live,
        "estimated_end_to_end_improvement_ceiling_pct": estimated_ceiling,
        "assessment": (
            "local_target_met_provider_remains_bottleneck"
            if warm["median_improvement_pct"] >= 30
            else "local_target_not_met"
        ),
        "limitations": [
            "基准只测量扫描前置日 K 缓存读取，不调用 provider，也不代表端到端扫描耗时。",
            "冷缓存使用同规模空数据库；暖缓存使用源数据库中的持久化日 K。",
            "端到端改善上限仅按历史中位耗时与本地节省量估算，需由后续真实扫描阶段指标复核。",
            "不得据此提高 provider 并发、降低数据门槛或接受未完成日 K。",
        ],
    }


def _warm_comparison(
    database: Path,
    symbols: Sequence[str],
    limit: int,
    batch_size: int,
    iterations: int,
) -> dict[str, Any]:
    before = database.stat()
    repository = MarketDataRepository(database, threading.RLock())
    result = _comparison(repository, symbols, limit=limit, batch_size=batch_size, iterations=iterations)
    after = database.stat()
    if (before.st_size, before.st_mtime_ns) != (after.st_size, after.st_mtime_ns):
        raise RuntimeError("暖缓存基准意外修改了源数据库")
    return result


def _cold_comparison(
    symbols: Sequence[str],
    limit: int,
    batch_size: int,
    iterations: int,
) -> dict[str, Any]:
    with TemporaryDirectory(prefix="ashare-scan-benchmark-") as tmpdir:
        empty_cache = SQLiteCache(Path(tmpdir) / "cold.sqlite3")
        return _comparison(
            empty_cache.market_data_repo,
            symbols,
            limit=limit,
            batch_size=batch_size,
            iterations=iterations,
        )


def main() -> int:
    args = _parser().parse_args()
    try:
        _validate_args(args)
        report = run_benchmark(
            args.database,
            symbol_limit=args.symbols,
            limit=args.limit,
            batch_size=args.batch_size,
            iterations=args.iterations,
        )
    except (OSError, sqlite3.Error, RuntimeError, ValueError) as exc:
        print(f"性能基准失败：{exc}", file=sys.stderr)
        return 2
    payload = json.dumps(report, ensure_ascii=False, indent=2) + "\n"
    if args.output:
        args.output.parent.mkdir(parents=True, exist_ok=True)
        args.output.write_text(payload, encoding="utf-8")
    else:
        print(payload, end="")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
