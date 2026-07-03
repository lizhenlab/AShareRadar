from __future__ import annotations

from app.models.schemas import AbnormalEventItem, AbnormalEventSummary
from app.services.scoring import clamp_score, score_level
from app.services.stock_abnormal_context import AbnormalEventContext

_MAIN_SIGNAL_PRIORITY = {"风险": 3, "积极": 2, "观察": 1}


def summarize_abnormal_events(
    context: AbnormalEventContext,
    events: list[AbnormalEventItem],
) -> AbnormalEventSummary:
    risk_count = sum(1 for item in events if item.level == "风险")
    positive_count = sum(1 for item in events if item.level == "积极")
    score = clamp_score(50 + positive_count * 10 - risk_count * 12 + min(len(events), 4) * 3)
    return AbnormalEventSummary(
        symbol=f"{context.quote.code}.{context.quote.market}",
        updated_at=context.quote.timestamp,
        score=score,
        level=_summary_level(score, risk_count, positive_count),
        main_signal=_main_signal(events),
        events=events[:8],
        notes=[
            "异动基于本地行情和K线估算，用于解释当天发生了什么。",
            "正式公告、龙虎榜和逐笔成交接入后，可进一步确认异动来源。",
        ],
    )


def _summary_level(score: int, risk_count: int, positive_count: int) -> str:
    if risk_count >= positive_count and risk_count > 0:
        return "风险"
    if risk_count > 0:
        return "观察"
    return score_level(score)


def _main_signal(events: list[AbnormalEventItem]) -> str:
    if not events:
        return "暂无明显异动"
    return max(events, key=lambda item: _MAIN_SIGNAL_PRIORITY.get(item.level, 0)).title


__all__ = ["summarize_abnormal_events"]
