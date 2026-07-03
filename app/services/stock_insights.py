from __future__ import annotations

from app.models.schemas import AnalysisResult, OrderBook, StockInsightBundle
from app.services.financial_health import build_financial_health
from app.services.stock_activity import build_fund_flow_analysis, build_order_pressure
from app.services.stock_abnormal_events import build_abnormal_events
from app.services.stock_event_summary import build_event_summary
from app.services.stock_lhb import build_lhb_summary
from app.services.stock_overview import build_stock_overview
from app.services.stock_rules import RULE_VERSION, build_rule_match_summary, rule_definitions
from app.services.stock_strategy import build_strategy_cards
from app.services.valuation_analysis import build_valuation_analysis


def build_stock_insight_bundle(
    analysis: AnalysisResult,
    *,
    order_book: OrderBook | None = None,
    order_book_error: str | None = None,
) -> StockInsightBundle:
    fund_flow = build_fund_flow_analysis(analysis)
    order_pressure = build_order_pressure(analysis, order_book=order_book, order_book_error=order_book_error)
    abnormal_events = build_abnormal_events(analysis)
    lhb = build_lhb_summary(analysis, abnormal_events)
    events = build_event_summary(analysis, abnormal_events=abnormal_events, lhb=lhb)
    strategy_cards = build_strategy_cards(analysis, fund_flow, order_pressure)
    overview = build_stock_overview(analysis, fund_flow, order_pressure, events)
    financial_health = build_financial_health(analysis)
    valuation = build_valuation_analysis(analysis)
    rule_matches = build_rule_match_summary(analysis, fund_flow, order_pressure, valuation, abnormal_events)
    return StockInsightBundle(
        overview=overview,
        fund_flow=fund_flow,
        order_pressure=order_pressure,
        events=events,
        strategy_cards=strategy_cards,
        financial_health=financial_health,
        valuation=valuation,
        lhb=lhb,
        abnormal_events=abnormal_events,
        rule_matches=rule_matches,
    )


__all__ = [
    "RULE_VERSION",
    "build_abnormal_events",
    "build_event_summary",
    "build_financial_health",
    "build_fund_flow_analysis",
    "build_lhb_summary",
    "build_order_pressure",
    "build_stock_insight_bundle",
    "build_stock_overview",
    "build_strategy_cards",
    "build_valuation_analysis",
    "rule_definitions",
]
