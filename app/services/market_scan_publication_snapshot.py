from __future__ import annotations

from app.models.market_scan import MarketScanPublicationSummary


def snapshot_publication_blockers(
    summary: MarketScanPublicationSummary,
    *,
    max_span_seconds: float,
) -> tuple[str, ...]:
    blockers: list[str] = []
    cluster = summary.systemic_stale_cluster
    if cluster is not None:
        markets = "/".join(cluster.markets) or "unknown"
        blockers.append(
            "系统性同日滞后："
            f"{cluster.data_date} 有 {cluster.count}/{cluster.total_count} 只，涉及 {markets}"
        )
    if summary.invalid_snapshot_timestamps:
        examples = "、".join(summary.invalid_snapshot_timestamps[:3])
        blockers.append(
            f"报价时间不可解析：{len(summary.invalid_snapshot_timestamps)} 个（{examples}）"
        )
    if summary.snapshot_contract_version == "v6":
        blockers.extend(_strict_v6_snapshot_blockers(summary, max_span_seconds))
    else:
        span = summary.snapshot_span_seconds
        if span is not None and span > max_span_seconds:
            blockers.append(f"全市场报价快照跨度 {span:g} 秒超过 {max_span_seconds:g} 秒门槛")
    return tuple(blockers)


def _strict_v6_snapshot_blockers(
    summary: MarketScanPublicationSummary,
    max_span_seconds: float,
) -> tuple[str, ...]:
    blockers: list[str] = []
    if not summary.capture_sealed:
        blockers.append("v6 报价采集信封缺失或未封存")
    if summary.capture_count != summary.expected_capture_count:
        blockers.append(
            "v6 报价请求覆盖数与股票池不一致："
            f"{summary.capture_count}/{summary.expected_capture_count}"
        )
    if summary.missing_observed_count:
        blockers.append(f"v6 报价观测时间缺失：{summary.missing_observed_count} 只")
    if summary.invalid_observed_timestamps:
        examples = "、".join(summary.invalid_observed_timestamps[:3])
        blockers.append(
            f"v6 报价观测时间不可解析：{len(summary.invalid_observed_timestamps)} 个（{examples}）"
        )
    capture_seconds = (
        float(summary.capture_duration_ms) / 1000
        if summary.capture_duration_ms is not None
        else None
    )
    if capture_seconds is not None and capture_seconds > max_span_seconds:
        blockers.append(
            f"全市场报价采集耗时 {capture_seconds:g} 秒超过 {max_span_seconds:g} 秒门槛"
        )
    observed_span = summary.observed_span_seconds
    if observed_span is not None and observed_span > max_span_seconds:
        blockers.append(
            f"全市场报价观测跨度 {observed_span:g} 秒超过 {max_span_seconds:g} 秒门槛"
        )
    for market_span in summary.market_event_spans:
        span = market_span.span_seconds
        if span is not None and span > max_span_seconds:
            blockers.append(
                f"{market_span.market} 报价事件跨度 {span:g} 秒超过 {max_span_seconds:g} 秒门槛"
            )
    return tuple(blockers)


__all__ = ["snapshot_publication_blockers"]
