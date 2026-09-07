"""Frozen-input, fixed 2x2 research ablations of the v5 score.

These candidates are never registered as production scoring variants. Parameters
are specified before outcomes are loaded; the module accepts signal evidence only.
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import dataclass, replace
import math
from typing import Literal, cast

from app.artifacts.io import canonical_json_bytes, sha256_hex
from app.models.market import Kline, KlineAdjustmentMode, Quote
from app.models.market_scan import MARKET_SCAN_FULL_MARKET_SCOPE, MarketScanResultItem, MarketScanRun
from app.services.indicator_trend import trend_score_from_impact
from app.services.indicator_trend_components import build_trend_context, trend_contributions
from app.services.market_scan_score_dimensions import verify_market_scan_point_in_time_evidence_context
from app.services.market_scan_scoring import market_scan_score_spec, replay_score_details, stable_score_spec_hash


ResearchChallenger = Literal["production_v5", "without_continuous_trend", "smooth_turnover", "combined"]
RESEARCH_CHALLENGERS: tuple[ResearchChallenger, ...] = (
    "production_v5", "without_continuous_trend", "smooth_turnover", "combined",
)


@dataclass(frozen=True)
class FrozenResearchScore:
    symbol: str
    original_rank: int
    production_raw_score: float
    trend_score: int
    quality_penalty: float
    continuous_adjustment: float
    impact_without_turnover: float
    turnover_rate: float
    source_digest: str


@dataclass(frozen=True)
class ResearchCandidateScore:
    symbol: str
    rank: int
    original_rank: int
    raw_score: float
    variant: ResearchChallenger
    specification_digest: str
    source_digest: str


def research_challenger_spec(variant: ResearchChallenger) -> dict[str, object]:
    if variant not in RESEARCH_CHALLENGERS:
        raise ValueError("unknown research challenger")
    return {
        "schema_version": "full-market-fixed-factorial-challenger-v1",
        "variant": variant,
        "research_only": True,
        "production_mutation": False,
        "production_score_contract": stable_score_spec_hash(market_scan_score_spec(min_data_quality_score=50)),
        "quality": "retain-frozen-quality-penalty",
        "turnover": "cubic-smoothstep-fixed-knots" if variant in ("smooth_turnover", "combined") else "production",
        "turnover_knots": [[0, 0], [1, 0], [2, 8], [8, 8], [10, 0], [14, 0], [16, -5]],
        "continuous_trend": "removed" if variant in ("without_continuous_trend", "combined") else "production",
        "trend_soft_clip": "retain-production-integer-tanh-rounding",
        "rounding": "python-round-half-to-even-4-decimals",
        "tie_break": "raw-score-desc,symbol-asc",
        "training": "none; fixed parameters before loading outcomes",
    }


def smooth_turnover_impact(rate: float) -> float:
    if not math.isfinite(rate) or rate < 0:
        raise ValueError("turnover must be a finite nonnegative number")
    if rate <= 1:
        return 0.0
    if rate < 2:
        return 8 * _smoothstep(rate - 1)
    if rate <= 8:
        return 8.0
    if rate < 10:
        return 8 * (1 - _smoothstep((rate - 8) / 2))
    if rate <= 14:
        return 0.0
    if rate < 16:
        return -5 * _smoothstep((rate - 14) / 2)
    return -5.0


def _smoothstep(value: float) -> float:
    return value * value * (3 - 2 * value)


def prepare_research_scores(items: Sequence[MarketScanResultItem], run: MarketScanRun) -> tuple[FrozenResearchScore, ...]:
    if run.mode != "official" or run.scope != MARKET_SCAN_FULL_MARKET_SCOPE:
        raise ValueError("research requires the exact official full-market contract")
    if not items or len(items) != run.success_count:
        raise ValueError("incomplete frozen ranked universe")
    symbols = [item.symbol for item in items]
    if len(set(symbols)) != len(symbols):
        raise ValueError("duplicate symbol in frozen universe")
    prepared = tuple(_prepare_score(item, run) for item in items)
    ordered = sorted(prepared, key=lambda item: (-item.production_raw_score, item.symbol))
    if [item.original_rank for item in ordered] != list(range(1, len(ordered)+1)):
        raise ValueError("frozen ranking is incomplete or inconsistent")
    return tuple(ordered)


def _prepare_score(item: MarketScanResultItem, run: MarketScanRun) -> FrozenResearchScore:
    if item.status != "success" or item.run_id != run.id or item.rank is None:
        raise ValueError("invalid frozen candidate identity")
    evidence = _score_evidence(item)
    if not verify_market_scan_point_in_time_evidence_context(
        evidence, item=item, expected_data_date=run.data_date, expected_quote_date=run.quote_date,
        expected_as_of=run.as_of, expected_mode=run.mode, require_action_eligible=False,
    ):
        raise ValueError("frozen score evidence cannot be replayed")
    replay = replay_score_details(item.score_details)
    if replay.score_spec_schema_version != 5 or replay.raw_score != item.raw_score:
        raise ValueError("frozen production v5 score mismatch")
    if item.score_details.get("score_spec_hash") != research_challenger_spec("production_v5")["production_score_contract"]:
        raise ValueError("research family requires the frozen v5 quality threshold 50 specification")
    payload = _mapping(evidence["payload"])
    impact, turnover = _turnover_inputs(payload)
    final = _mapping(_mapping(item.score_details["components"])["final_score"])
    return FrozenResearchScore(
        symbol=item.symbol, original_rank=item.rank, production_raw_score=replay.raw_score,
        trend_score=int(cast(int, item.trend_score)), quality_penalty=float(cast(float, final["quality_penalty"])),
        continuous_adjustment=float(cast(float, final["continuous_trend_adjustment"])),
        impact_without_turnover=impact, turnover_rate=turnover,
        source_digest=sha256_hex(canonical_json_bytes({"run_id": run.id, "rank": item.rank, "evidence": evidence})),
    )


def _score_evidence(item: MarketScanResultItem) -> Mapping[str, object]:
    components = _mapping(item.score_details.get("components"))
    dimensions = _mapping(components.get("score_dimensions"))
    return _mapping(dimensions.get("point_in_time_evidence"))


def _mapping(value: object) -> Mapping[str, object]:
    if not isinstance(value, Mapping):
        raise ValueError("frozen score evidence structure is invalid")
    return value


def _turnover_inputs(payload: Mapping[str, object]) -> tuple[float, float]:
    contracts = cast(list[list[object]], payload["bar_contract_61"])
    rows = [_bar(row) for row in contracts]
    turnover = float(cast(float, payload["quote_turnover_rate"]))
    quote = Quote.model_construct(
        price=float(cast(float, payload["quote_price"])),
        change_pct=float(cast(float, payload["quote_change_pct"])), turnover_rate=turnover,
    )
    contributions = trend_contributions(build_trend_context(quote, rows, mode="official"))
    return sum(item.impact for item in contributions if (item.category, item.name) != ("活跃度", "换手率")), turnover


def _bar(row: Sequence[object]) -> Kline:
    return Kline(
        date=str(row[0]), open=float(cast(float, row[1])), close=float(cast(float, row[2])),
        high=float(cast(float, row[3])), low=float(cast(float, row[4])), volume=float(cast(float, row[5])),
        adjustment_mode=cast(KlineAdjustmentMode, row[6]), data_version=str(row[7]),
        contract_version=str(row[8]), as_of=str(row[9]),
    )


def score_research_challenger(rows: Sequence[FrozenResearchScore], variant: ResearchChallenger) -> tuple[ResearchCandidateScore, ...]:
    specification_digest = sha256_hex(canonical_json_bytes(research_challenger_spec(variant)))
    if not rows or len({row.symbol for row in rows}) != len(rows):
        raise ValueError("research scores require a nonempty unique universe")
    if any(not all(math.isfinite(value) for value in (
        row.production_raw_score, row.trend_score, row.quality_penalty,
        row.continuous_adjustment, row.impact_without_turnover, row.turnover_rate,
    )) for row in rows):
        raise ValueError("research scores must be finite")
    provisional = [ResearchCandidateScore(
        symbol=row.symbol, rank=0, original_rank=row.original_rank, raw_score=_candidate_score(row, variant),
        variant=variant, specification_digest=specification_digest, source_digest=row.source_digest,
    ) for row in rows]
    ordered = sorted(provisional, key=lambda row: (-row.raw_score, row.symbol))
    return tuple(replace(row, rank=index) for index, row in enumerate(ordered, 1))


def _candidate_score(row: FrozenResearchScore, variant: ResearchChallenger) -> float:
    if variant == "production_v5":
        return row.production_raw_score
    trend = row.trend_score
    if variant in ("smooth_turnover", "combined"):
        trend = trend_score_from_impact(row.impact_without_turnover + smooth_turnover_impact(row.turnover_rate))
    adjustment = 0.0 if variant in ("without_continuous_trend", "combined") else row.continuous_adjustment
    return round(max(0.0, min(100.0, trend-row.quality_penalty+adjustment)), 4)
