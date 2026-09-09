"""Prepare note identity and defaults without inventing market observations."""
from __future__ import annotations

from typing import TYPE_CHECKING

from app.models.market import Quote, StockInfo
from app.models.user_data import StockNoteInput
from app.services.datahub_metadata_stock_pool import STOCK_POOL_FALLBACK_SECONDS
from app.services.datahub_runtime import run_cache_io
from app.utils.stock_pool import normalize_stock_metadata_text, normalize_stock_pool_row
from app.utils.symbols import standard_symbol

if TYPE_CHECKING:
    from app.services.datahub import DataHub


def _verified_note_stock(rows: list[StockInfo], target: str) -> StockInfo | None:
    matches: list[StockInfo] = []
    for item in rows:
        row = normalize_stock_pool_row(item)
        if row is None or row.symbol != target:
            continue
        if normalize_stock_metadata_text(row.name) is None:
            continue
        matches.append(row)
    # Canonical aliases in damaged metadata must not choose a name by row order.
    return matches[0] if len(matches) == 1 else None


async def prepare_stock_note_creation(
    datahub: DataHub, payload: StockNoteInput,
) -> tuple[Quote | StockInfo, StockNoteInput]:
    target = standard_symbol(payload.symbol)
    raw_trade_date = payload.trade_date.strip() if payload.trade_date else ""
    if payload.price is not None and raw_trade_date:
        rows = await run_cache_io(
            datahub.cache.get_stock_pool,
            STOCK_POOL_FALLBACK_SECONDS,
            limit=None,
            keyword=target.split(".", maxsplit=1)[0],
        )
        identity = _verified_note_stock(rows, target)
        if identity is not None:
            return identity, payload

    quote = await datahub.quote(payload.symbol)
    enriched = StockNoteInput(
        symbol=payload.symbol,
        content=payload.content,
        note_type=payload.note_type,
        price=payload.price if payload.price is not None else quote.price,
        trade_date=raw_trade_date or quote.timestamp,
        color=payload.color,
        visible=payload.visible,
    )
    return quote, enriched
