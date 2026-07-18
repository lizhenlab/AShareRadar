from __future__ import annotations

from app.models.market import KlineAdjustmentMode
from app.models.schemas import Kline, PlateItem, Quote, StockInfo


def make_quote(
    source: str = "测试行情",
    *,
    price: float = 1300.0,
    prev_close: float = 1290.0,
    high: float = 1310.0,
    low: float = 1288.0,
    change_pct: float = 0.78,
    turnover_rate: float | None = None,
    pe: float | None = None,
    pb: float | None = None,
    market_cap: float | None = None,
    timestamp: str = "2026-05-13 10:00:00",
) -> Quote:
    return Quote(
        code="600519",
        name="贵州茅台",
        market="SH",
        price=price,
        prev_close=prev_close,
        open=1295.0,
        high=high,
        low=low,
        volume=1000000,
        amount=1300000000,
        change=round(price - prev_close, 2),
        change_pct=change_pct,
        turnover_rate=turnover_rate,
        pe=pe,
        pb=pb,
        market_cap=market_cap,
        timestamp=timestamp,
        source=source,
    )


def make_stock_info(code: str = "600519", market: str = "SH") -> StockInfo:
    return StockInfo(
        symbol=f"{code}.{market}",
        code=code,
        market=market,
        name=f"测试{code}",
        industry="测试行业",
        list_date="20000101",
        source="测试股票池",
        updated_at="2026-05-13 10:00:00",
    )


def make_plate_item(change_pct: float = 1.0) -> PlateItem:
    return PlateItem(
        rank=1,
        name="测试行业",
        change_pct=change_pct,
        amount=2_000_000_000,
        turnover_rate=2.0,
        leading_stock="测试龙头",
        leading_stock_change_pct=3.0,
        source="测试板块",
        updated_at="2026-05-13 10:00:00",
    )


def make_kline(
    *,
    close: float = 100.0,
    high: float | None = None,
    low: float | None = None,
    volume: float = 1000.0,
    date: str = "2026-05-13",
    source: str | None = None,
    adjustment_mode: KlineAdjustmentMode = "qfq",
    as_of: str | None = "2026-12-31",
    data_version: str = "test-daily-kline-qfq-v1",
    from_cache: bool = False,
    fallback_used: bool = False,
) -> Kline:
    return Kline(
        date=date,
        open=close - 0.5,
        close=close,
        high=high if high is not None else close + 1,
        low=low if low is not None else close - 1,
        volume=volume,
        adjustment_mode=adjustment_mode,
        as_of=as_of,
        data_version=data_version,
        source=source,
        from_cache=from_cache,
        fallback_used=fallback_used,
    )
