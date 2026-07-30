from __future__ import annotations

import json
from contextlib import closing
from pathlib import Path
import sqlite3
import subprocess
import sys
from typing import Any, cast

import pytest

from app.db.schema import initialize_schema
from app.services.market_scan_evaluation import (
    DEFAULT_HORIZONS,
    DEFAULT_TOP_SIZES,
    EvaluationConfig,
    evaluate_market_scan_rankings,
)


def test_read_only_forward_evaluation_uses_frozen_rank_and_complete_future_days(tmp_path: Path) -> None:
    path = tmp_path / "evaluation.sqlite3"
    _initialize(path)
    run_id = _seed_run(
        path,
        mode="official",
        rule_version="rule-v1",
        quote_date="2026-01-05",
        ranks=("600001.SH", "000002.SZ", "920003.BJ"),
        changes=(2.0, 1.5, 1.0),
    )
    _seed_forward_prices(
        path,
        dates=("2026-01-06", "2026-01-07", "2026-01-08"),
        closes={
            "600001.SH": (110, 120, 130),
            "000002.SZ": (105, 110, 115),
            "920003.BJ": (90, 80, 70),
        },
        lows={
            "600001.SH": (95, 105, 115),
            "000002.SZ": (98, 103, 108),
            "920003.BJ": (85, 75, 65),
        },
    )

    report = evaluate_market_scan_rankings(
        path,
        config=EvaluationConfig(
            top_sizes=(2, 3),
            horizons=(1, 3),
            minimum_sample_size=2,
            complete_day_coverage=1.0,
        ),
        run_ids=[run_id],
    )

    assert report["status"] == "ok"
    assert report["source"] == {
        "database": str(path.resolve()),
        "published_run_count": 1,
        "eligible_run_count": 1,
        "observation_count": 3,
        "read_only": True,
        "ranking_source": "persisted_market_scan_result",
        "forward_price_source": "persisted_qfq_kline_daily",
    }
    contract = {"mode": "official", "scope": "SH/SZ/BJ", "rule_version": "rule-v1"}
    metric = _cohort(report, dimensions=contract, top_n=2, horizon=3)
    assert metric["status"] == "ok"
    assert metric["sample_size"] == 2
    assert metric["average_return"] == pytest.approx(0.225)
    assert metric["median_return"] == pytest.approx(0.225)
    assert metric["positive_return_rate"] == 1
    assert metric["equal_weight_market_return"] == pytest.approx(0.05)
    assert metric["equal_weight_market_excess_return"] == pytest.approx(0.175)
    assert metric["maximum_adverse_excursion"] == pytest.approx(-0.05)
    runs = cast(list[dict[str, Any]], report["runs"])
    cohorts = cast(list[dict[str, Any]], report["cohorts"])
    assert runs[0]["available_horizons"] == [1, 3]
    assert any(item["dimensions"] == {**contract, "market": "BJ"} for item in cohorts)
    assert any(item["dimensions"] == {**contract, "regime": "strong"} for item in cohorts)
    assert any(item["dimensions"] == {**contract, "quality": "high"} for item in cohorts)


def test_evaluation_reports_insufficient_samples_and_same_rule_rank_turnover(tmp_path: Path) -> None:
    path = tmp_path / "stability.sqlite3"
    _initialize(path)
    first = _seed_run(
        path,
        mode="official",
        rule_version="rule-v1",
        quote_date="2026-01-05",
        ranks=("600001.SH", "000002.SZ", "920003.BJ"),
    )
    second = _seed_run(
        path,
        mode="official",
        rule_version="rule-v1",
        quote_date="2026-01-10",
        ranks=("000002.SZ", "600001.SH", "300004.SZ"),
    )
    third = _seed_run(
        path,
        mode="official",
        rule_version="rule-v2",
        quote_date="2026-01-15",
        ranks=("600001.SH", "000002.SZ", "300004.SZ"),
    )
    for dates in (
        ("2026-01-06", "2026-01-07", "2026-01-08"),
        ("2026-01-11", "2026-01-12", "2026-01-13"),
        ("2026-01-16", "2026-01-17", "2026-01-18"),
    ):
        _seed_forward_prices(
            path,
            dates=dates,
            closes={symbol: (101, 102, 103) for symbol in ("600001.SH", "000002.SZ", "920003.BJ", "300004.SZ")},
        )

    report = evaluate_market_scan_rankings(
        path,
        config=EvaluationConfig(top_sizes=(2,), horizons=(1, 3), minimum_sample_size=5),
    )

    assert report["status"] == "insufficient_data"  # 不跨 rule_version 拼接样本门槛
    stability = cast(list[dict[str, Any]], report["stability"])
    pair = next(
        item
        for item in stability
        if item["previous_run_id"] == first and item["current_run_id"] == second
    )
    assert pair["comparable"] is True
    assert pair["overlap_rate"] == 1
    assert pair["turnover_rate"] == 0
    assert pair["rank_stability"] == -1
    mismatch = next(
        item
        for item in stability
        if item["previous_run_id"] == second and item["current_run_id"] == third
    )
    assert mismatch["comparable"] is False
    assert mismatch["status"] == "insufficient_data"
    monotonicity = cast(list[dict[str, Any]], report["monotonicity"])
    assert all(item["status"] == "insufficient_data" for item in monotonicity)

    default_report = evaluate_market_scan_rankings(path, run_ids=[first])
    assert DEFAULT_TOP_SIZES == (20, 50, 100)
    assert DEFAULT_HORIZONS == (1, 3, 5, 10, 20)
    assert default_report["status"] == "insufficient_data"


def test_evaluation_keeps_only_the_last_same_contract_session_snapshot(
    tmp_path: Path,
) -> None:
    path = tmp_path / "deduplicated-session.sqlite3"
    _initialize(path)
    first = _seed_run(
        path,
        mode="official",
        rule_version="rule-v1",
        quote_date="2026-01-05",
        ranks=("600001.SH", "000002.SZ"),
    )
    second = _seed_run(
        path,
        mode="official",
        rule_version="rule-v1",
        quote_date="2026-01-05",
        ranks=("000002.SZ", "600001.SH"),
    )
    _seed_forward_prices(
        path,
        dates=("2026-01-06",),
        closes={"600001.SH": (101,), "000002.SZ": (99,)},
    )

    report = evaluate_market_scan_rankings(
        path,
        config=EvaluationConfig(top_sizes=(2,), horizons=(1,), minimum_sample_size=2),
        run_ids=[first, second],
    )

    assert report["source"]["published_run_count"] == 1  # type: ignore[index]
    runs = cast(list[dict[str, Any]], report["runs"])
    assert [item["run_id"] for item in runs] == [second]


def test_evaluation_cli_emits_json_without_mutating_database(tmp_path: Path) -> None:
    path = tmp_path / "cli.sqlite3"
    _initialize(path)
    run_id = _seed_run(
        path,
        mode="intraday",
        rule_version="rule-v1",
        quote_date="2026-01-05",
        ranks=("600001.SH", "000002.SZ"),
    )
    _seed_forward_prices(
        path,
        dates=("2026-01-06", "2026-01-07", "2026-01-08"),
        closes={"600001.SH": (101, 102, 103), "000002.SZ": (99, 100, 101)},
    )
    before = path.read_bytes()

    completed = subprocess.run(
        [
            sys.executable,
            "tools/evaluate_market_scan.py",
            "--database",
            str(path),
            "--mode",
            "intraday",
            "--run-id",
            str(run_id),
            "--minimum-sample-size",
            "2",
        ],
        cwd=Path(__file__).resolve().parents[1],
        check=True,
        capture_output=True,
        text=True,
    )
    payload = json.loads(completed.stdout)

    assert payload["source"]["read_only"] is True
    assert payload["source"]["published_run_count"] == 1
    assert path.read_bytes() == before


def _cohort(
    report: dict[str, object],
    *,
    dimensions: dict[str, str],
    top_n: int,
    horizon: int,
) -> dict[str, object]:
    cohorts = cast(list[dict[str, Any]], report["cohorts"])
    return next(
        item
        for item in cohorts
        if item["dimensions"] == dimensions
        and item["top_n"] == top_n
        and item["horizon_trading_days"] == horizon
    )


def _initialize(path: Path) -> None:
    with closing(sqlite3.connect(path)) as conn, conn:
        initialize_schema(conn)


def _seed_run(
    path: Path,
    *,
    mode: str,
    rule_version: str,
    quote_date: str,
    ranks: tuple[str, ...],
    changes: tuple[float, ...] | None = None,
) -> int:
    timestamp = f"{quote_date}T08:00:00.000000Z"
    with closing(sqlite3.connect(path)) as conn, conn:
        run_id = conn.execute(
            """
            INSERT INTO market_scan_run (
                status, trigger, mode, rule_version, as_of, data_date, quote_date,
                scope, total_count, processed_count, success_count,
                created_at, updated_at, finished_at
            ) VALUES ('success', 'manual', ?, ?, ?, ?, ?, 'SH/SZ/BJ', ?, ?, ?, ?, ?, ?)
            """,
            (
                mode,
                rule_version,
                f"{quote_date} 15:00:00",
                quote_date,
                quote_date,
                len(ranks),
                len(ranks),
                len(ranks),
                timestamp,
                timestamp,
                timestamp,
            ),
        ).lastrowid
        assert run_id is not None
        for index, symbol in enumerate(ranks, start=1):
            code, market = symbol.split(".")
            conn.execute(
                """
                INSERT INTO market_scan_result (
                    run_id, symbol, code, market, name, status, rank, score, raw_score,
                    price, change_pct, data_quality_score, adjustment_mode, updated_at
                ) VALUES (?, ?, ?, ?, ?, 'success', ?, ?, ?, 100, ?, ?, 'qfq', ?)
                """,
                (
                    run_id,
                    symbol,
                    code,
                    market,
                    f"样本{code}",
                    index,
                    100 - index,
                    100 - index / 10,
                    changes[index - 1] if changes else 0,
                    95 if index == 1 else 85 if index == 2 else 75,
                    timestamp,
                ),
            )
    return int(run_id)


def _seed_forward_prices(
    path: Path,
    *,
    dates: tuple[str, ...],
    closes: dict[str, tuple[float, ...]],
    lows: dict[str, tuple[float, ...]] | None = None,
) -> None:
    with closing(sqlite3.connect(path)) as conn, conn:
        for symbol, prices in closes.items():
            low_prices = (lows or {}).get(symbol, prices)
            for date, close, low in zip(dates, prices, low_prices, strict=True):
                conn.execute(
                    """
                    INSERT OR REPLACE INTO kline_daily (
                        symbol, adjustment_mode, date, open, close, high, low, volume,
                        as_of, data_version, contract_version, fallback_used, source, fetched_at
                    ) VALUES (?, 'qfq', ?, ?, ?, ?, ?, 1000, ?, 'test-v1', 'test-v1', 0, 'test', ?)
                    """,
                    (symbol, date, close, close, close, low, date, f"{date}T08:00:00.000000Z"),
                )
