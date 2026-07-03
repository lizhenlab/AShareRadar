from __future__ import annotations

from dataclasses import dataclass, field
from typing import Callable

from app.models.schemas import AnalysisResult
from app.services.financial_metrics import format_amount_text, market_cap_view, pb_view, pe_view
from app.services.valuation_anchors import (
    peer_valuation_percentile,
    peer_valuation_sample_count,
    price_percentile_from_klines,
    valuation_anchor_label,
    valuation_percentile_from_history,
)
from app.utils.market_data import finite_float


@dataclass(frozen=True)
class ValuationAnchorSnapshot:
    price_percentile: float | None
    pe_percentile: float | None
    pb_percentile: float | None
    peer_pe_percentile: float | None
    peer_pb_percentile: float | None
    peer_sample_count: int
    valuation_anchor_label: str


@dataclass
class ValuationScoreState:
    score: int = 52
    evidence: list[str] = field(default_factory=list)
    watch_points: list[str] = field(default_factory=list)
    missing: list[str] = field(default_factory=list)


@dataclass(frozen=True)
class PercentileDeltaRule:
    name: str
    delta: int
    matches: Callable[[float], bool]


def build_valuation_anchor_snapshot(analysis: AnalysisResult) -> ValuationAnchorSnapshot:
    price_percentile = price_percentile_from_klines(analysis)
    pe_percentile = valuation_percentile_from_history(analysis, "pe")
    pb_percentile = valuation_percentile_from_history(analysis, "pb")
    peer_pe_percentile = peer_valuation_percentile(analysis, "pe")
    peer_pb_percentile = peer_valuation_percentile(analysis, "pb")
    return ValuationAnchorSnapshot(
        price_percentile=price_percentile,
        pe_percentile=pe_percentile,
        pb_percentile=pb_percentile,
        peer_pe_percentile=peer_pe_percentile,
        peer_pb_percentile=peer_pb_percentile,
        peer_sample_count=peer_valuation_sample_count(analysis),
        valuation_anchor_label=valuation_anchor_label(
            price_percentile,
            pe_percentile,
            pb_percentile,
            peer_pe_percentile,
            peer_pb_percentile,
        ),
    )


def build_valuation_score_state(
    analysis: AnalysisResult,
    anchors: ValuationAnchorSnapshot,
) -> ValuationScoreState:
    state = ValuationScoreState()
    _apply_price_anchor(state, anchors)
    _apply_pe_history(state, analysis, anchors)
    _apply_pb_history(state, analysis, anchors)
    _apply_current_pe(state, analysis)
    _apply_current_pb(state, analysis)
    _apply_peer_pe(state, analysis, anchors)
    _apply_peer_pb(state, analysis, anchors)
    _apply_market_cap(state, analysis)
    _apply_industry_context(state, analysis)
    _apply_trend_context(state, analysis)
    if not state.watch_points:
        state.watch_points.append("估值仅作安全边际观察，买卖仍需结合趋势、资金和风险触发。")
    return state


def _apply_price_anchor(state: ValuationScoreState, anchors: ValuationAnchorSnapshot) -> None:
    percentile = anchors.price_percentile
    if percentile is None:
        state.missing.append("价格历史分位")
        return
    state.evidence.append(f"价格历史锚：近120日价格分位 {percentile:.1f}%，只用于位置提醒，不直接替代估值分位。")
    if percentile >= 82:
        state.watch_points.append("价格处于自身近期高分位，任何估值偏高信号都需要更严格的失效条件。")
    elif percentile <= 25:
        state.watch_points.append("价格处于自身近期低分位，但仍需确认趋势止跌，不能只因位置低就提前乐观。")


def _apply_pe_history(
    state: ValuationScoreState,
    analysis: AnalysisResult,
    anchors: ValuationAnchorSnapshot,
) -> None:
    percentile = anchors.pe_percentile
    if percentile is None:
        if analysis.quote.pe is not None:
            state.missing.append("PE历史分位")
        return
    state.score += valuation_percentile_score_delta(percentile, analysis.quote.pe)
    state.evidence.append(f"PE历史锚：本地历史PE分位 {percentile:.1f}%，用于衡量自身估值压力。")
    if percentile >= 82:
        state.watch_points.append("PE处于自身历史高分位，趋势越强越要关注估值兑现风险。")
    elif percentile <= 25 and analysis.quote.pe and analysis.quote.pe > 0:
        state.watch_points.append("PE处于自身历史低分位，可作为安全边际线索，但仍需趋势确认。")


def _apply_pb_history(
    state: ValuationScoreState,
    analysis: AnalysisResult,
    anchors: ValuationAnchorSnapshot,
) -> None:
    percentile = anchors.pb_percentile
    if percentile is None:
        if analysis.quote.pb is not None:
            state.missing.append("PB历史分位")
        return
    state.score += round(valuation_percentile_score_delta(percentile, analysis.quote.pb) * 0.65)
    state.evidence.append(f"PB历史锚：本地历史PB分位 {percentile:.1f}%，用于观察资产估值压力。")
    if percentile >= 82:
        state.watch_points.append("PB处于自身历史高分位，回撤时估值压缩会更敏感。")


def _apply_current_pe(state: ValuationScoreState, analysis: AnalysisResult) -> None:
    pe = analysis.quote.pe
    if pe is None:
        state.missing.append("PE")
        return
    clean_pe = finite_float(pe)
    if clean_pe is None:
        pe_score, _, pe_summary = pe_view(float("nan"))
        state.score += round((pe_score - 50) * 0.35)
        state.evidence.append(f"PE 字段异常：{pe_summary}")
        state.watch_points.append("PE 非有限或脏值时，应优先确认行情源和盈利口径。")
        return
    pe_score, _, pe_summary = pe_view(clean_pe)
    state.score += round((pe_score - 50) * 0.35)
    state.evidence.append(f"PE {clean_pe:.2f}：{pe_summary}")
    if clean_pe <= 0:
        state.watch_points.append("PE 为负或无意义时，应优先检查盈利是否亏损或一次性扰动。")
    elif clean_pe > 60:
        state.watch_points.append("PE 偏高时，需要确认业绩增长能否兑现。")


def _apply_current_pb(state: ValuationScoreState, analysis: AnalysisResult) -> None:
    pb = analysis.quote.pb
    if pb is None:
        state.missing.append("PB")
        return
    clean_pb = finite_float(pb)
    if clean_pb is None:
        pb_score, _, pb_summary = pb_view(float("nan"))
        state.score += round((pb_score - 50) * 0.25)
        state.evidence.append(f"PB 字段异常：{pb_summary}")
        state.watch_points.append("PB 非有限或脏值时，应优先确认净资产或行情字段。")
        return
    pb_score, _, pb_summary = pb_view(clean_pb)
    state.score += round((pb_score - 50) * 0.25)
    state.evidence.append(f"PB {clean_pb:.2f}：{pb_summary}")
    if clean_pb > 8:
        state.watch_points.append("PB 偏高时，回撤中估值压缩会更敏感。")


def _apply_peer_pe(
    state: ValuationScoreState,
    analysis: AnalysisResult,
    anchors: ValuationAnchorSnapshot,
) -> None:
    percentile = anchors.peer_pe_percentile
    if percentile is None:
        if analysis.stock_profile and analysis.stock_profile.industry and analysis.quote.pe is not None:
            state.missing.append("同行PE分位")
        return
    state.score += round(peer_percentile_score_delta(percentile, analysis.quote.pe) * 0.8)
    state.evidence.append(f"同行PE分位：在同行缓存样本中约处于 {percentile:.1f}% 分位。")
    if percentile >= 80:
        state.watch_points.append("相对同行PE已偏高，若缺少业绩兑现，估值压力会先于趋势修正。")


def _apply_peer_pb(
    state: ValuationScoreState,
    analysis: AnalysisResult,
    anchors: ValuationAnchorSnapshot,
) -> None:
    percentile = anchors.peer_pb_percentile
    if percentile is None:
        if analysis.stock_profile and analysis.stock_profile.industry and analysis.quote.pb is not None:
            state.missing.append("同行PB分位")
        return
    state.score += round(peer_percentile_score_delta(percentile, analysis.quote.pb) * 0.55)
    state.evidence.append(f"同行PB分位：在同行缓存样本中约处于 {percentile:.1f}% 分位。")


def _apply_market_cap(state: ValuationScoreState, analysis: AnalysisResult) -> None:
    market_cap = analysis.quote.market_cap
    if market_cap is None:
        state.missing.append("总市值")
        return
    cap_score, _, cap_summary = market_cap_view(market_cap)
    state.score += round((cap_score - 50) * 0.12)
    state.evidence.append(f"总市值 {format_amount_text(market_cap)}：{cap_summary}")


def _apply_industry_context(state: ValuationScoreState, analysis: AnalysisResult) -> None:
    industry = analysis.industry_context
    if industry is None:
        state.missing.append("行业估值分位")
        return
    state.evidence.append(f"行业背景 {industry.name} 涨跌幅 {industry.change_pct:.2f}%。")
    if industry.change_pct < -1:
        state.watch_points.append("行业当日偏弱，估值修复可能缺少板块配合。")


def _apply_trend_context(state: ValuationScoreState, analysis: AnalysisResult) -> None:
    if analysis.trend_score < 45 and state.score >= 58:
        state.watch_points.append("估值看起来不贵，但趋势偏弱，不能只因便宜而提前判断止跌。")
    if analysis.trend_score >= 70 and state.score < 45:
        state.watch_points.append("趋势较强但估值压力偏大，追高时要更依赖风控线。")


def valuation_percentile_score_delta(percentile: float, value: float | None) -> int:
    return _percentile_score_delta(percentile, value, VALUATION_PERCENTILE_DELTA_RULES, invalid_delta=-10)


def peer_percentile_score_delta(percentile: float, value: float | None) -> int:
    return _percentile_score_delta(percentile, value, PEER_PERCENTILE_DELTA_RULES, invalid_delta=-8)


def _percentile_score_delta(
    percentile: float,
    value: float | None,
    rules: tuple[PercentileDeltaRule, ...],
    *,
    invalid_delta: int,
) -> int:
    clean_value = None if isinstance(value, bool) else finite_float(value)
    if value is not None and (clean_value is None or clean_value <= 0):
        return invalid_delta
    clean_percentile = _percentile_float(percentile)
    if clean_percentile is None:
        return 0
    for rule in rules:
        if rule.matches(clean_percentile):
            return rule.delta
    return 0


VALUATION_PERCENTILE_DELTA_RULES = (
    PercentileDeltaRule("very_high", -10, lambda percentile: percentile >= 88),
    PercentileDeltaRule("high", -5, lambda percentile: percentile >= 72),
    PercentileDeltaRule("very_low", 6, lambda percentile: percentile <= 18),
    PercentileDeltaRule("low", 3, lambda percentile: percentile <= 32),
)


PEER_PERCENTILE_DELTA_RULES = (
    PercentileDeltaRule("very_high", -8, lambda percentile: percentile >= 85),
    PercentileDeltaRule("high", -4, lambda percentile: percentile >= 72),
    PercentileDeltaRule("very_low", 5, lambda percentile: percentile <= 18),
    PercentileDeltaRule("low", 2, lambda percentile: percentile <= 32),
)


def valuation_summary(score: int, missing: list[str]) -> str:
    primary_missing = {"PE", "PB", "总市值"}.intersection(missing)
    if len(primary_missing) >= 2:
        return "估值字段不足，暂只能做低置信度观察。"
    if score >= 65:
        return "估值压力相对可控，但仍需和趋势确认一起使用。"
    if score >= 50:
        return "估值处在中性区间，重点看业绩和行业背景能否配合。"
    return "估值压力偏高或字段质量不足，追高需要更严格的失效条件。"


def _percentile_float(value: float) -> float | None:
    if isinstance(value, bool):
        return None
    number = finite_float(value)
    if number is None or number < 0 or number > 100:
        return None
    return number


__all__ = [
    "ValuationAnchorSnapshot",
    "ValuationScoreState",
    "build_valuation_anchor_snapshot",
    "build_valuation_score_state",
    "peer_percentile_score_delta",
    "valuation_percentile_score_delta",
    "valuation_summary",
]
