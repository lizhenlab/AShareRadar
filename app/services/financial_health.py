from __future__ import annotations

from app.models.schemas import AnalysisResult, FinancialHealth
from app.services.financial_health_components import build_financial_health_state, liquidity_metric_value
from app.services.financial_metrics import financial_summary
from app.services.scoring import clamp_score, score_level


def build_financial_health(analysis: AnalysisResult) -> FinancialHealth:
    quote = analysis.quote
    symbol = f"{quote.code}.{quote.market}"
    state = build_financial_health_state(analysis)
    score = clamp_score(state.score)
    return FinancialHealth(
        symbol=symbol,
        updated_at=quote.timestamp,
        score=score,
        level=score_level(score),
        summary=financial_summary(score, state.missing),
        metrics=state.metrics,
        highlights=state.highlights[:4],
        risk_notes=state.risk_notes[:4],
        missing_data=state.missing,
        source=f"{quote.source}·行情字段体检",
    )


def _liquidity_metric_value(amount: float, turnover_rate: float | None) -> str:
    return liquidity_metric_value(amount, turnover_rate)


__all__ = ["build_financial_health"]
