"""Contracts for full-market A-share scan runs and ranked results."""

from __future__ import annotations

from collections import Counter
from collections.abc import Iterable, Sequence
from dataclasses import asdict, dataclass, field
from datetime import datetime
from math import isfinite
from typing import Final, Literal

from pydantic import BaseModel, ConfigDict, Field, model_validator


MarketScanRunStatus = Literal[
    "queued",
    "running",
    "cancelling",
    "success",
    "degraded",
    "failed",
    "cancelled",
    "interrupted",
]
MarketScanResultStatus = Literal["pending", "success", "missing", "skipped"]
MarketScanTrigger = Literal["manual", "scheduled", "retry"]
MarketScanMode = Literal["official", "intraday", "preopen"]
MarketScanDiagnosticSeverity = Literal["info", "warning", "error"]
MarketScanSort = Literal[
    "rank",
    "score",
    "raw_score",
    "trend_score",
    "change_pct",
    "amount",
    "turnover_rate",
    "data_quality_score",
    "alpha_5d",
    "confidence",
    "risk",
    "tradability",
    "symbol",
]
MarketScanSortOrder = Literal["asc", "desc"]
MarketScanCoverageScope = Literal["ALL", "SH", "SZ", "BJ"]
MarketScanStage = Literal[
    "stock_pool",
    "bulk_quotes",
    "klines",
    "scoring",
    "persistence",
    "publication",
]
MarketScanFilterValues = str | Sequence[str] | None
MarketScanSortValues = MarketScanSort | Sequence[MarketScanSort]
MarketScanSortOrderValues = MarketScanSortOrder | Sequence[MarketScanSortOrder]

MARKET_SCAN_RANK_TIE_BREAK: Final[tuple[tuple[str, str], ...]] = (
    ("raw_score", "desc"),
    ("symbol", "asc"),
)
MARKET_SCAN_METADATA_DEGRADATION_REASONS: Final[frozenset[str]] = frozenset(
    {"industry_missing", "list_date_missing", "metadata_incomplete"}
)
MARKET_SCAN_DEGRADATION_REASONS: Final[frozenset[str]] = frozenset(
    {"quote_fallback", "kline_fallback", *MARKET_SCAN_METADATA_DEGRADATION_REASONS}
)
MARKET_SCAN_TOP100_REFRESH_SCOPE: Final[str] = "TOP100快速更新评分"
MARKET_SCAN_TOP100_REFRESH_LIMIT: Final[int] = 100
MARKET_SCAN_PUBLICATION_DIAGNOSTICS_SCHEMA_VERSION: Final[str] = (
    "market-scan-publication-diagnostics-v1"
)
MARKET_SCAN_PUBLICATION_DIAGNOSTIC_SEVERITIES: Final[tuple[str, ...]] = (
    "info",
    "warning",
    "error",
)


def is_market_scan_top100_refresh_scope(value: object) -> bool:
    return str(value or "").strip() == MARKET_SCAN_TOP100_REFRESH_SCOPE


@dataclass(frozen=True)
class MarketScanSeed:
    symbol: str
    code: str
    market: str
    name: str
    industry: str | None = None
    list_date: str | None = None
    is_st: bool = False
    is_new: bool = False
    metadata_source: str | None = None


@dataclass(frozen=True)
class MarketScanResultWrite:
    symbol: str
    status: MarketScanResultStatus
    score: int | None = None
    raw_score: float | None = None
    trend_score: int | None = None
    leader_score: int | None = None
    data_quality_score: int | None = None
    price: float | None = None
    change_pct: float | None = None
    turnover_rate: float | None = None
    volume_ratio: float | None = None
    amount: float | None = None
    tags: tuple[str, ...] = ()
    metrics: dict[str, float] = field(default_factory=dict)
    score_details: dict[str, object] = field(default_factory=dict)
    reason: str | None = None
    error: str | None = None
    data_date: str | None = None
    quote_timestamp: str | None = None
    quote_observed_at: str | None = None
    quote_source: str | None = None
    kline_source: str | None = None
    adjustment_mode: str | None = None
    quote_fallback_used: bool = False
    kline_fallback_used: bool = False
    metadata_degraded: bool = False
    degradation_reasons: tuple[str, ...] = ()


@dataclass(frozen=True)
class MarketScanRetryPlan:
    source_run_id: int
    result_count: int
    preserved_success_count: int
    pending_count: int
    needs_market_data: bool
    rule_version: str | None = None


@dataclass(frozen=True)
class MarketScanCoverage:
    scope: MarketScanCoverageScope
    total_count: int
    success_count: int
    missing_count: int = 0
    skipped_count: int = 0

    @property
    def coverage_ratio(self) -> float:
        if self.total_count <= 0:
            return 0.0
        return min(1.0, max(0.0, self.success_count / self.total_count))

    @property
    def population_count(self) -> int:
        return self.total_count + self.skipped_count

    @property
    def eligible_ratio(self) -> float:
        if self.population_count <= 0:
            return 0.0
        return min(1.0, max(0.0, self.total_count / self.population_count))


@dataclass(frozen=True)
class MarketScanStaleCluster:
    data_date: str
    count: int
    markets: tuple[str, ...]
    total_count: int

    @property
    def ratio(self) -> float:
        if self.total_count <= 0:
            return 0.0
        return min(1.0, max(0.0, self.count / self.total_count))


@dataclass(frozen=True)
class MarketScanMarketEventSpan:
    market: Literal["SH", "SZ", "BJ"]
    started_at: str | None = None
    finished_at: str | None = None
    span_seconds: float | None = None
    invalid_timestamps: tuple[str, ...] = ()


@dataclass(frozen=True)
class MarketScanPublicationSummary:
    coverages: tuple[MarketScanCoverage, ...]
    systemic_stale_cluster: MarketScanStaleCluster | None = None
    snapshot_contract_version: Literal["v5-legacy", "v6"] = "v5-legacy"
    expected_capture_count: int = 0
    capture_started_at: str | None = None
    capture_finished_at: str | None = None
    capture_duration_ms: int | None = None
    capture_count: int = 0
    capture_sealed: bool = False
    observed_started_at: str | None = None
    observed_finished_at: str | None = None
    observed_span_seconds: float | None = None
    observed_count: int = 0
    missing_observed_count: int = 0
    invalid_observed_timestamps: tuple[str, ...] = ()
    market_event_spans: tuple[MarketScanMarketEventSpan, ...] = ()
    snapshot_started_at: str | None = None
    snapshot_finished_at: str | None = None
    snapshot_span_seconds: float | None = None
    invalid_snapshot_timestamps: tuple[str, ...] = ()

    def coverage_for(self, scope: MarketScanCoverageScope) -> MarketScanCoverage | None:
        return next((item for item in self.coverages if item.scope == scope), None)


MarketScanScoreDistributionGateStatus = Literal["not-evaluated", "pass", "degraded", "failed"]


@dataclass(frozen=True)
class MarketScanScoreDistributionPolicy:
    version: str = "raw-score-distribution-v2"
    raw_score_decimals: int = 6
    top_size: int = 100
    minimum_sample_count: int = 100
    failed_observed_ratio_below: float = 0.95
    degraded_observed_ratio_below: float = 0.99
    failed_max_tie_group_ratio_at_least: float = 0.95
    failed_saturation_ratio_at_least: float = 0.95
    failed_top100_upper_saturation_ratio_at_least: float = 1.0
    degraded_distinct_ratio_at_most: float = 0.02
    degraded_max_tie_group_ratio_at_least: float = 0.25
    degraded_single_tie_group_ratio_at_least: float = 0.50
    degraded_saturation_ratio_at_least: float = 0.50
    degraded_top100_tie_ratio_at_least: float = 0.50

    def spec(self) -> dict[str, object]:
        return asdict(self)

    def assess(self, distribution: MarketScanScoreDistribution) -> MarketScanScoreDistributionAssessment:
        if distribution.expected_count < self.minimum_sample_count:
            return MarketScanScoreDistributionAssessment("not-evaluated")
        failed_reasons = self._failure_reasons(distribution)
        if failed_reasons:
            return MarketScanScoreDistributionAssessment("failed", failed_reasons)
        degraded_reasons = self._degraded_reasons(distribution)
        if degraded_reasons:
            return MarketScanScoreDistributionAssessment("degraded", degraded_reasons)
        return MarketScanScoreDistributionAssessment("pass")

    def _failure_reasons(self, distribution: MarketScanScoreDistribution) -> tuple[str, ...]:
        reasons: list[str] = []
        if distribution.sample_count < self.minimum_sample_count or distribution.observed_ratio < self.failed_observed_ratio_below:
            reasons.append(
                f"raw_score 可审计样本不足：{distribution.sample_count}/{distribution.expected_count}"
                f"（{distribution.observed_ratio:.2%}）"
            )
        if distribution.distinct_count == 1:
            reasons.append("成功结果 raw_score 全部相同")
        elif distribution.max_tie_group_ratio >= self.failed_max_tie_group_ratio_at_least:
            reasons.append(f"最大并列组占比 {distribution.max_tie_group_ratio:.2%}，接近常量分")
        if distribution.saturation_ratio >= self.failed_saturation_ratio_at_least:
            reasons.append(f"0/100 饱和率 {distribution.saturation_ratio:.2%}，评分大面积触及边界")
        if distribution.top100_count >= self.top_size and distribution.top100_upper_saturation_ratio >= self.failed_top100_upper_saturation_ratio_at_least:
            reasons.append("前100名 raw_score 全部饱和在 100")
        return tuple(dict.fromkeys(reasons))

    def _degraded_reasons(self, distribution: MarketScanScoreDistribution) -> tuple[str, ...]:
        reasons: list[str] = []
        if distribution.observed_ratio < self.degraded_observed_ratio_below:
            reasons.append(f"raw_score 可审计样本仅覆盖 {distribution.observed_ratio:.2%}")
        if (
            distribution.distinct_raw_score_ratio <= self.degraded_distinct_ratio_at_most
            and distribution.max_tie_group_ratio >= self.degraded_max_tie_group_ratio_at_least
        ):
            reasons.append(
                f"distinct raw score ratio 仅 {distribution.distinct_raw_score_ratio:.2%}，"
                f"最大并列组占比 {distribution.max_tie_group_ratio:.2%}"
            )
        elif distribution.max_tie_group_ratio >= self.degraded_single_tie_group_ratio_at_least:
            reasons.append(f"最大并列组占比达到 {distribution.max_tie_group_ratio:.2%}")
        if distribution.saturation_ratio >= self.degraded_saturation_ratio_at_least:
            reasons.append(f"0/100 饱和率达到 {distribution.saturation_ratio:.2%}")
        if distribution.top100_tie_ratio >= self.degraded_top100_tie_ratio_at_least:
            reasons.append(f"前100并列占比达到 {distribution.top100_tie_ratio:.2%}")
        return tuple(dict.fromkeys(reasons))


@dataclass(frozen=True)
class MarketScanScoreDistribution:
    policy_version: str
    expected_count: int
    sample_count: int
    distinct_count: int
    distinct_raw_score_ratio: float
    max_tie_group_count: int
    max_tie_group_ratio: float
    saturation_count: int
    saturation_ratio: float
    top100_count: int
    top100_max_tie_group_count: int
    top100_tied_count: int
    top100_tie_ratio: float
    top100_upper_saturation_ratio: float

    @classmethod
    def from_raw_scores(
        cls,
        raw_scores: Iterable[object],
        *,
        expected_count: int,
        policy: MarketScanScoreDistributionPolicy,
    ) -> "MarketScanScoreDistribution":
        values = sorted(
            (
                round(parsed, policy.raw_score_decimals)
                for value in raw_scores
                if (parsed := _parse_raw_score(value)) is not None
            ),
            reverse=True,
        )
        counts = Counter(values)
        sample_count = len(values)
        max_tie_count = max(counts.values(), default=0)
        saturation_count = sum(counts.get(boundary, 0) for boundary in (0.0, 100.0))
        top_values = values[: policy.top_size]
        top_counts = Counter(top_values)
        top_count = len(top_values)
        top_max_tie_count = max(top_counts.values(), default=0)
        top_tied_count = sum(1 for value in top_values if counts[value] > 1)
        return cls(
            policy_version=policy.version,
            expected_count=max(0, expected_count),
            sample_count=sample_count,
            distinct_count=len(counts),
            distinct_raw_score_ratio=_safe_ratio(len(counts), sample_count),
            max_tie_group_count=max_tie_count,
            max_tie_group_ratio=_safe_ratio(max_tie_count, sample_count),
            saturation_count=saturation_count,
            saturation_ratio=_safe_ratio(saturation_count, sample_count),
            top100_count=top_count,
            top100_max_tie_group_count=top_max_tie_count,
            top100_tied_count=top_tied_count,
            top100_tie_ratio=_safe_ratio(top_tied_count, top_count),
            top100_upper_saturation_ratio=_safe_ratio(top_counts.get(100.0, 0), top_count),
        )

    @property
    def observed_ratio(self) -> float:
        return _safe_ratio(self.sample_count, self.expected_count)

    def audit_text(self) -> str:
        return (
            f"评分分布门禁 {self.policy_version}：raw_score样本 {self.sample_count}/{self.expected_count}，"
            f"distinct ratio {self.distinct_raw_score_ratio:.2%}，"
            f"最大并列组 {self.max_tie_group_count}/{self.sample_count}（{self.max_tie_group_ratio:.2%}），"
            f"0/100饱和 {self.saturation_count}/{self.sample_count}（{self.saturation_ratio:.2%}），"
            f"前100并列 {self.top100_tied_count}/{self.top100_count}（{self.top100_tie_ratio:.2%}），"
            f"最大组 {self.top100_max_tie_group_count}"
        )


@dataclass(frozen=True)
class MarketScanScoreDistributionAssessment:
    status: MarketScanScoreDistributionGateStatus
    reasons: tuple[str, ...] = ()


def _parse_raw_score(value: object) -> float | None:
    if isinstance(value, bool) or not isinstance(value, int | float):
        return None
    parsed = float(value)
    return parsed if isfinite(parsed) and 0 <= parsed <= 100 else None


def _safe_ratio(numerator: int, denominator: int) -> float:
    if denominator <= 0:
        return 0.0
    return min(1.0, max(0.0, numerator / denominator))


class MarketScanStartRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    as_of: datetime | None = None
    mode: MarketScanMode = "official"


class MarketScanStageMetric(BaseModel):
    duration_ms: int = Field(default=0, ge=0)
    work_duration_ms: int = Field(default=0, ge=0)
    calls: int = Field(default=0, ge=0)
    items: int = Field(default=0, ge=0)


class MarketScanMarketProgress(BaseModel):
    market: Literal["SH", "SZ", "BJ"]
    total_count: int = Field(default=0, ge=0)
    processed_count: int = Field(default=0, ge=0)
    success_count: int = Field(default=0, ge=0)
    missing_count: int = Field(default=0, ge=0)
    skipped_count: int = Field(default=0, ge=0)
    coverage_pct: float = Field(default=0, ge=0, le=100)


class MarketScanPublicationDiagnostic(BaseModel):
    """Stable machine-readable diagnostic with user-facing Chinese copy."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    code: str = Field(pattern=r"^[a-z][a-z0-9_.-]*$")
    label: str = Field(min_length=1, max_length=80)
    detail: str = Field(min_length=1, max_length=800)
    severity: MarketScanDiagnosticSeverity


class MarketScanPublicationDiagnostics(BaseModel):
    """Typed publication evidence; absent on legacy or non-publication terminal runs."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    schema_version: Literal["market-scan-publication-diagnostics-v1"] = (
        "market-scan-publication-diagnostics-v1"
    )
    headline: str = Field(min_length=1, max_length=800)
    blockers: list[MarketScanPublicationDiagnostic] = Field(default_factory=list)
    passed_gates: list[MarketScanPublicationDiagnostic] = Field(default_factory=list)
    source_warnings: list[MarketScanPublicationDiagnostic] = Field(default_factory=list)


class MarketScanRun(BaseModel):
    id: int
    task_run_id: int | None = None
    retry_of_run_id: int | None = Field(default=None, ge=1)
    status: MarketScanRunStatus
    trigger: MarketScanTrigger
    mode: MarketScanMode = "official"
    rule_version: str
    as_of: str
    data_date: str
    quote_date: str = ""
    scope: str
    stock_pool_source: str | None = None
    total_count: int = Field(ge=0)
    excluded_count: int = Field(ge=0)
    processed_count: int = Field(ge=0)
    success_count: int = Field(ge=0)
    missing_count: int = Field(ge=0)
    skipped_count: int = Field(ge=0)
    retry_count: int = Field(ge=0)
    progress_pct: float = Field(ge=0, le=100)
    coverage_pct: float = Field(ge=0, le=100)
    created_at: str
    updated_at: str
    started_at: str | None = None
    finished_at: str | None = None
    duration_ms: int | None = Field(default=None, ge=0)
    quote_capture_started_at: str | None = None
    quote_capture_finished_at: str | None = None
    quote_capture_duration_ms: int | None = Field(default=None, ge=0)
    quote_capture_count: int = Field(default=0, ge=0)
    current_stage: MarketScanStage | None = None
    stage_started_at: str | None = None
    stage_metrics: dict[MarketScanStage, MarketScanStageMetric] = Field(default_factory=dict)
    market_progress: list[MarketScanMarketProgress] = Field(default_factory=list)
    elapsed_seconds: float | None = Field(default=None, ge=0)
    throughput_per_second: float | None = Field(default=None, ge=0)
    eta_seconds: float | None = Field(default=None, ge=0)
    message: str | None = None
    last_error: str | None = None
    publication_diagnostics: MarketScanPublicationDiagnostics | None = None
    cancel_requested_at: str | None = None

    @model_validator(mode="after")
    def default_quote_date_to_data_date(self) -> "MarketScanRun":
        if not self.quote_date:
            self.quote_date = self.data_date
        return self


class MarketScanStartResponse(BaseModel):
    accepted: bool
    deduplicated: bool = False
    run: MarketScanRun


class MarketScanResultItem(BaseModel):
    run_id: int
    symbol: str
    code: str
    market: str
    name: str
    industry: str | None = None
    list_date: str | None = None
    is_st: bool = False
    is_new: bool = False
    metadata_source: str | None = None
    status: MarketScanResultStatus
    rank: int | None = Field(default=None, ge=1)
    score: int | None = Field(default=None, ge=0, le=100)
    raw_score: float | None = Field(default=None, ge=0, le=100, allow_inf_nan=False)
    trend_score: int | None = Field(default=None, ge=0, le=100)
    leader_score: int | None = Field(default=None, ge=0, le=100)
    data_quality_score: int | None = Field(default=None, ge=0, le=100)
    price: float | None = None
    change_pct: float | None = None
    turnover_rate: float | None = None
    volume_ratio: float | None = None
    amount: float | None = None
    tags: list[str] = Field(default_factory=list)
    metrics: dict[str, float] = Field(default_factory=dict)
    score_details: dict[str, object] = Field(default_factory=dict)
    reason: str | None = None
    error: str | None = None
    data_date: str | None = None
    quote_timestamp: str | None = None
    quote_observed_at: str | None = None
    quote_source: str | None = None
    kline_source: str | None = None
    adjustment_mode: str | None = None
    quote_fallback_used: bool = False
    kline_fallback_used: bool = False
    metadata_degraded: bool = False
    degradation_reasons: list[str] = Field(default_factory=list)
    upside_probabilities: dict[str, dict[str, dict[str, object]]] = Field(default_factory=dict)
    updated_at: str


class MarketScanResultPage(BaseModel):
    run: MarketScanRun
    items: list[MarketScanResultItem]
    total: int = Field(ge=0)
    page: int = Field(ge=1)
    page_size: int = Field(ge=1)
    page_count: int = Field(ge=0)
    probability_research: dict[str, object] | None = None


class MarketScanFutureRangeArtifactSummary(BaseModel):
    model_config = ConfigDict(extra="forbid")

    schema_version: str
    generated_at: str
    integrity_digest: str


class MarketScanFutureRangeRecordPage(BaseModel):
    model_config = ConfigDict(extra="forbid")

    page: int = Field(ge=1)
    page_size: int = Field(ge=1, le=200)
    total: int = Field(ge=0)
    page_count: int = Field(ge=0)
    session_offset: Literal[1, 2, 3] | None = None
    symbol: str | None = None
    items: list[dict[str, object]] = Field(default_factory=list)


class MarketScanFutureRangeResearchResponse(BaseModel):
    model_config = ConfigDict(extra="forbid")

    schema_version: Literal["market-scan-future-range-api-v1"]
    generation_status: Literal["ready", "not_generated", "insufficient_data"]
    artifact: MarketScanFutureRangeArtifactSummary | None = None
    research: dict[str, object] | None = None
    record_page: MarketScanFutureRangeRecordPage


class MarketScanRunPage(BaseModel):
    items: list[MarketScanRun]
    total: int = Field(ge=0)
    page: int = Field(ge=1)
    page_size: int = Field(ge=1)
    page_count: int = Field(ge=0)


__all__ = [
    "MARKET_SCAN_RANK_TIE_BREAK",
    "MARKET_SCAN_TOP100_REFRESH_LIMIT",
    "MARKET_SCAN_TOP100_REFRESH_SCOPE",
    "MARKET_SCAN_PUBLICATION_DIAGNOSTICS_SCHEMA_VERSION",
    "MARKET_SCAN_PUBLICATION_DIAGNOSTIC_SEVERITIES",
    "MarketScanCoverage",
    "MarketScanCoverageScope",
    "MarketScanDiagnosticSeverity",
    "MarketScanFilterValues",
    "MarketScanFutureRangeArtifactSummary",
    "MarketScanFutureRangeRecordPage",
    "MarketScanFutureRangeResearchResponse",
    "MarketScanMode",
    "MarketScanMarketProgress",
    "MarketScanPublicationDiagnostic",
    "MarketScanPublicationDiagnostics",
    "MarketScanPublicationSummary",
    "MarketScanScoreDistribution",
    "MarketScanScoreDistributionAssessment",
    "MarketScanScoreDistributionGateStatus",
    "MarketScanScoreDistributionPolicy",
    "MarketScanResultItem",
    "MarketScanResultPage",
    "MarketScanResultStatus",
    "MarketScanResultWrite",
    "MarketScanRetryPlan",
    "MarketScanRun",
    "MarketScanRunPage",
    "MarketScanRunStatus",
    "MarketScanSort",
    "MarketScanSortOrder",
    "MarketScanSortOrderValues",
    "MarketScanSortValues",
    "MarketScanStage",
    "MarketScanStageMetric",
    "MarketScanStartRequest",
    "MarketScanStartResponse",
    "MarketScanStaleCluster",
    "MarketScanSeed",
    "MarketScanTrigger",
    "is_market_scan_top100_refresh_scope",
]
