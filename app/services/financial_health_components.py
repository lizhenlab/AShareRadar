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

FORMAL_FINANCIAL_FIELDS = ["ROE", "营收增速", "净利润增速", "经营现金流", "资产负债率", "分红记录"]


@dataclass
class FinancialHealthState:
    score: int = 54
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
    if quote.pe is None:
        state.metrics.append(missing_metric("市盈率", "行情源暂未返回 PE，无法判断利润对应估值。"))
        state.missing.append("PE")
        return
    pe_score, pe_level, pe_summary = pe_view(quote.pe)
    state.score += round((pe_score - 50) * 0.18)
    state.metrics.append(
        FinancialMetric(
            name="市盈率",
            value=f"{quote.pe:.2f}",
            level=pe_level,
            summary=pe_summary,
            source=quote.source,
        )
    )
    if pe_level in {"偏强", "强"}:
        state.highlights.append("市盈率处在相对可解释区间，估值压力暂不突出。")
    if pe_level in {"偏弱", "弱"}:
        state.risk_notes.append(pe_summary)


def _apply_pb_metric(state: FinancialHealthState, analysis: AnalysisResult) -> None:
    quote = analysis.quote
    if quote.pb is None:
        state.metrics.append(missing_metric("市净率", "行情源暂未返回 PB，资产估值锚不足。"))
        state.missing.append("PB")
        return
    pb_score, pb_level, pb_summary = pb_view(quote.pb)
    state.score += round((pb_score - 50) * 0.14)
    state.metrics.append(
        FinancialMetric(
            name="市净率",
            value=f"{quote.pb:.2f}",
            level=pb_level,
            summary=pb_summary,
            source=quote.source,
        )
    )
    if pb_level in {"偏强", "强"}:
        state.highlights.append("市净率没有明显脱离净资产锚。")
    if pb_level in {"偏弱", "弱"}:
        state.risk_notes.append(pb_summary)


def _apply_market_cap_metric(state: FinancialHealthState, analysis: AnalysisResult) -> None:
    quote = analysis.quote
    if quote.market_cap is None:
        state.metrics.append(missing_metric("总市值", "行情源暂未返回总市值，规模和流动性判断需降权。"))
        state.missing.append("总市值")
        return
    cap_score, cap_level, cap_summary = market_cap_view(quote.market_cap)
    state.score += round((cap_score - 50) * 0.12)
    state.metrics.append(
        FinancialMetric(
            name="总市值",
            value=format_amount_text(quote.market_cap),
            level=cap_level,
            summary=cap_summary,
            source=quote.source,
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
            )
        )
        return
    state.metrics.append(missing_metric("所属行业", "行业字段缺失，暂不能做同行比较。"))
    state.missing.extend(["所属行业", "行业估值分位"])


def _apply_liquidity_metric(state: FinancialHealthState, analysis: AnalysisResult) -> None:
    quote = analysis.quote
    liquidity_score, liquidity_level, liquidity_summary = liquidity_view(quote.amount, quote.turnover_rate)
    state.score += round((liquidity_score - 50) * 0.12)
    state.metrics.append(
        FinancialMetric(
            name="交易活跃度",
            value=liquidity_metric_value(quote.amount, quote.turnover_rate),
            level=liquidity_level,
            summary=liquidity_summary,
            source=quote.source,
        )
    )
    if liquidity_level in {"偏弱", "弱"}:
        state.risk_notes.append(liquidity_summary)


def _apply_financial_fallbacks(state: FinancialHealthState) -> None:
    if not state.highlights:
        state.highlights.append("当前只能从行情估值字段做基础体检，完整财报源接入后需要重新校验。")
    if not state.risk_notes:
        state.risk_notes.append("尚未接入资产负债、现金流和利润增长，基本面风险只能做初筛。")


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
