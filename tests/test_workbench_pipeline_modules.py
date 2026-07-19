from __future__ import annotations

import asyncio
import threading
from types import SimpleNamespace

import pytest

from app.models.schemas import ProviderCapability
from app.workflows.workbench_pipeline import _market_breadth_sample_or_empty, _order_book_or_error, _stock_concepts_or_error


def test_order_book_or_error_reports_disabled_futu_without_provider_call() -> None:
    class DisabledFutuProvider:
        def capability(self) -> ProviderCapability:
            return _capability(enabled=False)

    class DataHubStub:
        providers = {"futu": DisabledFutuProvider()}

        async def order_book(self, symbol: str):
            raise AssertionError("disabled Futu should skip order_book")

    order_book, error = asyncio.run(_order_book_or_error(DataHubStub(), "600519"))  # type: ignore[arg-type]

    assert order_book is None
    assert error == "Futu OpenAPI 未启用，盘口压力使用行情区间估算。"


def test_order_book_or_error_uses_readable_error_for_empty_exception() -> None:
    class EnabledFutuProvider:
        def capability(self) -> ProviderCapability:
            return _capability(enabled=True)

    class DataHubStub:
        providers = {"futu": EnabledFutuProvider()}

        async def order_book(self, symbol: str):
            raise TimeoutError()

    order_book, error = asyncio.run(_order_book_or_error(DataHubStub(), "600519"))  # type: ignore[arg-type]

    assert order_book is None
    assert error == "TimeoutError: 数据源响应超时"


def test_order_book_or_error_handles_futu_capability_failure_as_degraded_order_book() -> None:
    class BrokenCapabilityProvider:
        def capability(self) -> ProviderCapability:
            raise RuntimeError("capability probe down")

    class DataHubStub:
        providers = {"futu": BrokenCapabilityProvider()}

        async def order_book(self, symbol: str):
            raise AssertionError("capability failure should skip order_book")

    order_book, error = asyncio.run(_order_book_or_error(DataHubStub(), "600519"))  # type: ignore[arg-type]

    assert order_book is None
    assert error == "capability probe down"


def test_stock_concepts_or_error_keeps_workbench_renderable_on_source_failure() -> None:
    class DataHubStub:
        async def stock_concepts(self, symbol: str, limit: int = 8):
            raise RuntimeError("概念归属不可用：600706.SH；akshare: concept down")

    concepts, error = asyncio.run(_stock_concepts_or_error(DataHubStub(), "600706"))  # type: ignore[arg-type]

    assert concepts == []
    assert error == "概念归属不可用：600706.SH；akshare: concept down"


def test_stock_concepts_or_error_times_out_as_optional_data() -> None:
    class SettingsStub:
        workbench_optional_timeout_seconds = 0.01

    class DataHubStub:
        settings = SettingsStub()

        async def stock_concepts(self, symbol: str, limit: int = 8):
            await asyncio.sleep(1)
            return []

    concepts, error = asyncio.run(_stock_concepts_or_error(DataHubStub(), "600706"))  # type: ignore[arg-type]

    assert concepts == []
    assert error == "TimeoutError: 数据源响应超时"


def test_stale_concept_cache_is_withheld_from_theme_and_leader_scoring() -> None:
    class DataHubStub:
        async def stock_concepts_result(self, symbol: str, limit: int = 8):
            return SimpleNamespace(rows=[object()], used_fallback_cache=True)

    concepts, error = asyncio.run(_stock_concepts_or_error(DataHubStub(), "600706"))  # type: ignore[arg-type]

    assert concepts == []
    assert error == "概念数据源不可用，过期缓存不参与主题与龙头强度评分。"


def test_market_breadth_sample_preserves_all_quote_failure_as_warning() -> None:
    cache = _EventCache()

    class SettingsStub:
        seed_symbols = ("600519.SH",)
        workbench_optional_timeout_seconds = 0.5

    class DataHubStub:
        settings = SettingsStub()

        async def stock_pool(self, limit: int, refresh: bool):
            return []

        async def quotes(self, symbols):
            raise RuntimeError("provider secret detail")

    hub = DataHubStub()
    hub.cache = cache

    result = asyncio.run(_market_breadth_sample_or_empty(hub))  # type: ignore[arg-type]

    assert result.quotes == ()
    assert result.quote_sample.requested_count == 1
    assert result.quote_sample.unavailable is True
    assert result.warnings == ("市场宽度行情样本暂不可用，环境判断已降级。",)
    assert "provider secret detail" not in " ".join(result.warnings)


def test_market_breadth_sample_timeout_returns_stable_user_warning() -> None:
    cache = _EventCache()

    class SettingsStub:
        seed_symbols = ("600519.SH",)
        workbench_optional_timeout_seconds = 0.01

    class DataHubStub:
        settings = SettingsStub()

        async def stock_pool(self, limit: int, refresh: bool):
            await asyncio.sleep(1)
            return []

    hub = DataHubStub()
    hub.cache = cache

    async def run_check():
        event_loop_thread = threading.get_ident()
        result = await _market_breadth_sample_or_empty(hub)  # type: ignore[arg-type]
        return result, event_loop_thread

    result, event_loop_thread = asyncio.run(run_check())

    assert result.quotes == ()
    assert result.warnings == ("市场宽度数据源请求失败，环境判断已降级。",)
    assert any("TimeoutError" in message for _, message in cache.events)
    assert cache.io_threads
    assert all(thread_id != event_loop_thread for thread_id in cache.io_threads)


def test_market_breadth_cancellation_propagates_without_fallback_log() -> None:
    cache = _EventCache()

    class SettingsStub:
        seed_symbols = ("600519.SH",)
        workbench_optional_timeout_seconds = 0.5

    class DataHubStub:
        settings = SettingsStub()

        async def stock_pool(self, limit: int, refresh: bool):
            raise asyncio.CancelledError()

    hub = DataHubStub()
    hub.cache = cache

    with pytest.raises(asyncio.CancelledError):
        asyncio.run(_market_breadth_sample_or_empty(hub))  # type: ignore[arg-type]

    assert cache.events == []


def _capability(*, enabled: bool) -> ProviderCapability:
    return ProviderCapability(
        name="futu",
        installed=True,
        enabled=enabled,
        order_book=enabled,
        note="测试盘口能力",
    )


class _EventCache:
    def __init__(self) -> None:
        self.events: list[tuple[str, str]] = []
        self.io_threads: list[int] = []

    def log_event(self, category: str, message: str) -> None:
        self.io_threads.append(threading.get_ident())
        self.events.append((category, message))
