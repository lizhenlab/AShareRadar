from __future__ import annotations

from app.models.schemas import AnalysisResult, ValuationAnalysis
from app.services.financial_metrics import format_amount_text
from app.services.scoring import clamp_score, score_level
from app.services.valuation_anchors import (
    MIN_PEER_VALUATION_ROWS,
    MIN_VALUATION_HISTORY_ROWS,
    daily_quote_history_rows,
    peer_valuation_percentile,
    peer_valuation_sample_count,
    peer_valuation_values,
    price_percentile_from_klines,
    valuation_anchor_label,
    valuation_percentile_from_history,
)
from app.services.valuation_components import (
    build_valuation_anchor_snapshot,
    build_valuation_score_state,
    peer_percentile_score_delta,
    valuation_percentile_score_delta,
    valuation_summary,
)


def build_valuation_analysis(analysis: AnalysisResult) -> ValuationAnalysis:
    quote = analysis.quote
    anchors = build_valuation_anchor_snapshot(analysis)
    state = build_valuation_score_state(analysis, anchors)
    score = clamp_score(state.score)
    return ValuationAnalysis(
        symbol=f"{quote.code}.{quote.market}",
        updated_at=quote.timestamp,
        score=score,
        level=score_level(score),
        summary=valuation_summary(score, state.missing),
        pe=quote.pe,
        pb=quote.pb,
        market_cap=quote.market_cap,
        market_cap_text=format_amount_text(quote.market_cap) if quote.market_cap is not None else None,
        price_percentile=anchors.price_percentile,
        pe_percentile=anchors.pe_percentile,
        pb_percentile=anchors.pb_percentile,
        peer_pe_percentile=anchors.peer_pe_percentile,
        peer_pb_percentile=anchors.peer_pb_percentile,
        peer_sample_count=anchors.peer_sample_count,
        valuation_anchor_label=anchors.valuation_anchor_label,
        evidence=state.evidence or ["估值字段不足，暂不能形成有效判断。"],
        watch_points=state.watch_points,
        missing_data=state.missing,
        source=f"{quote.source}·估值字段",
    )


__all__ = [
    "MIN_PEER_VALUATION_ROWS",
    "MIN_VALUATION_HISTORY_ROWS",
    "build_valuation_analysis",
    "daily_quote_history_rows",
    "peer_percentile_score_delta",
    "peer_valuation_percentile",
    "peer_valuation_sample_count",
    "peer_valuation_values",
    "price_percentile_from_klines",
    "valuation_anchor_label",
    "valuation_percentile_from_history",
    "valuation_percentile_score_delta",
    "valuation_summary",
]
