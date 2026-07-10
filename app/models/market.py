"""Market data and provider-facing transfer models."""

from __future__ import annotations

from pydantic import BaseModel


class Quote(BaseModel):
    code: str
    name: str
    market: str
    price: float
    prev_close: float
    open: float
    high: float
    low: float
    volume: float
    amount: float
    change: float
    change_pct: float
    turnover_rate: float | None = None
    pe: float | None = None
    pb: float | None = None
    market_cap: float | None = None
    timestamp: str
    source: str = "腾讯行情"
    from_cache: bool = False
    fallback_used: bool = False


class Kline(BaseModel):
    date: str
    open: float
    close: float
    high: float
    low: float
    volume: float
    source: str | None = None
    fetched_at: str | None = None
    from_cache: bool = False
    fallback_used: bool = False


class MinuteKline(BaseModel):
    timestamp: str
    open: float
    close: float
    high: float
    low: float
    volume: float
    amount: float | None = None
    turnover_rate: float | None = None
    source: str | None = None
    interval: str = "5m"
    fetched_at: str | None = None
    from_cache: bool = False
    fallback_used: bool = False


class StockInfo(BaseModel):
    symbol: str
    code: str
    market: str
    name: str
    industry: str | None = None
    list_date: str | None = None
    source: str
    updated_at: str


class PlateItem(BaseModel):
    rank: int
    name: str
    change_pct: float
    amount: float | None = None
    turnover_rate: float | None = None
    leading_stock: str | None = None
    leading_stock_change_pct: float | None = None
    source: str
    updated_at: str


class StockConceptItem(BaseModel):
    symbol: str
    rank: int
    name: str
    change_pct: float = 0
    amount: float | None = None
    turnover_rate: float | None = None
    leading_stock: str | None = None
    leading_stock_change_pct: float | None = None
    match_reason: str = "概念成分匹配"
    source: str
    updated_at: str


class ProviderCapability(BaseModel):
    name: str
    installed: bool
    enabled: bool
    reliability_level: str = "公开源"
    realtime_quote: bool = False
    daily_kline: bool = False
    minute_kline: bool = False
    stock_pool: bool = False
    plate_rank: bool = False
    concept_board: bool = False
    order_book: bool = False
    note: str


class OrderBookLevel(BaseModel):
    price: float
    volume: float


class OrderBook(BaseModel):
    symbol: str
    code: str
    market: str
    bid: list[OrderBookLevel]
    ask: list[OrderBookLevel]
    source: str
    updated_at: str
