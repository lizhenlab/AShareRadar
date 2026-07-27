from __future__ import annotations

from math import ceil
from typing import Protocol

from app.models.reliability import ReliabilityDuration, ReliabilityIndicator, ReliabilityReport, ReliabilityStatus
from app.repositories.reliability import (
    ReliabilityBucketStats,
    ReliabilityScanStats,
    ReliabilityTaskStats,
)
from app.utils.audit_time import (
    audit_datetime_to_text,
    audit_now_text,
    audit_seconds_ago_text,
    parse_audit_time,
)


ROLLING_WINDOW_SECONDS = 7 * 24 * 60 * 60
MARKET_SCAN_WINDOW_SECONDS = 30 * 24 * 60 * 60
DEFAULT_MINIMUM_SAMPLES = 20
MARKET_SCAN_MINIMUM_SAMPLES = 3
MARKET_SCAN_P95_TARGET_MS = 90 * 60 * 1000
SLO_TARGETS = {
    "market_scan_coverage": 0.95,
    "market_scan_success": 0.90,
    "provider_attempt": 0.95,
    "task_success": 0.95,
    "workbench_fresh": 0.95,
    "workbench_non_fallback": 0.80,
    "workbench_quality": 0.95,
    "workbench_usable": 0.99,
}


class ReliabilityCache(Protocol):
    def reliability_bucket_stats(self, metric: str, since: str) -> ReliabilityBucketStats: ...

    def reliability_market_scan_stats(self, since: str) -> ReliabilityScanStats: ...

    def reliability_task_stats(self, since: str) -> ReliabilityTaskStats: ...


def build_reliability_report(cache: ReliabilityCache) -> ReliabilityReport:
    rolling_since = audit_seconds_ago_text(ROLLING_WINDOW_SECONDS)
    bucket_since = _floor_to_utc_hour(rolling_since)
    scan_since = audit_seconds_ago_text(MARKET_SCAN_WINDOW_SECONDS)
    bucket_stats = {
        metric: cache.reliability_bucket_stats(metric, bucket_since)
        for metric in (
            "workbench_usable",
            "workbench_quality",
            "workbench_fresh",
            "workbench_non_fallback",
            "provider_attempt",
        )
    }
    scan_stats = cache.reliability_market_scan_stats(scan_since)
    task_stats = cache.reliability_task_stats(rolling_since)
    indicators = [_bucket_indicator(metric, stats) for metric, stats in bucket_stats.items()]
    indicators.extend(
        (
            _indicator(
                "market_scan_success",
                scan_stats.good_runs,
                scan_stats.total_runs,
                window_seconds=MARKET_SCAN_WINDOW_SECONDS,
                minimum_samples=MARKET_SCAN_MINIMUM_SAMPLES,
            ),
            _indicator(
                "market_scan_coverage",
                min(scan_stats.successful_symbols, scan_stats.total_symbols),
                scan_stats.total_symbols,
                window_seconds=MARKET_SCAN_WINDOW_SECONDS,
                minimum_samples=MARKET_SCAN_MINIMUM_SAMPLES,
                status_samples=scan_stats.coverage_run_count,
            ),
            _indicator(
                "task_success",
                task_stats.good,
                task_stats.total,
                window_seconds=ROLLING_WINDOW_SECONDS,
                minimum_samples=DEFAULT_MINIMUM_SAMPLES,
            ),
        )
    )
    return ReliabilityReport(
        checked_at=audit_now_text(),
        indicators=indicators,
        durations=[_scan_duration(scan_stats)],
    )


def _bucket_indicator(name: str, stats: ReliabilityBucketStats) -> ReliabilityIndicator:
    return _indicator(
        name,
        stats.good,
        stats.samples,
        window_seconds=ROLLING_WINDOW_SECONDS,
        minimum_samples=DEFAULT_MINIMUM_SAMPLES,
    )


def _floor_to_utc_hour(value: str) -> str:
    timestamp = parse_audit_time(value)
    return audit_datetime_to_text(timestamp.replace(minute=0, second=0, microsecond=0))


def _indicator(
    name: str,
    good: int,
    samples: int,
    *,
    window_seconds: int,
    minimum_samples: int,
    status_samples: int | None = None,
) -> ReliabilityIndicator:
    normalized_samples = max(0, int(samples))
    normalized_good = min(max(0, int(good)), normalized_samples)
    ratio = normalized_good / normalized_samples if normalized_samples else None
    target = SLO_TARGETS[name]
    normalized_assessment_samples = (
        normalized_samples if status_samples is None else max(0, int(status_samples))
    )
    return ReliabilityIndicator(
        name=name,
        window_seconds=window_seconds,
        target_ratio=target,
        minimum_samples=minimum_samples,
        samples=normalized_samples,
        assessment_samples=normalized_assessment_samples,
        good=normalized_good,
        ratio=round(ratio, 6) if ratio is not None else None,
        status=_ratio_status(
            ratio,
            target,
            normalized_assessment_samples,
            minimum_samples,
        ),
    )


def _scan_duration(stats: ReliabilityScanStats) -> ReliabilityDuration:
    values = sorted(stats.durations_ms)
    p50 = _percentile(values, 0.50)
    p95 = _percentile(values, 0.95)
    status: ReliabilityStatus = "insufficient_data"
    if len(values) >= MARKET_SCAN_MINIMUM_SAMPLES and p95 is not None:
        status = "met" if p95 <= MARKET_SCAN_P95_TARGET_MS else "breached"
    return ReliabilityDuration(
        name="market_scan_duration",
        window_seconds=MARKET_SCAN_WINDOW_SECONDS,
        target_p95_ms=MARKET_SCAN_P95_TARGET_MS,
        minimum_samples=MARKET_SCAN_MINIMUM_SAMPLES,
        samples=len(values),
        p50_ms=p50,
        p95_ms=p95,
        max_ms=values[-1] if values else None,
        status=status,
    )


def _ratio_status(
    ratio: float | None,
    target: float,
    samples: int,
    minimum_samples: int,
) -> ReliabilityStatus:
    if ratio is None or samples < minimum_samples:
        return "insufficient_data"
    return "met" if ratio >= target else "breached"


def _percentile(values: list[int], percentile: float) -> int | None:
    if not values:
        return None
    index = max(0, ceil(len(values) * percentile) - 1)
    return values[index]


__all__ = [
    "DEFAULT_MINIMUM_SAMPLES",
    "MARKET_SCAN_MINIMUM_SAMPLES",
    "MARKET_SCAN_P95_TARGET_MS",
    "MARKET_SCAN_WINDOW_SECONDS",
    "ROLLING_WINDOW_SECONDS",
    "SLO_TARGETS",
    "build_reliability_report",
]
