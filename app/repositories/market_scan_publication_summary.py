from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
import sqlite3
from typing import Literal

from app.models.market_scan import (
    MarketScanCoverage,
    MarketScanMarketEventSpan,
    MarketScanPublicationSummary,
    MarketScanStaleCluster,
)
from app.utils.audit_time import audit_datetime_to_text, parse_audit_time
from app.utils.market_time import normalize_market_datetime


@dataclass(frozen=True)
class _PublicationTimeSummary:
    event_rows: list[tuple[str, str]]
    snapshot_started_at: str | None
    snapshot_finished_at: str | None
    snapshot_span_seconds: float | None
    invalid_snapshot_timestamps: tuple[str, ...]
    observed_started_at: str | None
    observed_finished_at: str | None
    observed_span_seconds: float | None
    observed_count: int
    missing_observed_count: int
    invalid_observed_timestamps: tuple[str, ...]


@dataclass(frozen=True)
class _CaptureSummary:
    contract_version: Literal["v5-legacy", "v6"]
    started_at: str | None
    finished_at: str | None
    duration_ms: int | None
    count: int
    sealed: bool


def publication_summary_from_evidence(
    run: sqlite3.Row,
    coverages: tuple[MarketScanCoverage, ...],
    stale_cluster: MarketScanStaleCluster | None,
    timestamp_rows: list[tuple[str, str | None, str | None]],
) -> MarketScanPublicationSummary:
    times = _publication_time_summary(timestamp_rows)
    capture = _capture_summary(run)
    total_count = int(run["total_count"] or 0)
    return MarketScanPublicationSummary(
        coverages=coverages,
        systemic_stale_cluster=stale_cluster,
        snapshot_contract_version=capture.contract_version,
        expected_capture_count=total_count,
        capture_started_at=capture.started_at,
        capture_finished_at=capture.finished_at,
        capture_duration_ms=capture.duration_ms,
        capture_count=capture.count,
        capture_sealed=capture.sealed,
        observed_started_at=times.observed_started_at,
        observed_finished_at=times.observed_finished_at,
        observed_span_seconds=times.observed_span_seconds,
        observed_count=times.observed_count,
        missing_observed_count=times.missing_observed_count,
        invalid_observed_timestamps=times.invalid_observed_timestamps,
        market_event_spans=_market_event_spans(times.event_rows),
        snapshot_started_at=times.snapshot_started_at,
        snapshot_finished_at=times.snapshot_finished_at,
        snapshot_span_seconds=times.snapshot_span_seconds,
        invalid_snapshot_timestamps=times.invalid_snapshot_timestamps,
    )


def _publication_time_summary(
    timestamp_rows: list[tuple[str, str | None, str | None]],
) -> _PublicationTimeSummary:
    event_rows = [(market, value) for market, value, _observed in timestamp_rows if value is not None]
    snapshot_started, snapshot_finished, snapshot_span, invalid_snapshot = _snapshot_span(event_rows)
    observed_values = [observed for _market, _timestamp, observed in timestamp_rows]
    observed_started, observed_finished, observed_span, invalid_observed = _observed_span(
        [value for value in observed_values if value is not None]
    )
    return _PublicationTimeSummary(
        event_rows=event_rows,
        snapshot_started_at=snapshot_started,
        snapshot_finished_at=snapshot_finished,
        snapshot_span_seconds=snapshot_span,
        invalid_snapshot_timestamps=invalid_snapshot,
        observed_started_at=observed_started,
        observed_finished_at=observed_finished,
        observed_span_seconds=observed_span,
        observed_count=sum(value is not None for value in observed_values),
        missing_observed_count=sum(value is None for value in observed_values),
        invalid_observed_timestamps=invalid_observed,
    )


def _capture_summary(run: sqlite3.Row) -> _CaptureSummary:
    started_at = run["quote_capture_started_at"]
    finished_at = run["quote_capture_finished_at"]
    duration_ms = run["quote_capture_duration_ms"]
    strict_v6 = str(run["rule_version"] or "").startswith("full-market-scan-v6:")
    return _CaptureSummary(
        contract_version="v6" if strict_v6 else "v5-legacy",
        started_at=str(started_at) if started_at is not None else None,
        finished_at=str(finished_at) if finished_at is not None else None,
        duration_ms=int(duration_ms) if duration_ms is not None else None,
        count=int(run["quote_capture_count"] or 0),
        sealed=_capture_envelope_sealed(started_at, finished_at, duration_ms),
    )


def _snapshot_span(
    timestamp_rows: list[tuple[str, str]],
) -> tuple[str | None, str | None, float | None, tuple[str, ...]]:
    parsed_times: list[datetime] = []
    invalid: list[str] = []
    for _market, value in timestamp_rows:
        snapshot_time = _parse_snapshot_time(value)
        if snapshot_time is None:
            invalid.append(value)
        else:
            parsed_times.append(snapshot_time)
    if not parsed_times:
        return None, None, None, tuple(dict.fromkeys(invalid))
    started = min(parsed_times)
    finished = max(parsed_times)
    return (
        started.isoformat(sep=" "),
        finished.isoformat(sep=" "),
        max(0.0, (finished - started).total_seconds()),
        tuple(dict.fromkeys(invalid)),
    )


def _market_event_spans(
    timestamp_rows: list[tuple[str, str]],
) -> tuple[MarketScanMarketEventSpan, ...]:
    spans: list[MarketScanMarketEventSpan] = []
    markets: tuple[Literal["SH", "SZ", "BJ"], ...] = ("SH", "SZ", "BJ")
    for market in markets:
        rows: list[tuple[str, str]] = [
            (market, value) for row_market, value in timestamp_rows if row_market == market
        ]
        started_at, finished_at, span_seconds, invalid = _snapshot_span(rows)
        spans.append(
            MarketScanMarketEventSpan(
                market=market,
                started_at=started_at,
                finished_at=finished_at,
                span_seconds=span_seconds,
                invalid_timestamps=invalid,
            )
        )
    return tuple(spans)


def _observed_span(
    values: list[str],
) -> tuple[str | None, str | None, float | None, tuple[str, ...]]:
    parsed_times: list[datetime] = []
    invalid: list[str] = []
    for value in values:
        try:
            parsed_times.append(parse_audit_time(value))
        except (TypeError, ValueError):
            invalid.append(value)
    if not parsed_times:
        return None, None, None, tuple(dict.fromkeys(invalid))
    started = min(parsed_times)
    finished = max(parsed_times)
    return (
        audit_datetime_to_text(started),
        audit_datetime_to_text(finished),
        max(0.0, (finished - started).total_seconds()),
        tuple(dict.fromkeys(invalid)),
    )


def _capture_envelope_sealed(
    started_at: object,
    finished_at: object,
    duration_ms: object,
) -> bool:
    if started_at is None or finished_at is None or duration_ms is None:
        return False
    try:
        return int(str(duration_ms)) >= 0 and parse_audit_time(str(finished_at)) >= parse_audit_time(str(started_at))
    except (TypeError, ValueError):
        return False


def _parse_snapshot_time(value: object) -> datetime | None:
    normalized = normalize_market_datetime(value)
    if normalized is None:
        return None
    try:
        return datetime.fromisoformat(normalized)
    except ValueError:
        return None


__all__ = ["publication_summary_from_evidence"]
