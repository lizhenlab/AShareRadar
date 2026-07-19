from __future__ import annotations

from collections.abc import Iterable
from typing import Any

from app.models.schemas import MinuteKline, OrderBook, OrderBookLevel, ProviderCapability, Quote
from app.services.data_quality_time import normalize_quote_event_time
from app.services.datahub_runtime import run_provider_io
from app.services.futu_mappers import futu_symbol, minute_kline_from_row, quote_from_snapshot_row
from app.services.provider_errors import ProviderProtocolError
from app.services.provider_utils import ensure_positive_limit, is_installed, pick
from app.utils.parsing import safe_float
from app.utils.symbols import normalize_symbol, standard_symbol
from app.utils.time import now_text


FUTU_KLINE_TYPE_BY_INTERVAL = {
    "1m": "K_1M",
    "3m": "K_3M",
    "5m": "K_5M",
    "10m": "K_10M",
    "15m": "K_15M",
    "30m": "K_30M",
    "60m": "K_60M",
}
MAX_ORDER_BOOK_LEVELS = 5


class FutuProvider:
    source_name = "Futu OpenAPI"

    def __init__(self, host: str = "127.0.0.1", port: int = 11111, enabled: bool = False) -> None:
        self.host = host
        self.port = port
        self.enabled = enabled

    async def quote(self, symbol: str) -> Quote:
        return (await self.quotes([symbol]))[0]

    async def quotes(self, symbols: Iterable[str]) -> list[Quote]:
        requested = _standard_symbols(symbols)
        if not requested:
            return []
        futu_symbols = [self._futu_symbol(symbol) for symbol in requested]
        self._ensure_ready()

        def load() -> list[Quote]:
            from futu import OpenQuoteContext, RET_OK

            ctx = OpenQuoteContext(host=self.host, port=self.port)
            try:
                ret, data = ctx.get_market_snapshot(futu_symbols)
                _ensure_futu_ok(ret, data, RET_OK)
                return _ordered_snapshot_quotes(requested, data, source_name=self.source_name)
            finally:
                ctx.close()

        return await run_provider_io(load)

    async def order_book(self, symbol: str) -> OrderBook:
        futu_symbol = self._futu_symbol(symbol)
        self._ensure_ready()

        def load() -> OrderBook:
            from futu import OpenQuoteContext, RET_OK

            ctx = OpenQuoteContext(host=self.host, port=self.port)
            try:
                ret, data = ctx.get_order_book(futu_symbol)
                _ensure_futu_ok(ret, data, RET_OK)
                return _order_book_from_response(symbol, data, source_name=self.source_name)
            finally:
                ctx.close()

        return await run_provider_io(load)

    async def minute_kline(self, symbol: str, interval: str = "5m", limit: int = 120) -> list[MinuteKline]:
        ensure_positive_limit(limit)
        futu_symbol = self._futu_symbol(symbol)
        self._ensure_ready()

        def load() -> list[MinuteKline]:
            from futu import KLType, OpenQuoteContext, RET_OK

            ctx = OpenQuoteContext(host=self.host, port=self.port)
            try:
                normalized_interval = _normalize_futu_interval(interval)
                ktype = _futu_kltype(KLType, normalized_interval)
                ret, data = ctx.get_cur_kline(futu_symbol, num=limit, ktype=ktype)
                _ensure_futu_ok(ret, data, RET_OK)
                return _minute_klines_from_response(data, interval=normalized_interval, source_name=self.source_name)
            finally:
                ctx.close()

        return await run_provider_io(load)

    async def ping(self) -> str:
        self._ensure_ready()

        def load() -> str:
            from futu import OpenQuoteContext

            ctx = OpenQuoteContext(host=self.host, port=self.port)
            try:
                return f"OpenD连接正常：{self.host}:{self.port}"
            finally:
                ctx.close()

        return await run_provider_io(load)

    def capability(self) -> ProviderCapability:
        installed = is_installed("futu")
        return ProviderCapability(
            name="futu",
            installed=installed,
            enabled=installed and self.enabled,
            reliability_level="客户端授权源",
            realtime_quote=installed and self.enabled,
            minute_kline=installed and self.enabled,
            order_book=installed and self.enabled,
            note="需要启动 Futu OpenD；当前只用于个股实时行情和盘口观察，不接交易。",
        )

    def _ensure_ready(self) -> None:
        if not is_installed("futu"):
            raise RuntimeError("未安装 futu-api，请执行 python3 -m pip install futu-api")
        if not self.enabled:
            raise RuntimeError("Futu OpenAPI 未启用。设置 ASHARE_RADAR_FUTU_ENABLED=1 并启动 Futu OpenD 后再使用")

    @staticmethod
    def _futu_symbol(symbol: str) -> str:
        return futu_symbol(symbol)


def _standard_symbols(symbols: Iterable[str]) -> list[str]:
    return [standard_symbol(symbol) for symbol in symbols]


def _ensure_futu_ok(ret: Any, data: Any, ok_value: Any) -> None:
    if ret != ok_value:
        raise RuntimeError(str(data))


def _ordered_snapshot_quotes(requested: list[str], data: Any, *, source_name: str) -> list[Quote]:
    by_symbol = _snapshot_quotes_by_symbol(data, wanted=set(requested), source_name=source_name)
    missing = [symbol for symbol in requested if symbol not in by_symbol]
    if missing:
        raise RuntimeError(f"Futu快照缺少A股代码：{','.join(missing)}")
    return [by_symbol[symbol] for symbol in requested]


def _snapshot_quotes_by_symbol(data: Any, *, wanted: set[str], source_name: str) -> dict[str, Quote]:
    result: dict[str, Quote] = {}
    missing_event_time: set[str] = set()
    for _, row in data.iterrows():
        event_time = _futu_quote_event_time(row)
        quote = quote_from_snapshot_row(row, stamp=event_time or "", source_name=source_name)
        if quote:
            symbol = f"{quote.code}.{quote.market}"
            if symbol not in wanted:
                continue
            if event_time is None:
                missing_event_time.add(symbol)
            else:
                result[symbol] = quote
    unresolved_event_time = sorted(missing_event_time - result.keys())
    if unresolved_event_time:
        raise ProviderProtocolError(f"Futu快照缺少可解析的事件时间：{','.join(unresolved_event_time)}")
    return result


def _futu_quote_event_time(row: Any) -> str | None:
    value = pick(
        row,
        "update_time",
        "last_trade_time",
        "trade_time",
        "timestamp",
        default=None,
    )
    event_date = pick(row, "trade_date", "date", default=None)
    return normalize_quote_event_time(value, event_date=event_date)


def _order_book_from_response(symbol: str, data: dict[str, Any], *, source_name: str) -> OrderBook:
    code, market = normalize_symbol(symbol)
    bid = _order_book_levels(data.get("Bid", []))
    ask = _order_book_levels(data.get("Ask", []))
    if not bid and not ask:
        raise RuntimeError("Futu盘口深度为空")
    return OrderBook(
        symbol=standard_symbol(symbol),
        code=code,
        market=market.upper(),
        bid=bid,
        ask=ask,
        source=source_name,
        updated_at=now_text(),
    )


def _order_book_levels(rows: Iterable[Any]) -> list[OrderBookLevel]:
    levels: list[OrderBookLevel] = []
    for row in rows:
        level = _order_book_level(row)
        if level is None:
            continue
        levels.append(level)
        if len(levels) >= MAX_ORDER_BOOK_LEVELS:
            break
    return levels


def _order_book_level(row: Any) -> OrderBookLevel | None:
    try:
        price = safe_float(str(row[0]))
        volume = safe_float(str(row[1]))
    except (IndexError, KeyError, TypeError):
        return None
    if price <= 0 or volume < 0:
        return None
    return OrderBookLevel(price=price, volume=volume)


def _minute_klines_from_response(data: Any, *, interval: str, source_name: str) -> list[MinuteKline]:
    result: list[MinuteKline] = []
    for _, row in data.iterrows():
        item = minute_kline_from_row(row, interval=interval, source_name=source_name)
        if item:
            result.append(item)
    return result


def _futu_kltype(kltype, interval: str):
    normalized = _normalize_futu_interval(interval)
    if normalized not in FUTU_KLINE_TYPE_BY_INTERVAL:
        raise ValueError("Futu 分钟周期只支持 1m、3m、5m、10m、15m、30m、60m")
    return getattr(kltype, FUTU_KLINE_TYPE_BY_INTERVAL[normalized])


def _normalize_futu_interval(interval: str) -> str:
    return str(interval or "").lower().strip()
