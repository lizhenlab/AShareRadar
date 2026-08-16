"""Typed contracts for read-only screening over frozen market-scan rows."""

from __future__ import annotations

from datetime import date, datetime
from typing import Literal, Self

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

from app.artifacts.io import canonical_json_bytes, sha256_hex
from app.models.market_scan import (
    MARKET_SCAN_FULL_MARKET_SCOPE,
    MarketScanResultItem,
    MarketScanResultStatus,
    MarketScanRunStatus,
    MarketScanSnapshotSealOrigin,
)


ScreenSortField = Literal[
    "rank", "score", "raw_score", "trend_score", "change_pct", "amount",
    "turnover_rate", "data_quality_score", "alpha_5d", "confidence", "risk",
    "tradability", "symbol", "market", "industry", "is_st", "is_new",
]
ScreenSortOrder = Literal["asc", "desc"]
ScreenRangeField = Literal[
    "score", "trend_score", "change_pct", "turnover_rate", "amount",
    "data_quality_score", "confidence", "risk", "tradability",
]


class _StrictScreenModel(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True, str_strip_whitespace=True)


class ScreenNumericRange(_StrictScreenModel):
    min: float | None = Field(default=None, allow_inf_nan=False)
    max: float | None = Field(default=None, allow_inf_nan=False)

    @model_validator(mode="after")
    def validate_order(self) -> Self:
        if self.min is None and self.max is None:
            raise ValueError("数值范围至少需要一个边界")
        if self.min is not None and self.max is not None and self.min > self.max:
            raise ValueError("范围下限不能大于上限")
        return self


class ScreenBoundedScoreRange(ScreenNumericRange):
    min: float | None = Field(default=None, ge=0, le=100, allow_inf_nan=False)
    max: float | None = Field(default=None, ge=0, le=100, allow_inf_nan=False)


class ScreenChangeRange(ScreenNumericRange):
    min: float | None = Field(default=None, ge=-1000, le=1000, allow_inf_nan=False)
    max: float | None = Field(default=None, ge=-1000, le=1000, allow_inf_nan=False)


class ScreenTurnoverRange(ScreenNumericRange):
    min: float | None = Field(default=None, ge=0, le=10_000, allow_inf_nan=False)
    max: float | None = Field(default=None, ge=0, le=10_000, allow_inf_nan=False)


class ScreenAmountRange(ScreenNumericRange):
    min: float | None = Field(default=None, ge=0, le=1_000_000_000_000_000, allow_inf_nan=False)
    max: float | None = Field(default=None, ge=0, le=1_000_000_000_000_000, allow_inf_nan=False)


class ScreenRangesV2(_StrictScreenModel):
    score: ScreenBoundedScoreRange | None = None
    trend_score: ScreenBoundedScoreRange | None = None
    change_pct: ScreenChangeRange | None = None
    turnover_rate: ScreenTurnoverRange | None = None
    amount: ScreenAmountRange | None = None
    data_quality_score: ScreenBoundedScoreRange | None = None
    confidence: ScreenBoundedScoreRange | None = None
    risk: ScreenBoundedScoreRange | None = None
    tradability: ScreenBoundedScoreRange | None = None


class ScreenSortV2(_StrictScreenModel):
    field: ScreenSortField
    order: ScreenSortOrder


def _default_sort() -> list[ScreenSortV2]:
    return [ScreenSortV2(field="rank", order="asc")]


class ScreenSpecV2(_StrictScreenModel):
    """Canonical executable screen; unsupported fields are rejected, never ignored."""

    schema_version: Literal["screen-spec-v2"] = "screen-spec-v2"
    status: MarketScanResultStatus | None = "success"
    markets: list[Literal["SH", "SZ", "BJ"]] = Field(default_factory=list, max_length=3)
    industries: list[str] = Field(default_factory=list, max_length=20)
    is_st: bool | None = None
    is_new: bool | None = None
    ranges: ScreenRangesV2 = Field(default_factory=ScreenRangesV2)
    keyword: str | None = Field(default=None, max_length=80)
    sort: list[ScreenSortV2] = Field(default_factory=_default_sort, min_length=1, max_length=3)

    @field_validator("markets", "industries")
    @classmethod
    def validate_unique_values(cls, values: list[str]) -> list[str]:
        values = [" ".join(value.split()).strip() for value in values]
        if len(values) != len(set(values)):
            raise ValueError("筛选值不能重复")
        if any(not value for value in values):
            raise ValueError("筛选值不能为空")
        if any(any(ord(character) < 32 or ord(character) == 127 for character in value) for value in values):
            raise ValueError("筛选值不能包含控制字符")
        return values

    @field_validator("keyword")
    @classmethod
    def normalize_keyword(cls, value: str | None) -> str | None:
        if value is None:
            return None
        normalized = " ".join(value.split()).strip()
        return normalized or None

    @field_validator("sort")
    @classmethod
    def validate_unique_sort_fields(cls, values: list[ScreenSortV2]) -> list[ScreenSortV2]:
        fields = [item.field for item in values]
        if len(fields) != len(set(fields)):
            raise ValueError("排序字段不能重复")
        return values


class MarketScanScreenEvidence(_StrictScreenModel):
    run_id: int = Field(ge=1)
    status: MarketScanRunStatus
    mode: Literal["official", "intraday", "preopen"]
    scope: str
    data_date: str
    quote_date: str
    rule_version: str
    finished_at: str | None = None
    snapshot_digest: str = Field(pattern=r"^[0-9a-f]{64}$")
    snapshot_seal_origin: MarketScanSnapshotSealOrigin
    snapshot_sealed_at: str

    @model_validator(mode="after")
    def validate_frozen_evidence(self) -> Self:
        if self.status not in {"success", "degraded"}:
            raise ValueError("可信筛选证据必须来自已发布批次")
        if self.scope != MARKET_SCAN_FULL_MARKET_SCOPE:
            raise ValueError("可信筛选证据必须来自完整全市场批次")
        _require_iso_date(self.data_date, "evidence.data_date")
        _require_iso_date(self.quote_date, "evidence.quote_date")
        if self.finished_at is None:
            raise ValueError("可信筛选证据缺少批次完成时间")
        finished = _parsed_timestamp(self.finished_at, "evidence.finished_at")
        sealed = _parsed_timestamp(self.snapshot_sealed_at, "evidence.snapshot_sealed_at")
        if not _timestamps_comparable(finished, sealed) or sealed < finished:
            raise ValueError("快照封印时间不能早于批次完成时间")
        return self


class MarketBreadthPopulation(_StrictScreenModel):
    total: int = Field(ge=0)
    by_status: dict[str, int]
    by_market: dict[str, int]


class MarketBreadthBin(_StrictScreenModel):
    lower: float = Field(allow_inf_nan=False)
    upper: float = Field(allow_inf_nan=False)
    count: int = Field(ge=0)


class MarketBreadthScore(_StrictScreenModel):
    present_count: int = Field(ge=0)
    missing_count: int = Field(ge=0)
    min: float | None = Field(default=None, allow_inf_nan=False)
    max: float | None = Field(default=None, allow_inf_nan=False)
    mean: float | None = Field(default=None, allow_inf_nan=False)
    percentiles: dict[str, float | None]
    bins: list[MarketBreadthBin]


class MarketBreadthChange(_StrictScreenModel):
    advancing: int = Field(ge=0)
    flat: int = Field(ge=0)
    declining: int = Field(ge=0)
    missing: int = Field(ge=0)


class MarketBreadthIndustry(_StrictScreenModel):
    industry: str | None = None
    count: int = Field(ge=0)
    score_present_count: int = Field(ge=0)
    average_score: float | None = Field(default=None, allow_inf_nan=False)


class MarketBreadthV1(_StrictScreenModel):
    schema_version: Literal["market-scan-breadth-v1"] = "market-scan-breadth-v1"
    evidence: MarketScanScreenEvidence
    population: MarketBreadthPopulation
    score: MarketBreadthScore
    change: MarketBreadthChange
    industries: list[MarketBreadthIndustry]
    canonical_digest: str = Field(pattern=r"^[0-9a-f]{64}$")

    @model_validator(mode="after")
    def validate_breadth_contract(self) -> Self:
        _validate_population_counts(self.population)
        _validate_score_summary(self.score, self.population.total)
        if sum(self.change.model_dump().values()) != self.population.total:
            raise ValueError("涨跌分布与总体数量不守恒")
        _validate_industry_counts(self.industries, self.population.total, self.score.present_count)
        _require_canonical_digest(self)
        return self


class MarketScanFunnelStep(_StrictScreenModel):
    index: int = Field(ge=1)
    condition_code: str
    label: str
    input_count: int = Field(ge=0)
    matched_count: int = Field(ge=0)
    excluded_count: int = Field(ge=0)
    missing_count: int = Field(ge=0)


class MarketScanExclusionReason(_StrictScreenModel):
    code: str
    label: str
    count: int = Field(ge=0)
    missing_count: int = Field(ge=0)


class MarketScanFailedCondition(_StrictScreenModel):
    code: str
    label: str
    missing: bool = False


class MarketScanNearMiss(_StrictScreenModel):
    item: MarketScanResultItem
    failed_conditions: list[MarketScanFailedCondition]


class MarketScanMatchExplanation(_StrictScreenModel):
    symbol: str
    passed_conditions: list[str]


class MarketScanScreenMatchedPage(_StrictScreenModel):
    items: list[MarketScanResultItem]
    total: int = Field(ge=0)
    page: int = Field(ge=1)
    page_size: int = Field(ge=1, le=200)
    page_count: int = Field(ge=0)

    @model_validator(mode="after")
    def validate_page_shape(self) -> Self:
        expected_page_count = (self.total + self.page_size - 1) // self.page_size if self.total else 0
        if self.page_count != expected_page_count:
            raise ValueError("筛选分页 page_count 与 total/page_size 不一致")
        expected_items = 0 if self.page > self.page_count else min(
            self.page_size,
            self.total - (self.page - 1) * self.page_size,
        )
        if len(self.items) != expected_items:
            raise ValueError("筛选分页 items 数量与当前页不一致")
        return self


class MarketScanScreenEvaluateRequest(_StrictScreenModel):
    spec: ScreenSpecV2 = Field(default_factory=ScreenSpecV2)
    page: int = Field(default=1, ge=1)
    page_size: int = Field(default=100, ge=1, le=200)
    near_miss_limit: int = Field(default=20, ge=0, le=100)
    near_miss_max_failures: int = Field(default=1, ge=1, le=3)


class MarketScanScreenEvaluationV1(_StrictScreenModel):
    schema_version: Literal["market-scan-screen-evaluation-v1"] = (
        "market-scan-screen-evaluation-v1"
    )
    evidence: MarketScanScreenEvidence
    spec: ScreenSpecV2
    spec_digest: str = Field(pattern=r"^[0-9a-f]{64}$")
    population_count: int = Field(ge=0)
    matched_count: int = Field(ge=0)
    funnel: list[MarketScanFunnelStep]
    exclusion_reasons: list[MarketScanExclusionReason]
    matched: MarketScanScreenMatchedPage
    matched_explanations: list[MarketScanMatchExplanation]
    near_misses: list[MarketScanNearMiss]
    canonical_digest: str = Field(pattern=r"^[0-9a-f]{64}$")

    @model_validator(mode="after")
    def validate_evaluation_contract(self) -> Self:
        if self.spec_digest != sha256_hex(canonical_json_bytes(self.spec.model_dump(mode="json"))):
            raise ValueError("筛选规则摘要与规则内容不一致")
        if self.matched_count > self.population_count or self.matched.total != self.matched_count:
            raise ValueError("筛选命中数量与总体或分页不一致")
        condition_codes = _screen_condition_codes(self.spec)
        _validate_funnel(self.funnel, condition_codes, self.population_count, self.matched_count)
        _validate_exclusion_reasons(self.exclusion_reasons, condition_codes, self.population_count)
        matched_symbols = _validated_result_symbols(self.matched.items, self.evidence.run_id)
        _validate_explanations(self.matched_explanations, matched_symbols, condition_codes)
        _validate_near_misses(self.near_misses, matched_symbols, self.evidence.run_id, condition_codes)
        _require_canonical_digest(self)
        return self


def _require_iso_date(value: str, field: str) -> None:
    try:
        parsed = date.fromisoformat(value)
    except (TypeError, ValueError) as exc:
        raise ValueError(f"{field} 必须是有效 YYYY-MM-DD 日期") from exc
    if parsed.isoformat() != value:
        raise ValueError(f"{field} 必须是规范 YYYY-MM-DD 日期")


def _require_timestamp(value: str, field: str) -> None:
    _parsed_timestamp(value, field)


def _parsed_timestamp(value: str, field: str) -> datetime:
    if len(value) < 19 or value[10] not in {"T", " "}:
        raise ValueError(f"{field} 必须包含日期与时间")
    try:
        return datetime.fromisoformat(value.replace("Z", "+00:00"))
    except (AttributeError, TypeError, ValueError) as exc:
        raise ValueError(f"{field} 必须是有效 ISO 时间") from exc


def _timestamps_comparable(left: datetime, right: datetime) -> bool:
    return (left.utcoffset() is None) == (right.utcoffset() is None)


def _require_canonical_digest(value: BaseModel) -> None:
    payload = value.model_dump(mode="json", exclude={"canonical_digest"})
    if getattr(value, "canonical_digest") != sha256_hex(canonical_json_bytes(payload)):
        raise ValueError("canonical_digest 与响应内容不一致")


def _validate_nonnegative_counts(values: dict[str, int], field: str) -> None:
    if any(isinstance(value, bool) or value < 0 for value in values.values()):
        raise ValueError(f"{field} 必须只包含非负整数")


def _validate_population_counts(population: MarketBreadthPopulation) -> None:
    _validate_nonnegative_counts(population.by_status, "population.by_status")
    _validate_nonnegative_counts(population.by_market, "population.by_market")
    if set(population.by_status) - {"pending", "success", "missing", "skipped"}:
        raise ValueError("population.by_status 包含未知状态")
    if set(population.by_market) - {"SH", "SZ", "BJ"}:
        raise ValueError("population.by_market 包含未知市场")
    if sum(population.by_status.values()) != population.total:
        raise ValueError("population.by_status 与总体数量不守恒")
    if sum(population.by_market.values()) != population.total:
        raise ValueError("population.by_market 与总体数量不守恒")


def _validate_score_summary(score: MarketBreadthScore, total: int) -> None:
    if score.present_count + score.missing_count != total:
        raise ValueError("评分可用/缺失数量与总体不守恒")
    values = (score.min, score.max, score.mean)
    if score.present_count == 0 and any(value is not None for value in values):
        raise ValueError("无可用评分时统计值必须为空")
    if score.present_count > 0 and any(value is None for value in values):
        raise ValueError("存在可用评分时统计值不能为空")
    if any(value is not None and not 0 <= value <= 100 for value in values):
        raise ValueError("评分统计值必须位于 0 至 100")
    _validate_percentiles(score)
    _validate_score_bins(score)


def _validate_percentiles(score: MarketBreadthScore) -> None:
    labels = ("p10", "p25", "p50", "p75", "p90")
    if tuple(score.percentiles) != labels:
        raise ValueError("评分分位数字段不完整或顺序异常")
    values = [score.percentiles[label] for label in labels]
    if score.present_count == 0 and any(value is not None for value in values):
        raise ValueError("无可用评分时分位数必须为空")
    if score.present_count > 0 and any(value is None for value in values):
        raise ValueError("存在可用评分时分位数不能为空")
    present = [float(value) for value in values if value is not None]
    if present != sorted(present) or any(not 0 <= value <= 100 for value in present):
        raise ValueError("评分分位数必须有序且位于 0 至 100")


def _validate_score_bins(score: MarketBreadthScore) -> None:
    expected = [(float(lower), float(lower + 10)) for lower in range(0, 100, 10)]
    observed = [(item.lower, item.upper) for item in score.bins]
    if observed != expected or sum(item.count for item in score.bins) != score.present_count:
        raise ValueError("评分区间或区间计数与可用评分不一致")


def _validate_industry_counts(
    industries: list[MarketBreadthIndustry],
    total: int,
    score_present_count: int,
) -> None:
    if len({item.industry for item in industries}) != len(industries):
        raise ValueError("行业宽度不能包含重复行业")
    if sum(item.count for item in industries) != total:
        raise ValueError("行业数量与总体数量不守恒")
    if sum(item.score_present_count for item in industries) != score_present_count:
        raise ValueError("行业评分数量与总体评分数量不守恒")
    for item in industries:
        if item.score_present_count > item.count:
            raise ValueError("行业评分数量不能大于行业数量")
        if (item.score_present_count == 0) != (item.average_score is None):
            raise ValueError("行业平均分可用性与评分数量不一致")
        if item.average_score is not None and not 0 <= item.average_score <= 100:
            raise ValueError("行业平均分必须位于 0 至 100")


def _screen_condition_codes(spec: ScreenSpecV2) -> tuple[str, ...]:
    codes: list[str] = []
    for present, code in (
        (spec.status is not None, "status"),
        (bool(spec.markets), "market"),
        (bool(spec.industries), "industry"),
        (spec.is_st is not None, "is_st"),
        (spec.is_new is not None, "is_new"),
    ):
        if present:
            codes.append(code)
    codes.extend(
        f"range.{field}" for field in ScreenRangesV2.model_fields if getattr(spec.ranges, field) is not None
    )
    if spec.keyword:
        codes.append("keyword")
    return tuple(codes)


def _validate_funnel(
    steps: list[MarketScanFunnelStep],
    condition_codes: tuple[str, ...],
    population_count: int,
    matched_count: int,
) -> None:
    if [item.condition_code for item in steps] != list(condition_codes):
        raise ValueError("筛选漏斗条件与筛选规则不一致")
    previous = population_count
    for index, step in enumerate(steps, start=1):
        if step.index != index or step.input_count != previous:
            raise ValueError("筛选漏斗序号或输入数量不连续")
        if step.matched_count + step.excluded_count != step.input_count:
            raise ValueError("筛选漏斗命中与排除数量不守恒")
        if step.missing_count > step.excluded_count:
            raise ValueError("筛选漏斗缺失数量不能大于排除数量")
        previous = step.matched_count
    if previous != matched_count:
        raise ValueError("筛选漏斗最终命中数量与响应不一致")


def _validate_exclusion_reasons(
    reasons: list[MarketScanExclusionReason],
    condition_codes: tuple[str, ...],
    population_count: int,
) -> None:
    codes = [item.code for item in reasons]
    if len(codes) != len(set(codes)) or any(code not in condition_codes for code in codes):
        raise ValueError("排除原因包含重复或未知条件")
    for reason in reasons:
        if reason.count > population_count or reason.missing_count > reason.count:
            raise ValueError("排除原因计数超出总体或自身计数")


def _validated_result_symbols(items: list[MarketScanResultItem], run_id: int) -> list[str]:
    symbols: list[str] = []
    for item in items:
        if item.run_id != run_id or item.symbol != f"{item.code}.{item.market}":
            raise ValueError("筛选结果股票归属与冻结批次不一致")
        symbols.append(item.symbol)
    if len(symbols) != len(set(symbols)):
        raise ValueError("筛选结果股票不能重复")
    return symbols


def _validate_explanations(
    explanations: list[MarketScanMatchExplanation],
    matched_symbols: list[str],
    condition_codes: tuple[str, ...],
) -> None:
    if [item.symbol for item in explanations] != matched_symbols:
        raise ValueError("命中解释与当前命中分页股票不一致")
    expected = list(condition_codes) if condition_codes else ["all_conditions_passed"]
    if any(item.passed_conditions != expected for item in explanations):
        raise ValueError("命中解释与筛选规则条件不一致")


def _validate_near_misses(
    near_misses: list[MarketScanNearMiss],
    matched_symbols: list[str],
    run_id: int,
    condition_codes: tuple[str, ...],
) -> None:
    symbols = _validated_result_symbols([item.item for item in near_misses], run_id)
    if set(symbols) & set(matched_symbols):
        raise ValueError("近似命中股票不能与命中分页重叠")
    for near_miss in near_misses:
        if not near_miss.failed_conditions:
            raise ValueError("近似命中必须包含失败条件")
        base_codes = [item.code.removesuffix(".missing") for item in near_miss.failed_conditions]
        if len(base_codes) != len(set(base_codes)) or any(code not in condition_codes for code in base_codes):
            raise ValueError("近似命中包含重复或未知失败条件")


__all__ = [
    "MarketBreadthV1",
    "MarketScanFailedCondition",
    "MarketScanMatchExplanation",
    "MarketScanScreenEvaluateRequest",
    "MarketScanScreenEvaluationV1",
    "MarketScanScreenEvidence",
    "ScreenRangeField",
    "ScreenRangesV2",
    "ScreenSortField",
    "ScreenSortV2",
    "ScreenSpecV2",
]
