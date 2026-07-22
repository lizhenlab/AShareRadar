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
    external_source_capabilities,
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
    capabilities = external_source_capabilities(lhb)
    return StockEventSummary(
        symbol=f"{quote.code}.{quote.market}",
        updated_at=now_text(),
        events=events[-8:],
        notes=[
            "事件列表仅包含已有数据形成的历史复盘、行业背景、行情异动和质量提醒。",
            "公告、研报、龙虎榜和融资融券源不可用时，仅显示能力状态与核查建议，不生成占位事件。",
        ],
        missing_sources=[item.label for item in capabilities if item.status == "unavailable"],
        next_steps=event_next_steps(analysis, lhb),
        source_capabilities=capabilities,
    )


__all__ = ["build_event_summary", "event_next_steps", "external_event_placeholders"]
