from __future__ import annotations

from app.models.schemas import AbnormalEventSummary, AnalysisResult
from app.services.stock_abnormal_context import build_abnormal_context
from app.services.stock_abnormal_rules import detect_abnormal_events
from app.services.stock_abnormal_summary import summarize_abnormal_events


def build_abnormal_events(analysis: AnalysisResult) -> AbnormalEventSummary:
    context = build_abnormal_context(analysis)
    events = detect_abnormal_events(context)
    return summarize_abnormal_events(context, events)


__all__ = ["build_abnormal_events"]
