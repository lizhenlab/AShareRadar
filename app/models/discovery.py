"""Typed contracts for configurable local discovery workflows."""

from __future__ import annotations

from collections.abc import Iterable, Sequence
from math import isfinite
from typing import Annotated, Literal, Self, TypeVar

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator


DISCOVERY_PRESET_FORMAT = "ashare-radar.discovery-preset"
DISCOVERY_PRESET_SCHEMA_VERSION = 2

DiscoveryMarket = Literal["SH", "SZ", "BJ"]
DiscoveryRunMode = Literal["official", "intraday", "preopen"]
DiscoverySortField = Literal[
    "rank",
    "symbol",
    "market",
    "industry",
    "is_st",
    "is_new",
    "quality",
    "trend",
    "change",
    "turnover",
    "amount",
    "score",
    "raw_score",
]
DiscoverySortOrder = Literal["asc", "desc"]
DiscoveryColumnView = Literal["overview", "trend", "liquidity", "risk", "research"]
DiscoveryRankMovement = Literal["up", "down", "unchanged", "new", "exit", "unavailable"]
DiscoveryComparisonReason = Literal["no_previous_run", "rule_version_mismatch"]

NameText = Annotated[str, Field(min_length=1, max_length=80)]
IndustryText = Annotated[str, Field(min_length=1, max_length=80)]
SymbolText = Annotated[str, Field(pattern=r"^\d{6}\.(SH|SZ|BJ)$")]
T = TypeVar("T")


class _StrictModel(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True, str_strip_whitespace=True)


class DiscoveryScoreRange(_StrictModel):
    min: int | None = Field(default=None, ge=0, le=100)
    max: int | None = Field(default=None, ge=0, le=100)

    @model_validator(mode="after")
    def validate_order(self) -> Self:
        _validate_range_order(self.min, self.max)
        return self


class DiscoveryChangeRange(_StrictModel):
    min: float | None = Field(default=None, ge=-1000, le=1000, allow_inf_nan=False)
    max: float | None = Field(default=None, ge=-1000, le=1000, allow_inf_nan=False)

    @model_validator(mode="after")
    def validate_order(self) -> Self:
        _validate_range_order(self.min, self.max)
        return self


class DiscoveryTurnoverRange(_StrictModel):
    min: float | None = Field(default=None, ge=0, le=10_000, allow_inf_nan=False)
    max: float | None = Field(default=None, ge=0, le=10_000, allow_inf_nan=False)

    @model_validator(mode="after")
    def validate_order(self) -> Self:
        _validate_range_order(self.min, self.max)
        return self


class DiscoveryAmountRange(_StrictModel):
    min: float | None = Field(default=None, ge=0, le=1_000_000_000_000_000, allow_inf_nan=False)
    max: float | None = Field(default=None, ge=0, le=1_000_000_000_000_000, allow_inf_nan=False)

    @model_validator(mode="after")
    def validate_order(self) -> Self:
        _validate_range_order(self.min, self.max)
        return self


class DiscoveryCriteria(_StrictModel):
    market: list[DiscoveryMarket] | None = Field(default=None, min_length=1, max_length=3)
    industry: list[IndustryText] | None = Field(default=None, min_length=1, max_length=20)
    is_st: bool | None = None
    is_new: bool | None = None
    quality: DiscoveryScoreRange | None = None
    trend: DiscoveryScoreRange | None = None
    change: DiscoveryChangeRange | None = None
    turnover: DiscoveryTurnoverRange | None = None
    amount: DiscoveryAmountRange | None = None
    score: DiscoveryScoreRange | None = None
    confidence: DiscoveryScoreRange | None = None
    risk: DiscoveryScoreRange | None = None
    tradability: DiscoveryScoreRange | None = None
    keyword: Annotated[str, Field(min_length=1, max_length=80)] | None = None

    @field_validator("market")
    @classmethod
    def validate_unique_markets(cls, value: list[DiscoveryMarket] | None) -> list[DiscoveryMarket] | None:
        return _unique_values(value, "market")

    @field_validator("industry")
    @classmethod
    def validate_industries(cls, value: list[str] | None) -> list[str] | None:
        checked = _unique_values(value, "industry")
        if checked is not None:
            for industry in checked:
                _reject_control_characters(industry, "industry")
        return checked

    @field_validator("keyword")
    @classmethod
    def validate_keyword(cls, value: str | None) -> str | None:
        if value is not None:
            _reject_control_characters(value, "keyword")
        return value


class DiscoverySort(_StrictModel):
    field: DiscoverySortField
    order: DiscoverySortOrder


def _default_sort() -> list[DiscoverySort]:
    return [DiscoverySort(field="rank", order="asc")]


class DiscoveryPresetDefinition(_StrictModel):
    name: NameText
    criteria: DiscoveryCriteria = Field(default_factory=DiscoveryCriteria)
    sort: list[DiscoverySort] = Field(default_factory=_default_sort, min_length=1, max_length=3)
    column_view: DiscoveryColumnView = "overview"

    @field_validator("name")
    @classmethod
    def validate_name(cls, value: str) -> str:
        _reject_control_characters(value, "name")
        return value

    @field_validator("sort")
    @classmethod
    def validate_unique_sort_fields(cls, value: list[DiscoverySort]) -> list[DiscoverySort]:
        fields = [item.field for item in value]
        if len(fields) != len(set(fields)):
            raise ValueError("排序字段不能重复")
        return value


class DiscoveryPresetCreate(DiscoveryPresetDefinition):
    pass


class DiscoveryPresetUpdate(DiscoveryPresetDefinition):
    expected_revision: int = Field(ge=1)


class DiscoveryPresetPortable(DiscoveryPresetDefinition):
    pass


class DiscoveryPreset(DiscoveryPresetDefinition):
    id: int = Field(ge=1)
    schema_version: int = Field(ge=1)
    revision: int = Field(ge=1)
    created_at: str
    updated_at: str


class DiscoveryPresetRename(_StrictModel):
    name: NameText
    expected_revision: int = Field(ge=1)

    @field_validator("name")
    @classmethod
    def validate_name(cls, value: str) -> str:
        _reject_control_characters(value, "name")
        return value


class DiscoveryPresetPage(BaseModel):
    items: list[DiscoveryPreset]
    total: int = Field(ge=0)
    page: int = Field(ge=1)
    page_size: int = Field(ge=1, le=100)
    page_count: int = Field(ge=0)

    @model_validator(mode="after")
    def validate_page_shape(self) -> Self:
        _validate_page_shape(self.items, self.total, self.page, self.page_size, self.page_count)
        _require_unique((item.id for item in self.items), "筛选方案 id")
        return self


class DiscoveryPresetArchive(_StrictModel):
    format: Literal["ashare-radar.discovery-preset"]
    schema_version: int = Field(ge=1, le=1000)
    checksum_algorithm: Literal["sha256"]
    checksum: str = Field(pattern=r"^[0-9a-f]{64}$")
    exported_at: str = Field(min_length=1, max_length=40)
    preset: DiscoveryPresetPortable


class DiscoveryPresetApplyRequest(_StrictModel):
    run_id: int = Field(ge=1)
    page: int = Field(default=1, ge=1)
    page_size: int = Field(default=50, ge=1, le=200)


class DiscoveryLeaderboardItem(BaseModel):
    position: int = Field(ge=1)
    source_rank: int = Field(ge=1)
    symbol: SymbolText
    code: str = Field(pattern=r"^\d{6}$")
    market: DiscoveryMarket
    name: str = Field(min_length=1)
    industry: str | None = None
    is_st: bool
    is_new: bool
    quality: int = Field(ge=0, le=100)
    trend: int = Field(ge=0, le=100)
    change: float = Field(ge=-1000, le=1000, allow_inf_nan=False)
    turnover: float | None = Field(default=None, ge=0, allow_inf_nan=False)
    amount: float = Field(ge=0, allow_inf_nan=False)
    score: int = Field(ge=0, le=100)
    raw_score: float = Field(ge=0, le=100, allow_inf_nan=False)

    @model_validator(mode="after")
    def validate_symbol_binding(self) -> Self:
        _validate_symbol_binding(self.symbol, self.code, self.market)
        _validate_optional_finite_numbers(self.change, self.turnover, self.amount)
        return self


class DiscoveryLeaderboardPage(BaseModel):
    preset: DiscoveryPreset
    run_id: int = Field(ge=1)
    rule_version: str
    items: list[DiscoveryLeaderboardItem]
    total: int = Field(ge=0)
    page: int = Field(ge=1)
    page_size: int = Field(ge=1, le=200)
    page_count: int = Field(ge=0)

    @model_validator(mode="after")
    def validate_page_shape(self) -> Self:
        _validate_page_shape(self.items, self.total, self.page, self.page_size, self.page_count)
        _require_unique((item.position for item in self.items), "筛选结果位置")
        _require_unique((item.symbol for item in self.items), "筛选结果股票")
        offset = (self.page - 1) * self.page_size
        if any(item.position != offset + index for index, item in enumerate(self.items, start=1)):
            raise ValueError("筛选结果 position 与当前分页不一致")
        return self


class DiscoveryResearchQueueRequest(_StrictModel):
    run_id: int = Field(ge=1)
    expected_preset_revision: int = Field(ge=1)
    symbols: list[SymbolText] = Field(min_length=1, max_length=100)

    @field_validator("symbols")
    @classmethod
    def validate_unique_symbols(cls, value: list[str]) -> list[str]:
        checked = _unique_values(value, "symbols")
        assert checked is not None
        return checked


class DiscoveryResearchQueueItem(BaseModel):
    symbol: SymbolText
    source_run_id: int = Field(ge=1)
    source_preset_id: int = Field(ge=1)
    source_preset_revision: int = Field(ge=1)
    source_preset_name: str = Field(min_length=1)
    enqueued_at: str = Field(min_length=1)
    added: bool


class DiscoveryResearchQueueResponse(BaseModel):
    items: list[DiscoveryResearchQueueItem]
    added_count: int = Field(ge=0)
    existing_count: int = Field(ge=0)

    @model_validator(mode="after")
    def validate_counts(self) -> Self:
        _require_unique((item.symbol for item in self.items), "研究队列股票")
        added_count = sum(item.added for item in self.items)
        if self.added_count != added_count or self.existing_count != len(self.items) - added_count:
            raise ValueError("研究队列计数与项目不一致")
        return self


class DiscoveryPresetDeleteResponse(BaseModel):
    deleted: Literal[True] = True
    preset_id: int = Field(ge=1)


class DiscoveryRunReference(BaseModel):
    id: int = Field(ge=1)
    status: str
    mode: DiscoveryRunMode
    rule_version: str
    scope: str
    data_date: str
    as_of: str
    snapshot_digest: str | None = Field(default=None, pattern=r"^[0-9a-f]{64}$")
    snapshot_seal_origin: Literal["publication", "legacy_backfill"] | None = None
    snapshot_sealed_at: str | None = None


class DiscoveryRankChangeItem(BaseModel):
    symbol: SymbolText
    code: str = Field(pattern=r"^\d{6}$")
    market: DiscoveryMarket
    name: str = Field(min_length=1)
    previous_rank: int | None = Field(default=None, ge=1)
    current_rank: int | None = Field(default=None, ge=1)
    rank_delta: int | None = None
    movement: DiscoveryRankMovement

    @model_validator(mode="after")
    def validate_rank_state(self) -> Self:
        _validate_symbol_binding(self.symbol, self.code, self.market)
        _validate_rank_change_state(self)
        return self


class DiscoveryRankChangePage(BaseModel):
    current_run_id: int = Field(ge=1)
    previous_run_id: int | None = Field(default=None, ge=1)
    current_rule_version: str
    previous_rule_version: str | None = None
    comparable: bool
    reason: DiscoveryComparisonReason | None = None
    items: list[DiscoveryRankChangeItem]
    total: int = Field(ge=0)
    page: int = Field(ge=1)
    page_size: int = Field(ge=1, le=200)
    page_count: int = Field(ge=0)

    @model_validator(mode="after")
    def validate_page_and_comparison(self) -> Self:
        _validate_page_shape(self.items, self.total, self.page, self.page_size, self.page_count)
        _require_unique((item.symbol for item in self.items), "排名变化股票")
        _validate_comparison_state(self)
        return self


def _validate_rank_change_state(item: DiscoveryRankChangeItem) -> None:
    if item.movement in {"up", "down", "unchanged"}:
        _validate_comparable_rank_change(item)
    elif item.rank_delta is not None:
        raise ValueError("不可比较排名变化不能包含 rank_delta")
    if item.movement == "new" and (item.previous_rank is not None or item.current_rank is None):
        raise ValueError("新进状态与排名不一致")
    if item.movement == "exit" and (item.previous_rank is None or item.current_rank is not None):
        raise ValueError("离榜状态与排名不一致")


def _validate_comparable_rank_change(item: DiscoveryRankChangeItem) -> None:
    if item.previous_rank is None or item.current_rank is None:
        raise ValueError("可比较排名变化必须包含前后排名")
    expected_delta = item.previous_rank - item.current_rank
    if item.rank_delta != expected_delta:
        raise ValueError("rank_delta 与前后排名不一致")
    invalid_direction = (
        (item.movement == "up" and expected_delta <= 0)
        or (item.movement == "down" and expected_delta >= 0)
        or (item.movement == "unchanged" and expected_delta != 0)
    )
    if invalid_direction:
        raise ValueError("movement 与 rank_delta 不一致")


def _validate_comparison_state(page: DiscoveryRankChangePage) -> None:
    if page.comparable:
        if page.previous_run_id is None or page.reason is not None or page.previous_rule_version != page.current_rule_version:
            raise ValueError("可比较状态与批次/规则不一致")
        return
    if page.reason is None:
        raise ValueError("不可比较状态必须包含原因")
    if page.items or page.total or page.page_count:
        raise ValueError("不可比较状态不能包含排名变化项目")
    _validate_unavailable_comparison_reason(page)


def _validate_unavailable_comparison_reason(page: DiscoveryRankChangePage) -> None:
    if page.reason == "no_previous_run" and (
        page.previous_run_id is not None or page.previous_rule_version is not None
    ):
        raise ValueError("无上一批次状态不能声明上一批次")
    if page.reason == "rule_version_mismatch" and (
        page.previous_run_id is None
        or page.previous_rule_version is None
        or page.previous_rule_version == page.current_rule_version
    ):
        raise ValueError("规则不一致状态缺少不同规则的上一批次")


def _validate_range_order(minimum: int | float | None, maximum: int | float | None) -> None:
    if minimum is not None and maximum is not None and minimum > maximum:
        raise ValueError("范围下限不能大于上限")


def _unique_values(value: list[T] | None, field: str) -> list[T] | None:
    if value is not None and len(value) != len(set(value)):
        raise ValueError(f"{field} 不能包含重复值")
    return value


def _reject_control_characters(value: str, field: str) -> None:
    if any(ord(character) < 32 or ord(character) == 127 for character in value):
        raise ValueError(f"{field} 不能包含控制字符")


def _validate_page_shape(
    items: Sequence[object],
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


def _require_unique(values: Iterable[object], label: str) -> None:
    resolved = tuple(values)
    if len(resolved) != len(set(resolved)):
        raise ValueError(f"{label}不能重复")


def _validate_symbol_binding(symbol: str, code: str, market: str) -> None:
    if symbol != f"{code}.{market}":
        raise ValueError("symbol/code/market 不一致")


def _validate_optional_finite_numbers(*values: float | None) -> None:
    if any(value is not None and not isfinite(value) for value in values):
        raise ValueError("数值字段必须是有限数值")


__all__ = [
    "DISCOVERY_PRESET_FORMAT",
    "DISCOVERY_PRESET_SCHEMA_VERSION",
    "DiscoveryColumnView",
    "DiscoveryComparisonReason",
    "DiscoveryCriteria",
    "DiscoveryLeaderboardItem",
    "DiscoveryLeaderboardPage",
    "DiscoveryPreset",
    "DiscoveryPresetApplyRequest",
    "DiscoveryPresetArchive",
    "DiscoveryPresetCreate",
    "DiscoveryPresetDeleteResponse",
    "DiscoveryPresetPage",
    "DiscoveryPresetPortable",
    "DiscoveryPresetRename",
    "DiscoveryPresetUpdate",
    "DiscoveryRankChangeItem",
    "DiscoveryRankChangePage",
    "DiscoveryResearchQueueItem",
    "DiscoveryResearchQueueRequest",
    "DiscoveryResearchQueueResponse",
    "DiscoveryRunReference",
    "DiscoverySort",
    "DiscoverySortField",
    "DiscoverySortOrder",
]
