"""Declared selection and compact, non-performance availability evidence."""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from datetime import date, datetime
from typing import Literal, cast

from app.models.market_scan import MarketScanMode
from app.services.trading_calendar import trading_dates_between
from app.utils.clock import ASHARE_TIMEZONE


AVAILABILITY_PLAN_VERSION = "market-scan-availability-plan-v1"
AVAILABILITY_REPORT_VERSION = "market-scan-availability-audit-v1"
AVAILABILITY_STATUSES = ("success", "missing", "pending", "skipped")
AvailabilitySelection = Literal["latest-published-by-as-of-and-id", "explicit-run-ids"]


@dataclass(frozen=True)
class AvailabilityPlan:
    trading_dates: tuple[str, ...]
    selection_policy: AvailabilitySelection
    selection_cutoff: str
    mode: MarketScanMode = "official"
    run_ids: tuple[int, ...] = ()

    def __post_init__(self) -> None:
        validate_availability_calendar(self.trading_dates)
        cutoff = availability_timestamp(self.selection_cutoff, require_timezone=True)
        if cutoff.date().isoformat() < self.trading_dates[-1]:
            raise ValueError("selection_cutoff must cover the declared calendar")
        if not isinstance(self.mode, str) or self.mode not in {"official", "preopen", "intraday"}:
            raise ValueError("unsupported availability mode")
        if not isinstance(self.selection_policy, str) or self.selection_policy not in {"latest-published-by-as-of-and-id", "explicit-run-ids"}:
            raise ValueError("unsupported availability selection policy")
        validate_availability_run_ids(self.run_ids, self.selection_policy)


def validate_availability_calendar(values: tuple[str, ...]) -> None:
    if not isinstance(values, tuple) or not values or len(values) > 1024:
        raise ValueError("declare 1 to 1024 trading dates per bounded audit")
    if any(not isinstance(value, str) or date.fromisoformat(value).isoformat() != value for value in values):
        raise ValueError("trading_dates must use canonical ISO dates")
    expected = tuple(day.isoformat() for day in trading_dates_between(date.fromisoformat(values[0]), date.fromisoformat(values[-1])))
    if values != expected:
        raise ValueError("trading_dates must match the complete ordered trusted trading calendar")


def validate_availability_run_ids(values: tuple[int, ...], policy: str) -> None:
    if not isinstance(values, tuple) or any(type(value) is not int or value <= 0 for value in values):
        raise ValueError("run_ids must be positive integers")
    if len(set(values)) != len(values) or len(values) > 1024:
        raise ValueError("run_ids must be unique and bounded")
    if bool(values) != (policy == "explicit-run-ids"):
        raise ValueError("explicit-run-ids requires ids; automatic selection forbids ids")


def availability_timestamp(value: str, *, require_timezone: bool = False) -> datetime:
    if not isinstance(value, str) or "T" not in value and " " not in value:
        raise ValueError("availability timestamps require a full ISO datetime")
    parsed = datetime.fromisoformat(value)
    if parsed.tzinfo is None:
        if require_timezone:
            raise ValueError("selection_cutoff requires an explicit timezone")
        parsed = parsed.replace(tzinfo=ASHARE_TIMEZONE)
    return parsed.astimezone(ASHARE_TIMEZONE)


def availability_plan_from_payload(value: object) -> AvailabilityPlan:
    if not isinstance(value, Mapping) or set(value) != {"schema_version", "trading_dates", "selection_policy", "selection_cutoff", "mode", "run_ids"}:
        raise ValueError("availability plan requires the exact declared schema")
    if value["schema_version"] != AVAILABILITY_PLAN_VERSION:
        raise ValueError("unsupported availability plan version")
    dates, ids = value["trading_dates"], value["run_ids"]
    if not isinstance(dates, list) or not isinstance(ids, list):
        raise ValueError("trading_dates and run_ids must be arrays")
    return AvailabilityPlan(
        tuple(dates), cast(AvailabilitySelection, value["selection_policy"]),
        cast(str, value["selection_cutoff"]), cast(MarketScanMode, value["mode"]), tuple(ids),
    )


@dataclass(frozen=True)
class AvailabilityMember:
    symbol: str
    status: str
    market: str
    industry: str
    metadata_source: str
    quote_source: str
    kline_source: str
    error_category: str
    predecision_amount: float | None = None


@dataclass(frozen=True)
class AvailabilityBatch:
    data_date: str
    run_id: int
    snapshot_digest: str
    seal_origin: str | None
    members: tuple[AvailabilityMember, ...]
