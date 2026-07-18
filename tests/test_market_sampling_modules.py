from __future__ import annotations

import asyncio
import threading

import pytest

from app.services.market_sampling import (
    MARKET_BREADTH_LIMIT,
    _fill_sample_to_limit,
    _seed_codes,
    dedupe_quotes,
    even_sample,
    fetch_quote_sample,
    fetch_quotes_with_single_fallback,
    industry_symbol_groups,
    market_breadth_quote_sample,
    market_breadth_symbols,
    peer_quote_sample,
    peer_quotes,
    peer_symbols,
    stratified_market_breadth_symbols,
    unique_standard_symbols,
)
from tests.factories import make_quote, make_stock_info


def test_seed_codes_normalizes_supported_symbol_forms_and_skips_invalid_values() -> None:
    assert _seed_codes(["600519", "600519.SH", "SZ000001", "bad"]) == {"600519", "000001"}


def test_unique_standard_symbols_sanitizes_custom_inputs_without_reordering() -> None:
    symbols = unique_standard_symbols([" SZ000001 ", "bad", None, "", "000001.SZ", "sh600519", "000000"])

    assert symbols == ["000001.SZ", "600519.SH"]


def test_stratified_market_breadth_symbols_excludes_seed_codes_from_sample_pool() -> None:
    pool = [
        make_stock_info(code="600519", market="SH"),
        make_stock_info(code="600000", market="SH"),
        make_stock_info(code="000001", market="SZ"),
        make_stock_info(code="000002", market="SZ"),
    ]

    symbols = stratified_market_breadth_symbols(pool, 3, ["600519.SH"])

    assert "600519.SH" not in symbols
    assert len(symbols) == 3
    assert any(item.endswith(".SH") for item in symbols)
    assert any(item.endswith(".SZ") for item in symbols)


def test_stratified_market_breadth_symbols_normalizes_pool_symbols_and_skips_mismatches() -> None:
    pool = [
        make_stock_info(code="600001", market="SH").model_copy(update={"symbol": "SH600001"}),
        make_stock_info(code="600002", market="SH").model_copy(update={"symbol": "600999.SH"}),
        make_stock_info(code="000001", market="SZ").model_copy(update={"symbol": "bad"}),
    ]

    symbols = stratified_market_breadth_symbols(pool, 5)

    assert symbols == ["600001.SH"]


def test_industry_symbol_groups_cleans_metadata_and_skips_invalid_industries() -> None:
    pool = [
        make_stock_info(code="600001", market="SH").model_copy(update={"symbol": "SH600001", "industry": " 白酒 "}),
        make_stock_info(code="600002", market="SH").model_copy(update={"symbol": "600002.SH", "market": "sh", "industry": "白酒"}),
        make_stock_info(code="600003", market="SH").model_copy(update={"symbol": "600003.SH", "industry": "nan"}),
        make_stock_info(code="600004", market="SH").model_copy(update={"symbol": "600004.SH", "industry": " "}),
    ]

    groups = industry_symbol_groups(pool)

    assert groups == {"白酒": ["600001.SH", "600002.SH"]}


def test_fill_sample_to_limit_dedupes_and_fills_from_remaining_candidates() -> None:
    filled = _fill_sample_to_limit(["600001.SH", "600001.SH"], ["600001.SH", "600002.SH", "600003.SH"], 3)

    assert filled == ["600001.SH", "600002.SH", "600003.SH"]


def test_even_sample_keeps_middle_for_single_item_and_fills_index_collisions() -> None:
    assert even_sample(["a", "b", "c", "d", "e"], 1) == ["c"]
    assert len(even_sample([f"s{index}" for index in range(7)], 5)) == 5
    assert len(set(even_sample([f"s{index}" for index in range(7)], 5))) == 5


def test_even_sample_dedupes_before_applying_limit() -> None:
    assert even_sample(["a", "a", "b"], 5) == ["a", "b"]


def test_dedupe_quotes_uses_normalized_symbol_keys_and_skips_invalid_quotes() -> None:
    quotes = [
        _quote_for("600001.SH"),
        _quote_for("600001.SH").model_copy(update={"market": "sh", "price": 11.0}),
        _quote_for("600002.SH").model_copy(update={"code": "000000"}),
    ]

    deduped = dedupe_quotes(quotes)

    assert len(deduped) == 1
    assert deduped[0].price == 11.0


def test_peer_symbols_filters_target_market_and_industry_before_sampling() -> None:
    profile = make_stock_info(code="600519", market="SH")
    profile = profile.model_copy(update={"industry": "白酒"})
    pool = [
        make_stock_info(code="600519", market="SH").model_copy(update={"industry": "白酒"}),
        make_stock_info(code="600000", market="SH").model_copy(update={"industry": "白酒"}),
        make_stock_info(code="000001", market="SZ").model_copy(update={"industry": "银行"}),
        make_stock_info(code="430001", market="BJ").model_copy(update={"industry": "白酒"}),
        make_stock_info(code="000002", market="SZ").model_copy(update={"industry": "白酒"}),
    ]

    symbols = peer_symbols(pool, profile, "600519.SH", limit=5)

    assert symbols == ["000002.SZ", "600000.SH"]


def test_peer_symbols_excludes_target_profile_code_even_with_alias_target_symbol() -> None:
    profile = make_stock_info(code="600519", market="SH").model_copy(update={"industry": "白酒"})
    pool = [
        make_stock_info(code="600519", market="SH").model_copy(update={"industry": "白酒"}),
        make_stock_info(code="600000", market="SH").model_copy(update={"industry": "白酒"}),
    ]

    symbols = peer_symbols(pool, profile, "sh600519", limit=5)

    assert symbols == ["600000.SH"]


def test_peer_symbols_cleans_profile_and_pool_metadata_before_matching() -> None:
    profile = make_stock_info(code="600519", market="SH").model_copy(update={"code": " 600519 ", "industry": " 白酒 "})
    pool = [
        make_stock_info(code="600519", market="SH").model_copy(update={"industry": "白酒"}),
        make_stock_info(code="600000", market="SH").model_copy(update={"symbol": "SH600000", "market": "sh", "industry": "白酒 "}),
        make_stock_info(code="600001", market="SH").model_copy(update={"industry": "nan"}),
    ]

    symbols = peer_symbols(pool, profile, "bad-target", limit=5)

    assert symbols == ["600000.SH"]


def test_market_breadth_symbols_logs_stock_pool_failure_and_keeps_seed_symbols() -> None:
    hub = _FailingStockPoolHub(seed_symbols=["600519.SH"])

    symbols = asyncio.run(market_breadth_symbols(hub))

    assert symbols == ["600519.SH"]
    assert "市场宽度股票池不可用" in hub.cache.events[0][1]


def test_market_breadth_symbols_ignores_log_event_failure_when_stock_pool_is_down() -> None:
    hub = _LogFailingStockPoolHub(seed_symbols=["600519.SH"])

    symbols = asyncio.run(market_breadth_symbols(hub))

    assert symbols == ["600519.SH"]


def test_market_breadth_symbols_normalizes_and_dedupes_seed_symbols_before_sampling() -> None:
    hub = _FailingStockPoolHub(seed_symbols=["600519", "600519.SH", "bad", "SZ000001"])

    symbols = asyncio.run(market_breadth_symbols(hub))

    assert symbols == ["600519.SH", "000001.SZ"]


def test_market_breadth_symbols_caps_seed_only_sample_in_stable_order() -> None:
    seed_symbols = [f"600{index:03d}.SH" for index in range(MARKET_BREADTH_LIMIT + 5)]
    hub = _FailingStockPoolHub(seed_symbols=seed_symbols)

    symbols = asyncio.run(market_breadth_symbols(hub))

    assert symbols == seed_symbols[:MARKET_BREADTH_LIMIT]


def test_fetch_quotes_with_single_fallback_normalizes_symbol_sample_and_logs_skipped_values() -> None:
    hub = _QuoteHub()

    quotes = asyncio.run(
        fetch_quotes_with_single_fallback(
            hub,
            ["600519", "600519.SH", "bad", "SZ000001"],
            batch_size=10,
            context="测试样本",
        )
    )

    assert [f"{item.code}.{item.market}" for item in quotes] == ["600519.SH", "000001.SZ"]
    assert hub.requested_symbols == [["600519.SH", "000001.SZ"]]
    assert "测试样本剔除 2 个重复或无效样本" in hub.cache.events[0][1]


def test_fetch_quotes_with_single_fallback_ignores_log_event_failure() -> None:
    hub = _LogFailingQuoteHub()

    quotes = asyncio.run(
        fetch_quotes_with_single_fallback(
            hub,
            ["600519", "600519.SH", "bad"],
            batch_size=10,
            context="测试样本",
        )
    )

    assert [f"{item.code}.{item.market}" for item in quotes] == ["600519.SH"]
    assert hub.requested_symbols == [["600519.SH"]]


def test_sampling_event_writes_run_outside_the_event_loop_thread() -> None:
    hub = _QuoteHub()
    hub.cache = _ThreadRecordingEventCache()

    async def run() -> int:
        event_loop_thread = threading.get_ident()
        await fetch_quotes_with_single_fallback(hub, ["600519", "600519.SH", "bad"], context="测试样本")
        return event_loop_thread

    event_loop_thread = asyncio.run(run())

    assert hub.cache.thread_ids
    assert all(thread_id != event_loop_thread for thread_id in hub.cache.thread_ids)


def test_fetch_quotes_with_single_fallback_filters_unrequested_quotes_and_preserves_order() -> None:
    hub = _ShuffledQuoteHub()

    quotes = asyncio.run(
        fetch_quotes_with_single_fallback(
            hub,
            ["600001.SH", "600002.SH"],
            batch_size=2,
            context="测试样本",
        )
    )

    assert [f"{item.code}.{item.market}" for item in quotes] == ["600001.SH", "600002.SH"]
    assert hub.cache.events == []


def test_fetch_quotes_with_single_fallback_logs_batch_and_single_failures() -> None:
    hub = _BatchFailingQuoteHub(failing_symbol="600002.SH")

    quotes = asyncio.run(
        fetch_quotes_with_single_fallback(
            hub,
            ["600001.SH", "600002.SH"],
            batch_size=2,
            context="测试样本",
        )
    )

    assert [f"{item.code}.{item.market}" for item in quotes] == ["600001.SH"]
    assert hub.requested_symbols == [["600001.SH", "600002.SH"], ["600001.SH"], ["600002.SH"]]
    messages = "；".join(message for _, message in hub.cache.events)
    assert "测试样本批量行情失败，改为逐只重试" in messages
    assert "测试样本单只行情失败：600002.SH" in messages
    assert "测试样本最终缺失 1 / 2 个样本" in messages


def test_fetch_quotes_with_single_fallback_keeps_empty_result_when_all_quotes_fail() -> None:
    hub = _AllFailingQuoteHub()

    quotes = asyncio.run(
        fetch_quotes_with_single_fallback(
            hub,
            ["600001.SH", "600002.SH"],
            batch_size=2,
            context="测试样本",
        )
    )

    assert quotes == []
    assert hub.requested_symbols == [["600001.SH", "600002.SH"], ["600001.SH"], ["600002.SH"]]
    messages = "；".join(message for _, message in hub.cache.events)
    assert "测试样本批量行情失败，改为逐只重试" in messages
    assert "测试样本单只行情失败：600001.SH" in messages
    assert "测试样本单只行情失败：600002.SH" in messages
    assert "测试样本最终缺失 2 / 2 个样本" in messages


def test_fetch_quote_sample_exposes_partial_failure_status_without_losing_available_quotes() -> None:
    hub = _BatchFailingQuoteHub(failing_symbol="600002.SH")

    result = asyncio.run(
        fetch_quote_sample(
            hub,
            ["600001.SH", "600002.SH"],
            batch_size=2,
            context="测试样本",
        )
    )

    assert [f"{item.code}.{item.market}" for item in result.quotes] == ["600001.SH"]
    assert result.requested_symbols == ("600001.SH", "600002.SH")
    assert result.missing_symbols == ("600002.SH",)
    assert result.requested_count == 2
    assert result.sample_count == 1
    assert result.fallback_batch_count == 1
    assert result.degraded is True
    assert result.unavailable is False


def test_fetch_quote_sample_marks_all_requested_quotes_unavailable() -> None:
    result = asyncio.run(
        fetch_quote_sample(
            _AllFailingQuoteHub(),
            ["600001.SH", "600002.SH"],
            batch_size=2,
            context="测试样本",
        )
    )

    assert result.quotes == ()
    assert result.missing_symbols == ("600001.SH", "600002.SH")
    assert result.degraded is True
    assert result.unavailable is True


def test_fetch_quotes_with_single_fallback_retries_missing_batch_symbols_and_logs_wrong_single_return() -> None:
    hub = _MissingThenWrongQuoteHub()

    quotes = asyncio.run(
        fetch_quotes_with_single_fallback(
            hub,
            ["600001.SH", "600002.SH"],
            batch_size=2,
            context="测试样本",
        )
    )

    assert [f"{item.code}.{item.market}" for item in quotes] == ["600001.SH"]
    assert hub.requested_symbols == [["600001.SH", "600002.SH"], ["600002.SH"]]
    messages = "；".join(message for _, message in hub.cache.events)
    assert "测试样本批量行情缺失 1 个样本，改为逐只补齐：600002.SH" in messages
    assert "测试样本单只行情未返回请求符号：600002.SH" in messages
    assert "测试样本最终缺失 1 / 2 个样本" in messages


def test_fetch_quotes_with_single_fallback_does_not_swallow_cancelled_error() -> None:
    hub = _CancellingQuoteHub()

    with pytest.raises(asyncio.CancelledError):
        asyncio.run(fetch_quotes_with_single_fallback(hub, ["600519.SH"], context="测试样本"))


def test_peer_quote_sample_labels_stock_pool_failure() -> None:
    hub = _FailingStockPoolHub(seed_symbols=[])
    profile = make_stock_info(code="600519", market="SH").model_copy(update={"industry": "白酒"})

    result = asyncio.run(peer_quote_sample(hub, profile, "600519.SH"))

    assert result.quotes == ()
    assert result.status == "unavailable"
    assert result.warning == "白酒同行股票池暂不可用。"
    assert "白酒同行股票池不可用" in hub.cache.events[0][1]


def test_peer_quote_sample_keeps_partial_quotes_and_missing_count() -> None:
    hub = _PeerSampleHub(failing_symbol="600002.SH")
    profile = make_stock_info(code="600519", market="SH").model_copy(update={"industry": "白酒"})

    result = asyncio.run(peer_quote_sample(hub, profile, "600519.SH"))

    assert [f"{item.code}.{item.market}" for item in result.quotes] == ["600001.SH"]
    assert result.status == "degraded"
    assert result.requested_count == 2
    assert result.missing_count == 1
    assert result.warning == "白酒同行行情样本部分缺失，成功 1/2 个。"


def test_peer_quotes_compatibility_wrapper_returns_sample_quotes() -> None:
    hub = _PeerSampleHub(failing_symbol="600002.SH")
    profile = make_stock_info(code="600519", market="SH").model_copy(update={"industry": "白酒"})

    quotes = asyncio.run(peer_quotes(hub, profile, "600519.SH"))

    assert [f"{item.code}.{item.market}" for item in quotes] == ["600001.SH"]


def test_market_breadth_symbols_does_not_swallow_cancelled_error() -> None:
    hub = _CancellingStockPoolHub(seed_symbols=[])

    with pytest.raises(asyncio.CancelledError):
        asyncio.run(market_breadth_symbols(hub))


def test_market_breadth_quote_sample_labels_stock_pool_fallback_even_when_seed_quote_succeeds() -> None:
    hub = _FailingStockPoolHub(seed_symbols=["600519.SH"])

    result = asyncio.run(market_breadth_quote_sample(hub))

    assert [f"{item.code}.{item.market}" for item in result.quotes] == ["600519.SH"]
    assert result.warnings == ("市场宽度股票池暂不可用，当前仅使用默认观察样本。",)


class _EventCache:
    def __init__(self) -> None:
        self.events: list[tuple[str, str]] = []

    def log_event(self, category: str, message: str) -> None:
        self.events.append((category, message))


class _FailingEventCache:
    def log_event(self, category: str, message: str) -> None:
        raise RuntimeError("cache log down")


class _ThreadRecordingEventCache(_EventCache):
    def __init__(self) -> None:
        super().__init__()
        self.thread_ids: list[int] = []

    def log_event(self, category: str, message: str) -> None:
        self.thread_ids.append(threading.get_ident())
        super().log_event(category, message)


class _Settings:
    def __init__(self, seed_symbols: list[str]) -> None:
        self.seed_symbols = seed_symbols


class _FailingStockPoolHub:
    def __init__(self, *, seed_symbols: list[str]) -> None:
        self.settings = _Settings(seed_symbols)
        self.cache = _EventCache()

    async def stock_pool(self, limit: int = 1200, refresh: bool = False):
        raise RuntimeError("stock pool down")

    async def quotes(self, symbols, use_cache: bool = True):
        return [_quote_for(symbol) for symbol in symbols]


class _LogFailingStockPoolHub(_FailingStockPoolHub):
    def __init__(self, *, seed_symbols: list[str]) -> None:
        super().__init__(seed_symbols=seed_symbols)
        self.cache = _FailingEventCache()


class _QuoteHub:
    def __init__(self) -> None:
        self.cache = _EventCache()
        self.requested_symbols: list[list[str]] = []

    async def quotes(self, symbols, use_cache: bool = True):
        normalized = list(symbols)
        self.requested_symbols.append(normalized)
        result = []
        for symbol in normalized:
            result.append(_quote_for(symbol))
        return result


class _LogFailingQuoteHub(_QuoteHub):
    def __init__(self) -> None:
        super().__init__()
        self.cache = _FailingEventCache()


class _ShuffledQuoteHub:
    def __init__(self) -> None:
        self.cache = _EventCache()

    async def quotes(self, symbols, use_cache: bool = True):
        return [_quote_for("000001.SZ"), _quote_for("600002.SH"), _quote_for("600001.SH")]


class _BatchFailingQuoteHub:
    def __init__(self, *, failing_symbol: str) -> None:
        self.cache = _EventCache()
        self.failing_symbol = failing_symbol
        self.requested_symbols: list[list[str]] = []

    async def quotes(self, symbols, use_cache: bool = True):
        normalized = list(symbols)
        self.requested_symbols.append(normalized)
        if len(normalized) > 1:
            raise RuntimeError("batch failed")
        if normalized[0] == self.failing_symbol:
            raise RuntimeError("single failed")
        return [_quote_for(normalized[0])]


class _PeerSampleHub(_BatchFailingQuoteHub):
    async def stock_pool(self, limit: int = 1200, refresh: bool = False):
        return [
            make_stock_info(code="600001", market="SH").model_copy(update={"industry": "白酒"}),
            make_stock_info(code="600002", market="SH").model_copy(update={"industry": "白酒"}),
        ]


class _AllFailingQuoteHub:
    def __init__(self) -> None:
        self.cache = _EventCache()
        self.requested_symbols: list[list[str]] = []

    async def quotes(self, symbols, use_cache: bool = True):
        normalized = list(symbols)
        self.requested_symbols.append(normalized)
        raise RuntimeError("quote source down")


class _MissingThenWrongQuoteHub:
    def __init__(self) -> None:
        self.cache = _EventCache()
        self.requested_symbols: list[list[str]] = []

    async def quotes(self, symbols, use_cache: bool = True):
        normalized = list(symbols)
        self.requested_symbols.append(normalized)
        if len(normalized) > 1:
            return [_quote_for("600001.SH")]
        return [_quote_for("000001.SZ")]


class _CancellingQuoteHub:
    def __init__(self) -> None:
        self.cache = _EventCache()

    async def quotes(self, symbols, use_cache: bool = True):
        raise asyncio.CancelledError


class _CancellingStockPoolHub(_FailingStockPoolHub):
    async def stock_pool(self, limit: int = 1200, refresh: bool = False):
        raise asyncio.CancelledError


def _quote_for(symbol: str):
    code, market = symbol.split(".")
    return make_quote().model_copy(update={"code": code, "market": market, "name": f"测试{symbol}"})
