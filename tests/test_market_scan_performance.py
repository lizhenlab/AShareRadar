from __future__ import annotations

import json
from pathlib import Path
import subprocess
import sys

from app.services.cache import SQLiteCache
from tests.factories import make_kline
from tools.benchmark_market_scan import run_benchmark


def _performance_database(path: Path) -> Path:
    cache = SQLiteCache(path)
    for offset, symbol in enumerate(("600001.SH", "000001.SZ", "920066.BJ")):
        cache.save_klines(
            symbol,
            [
                make_kline(
                    date=f"2026-07-{day:02d}",
                    close=20 + offset + day,
                )
                for day in range(1, 6)
            ],
            "性能基准测试源",
        )
    return path


def test_cache_prefetch_benchmark_is_equivalent_and_does_not_mutate_source(
    tmp_path: Path,
) -> None:
    database = _performance_database(tmp_path / "performance.sqlite3")
    before = database.read_bytes()

    report = run_benchmark(
        database,
        symbol_limit=0,
        limit=3,
        batch_size=2,
        iterations=1,
    )

    assert database.read_bytes() == before
    assert report["provider_calls"] == 0
    assert report["read_only_source_database"] is True
    assert report["symbol_count"] == 3
    assert report["cold_empty_cache"]["row_count"] == 0
    assert report["warm_persisted_cache"]["row_count"] == 9
    assert report["warm_persisted_cache"]["equivalent"] is True
    assert report["historical_live_scans"]["sample_count"] == 0


def test_cache_prefetch_benchmark_cli_writes_json_report(tmp_path: Path) -> None:
    database = _performance_database(tmp_path / "performance-cli.sqlite3")
    output = tmp_path / "performance.json"

    completed = subprocess.run(
        [
            sys.executable,
            "tools/benchmark_market_scan.py",
            "--database",
            str(database),
            "--symbols",
            "3",
            "--limit",
            "3",
            "--batch-size",
            "2",
            "--iterations",
            "1",
            "--output",
            str(output),
        ],
        cwd=Path(__file__).resolve().parents[1],
        check=False,
        capture_output=True,
        text=True,
    )

    assert completed.returncode == 0, completed.stderr
    payload = json.loads(output.read_text(encoding="utf-8"))
    assert payload["benchmark"] == "full_market_daily_kline_preservation_cache"
    assert payload["warm_persisted_cache"]["equivalent"] is True
