"""Typed contract for idempotent saved-screen membership change events."""

from __future__ import annotations

from typing import Annotated, Final, Literal, Self

from pydantic import BaseModel, ConfigDict, Field, StringConstraints, field_validator, model_validator

from app.models.market_scan import MarketScanMode, MarketScanRunStatus
from app.utils.audit_time import parse_audit_time
from app.utils.symbols import standard_symbol


MARKET_SCAN_SCREEN_ALERT_SCHEMA_VERSION: Final[
    Literal["market-scan-screen-alert-v1"]
] = "market-scan-screen-alert-v1"

MarketScanScreenAlertStatus = Literal["ready", "unavailable"]
MarketScanScreenAlertUnavailableReason = Literal[
    "current_not_published",
    "current_not_full_market",
    "previous_same_cohort_not_found",
]


class _FrozenAlertModel(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True, str_strip_whitespace=True)


class MarketScanScreenAlertPresetRef(_FrozenAlertModel):
    preset_id: int = Field(ge=1)
    preset_revision: int = Field(ge=1)
    preset_name: str = Field(min_length=1, max_length=80)
    spec_digest: str = Field(pattern=r"^[0-9a-f]{64}$")


class MarketScanScreenAlertRunRef(_FrozenAlertModel):
    run_id: int = Field(ge=1)
    status: MarketScanRunStatus
    mode: MarketScanMode
    scope: str = Field(min_length=1)
    rule_version: str = Field(min_length=1)
    data_date: str = Field(min_length=1)
    finished_at: str | None = None


class MarketScanScreenAlertRequest(_FrozenAlertModel):
    current_run_id: int = Field(ge=1)
    expected_preset_revision: int | None = Field(default=None, ge=1)


class MarketScanScreenAlertResponse(_FrozenAlertModel):
    schema_version: Literal["market-scan-screen-alert-v1"] = (
        MARKET_SCAN_SCREEN_ALERT_SCHEMA_VERSION
    )
    status: MarketScanScreenAlertStatus
    unavailable_reason: MarketScanScreenAlertUnavailableReason | None = None
    preset: MarketScanScreenAlertPresetRef
    current: MarketScanScreenAlertRunRef
    previous: MarketScanScreenAlertRunRef | None = None
    entered_symbols: tuple[str, ...] = ()
    exited_symbols: tuple[str, ...] = ()
    suppressed_unrankable_symbols: tuple[str, ...] = ()
    event_digest: str = Field(pattern=r"^[0-9a-f]{64}$")
    created: bool

    @model_validator(mode="after")
    def validate_state(self) -> Self:
        symbol_sets = [
            set(self.entered_symbols),
            set(self.exited_symbols),
            set(self.suppressed_unrankable_symbols),
        ]
        if any(len(values) != len(source) for values, source in zip(
            symbol_sets,
            (self.entered_symbols, self.exited_symbols, self.suppressed_unrankable_symbols),
            strict=True,
        )):
            raise ValueError("筛选变化股票不能重复")
        if any(left & right for index, left in enumerate(symbol_sets) for right in symbol_sets[index + 1 :]):
            raise ValueError("筛选变化股票集合不能重叠")
        if self.status == "ready":
            if self.previous is None or self.unavailable_reason is not None:
                raise ValueError("可用筛选变化必须包含前批次且不能包含不可用原因")
        elif self.previous is not None or self.unavailable_reason is None or self.created:
            raise ValueError("不可用筛选变化不能写事件，且必须解释原因")
        return self


MarketScanScreenAlertChangeKind = Literal["entered", "exited", "unrankable"]
MarketScanScreenAlertHistoryKind = Literal["all", "entered", "exited", "unrankable"]
ScreenAlertSymbol = Annotated[str, StringConstraints(strict=True, strip_whitespace=False, pattern=r"^[0-9]{6}\.(SH|SZ|BJ)$")]


class MarketScanScreenAlertEventSummary(_FrozenAlertModel):
    """Stored event identity; historical name/spec were not persisted."""

    model_config = ConfigDict(extra="forbid", frozen=True, strict=True, str_strip_whitespace=False)
    id: int = Field(ge=1)
    preset_id: int = Field(ge=1)
    preset_revision: int = Field(ge=1)
    current_run_id: int = Field(ge=1)
    previous_run_id: int = Field(ge=1)
    event_digest: str = Field(pattern=r"^[0-9a-f]{64}$")
    created_at: str
    entered_count: int = Field(ge=0)
    exited_count: int = Field(ge=0)
    suppressed_unrankable_count: int = Field(ge=0)

    @model_validator(mode="after")
    def validate_identity(self) -> Self:
        if self.current_run_id == self.previous_run_id:
            raise ValueError("筛选变化前后批次不能相同")
        if len(self.created_at) < 19 or self.created_at[10] not in {"T", " "}:
            raise ValueError("筛选变化事件时间无效")
        parse_audit_time(self.created_at)
        return self


class MarketScanScreenAlertHistoryItem(_FrozenAlertModel):
    symbol: ScreenAlertSymbol
    change: MarketScanScreenAlertChangeKind

    @field_validator("symbol")
    @classmethod
    def validate_symbol(cls, value: str) -> str:
        if standard_symbol(value) != value:
            raise ValueError("筛选变化股票代码不是规范格式")
        return value


class MarketScanScreenAlertHistoryPage(_FrozenAlertModel):
    preset_id: int = Field(ge=1)
    items: list[MarketScanScreenAlertEventSummary]
    total: int = Field(ge=0)
    page: int = Field(ge=1)
    page_size: int = Field(ge=1, le=100)
    page_count: int = Field(ge=0)

    @model_validator(mode="after")
    def validate_page(self) -> Self:
        _require_history_page(len(self.items), self.total, self.page, self.page_size, self.page_count)
        identifiers = [item.id for item in self.items]
        if identifiers != sorted(set(identifiers), reverse=True) or any(item.preset_id != self.preset_id for item in self.items):
            raise ValueError("筛选变化历史分页身份或排序不一致")
        return self


class MarketScanScreenAlertDetailPage(_FrozenAlertModel):
    event: MarketScanScreenAlertEventSummary
    items: list[MarketScanScreenAlertHistoryItem]
    total: int = Field(ge=0)
    page: int = Field(ge=1)
    page_size: int = Field(ge=1, le=100)
    page_count: int = Field(ge=0)
    kind: MarketScanScreenAlertHistoryKind

    @model_validator(mode="after")
    def validate_page(self) -> Self:
        counts = {"entered": self.event.entered_count, "exited": self.event.exited_count, "unrankable": self.event.suppressed_unrankable_count}
        expected = sum(counts.values()) if self.kind == "all" else counts[self.kind]
        _require_history_page(len(self.items), expected, self.page, self.page_size, self.page_count)
        if self.total != expected or any(self.kind != "all" and item.change != self.kind for item in self.items):
            raise ValueError("筛选变化明细类型或数量不一致")
        if any(sum(item.change == kind for item in self.items) > count for kind, count in counts.items()):
            raise ValueError("筛选变化明细成员数量超过事件记录")
        keys = [(tuple(counts).index(item.change), item.symbol) for item in self.items]
        if len({item.symbol for item in self.items}) != len(self.items) or keys != sorted(keys):
            raise ValueError("筛选变化明细重复或排序不一致")
        return self


def _require_history_page(length: int, total: int, page: int, page_size: int, page_count: int) -> None:
    expected_length = min(page_size, max(0, total - (page - 1) * page_size))
    if length != expected_length or page_count != (total + page_size - 1) // page_size:
        raise ValueError("筛选变化历史分页数量不一致")


__all__ = [
    "MARKET_SCAN_SCREEN_ALERT_SCHEMA_VERSION",
    "MarketScanScreenAlertPresetRef",
    "MarketScanScreenAlertRequest",
    "MarketScanScreenAlertResponse",
    "MarketScanScreenAlertRunRef",
    "MarketScanScreenAlertStatus",
    "MarketScanScreenAlertUnavailableReason",
    "MarketScanScreenAlertChangeKind",
    "MarketScanScreenAlertHistoryKind",
    "MarketScanScreenAlertEventSummary",
    "MarketScanScreenAlertHistoryItem",
    "MarketScanScreenAlertHistoryPage",
    "MarketScanScreenAlertDetailPage",
]
