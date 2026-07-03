from __future__ import annotations

from app.models.schemas import (
    AbnormalEventSummary,
    AnalysisResult,
    LhbSummary,
    StockEventSummary,
)
from app.services.stock_event_sources import (
    collect_event_items,
    default_observation_event,
    event_next_steps,
    external_event_placeholders,
)
from app.utils.time import now_text


def build_event_summary(
    analysis: AnalysisResult,
    *,
    abnormal_events: AbnormalEventSummary | None = None,
    lhb: LhbSummary | None = None,
) -> StockEventSummary:
    quote = analysis.quote
    events = collect_event_items(analysis, abnormal_events=abnormal_events, lhb=lhb)
    if not events:
        events.append(default_observation_event(analysis))
    return StockEventSummary(
        symbol=f"{quote.code}.{quote.market}",
        updated_at=now_text(),
        events=events[-8:],
        notes=[
            "事件层已区分正式数据、公开板块和本地行情推断；缺正式源时自动标注可靠性。",
            "公告、研报、龙虎榜、融资融券接入后可替换候选事件。",
        ],
        missing_sources=["交易所公告", "龙虎榜席位", "融资融券余额", "研报摘要"],
        next_steps=event_next_steps(analysis, lhb),
    )


__all__ = ["build_event_summary", "event_next_steps", "external_event_placeholders"]
