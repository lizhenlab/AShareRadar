from __future__ import annotations

import asyncio
import threading

import pytest

from app.workflows.stock_analysis import (
    _peer_quote_sample_or_fallback,
    _peer_sample_info,
    _safe_quote_history,
    _safe_save_advice_snapshot,
    analyze_individual_stock,
)
from tests.factories import make_quote, make_stock_info


def test_peer_sample_stock_pool_failure_reaches_analysis_contract() -> None:
    hub = _PeerHub(stock_pool_delay=0)
    hub.fail_stock_pool = True
    profile = make_stock_info().model_copy(update={"industry": "白酒"})

    sample = asyncio.run(_peer_quote_sample_or_fallback(hub, profile, "600519.SH"))  # type: ignore[arg-type]
    info = _peer_sample_info(sample)

    assert info.status == "unavailable"
    assert info.warning == "白酒同行股票池暂不可用。"
    assert any("白酒同行股票池不可用" in message for _, message in hub.cache.events)


def test_peer_sample_timeout_uses_stable_warning_without_internal_error_text() -> None:
    hub = _PeerHub(stock_pool_delay=1)
    hub.settings.workbench_optional_timeout_seconds = 0.01
    profile = make_stock_info().model_copy(update={"industry": "白酒"})

    sample = asyncio.run(_peer_quote_sample_or_fallback(hub, profile, "600519.SH"))  # type: ignore[arg-type]
    info = _peer_sample_info(sample)

    assert info.status == "unavailable"
    assert info.warning == "同行样本请求失败，当前仅使用个股历史和行业背景。"
    assert "TimeoutError" not in info.warning
    assert any("TimeoutError" in message for _, message in hub.cache.events)


def test_analysis_cache_failures_and_logs_run_off_event_loop_thread() -> None:
    class FailingAnalysisCache:
        def __init__(self) -> None:
            self.calls: list[tuple[str, int]] = []
            self.events: list[tuple[str, str]] = []

        def quote_history(self, symbol: str, *, limit: int):
            self.calls.append(("quote_history", threading.get_ident()))
            raise RuntimeError("history db unavailable")

        def save_advice_snapshot(self, analysis: object) -> None:
            self.calls.append(("save_advice_snapshot", threading.get_ident()))
            raise RuntimeError("advice db unavailable")

        def log_event(self, category: str, message: str) -> None:
            self.calls.append(("log_event", threading.get_ident()))
            self.events.append((category, message))
            raise RuntimeError("event log unavailable")

    async def run_check():
        cache = FailingAnalysisCache()
        hub = type("Hub", (), {"cache": cache})()
        event_loop_thread = threading.get_ident()
        history = await _safe_quote_history(hub, "600519.SH")  # type: ignore[arg-type]
        await _safe_save_advice_snapshot(hub, object(), "600519.SH")  # type: ignore[arg-type]
        return cache, history, event_loop_thread

    cache, history, event_loop_thread = asyncio.run(run_check())

    assert history == []
    assert [operation for operation, _thread_id in cache.calls] == [
        "quote_history",
        "log_event",
        "save_advice_snapshot",
        "log_event",
    ]
    assert all(thread_id != event_loop_thread for _operation, thread_id in cache.calls)
    assert len(cache.events) == 2


def test_analysis_required_child_cancellation_propagates() -> None:
    class CancellingHub:
        settings = _Settings()

        def __init__(self) -> None:
            self.kline_limits: list[int] = []

        async def stock_profile(self, symbol: str):
            return make_stock_info()

        async def quote(self, symbol: str):
            raise asyncio.CancelledError()

        async def kline(self, symbol: str, limit: int):
            self.kline_limits.append(limit)
            return []

        async def plate_rank(self, limit: int):
            return []

    hub = CancellingHub()
    with pytest.raises(asyncio.CancelledError):
        asyncio.run(analyze_individual_stock(hub, "600519"))  # type: ignore[arg-type]

    assert hub.kline_limits == [240]


def test_analysis_required_kline_failure_propagates_after_requesting_240_rows() -> None:
    class FailingKlineHub:
        settings = _Settings()

        def __init__(self) -> None:
            self.kline_limits: list[int] = []

        async def stock_profile(self, symbol: str):
            return make_stock_info()

        async def quote(self, symbol: str):
            return make_quote()

        async def kline(self, symbol: str, limit: int):
            self.kline_limits.append(limit)
            raise RuntimeError("daily kline unavailable")

        async def plate_rank(self, limit: int):
            return []

    hub = FailingKlineHub()
    with pytest.raises(RuntimeError, match="daily kline unavailable"):
        asyncio.run(analyze_individual_stock(hub, "600519"))  # type: ignore[arg-type]

    assert hub.kline_limits == [240]


class _Settings:
    seed_symbols: tuple[str, ...] = ()
    workbench_optional_timeout_seconds = 0.5


class _EventCache:
    def __init__(self) -> None:
        self.events: list[tuple[str, str]] = []

    def log_event(self, category: str, message: str) -> None:
        self.events.append((category, message))


class _PeerHub:
    def __init__(self, *, stock_pool_delay: float) -> None:
        self.settings = _Settings()
        self.cache = _EventCache()
        self.stock_pool_delay = stock_pool_delay
        self.fail_stock_pool = False

    async def stock_pool(self, limit: int = 1200, refresh: bool = False):
        if self.stock_pool_delay:
            await asyncio.sleep(self.stock_pool_delay)
        if self.fail_stock_pool:
            raise RuntimeError("private provider detail")
        return []
