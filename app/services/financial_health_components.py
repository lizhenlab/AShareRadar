from __future__ import annotations

from dataclasses import dataclass, field

from app.models.schemas import AnalysisResult, FinancialMetric
from app.services.financial_metrics import (
    format_amount_text,
    liquidity_view,
    market_cap_view,
    missing_metric,
    pb_view,
    pe_view,
)
from app.utils.market_data import finite_float

FORMAL_FINANCIAL_FIELDS = ["报告期", "ROE", "营收增速", "净利润增速", "经营现金流", "资产负债率"]


@dataclass
class FinancialHealthState:
    score: int | None = None
    formal_minimum_complete: bool = False
    metrics: list[FinancialMetric] = field(default_factory=list)
    highlights: list[str] = field(default_factory=list)
    risk_notes: list[str] = field(default_factory=list)
    missing: list[str] = field(default_factory=lambda: list(FORMAL_FINANCIAL_FIELDS))


def build_financial_health_state(analysis: AnalysisResult) -> FinancialHealthState:
    state = FinancialHealthState()
    _apply_pe_metric(state, analysis)
    _apply_pb_metric(state, analysis)
    _apply_market_cap_metric(state, analysis)
    _apply_industry_metric(state, analysis)
    _apply_liquidity_metric(state, analysis)
    _apply_financial_fallbacks(state)
    return state


def _apply_pe_metric(state: FinancialHealthState, analysis: AnalysisResult) -> None:
    quote = analysis.quote
    pe = finite_float(quote.pe)
    if pe is None:
        state.metrics.append(missing_metric("市盈率", "行情源未返回有效 PE，市场估值锚不可用。", category="market_valuation"))
        state.missing.append("PE")
        return
    _pe_score, pe_level, pe_summary = pe_view(pe)
    state.metrics.append(
        FinancialMetric(
            name="市盈率",
            value=f"{pe:.2f}",
            level=pe_level,
            summary=pe_summary,
            source=quote.source,
            category="market_valuation",
        )
    )
    if pe_level in {"偏强", "强"}:
        state.highlights.append("市盈率处在相对可解释区间，估值压力暂不突出。")
    if pe_level in {"偏弱", "弱"}:
        state.risk_notes.append(pe_summary)


def _apply_pb_metric(state: FinancialHealthState, analysis: AnalysisResult) -> None:
    quote = analysis.quote
    pb = finite_float(quote.pb)
    if pb is None:
        state.metrics.append(missing_metric("市净率", "行情源未返回有效 PB，市场资产估值锚不可用。", category="market_valuation"))
        state.missing.append("PB")
        return
    _pb_score, pb_level, pb_summary = pb_view(pb)
    state.metrics.append(
        FinancialMetric(
            name="市净率",
            value=f"{pb:.2f}",
            level=pb_level,
            summary=pb_summary,
            source=quote.source,
            category="market_valuation",
        )
    )
    if pb_level in {"偏强", "强"}:
        state.highlights.append("市净率没有明显脱离净资产锚。")
    if pb_level in {"偏弱", "弱"}:
        state.risk_notes.append(pb_summary)


def _apply_market_cap_metric(state: FinancialHealthState, analysis: AnalysisResult) -> None:
    quote = analysis.quote
    market_cap = finite_float(quote.market_cap)
    if market_cap is None or market_cap <= 0:
        state.metrics.append(missing_metric("总市值", "行情源未返回有效总市值，规模体征不可用。", category="market_valuation"))
        state.missing.append("总市值")
        return
    _cap_score, cap_level, cap_summary = market_cap_view(market_cap)
    state.metrics.append(
        FinancialMetric(
            name="总市值",
            value=format_amount_text(market_cap),
            level=cap_level,
            summary=cap_summary,
            source=quote.source,
            category="market_valuation",
        )
    )
    state.highlights.append(cap_summary)


def _apply_industry_metric(state: FinancialHealthState, analysis: AnalysisResult) -> None:
    if analysis.stock_profile and analysis.stock_profile.industry:
        state.metrics.append(
            FinancialMetric(
                name="所属行业",
                value=analysis.stock_profile.industry,
                level="观察",
                summary="行业用于横向估值比较，后续接入行业分位会更有参考价值。",
                source=analysis.stock_profile.source,
                category="context",
            )
        )
        return
    state.metrics.append(missing_metric("所属行业", "行业字段缺失，暂不能做同行比较。", category="context"))
    state.missing.extend(["所属行业", "行业估值分位"])


def _apply_liquidity_metric(state: FinancialHealthState, analysis: AnalysisResult) -> None:
    quote = analysis.quote
    liquidity_score, liquidity_level, liquidity_summary = liquidity_view(quote.amount, quote.turnover_rate)
    state.metrics.append(
        FinancialMetric(
            name="交易活跃度",
            value=liquidity_metric_value(quote.amount, quote.turnover_rate),
            level=liquidity_level,
            summary=liquidity_summary,
            source=quote.source,
            category="trading_vital",
        )
    )
    if liquidity_level == "不可用":
        state.missing.append("成交额")
    if liquidity_level in {"偏弱", "弱"}:
        state.risk_notes.append(liquidity_summary)


def _apply_financial_fallbacks(state: FinancialHealthState) -> None:
    state.highlights.insert(0, "当前仅展示市场估值与交易体征，不生成财务体检分。")
    if not state.risk_notes:
        state.risk_notes.append("正式报告期、ROE、营收与利润、现金流和负债率最小集不完整，财务健康结论不可用。")


def liquidity_metric_value(amount: float, turnover_rate: float | None) -> str:
    amount_text = f"成交额 {format_amount_text(amount)}"
    if turnover_rate is None:
        return amount_text
    return f"{amount_text} / 换手 {turnover_rate:.2f}%"


__all__ = [
    "FORMAL_FINANCIAL_FIELDS",
    "FinancialHealthState",
    "build_financial_health_state",
    "liquidity_metric_value",
]
