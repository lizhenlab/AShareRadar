"""Market data and provider-facing transfer models."""

from __future__ import annotations

from typing import Literal

from pydantic import BaseModel, FiniteFloat


KlineAdjustmentMode = Literal["qfq", "hfq", "none", "unknown"]
DAILY_KLINE_CONTRACT_VERSION = "daily-kline.v1"
DEFAULT_DAILY_KLINE_ADJUSTMENT_MODE: KlineAdjustmentMode = "qfq"
UNKNOWN_KLINE_DATA_VERSION = "unknown"


class Quote(BaseModel):
    code: str
    name: str
    market: str
    price: FiniteFloat
    prev_close: FiniteFloat
    open: FiniteFloat
    high: FiniteFloat
    low: FiniteFloat
    volume: FiniteFloat
    amount: FiniteFloat
    change: FiniteFloat
    change_pct: FiniteFloat
    turnover_rate: FiniteFloat | None = None
    pe: FiniteFloat | None = None
    pb: FiniteFloat | None = None
    market_cap: FiniteFloat | None = None
    timestamp: str
    source: str = "腾讯行情"
    from_cache: bool = False
    fallback_used: bool = False


class Kline(BaseModel):
    date: str
    open: FiniteFloat
    close: FiniteFloat
    high: FiniteFloat
    low: FiniteFloat
    volume: FiniteFloat
    adjustment_mode: KlineAdjustmentMode = "unknown"
    as_of: str | None = None
    data_version: str = UNKNOWN_KLINE_DATA_VERSION
    contract_version: str = DAILY_KLINE_CONTRACT_VERSION
    source: str | None = None
    fetched_at: str | None = None
    from_cache: bool = False
    fallback_used: bool = False


class MinuteKline(BaseModel):
    timestamp: str
    open: FiniteFloat
    close: FiniteFloat
    high: FiniteFloat
    low: FiniteFloat
    volume: FiniteFloat
    amount: FiniteFloat | None = None
    turnover_rate: FiniteFloat | None = None
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
    change_pct: FiniteFloat
    amount: FiniteFloat | None = None
    turnover_rate: FiniteFloat | None = None
    leading_stock: str | None = None
    leading_stock_change_pct: FiniteFloat | None = None
    source: str
    updated_at: str
    fallback_used: bool = False


class StockConceptItem(BaseModel):
    symbol: str
    rank: int
    name: str
    change_pct: FiniteFloat = 0
    amount: FiniteFloat | None = None
    turnover_rate: FiniteFloat | None = None
    leading_stock: str | None = None
    leading_stock_change_pct: FiniteFloat | None = None
    match_reason: str = "概念成分匹配"
    source: str
    updated_at: str
    fallback_used: bool = False


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
    price: FiniteFloat
    volume: FiniteFloat


class OrderBook(BaseModel):
    symbol: str
    code: str
    market: str
    bid: list[OrderBookLevel]
    ask: list[OrderBookLevel]
    source: str
    updated_at: str
