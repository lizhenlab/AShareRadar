from __future__ import annotations

import asyncio
from datetime import date, datetime

from app.models.market import Kline, Quote
from app.models.market_scan import MarketScanResultItem, MarketScanResultWrite, MarketScanRun
from app.services.market_scan_stock_evaluation import MarketScanStockEvaluator


async def evaluate_market_scan_batch(
    evaluator: MarketScanStockEvaluator,
    run: MarketScanRun,
    items: list[MarketScanResultItem],
    *,
    quote_map: dict[str, Quote],
    quote_error: str | None,
    semaphore: asyncio.Semaphore,
    cancel_event: asyncio.Event,
    as_of: datetime,
    cutoff: date,
    expected_data_date: date,
    expected_quote_date: date,
    prefetched_klines: dict[str, list[Kline]] | None,
) -> list[MarketScanResultWrite | BaseException]:
    outcomes = await asyncio.gather(
        *(
            evaluator.scan_one(
                item,
                quote_map.get(item.symbol),
                quote_error=quote_error,
                semaphore=semaphore,
                cancel_event=cancel_event,
                as_of=as_of,
                cutoff=cutoff,
                expected_data_date=expected_data_date,
                expected_quote_date=expected_quote_date,
                mode=run.mode,
                rule_version=run.rule_version,
                prefetched_cache=(
                    prefetched_klines.get(item.symbol, [])
                    if prefetched_klines is not None
                    else None
                ),
            )
            for item in items
        ),
        return_exceptions=True,
    )
    return list(outcomes)


__all__ = ["evaluate_market_scan_batch"]
