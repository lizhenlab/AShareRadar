from __future__ import annotations

from app.models.market_scan import (
    MarketScanPublicationDiagnostic,
    MarketScanPublicationSummary,
)


def snapshot_publication_blockers(
    summary: MarketScanPublicationSummary,
    *,
    max_span_seconds: float,
) -> tuple[str, ...]:
    return tuple(
        diagnostic.detail
        for diagnostic in snapshot_publication_diagnostics(
            summary,
            max_span_seconds=max_span_seconds,
        )
    )


def snapshot_publication_diagnostics(
    summary: MarketScanPublicationSummary,
    *,
    max_span_seconds: float,
) -> tuple[MarketScanPublicationDiagnostic, ...]:
    blockers: list[MarketScanPublicationDiagnostic] = []
    cluster = summary.systemic_stale_cluster
    if cluster is not None:
        markets = "/".join(cluster.markets) or "unknown"
        blockers.append(
            _blocker(
                "publication.snapshot.systemic_stale_cluster",
                "系统性同日滞后",
                "系统性同日滞后："
                f"{cluster.data_date} 有 {cluster.count}/{cluster.total_count} 只，涉及 {markets}",
            )
        )
    if summary.invalid_snapshot_timestamps:
        examples = "、".join(summary.invalid_snapshot_timestamps[:3])
        blockers.append(
            _blocker(
                "publication.snapshot.invalid_timestamp",
                "报价时间不可解析",
                f"报价时间不可解析：{len(summary.invalid_snapshot_timestamps)} 个（{examples}）",
            )
        )
    if summary.snapshot_contract_version == "v6":
        blockers.extend(_strict_v6_snapshot_diagnostics(summary, max_span_seconds))
    else:
        span = summary.snapshot_span_seconds
        if span is not None and span > max_span_seconds:
            blockers.append(
                _blocker(
                    "publication.snapshot.span_exceeded",
                    "报价快照跨度超限",
                    f"全市场报价快照跨度 {span:g} 秒超过 {max_span_seconds:g} 秒门槛",
                )
            )
    return tuple(blockers)


def _strict_v6_snapshot_diagnostics(
    summary: MarketScanPublicationSummary,
    max_span_seconds: float,
) -> tuple[MarketScanPublicationDiagnostic, ...]:
    return (
        *_strict_v6_envelope_diagnostics(summary),
        *_strict_v6_span_diagnostics(summary, max_span_seconds),
    )


def _strict_v6_envelope_diagnostics(
    summary: MarketScanPublicationSummary,
) -> tuple[MarketScanPublicationDiagnostic, ...]:
    blockers: list[MarketScanPublicationDiagnostic] = []
    if not summary.capture_sealed:
        blockers.append(
            _blocker(
                "publication.snapshot.capture_unsealed",
                "报价采集信封未封存",
                "v6 报价采集信封缺失或未封存",
            )
        )
    if summary.capture_count != summary.expected_capture_count:
        blockers.append(
            _blocker(
                "publication.snapshot.capture_count_mismatch",
                "报价请求覆盖数不一致",
                "v6 报价请求覆盖数与股票池不一致："
                f"{summary.capture_count}/{summary.expected_capture_count}",
            )
        )
    if summary.missing_observed_count:
        blockers.append(
            _blocker(
                "publication.snapshot.observed_timestamp_missing",
                "报价观测时间缺失",
                f"v6 报价观测时间缺失：{summary.missing_observed_count} 只",
            )
        )
    if summary.invalid_observed_timestamps:
        examples = "、".join(summary.invalid_observed_timestamps[:3])
        blockers.append(
            _blocker(
                "publication.snapshot.observed_timestamp_invalid",
                "报价观测时间不可解析",
                f"v6 报价观测时间不可解析：{len(summary.invalid_observed_timestamps)} 个（{examples}）",
            )
        )
    return tuple(blockers)


def _strict_v6_span_diagnostics(
    summary: MarketScanPublicationSummary,
    max_span_seconds: float,
) -> tuple[MarketScanPublicationDiagnostic, ...]:
    blockers: list[MarketScanPublicationDiagnostic] = []
    capture_seconds = (
        float(summary.capture_duration_ms) / 1000
        if summary.capture_duration_ms is not None
        else None
    )
    if capture_seconds is not None and capture_seconds > max_span_seconds:
        blockers.append(
            _blocker(
                "publication.snapshot.capture_duration_exceeded",
                "报价采集耗时超限",
                f"全市场报价采集耗时 {capture_seconds:g} 秒超过 {max_span_seconds:g} 秒门槛",
            )
        )
    observed_span = summary.observed_span_seconds
    if observed_span is not None and observed_span > max_span_seconds:
        blockers.append(
            _blocker(
                "publication.snapshot.observed_span_exceeded",
                "报价观测跨度超限",
                f"全市场报价观测跨度 {observed_span:g} 秒超过 {max_span_seconds:g} 秒门槛",
            )
        )
    for market_span in summary.market_event_spans:
        span = market_span.span_seconds
        if span is not None and span > max_span_seconds:
            blockers.append(
                _blocker(
                    "publication.snapshot.market_event_span_exceeded",
                    "市场报价事件跨度超限",
                    f"{market_span.market} 报价事件跨度 {span:g} 秒超过 {max_span_seconds:g} 秒门槛",
                )
            )
    return tuple(blockers)


def _blocker(code: str, label: str, detail: str) -> MarketScanPublicationDiagnostic:
    return MarketScanPublicationDiagnostic(
        code=code,
        label=label[:80],
        detail=detail[:800],
        severity="error",
    )


__all__ = ["snapshot_publication_blockers", "snapshot_publication_diagnostics"]
