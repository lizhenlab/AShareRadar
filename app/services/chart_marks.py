from __future__ import annotations

from datetime import datetime
import math

from app.models.schemas import ChartMarkItem, ChartMarkSummary, StockEventItem, StockInsightBundle, StockNoteItem
from app.services.datahub import DataHub
from app.utils.symbols import normalize_symbol
from app.utils.time import now_text


ABNORMAL_EVENT_MARK_LIMIT = 8
GENERAL_EVENT_MARK_LIMIT = 6
DATE_KEY_FORMATS = (
    "%Y-%m-%d %H:%M:%S",
    "%Y/%m/%d %H:%M:%S",
    "%Y-%m-%d",
    "%Y/%m/%d",
)
INVALID_MARK_TEXT_VALUES = {"", "none", "null", "nan", "+nan", "-nan", "inf", "+inf", "-inf", "infinity", "+infinity", "-infinity"}


async def build_chart_marks(datahub: DataHub, symbol: str, limit: int = 80) -> ChartMarkSummary:
    code, market = normalize_symbol(symbol)
    normalized = f"{code}.{market.upper()}"
    from app.workflows.individual import stock_workbench_context

    context = await stock_workbench_context(datahub, normalized)
    return build_chart_marks_from_context(datahub, normalized, context.insights, limit=limit)


def build_chart_marks_from_context(
    datahub: DataHub,
    symbol: str,
    bundle: StockInsightBundle,
    limit: int = 80,
) -> ChartMarkSummary:
    code, market = normalize_symbol(symbol)
    normalized = f"{code}.{market.upper()}"
    marks = _all_chart_marks(datahub, normalized, bundle, limit)
    visible_marks = [item for item in marks if item.visible][: max(0, limit)]
    categories = sorted({item.category for item in visible_marks})
    return ChartMarkSummary(symbol=normalized, updated_at=now_text(), marks=visible_marks, categories=categories)


def _all_chart_marks(datahub: DataHub, symbol: str, bundle: StockInsightBundle, limit: int) -> list[ChartMarkItem]:
    notes = datahub.cache.stock_notes(symbol, limit=max(0, limit), visible_only=True)
    return [
        *_note_marks(notes),
        *_abnormal_event_marks(bundle),
        *_regular_event_marks(bundle),
    ]


def _abnormal_event_marks(bundle: StockInsightBundle) -> list[ChartMarkItem]:
    return [
        _event_mark(
            date=item.date,
            label=item.title,
            category="异动",
            level=item.level,
            description=item.description,
            source="行情异动识别",
        )
        for item in bundle.abnormal_events.events[:ABNORMAL_EVENT_MARK_LIMIT]
    ]


def _regular_event_marks(bundle: StockInsightBundle) -> list[ChartMarkItem]:
    return [
        _event_mark(
            date=item.date,
            label=item.title,
            category=item.category,
            level=item.level,
            description=item.description,
            source=item.source,
        )
        for item in _non_abnormal_events(bundle)
    ]


def _non_abnormal_events(bundle: StockInsightBundle) -> list[StockEventItem]:
    return [item for item in bundle.events.events if item.category != "异动"][:GENERAL_EVENT_MARK_LIMIT]


def _event_mark(*, date: str, label: str, category: str, level: str, description: str, source: str) -> ChartMarkItem:
    clean_date = _clean_text(date) or ""
    kline_date = _date_key(clean_date)
    clean_label = _clean_text(label)
    clean_category = _clean_text(category) or "事件"
    clean_level = _clean_text(level) or "观察"
    return ChartMarkItem(
        date=clean_date,
        kline_date=kline_date,
        price=None,
        label=clean_label or clean_category,
        category=clean_category,
        level=clean_level,
        description=_clean_text(description) or "事件详情待确认。",
        source=_clean_text(source) or "事件",
        color=_level_color(clean_level),
        anchor_price_type="close",
        visible=bool(kline_date and clean_label),
    )


def _note_marks(notes: list[StockNoteItem]) -> list[ChartMarkItem]:
    marks: list[ChartMarkItem] = []
    for note in notes:
        date_text = _clean_text(getattr(note, "trade_date", None)) or _clean_text(getattr(note, "created_at", None)) or ""
        kline_date = _date_key(date_text)
        price = _positive_price(getattr(note, "price", None))
        marks.append(
            ChartMarkItem(
                date=date_text,
                kline_date=kline_date,
                price=price,
                label=_clean_text(getattr(note, "note_type", None)) or "观察",
                category="笔记",
                level="观察",
                description=_clean_text(getattr(note, "content", None)) or "用户笔记。",
                source="用户笔记",
                color=_clean_text(getattr(note, "color", None)) or "#2563eb",
                anchor_price_type="manual" if price is not None else "close",
                visible=bool(getattr(note, "visible", False) and kline_date),
            )
        )
    return marks


def _level_color(level: str) -> str:
    if level == "风险":
        return "#0f9f6e"
    if level == "积极":
        return "#d92d20"
    return "#b7791f"


def _date_key(value: str | None) -> str | None:
    if not value:
        return None
    raw = value.strip()
    for input_format in DATE_KEY_FORMATS:
        try:
            return datetime.strptime(raw, input_format).strftime("%Y-%m-%d")
        except ValueError:
            continue
    return None


def _positive_price(value: object) -> float | None:
    if isinstance(value, bool) or value is None:
        return None
    try:
        price = float(value)
    except (TypeError, ValueError):
        return None
    return price if math.isfinite(price) and price > 0 else None


def _clean_text(value: object) -> str | None:
    if value is None:
        return None
    text = " ".join(str(value).split())
    return None if text.casefold() in INVALID_MARK_TEXT_VALUES else text
