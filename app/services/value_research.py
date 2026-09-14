"""Explain admitted valuation inputs without adding another score or provider call."""

from __future__ import annotations

import math

from app.models.fuyao import FinancialReportBundle
from app.models.fuyao_scoring import FuyaoValuationScore
from app.models.value_research import ValueResearchCheck, ValueResearchReport, ValueValuationEvidence
from app.services.fuyao_observations import canonical_stock
from app.services.value_research_financials import financial_value_periods


VALUE_RESEARCH_LIMITATIONS = [
    "估值覆盖只统计已准入的 PE TTM / PB MRQ，不是置信度或完整价值分析覆盖率；不新增评分。",
    "盈利收益率是账面盈利与价格的倒数换算，不是股息率、现金收益率或预期投资回报；账面价值比不是清算价值。",
    "财报观察按所选报告期解释，不等同当前或 TTM 财务状态；未见负值不代表财务健康。",
    "金额单位、季度单季/累计口径及首次披露与修订记录仍待确认，不计算现金转换率、财务总分或目标价。",
    "当前缓存估值与财报各有独立观察时间；本摘要不改变行情批次评分或全市场排名。",
]


def build_value_research(
    valuation: FuyaoValuationScore, financials: FinancialReportBundle | None,
) -> ValueResearchReport:
    if financials is not None and canonical_stock(financials.symbol) != canonical_stock(valuation.symbol):
        raise ValueError("value research inputs belong to different stocks")
    return ValueResearchReport(
        symbol=valuation.symbol, evaluated_at=valuation.evaluated_at,
        valuation=_valuation_evidence(valuation),
        periods=financial_value_periods(financials, valuation.evaluated_at) if financials else [],
        limitations=list(VALUE_RESEARCH_LIMITATIONS),
    )


def _valuation_evidence(result: FuyaoValuationScore) -> ValueValuationEvidence:
    pe, pb = (result.pe_ttm, result.pb_mrq) if result.score_available else (None, None)
    checks = [_multiple_check("pe_ttm", "PE TTM", pe, result), _multiple_check("pb_mrq", "PB MRQ", pb, result)]
    count = sum(item.status != "unavailable" for item in checks)
    earnings, earnings_reason = _positive_inverse(pe, "PE TTM", "盈利收益率", result)
    book, book_reason = _positive_inverse(pb, "PB MRQ", "账面价值比", result)
    return ValueValuationEvidence(
        available_inputs=count, coverage="complete" if count == 2 else "partial" if count else "unavailable",
        source=result.source, fetched_at=result.fetched_at, checks=checks,
        earnings_yield_pct=earnings, earnings_yield_reason=earnings_reason,
        book_to_price_pct=book, book_to_price_reason=book_reason,
    )


def _multiple_check(key: str, label: str, value: float | None, result: FuyaoValuationScore) -> ValueResearchCheck:
    if value is None or value == 0:
        reason = result.unavailable_reason or ("零值不构成有意义的估值倍数" if value == 0 else "提供者未返回该指标")
        return ValueResearchCheck(key=key, label=label, status="unavailable", summary=reason,
                                  action="刷新估值记录并核实该指标口径；缺失项不填中性值。")
    if value < 0:
        reason = "负 PE 提示盈利口径为负，不能按低市盈率解释。" if key == "pe_ttm" else "负 PB 提示账面权益口径为负，不能按便宜资产解释。"
        return ValueResearchCheck(key=key, label=label, status="attention", value=value, unit="倍",
                                  summary=reason, action="核对对应财报、异常损益和权益口径。")
    return ValueResearchCheck(key=key, label=label, status="observed", value=value, unit="倍",
                              summary="当前缓存观察已通过身份、摘要和七日时效准入；正倍数本身不证明低估。",
                              action="结合相同行业、相同口径历史与未来盈利可持续性核实。")


def _positive_inverse(
    value: float | None, label: str, name: str, result: FuyaoValuationScore,
) -> tuple[float | None, str]:
    if not result.score_available:
        return None, result.unavailable_reason or "当前估值观察未通过准入"
    if value is None or value <= 0:
        return None, f"本页面仅对正 {label} 展示{name}；缺失、零值和负值保留原始解释。"
    inverse = 100.0 / value
    if not math.isfinite(inverse):
        return None, f"{label} 过于接近零，倒数超出可显示范围。"
    return inverse, f"100 ÷ {label}，依据当前已准入观察计算，未作为额外加分项。"
