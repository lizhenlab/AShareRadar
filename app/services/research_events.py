from __future__ import annotations

from dataclasses import dataclass

from app.models.schemas import AbnormalEventItem, EventDigestReport, StockEventItem, StockInsightBundle


MAX_EVENT_ITEMS = 4
MAX_MISSING_DATA_ITEMS = 6
DEFAULT_WATCH_EVENT = "暂无会改变结论的明确事件，继续观察行情和行业背景。"


@dataclass
class EventDigestBuckets:
    positive: list[str]
    negative: list[str]
    watch: list[str]

    @classmethod
    def empty(cls) -> "EventDigestBuckets":
        return cls(positive=[], negative=[], watch=[])

    def add(self, text: str, bucket: str) -> None:
        if bucket == "negative":
            self.negative.append(text)
        elif bucket == "positive":
            self.positive.append(text)
        else:
            self.watch.append(text)


def build_event_digest_report(insights: StockInsightBundle) -> EventDigestReport:
    buckets = _event_buckets(insights)
    impact = _impact_label(buckets)
    return EventDigestReport(
        impact_label=impact,
        summary=f"{impact}。事件层仅统计已有数据形成的异动、行业背景和复盘记录；未接入的外部源不计作事件证据。",
        positive_events=_dedupe(buckets.positive)[:MAX_EVENT_ITEMS],
        negative_events=_dedupe(buckets.negative)[:MAX_EVENT_ITEMS],
        watch_events=_watch_events(buckets),
        missing_data=_missing_data(insights),
    )


def _event_buckets(insights: StockInsightBundle) -> EventDigestBuckets:
    buckets = EventDigestBuckets.empty()
    for item in insights.abnormal_events.events:
        buckets.add(_abnormal_event_text(item), _abnormal_event_bucket(item))
    for item in insights.events.events[:MAX_EVENT_ITEMS]:
        buckets.add(_stock_event_text(item), _stock_event_bucket(item))
    return buckets


def _abnormal_event_text(item: AbnormalEventItem) -> str:
    return f"{item.title}：{item.description}"


def _stock_event_text(item: StockEventItem) -> str:
    return f"{item.title}：{item.description}"


def _abnormal_event_bucket(item: AbnormalEventItem) -> str:
    if item.level == "风险" or item.direction == "利空":
        return "negative"
    if item.direction == "利好" or item.level == "积极":
        return "positive"
    return "watch"


def _stock_event_bucket(item: StockEventItem) -> str:
    if item.level == "风险":
        return "negative"
    if item.level == "积极":
        return "positive"
    return "watch"


def _impact_label(buckets: EventDigestBuckets) -> str:
    if buckets.negative:
        return "事件偏风险"
    if buckets.positive:
        return "事件偏积极"
    return "事件待确认"


def _watch_events(buckets: EventDigestBuckets) -> list[str]:
    return _dedupe(buckets.watch)[:MAX_EVENT_ITEMS] or [DEFAULT_WATCH_EVENT]


def _missing_data(insights: StockInsightBundle) -> list[str]:
    return _dedupe([*insights.events.notes, *insights.lhb.missing_data, *insights.abnormal_events.notes])[:MAX_MISSING_DATA_ITEMS]


def _dedupe(items: list[str]) -> list[str]:
    seen: set[str] = set()
    result: list[str] = []
    for item in items:
        if not item or item in seen:
            continue
        seen.add(item)
        result.append(item)
    return result
