from __future__ import annotations

from app.market_scan_repository_contracts import (
    MARKET_SCAN_MAX_SNAPSHOT_SPAN_SECONDS,
    MARKET_SCAN_PUBLISH_MIN_COVERAGE,
    MARKET_SCAN_PUBLISH_MIN_ELIGIBLE_RATIO,
)
from app.models.market_scan import (
    MarketScanCoverageScope,
    MarketScanPublicationDiagnostic,
    MarketScanPublicationDiagnostics,
    MarketScanPublicationSummary,
    MarketScanRun,
    MarketScanRunStatus,
    MarketScanScoreDistribution,
    MarketScanScoreDistributionAssessment,
    MarketScanScoreDistributionPolicy,
    is_market_scan_top100_refresh_scope,
)
from app.services.market_scan_publication_snapshot import snapshot_publication_diagnostics


MARKET_SCAN_PUBLICATION_SCOPES: tuple[MarketScanCoverageScope, ...] = (
    "ALL",
    "SH",
    "SZ",
    "BJ",
)
MARKET_SCAN_SCORE_DISTRIBUTION_POLICY = MarketScanScoreDistributionPolicy()


def assess_market_scan_score_distribution(
    distribution: MarketScanScoreDistribution,
    *,
    policy: MarketScanScoreDistributionPolicy = MARKET_SCAN_SCORE_DISTRIBUTION_POLICY,
) -> MarketScanScoreDistributionAssessment:
    return policy.assess(distribution)


def completion_status(
    run: MarketScanRun,
    degraded_count: int = 0,
    *,
    publication_summary: MarketScanPublicationSummary | None = None,
    score_distribution: MarketScanScoreDistribution | None = None,
) -> tuple[MarketScanRunStatus, str]:
    scan_label = _scan_label(run)
    pending_count = max(0, run.total_count - run.processed_count)
    if pending_count:
        return "failed", f"{scan_label}尚有 {pending_count} 只待处理，不能发布"
    assessment = _distribution_assessment(score_distribution)
    blockers = list(
        publication_blockers(publication_summary)
        if publication_summary is not None
        else ()
    )
    if assessment.status == "failed":
        blockers.extend(assessment.reasons)
    if blockers:
        return "failed", (
            f"{scan_label}未达到发布可信度：发布阻断："
            + "；".join(blockers)
            + _score_distribution_audit_suffix(score_distribution, assessment)
        )
    if run.success_count == 0:
        return "failed", (
            f"{scan_label}没有生成有效排名；缺失 {run.missing_count}，跳过 {run.skipped_count}"
            + _score_distribution_audit_suffix(score_distribution, assessment)
        )
    return _successful_completion_status(
        run,
        degraded_count,
        scan_label=scan_label,
        score_distribution=score_distribution,
        assessment=assessment,
    )


def _successful_completion_status(
    run: MarketScanRun,
    degraded_count: int,
    *,
    scan_label: str,
    score_distribution: MarketScanScoreDistribution | None,
    assessment: MarketScanScoreDistributionAssessment,
) -> tuple[MarketScanRunStatus, str]:
    stale_stock_pool = run.stock_pool_source == "stale-fallback"
    distribution_degraded = assessment.status == "degraded"
    degraded = bool(
        run.missing_count
        or run.skipped_count
        or run.processed_count < run.total_count
        or degraded_count
        or stale_stock_pool
        or distribution_degraded
    )
    audit = _score_distribution_audit_suffix(score_distribution, assessment)
    if not degraded:
        return "success", f"{scan_label}完成：成功 {run.success_count}/{run.total_count}{audit}"
    eligible_count = max(0, run.total_count - run.skipped_count)
    details = _degraded_completion_details(
        degraded_count,
        stale_stock_pool=stale_stock_pool,
        assessment=assessment,
    )
    suffix = f"，{'，'.join(details)}" if details else ""
    return "degraded", (
        f"{scan_label}降级完成：有效覆盖 {run.success_count}/{eligible_count}，"
        f"缺失 {run.missing_count}，跳过 {run.skipped_count}{suffix}{audit}"
    )


def _degraded_completion_details(
    degraded_count: int,
    *,
    stale_stock_pool: bool,
    assessment: MarketScanScoreDistributionAssessment,
) -> list[str]:
    details: list[str] = []
    if degraded_count:
        details.append(f"降级结果 {degraded_count}")
    if stale_stock_pool:
        details.append("股票池使用本地缓存")
    if assessment.status == "degraded":
        details.append("评分分布退化：" + "、".join(assessment.reasons))
    return details


def completion_diagnostics(
    run: MarketScanRun,
    message: str,
    *,
    warnings: tuple[str, ...] = (),
    publication_summary: MarketScanPublicationSummary | None = None,
    score_distribution: MarketScanScoreDistribution | None = None,
) -> MarketScanPublicationDiagnostics:
    assessment = _distribution_assessment(score_distribution)
    blockers = _completion_blocker_diagnostics(
        run,
        publication_summary=publication_summary,
        distribution_assessment=assessment,
    )
    source_warnings = tuple(
        _source_warning(item)
        for item in tuple(dict.fromkeys(item.strip() for item in warnings if item.strip()))[:3]
    )
    audit_suffix = _score_distribution_audit_suffix(score_distribution, assessment)
    headline = message[: -len(audit_suffix)] if audit_suffix and message.endswith(audit_suffix) else message
    return MarketScanPublicationDiagnostics(
        headline=headline[:800],
        blockers=list(blockers),
        passed_gates=list(_passed_gate_diagnostics(score_distribution, assessment)),
        source_warnings=list(source_warnings),
    )


def _completion_blocker_diagnostics(
    run: MarketScanRun,
    *,
    publication_summary: MarketScanPublicationSummary | None,
    distribution_assessment: MarketScanScoreDistributionAssessment,
) -> tuple[MarketScanPublicationDiagnostic, ...]:
    pending_count = max(0, run.total_count - run.processed_count)
    if pending_count:
        return (
            _publication_blocker(
                "publication.pending_items",
                "仍有待处理股票",
                f"{_scan_label(run)}尚有 {pending_count} 只待处理，不能发布",
            ),
        )
    blockers = list(
        publication_diagnostics(publication_summary)
        if publication_summary is not None
        else ()
    )
    if distribution_assessment.status == "failed":
        blockers.extend(
            _publication_blocker(
                "score_distribution.blocked",
                "评分分布未通过",
                reason,
            )
            for reason in distribution_assessment.reasons
        )
    if blockers:
        return tuple(blockers)
    if run.success_count == 0:
        return (
            _publication_blocker(
                "publication.no_valid_ranking",
                "没有有效排名",
                f"没有生成有效排名；缺失 {run.missing_count}，跳过 {run.skipped_count}",
            ),
        )
    return ()


def _passed_gate_diagnostics(
    distribution: MarketScanScoreDistribution | None,
    assessment: MarketScanScoreDistributionAssessment,
) -> tuple[MarketScanPublicationDiagnostic, ...]:
    if distribution is None or assessment.status != "pass":
        return ()
    return (
        MarketScanPublicationDiagnostic(
            code="score_distribution.pass",
            label="评分分布",
            detail=distribution.audit_text().removeprefix("评分分布门禁 "),
            severity="info",
        ),
    )


def publication_blockers(summary: MarketScanPublicationSummary) -> tuple[str, ...]:
    return tuple(item.detail for item in publication_diagnostics(summary))


def publication_diagnostics(
    summary: MarketScanPublicationSummary,
) -> tuple[MarketScanPublicationDiagnostic, ...]:
    blockers = list(
        snapshot_publication_diagnostics(
            summary,
            max_span_seconds=MARKET_SCAN_MAX_SNAPSHOT_SPAN_SECONDS,
        )
    )
    for scope in MARKET_SCAN_PUBLICATION_SCOPES:
        blockers.extend(_scope_publication_diagnostics(summary, scope))
    return tuple(blockers)


def _scope_publication_diagnostics(
    summary: MarketScanPublicationSummary,
    scope: MarketScanCoverageScope,
) -> tuple[MarketScanPublicationDiagnostic, ...]:
    blockers: list[MarketScanPublicationDiagnostic] = []
    coverage = summary.coverage_for(scope)
    coverage_threshold = MARKET_SCAN_PUBLISH_MIN_COVERAGE[scope]
    if coverage is None or coverage.coverage_ratio < coverage_threshold:
        total = coverage.total_count if coverage is not None else 0
        success = coverage.success_count if coverage is not None else 0
        ratio = coverage.coverage_ratio if coverage is not None else 0.0
        blockers.append(
            _publication_blocker(
                "publication.coverage.insufficient",
                f"{scope} 发布覆盖不足",
                f"{scope} 发布覆盖不足：{success}/{total}"
                f"（{ratio:.2%}，门槛 {coverage_threshold:.2%}）",
            )
        )
    eligible_threshold = MARKET_SCAN_PUBLISH_MIN_ELIGIBLE_RATIO[scope]
    if coverage is None or coverage.eligible_ratio < eligible_threshold:
        eligible = coverage.total_count if coverage is not None else 0
        population = coverage.population_count if coverage is not None else 0
        ratio = coverage.eligible_ratio if coverage is not None else 0.0
        blockers.append(
            _publication_blocker(
                "publication.eligible_ratio.insufficient",
                f"{scope} 有效样本占比不足",
                f"{scope} 有效样本占比不足：{eligible}/{population}"
                f"（{ratio:.2%}，门槛 {eligible_threshold:.2%}）",
            )
        )
    return tuple(blockers)


def _distribution_assessment(
    distribution: MarketScanScoreDistribution | None,
) -> MarketScanScoreDistributionAssessment:
    if distribution is None:
        return MarketScanScoreDistributionAssessment("not-evaluated")
    return assess_market_scan_score_distribution(distribution)


def _score_distribution_audit_suffix(
    distribution: MarketScanScoreDistribution | None,
    assessment: MarketScanScoreDistributionAssessment,
) -> str:
    if distribution is None:
        return ""
    label = "已通过" if assessment.status == "pass" else "评分分布审计"
    return f"；{label}：{distribution.audit_text()}"


def _scan_label(run: MarketScanRun) -> str:
    if is_market_scan_top100_refresh_scope(run.scope):
        return "TOP100 快速更新评分"
    return {
        "intraday": "盘中临时扫描",
        "preopen": "盘前复盘扫描",
        "official": "盘后正式扫描",
    }[run.mode]


def _source_warning(detail: str) -> MarketScanPublicationDiagnostic:
    return MarketScanPublicationDiagnostic(
        code="source.runtime_warning",
        label="数据源告警",
        detail=detail[:800],
        severity="warning",
    )


def _publication_blocker(
    code: str,
    label: str,
    detail: str,
) -> MarketScanPublicationDiagnostic:
    return MarketScanPublicationDiagnostic(
        code=code,
        label=label[:80],
        detail=detail[:800],
        severity="error",
    )


__all__ = [
    "MARKET_SCAN_MAX_SNAPSHOT_SPAN_SECONDS",
    "MARKET_SCAN_PUBLISH_MIN_COVERAGE",
    "MARKET_SCAN_PUBLISH_MIN_ELIGIBLE_RATIO",
    "MARKET_SCAN_SCORE_DISTRIBUTION_POLICY",
    "assess_market_scan_score_distribution",
    "completion_diagnostics",
    "completion_status",
    "publication_blockers",
    "publication_diagnostics",
]
