from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass

from app.models.schemas import AbnormalEventSummary, AnalysisResult, LhbSummary

LHB_MOVE_THRESHOLD = 7
LHB_TURNOVER_THRESHOLD = 12
LHB_LARGE_AMOUNT_THRESHOLD = 1_000_000_000
LHB_WEAK_TREND_THRESHOLD = 45
LHB_MISSING_DATA = ["龙虎榜上榜日期", "买入席位", "卖出席位", "净买入额", "游资/机构标签"]
LHB_CAPABILITY_MESSAGE = "当前未接入真实龙虎榜榜单与席位数据，无法判断是否上榜。"
LHB_DEFAULT_ACTION = "当前未检测到需要额外核查的强量价异动。"
LHB_DEFAULT_SUMMARY = f"龙虎榜数据能力不可用：{LHB_CAPABILITY_MESSAGE}系统不会根据行情推断上榜事实。"
LHB_REVIEW_SUMMARY = (
    f"龙虎榜数据能力不可用：{LHB_CAPABILITY_MESSAGE}下列内容仅为异动核查建议，不代表该股已上榜，也不构成龙虎榜证据。"
)


@dataclass(frozen=True)
class LhbRuleResult:
    reasons: list[str]
    action_items: list[str]


@dataclass(frozen=True)
class LhbSignalRule:
    evaluate: Callable[[AnalysisResult], LhbRuleResult]


LHB_SIGNAL_RULES = (
    LhbSignalRule(lambda analysis: _large_move_signal(analysis)),
    LhbSignalRule(lambda analysis: _high_turnover_signal(analysis)),
)


def build_lhb_summary(analysis: AnalysisResult, abnormal_events: AbnormalEventSummary | None = None) -> LhbSummary:
    quote = analysis.quote
    reasons = _lhb_reasons(analysis, abnormal_events)
    action_items = _lhb_action_items(analysis, reasons)
    return LhbSummary(
        symbol=f"{quote.code}.{quote.market}",
        available=False,
        updated_at=quote.timestamp,
        score=0,
        level="不可用",
        summary=_lhb_summary(reasons),
        reasons=reasons,
        seats=[],
        missing_data=LHB_MISSING_DATA,
        action_items=action_items or [LHB_DEFAULT_ACTION],
        reliability="不可用（未接入真实源）",
        source="未接入真实龙虎榜数据源",
        capability_status="unavailable",
        capability_message=LHB_CAPABILITY_MESSAGE,
    )


def _lhb_reasons(analysis: AnalysisResult, abnormal_events: AbnormalEventSummary | None) -> list[str]:
    reasons = [reason for rule in LHB_SIGNAL_RULES for reason in rule.evaluate(analysis).reasons]
    return [*reasons, *_abnormal_event_reasons(abnormal_events)]


def _lhb_action_items(analysis: AnalysisResult, reasons: list[str]) -> list[str]:
    action_items = [action for rule in LHB_SIGNAL_RULES for action in rule.evaluate(analysis).action_items]
    action_items.extend(_context_action_items(analysis, reasons))
    return action_items


def _large_move_signal(analysis: AnalysisResult) -> LhbRuleResult:
    change_pct = analysis.quote.change_pct
    if abs(change_pct) < LHB_MOVE_THRESHOLD:
        return LhbRuleResult([], [])
    return LhbRuleResult(
        [f"量价异动：当日涨跌幅 {change_pct:.2f}%。"],
        ["异动核查建议：如需判断是否上榜，请以交易所正式龙虎榜为准。"],
    )


def _high_turnover_signal(analysis: AnalysisResult) -> LhbRuleResult:
    turnover = analysis.quote.turnover_rate
    if turnover is None or turnover < LHB_TURNOVER_THRESHOLD:
        return LhbRuleResult([], [])
    return LhbRuleResult(
        [f"量价异动：换手率 {turnover:.2f}%，成交活跃。"],
        ["异动核查建议：若交易所确认上榜，再核对买卖席位、净买入额及机构/游资方向。"],
    )


def _abnormal_event_reasons(abnormal_events: AbnormalEventSummary | None) -> list[str]:
    if not abnormal_events:
        return []
    return [f"行情异动：{item.title}。" for item in abnormal_events.events[:3]]


def _context_action_items(analysis: AnalysisResult, reasons: list[str]) -> list[str]:
    action_items: list[str] = []
    if reasons and analysis.quote.amount >= LHB_LARGE_AMOUNT_THRESHOLD:
        action_items.append("异动核查建议：若正式榜单可查，再比较净买入额与成交额，避免只看绝对金额。")
    if analysis.trend_score < LHB_WEAK_TREND_THRESHOLD and reasons:
        action_items.append("异动核查建议：趋势偏弱时，优先判断量价变化是短暂修复还是抛压释放。")
    return action_items


def _lhb_summary(reasons: list[str]) -> str:
    return LHB_REVIEW_SUMMARY if reasons else LHB_DEFAULT_SUMMARY


__all__ = ["build_lhb_summary"]
