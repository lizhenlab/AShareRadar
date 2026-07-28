from __future__ import annotations

from datetime import UTC, datetime
from pathlib import Path
import sqlite3
from types import SimpleNamespace

import pytest

from app.services.cache import SQLiteCache
from app.services.reliability import (
    MARKET_SCAN_WINDOW_SECONDS,
    MARKET_SCAN_P95_TARGET_MS,
    ROLLING_WINDOW_SECONDS,
    _floor_to_utc_hour,
    _indicator,
    _percentile,
    build_reliability_report,
)
from app.utils.audit_time import audit_now_text, audit_seconds_ago_text


def test_hourly_buckets_aggregate_without_request_cardinality(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    cache = SQLiteCache(tmp_path / "reliability.sqlite3")
    fixed_now = datetime(2026, 7, 24, 3, 22, 15, tzinfo=UTC)
    monkeypatch.setattr("app.repositories.reliability.utc_now", lambda: fixed_now)

    cache.record_workbench_reliability(
        usable=True,
        duration_ms=12,
        quality=False,
        fresh=True,
        non_fallback=False,
    )
    cache.record_workbench_reliability(usable=False, duration_ms=8)
    cache.provider_status_repo.record_capability_success("tencent", "quote", 1, 21.4)
    cache.provider_status_repo.record_capability_failure("tencent", "quote", 1, "timeout")

    usable = cache.reliability_bucket_stats("workbench_usable", "2026-07-01T00:00:00Z")
    quality = cache.reliability_bucket_stats("workbench_quality", "2026-07-01T00:00:00Z")
    non_fallback = cache.reliability_bucket_stats("workbench_non_fallback", "2026-07-01T00:00:00Z")
    provider = cache.reliability_bucket_stats("provider_attempt", "2026-07-01T00:00:00Z")

    assert (usable.samples, usable.good, usable.failures) == (2, 1, 1)
    assert (usable.duration_total_ms, usable.duration_max_ms) == (20, 12)
    assert (quality.samples, quality.good, quality.degraded) == (1, 0, 1)
    assert (non_fallback.samples, non_fallback.good, non_fallback.fallback) == (1, 0, 1)
    assert (provider.samples, provider.good, provider.failures) == (2, 1, 1)

    with sqlite3.connect(cache.path) as conn:
        rows = conn.execute(
            "SELECT bucket_start_utc, metric, subject, capability FROM reliability_bucket ORDER BY metric"
        ).fetchall()
    assert len(rows) == 5
    assert {row[0] for row in rows} == {"2026-07-24T03:00:00.000000Z"}
    assert (
        "2026-07-24T03:00:00.000000Z",
        "provider_attempt",
        "tencent",
        "quote",
    ) in rows


def test_report_uses_fixed_windows_minimum_samples_and_terminal_run_semantics(tmp_path: Path) -> None:
    cache = SQLiteCache(tmp_path / "reliability.sqlite3")
    for index in range(20):
        cache.record_workbench_reliability(
            usable=index != 0,
            duration_ms=10 + index,
            quality=index != 0,
            fresh=True,
            non_fallback=index >= 4,
        )
        if index == 0:
            cache.provider_status_repo.record_capability_failure("tencent", "quote", 1, "timeout")
        else:
            cache.provider_status_repo.record_capability_success("tencent", "quote", 1, 10 + index)

    now = audit_now_text()
    with sqlite3.connect(cache.path) as conn:
        for index in range(20):
            status = "failed" if index == 0 else "success"
            conn.execute(
                """
                INSERT INTO task_run (task_name, status, started_at, finished_at, duration_ms)
                VALUES (?, ?, ?, ?, ?)
                """,
                ("refresh_watch_quotes", status, now, now, 100 + index),
            )
        conn.execute(
            "INSERT INTO task_run (task_name, status, started_at, finished_at) VALUES (?, ?, ?, ?)",
            ("full_market_scan", "failed", now, now),
        )
        conn.execute(
            "INSERT INTO task_run (task_name, status, started_at, finished_at) VALUES (?, ?, ?, ?)",
            ("refresh_watch_quotes", "cancelled", now, now),
        )
        _insert_scan(conn, status="success", trigger="manual", total=100, success=100, duration_ms=60 * 60 * 1000)
        _insert_scan(conn, status="degraded", trigger="scheduled", total=100, success=90, duration_ms=100 * 60 * 1000)
        _insert_scan(conn, status="failed", trigger="manual", total=100, success=0, duration_ms=10 * 60 * 1000)
        _insert_scan(conn, status="cancelled", trigger="manual", total=100, success=100, duration_ms=1)
        _insert_scan(conn, status="success", trigger="retry", total=100, success=100, duration_ms=180 * 60 * 1000)

    report = build_reliability_report(cache)
    indicators = {indicator.name: indicator for indicator in report.indicators}
    duration = report.durations[0]

    assert indicators["workbench_usable"].status == "breached"
    assert indicators["workbench_fresh"].status == "met"
    assert indicators["workbench_non_fallback"].status == "met"
    assert indicators["provider_attempt"].ratio == 0.95
    assert indicators["provider_attempt"].status == "met"
    assert indicators["task_success"].samples == 20
    assert indicators["task_success"].status == "met"
    assert indicators["market_scan_success"].samples == 4
    assert indicators["market_scan_success"].status == "breached"
    assert indicators["market_scan_coverage"].assessment_samples == 4
    assert indicators["market_scan_coverage"].samples == 400
    assert indicators["market_scan_coverage"].ratio == 0.725
    assert indicators["market_scan_coverage"].status == "breached"
    assert duration.samples == 3
    assert duration.p95_ms == 100 * 60 * 1000
    assert duration.p95_ms > MARKET_SCAN_P95_TARGET_MS
    assert duration.status == "breached"


def test_coverage_sample_gate_counts_only_runs_with_a_denominator(tmp_path: Path) -> None:
    cache = SQLiteCache(tmp_path / "reliability.sqlite3")
    with sqlite3.connect(cache.path) as conn:
        _insert_scan(conn, status="success", trigger="manual", total=100, success=100, duration_ms=100)
        _insert_scan(conn, status="failed", trigger="manual", total=0, success=0, duration_ms=100)
        _insert_scan(conn, status="interrupted", trigger="manual", total=0, success=0, duration_ms=100)

    report = build_reliability_report(cache)
    coverage = next(item for item in report.indicators if item.name == "market_scan_coverage")

    assert coverage.samples == 100
    assert coverage.good == 100
    assert coverage.ratio == 1.0
    assert coverage.assessment_samples == 1
    assert coverage.status == "insufficient_data"


def test_market_scan_reliability_coverage_excludes_deterministic_skips(tmp_path: Path) -> None:
    cache = SQLiteCache(tmp_path / "reliability.sqlite3")
    with sqlite3.connect(cache.path) as conn:
        _insert_scan(
            conn,
            status="degraded",
            trigger="manual",
            total=100,
            success=90,
            skipped=10,
            duration_ms=100,
        )

    stats = cache.reliability_market_scan_stats(audit_seconds_ago_text(60))

    assert stats.coverage_run_count == 1
    assert stats.successful_symbols == 90
    assert stats.total_symbols == 90


def test_reliability_bucket_window_floors_only_the_rolling_start_to_utc_hour(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    rolling_since = "2026-07-17T03:22:15.123456Z"
    scan_since = "2026-06-24T03:22:15.123456Z"
    cache = _ReliabilityWindowSpy()

    def seconds_ago(seconds: int) -> str:
        return {
            ROLLING_WINDOW_SECONDS: rolling_since,
            MARKET_SCAN_WINDOW_SECONDS: scan_since,
        }[seconds]

    monkeypatch.setattr("app.services.reliability.audit_seconds_ago_text", seconds_ago)

    build_reliability_report(cache)

    assert _floor_to_utc_hour(rolling_since) == "2026-07-17T03:00:00.000000Z"
    assert cache.bucket_windows == ["2026-07-17T03:00:00.000000Z"] * 5
    assert cache.task_windows == [rolling_since]
    assert cache.scan_windows == [scan_since]


@pytest.mark.parametrize(
    ("good", "samples", "expected_good", "expected_ratio"),
    [
        (-1, 10, 0, 0.0),
        (3, -1, 0, None),
        (11, 10, 10, 1.0),
        (7, 10, 7, 0.7),
    ],
)
def test_ratio_indicator_preserves_counter_invariants(
    good: int,
    samples: int,
    expected_good: int,
    expected_ratio: float | None,
) -> None:
    indicator = _indicator(
        "task_success",
        good,
        samples,
        window_seconds=60,
        minimum_samples=20,
    )

    assert 0 <= indicator.good <= indicator.samples
    assert indicator.good == expected_good
    assert indicator.ratio == expected_ratio
    assert indicator.status == "insufficient_data"


@pytest.mark.parametrize(
    ("values", "percentile", "expected"),
    [
        ([], 0.95, None),
        ([5], 0.95, 5),
        ([1, 2, 3, 4], 0.50, 2),
        ([1, 2, 3, 100], 0.95, 100),
    ],
)
def test_nearest_rank_percentile_is_deterministic(
    values: list[int],
    percentile: float,
    expected: int | None,
) -> None:
    assert _percentile(values, percentile) == expected


def _insert_scan(
    conn: sqlite3.Connection,
    *,
    status: str,
    trigger: str,
    total: int,
    success: int,
    duration_ms: int,
    skipped: int = 0,
) -> None:
    now = audit_now_text()
    conn.execute(
        """
        INSERT INTO market_scan_run (
            status, trigger, rule_version, as_of, data_date, scope,
            total_count, processed_count, success_count, skipped_count,
            created_at, updated_at, started_at, finished_at, duration_ms
        ) VALUES (?, ?, 'test-v1', ?, ?, 'all', ?, ?, ?, ?, ?, ?, ?, ?, ?)
        """,
        (
            status,
            trigger,
            now,
            now[:10],
            total,
            total,
            success,
            skipped,
            now,
            now,
            now,
            now,
            duration_ms,
        ),
    )


class _ReliabilityWindowSpy:
    def __init__(self) -> None:
        self.bucket_windows: list[str] = []
        self.scan_windows: list[str] = []
        self.task_windows: list[str] = []

    def reliability_bucket_stats(self, metric: str, since: str) -> SimpleNamespace:
        self.bucket_windows.append(since)
        return SimpleNamespace(metric=metric, samples=0, good=0)

    def reliability_market_scan_stats(self, since: str) -> SimpleNamespace:
        self.scan_windows.append(since)
        return SimpleNamespace(
            good_runs=0,
            total_runs=0,
            coverage_run_count=0,
            successful_symbols=0,
            total_symbols=0,
            durations_ms=(),
        )

    def reliability_task_stats(self, since: str) -> SimpleNamespace:
        self.task_windows.append(since)
        return SimpleNamespace(good=0, total=0)
