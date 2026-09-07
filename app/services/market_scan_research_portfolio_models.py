"""Serializable contracts for a finite-capital, daily research account."""

from __future__ import annotations

from dataclasses import dataclass, field
import math
from typing import Literal

from app.models.paper_trading import CostProfileName
from app.services.market_scan_official_execution import OfficialExecutionSessionRow


RESEARCH_PORTFOLIO_VERSION = "shared-cash-official-daily-ledger-v3"
RESEARCH_CAPITAL_POLICY = "fixed-H-plus-1-sleeves;open-before-close;no-symbol-stacking;no-slot-refill"


@dataclass(frozen=True)
class ResearchPortfolioConfig:
    initial_cash: float = 1_000_000
    top_n: int = 100
    horizon: int = 5
    cost_profile: CostProfileName = "base"
    max_participation_rate: float = .01
    allocation: Literal["top-n", "frozen-universe"] = "top-n"

    def __post_init__(self) -> None:
        if isinstance(self.initial_cash, bool) or not math.isfinite(self.initial_cash) or self.initial_cash <= 0:
            raise ValueError("initial_cash must be finite and positive")
        if round(self.initial_cash, 2) != self.initial_cash:
            raise ValueError("initial_cash must be an exact cent amount")
        for name in ("top_n", "horizon"):
            value = getattr(self, name)
            if isinstance(value, bool) or not isinstance(value, int) or value <= 0:
                raise ValueError(f"{name} must be a positive integer")
        if self.cost_profile not in {"base", "conservative", "stress"}:
            raise ValueError("unsupported cost profile")
        if self.allocation not in {"top-n", "frozen-universe"}:
            raise ValueError("unsupported allocation policy")
        if isinstance(self.max_participation_rate, bool) or not 0 < self.max_participation_rate <= 1:
            raise ValueError("max_participation_rate must be in (0, 1]")


@dataclass(frozen=True)
class ResearchSignalCandidate:
    symbol: str
    frozen_rank: int
    source_identity: str


@dataclass(frozen=True)
class ResearchSignalBatch:
    batch_id: str
    signal_date: str
    source_digest: str
    candidates: tuple[ResearchSignalCandidate, ...]


@dataclass(frozen=True)
class ResearchPortfolioTrade:
    session_date: str
    side: Literal["buy", "sell"]
    batch_id: str
    source_digest: str
    source_identity: str
    symbol: str
    frozen_rank: int
    sleeve: int
    quantity: int
    price: float
    gross_amount: float
    fees: float
    sleeve_cash_after: float
    execution_row_digest: str
    exit_delay_sessions: int = 0


@dataclass(frozen=True)
class ResearchPortfolioEvent:
    session_date: str
    batch_id: str
    sleeve: int
    symbol: str | None
    reason: str


@dataclass(frozen=True)
class ResearchPortfolioPosition:
    batch_id: str
    source_digest: str
    source_identity: str
    symbol: str
    frozen_rank: int
    sleeve: int
    quantity: int
    entry_date: str
    target_exit_date: str
    entry_price: float
    entry_fees: float
    cost_basis: float
    mark_price: float | None = None
    market_value: float | None = None
    unresolved_reason: str | None = None


@dataclass(frozen=True)
class ResearchPortfolioDay:
    session_date: str
    cash: float
    sleeve_cash: tuple[float, ...]
    market_value: float | None
    nav: float | None
    daily_return: float | None
    drawdown: float | None
    gross_traded: float
    turnover: float | None
    invested_cost_basis: float
    fees: float
    cumulative_fees: float
    positions: tuple[ResearchPortfolioPosition, ...]
    unresolved_reasons: tuple[str, ...]


@dataclass(frozen=True)
class ResearchPortfolioResult:
    schema_version: str
    capital_policy: str
    config: ResearchPortfolioConfig
    provenance_status: str
    source_artifact_digests: tuple[str, ...]
    input_digest: str
    result_digest: str
    days: tuple[ResearchPortfolioDay, ...]
    trades: tuple[ResearchPortfolioTrade, ...]
    events: tuple[ResearchPortfolioEvent, ...]
    final_positions: tuple[ResearchPortfolioPosition, ...]
    total_fees: float
    total_return: float | None
    maximum_drawdown: float | None
    valuation_coverage: float
    expected_entry_slots: int
    filled_entry_slots: int
    entry_fill_coverage: float
    unknown_entry_slots: int
    entry_decision_evidence_coverage: float
    promotion_eligible: bool = False
    limitations: tuple[str, ...] = (
        "research_only; no live orders or production ranking changes",
        "no corporate-action cash/share ledger; affected holdings remain unresolved",
        "daily execution states do not prove order-book fills or close-auction capacity",
        "entry capacity uses exact prior-session turnover; missing frozen slots retain cash",
        "source ranks require independent immutable decision admission before strategy claims",
    )


@dataclass(frozen=True)
class ResearchMarketData:
    rows: dict[tuple[str, str], OfficialExecutionSessionRow]
    provenance_status: str
    source_artifact_digests: tuple[str, ...]


@dataclass
class ResearchReplayState:
    cash: list[float]
    positions: list[ResearchPortfolioPosition] = field(default_factory=list)
    trades: list[ResearchPortfolioTrade] = field(default_factory=list)
    events: list[ResearchPortfolioEvent] = field(default_factory=list)
    days: list[ResearchPortfolioDay] = field(default_factory=list)
    cumulative_fees: float = 0.0
    nav_peak: float = 0.0
    valuation_gap: bool = False
    unknown_entry_slots: int = 0
