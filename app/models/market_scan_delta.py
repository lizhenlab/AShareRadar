"""Typed, deterministic comparison contract for published market-scan cohorts."""

from __future__ import annotations

from collections import Counter
from datetime import date, datetime
from math import isclose
from typing import Final, Literal, Self

from pydantic import BaseModel, ConfigDict, Field, model_validator

from app.artifacts.io import canonical_json_bytes, sha256_hex
from app.models.market_scan import (
    MARKET_SCAN_FULL_MARKET_SCOPE,
    MarketScanMode,
    MarketScanRunStatus,
    MarketScanSnapshotSealOrigin,
)


MARKET_SCAN_DELTA_SCHEMA_VERSION: Final[Literal["market-scan-delta-v1"]] = (
    "market-scan-delta-v1"
)
MARKET_SCAN_DELTA_TOP_THRESHOLDS: Final[tuple[Literal[20, 50, 100], ...]] = (
    20,
    50,
    100,
)

MarketScanDeltaStatus = Literal["ready", "unavailable"]
MarketScanDeltaUnavailableReason = Literal[
    "current_not_published",
    "current_not_full_market",
    "previous_same_cohort_not_found",
]
MarketScanDeltaMembershipReason = Literal[
    "instrument_new_in_current_universe",
    "instrument_absent_from_current_universe",
    "became_rankable",
    "crossed_into_top_n",
    "crossed_out_of_top_n",
    "present_but_unrankable",
    "current_status_pending",
    "current_status_missing",
    "current_status_skipped",
    "current_rank_missing",
]
MarketScanDeltaMovementReason = Literal[
    "rank_improved",
    "rank_declined",
    "rank_unchanged",
    "score_increased",
    "score_decreased",
    "score_unchanged",
]
MarketScanDeltaEvidenceReason = Literal[
    "status_changed",
    "quote_source_changed",
    "kline_source_changed",
    "metadata_source_changed",
    "quote_fallback_changed",
    "kline_fallback_changed",
    "metadata_degradation_changed",
    "degradation_reasons_changed",
]


class _FrozenModel(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)


class MarketScanDeltaRunRef(_FrozenModel):
    run_id: int = Field(ge=1)
    status: MarketScanRunStatus
    mode: MarketScanMode
    scope: str = Field(min_length=1)
    rule_version: str = Field(min_length=1)
    data_date: str = Field(min_length=1)
    finished_at: str | None = None
    snapshot_digest: str | None = Field(default=None, pattern=r"^[0-9a-f]{64}$")
    snapshot_seal_origin: MarketScanSnapshotSealOrigin | None = None
    snapshot_sealed_at: str | None = None

    @model_validator(mode="after")
    def validate_published_snapshot(self) -> Self:
        _require_iso_date(self.data_date, "delta run data_date")
        fields = (self.snapshot_digest, self.snapshot_seal_origin, self.snapshot_sealed_at)
        if self.status in {"success", "degraded"}:
            if self.finished_at is None or any(value is None for value in fields):
                raise ValueError("已发布变化证据缺少完成时间或快照封印")
            finished = _parsed_timestamp(self.finished_at, "delta run finished_at")
            sealed = _parsed_timestamp(self.snapshot_sealed_at or "", "delta run snapshot_sealed_at")
            if not _timestamps_comparable(finished, sealed) or sealed < finished:
                raise ValueError("变化证据快照封印时间不能早于批次完成时间")
        elif self.finished_at is not None or any(value is not None for value in fields):
            raise ValueError("未发布变化证据不能包含完成时间或快照封印")
        return self


class MarketScanDeltaCohort(_FrozenModel):
    mode: MarketScanMode
    scope: str = Field(min_length=1)
    rule_version: str = Field(min_length=1)


class MarketScanDeltaMembershipItem(_FrozenModel):
    symbol: str = Field(min_length=1)
    name: str = Field(min_length=1)
    market: str = Field(min_length=1)
    industry: str | None = None
    previous_rank: int | None = Field(default=None, ge=1)
    current_rank: int | None = Field(default=None, ge=1)
    previous_raw_score: float | None = Field(default=None, allow_inf_nan=False)
    current_raw_score: float | None = Field(default=None, allow_inf_nan=False)
    reason_codes: tuple[MarketScanDeltaMembershipReason, ...] = ()


class MarketScanDeltaTopBucket(_FrozenModel):
    top_n: Literal[20, 50, 100]
    previous_count: int = Field(ge=0)
    current_count: int = Field(ge=0)
    retained_count: int = Field(ge=0)
    entrants: tuple[MarketScanDeltaMembershipItem, ...] = ()
    exits: tuple[MarketScanDeltaMembershipItem, ...] = ()
    present_but_unrankable: tuple[MarketScanDeltaMembershipItem, ...] = ()

    @model_validator(mode="after")
    def validate_bucket_counts(self) -> Self:
        if self.previous_count > self.top_n or self.current_count > self.top_n:
            raise ValueError("Top-N 数量不能大于分桶阈值")
        if self.retained_count > min(self.previous_count, self.current_count):
            raise ValueError("Top-N 保留数量不能大于前后批次数量")
        if self.current_count != self.retained_count + len(self.entrants):
            raise ValueError("Top-N 当前数量与保留/进入数量不守恒")
        if self.previous_count != self.retained_count + len(self.exits) + len(self.present_but_unrankable):
            raise ValueError("Top-N 上期数量与保留/退出数量不守恒")
        _validate_bucket_symbols(self)
        return self


class MarketScanDeltaRankScoreChange(_FrozenModel):
    symbol: str = Field(min_length=1)
    name: str = Field(min_length=1)
    market: str = Field(min_length=1)
    industry: str | None = None
    previous_rank: int = Field(ge=1)
    current_rank: int = Field(ge=1)
    rank_change: int
    previous_raw_score: float | None = Field(default=None, allow_inf_nan=False)
    current_raw_score: float | None = Field(default=None, allow_inf_nan=False)
    raw_score_change: float | None = Field(default=None, allow_inf_nan=False)
    reason_codes: tuple[MarketScanDeltaMovementReason, ...]

    @model_validator(mode="after")
    def validate_movement(self) -> Self:
        _validate_symbol_market(self.symbol, self.market)
        if self.rank_change != self.previous_rank - self.current_rank:
            raise ValueError("排名变化与前后排名不一致")
        expected_score_change = _optional_difference(self.previous_raw_score, self.current_raw_score)
        if not _optional_close(self.raw_score_change, expected_score_change):
            raise ValueError("分数变化与前后分数不一致")
        expected_reasons = (_rank_reason(self.rank_change), _score_reason(expected_score_change))
        if self.reason_codes != expected_reasons:
            raise ValueError("排名/分数变化原因与数值不一致")
        return self


class MarketScanDeltaExposureChange(_FrozenModel):
    top_n: Literal[20, 50, 100]
    dimension: Literal["market", "industry"]
    category: str = Field(min_length=1)
    previous_count: int = Field(ge=0)
    current_count: int = Field(ge=0)
    count_change: int
    previous_share: float = Field(ge=0, le=1, allow_inf_nan=False)
    current_share: float = Field(ge=0, le=1, allow_inf_nan=False)
    share_change: float = Field(ge=-1, le=1, allow_inf_nan=False)

    @model_validator(mode="after")
    def validate_exposure_change(self) -> Self:
        if self.count_change != self.current_count - self.previous_count:
            raise ValueError("暴露数量变化与前后数量不一致")
        if not isclose(self.share_change, self.current_share - self.previous_share, abs_tol=1e-12):
            raise ValueError("暴露占比变化与前后占比不一致")
        return self


class MarketScanDeltaReasonCount(_FrozenModel):
    code: MarketScanDeltaEvidenceReason
    count: int = Field(ge=1)


class MarketScanDeltaEvidenceState(_FrozenModel):
    status: str = Field(min_length=1)
    quote_source: str | None = None
    kline_source: str | None = None
    metadata_source: str | None = None
    quote_fallback_used: bool
    kline_fallback_used: bool
    metadata_degraded: bool
    degradation_reasons: tuple[str, ...] = ()


class MarketScanDeltaEvidenceChange(_FrozenModel):
    symbol: str = Field(min_length=1)
    reason_codes: tuple[MarketScanDeltaEvidenceReason, ...]
    previous: MarketScanDeltaEvidenceState
    current: MarketScanDeltaEvidenceState


class MarketScanDeltaSummary(_FrozenModel):
    previous_present_count: int = Field(ge=0)
    current_present_count: int = Field(ge=0)
    compared_symbol_count: int = Field(ge=0)
    evidence_detail_scope: Literal["top100_union"] = "top100_union"
    evidence_change_reason_counts: tuple[MarketScanDeltaReasonCount, ...] = ()


class MarketScanDeltaResponse(_FrozenModel):
    schema_version: Literal["market-scan-delta-v1"] = MARKET_SCAN_DELTA_SCHEMA_VERSION
    status: MarketScanDeltaStatus
    unavailable_reason: MarketScanDeltaUnavailableReason | None = None
    current: MarketScanDeltaRunRef
    previous: MarketScanDeltaRunRef | None = None
    cohort: MarketScanDeltaCohort
    summary: MarketScanDeltaSummary
    top_buckets: tuple[MarketScanDeltaTopBucket, ...] = ()
    rank_score_changes: tuple[MarketScanDeltaRankScoreChange, ...] = ()
    exposure_changes: tuple[MarketScanDeltaExposureChange, ...] = ()
    evidence_changes: tuple[MarketScanDeltaEvidenceChange, ...] = ()
    canonical_digest: str = Field(pattern=r"^[0-9a-f]{64}$")

    @model_validator(mode="after")
    def validate_delta_contract(self) -> Self:
        _validate_cohort(self)
        if self.status == "unavailable":
            _validate_unavailable_delta(self)
        else:
            _validate_ready_delta(self)
        _require_canonical_digest(self)
        return self


def _require_iso_date(value: str, field: str) -> None:
    try:
        parsed = date.fromisoformat(value)
    except (TypeError, ValueError) as exc:
        raise ValueError(f"{field} 必须是有效日期") from exc
    if parsed.isoformat() != value:
        raise ValueError(f"{field} 必须是规范日期")


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
        raise ValueError("canonical_digest 与变化响应内容不一致")


def _validate_symbol_market(symbol: str, market: str) -> None:
    if market not in {"SH", "SZ", "BJ"} or not symbol.endswith(f".{market}"):
        raise ValueError("变化项目 symbol 与 market 不一致")


def _validate_membership_item(item: MarketScanDeltaMembershipItem) -> None:
    _validate_symbol_market(item.symbol, item.market)
    if not item.reason_codes or len(item.reason_codes) != len(set(item.reason_codes)):
        raise ValueError("Top-N 成员原因不能为空或重复")


def _validate_bucket_symbols(bucket: MarketScanDeltaTopBucket) -> None:
    groups = (bucket.entrants, bucket.exits, bucket.present_but_unrankable)
    symbols: list[str] = []
    for group in groups:
        for item in group:
            _validate_membership_item(item)
            symbols.append(item.symbol)
    if len(symbols) != len(set(symbols)):
        raise ValueError("Top-N 进入/退出/不可排名股票不能重复或重叠")


def _optional_difference(before: float | None, after: float | None) -> float | None:
    return None if before is None or after is None else after - before


def _optional_close(left: float | None, right: float | None) -> bool:
    if left is None or right is None:
        return left is right
    return isclose(left, right, abs_tol=1e-12)


def _rank_reason(change: int) -> MarketScanDeltaMovementReason:
    return "rank_improved" if change > 0 else "rank_declined" if change < 0 else "rank_unchanged"


def _score_reason(change: float | None) -> MarketScanDeltaMovementReason:
    if change is not None and change > 0:
        return "score_increased"
    if change is not None and change < 0:
        return "score_decreased"
    return "score_unchanged"


def _validate_cohort(response: MarketScanDeltaResponse) -> None:
    current = response.current
    if (response.cohort.mode, response.cohort.scope, response.cohort.rule_version) != (
        current.mode,
        current.scope,
        current.rule_version,
    ):
        raise ValueError("变化 cohort 与当前批次不一致")


def _validate_unavailable_delta(response: MarketScanDeltaResponse) -> None:
    if response.unavailable_reason is None:
        raise ValueError("不可用变化响应必须包含原因")
    if response.previous is not None or any(
        (response.top_buckets, response.rank_score_changes, response.exposure_changes, response.evidence_changes)
    ):
        raise ValueError("不可用变化响应不能包含上一批次或派生变化")
    summary = response.summary
    if summary.previous_present_count or summary.compared_symbol_count or summary.evidence_change_reason_counts:
        raise ValueError("不可用变化响应摘要不能包含对比证据")
    _validate_unavailable_reason(response)


def _validate_unavailable_reason(response: MarketScanDeltaResponse) -> None:
    reason = response.unavailable_reason
    published = response.current.status in {"success", "degraded"}
    if reason == "current_not_published" and published:
        raise ValueError("不可用原因与当前批次发布状态矛盾")
    if reason == "current_not_full_market" and (not published or response.current.scope == MARKET_SCAN_FULL_MARKET_SCOPE):
        raise ValueError("不可用原因与当前批次范围矛盾")
    if reason == "previous_same_cohort_not_found" and (
        not published or response.current.scope != MARKET_SCAN_FULL_MARKET_SCOPE
    ):
        raise ValueError("缺少上一批次原因要求当前批次为正式全市场快照")


def _validate_ready_delta(response: MarketScanDeltaResponse) -> None:
    if response.unavailable_reason is not None or response.previous is None:
        raise ValueError("可用变化响应必须包含上一批次且不能包含不可用原因")
    _validate_previous_cohort(response)
    if response.current.scope != MARKET_SCAN_FULL_MARKET_SCOPE:
        raise ValueError("可用变化响应必须来自完整全市场批次")
    summary = response.summary
    if summary.compared_symbol_count > min(summary.previous_present_count, summary.current_present_count):
        raise ValueError("对比股票数不能大于前后批次股票数")
    if tuple(item.top_n for item in response.top_buckets) != MARKET_SCAN_DELTA_TOP_THRESHOLDS:
        raise ValueError("变化响应必须完整包含 Top20/50/100 分桶")
    _validate_unique_delta_symbols(response)
    _validate_evidence_reason_counts(response)


def _validate_previous_cohort(response: MarketScanDeltaResponse) -> None:
    previous = response.previous
    if previous is None:
        raise ValueError("变化响应缺少上一批次")
    expected = (response.cohort.mode, response.cohort.scope, response.cohort.rule_version)
    if (previous.mode, previous.scope, previous.rule_version) != expected:
        raise ValueError("上一批次与变化 cohort 不一致")
    if previous.run_id == response.current.run_id:
        raise ValueError("变化响应前后批次不能相同")
    previous_finished = _parsed_timestamp(previous.finished_at or "", "previous.finished_at")
    current_finished = _parsed_timestamp(response.current.finished_at or "", "current.finished_at")
    if not _timestamps_comparable(previous_finished, current_finished) or previous_finished >= current_finished:
        raise ValueError("上一批次完成时间必须早于当前批次")


def _validate_unique_delta_symbols(response: MarketScanDeltaResponse) -> None:
    for name, items in (
        ("rank_score_changes", response.rank_score_changes),
        ("evidence_changes", response.evidence_changes),
    ):
        symbols = [item.symbol for item in items]
        if len(symbols) != len(set(symbols)):
            raise ValueError(f"{name} 不能包含重复股票")


def _validate_evidence_reason_counts(response: MarketScanDeltaResponse) -> None:
    expected = Counter(code for item in response.evidence_changes for code in item.reason_codes)
    observed = {item.code: item.count for item in response.summary.evidence_change_reason_counts}
    if len(observed) != len(response.summary.evidence_change_reason_counts) or observed != expected:
        raise ValueError("证据变化原因计数与明细不一致")


__all__ = [
    "MARKET_SCAN_DELTA_SCHEMA_VERSION",
    "MARKET_SCAN_DELTA_TOP_THRESHOLDS",
    "MarketScanDeltaCohort",
    "MarketScanDeltaEvidenceChange",
    "MarketScanDeltaEvidenceReason",
    "MarketScanDeltaEvidenceState",
    "MarketScanDeltaExposureChange",
    "MarketScanDeltaMembershipItem",
    "MarketScanDeltaMembershipReason",
    "MarketScanDeltaMovementReason",
    "MarketScanDeltaReasonCount",
    "MarketScanDeltaResponse",
    "MarketScanDeltaRunRef",
    "MarketScanDeltaStatus",
    "MarketScanDeltaSummary",
    "MarketScanDeltaTopBucket",
    "MarketScanDeltaUnavailableReason",
]
