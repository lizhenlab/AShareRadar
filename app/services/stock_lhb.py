from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass

from app.models.schemas import AbnormalEventSummary, AnalysisResult, LhbSummary
from app.services.scoring import clamp_score, score_level

LHB_MOVE_THRESHOLD = 7
LHB_STRONG_MOVE_THRESHOLD = 9
LHB_TURNOVER_THRESHOLD = 12
LHB_LARGE_AMOUNT_THRESHOLD = 1_000_000_000
LHB_WEAK_TREND_THRESHOLD = 45
LHB_BASE_SCORE = 42
LHB_REASON_SCORE = 10
LHB_STRONG_MOVE_BONUS = 8
LHB_MISSING_DATA = ["龙虎榜上榜日期", "买入席位", "卖出席位", "净买入额", "游资/机构标签"]
LHB_DEFAULT_REASON = "未触发明显龙虎榜前置观察条件。"
LHB_DEFAULT_ACTION = "未触发强异动时，龙虎榜不是当前分析主线。"
LHB_DEFAULT_SUMMARY = "龙虎榜正式席位数据源待接入，当前先根据涨跌幅、换手和异动强度提示关注价值。"
LHB_TRIGGERED_SUMMARY = "存在短线异动特征，适合在正式龙虎榜源接入后重点核查买卖席位。"


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
    score = _lhb_score(analysis, reasons)
    level = score_level(score)
    return LhbSummary(
        symbol=f"{quote.code}.{quote.market}",
        available=False,
        updated_at=quote.timestamp,
        score=score,
        level=level,
        summary=_lhb_summary(reasons),
        reasons=reasons or [LHB_DEFAULT_REASON],
        seats=[],
        missing_data=LHB_MISSING_DATA,
        action_items=action_items or [LHB_DEFAULT_ACTION],
        reliability="正式榜单待接入，当前为前置候选判断",
        source="预留接口·本地异动前置判断",
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
        [f"当日涨跌幅 {change_pct:.2f}%，接近龙虎榜常见异动观察区。"],
        ["收盘后核查是否进入龙虎榜异动名单。"],
    )


def _high_turnover_signal(analysis: AnalysisResult) -> LhbRuleResult:
    turnover = analysis.quote.turnover_rate
    if turnover is None or turnover < LHB_TURNOVER_THRESHOLD:
        return LhbRuleResult([], [])
    return LhbRuleResult(
        [f"换手率 {turnover:.2f}%，短线资金博弈强。"],
        ["重点看买一/卖一席位是否集中，机构与游资是否同向。"],
    )


def _abnormal_event_reasons(abnormal_events: AbnormalEventSummary | None) -> list[str]:
    if not abnormal_events:
        return []
    return [item.title for item in abnormal_events.events[:3]]


def _context_action_items(analysis: AnalysisResult, reasons: list[str]) -> list[str]:
    action_items: list[str] = []
    if analysis.quote.amount >= LHB_LARGE_AMOUNT_THRESHOLD:
        action_items.append("成交额较大时，核查榜单净买入额占成交额比例，避免只看绝对金额。")
    if analysis.trend_score < LHB_WEAK_TREND_THRESHOLD and reasons:
        action_items.append("趋势偏弱时，即使上榜也先判断是修复还是出货。")
    return action_items


def _lhb_score(analysis: AnalysisResult, reasons: list[str]) -> int:
    strong_move_bonus = LHB_STRONG_MOVE_BONUS if abs(analysis.quote.change_pct) >= LHB_STRONG_MOVE_THRESHOLD else 0
    return clamp_score(LHB_BASE_SCORE + len(reasons) * LHB_REASON_SCORE + strong_move_bonus)


def _lhb_summary(reasons: list[str]) -> str:
    return LHB_TRIGGERED_SUMMARY if reasons else LHB_DEFAULT_SUMMARY


__all__ = ["build_lhb_summary"]
