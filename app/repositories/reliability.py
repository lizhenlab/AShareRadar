from __future__ import annotations

from dataclasses import dataclass
import math
import sqlite3

from app.repositories.base import SQLiteRepository
from app.utils.audit_time import audit_datetime_to_text
from app.utils.clock import utc_now


RELIABILITY_METRICS = frozenset(
    {
        "provider_attempt",
        "workbench_fresh",
        "workbench_non_fallback",
        "workbench_quality",
        "workbench_usable",
    }
)


@dataclass(frozen=True)
class ReliabilityBucketStats:
    metric: str
    samples: int
    good: int
    degraded: int
    failures: int
    fallback: int
    duration_total_ms: int
    duration_max_ms: int


@dataclass(frozen=True)
class ReliabilityScanStats:
    good_runs: int
    total_runs: int
    coverage_run_count: int
    successful_symbols: int
    total_symbols: int
    durations_ms: tuple[int, ...]


@dataclass(frozen=True)
class ReliabilityTaskStats:
    good: int
    total: int


class ReliabilityRepository(SQLiteRepository):
    def record_workbench(
        self,
        *,
        usable: bool,
        duration_ms: float | int,
        quality: bool | None = None,
        fresh: bool | None = None,
        non_fallback: bool | None = None,
    ) -> None:
        observations = [("workbench_usable", usable, False)]
        for metric, value in (
            ("workbench_quality", quality),
            ("workbench_fresh", fresh),
            ("workbench_non_fallback", non_fallback),
        ):
            if value is not None:
                observations.append((metric, value, metric == "workbench_non_fallback" and not value))
        with self._lock, self._connect() as conn:
            for metric, good, fallback in observations:
                upsert_reliability_bucket(
                    conn,
                    metric=metric,
                    good=good,
                    degraded=usable and not good,
                    failed=not usable,
                    fallback=fallback,
                    duration_ms=duration_ms if metric == "workbench_usable" else None,
                )

    def record(
        self,
        metric: str,
        *,
        subject: str = "",
        capability: str = "",
        good: bool,
        degraded: bool = False,
        failed: bool = False,
        fallback: bool = False,
        duration_ms: float | int | None = None,
    ) -> None:
        with self._lock, self._connect() as conn:
            upsert_reliability_bucket(
                conn,
                metric=metric,
                subject=subject,
                capability=capability,
                good=good,
                degraded=degraded,
                failed=failed,
                fallback=fallback,
                duration_ms=duration_ms,
            )

    def bucket_stats(self, metric: str, since: str) -> ReliabilityBucketStats:
        _validate_metric(metric)
        with self._lock, self._connect() as conn:
            row = conn.execute(
                """
                SELECT
                    COALESCE(SUM(samples), 0) AS samples,
                    COALESCE(SUM(good), 0) AS good,
                    COALESCE(SUM(degraded), 0) AS degraded,
                    COALESCE(SUM(failures), 0) AS failures,
                    COALESCE(SUM(fallback), 0) AS fallback,
                    COALESCE(SUM(duration_total_ms), 0) AS duration_total_ms,
                    COALESCE(MAX(duration_max_ms), 0) AS duration_max_ms
                FROM reliability_bucket
                WHERE metric = ?
                  AND ashare_audit_epoch(bucket_start_utc) >= ashare_audit_epoch(?)
                """,
                (metric, since),
            ).fetchone()
        return ReliabilityBucketStats(
            metric=metric,
            samples=max(0, int(row["samples"] or 0)),
            good=max(0, int(row["good"] or 0)),
            degraded=max(0, int(row["degraded"] or 0)),
            failures=max(0, int(row["failures"] or 0)),
            fallback=max(0, int(row["fallback"] or 0)),
            duration_total_ms=max(0, int(row["duration_total_ms"] or 0)),
            duration_max_ms=max(0, int(row["duration_max_ms"] or 0)),
        )

    def market_scan_stats(self, since: str) -> ReliabilityScanStats:
        with self._lock, self._connect() as conn:
            rows = conn.execute(
                """
                SELECT status, trigger, total_count, success_count, duration_ms
                FROM market_scan_run
                WHERE status IN ('success', 'degraded', 'failed', 'interrupted')
                  AND ashare_audit_epoch(COALESCE(finished_at, updated_at, created_at))
                      >= ashare_audit_epoch(?)
                ORDER BY id ASC
                """,
                (since,),
            ).fetchall()
        durations = tuple(
            max(0, int(row["duration_ms"]))
            for row in rows
            if row["duration_ms"] is not None and str(row["trigger"]) != "retry"
        )
        return ReliabilityScanStats(
            good_runs=sum(str(row["status"]) in {"success", "degraded"} for row in rows),
            total_runs=len(rows),
            coverage_run_count=sum(max(0, int(row["total_count"] or 0)) > 0 for row in rows),
            successful_symbols=sum(max(0, int(row["success_count"] or 0)) for row in rows),
            total_symbols=sum(max(0, int(row["total_count"] or 0)) for row in rows),
            durations_ms=durations,
        )

    def task_stats(self, since: str) -> ReliabilityTaskStats:
        with self._lock, self._connect() as conn:
            row = conn.execute(
                """
                SELECT
                    SUM(CASE WHEN status IN ('success', 'degraded') THEN 1 ELSE 0 END) AS good,
                    COUNT(*) AS total
                FROM task_run
                WHERE status IN ('success', 'degraded', 'failed')
                  AND task_name <> 'full_market_scan'
                  AND ashare_audit_epoch(COALESCE(finished_at, started_at))
                      >= ashare_audit_epoch(?)
                """,
                (since,),
            ).fetchone()
        return ReliabilityTaskStats(
            good=max(0, int(row["good"] or 0)),
            total=max(0, int(row["total"] or 0)),
        )


def upsert_reliability_bucket(
    conn: sqlite3.Connection,
    *,
    metric: str,
    subject: str = "",
    capability: str = "",
    good: bool,
    degraded: bool = False,
    failed: bool = False,
    fallback: bool = False,
    duration_ms: float | int | None = None,
) -> None:
    _validate_metric(metric)
    normalized_duration = _non_negative_duration(duration_ms)
    timestamp = utc_now()
    bucket_start = audit_datetime_to_text(timestamp.replace(minute=0, second=0, microsecond=0))
    updated_at = audit_datetime_to_text(timestamp)
    conn.execute(
        """
        INSERT INTO reliability_bucket (
            bucket_start_utc, metric, subject, capability,
            samples, good, degraded, failures, fallback,
            duration_total_ms, duration_max_ms, updated_at
        ) VALUES (?, ?, ?, ?, 1, ?, ?, ?, ?, ?, ?, ?)
        ON CONFLICT(bucket_start_utc, metric, subject, capability) DO UPDATE SET
            samples = reliability_bucket.samples + 1,
            good = reliability_bucket.good + excluded.good,
            degraded = reliability_bucket.degraded + excluded.degraded,
            failures = reliability_bucket.failures + excluded.failures,
            fallback = reliability_bucket.fallback + excluded.fallback,
            duration_total_ms = reliability_bucket.duration_total_ms + excluded.duration_total_ms,
            duration_max_ms = MAX(reliability_bucket.duration_max_ms, excluded.duration_max_ms),
            updated_at = excluded.updated_at
        """,
        (
            bucket_start,
            metric,
            _dimension(subject, "subject"),
            _dimension(capability, "capability"),
            int(good),
            int(degraded),
            int(failed),
            int(fallback),
            normalized_duration,
            normalized_duration,
            updated_at,
        ),
    )


def _validate_metric(metric: str) -> None:
    if metric not in RELIABILITY_METRICS:
        raise ValueError(f"unsupported reliability metric: {metric}")


def _dimension(value: str, field: str) -> str:
    normalized = " ".join(str(value or "").split()).strip()
    if len(normalized) > 80:
        raise ValueError(f"reliability {field} is too long")
    return normalized


def _non_negative_duration(value: float | int | None) -> int:
    if value is None:
        return 0
    if isinstance(value, bool):
        raise ValueError("duration_ms must be numeric")
    number = float(value)
    if not math.isfinite(number) or number < 0:
        raise ValueError("duration_ms must be finite and non-negative")
    return round(number)


__all__ = [
    "RELIABILITY_METRICS",
    "ReliabilityBucketStats",
    "ReliabilityRepository",
    "ReliabilityScanStats",
    "ReliabilityTaskStats",
    "upsert_reliability_bucket",
]
