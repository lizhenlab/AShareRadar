from __future__ import annotations

from collections.abc import Iterable, Sequence
from dataclasses import dataclass
from datetime import datetime, timedelta
import math
from typing import Literal

from app.models.market_scan import (
    MarketScanCoverageScope,
    MarketScanPublicationSummary,
    MarketScanRetryPlan,
    MarketScanRun,
)
from app.services.data_quality_time import latest_expected_daily_kline_date
from app.services.market_scan_completion import (
    MARKET_SCAN_MAX_SNAPSHOT_SPAN_SECONDS,
    MARKET_SCAN_PUBLISH_MIN_COVERAGE,
    MARKET_SCAN_PUBLISH_MIN_ELIGIBLE_RATIO,
)
from app.utils.time import parse_text_time


DEFAULT_MARKET_SCAN_AUTO_RETRY_DELAYS_SECONDS = (10 * 60.0, 30 * 60.0, 60 * 60.0)
DEFAULT_MARKET_SCAN_AUTO_RETRY_MAX_ATTEMPTS = 3
_PUBLISH_COVERAGE_SCOPES: tuple[MarketScanCoverageScope, ...] = ("ALL", "SH", "SZ", "BJ")


@dataclass(frozen=True)
class MarketScanAutomaticAction:
    kind: Literal["scheduled", "retry"]
    data_date: str
    source_run_id: int | None = None


@dataclass(frozen=True)
class MarketScanRetryDecision:
    eligible: bool
    due_at: datetime | None = None
    reason: str = ""

    def is_due(self, current: datetime) -> bool:
        return self.eligible and self.due_at is not None and current >= self.due_at


def automatic_retry_decision(
    run: MarketScanRun,
    plan: MarketScanRetryPlan,
    summary: MarketScanPublicationSummary,
    *,
    current: datetime,
    delays_seconds: Sequence[float],
    max_retry_attempts: int,
) -> MarketScanRetryDecision:
    if run.trigger not in {"scheduled", "retry"} or run.status not in {"failed", "interrupted"}:
        return MarketScanRetryDecision(False, reason="status-or-trigger-excluded")
    if run.data_date != latest_expected_daily_kline_date(current).isoformat():
        return MarketScanRetryDecision(False, reason="not-current-data-date")
    retry_limit = effective_auto_retry_limit(delays_seconds, max_retry_attempts)
    if run.retry_count >= retry_limit:
        return MarketScanRetryDecision(False, reason="attempts-exhausted")
    if run.status == "failed" and not _failed_run_is_retryable(run, plan, summary):
        return MarketScanRetryDecision(False, reason="individual-or-non-retryable-failure")
    finished_at = _run_finished_at(run)
    if finished_at is None:
        return MarketScanRetryDecision(False, reason="missing-finished-at")
    due_at = finished_at + timedelta(seconds=float(delays_seconds[run.retry_count]))
    return MarketScanRetryDecision(True, due_at=due_at, reason="retryable-terminal-run")


def _failed_run_is_retryable(
    run: MarketScanRun,
    plan: MarketScanRetryPlan,
    summary: MarketScanPublicationSummary,
) -> bool:
    if not plan.needs_market_data:
        return False
    if _completed_with_only_deterministic_skips(run, summary):
        return False
    if run.total_count == 0 or run.processed_count < run.total_count:
        return True
    if summary.systemic_stale_cluster is not None:
        return True
    if _coverage_below_publish_floor(summary):
        return True
    if summary.invalid_snapshot_timestamps:
        return True
    span = summary.snapshot_span_seconds
    return span is not None and span > MARKET_SCAN_MAX_SNAPSHOT_SPAN_SECONDS


def _completed_with_only_deterministic_skips(
    run: MarketScanRun,
    summary: MarketScanPublicationSummary,
) -> bool:
    span = summary.snapshot_span_seconds
    return (
        run.total_count > 0
        and run.processed_count == run.total_count
        and run.skipped_count > 0
        and run.missing_count == 0
        and run.success_count + run.skipped_count == run.total_count
        and summary.systemic_stale_cluster is None
        and not summary.invalid_snapshot_timestamps
        and (span is None or span <= MARKET_SCAN_MAX_SNAPSHOT_SPAN_SECONDS)
        and not _coverage_below_publish_floor(summary)
    )


def _coverage_below_publish_floor(summary: MarketScanPublicationSummary) -> bool:
    return any(
        (coverage := summary.coverage_for(scope)) is None
        or coverage.coverage_ratio < MARKET_SCAN_PUBLISH_MIN_COVERAGE[scope]
        or coverage.eligible_ratio < MARKET_SCAN_PUBLISH_MIN_ELIGIBLE_RATIO[scope]
        for scope in _PUBLISH_COVERAGE_SCOPES
    )


def configured_auto_retry_policy(settings: object) -> tuple[tuple[float, ...], int]:
    raw_delays = getattr(
        settings,
        "market_scan_auto_retry_delays_seconds",
        DEFAULT_MARKET_SCAN_AUTO_RETRY_DELAYS_SECONDS,
    )
    delays = _positive_delays(raw_delays)
    raw_attempts = getattr(
        settings,
        "market_scan_auto_retry_max_attempts",
        DEFAULT_MARKET_SCAN_AUTO_RETRY_MAX_ATTEMPTS,
    )
    try:
        attempts = max(0, int(raw_attempts))
    except (TypeError, ValueError):
        attempts = DEFAULT_MARKET_SCAN_AUTO_RETRY_MAX_ATTEMPTS
    return delays, attempts


def effective_auto_retry_limit(delays_seconds: Sequence[float], max_retry_attempts: int) -> int:
    return min(len(delays_seconds), max(0, int(max_retry_attempts)))


def _positive_delays(values: object) -> tuple[float, ...]:
    if isinstance(values, str):
        candidates: Iterable[object] = values.split(",")
    elif isinstance(values, Iterable):
        candidates = values
    else:
        return DEFAULT_MARKET_SCAN_AUTO_RETRY_DELAYS_SECONDS
    parsed = tuple(_positive_delay(value) for value in candidates)
    valid = tuple(value for value in parsed if value is not None)
    return valid or DEFAULT_MARKET_SCAN_AUTO_RETRY_DELAYS_SECONDS


def _positive_delay(value: object) -> float | None:
    try:
        parsed = float(str(value).strip())
    except (TypeError, ValueError):
        return None
    return parsed if math.isfinite(parsed) and parsed > 0 else None


def _run_finished_at(run: MarketScanRun) -> datetime | None:
    if not run.finished_at:
        return None
    try:
        return parse_text_time(run.finished_at)
    except ValueError:
        return None


__all__ = [
    "MarketScanAutomaticAction",
    "MarketScanRetryDecision",
    "automatic_retry_decision",
    "configured_auto_retry_policy",
    "effective_auto_retry_limit",
]
