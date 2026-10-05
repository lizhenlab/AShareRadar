"""Fixed directional evidence budgets, independent of current profile weights.

These budgets and the risk coefficient are engineering choices, not fitted
probabilities. The existing risk observation contains quality and other risk
inputs; its deduction must remain visible separately from directional evidence.
"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass
import math
from types import MappingProxyType

from app.models.research import StandardFactor


FACTOR_AGGREGATION_VERSION = "factor-aggregation.v3"
RISK_PENALTY_COEFFICIENT = 0.25
_BUDGET_UNITS = MappingProxyType({
    "trend_momentum": 1,
    "chip_position": 1,
    "volume_confirmation": 1,
    "fund_flow_proxy": 1,
    "valuation_anchor": 2,
    "risk_pressure": 0,
})
_TOTAL_BUDGET_UNITS = 6
FACTOR_SHARES = MappingProxyType({factor_id: units * 100 / _TOTAL_BUDGET_UNITS for factor_id, units in _BUDGET_UNITS.items()})
FACTOR_GROUPS = (
    ("趋势与价位", ("trend_momentum", "chip_position")),
    ("量价", ("volume_confirmation", "fund_flow_proxy")),
    ("估值", ("valuation_anchor",)),
)


@dataclass(frozen=True)
class FactorGroupContribution:
    name: str
    budget_pct: float
    contribution: float
    coverage_pct: float


@dataclass(frozen=True)
class FactorAggregationBreakdown:
    version: str
    directional_score: float
    risk_penalty: float
    total_score: int
    coverage_pct: float
    groups: tuple[FactorGroupContribution, ...]
    factor_shares: dict[str, float]
    excluded_ids: tuple[str, ...]


def aggregate_factor_scores(factors: Sequence[StandardFactor]) -> FactorAggregationBreakdown:
    """Keep missing slots at 50; reject ambiguous known identities before scoring."""
    scores, excluded_ids = _admitted_scores(factors)
    groups = tuple(_group_contribution(name, ids, scores) for name, ids in FACTOR_GROUPS)
    directional = 50 + math.fsum((score - 50) * _BUDGET_UNITS[factor_id]
                                for factor_id, score in scores.items()) / _TOTAL_BUDGET_UNITS
    penalty = max(0.0, 50 - scores.get("risk_pressure", 50)) * RISK_PENALTY_COEFFICIENT
    return FactorAggregationBreakdown(
        version=FACTOR_AGGREGATION_VERSION,
        directional_score=directional,
        risk_penalty=penalty,
        total_score=round(max(0.0, min(100.0, directional - penalty))),
        coverage_pct=sum(_BUDGET_UNITS[factor_id] for factor_id in scores) * 100 / _TOTAL_BUDGET_UNITS,
        groups=groups,
        factor_shares=dict(FACTOR_SHARES),
        excluded_ids=excluded_ids,
    )


def _admitted_scores(factors: Sequence[StandardFactor]) -> tuple[dict[str, float], tuple[str, ...]]:
    scores: dict[str, float] = {}
    seen: set[str] = set()
    excluded: set[str] = set()
    for factor in factors:
        if factor.id not in FACTOR_SHARES:
            excluded.add(factor.id)
            continue
        if factor.id in seen:
            raise ValueError(f"duplicate factor identity: {factor.id}")
        seen.add(factor.id)
        score = _admitted_score(factor)
        if score is None:
            excluded.add(factor.id)
        else:
            scores[factor.id] = score
    return scores, tuple(sorted(excluded))


def _admitted_score(factor: StandardFactor) -> float | None:
    if (factor.participates_in_current_score is not True
            or factor.aggregation_role != "independent" or factor.data_nature == "unavailable"):
        return None
    if isinstance(factor.score, bool) or not isinstance(factor.score, (int, float)):
        return None
    try:
        score = float(factor.score)
    except (ValueError, OverflowError):
        return None
    return max(0.0, min(100.0, score)) if math.isfinite(score) else None


def _group_contribution(name: str, factor_ids: tuple[str, ...], scores: dict[str, float]) -> FactorGroupContribution:
    budget = sum(_BUDGET_UNITS[factor_id] for factor_id in factor_ids)
    available = sum(_BUDGET_UNITS[factor_id] for factor_id in factor_ids if factor_id in scores)
    contribution = math.fsum(
        (scores.get(factor_id, 50) - 50) * _BUDGET_UNITS[factor_id] for factor_id in factor_ids
    ) / _TOTAL_BUDGET_UNITS
    return FactorGroupContribution(name, budget * 100 / _TOTAL_BUDGET_UNITS, contribution, 100 * available / budget)


__all__ = [
    "FACTOR_AGGREGATION_VERSION", "FACTOR_SHARES", "FactorAggregationBreakdown",
    "FactorGroupContribution", "aggregate_factor_scores",
]
