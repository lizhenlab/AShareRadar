"""Typed contract for idempotent saved-screen membership change events."""

from __future__ import annotations

from typing import Final, Literal, Self

from pydantic import BaseModel, ConfigDict, Field, model_validator

from app.models.market_scan import MarketScanMode, MarketScanRunStatus


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


__all__ = [
    "MARKET_SCAN_SCREEN_ALERT_SCHEMA_VERSION",
    "MarketScanScreenAlertPresetRef",
    "MarketScanScreenAlertRequest",
    "MarketScanScreenAlertResponse",
    "MarketScanScreenAlertRunRef",
    "MarketScanScreenAlertStatus",
    "MarketScanScreenAlertUnavailableReason",
]
