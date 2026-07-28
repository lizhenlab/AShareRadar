"""Typed contracts for configurable local discovery workflows."""

from __future__ import annotations

from typing import Annotated, Literal, Self, TypeVar

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator


DISCOVERY_PRESET_FORMAT = "ashare-radar.discovery-preset"
DISCOVERY_PRESET_SCHEMA_VERSION = 1

DiscoveryMarket = Literal["SH", "SZ", "BJ"]
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


class DiscoverySort(_StrictModel):
    field: DiscoverySortField
    order: DiscoverySortOrder


def _default_sort() -> list[DiscoverySort]:
    return [DiscoverySort(field="rank", order="asc")]


class DiscoveryPresetDefinition(_StrictModel):
    name: NameText
    criteria: DiscoveryCriteria = Field(default_factory=DiscoveryCriteria)
    sort: list[DiscoverySort] = Field(default_factory=_default_sort, min_length=1, max_length=3)

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
    source_rank: int | None = Field(default=None, ge=1)
    symbol: str
    code: str
    market: DiscoveryMarket
    name: str
    industry: str | None = None
    is_st: bool
    is_new: bool
    quality: int | None = Field(default=None, ge=0, le=100)
    trend: int | None = Field(default=None, ge=0, le=100)
    change: float | None = None
    turnover: float | None = None
    amount: float | None = None
    score: int | None = Field(default=None, ge=0, le=100)
    raw_score: float | None = Field(default=None, ge=0, le=100, allow_inf_nan=False)


class DiscoveryLeaderboardPage(BaseModel):
    preset: DiscoveryPreset
    run_id: int = Field(ge=1)
    rule_version: str
    items: list[DiscoveryLeaderboardItem]
    total: int = Field(ge=0)
    page: int = Field(ge=1)
    page_size: int = Field(ge=1, le=200)
    page_count: int = Field(ge=0)


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
    symbol: str
    source_run_id: int = Field(ge=1)
    source_preset_id: int = Field(ge=1)
    source_preset_revision: int = Field(ge=1)
    source_preset_name: str
    enqueued_at: str
    added: bool


class DiscoveryResearchQueueResponse(BaseModel):
    items: list[DiscoveryResearchQueueItem]
    added_count: int = Field(ge=0)
    existing_count: int = Field(ge=0)


class DiscoveryPresetDeleteResponse(BaseModel):
    deleted: Literal[True] = True
    preset_id: int = Field(ge=1)


class DiscoveryRunReference(BaseModel):
    id: int = Field(ge=1)
    status: str
    rule_version: str
    scope: str
    data_date: str
    as_of: str


class DiscoveryRankChangeItem(BaseModel):
    symbol: str
    code: str
    market: DiscoveryMarket
    name: str
    previous_rank: int | None = Field(default=None, ge=1)
    current_rank: int | None = Field(default=None, ge=1)
    rank_delta: int | None = None
    movement: DiscoveryRankMovement


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


__all__ = [
    "DISCOVERY_PRESET_FORMAT",
    "DISCOVERY_PRESET_SCHEMA_VERSION",
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
