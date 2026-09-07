"""Strict source snapshots and an independently versioned allocation policy."""

from __future__ import annotations

from collections.abc import Mapping
from datetime import date, datetime
from decimal import Decimal
from typing import Annotated, Literal, Self

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

from app.artifacts.io import ArtifactIOError, canonical_json_bytes, sha256_hex
from app.utils.clock import ASHARE_TIMEZONE


MAX_ALLOCATION_BYTES = 64 * 1024 * 1024
MAX_ACCOUNT_MONEY = 1_000_000_000_000
Symbol = Annotated[str, Field(pattern=r"^[0-9]{6}\.(SH|SZ|BJ)$")]
Label = Annotated[str, Field(min_length=1, max_length=256, pattern=r"^\S(?:.*\S)?$")]
Money = Annotated[float, Field(ge=0, le=MAX_ACCOUNT_MONEY)]


def allocation_json_bytes(value: object) -> bytes:
    try:
        encoded = canonical_json_bytes(value)
    except ArtifactIOError as error:
        raise ValueError("allocation requires finite canonical JSON") from error
    if len(encoded) > MAX_ALLOCATION_BYTES:
        raise ValueError("allocation input exceeds the bounded byte limit")
    return encoded


def allocation_digest(value: object) -> str:
    return sha256_hex(allocation_json_bytes(value))


def allocation_source_digests(payload: object) -> dict[str, str]:
    """Compute identities to retain independently; hashing is not certification."""
    if not isinstance(payload, Mapping) or not {"account", "candidates", "market"} <= payload.keys():
        raise ValueError("allocation requires account, candidates and market sources")
    return {name: allocation_digest(payload[name]) for name in ("account", "candidates", "market")}


def allocation_time(value: str) -> datetime:
    if "T" not in value:
        raise ValueError("allocation timestamps require ISO T separator")
    stamp = datetime.fromisoformat(value)
    if stamp.tzinfo is None or stamp.utcoffset() is None:
        raise ValueError("allocation timestamps require an explicit timezone")
    try:
        return stamp.astimezone(ASHARE_TIMEZONE)
    except (OverflowError, ValueError) as error:
        raise ValueError("allocation timestamp is outside the supported timezone range") from error


def allocation_date(value: str) -> date:
    parsed = date.fromisoformat(value)
    if parsed.isoformat() != value:
        raise ValueError("allocation dates require canonical ISO format")
    return parsed


def allocation_cents(value: float) -> int:
    amount = Decimal(str(value)) * 100
    if not amount.is_finite() or amount != amount.to_integral_value() or not 0 <= amount <= MAX_ACCOUNT_MONEY * 100:
        raise ValueError("allocation money must be bounded nonnegative exact cents")
    return int(amount)


class AllocationModel(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True, frozen=True, allow_inf_nan=False)


class AllocationHolding(AllocationModel):
    position_id: Label
    symbol: Symbol
    sleeve: int = Field(ge=0, le=10000)
    quantity: int = Field(gt=0, le=1_000_000_000_000)


class AllocationAccount(AllocationModel):
    source_id: Label
    observed_as_of: str
    holdings_complete: Literal[True]
    cash: Money
    positions: list[AllocationHolding] = Field(max_length=10000)

    @field_validator("holdings_complete", mode="before")
    @classmethod
    def exact_complete_flag(cls, value: object) -> object:
        if value is not True:
            raise ValueError("complete current holdings must be explicitly declared")
        return value

    @model_validator(mode="after")
    def valid_account(self) -> Self:
        allocation_cents(self.cash)
        if len({row.position_id for row in self.positions}) != len(self.positions):
            raise ValueError("duplicate current position identity")
        return self


class AllocationCandidate(AllocationModel):
    symbol: Symbol
    frozen_rank: int = Field(gt=0, le=1000)


class AllocationCandidates(AllocationModel):
    source_id: Label
    observed_as_of: str
    rows: list[AllocationCandidate] = Field(min_length=1, max_length=1000)

    @model_validator(mode="after")
    def unique_candidates(self) -> Self:
        if len({row.symbol for row in self.rows}) != len(self.rows):
            raise ValueError("duplicate candidate symbol")
        if sorted(row.frozen_rank for row in self.rows) != list(range(1, len(self.rows) + 1)):
            raise ValueError("candidates require every unique frozen rank from one through N")
        return self


class AllocationInstrument(AllocationModel):
    symbol: Symbol
    price: float | None = Field(ge=.01, le=1_000_000)
    valuation_observed_at: str | None
    industry: Label | None
    classification_observed_at: str | None
    classification_effective_from: str | None
    classification_effective_through: str | None
    minimum_buy_quantity: int = Field(gt=0, le=1_000_000)
    buy_quantity_step: int = Field(gt=0, le=1_000_000)
    capacity_amount: Money | None
    capacity_session_date: str | None
    capacity_observed_at: str | None

    @model_validator(mode="after")
    def valid_lot_rules(self) -> Self:
        if self.minimum_buy_quantity % self.buy_quantity_step:
            raise ValueError("minimum buy quantity must align to the supported lot step")
        return self


class AllocationMarket(AllocationModel):
    source_id: Label
    observed_as_of: str
    rows: list[AllocationInstrument] = Field(max_length=20000)

    @model_validator(mode="after")
    def unique_instruments(self) -> Self:
        if len({row.symbol for row in self.rows}) != len(self.rows):
            raise ValueError("duplicate or conflicting valuation/classification symbol")
        return self


class AllocationInput(AllocationModel):
    schema_version: Literal["market-scan-allocation-input-v1"]
    decision_at: str
    account: AllocationAccount
    candidates: AllocationCandidates
    market: AllocationMarket


class AllocationPolicy(AllocationModel):
    schema_version: Literal["market-scan-allocation-policy-v1"]
    allocation: Literal["fixed-candidate-slots"]
    top_n: int = Field(gt=0, le=1000)
    max_symbol_weight: float = Field(gt=0, le=1)
    max_industry_weight: float = Field(gt=0, le=1)
    max_participation_rate: float = Field(gt=0, le=1)
    cost_profile: Literal["base", "conservative", "stress"]
    cost_profile_id: Label
    max_input_age_seconds: int = Field(ge=0, le=7 * 86400)
    max_quote_age_seconds: int = Field(ge=0, le=7 * 86400)
    max_classification_age_days: int = Field(ge=0, le=3650)
    existing_breach_policy: Literal["block-new-orders"]
