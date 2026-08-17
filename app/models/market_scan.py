"""Contracts for full-market A-share scan runs and ranked results."""

from __future__ import annotations

import re
from collections import Counter
from collections.abc import Iterable, Sequence
from dataclasses import asdict, dataclass, field, replace
from datetime import date, datetime
from math import fsum, isfinite, log2
from typing import Final, Literal

from pydantic import BaseModel, ConfigDict, Field, model_validator

from app.utils.audit_time import parse_audit_time


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
MarketScanSnapshotSealOrigin = Literal["publication", "legacy_backfill"]
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
MARKET_SCAN_FULL_MARKET_SCOPE: Final[str] = "沪市 + 深市 + 北交所当前上市A股"
MARKET_SCAN_MIN_HISTORY_ROWS: Final[int] = 61
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
MarketScanScoreLayerName = Literal["base", "integer", "final"]
MarketScanScoreComponentName = Literal[
    "leader_score",
    "trend_score",
    "data_quality_score",
    "rank_refinement_score",
]


@dataclass(frozen=True)
class MarketScanScoreLayerDiagnostic:
    """Layer diversity; entropy is normalized by log2(sample_count)."""

    name: MarketScanScoreLayerName
    sample_count: int
    distinct_count: int
    entropy_bits: float
    normalized_entropy: float
    effective_distinct_count: float
    effective_precision_digits: int
    variance: float

    def __post_init__(self) -> None:
        _validate_layer_diagnostic_identity(self)
        _validate_layer_diagnostic_numbers(self)
        _validate_layer_diagnostic_consistency(self)


@dataclass(frozen=True)
class MarketScanScoreComponentDiagnostic:
    """Deterministic population variance and coverage for one production component."""

    name: MarketScanScoreComponentName
    sample_count: int
    variance: float

    def __post_init__(self) -> None:
        if self.name not in {
            "leader_score", "trend_score", "data_quality_score", "rank_refinement_score",
        }:
            raise ValueError("评分组件名称无效")
        if isinstance(self.sample_count, bool) or not isinstance(self.sample_count, int) or self.sample_count < 0:
            raise ValueError("评分组件样本计数无效")
        if isinstance(self.variance, bool) or not isinstance(self.variance, int | float):
            raise ValueError("评分组件方差必须是有限非负数")
        if not isfinite(float(self.variance)) or self.variance < 0:
            raise ValueError("评分组件方差必须是有限非负数")


@dataclass(frozen=True)
class MarketScanScoreDistributionObservation:
    """Strict score layers persisted for one successful scan result."""

    symbol: str
    base_score: float | None
    integer_score: int | None
    raw_score: float | None
    leader_score: float | None = None
    trend_score: float | None = None
    data_quality_score: float | None = None
    rank_refinement_score: float | None = None

    def __post_init__(self) -> None:
        _validate_distribution_symbol(self.symbol)
        for name, value in (("base_score", self.base_score), ("raw_score", self.raw_score)):
            _validate_optional_score(name, value, upper=100.0)
        _validate_optional_integer_score(self.integer_score)
        for name, value, upper in (
            ("leader_score", self.leader_score, 100.0),
            ("trend_score", self.trend_score, 100.0),
            ("data_quality_score", self.data_quality_score, 100.0),
            ("rank_refinement_score", self.rank_refinement_score, 1.0),
        ):
            _validate_optional_score(name, value, upper=upper)


@dataclass(frozen=True)
class MarketScanProductionScoreContract:
    """One exact production-score contract shared by every success row in a run."""

    production_score_rule_version: str
    production_score_spec_hash: str
    success_count: int

    def __post_init__(self) -> None:
        rule_version = self.production_score_rule_version
        if not isinstance(rule_version, str) or not rule_version or rule_version != rule_version.strip():
            raise ValueError("production_score_rule_version 必须是非空且已规范化的文本")
        score_hash = self.production_score_spec_hash
        if (
            not isinstance(score_hash, str)
            or len(score_hash) != 64
            or any(char not in "0123456789abcdef" for char in score_hash)
        ):
            raise ValueError("production_score_spec_hash 必须是 64 位小写十六进制 SHA-256")
        if isinstance(self.success_count, bool) or not isinstance(self.success_count, int) or self.success_count <= 0:
            raise ValueError("success_count 必须是正整数")


@dataclass(frozen=True)
class MarketScanAutomaticState:
    """Cheap durable identity used only to suppress duplicate scheduler decisions."""

    run_id: int
    status: MarketScanRunStatus
    trigger: MarketScanTrigger
    data_date: str
    scope: str
    mode: MarketScanMode
    rule_version: str
    updated_at: str
    snapshot_digest: str | None
    snapshot_seal_origin: MarketScanSnapshotSealOrigin | None
    snapshot_sealed_at: str | None
    finished_at: str | None
    database_identity: str


@dataclass(frozen=True)
class MarketScanScoreDistributionPolicy:
    version: str = "score-layer-distribution-v4"
    base_score_decimals: int = 4
    raw_score_decimals: int = 6
    top_size: int = 100
    minimum_sample_count: int = 100
    failed_observed_ratio_below: float = 0.95
    required_component_observed_ratio: float = 1.0
    required_leader_trend_alias_ratio: float = 1.0
    degraded_observed_ratio_below: float = 0.99
    failed_max_tie_group_ratio_at_least: float = 0.95
    failed_saturation_ratio_at_least: float = 0.95
    failed_top100_upper_saturation_ratio_at_least: float = 1.0
    degraded_distinct_ratio_at_most: float = 0.02
    degraded_max_tie_group_ratio_at_least: float = 0.25
    degraded_single_tie_group_ratio_at_least: float = 0.50
    degraded_saturation_ratio_at_least: float = 0.50
    degraded_top100_tie_ratio_at_least: float = 0.50
    degraded_base_distinct_ratio_at_most: float = 0.02
    degraded_final_distinct_ratio_at_least: float = 0.50
    degraded_final_distinct_lift_at_least: float = 0.50
    degraded_top100_base_tie_ratio_at_least: float = 0.50
    degraded_base_normalized_entropy_at_most: float = 0.60
    degraded_final_normalized_entropy_at_least: float = 0.90
    degraded_effective_precision_lift_at_least: int = 3

    def spec(self) -> dict[str, object]:
        return asdict(self)

    def assess(self, distribution: MarketScanScoreDistribution) -> MarketScanScoreDistributionAssessment:
        if distribution.expected_count < self.minimum_sample_count:
            return MarketScanScoreDistributionAssessment("not-evaluated")
        failed_reasons = self._failure_reasons(distribution)
        if failed_reasons:
            return MarketScanScoreDistributionAssessment(
                "failed",
                failed_reasons,
                self._warnings(distribution),
            )
        degraded_reasons = self._degraded_reasons(distribution)
        if degraded_reasons:
            return MarketScanScoreDistributionAssessment(
                "degraded",
                degraded_reasons,
                self._warnings(distribution),
            )
        return MarketScanScoreDistributionAssessment(
            "pass",
            warnings=self._warnings(distribution),
        )

    def _failure_reasons(self, distribution: MarketScanScoreDistribution) -> tuple[str, ...]:
        return _unique_reasons(
            self._raw_failure_reasons(distribution),
            self._layer_coverage_failure_reasons(distribution),
            self._component_failure_reasons(distribution),
            self._base_failure_reasons(distribution),
        )

    def _raw_failure_reasons(
        self,
        distribution: MarketScanScoreDistribution,
    ) -> tuple[str, ...]:
        coverage = self._raw_coverage_failure(distribution)
        tie = self._raw_tie_failure(distribution)
        saturation = (
            f"0/100 饱和率 {distribution.saturation_ratio:.2%}，评分大面积触及边界"
            if distribution.saturation_ratio >= self.failed_saturation_ratio_at_least
            else None
        )
        top = (
            "前100名 raw_score 全部饱和在 100"
            if distribution.top100_count >= self.top_size
            and distribution.top100_upper_saturation_ratio
            >= self.failed_top100_upper_saturation_ratio_at_least
            else None
        )
        return _present_reasons(coverage, tie, saturation, top)

    def _raw_coverage_failure(
        self,
        distribution: MarketScanScoreDistribution,
    ) -> str | None:
        insufficient = (
            distribution.sample_count < self.minimum_sample_count
            or distribution.observed_ratio < self.failed_observed_ratio_below
        )
        if not insufficient:
            return None
        return (
            f"raw_score 可审计样本不足：{distribution.sample_count}/"
            f"{distribution.expected_count}（{distribution.observed_ratio:.2%}）"
        )

    def _raw_tie_failure(self, distribution: MarketScanScoreDistribution) -> str | None:
        if distribution.distinct_count == 1:
            return "成功结果 raw_score 全部相同"
        if distribution.max_tie_group_ratio >= self.failed_max_tie_group_ratio_at_least:
            return f"最大并列组占比 {distribution.max_tie_group_ratio:.2%}，接近常量分"
        return None

    def _layer_coverage_failure_reasons(
        self,
        distribution: MarketScanScoreDistribution,
    ) -> tuple[str, ...]:
        if not distribution.layered_observation_count:
            return ("仅有 raw-only 评分分布，缺少 base/integer/final 与组件证据",)
        layered = _layer_coverage_reason(
            "分层观测",
            distribution.layered_observation_count,
            distribution.expected_count,
            _safe_ratio(distribution.layered_observation_count, distribution.expected_count),
            self.failed_observed_ratio_below,
        )
        base = _layer_coverage_reason(
            "基础分",
            distribution.base_score_sample_count,
            distribution.expected_count,
            distribution.base_score_observed_ratio,
            self.failed_observed_ratio_below,
        )
        integer = _layer_coverage_reason(
            "整数分",
            distribution.integer_score_sample_count,
            distribution.expected_count,
            distribution.integer_score_observed_ratio,
            self.failed_observed_ratio_below,
        )
        return _present_reasons(layered, base, integer)

    def _component_failure_reasons(
        self,
        distribution: MarketScanScoreDistribution,
    ) -> tuple[str, ...]:
        if not distribution.layered_observation_count:
            return ()
        reasons = [
            f"{item.name} 有限值覆盖不足：{item.sample_count}/{distribution.expected_count}"
            for item in distribution.component_diagnostics
            if _safe_ratio(item.sample_count, distribution.expected_count)
            < self.required_component_observed_ratio
        ]
        if (
            distribution.leader_trend_pair_count != distribution.expected_count
            or distribution.leader_trend_alias_ratio < self.required_leader_trend_alias_ratio
        ):
            reasons.append(
                "leader_score/trend_score 精确别名关系不完整："
                f"{distribution.leader_trend_alias_count}/{distribution.expected_count}"
            )
        return tuple(reasons)

    @staticmethod
    def _warnings(distribution: MarketScanScoreDistribution) -> tuple[str, ...]:
        return tuple(
            f"{item.name} 方差为零，组件在本批次不提供横截面区分度"
            for item in distribution.component_diagnostics
            if item.sample_count == distribution.expected_count and item.variance == 0
        )

    def _base_failure_reasons(
        self,
        distribution: MarketScanScoreDistribution,
    ) -> tuple[str, ...]:
        if distribution.base_score_sample_count < self.minimum_sample_count:
            return ()
        if distribution.base_score_distinct_count == 1:
            return ("成功结果基础分全部相同，小数精排不能掩盖常量分",)
        if distribution.base_score_max_tie_group_ratio >= self.failed_max_tie_group_ratio_at_least:
            return (
                f"基础分最大并列组占比 {distribution.base_score_max_tie_group_ratio:.2%}，"
                "接近常量分",
            )
        return ()

    def _degraded_reasons(self, distribution: MarketScanScoreDistribution) -> tuple[str, ...]:
        return _unique_reasons(
            self._raw_degraded_reasons(distribution),
            self._layer_degraded_reasons(distribution),
        )

    def _raw_degraded_reasons(
        self,
        distribution: MarketScanScoreDistribution,
    ) -> tuple[str, ...]:
        coverage = (
            f"raw_score 可审计样本仅覆盖 {distribution.observed_ratio:.2%}"
            if distribution.observed_ratio < self.degraded_observed_ratio_below
            else None
        )
        saturation = (
            f"0/100 饱和率达到 {distribution.saturation_ratio:.2%}"
            if distribution.saturation_ratio >= self.degraded_saturation_ratio_at_least
            else None
        )
        top_tie = (
            f"前100并列占比达到 {distribution.top100_tie_ratio:.2%}"
            if distribution.top100_tie_ratio >= self.degraded_top100_tie_ratio_at_least
            else None
        )
        return _present_reasons(coverage, self._raw_tie_degradation(distribution), saturation, top_tie)

    def _raw_tie_degradation(
        self,
        distribution: MarketScanScoreDistribution,
    ) -> str | None:
        if (
            distribution.distinct_raw_score_ratio <= self.degraded_distinct_ratio_at_most
            and distribution.max_tie_group_ratio >= self.degraded_max_tie_group_ratio_at_least
        ):
            return (
                f"distinct raw score ratio 仅 {distribution.distinct_raw_score_ratio:.2%}，"
                f"最大并列组占比 {distribution.max_tie_group_ratio:.2%}"
            )
        if distribution.max_tie_group_ratio >= self.degraded_single_tie_group_ratio_at_least:
            return f"最大并列组占比达到 {distribution.max_tie_group_ratio:.2%}"
        return None

    def _layer_degraded_reasons(
        self,
        distribution: MarketScanScoreDistribution,
    ) -> tuple[str, ...]:
        if distribution.base_score_sample_count < self.minimum_sample_count:
            return ()
        jitter = self._decimal_jitter_reason(distribution)
        top_tie = (
            f"前100基础分并列占比达到 {distribution.top100_base_tie_ratio:.2%}"
            if distribution.top100_base_tie_ratio
            >= self.degraded_top100_base_tie_ratio_at_least
            else None
        )
        return _present_reasons(jitter, top_tie)

    def _decimal_jitter_reason(
        self,
        distribution: MarketScanScoreDistribution,
    ) -> str | None:
        distinct_mask = (
            distribution.base_score_distinct_ratio <= self.degraded_base_distinct_ratio_at_most
            and distribution.distinct_raw_score_ratio >= self.degraded_final_distinct_ratio_at_least
            and distribution.final_distinct_lift_over_base >= self.degraded_final_distinct_lift_at_least
        )
        base_entropy = distribution.layer_diagnostic("base")
        final_entropy = distribution.layer_diagnostic("final")
        entropy_precision_mask = _entropy_precision_mask(
            base_entropy,
            final_entropy,
            base_entropy_at_most=self.degraded_base_normalized_entropy_at_most,
            final_entropy_at_least=self.degraded_final_normalized_entropy_at_least,
            precision_lift_at_least=self.degraded_effective_precision_lift_at_least,
        )
        if not distinct_mask and not entropy_precision_mask:
            return None
        entropy_audit = (
            f"，归一化熵 {base_entropy.normalized_entropy:.2%}→"
            f"{final_entropy.normalized_entropy:.2%}，有效精度 "
            f"{base_entropy.effective_precision_digits}d→{final_entropy.effective_precision_digits}d"
            if base_entropy is not None and final_entropy is not None
            else ""
        )
        return (
            f"基础分离散度仅 {distribution.base_score_distinct_ratio:.2%}，"
            f"小数决胜后为 {distribution.distinct_raw_score_ratio:.2%}{entropy_audit}；"
            "疑似小数抖动伪离散"
        )


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
    layered_observation_count: int = 0
    base_score_sample_count: int = 0
    base_score_distinct_count: int = 0
    base_score_distinct_ratio: float = 0.0
    base_score_max_tie_group_count: int = 0
    base_score_max_tie_group_ratio: float = 0.0
    integer_score_sample_count: int = 0
    integer_score_distinct_count: int = 0
    integer_score_distinct_ratio: float = 0.0
    integer_score_max_tie_group_count: int = 0
    integer_score_max_tie_group_ratio: float = 0.0
    top100_base_tied_count: int = 0
    top100_base_tie_ratio: float = 0.0
    top100_integer_tied_count: int = 0
    top100_integer_tie_ratio: float = 0.0
    final_distinct_lift_over_base: float = 0.0
    leader_trend_pair_count: int = 0
    leader_trend_alias_count: int = 0
    leader_trend_alias_ratio: float = 0.0
    layer_diagnostics: tuple[MarketScanScoreLayerDiagnostic, ...] = ()
    component_diagnostics: tuple[MarketScanScoreComponentDiagnostic, ...] = ()

    def __post_init__(self) -> None:
        layer_names = tuple(item.name for item in self.layer_diagnostics)
        component_names = tuple(item.name for item in self.component_diagnostics)
        if layer_names not in {(), ("final",), ("base", "integer", "final")}:
            raise ValueError("评分层诊断必须按 base/integer/final 固定顺序且不得重复")
        if component_names not in {
            (),
            ("leader_score", "trend_score", "data_quality_score", "rank_refinement_score"),
        }:
            raise ValueError("评分组件诊断必须使用固定顺序且不得重复")
        if not (
            0 <= self.leader_trend_alias_count <= self.leader_trend_pair_count
            <= self.layered_observation_count
            and 0 <= self.leader_trend_alias_ratio <= 1
        ):
            raise ValueError("leader/trend 别名诊断计数无效")

    @classmethod
    def from_raw_scores(
        cls,
        raw_scores: Iterable[object],
        *,
        expected_count: int,
        policy: MarketScanScoreDistributionPolicy,
    ) -> "MarketScanScoreDistribution":
        values = _score_values(raw_scores, decimals=policy.raw_score_decimals)
        return cls._from_layers(
            values,
            expected_count=expected_count,
            policy=policy,
        )

    @classmethod
    def from_score_observations(
        cls,
        observations: Iterable[MarketScanScoreDistributionObservation],
        *,
        expected_count: int,
        policy: MarketScanScoreDistributionPolicy,
    ) -> "MarketScanScoreDistribution":
        rows = tuple(observations)
        if any(not isinstance(item, MarketScanScoreDistributionObservation) for item in rows):
            raise TypeError("评分分布观测必须使用 MarketScanScoreDistributionObservation")
        symbols = tuple(item.symbol for item in rows)
        if len(symbols) != len(set(symbols)):
            raise ValueError("评分分布观测包含重复股票")
        base_values = _score_values(
            (item.base_score for item in rows),
            decimals=policy.base_score_decimals,
        )
        integer_values = _score_values(
            (item.integer_score for item in rows),
            decimals=0,
        )
        component_values = _score_component_values(rows)
        ordered_rows = sorted(
            (
                (round(raw, policy.raw_score_decimals), item.symbol, item)
                for item in rows
                if (raw := _parse_raw_score(item.raw_score)) is not None
            ),
            key=lambda entry: (-entry[0], entry[1]),
        )
        values = [entry[0] for entry in ordered_rows]
        ordered_observations = tuple(item for _raw, _symbol, item in ordered_rows)
        return cls._from_layers(
            values,
            expected_count=expected_count,
            policy=policy,
            layered_observation_count=len(rows),
            base_values=base_values,
            integer_values=integer_values,
            ordered_observations=ordered_observations,
            component_values=component_values,
        )

    @classmethod
    def _from_layers(
        cls,
        values: list[float],
        *,
        expected_count: int,
        policy: MarketScanScoreDistributionPolicy,
        layered_observation_count: int = 0,
        base_values: list[float] | None = None,
        integer_values: list[float] | None = None,
        ordered_observations: tuple[MarketScanScoreDistributionObservation, ...] = (),
        component_values: dict[MarketScanScoreComponentName, list[float]] | None = None,
    ) -> "MarketScanScoreDistribution":
        raw_distribution = cls._from_raw_values(
            values,
            expected_count=expected_count,
            policy=policy,
        )
        if not layered_observation_count:
            return raw_distribution
        return _with_layer_distribution(
            raw_distribution,
            layered_observation_count=layered_observation_count,
            base_values=base_values or [],
            integer_values=integer_values or [],
            ordered_observations=ordered_observations,
            policy=policy,
            component_values=component_values or {},
        )

    @classmethod
    def _from_raw_values(
        cls,
        values: list[float],
        *,
        expected_count: int,
        policy: MarketScanScoreDistributionPolicy,
    ) -> "MarketScanScoreDistribution":
        counts = Counter(values)
        top_values = values[: policy.top_size]
        top_counts = Counter(top_values)
        sample_count = len(values)
        top_count = len(top_values)
        max_tie_count = max(counts.values(), default=0)
        saturation_count = sum(counts.get(boundary, 0) for boundary in (0.0, 100.0))
        top_tied_count = sum(1 for value in top_values if counts[value] > 1)
        return cls(
            policy.version, max(0, expected_count), sample_count, len(counts),
            _safe_ratio(len(counts), sample_count), max_tie_count,
            _safe_ratio(max_tie_count, sample_count), saturation_count,
            _safe_ratio(saturation_count, sample_count), top_count,
            max(top_counts.values(), default=0), top_tied_count,
            _safe_ratio(top_tied_count, top_count),
            _safe_ratio(top_counts.get(100.0, 0), top_count),
            layer_diagnostics=(
                _score_layer_diagnostic("final", values, policy.raw_score_decimals),
            ),
        )

    @property
    def observed_ratio(self) -> float:
        return _safe_ratio(self.sample_count, self.expected_count)

    @property
    def base_score_observed_ratio(self) -> float:
        return _safe_ratio(self.base_score_sample_count, self.expected_count)

    @property
    def integer_score_observed_ratio(self) -> float:
        return _safe_ratio(self.integer_score_sample_count, self.expected_count)

    def layer_diagnostic(self, name: MarketScanScoreLayerName) -> MarketScanScoreLayerDiagnostic | None:
        return next((item for item in self.layer_diagnostics if item.name == name), None)

    def component_diagnostic(
        self,
        name: MarketScanScoreComponentName,
    ) -> MarketScanScoreComponentDiagnostic | None:
        return next((item for item in self.component_diagnostics if item.name == name), None)

    def audit_text(self) -> str:
        raw_audit = (
            f"评分分布门禁 {self.policy_version}：raw_score样本 {self.sample_count}/{self.expected_count}，"
            f"distinct ratio {self.distinct_raw_score_ratio:.2%}，"
            f"最大并列组 {self.max_tie_group_count}/{self.sample_count}（{self.max_tie_group_ratio:.2%}），"
            f"0/100饱和 {self.saturation_count}/{self.sample_count}（{self.saturation_ratio:.2%}），"
            f"前100并列 {self.top100_tied_count}/{self.top100_count}（{self.top100_tie_ratio:.2%}），"
            f"最大组 {self.top100_max_tie_group_count}"
        )
        if not self.layered_observation_count:
            return raw_audit
        return (
            raw_audit
            + f"；基础分 distinct {self.base_score_distinct_count}/{self.base_score_sample_count}"
            f"（{self.base_score_distinct_ratio:.2%}），最大并列 "
            f"{self.base_score_max_tie_group_count}/{self.base_score_sample_count}"
            f"（{self.base_score_max_tie_group_ratio:.2%}）；整数分 distinct "
            f"{self.integer_score_distinct_count}/{self.integer_score_sample_count}"
            f"（{self.integer_score_distinct_ratio:.2%}）；前100基础分并列 "
            f"{self.top100_base_tied_count}/{self.top100_count}"
            f"（{self.top100_base_tie_ratio:.2%}）；小数决胜 distinct 提升 "
            f"{self.final_distinct_lift_over_base:.2%}"
            + _layer_diagnostics_audit(self.layer_diagnostics)
            + _component_diagnostics_audit(self.component_diagnostics)
            + f"；leader/trend精确别名 {self.leader_trend_alias_count}/"
            f"{self.leader_trend_pair_count}（{self.leader_trend_alias_ratio:.2%}）"
        )


def _with_layer_distribution(
    distribution: MarketScanScoreDistribution,
    *,
    layered_observation_count: int,
    base_values: list[float],
    integer_values: list[float],
    ordered_observations: tuple[MarketScanScoreDistributionObservation, ...],
    policy: MarketScanScoreDistributionPolicy,
    component_values: dict[MarketScanScoreComponentName, list[float]],
) -> MarketScanScoreDistribution:
    base_counts = Counter(base_values)
    integer_counts = Counter(integer_values)
    top = ordered_observations[: policy.top_size]
    base_tied = _top_layer_tied_count(top, "base_score", policy.base_score_decimals, base_counts)
    integer_tied = _top_layer_tied_count(top, "integer_score", 0, integer_counts)
    alias_pairs = tuple(
        item
        for item in ordered_observations
        if item.leader_score is not None and item.trend_score is not None
    )
    alias_count = sum(item.leader_score == item.trend_score for item in alias_pairs)
    base_distinct_ratio = _safe_ratio(len(base_counts), len(base_values))
    return replace(
        distribution,
        layered_observation_count=layered_observation_count,
        base_score_sample_count=len(base_values),
        base_score_distinct_count=len(base_counts),
        base_score_distinct_ratio=base_distinct_ratio,
        base_score_max_tie_group_count=max(base_counts.values(), default=0),
        base_score_max_tie_group_ratio=_max_tie_ratio(base_counts, len(base_values)),
        integer_score_sample_count=len(integer_values),
        integer_score_distinct_count=len(integer_counts),
        integer_score_distinct_ratio=_safe_ratio(len(integer_counts), len(integer_values)),
        integer_score_max_tie_group_count=max(integer_counts.values(), default=0),
        integer_score_max_tie_group_ratio=_max_tie_ratio(integer_counts, len(integer_values)),
        top100_base_tied_count=base_tied,
        top100_base_tie_ratio=_safe_ratio(base_tied, len(top)),
        top100_integer_tied_count=integer_tied,
        top100_integer_tie_ratio=_safe_ratio(integer_tied, len(top)),
        final_distinct_lift_over_base=max(
            0.0,
            distribution.distinct_raw_score_ratio - base_distinct_ratio,
        ),
        leader_trend_pair_count=len(alias_pairs),
        leader_trend_alias_count=alias_count,
        leader_trend_alias_ratio=_safe_ratio(alias_count, len(alias_pairs)),
        layer_diagnostics=(
            _score_layer_diagnostic("base", base_values, policy.base_score_decimals),
            _score_layer_diagnostic("integer", integer_values, 0),
            _score_layer_diagnostic(
                "final",
                _score_values(
                    (item.raw_score for item in ordered_observations),
                    decimals=policy.raw_score_decimals,
                ),
                policy.raw_score_decimals,
            ),
        ),
        component_diagnostics=_score_component_diagnostics(component_values),
    )


def _score_component_values(
    observations: tuple[MarketScanScoreDistributionObservation, ...],
) -> dict[MarketScanScoreComponentName, list[float]]:
    fields: tuple[MarketScanScoreComponentName, ...] = (
        "leader_score",
        "trend_score",
        "data_quality_score",
        "rank_refinement_score",
    )
    return {
        field: _score_values(
            (getattr(item, field) for item in observations),
            decimals=6,
        )
        for field in fields
    }


def _entropy_precision_mask(
    base: MarketScanScoreLayerDiagnostic | None,
    final: MarketScanScoreLayerDiagnostic | None,
    *,
    base_entropy_at_most: float,
    final_entropy_at_least: float,
    precision_lift_at_least: int,
) -> bool:
    return bool(
        base is not None
        and final is not None
        and base.normalized_entropy <= base_entropy_at_most
        and final.normalized_entropy >= final_entropy_at_least
        and final.effective_precision_digits - base.effective_precision_digits
        >= precision_lift_at_least
    )


def _score_component_diagnostics(
    component_values: dict[MarketScanScoreComponentName, list[float]],
) -> tuple[MarketScanScoreComponentDiagnostic, ...]:
    names: tuple[MarketScanScoreComponentName, ...] = (
        "leader_score",
        "trend_score",
        "data_quality_score",
        "rank_refinement_score",
    )
    return tuple(
        MarketScanScoreComponentDiagnostic(
            name=name,
            sample_count=len(component_values.get(name, ())),
            variance=_population_variance(component_values.get(name, ())),
        )
        for name in names
    )


def _score_layer_diagnostic(
    name: MarketScanScoreLayerName,
    values: list[float],
    max_precision_digits: int,
) -> MarketScanScoreLayerDiagnostic:
    counts = Counter(values)
    sample_count = len(values)
    entropy_bits = _entropy_bits(counts, sample_count)
    normalized_entropy = (
        entropy_bits / log2(sample_count)
        if sample_count > 1
        else 0.0
    )
    return MarketScanScoreLayerDiagnostic(
        name=name,
        sample_count=sample_count,
        distinct_count=len(counts),
        entropy_bits=round(entropy_bits, 12),
        normalized_entropy=round(min(1.0, max(0.0, normalized_entropy)), 12),
        effective_distinct_count=round(2**entropy_bits, 12) if sample_count else 0.0,
        effective_precision_digits=_effective_precision_digits(values, max_precision_digits),
        variance=_population_variance(values),
    )


def _entropy_bits(counts: Counter[float], sample_count: int) -> float:
    if sample_count <= 0:
        return 0.0
    return fsum(
        -(count / sample_count) * log2(count / sample_count)
        for _value, count in sorted(counts.items())
    )


def _effective_precision_digits(values: Sequence[float], max_digits: int) -> int:
    """Smallest decimal precision that preserves the layer's full distinct count."""
    if not values:
        return 0
    target = len(set(values))
    return next(
        (digits for digits in range(max_digits + 1) if len({round(value, digits) for value in values}) == target),
        max_digits,
    )


def _population_variance(values: Sequence[float]) -> float:
    if not values:
        return 0.0
    ordered = sorted(values)
    mean = fsum(ordered) / len(ordered)
    return round(fsum((value - mean) ** 2 for value in ordered) / len(ordered), 12)


def _layer_diagnostics_audit(
    diagnostics: tuple[MarketScanScoreLayerDiagnostic, ...],
) -> str:
    if not diagnostics:
        return ""
    details = ", ".join(
        f"{item.name} Hn={item.normalized_entropy:.3f}/有效档={item.effective_distinct_count:.1f}/精度={item.effective_precision_digits}d"
        for item in diagnostics
    )
    return f"；层熵诊断 {details}"


def _component_diagnostics_audit(
    diagnostics: tuple[MarketScanScoreComponentDiagnostic, ...],
) -> str:
    if not diagnostics:
        return ""
    details = ", ".join(
        f"{item.name}={item.variance:.6g}/{item.sample_count}"
        for item in diagnostics
    )
    return f"；组件方差/样本 {details}"


def _top_layer_tied_count(
    observations: tuple[MarketScanScoreDistributionObservation, ...],
    field_name: Literal["base_score", "integer_score"],
    decimals: int,
    counts: Counter[float],
) -> int:
    return sum(
        1
        for item in observations
        if (value := _rounded_score(getattr(item, field_name), decimals)) is not None
        and counts[value] > 1
    )


def _max_tie_ratio(counts: Counter[float], sample_count: int) -> float:
    return _safe_ratio(max(counts.values(), default=0), sample_count)


@dataclass(frozen=True)
class MarketScanScoreDistributionAssessment:
    status: MarketScanScoreDistributionGateStatus
    reasons: tuple[str, ...] = ()
    warnings: tuple[str, ...] = ()


def _present_reasons(*values: str | None) -> tuple[str, ...]:
    return tuple(value for value in values if value)


def _validate_distribution_symbol(value: object) -> None:
    if not isinstance(value, str) or not value.strip() or value != value.strip():
        raise ValueError("symbol 必须是非空且已规范化的文本")


def _validate_layer_diagnostic_identity(item: MarketScanScoreLayerDiagnostic) -> None:
    if item.name not in {"base", "integer", "final"}:
        raise ValueError("评分层名称无效")
    if (
        isinstance(item.sample_count, bool)
        or not isinstance(item.sample_count, int)
        or isinstance(item.distinct_count, bool)
        or not isinstance(item.distinct_count, int)
        or not 0 <= item.distinct_count <= item.sample_count
    ):
        raise ValueError("评分层样本计数无效")
    if (
        isinstance(item.effective_precision_digits, bool)
        or not isinstance(item.effective_precision_digits, int)
        or not 0 <= item.effective_precision_digits <= 12
    ):
        raise ValueError("评分层有效精度必须是 0 到 12 的整数")


def _validate_layer_diagnostic_numbers(item: MarketScanScoreLayerDiagnostic) -> None:
    values = (
        item.entropy_bits,
        item.normalized_entropy,
        item.effective_distinct_count,
        item.variance,
    )
    if any(
        isinstance(value, bool)
        or not isinstance(value, int | float)
        or not isfinite(float(value))
        for value in values
    ):
        raise ValueError("评分层诊断必须是有限数值")
    if item.entropy_bits < 0 or not 0 <= item.normalized_entropy <= 1:
        raise ValueError("评分层熵诊断超出范围")
    if item.effective_distinct_count < 0 or item.variance < 0:
        raise ValueError("评分层有效档位或方差不能为负")


def _validate_layer_diagnostic_consistency(item: MarketScanScoreLayerDiagnostic) -> None:
    expected_entropy_ratio = (
        item.entropy_bits / log2(item.sample_count)
        if item.sample_count > 1
        else 0.0
    )
    expected_effective_count = 2**item.entropy_bits if item.sample_count else 0.0
    if not _diagnostic_close(item.normalized_entropy, expected_entropy_ratio):
        raise ValueError("评分层归一化熵与样本数不一致")
    if not _diagnostic_close(item.effective_distinct_count, expected_effective_count):
        raise ValueError("评分层有效档位与熵不一致")
    if item.sample_count == 0 and (item.distinct_count or item.variance):
        raise ValueError("空评分层不能包含离散度或方差")
    if item.sample_count and not 1 <= item.effective_distinct_count <= item.distinct_count + 1e-8:
        raise ValueError("评分层有效档位超出实际档位数")


def _diagnostic_close(left: float, right: float) -> bool:
    return abs(left - right) <= 1e-9 * max(1.0, abs(right))


def _validate_optional_score(name: str, value: object, *, upper: float) -> None:
    if value is None:
        return
    if (
        isinstance(value, bool)
        or not isinstance(value, int | float)
        or not isfinite(float(value))
        or not 0 <= float(value) <= upper
    ):
        raise ValueError(f"{name} 必须是 0 到 {upper:g} 的有限数或 None")


def _validate_optional_integer_score(value: object) -> None:
    if value is None:
        return
    if isinstance(value, bool) or not isinstance(value, int) or not 0 <= value <= 100:
        raise ValueError("integer_score 必须是 0 到 100 的整数或 None")


def _unique_reasons(*groups: tuple[str, ...]) -> tuple[str, ...]:
    return tuple(dict.fromkeys(reason for group in groups for reason in group))


def _layer_coverage_reason(
    label: str,
    sample_count: int,
    expected_count: int,
    observed_ratio: float,
    minimum_ratio: float,
) -> str | None:
    if observed_ratio >= minimum_ratio:
        return None
    return (
        f"{label}可审计样本不足：{sample_count}/{expected_count}"
        f"（{observed_ratio:.2%}）"
    )


def _parse_raw_score(value: object) -> float | None:
    if isinstance(value, bool) or not isinstance(value, int | float):
        return None
    parsed = float(value)
    return parsed if isfinite(parsed) and 0 <= parsed <= 100 else None


def _rounded_score(value: object, decimals: int) -> float | None:
    parsed = _parse_raw_score(value)
    return round(parsed, decimals) if parsed is not None else None


def _score_values(values: Iterable[object], *, decimals: int) -> list[float]:
    return sorted(
        (
            rounded
            for value in values
            if (rounded := _rounded_score(value, decimals)) is not None
        ),
        reverse=True,
    )


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

    @model_validator(mode="after")
    def validate_counts(self) -> "MarketScanMarketProgress":
        if self.processed_count != self.success_count + self.missing_count + self.skipped_count:
            raise ValueError("分市场 processed_count 与状态计数不守恒")
        if self.processed_count > self.total_count:
            raise ValueError("分市场 processed_count 不能大于 total_count")
        expected_coverage = _percentage(
            self.success_count,
            max(0, self.total_count - self.skipped_count),
        )
        if abs(self.coverage_pct - expected_coverage) > 0.011:
            raise ValueError("分市场 coverage_pct 与状态计数不一致")
        return self


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
    snapshot_digest: str | None = Field(default=None, pattern=r"^[0-9a-f]{64}$")
    snapshot_seal_origin: MarketScanSnapshotSealOrigin | None = None
    snapshot_sealed_at: str | None = None
    cancel_requested_at: str | None = None

    @model_validator(mode="after")
    def validate_run_contract(self) -> "MarketScanRun":
        if not self.quote_date:
            self.quote_date = self.data_date
        _validate_run_identity(self)
        _validate_run_counts(self)
        _validate_published_run(self)
        return self


class MarketScanStartResponse(BaseModel):
    accepted: bool
    deduplicated: bool = False
    run: MarketScanRun

    @model_validator(mode="after")
    def validate_acceptance_state(self) -> "MarketScanStartResponse":
        if self.accepted == self.deduplicated:
            raise ValueError("accepted 与 deduplicated 必须且只能有一个为 true")
        return self


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
    price: float | None = Field(default=None, gt=0, allow_inf_nan=False)
    change_pct: float | None = Field(default=None, ge=-1000, le=1000, allow_inf_nan=False)
    turnover_rate: float | None = Field(default=None, ge=0, allow_inf_nan=False)
    volume_ratio: float | None = Field(default=None, ge=0, allow_inf_nan=False)
    amount: float | None = Field(default=None, ge=0, allow_inf_nan=False)
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

    def validate_public_response(self, *, run: MarketScanRun) -> None:
        if self.run_id != run.id:
            raise ValueError("榜单项目 run_id 与榜单批次不一致")
        if self.symbol != f"{self.code}.{self.market}":
            raise ValueError("榜单项目 symbol/code/market 不一致")
        _required_timestamp(self.updated_at, "result.updated_at")
        if run.status in {"success", "degraded"} and _timestamp_before(
            _comparable_timestamp(run.updated_at),
            _comparable_timestamp(self.updated_at),
        ):
            raise ValueError("榜单项目 updated_at 不能晚于已发布批次 updated_at")
        if self.status == "success":
            _validate_success_result(self, run)
            return
        _validate_non_success_result(self)


class MarketScanResultPage(BaseModel):
    run: MarketScanRun
    items: list[MarketScanResultItem]
    total: int = Field(ge=0)
    page: int = Field(ge=1)
    page_size: int = Field(ge=1)
    page_count: int = Field(ge=0)
    probability_research: dict[str, object] | None = None

    @model_validator(mode="after")
    def validate_page_binding(self) -> "MarketScanResultPage":
        _validate_response_page_shape(
            self.items,
            total=self.total,
            page=self.page,
            page_size=self.page_size,
            page_count=self.page_count,
        )
        symbols: set[str] = set()
        for item in self.items:
            item.validate_public_response(run=self.run)
            if item.symbol in symbols:
                raise ValueError("榜单项目 symbol 不能重复")
            symbols.add(item.symbol)
        return self


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

    @model_validator(mode="after")
    def validate_page_shape(self) -> "MarketScanFutureRangeRecordPage":
        _validate_response_page_shape(
            self.items,
            total=self.total,
            page=self.page,
            page_size=self.page_size,
            page_count=self.page_count,
        )
        return self


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

    @model_validator(mode="after")
    def validate_page_shape(self) -> "MarketScanRunPage":
        _validate_response_page_shape(
            self.items,
            total=self.total,
            page=self.page,
            page_size=self.page_size,
            page_count=self.page_count,
        )
        if len({item.id for item in self.items}) != len(self.items):
            raise ValueError("扫描批次列表不能包含重复 id")
        return self


def _validate_response_page_shape(
    items: Sequence[object],
    *,
    total: int,
    page: int,
    page_size: int,
    page_count: int,
) -> None:
    expected_page_count = (total + page_size - 1) // page_size if total else 0
    if page_count != expected_page_count:
        raise ValueError("page_count 与 total/page_size 不一致")
    expected_items = 0 if page > page_count else min(page_size, total - (page - 1) * page_size)
    if len(items) != expected_items:
        raise ValueError("items 数量与当前分页不一致")


def _validate_run_identity(run: MarketScanRun) -> None:
    _required_iso_date(run.data_date, "data_date")
    _required_iso_date(run.quote_date, "quote_date")
    _required_timestamp(run.as_of, "as_of")
    created_at = _required_timestamp(run.created_at, "created_at")
    updated_at = _required_timestamp(run.updated_at, "updated_at")
    if _timestamp_before(updated_at, created_at):
        raise ValueError("updated_at 不能早于 created_at")
    for field_name in (
        "started_at", "finished_at", "quote_capture_started_at",
        "quote_capture_finished_at", "stage_started_at", "cancel_requested_at",
        "snapshot_sealed_at",
    ):
        value = getattr(run, field_name)
        if value is not None:
            _required_timestamp(value, field_name)
    _validate_run_time_order(run, created_at)


def _validate_run_time_order(run: MarketScanRun, created_at: datetime) -> None:
    started_at = _optional_timestamp(run.started_at, "started_at")
    finished_at = _optional_timestamp(run.finished_at, "finished_at")
    _validate_execution_time_order(started_at, finished_at, created_at)
    capture_started = _optional_timestamp(run.quote_capture_started_at, "quote_capture_started_at")
    capture_finished = _optional_timestamp(run.quote_capture_finished_at, "quote_capture_finished_at")
    _validate_quote_capture_time_order(run, capture_started, capture_finished)


def _validate_execution_time_order(
    started_at: datetime | None,
    finished_at: datetime | None,
    created_at: datetime,
) -> None:
    if started_at is not None and _timestamp_before(started_at, created_at):
        raise ValueError("started_at 不能早于 created_at")
    if finished_at is not None and _timestamp_before(finished_at, started_at or created_at):
        raise ValueError("finished_at 不能早于运行开始时间")


def _validate_quote_capture_time_order(
    run: MarketScanRun,
    capture_started: datetime | None,
    capture_finished: datetime | None,
) -> None:
    if capture_finished is not None and capture_started is not None and _timestamp_before(capture_finished, capture_started):
        raise ValueError("quote_capture_finished_at 不能早于 quote_capture_started_at")
    if capture_finished is not None and capture_started is None:
        raise ValueError("quote_capture_finished_at 缺少 quote_capture_started_at")
    if run.quote_capture_duration_ms is not None and (capture_started is None or capture_finished is None):
        raise ValueError("quote_capture_duration_ms 缺少完整采集时点")


def _validate_run_counts(run: MarketScanRun) -> None:
    if run.processed_count != run.success_count + run.missing_count + run.skipped_count:
        raise ValueError("processed_count 与 success/missing/skipped 计数不守恒")
    if run.processed_count > run.total_count:
        raise ValueError("processed_count 不能大于 total_count")
    if run.quote_capture_count > run.total_count:
        raise ValueError("quote_capture_count 不能大于 total_count")
    expected_progress = 100.0 if not run.total_count and run.status in {"success", "degraded"} else _percentage(run.processed_count, run.total_count)
    if abs(run.progress_pct - expected_progress) > 0.011:
        raise ValueError("progress_pct 与处理计数不一致")
    expected_coverage = _percentage(run.success_count, max(0, run.total_count - run.skipped_count))
    if abs(run.coverage_pct - expected_coverage) > 0.011:
        raise ValueError("coverage_pct 与有效覆盖计数不一致")
    _validate_market_progress(run)


def _validate_market_progress(run: MarketScanRun) -> None:
    if not run.market_progress:
        if _requires_current_full_market_progress(run):
            raise ValueError("当前已发布全市场批次必须包含 SH/SZ/BJ 覆盖分母证据")
        return
    if len({item.market for item in run.market_progress}) != len(run.market_progress):
        raise ValueError("market_progress 市场不能重复")
    if _requires_current_full_market_progress(run) and {
        item.market for item in run.market_progress
    } != {"SH", "SZ", "BJ"}:
        raise ValueError("当前已发布全市场批次必须完整覆盖 SH/SZ/BJ")
    for field_name in ("total_count", "processed_count", "success_count", "missing_count", "skipped_count"):
        if sum(getattr(item, field_name) for item in run.market_progress) != getattr(run, field_name):
            raise ValueError(f"market_progress.{field_name} 与运行总计不守恒")


def _requires_current_full_market_progress(run: MarketScanRun) -> bool:
    prefix = "full-market-scan-v6:"
    digest = run.rule_version.removeprefix(prefix)
    return (
        run.status in {"success", "degraded"}
        and run.snapshot_seal_origin == "publication"
        and run.scope == MARKET_SCAN_FULL_MARKET_SCOPE
        and run.rule_version.startswith(prefix)
        and len(digest) == 64
        and all(char in "0123456789abcdef" for char in digest)
    )


def _validate_published_run(run: MarketScanRun) -> None:
    if run.status not in {"success", "degraded"}:
        if any(
            value is not None
            for value in (
                run.snapshot_digest,
                run.snapshot_seal_origin,
                run.snapshot_sealed_at,
            )
        ):
            raise ValueError("未发布批次不得包含快照封印字段")
        return
    if run.processed_count != run.total_count or run.progress_pct != 100:
        raise ValueError("已发布批次必须完成全部股票处理")
    if run.finished_at is None and run.snapshot_seal_origin == "publication":
        raise ValueError("已发布批次必须包含完成时间")
    if run.current_stage is not None or run.stage_started_at is not None:
        raise ValueError("已发布批次不能保留运行中阶段")
    _validate_published_seal_contract(run)


def _validate_published_seal_contract(run: MarketScanRun) -> None:
    if (
        run.snapshot_digest is None
        or run.snapshot_seal_origin is None
        or run.snapshot_sealed_at is None
    ):
        raise ValueError("已发布批次必须包含完整快照封印来源与时间")
    if run.snapshot_seal_origin == "publication":
        assert run.finished_at is not None
        finished_at = _comparable_timestamp(run.finished_at)
        updated_at = _comparable_timestamp(run.updated_at)
        sealed_at = _comparable_timestamp(run.snapshot_sealed_at)
        if _timestamp_before(updated_at, finished_at):
            raise ValueError("已发布批次 updated_at 不能早于 finished_at")
        if _timestamp_before(sealed_at, updated_at):
            raise ValueError("已发布批次 snapshot_sealed_at 不能早于 updated_at")


def _validate_success_result(item: MarketScanResultItem, run: MarketScanRun) -> None:
    required = {
        "score": item.score, "raw_score": item.raw_score,
        "trend_score": item.trend_score, "leader_score": item.leader_score,
        "data_quality_score": item.data_quality_score, "price": item.price,
        "data_date": item.data_date, "quote_timestamp": item.quote_timestamp,
        "quote_observed_at": item.quote_observed_at, "quote_source": item.quote_source,
        "kline_source": item.kline_source,
    }
    missing = next((field_name for field_name, value in required.items() if value is None or value == ""), None)
    if missing is not None:
        raise ValueError(f"success 榜单项目缺少 {missing}")
    if item.data_date != run.data_date:
        raise ValueError("榜单项目 data_date 与批次不一致")
    if item.adjustment_mode != "qfq":
        raise ValueError("success 榜单项目 adjustment_mode 必须是 qfq")
    if run.status in {"success", "degraded"} and item.rank is None:
        raise ValueError("已发布 success 榜单项目缺少 rank")
    if item.error is not None:
        raise ValueError("success 榜单项目不能包含 error")
    _required_timestamp(item.quote_timestamp or "", "result.quote_timestamp")
    _required_timestamp(item.quote_observed_at or "", "result.quote_observed_at")


def _validate_non_success_result(item: MarketScanResultItem) -> None:
    for field_name in ("rank", "score", "raw_score", "trend_score", "leader_score", "data_quality_score"):
        if getattr(item, field_name) is not None:
            raise ValueError(f"非 success 榜单项目 {field_name} 必须为空")
    if item.status == "pending" and (item.reason is not None or item.error is not None):
        raise ValueError("pending 榜单项目不能包含 reason/error")
    if item.status in {"missing", "skipped"} and not str(item.reason or item.error or "").strip():
        raise ValueError(f"{item.status} 榜单项目必须包含 reason 或 error")


def _percentage(numerator: int, denominator: int) -> float:
    if denominator <= 0:
        return 0.0
    return round(min(100.0, max(0.0, numerator / denominator * 100)), 2)


def _required_iso_date(value: str, field: str) -> None:
    try:
        parsed = date.fromisoformat(value)
    except (TypeError, ValueError) as exc:
        raise ValueError(f"{field} 必须是有效 YYYY-MM-DD 日期") from exc
    if parsed.isoformat() != value:
        raise ValueError(f"{field} 必须是规范 YYYY-MM-DD 日期")


def _required_timestamp(value: str, field: str) -> datetime:
    if not isinstance(value, str) or re.fullmatch(
        r"\d{4}-\d{2}-\d{2}[ T]\d{2}:\d{2}:\d{2}(?:\.\d+)?(?:Z|[+-]\d{2}:\d{2})?",
        value,
    ) is None:
        raise ValueError(f"{field} 必须是有效 ISO 时间")
    try:
        return datetime.fromisoformat(value.replace("Z", "+00:00"))
    except (AttributeError, TypeError, ValueError) as exc:
        raise ValueError(f"{field} 必须是有效 ISO 时间") from exc


def _optional_timestamp(value: str | None, field: str) -> datetime | None:
    return None if value is None else _required_timestamp(value, field)


def _timestamp_before(left: datetime, right: datetime) -> bool:
    if (left.tzinfo is None) != (right.tzinfo is None):
        return False
    return left < right


def _comparable_timestamp(value: str) -> datetime:
    _required_timestamp(value, "timestamp")
    return parse_audit_time(value)


__all__ = [
    "MARKET_SCAN_MIN_HISTORY_ROWS",
    "MARKET_SCAN_RANK_TIE_BREAK",
    "MARKET_SCAN_TOP100_REFRESH_LIMIT",
    "MARKET_SCAN_TOP100_REFRESH_SCOPE",
    "MARKET_SCAN_PUBLICATION_DIAGNOSTICS_SCHEMA_VERSION",
    "MARKET_SCAN_PUBLICATION_DIAGNOSTIC_SEVERITIES",
    "MarketScanCoverage",
    "MarketScanCoverageScope",
    "MarketScanAutomaticState",
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
    "MarketScanProductionScoreContract",
    "MarketScanScoreDistribution",
    "MarketScanScoreDistributionAssessment",
    "MarketScanScoreDistributionGateStatus",
    "MarketScanScoreDistributionObservation",
    "MarketScanScoreDistributionPolicy",
    "MarketScanScoreComponentDiagnostic",
    "MarketScanScoreComponentName",
    "MarketScanScoreLayerDiagnostic",
    "MarketScanScoreLayerName",
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
