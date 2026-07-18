from __future__ import annotations

import asyncio
import threading

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
    assert overview.degraded is True
    assert overview.index_meta.requested_count == 3
    assert overview.index_meta.sample_count == 1
    assert overview.index_meta.missing_count == 2
    assert overview.index_meta.degraded is True
    assert "市场指数样本行情部分缺失" in overview.index_meta.warnings[0]
    messages = "；".join(message for _, message in hub.cache.events)
    assert "市场指数样本批量行情失败" in messages
    assert "399001.SZ" in messages


def test_strong_stock_watch_keeps_available_custom_symbols_after_single_failure() -> None:
    hub = _OverviewHub(failing_symbols={"600002.SH"})

    result = asyncio.run(strong_stock_watch(hub, Settings(), symbols="600001.SH,600002.SH,600003.SH"))

    assert result["sample_count"] == 2
    assert result["requested_count"] == 3
    assert result["missing_count"] == 1
    assert result["degraded"] is True
    assert "行情部分缺失" in result["warnings"][0]
    assert [item.code for item in result["items"]] == ["600001", "600003"]
    messages = "；".join(message for _, message in hub.cache.events)
    assert "自定义列表强股样本批量行情失败" in messages
    assert "600002.SH" in messages


def test_strong_stock_watch_rejects_custom_symbols_when_all_quotes_fail() -> None:
    hub = _OverviewHub(failing_symbols={"600001.SH", "600002.SH"})

    with pytest.raises(RuntimeError, match="自定义强股列表行情不可用") as exc_info:
        asyncio.run(strong_stock_watch(hub, Settings(), symbols="600001.SH,600002.SH"))

    assert "600001.SH" in str(exc_info.value)
    assert "600002.SH" in str(exc_info.value)


def test_strong_stock_watch_rejects_custom_symbols_when_provider_returns_other_quotes() -> None:
    hub = _WrongQuoteHub()

    with pytest.raises(RuntimeError, match="自定义强股列表行情不可用") as exc_info:
        asyncio.run(strong_stock_watch(hub, Settings(), symbols="000001.SZ"))

    assert "000001.SZ" in str(exc_info.value)


def test_strong_stock_watch_labels_default_scope_when_all_quotes_fail() -> None:
    hub = _OverviewHub(failing_symbols={"600001.SH"}, seed_symbols=("600001.SH",))

    result = asyncio.run(strong_stock_watch(hub, Settings(seed_symbols=("600001.SH",))))

    assert result["sample_count"] == 0
    assert result["items"] == []
    assert result["updated_at"] == ""
    assert result["requested_count"] == 1
    assert result["missing_count"] == 1
    assert result["degraded"] is True
    assert result["warnings"] == ["自选股 + 默认观察池 + 股票池分层抽样行情暂不可用，成功 0/1 个样本，请稍后重试。"]


def test_strong_stock_watch_offloads_watchlist_failure_and_uses_default_samples() -> None:
    hub = _OverviewHub(failing_symbols=set(), seed_symbols=("600001.SH",))
    hub.cache = _ThreadRecordingFailingWatchlistCache()

    async def run() -> tuple[dict[str, object], int]:
        event_loop_thread = threading.get_ident()
        return await strong_stock_watch(hub, Settings(seed_symbols=("600001.SH",))), event_loop_thread

    result, event_loop_thread = asyncio.run(run())

    assert hub.cache.watchlist_thread is not None
    assert hub.cache.watchlist_thread != event_loop_thread
    assert result["sample_count"] == 1
    assert result["degraded"] is True
    assert any("自选股读取暂不可用" in warning for warning in result["warnings"])


def test_strong_stock_watch_ignores_kline_failure_log_event_failure() -> None:
    hub = _KlineAndLogFailingOverviewHub(failing_symbols=set())

    result = asyncio.run(strong_stock_watch(hub, Settings(), symbols="600001.SH,600002.SH"))

    assert result["sample_count"] == 2
    assert result["items"] == []
    assert result["degraded"] is True
    assert result["warnings"] == ["强股排序所需日K线全部不可用，当前无法生成排序。"]


def test_strong_stock_watch_redacts_secrets_from_kline_failure_events() -> None:
    hub = _SensitiveKlineOverviewHub(failing_symbols=set())

    result = asyncio.run(strong_stock_watch(hub, Settings(), symbols="600001.SH"))

    assert result["items"] == []
    messages = "；".join(message for _, message in hub.cache.events)
    assert "private-token" not in messages
    assert "<redacted>" in messages


def test_market_overview_labels_independent_index_and_strong_stock_failures() -> None:
    failing_symbols = {"000001.SH", "399001.SZ", "399006.SZ", "600001.SH"}
    hub = _OverviewHub(failing_symbols=failing_symbols, seed_symbols=("600001.SH",))

    overview = asyncio.run(market_overview(hub, Settings(seed_symbols=("600001.SH",))))

    assert overview.indices == []
    assert overview.strong_stocks == []
    assert overview.degraded is True
    assert overview.index_meta.sample_count == 0
    assert overview.strong_stocks_meta.sample_count == 0
    assert overview.index_meta.warnings == ["市场指数样本行情暂不可用，成功 0/3 个样本，请稍后重试。"]
    assert overview.strong_stocks_meta.warnings == ["市场概览强股样本行情暂不可用，成功 0/1 个样本，请稍后重试。"]
    assert overview.warnings == [*overview.index_meta.warnings, *overview.strong_stocks_meta.warnings]


def test_strong_stock_watch_does_not_swallow_kline_cancellation() -> None:
    hub = _CancellingKlineOverviewHub(failing_symbols=set())

    with pytest.raises(asyncio.CancelledError):
        asyncio.run(strong_stock_watch(hub, Settings(), symbols="600001.SH"))


def test_strong_stock_watch_rejects_explicit_symbols_when_all_are_invalid() -> None:
    hub = _OverviewHub(failing_symbols=set())

    with pytest.raises(ValueError, match="至少输入一个有效股票代码"):
        asyncio.run(strong_stock_watch(hub, Settings(), symbols="bad,bad2"))


def test_strong_stock_watch_rejects_explicit_empty_symbol_list() -> None:
    hub = _OverviewHub(failing_symbols=set())

    with pytest.raises(ValueError, match="至少输入一个有效股票代码"):
        asyncio.run(strong_stock_watch(hub, Settings(), symbols=""))


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


class _LogFailingEventCache(_EventCache):
    def log_event(self, category: str, message: str) -> None:
        raise RuntimeError("event log down")


class _ThreadRecordingFailingWatchlistCache(_EventCache):
    def __init__(self) -> None:
        super().__init__()
        self.watchlist_thread: int | None = None

    def watchlist_symbols(self) -> list[str]:
        self.watchlist_thread = threading.get_ident()
        raise RuntimeError("watchlist database busy")


class _OverviewHub:
    def __init__(self, *, failing_symbols: set[str], seed_symbols: tuple[str, ...] = ()) -> None:
        self.cache = _EventCache()
        self.failing_symbols = failing_symbols
        self.settings = Settings(seed_symbols=seed_symbols)

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

    async def stock_pool(self, *args, **kwargs):
        return []

    async def kline(self, symbol: str, limit: int = 80):
        return [make_kline(date="2026-05-13", close=10 + index * 0.1, high=11 + index * 0.1, low=9 + index * 0.1) for index in range(limit)]


class _KlineAndLogFailingOverviewHub(_OverviewHub):
    def __init__(self, *, failing_symbols: set[str]) -> None:
        super().__init__(failing_symbols=failing_symbols)
        self.cache = _LogFailingEventCache()

    async def kline(self, symbol: str, limit: int = 80):
        raise RuntimeError("kline down")


class _CancellingKlineOverviewHub(_OverviewHub):
    async def kline(self, symbol: str, limit: int = 80):
        raise asyncio.CancelledError()


class _SensitiveKlineOverviewHub(_OverviewHub):
    async def kline(self, symbol: str, limit: int = 80):
        raise RuntimeError("GET https://example.test/kline?access_token=private-token")


class _WrongQuoteHub(_OverviewHub):
    def __init__(self) -> None:
        super().__init__(failing_symbols=set())

    async def quotes(self, symbols, use_cache: bool = True) -> list[Quote]:
        return [
            Quote(
                code="600519",
                name="贵州茅台",
                market="SH",
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
        ]


def _normalize_symbol(symbol: str) -> str:
    lowered = symbol.lower()
    if lowered.startswith("sh") and "." not in symbol:
        return f"{symbol[-6:]}.SH"
    if lowered.startswith("sz") and "." not in symbol:
        return f"{symbol[-6:]}.SZ"
    return standard_symbol(symbol)
