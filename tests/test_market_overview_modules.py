from __future__ import annotations

import asyncio

import pytest

from app.config import Settings
from app.models.schemas import Quote
from app.utils.symbols import standard_symbol
from app.workflows.market_overview import market_overview, strong_stock_watch
from tests.factories import make_kline


def test_market_overview_keeps_available_indices_when_some_index_quotes_fail() -> None:
    hub = _OverviewHub(failing_symbols={"399001.SZ", "399006.SZ"})

    overview = asyncio.run(market_overview(hub, Settings()))

    assert [item.code for item in overview.indices] == ["000001"]
    assert overview.strong_stocks
    messages = "；".join(message for _, message in hub.cache.events)
    assert "市场指数样本批量行情失败" in messages
    assert "399001.SZ" in messages


def test_strong_stock_watch_keeps_available_custom_symbols_after_single_failure() -> None:
    hub = _OverviewHub(failing_symbols={"600002.SH"})

    result = asyncio.run(strong_stock_watch(hub, Settings(), symbols="600001.SH,600002.SH,600003.SH"))

    assert result["sample_count"] == 2
    assert [item.code for item in result["items"]] == ["600001", "600003"]
    messages = "；".join(message for _, message in hub.cache.events)
    assert "自定义列表强股样本批量行情失败" in messages
    assert "600002.SH" in messages


def test_strong_stock_watch_rejects_explicit_symbols_when_all_are_invalid() -> None:
    hub = _OverviewHub(failing_symbols=set())

    with pytest.raises(ValueError, match="至少输入一个有效股票代码"):
        asyncio.run(strong_stock_watch(hub, Settings(), symbols="bad,bad2"))


def test_strong_stock_watch_rejects_oversized_custom_symbol_list() -> None:
    hub = _OverviewHub(failing_symbols=set())
    symbols = ",".join(f"{index:06d}.SZ" for index in range(1, 52))

    with pytest.raises(ValueError, match="一次最多分析 50 个股票代码"):
        asyncio.run(strong_stock_watch(hub, Settings(), symbols=symbols))


class _EventCache:
    def __init__(self) -> None:
        self.events: list[tuple[str, str]] = []

    def log_event(self, category: str, message: str) -> None:
        self.events.append((category, message))

    def watchlist_symbols(self) -> list[str]:
        return []


class _OverviewHub:
    def __init__(self, *, failing_symbols: set[str]) -> None:
        self.cache = _EventCache()
        self.failing_symbols = failing_symbols

    async def quotes(self, symbols, use_cache: bool = True) -> list[Quote]:
        normalized = [_normalize_symbol(symbol) for symbol in symbols]
        if len(normalized) > 1 and any(symbol in self.failing_symbols for symbol in normalized):
            raise RuntimeError("batch failed")
        result = []
        for symbol in normalized:
            if symbol in self.failing_symbols:
                raise RuntimeError(f"{symbol} failed")
            code, market = symbol.split(".")
            result.append(
                Quote(
                    code=code,
                    name=f"测试{code}",
                    market=market,
                    price=10.0,
                    prev_close=9.8,
                    open=9.9,
                    high=10.2,
                    low=9.7,
                    volume=100000,
                    amount=1_000_000,
                    change=0.2,
                    change_pct=2.0,
                    turnover_rate=1.5,
                    timestamp="2026-05-13 10:00:00",
                    source="测试行情",
                )
            )
        return result

    async def kline(self, symbol: str, limit: int = 80):
        return [make_kline(date="2026-05-13", close=10 + index * 0.1, high=11 + index * 0.1, low=9 + index * 0.1) for index in range(limit)]


def _normalize_symbol(symbol: str) -> str:
    lowered = symbol.lower()
    if lowered.startswith("sh") and "." not in symbol:
        return f"{symbol[-6:]}.SH"
    if lowered.startswith("sz") and "." not in symbol:
        return f"{symbol[-6:]}.SZ"
    return standard_symbol(symbol)
