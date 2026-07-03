from __future__ import annotations

from dataclasses import dataclass
from typing import Callable

from app.models.schemas import (
    AbnormalEventSummary,
    AnalysisResult,
    LhbSummary,
    StockEventItem,
)


@dataclass(frozen=True)
class ExternalEventContext:
    analysis: AnalysisResult
    lhb: LhbSummary | None

    @property
    def quote_timestamp(self) -> str:
        return self.analysis.quote.timestamp

    @property
    def change_pct(self) -> float:
        return self.analysis.quote.change_pct

    @property
    def turnover_rate(self) -> float | None:
        return self.analysis.quote.turnover_rate


@dataclass(frozen=True)
class ExternalEventRule:
    name: str
    matches: Callable[[ExternalEventContext], bool]
    event: Callable[[ExternalEventContext], StockEventItem]
    next_step: str


def collect_event_items(
    analysis: AnalysisResult,
    *,
    abnormal_events: AbnormalEventSummary | None = None,
    lhb: LhbSummary | None = None,
) -> list[StockEventItem]:
    events: list[StockEventItem] = []
    events.extend(review_events(analysis))
    events.extend(industry_events(analysis))
    events.extend(abnormal_event_items(abnormal_events))
    events.extend(lhb_events(lhb))
    events.extend(data_quality_events(analysis))
    events.extend(external_event_placeholders(analysis, lhb))
    return events


def review_events(analysis: AnalysisResult) -> list[StockEventItem]:
    if not analysis.review:
        return []
    return [
        StockEventItem(
            date=item.date,
            title=item.title,
            category="历史复盘",
            level=item.level,
            description=item.description,
            source="本地K线复盘",
            reliability="本地复盘",
            action_hint="用于理解历史波动，不等同于最新消息。",
        )
        for item in analysis.review.events
    ]


def industry_events(analysis: AnalysisResult) -> list[StockEventItem]:
    if not analysis.industry_context:
        return []
    industry = analysis.industry_context
    level = "积极" if industry.change_pct > 1 else "风险" if industry.change_pct < -1 else "观察"
    return [
        StockEventItem(
            date=industry.updated_at,
            title="行业背景变化",
            category="行业",
            level=level,
            description=f"{industry.name} 当前涨跌幅 {industry.change_pct:.2f}%。",
            source=industry.source,
            reliability="公开板块数据",
            action_hint="结合个股强弱判断是否跟随行业。",
        )
    ]


def abnormal_event_items(abnormal_events: AbnormalEventSummary | None) -> list[StockEventItem]:
    if not abnormal_events:
        return []
    return [
        StockEventItem(
            date=item.date,
            title=item.title,
            category="异动",
            level=item.level,
            description=item.description,
            source="行情异动识别",
            reliability="行情推断",
            action_hint=(item.watch_points or ["观察后续确认。"])[0],
        )
        for item in abnormal_events.events[:4]
    ]


def lhb_events(lhb: LhbSummary | None) -> list[StockEventItem]:
    if not lhb or not lhb.available:
        return []
    return [
        StockEventItem(
            date=lhb.updated_at,
            title="龙虎榜信号",
            category="龙虎榜",
            level=lhb.level,
            description=lhb.summary,
            source=lhb.source,
            reliability=lhb.reliability,
            action_hint=(lhb.action_items or ["核查正式龙虎榜。"])[0],
        )
    ]


def data_quality_events(analysis: AnalysisResult) -> list[StockEventItem]:
    quote = analysis.quote
    return [
        StockEventItem(
            date=analysis.data_quality.checked_at or quote.timestamp,
            title="数据质量提醒",
            category="数据",
            level="观察",
            description=note,
            source=analysis.data_quality.source,
            reliability="系统检测",
            action_hint="数据质量下降时，所有策略结论自动降权。",
        )
        for note in analysis.data_quality.anomalies[:3]
    ]


def external_event_placeholders(analysis: AnalysisResult, lhb: LhbSummary | None) -> list[StockEventItem]:
    context = ExternalEventContext(analysis=analysis, lhb=lhb)
    return [rule.event(context) for rule in EXTERNAL_EVENT_RULES if rule.matches(context)][:3]


def default_observation_event(analysis: AnalysisResult) -> StockEventItem:
    quote = analysis.quote
    return StockEventItem(
        date=quote.timestamp,
        title="暂无高强度事件",
        category="观察",
        level="观察",
        description="当前未从K线、行业和数据质量中识别出明显事件，公告/研报源接入后会补充。",
        source="本地分析",
        reliability="本地推断",
        action_hint="继续观察行情、行业和数据质量变化。",
    )


def event_next_steps(analysis: AnalysisResult, lhb: LhbSummary | None) -> list[str]:
    steps = ["优先确认数据质量，低质量行情下事件结论自动降权。"]
    context = ExternalEventContext(analysis=analysis, lhb=lhb)
    steps.extend(rule.next_step for rule in EXTERNAL_EVENT_RULES if rule.matches(context))
    return steps[:4]


def _has_lhb_candidate(context: ExternalEventContext) -> bool:
    return bool(context.lhb and context.lhb.reasons)


def _needs_announcement_check(context: ExternalEventContext) -> bool:
    return abs(context.change_pct) >= 5 or context.analysis.risk_level == "高风险"


def _needs_margin_check(context: ExternalEventContext) -> bool:
    return context.turnover_rate is not None and context.turnover_rate >= 8


def _lhb_candidate_event(context: ExternalEventContext) -> StockEventItem:
    assert context.lhb is not None
    lhb = context.lhb
    return StockEventItem(
        date=context.quote_timestamp,
        title="龙虎榜候选核查",
        category="龙虎榜",
        level=lhb.level,
        description="已触发龙虎榜前置候选条件，但尚未接入正式席位明细。",
        source=lhb.source,
        reliability=lhb.reliability,
        action_hint=(lhb.action_items or ["收盘后核查正式榜单。"])[0],
    )


def _announcement_check_event(context: ExternalEventContext) -> StockEventItem:
    return StockEventItem(
        date=context.quote_timestamp,
        title="公告事件待核查",
        category="公告",
        level="观察" if context.change_pct >= 0 else "风险",
        description="价格波动较大，建议核查是否有公告、监管问询、业绩预告或重大事项影响。",
        source="预留接口·公告核查清单",
        reliability="待接入",
        action_hint="正式公告源接入前，不把消息面作为确定性结论。",
    )


def _margin_check_event(context: ExternalEventContext) -> StockEventItem:
    return StockEventItem(
        date=context.quote_timestamp,
        title="融资融券待核查",
        category="融资融券",
        level="观察",
        description="换手活跃，若融资余额快速上升，需警惕杠杆资金追涨；若融券增加，需关注分歧。",
        source="预留接口·两融核查清单",
        reliability="待接入",
        action_hint="两融数据未接入前，仅作为下一步核查项。",
    )


EXTERNAL_EVENT_RULES = (
    ExternalEventRule(
        name="lhb_candidate",
        matches=_has_lhb_candidate,
        event=_lhb_candidate_event,
        next_step="收盘后核查龙虎榜席位、净买入额和机构/游资方向。",
    ),
    ExternalEventRule(
        name="announcement_check",
        matches=_needs_announcement_check,
        event=_announcement_check_event,
        next_step="核查交易所公告、业绩预告、监管问询和行业新闻。",
    ),
    ExternalEventRule(
        name="margin_check",
        matches=_needs_margin_check,
        event=_margin_check_event,
        next_step="补充融资融券余额变化，判断活跃成交是否带有杠杆资金。",
    ),
)


__all__ = [
    "abnormal_event_items",
    "collect_event_items",
    "data_quality_events",
    "default_observation_event",
    "event_next_steps",
    "EXTERNAL_EVENT_RULES",
    "external_event_placeholders",
    "industry_events",
    "lhb_events",
    "review_events",
]
