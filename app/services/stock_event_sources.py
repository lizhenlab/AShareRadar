from __future__ import annotations

from dataclasses import dataclass
from typing import Callable

from app.models.schemas import (
    AbnormalEventSummary,
    AnalysisResult,
    EventSourceCapability,
    LhbSummary,
    StockEventItem,
)


@dataclass(frozen=True)
class ExternalEventContext:
    analysis: AnalysisResult
    lhb: LhbSummary | None

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
    if not lhb or not lhb.available or lhb.capability_status != "available":
        return []
    return [
        StockEventItem(
            date=lhb.updated_at,
            title="龙虎榜记录",
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
    """Backward-compatible hook that never fabricates events for unavailable sources."""
    return []


def external_source_capabilities(lhb: LhbSummary | None) -> list[EventSourceCapability]:
    lhb_available = bool(lhb and lhb.available and lhb.capability_status == "available")
    return [
        EventSourceCapability(
            key="exchange_announcements",
            label="交易所公告",
            status="unavailable",
            detail="未接入公告原文源；价格波动不会被包装成公告事件。",
        ),
        EventSourceCapability(
            key="lhb",
            label="龙虎榜席位",
            status="available" if lhb_available else "unavailable",
            detail=f"已接入：{lhb.source}" if lhb_available and lhb else "未接入真实榜单与席位源；不会根据行情推断上榜事实。",
        ),
        EventSourceCapability(
            key="margin_financing",
            label="融资融券余额",
            status="unavailable",
            detail="未接入融资融券余额源；换手活跃不会被包装成两融事件。",
        ),
        EventSourceCapability(
            key="research_reports",
            label="研报摘要",
            status="unavailable",
            detail="未接入可核验研报源；不会生成研报占位事件。",
        ),
    ]


def default_observation_event(analysis: AnalysisResult) -> StockEventItem:
    quote = analysis.quote
    return StockEventItem(
        date=quote.timestamp,
        title="暂无高强度事件",
        category="观察",
        level="观察",
        description="当前未从K线、行业和数据质量中识别出明显事件；未接入的外部源不会生成占位记录。",
        source="本地分析",
        reliability="本地推断",
        action_hint="继续观察行情、行业和数据质量变化。",
    )


def event_next_steps(analysis: AnalysisResult, lhb: LhbSummary | None) -> list[str]:
    context = ExternalEventContext(analysis=analysis, lhb=lhb)
    steps = ["优先确认数据质量，低质量行情下事件结论自动降权。"]
    steps.extend(rule.next_step for rule in EXTERNAL_EVENT_RULES if rule.matches(context))
    return steps[:4]


def _needs_lhb_verification(context: ExternalEventContext) -> bool:
    return bool(context.lhb and context.lhb.reasons)


def _needs_announcement_check(context: ExternalEventContext) -> bool:
    return abs(context.change_pct) >= 5 or context.analysis.risk_level == "高风险"


def _needs_margin_check(context: ExternalEventContext) -> bool:
    return context.turnover_rate is not None and context.turnover_rate >= 8


EXTERNAL_EVENT_RULES = (
    ExternalEventRule(
        name="lhb_verification",
        matches=_needs_lhb_verification,
        next_step="异动核查建议：如需确认是否上榜，请查询交易所正式龙虎榜。",
    ),
    ExternalEventRule(
        name="announcement_verification",
        matches=_needs_announcement_check,
        next_step="异动核查建议：价格波动较大，可核查交易所公告、业绩预告和监管问询。",
    ),
    ExternalEventRule(
        name="margin_financing_verification",
        matches=_needs_margin_check,
        next_step="异动核查建议：换手活跃，可到正式渠道核对融资融券余额变化。",
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
    "external_source_capabilities",
    "industry_events",
    "lhb_events",
    "review_events",
]
