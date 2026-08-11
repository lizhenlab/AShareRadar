"""Point-in-time, executable forward labels for full-market probability research."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date
import math
from typing import Literal, Mapping, Sequence

from app.models.market import Kline
from app.models.paper_trading import CostProfileName, PaperCostProfile, PaperInstrumentMetadata, PaperTradeRuleProfile
from app.services.paper_trading_costs import resolve_cost_profile, trade_costs
from app.services.paper_trading_rules import assess_daily_tradeability, resolve_trade_rule_profile


PROBABILITY_LABEL_VERSION = "market-scan-upside-label-v2"
PROBABILITY_EXECUTION_MODEL = "next-session-open,H-holding-session-close,T+1,no-delayed-exit"
PROBABILITY_DEFAULT_HORIZONS = (1, 5, 20)
ProbabilityLabelStatus = Literal["modelled", "unfilled", "data_unavailable"]


@dataclass(frozen=True)
class ProbabilityLabelConfig:
    horizons: tuple[int, ...] = PROBABILITY_DEFAULT_HORIZONS
    cost_profile: CostProfileName = "base"
    execution_notional: float = 100_000.0
    max_daily_participation_rate: float = 0.01

    def __post_init__(self) -> None:
        if not self.horizons or any(value <= 0 for value in self.horizons):
            raise ValueError("probability label horizons must be positive")
        if self.execution_notional <= 0:
            raise ValueError("probability label execution_notional must be positive")
        if not 0 < self.max_daily_participation_rate <= 1:
            raise ValueError("probability label participation rate must be in (0, 1]")


@dataclass(frozen=True)
class ProbabilityLabelOutcome:
    horizon: int
    status: ProbabilityLabelStatus
    reason: str
    label: int | None = None
    gross_return: float | None = None
    net_return: float | None = None
    cost_drag: float | None = None
    entry_date: str | None = None
    exit_date: str | None = None
    entry_price: float | None = None
    exit_price: float | None = None
    model_limited: bool = False
    rule_profile_verified: bool = True
    daily_bar_model_limited: bool = False


@dataclass(frozen=True)
class _ProbabilityEntry:
    bars: Mapping[str, Kline]
    metadata: PaperInstrumentMetadata
    entry_date: str
    entry_price: float
    quantity: int
    buy_amount: float
    buy_cost: float
    cost_profile: PaperCostProfile
    model_limited: bool
    rule_profile_verified: bool
    daily_bar_model_limited: bool


def build_probability_label_outcomes(
    *,
    symbol: str,
    market: str,
    list_date: str | None,
    is_st: bool,
    quote_date: str,
    amount: float,
    rows: Sequence[Kline],
    eligible_dates: Sequence[str],
    config: ProbabilityLabelConfig | None = None,
) -> dict[int, ProbabilityLabelOutcome]:
    settings = config or ProbabilityLabelConfig()
    bars = _validated_bars(rows)
    dates = tuple(dict.fromkeys(value for value in eligible_dates if value > quote_date))
    entry = _prepare_entry(symbol, market, list_date, is_st, quote_date, amount, bars, dates, settings)
    if isinstance(entry, ProbabilityLabelOutcome):
        return {horizon: _outcome_for_horizon(entry, horizon) for horizon in settings.horizons}
    return {
        horizon: _horizon_outcome(entry, symbol, dates, horizon)
        for horizon in settings.horizons
    }


def probability_label_contract(config: ProbabilityLabelConfig | None = None) -> dict[str, object]:
    settings = config or ProbabilityLabelConfig()
    cost = resolve_cost_profile(settings.cost_profile)
    return {
        "label_version": PROBABILITY_LABEL_VERSION,
        "execution_model": PROBABILITY_EXECUTION_MODEL,
        "horizons": list(settings.horizons),
        "target_definitions": ["absolute_net_return_positive", "equal_weight_market_net_excess_positive"],
        "cost_model_version": cost.version,
        "cost_profile_id": cost.profile_id,
        "execution_notional": settings.execution_notional,
        "max_daily_participation_rate": settings.max_daily_participation_rate,
    }


def _validated_bars(rows: Sequence[Kline]) -> dict[str, Kline]:
    bars: dict[str, Kline] = {}
    for row in sorted(rows, key=lambda item: item.date):
        values = (row.open, row.close, row.high, row.low, row.volume)
        if any(not math.isfinite(float(value)) for value in values) or row.open <= 0 or row.close <= 0:
            continue
        existing = bars.get(row.date)
        signature = (row.open, row.close, row.high, row.low, row.volume, row.adjustment_mode)
        if existing is not None and signature != (
            existing.open, existing.close, existing.high, existing.low, existing.volume, existing.adjustment_mode,
        ):
            raise ValueError(f"conflicting probability label bar: {row.date}")
        bars[row.date] = row
    return bars


def _prepare_entry(
    symbol: str,
    market: str,
    list_date: str | None,
    is_st: bool,
    quote_date: str,
    amount: float,
    bars: Mapping[str, Kline],
    dates: Sequence[str],
    config: ProbabilityLabelConfig,
) -> _ProbabilityEntry | ProbabilityLabelOutcome:
    if not dates:
        return ProbabilityLabelOutcome(0, "data_unavailable", "entry_date_missing")
    entry_date = dates[0]
    entry_bar, previous = bars.get(entry_date), _previous_bar(bars, entry_date)
    if entry_bar is None or previous is None:
        return ProbabilityLabelOutcome(0, "data_unavailable", "entry_or_previous_bar_missing", entry_date=entry_date)
    if amount <= 0 or config.execution_notional / amount > config.max_daily_participation_rate:
        return ProbabilityLabelOutcome(0, "unfilled", "daily_capacity_limit", entry_date=entry_date)
    metadata = _metadata(symbol, market, list_date, is_st, quote_date)
    profile = _safe_profile(symbol, entry_date, metadata)
    if profile is None:
        return ProbabilityLabelOutcome(
            0,
            "data_unavailable",
            "entry_rule_unavailable",
            entry_date=entry_date,
            rule_profile_verified=False,
        )
    if profile.quality != "ok":
        return ProbabilityLabelOutcome(
            0,
            "data_unavailable",
            "entry_rule_profile_degraded",
            entry_date=entry_date,
            rule_profile_verified=False,
        )
    tradeability = assess_daily_tradeability(entry_bar, previous_close=previous.close, profile=profile)
    if not tradeability.can_buy:
        return ProbabilityLabelOutcome(
            0,
            "unfilled",
            tradeability.code,
            entry_date=entry_date,
            model_limited=tradeability.model_limited,
            rule_profile_verified=True,
            daily_bar_model_limited=tradeability.model_limited,
        )
    return _entry_from_tradeable_bar(entry_bar, bars, metadata, profile, tradeability.model_limited, config)


def _entry_from_tradeable_bar(
    row: Kline,
    bars: Mapping[str, Kline],
    metadata: PaperInstrumentMetadata,
    profile: PaperTradeRuleProfile,
    tradeability_limited: bool,
    config: ProbabilityLabelConfig,
) -> _ProbabilityEntry | ProbabilityLabelOutcome:
    quantity = _model_quantity(config.execution_notional, row.open, profile.min_buy_quantity, profile.buy_quantity_step)
    if quantity <= 0:
        return ProbabilityLabelOutcome(0, "unfilled", "minimum_quantity_unaffordable", entry_date=row.date)
    cost_profile = resolve_cost_profile(config.cost_profile)
    buy_amount = row.open * quantity
    buy_cost = trade_costs(cost_profile, side="buy", gross_amount=buy_amount).total
    return _ProbabilityEntry(
        bars=bars, metadata=metadata, entry_date=row.date, entry_price=row.open, quantity=quantity,
        buy_amount=buy_amount, buy_cost=buy_cost, cost_profile=cost_profile,
        model_limited=tradeability_limited,
        rule_profile_verified=True,
        daily_bar_model_limited=tradeability_limited,
    )


def _horizon_outcome(
    entry: _ProbabilityEntry,
    symbol: str,
    dates: Sequence[str],
    horizon: int,
) -> ProbabilityLabelOutcome:
    bars = entry.bars
    if not bars:
        raise ValueError("probability entry bars were not bound")
    if horizon >= len(dates):
        return ProbabilityLabelOutcome(horizon, "data_unavailable", "target_date_missing", entry_date=entry.entry_date)
    exit_date = dates[horizon]
    exit_bar, previous = bars.get(exit_date), _previous_bar(bars, exit_date)
    if exit_bar is None or previous is None:
        return ProbabilityLabelOutcome(horizon, "data_unavailable", "exit_or_previous_bar_missing", entry_date=entry.entry_date, exit_date=exit_date)
    profile = _safe_profile(symbol, exit_date, entry.metadata)
    if profile is None:
        return ProbabilityLabelOutcome(
            horizon,
            "data_unavailable",
            "exit_rule_unavailable",
            entry_date=entry.entry_date,
            exit_date=exit_date,
            rule_profile_verified=False,
        )
    if profile.quality != "ok":
        return ProbabilityLabelOutcome(
            horizon,
            "data_unavailable",
            "exit_rule_profile_degraded",
            entry_date=entry.entry_date,
            exit_date=exit_date,
            rule_profile_verified=False,
        )
    tradeability = assess_daily_tradeability(exit_bar, previous_close=previous.close, profile=profile)
    if not tradeability.can_sell:
        return ProbabilityLabelOutcome(
            horizon,
            "unfilled",
            tradeability.code,
            entry_date=entry.entry_date,
            exit_date=exit_date,
            model_limited=entry.model_limited or tradeability.model_limited,
            rule_profile_verified=True,
            daily_bar_model_limited=entry.daily_bar_model_limited or tradeability.model_limited,
        )
    return _modelled_outcome(entry, exit_bar, horizon, tradeability.model_limited or profile.quality != "ok")


def _modelled_outcome(
    entry: _ProbabilityEntry,
    exit_bar: Kline,
    horizon: int,
    exit_limited: bool,
) -> ProbabilityLabelOutcome:
    sell_amount = exit_bar.close * entry.quantity
    sell_cost = trade_costs(entry.cost_profile, side="sell", gross_amount=sell_amount).total
    gross_return = exit_bar.close / entry.entry_price - 1
    net_return = (sell_amount - sell_cost - entry.buy_amount - entry.buy_cost) / (entry.buy_amount + entry.buy_cost)
    if not math.isfinite(net_return):
        return ProbabilityLabelOutcome(horizon, "data_unavailable", "non_finite_return", entry_date=entry.entry_date, exit_date=exit_bar.date)
    return ProbabilityLabelOutcome(
        horizon, "modelled", "target_close", label=int(net_return > 0), gross_return=gross_return,
        net_return=net_return, cost_drag=gross_return - net_return, entry_date=entry.entry_date,
        exit_date=exit_bar.date, entry_price=entry.entry_price, exit_price=exit_bar.close,
        model_limited=entry.model_limited or exit_limited,
        rule_profile_verified=entry.rule_profile_verified,
        daily_bar_model_limited=entry.daily_bar_model_limited or exit_limited,
    )


def _metadata(
    symbol: str,
    market: str,
    list_date: str | None,
    is_st: bool,
    quote_date: str,
) -> PaperInstrumentMetadata:
    return PaperInstrumentMetadata(
        symbol=symbol, market=market, list_date=list_date, is_st=is_st,
        status_effective_date=quote_date, source="market-scan-probability-label",
    )


def _safe_profile(
    symbol: str,
    row_date: str,
    metadata: PaperInstrumentMetadata,
) -> PaperTradeRuleProfile | None:
    try:
        return resolve_trade_rule_profile(symbol, date.fromisoformat(row_date), metadata)
    except (KeyError, ValueError):
        return None


def _previous_bar(bars: Mapping[str, Kline], row_date: str) -> Kline | None:
    prior_dates = [value for value in bars if value < row_date]
    return bars[max(prior_dates)] if prior_dates else None


def _model_quantity(notional: float, price: float, minimum: int, step: int) -> int:
    if price <= 0 or step <= 0:
        return 0
    quantity = (math.floor(notional / price) // step) * step
    return quantity if quantity >= minimum else 0


def _outcome_for_horizon(outcome: ProbabilityLabelOutcome, horizon: int) -> ProbabilityLabelOutcome:
    return ProbabilityLabelOutcome(
        horizon=horizon, status=outcome.status, reason=outcome.reason,
        entry_date=outcome.entry_date, exit_date=outcome.exit_date,
        model_limited=outcome.model_limited,
        rule_profile_verified=outcome.rule_profile_verified,
        daily_bar_model_limited=outcome.daily_bar_model_limited,
    )


__all__ = [
    "PROBABILITY_DEFAULT_HORIZONS",
    "PROBABILITY_EXECUTION_MODEL",
    "PROBABILITY_LABEL_VERSION",
    "ProbabilityLabelConfig",
    "ProbabilityLabelOutcome",
    "build_probability_label_outcomes",
    "probability_label_contract",
]
