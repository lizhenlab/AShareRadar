from __future__ import annotations

from app.models.schemas import ChartMarkItem, ChartMarkSummary, StockInsightBundle, StockNoteItem
from app.services.datahub import DataHub
from app.utils.symbols import normalize_symbol
from app.utils.time import now_text


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
    notes = datahub.cache.stock_notes(normalized, limit=limit, visible_only=True)
    marks: list[ChartMarkItem] = []
    marks.extend(_note_marks(notes))
    for item in bundle.abnormal_events.events[:8]:
        marks.append(
            ChartMarkItem(
                date=item.date,
                kline_date=_date_key(item.date),
                price=None,
                label=item.title,
                category="异动",
                level=item.level,
                description=item.description,
                source="行情异动识别",
                color=_level_color(item.level),
                anchor_price_type="close",
                visible=True,
            )
        )
    for item in bundle.events.events[:6]:
        if item.category == "异动":
            continue
        marks.append(
            ChartMarkItem(
                date=item.date,
                kline_date=_date_key(item.date),
                price=None,
                label=item.title,
                category=item.category,
                level=item.level,
                description=item.description,
                source=item.source,
                color=_level_color(item.level),
                anchor_price_type="close",
                visible=True,
            )
        )
    categories = sorted({item.category for item in marks})
    return ChartMarkSummary(symbol=normalized, updated_at=now_text(), marks=marks[:limit], categories=categories)


def _note_marks(notes: list[StockNoteItem]) -> list[ChartMarkItem]:
    marks: list[ChartMarkItem] = []
    for note in notes:
        marks.append(
            ChartMarkItem(
                date=note.trade_date or note.created_at,
                kline_date=_date_key(note.trade_date or note.created_at),
                price=note.price,
                label=note.note_type,
                category="笔记",
                level="观察",
                description=note.content,
                source="用户笔记",
                color=note.color or "#2563eb",
                anchor_price_type="manual" if note.price is not None else "close",
                visible=note.visible,
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
    return value[:10].replace("/", "-")
