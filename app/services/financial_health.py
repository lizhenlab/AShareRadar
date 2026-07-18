from __future__ import annotations

from app.models.schemas import AnalysisResult, FinancialHealth
from app.services.financial_health_components import build_financial_health_state, liquidity_metric_value
from app.services.financial_metrics import financial_summary


def build_financial_health(analysis: AnalysisResult) -> FinancialHealth:
    quote = analysis.quote
    symbol = f"{quote.code}.{quote.market}"
    state = build_financial_health_state(analysis)
    return FinancialHealth(
        symbol=symbol,
        updated_at=quote.timestamp,
        score=None,
        score_available=False,
        formal_minimum_complete=state.formal_minimum_complete,
        report_period=None,
        metric_scope="market_valuation_trading_vitals",
        level="不可用",
        summary=financial_summary(None, state.missing, formal_minimum_complete=state.formal_minimum_complete),
        metrics=state.metrics,
        highlights=state.highlights[:4],
        risk_notes=state.risk_notes[:4],
        missing_data=state.missing,
        source=f"{quote.source}·市场估值与交易体征",
    )


def _liquidity_metric_value(amount: float, turnover_rate: float | None) -> str:
    return liquidity_metric_value(amount, turnover_rate)


__all__ = ["build_financial_health"]
