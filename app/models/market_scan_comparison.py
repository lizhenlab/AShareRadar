"""Bounded, audit-only comparisons of selected frozen market-scan rows."""

from __future__ import annotations

from typing import Annotated, Literal, Self

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

from app.artifacts.io import canonical_json_bytes, sha256_hex
from app.models.market_scan import MarketScanResultStatus
from app.models.market_scan_screening import MarketScanScreenEvidence


ComparisonSymbol = Annotated[str, Field(pattern=r"^[0-9]{6}\.(SH|SZ|BJ)$")]
ComparisonDigest = Annotated[str, Field(pattern=r"^[0-9a-f]{64}$")]
ComparisonScore = Annotated[int, Field(ge=0, le=100)]
ComparisonDimension = Annotated[float, Field(ge=0, le=100, allow_inf_nan=False)]


class _StrictComparisonModel(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)


class MarketScanComparisonRequest(_StrictComparisonModel):
    symbols: list[ComparisonSymbol] = Field(min_length=2, max_length=4)
    expected_snapshot_digest: ComparisonDigest

    @field_validator("symbols")
    @classmethod
    def validate_unique_symbols(cls, values: list[str]) -> list[str]:
        if len(set(values)) != len(values):
            raise ValueError("对比股票不能重复")
        return values


class MarketScanComparisonItem(_StrictComparisonModel):
    run_id: int = Field(ge=1)
    symbol: ComparisonSymbol
    code: str = Field(pattern=r"^[0-9]{6}$")
    market: Literal["SH", "SZ", "BJ"]
    name: str
    board: str
    industry: str | None
    list_date: str | None
    is_st: bool
    is_new: bool
    status: MarketScanResultStatus
    rank: int | None = Field(ge=1)
    score: ComparisonScore | None
    raw_score: ComparisonDimension | None
    trend_score: ComparisonScore | None
    leader_score: ComparisonScore | None
    data_quality_score: ComparisonScore | None
    price: float | None = Field(gt=0, allow_inf_nan=False)
    change_pct: float | None = Field(ge=-1000, le=1000, allow_inf_nan=False)
    turnover_rate: float | None = Field(ge=0, allow_inf_nan=False)
    amount: float | None = Field(ge=0, allow_inf_nan=False)
    confidence: ComparisonDimension | None
    risk: ComparisonDimension | None
    tradability: ComparisonDimension | None
    reason: str | None
    error: str | None
    data_date: str | None
    quote_timestamp: str | None
    quote_observed_at: str | None
    quote_source: str | None
    kline_source: str | None
    metadata_source: str | None
    adjustment_mode: str | None
    quote_fallback_used: bool
    kline_fallback_used: bool
    metadata_degraded: bool
    degradation_reasons: list[str]
    updated_at: str

    @model_validator(mode="after")
    def validate_frozen_identity(self) -> Self:
        if self.symbol != f"{self.code}.{self.market}":
            raise ValueError("对比股票代码与市场不一致")
        scores = (self.rank, self.score, self.raw_score, self.trend_score, self.leader_score, self.data_quality_score)
        if self.status == "success":
            _validate_success_fields(self, scores)
        else:
            _validate_non_success_fields(self, scores)
        return self


def _validate_success_fields(item: MarketScanComparisonItem, scores: tuple[int | float | None, ...]) -> None:
    required = (*scores, item.price, item.data_date, item.quote_timestamp, item.quote_observed_at, item.quote_source, item.kline_source)
    if any(value is None or value == "" for value in required):
        raise ValueError("success 对比股票缺少冻结评分或行情来源")
    if item.error is not None or item.adjustment_mode != "qfq":
        raise ValueError("success 对比股票错误或复权方式无效")


def _validate_non_success_fields(item: MarketScanComparisonItem, scores: tuple[int | float | None, ...]) -> None:
    if any(value is not None for value in scores):
        raise ValueError("非 success 对比股票的冻结排名和分数必须为空")
    if item.status in {"missing", "skipped"} and not str(item.reason or item.error or "").strip():
        raise ValueError("缺失或跳过的对比股票必须保留原因")
    if item.status == "pending" and (item.reason is not None or item.error is not None):
        raise ValueError("待处理的对比股票不能包含失败原因")


class MarketScanComparisonResponse(_StrictComparisonModel):
    schema_version: Literal["market-scan-comparison-v1"] = "market-scan-comparison-v1"
    evidence: MarketScanScreenEvidence
    requested_symbols: list[ComparisonSymbol] = Field(min_length=2, max_length=4)
    audit_only: Literal[True] = True
    ranking_basis: Literal["frozen_base"] = "frozen_base"
    action_source_eligible: bool
    items: list[MarketScanComparisonItem] = Field(min_length=2, max_length=4)
    canonical_digest: ComparisonDigest

    @model_validator(mode="after")
    def validate_comparison_binding(self) -> Self:
        if len(set(self.requested_symbols)) != len(self.requested_symbols):
            raise ValueError("对比请求股票不能重复")
        if [item.symbol for item in self.items] != self.requested_symbols:
            raise ValueError("对比结果必须完整保留请求股票的顺序")
        if any(item.run_id != self.evidence.run_id for item in self.items):
            raise ValueError("对比结果不属于同一冻结批次")
        if any(item.status == "success" and item.data_date != self.evidence.data_date for item in self.items):
            raise ValueError("success 对比结果数据日与冻结批次不一致")
        if self.action_source_eligible and self.evidence.snapshot_seal_origin != "publication":
            raise ValueError("回填封印不能证明原始发布动作来源")
        payload = self.model_dump(mode="json", exclude={"canonical_digest"})
        if self.canonical_digest != sha256_hex(canonical_json_bytes(payload)):
            raise ValueError("对比摘要与响应内容不一致")
        return self


__all__ = ["MarketScanComparisonItem", "MarketScanComparisonRequest", "MarketScanComparisonResponse"]
