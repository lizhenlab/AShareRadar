from __future__ import annotations

import asyncio
from pathlib import Path
from tempfile import TemporaryDirectory
import threading

from app.config import Settings
from app.models.schemas import OrderBook, OrderBookLevel, ProviderCapability
from app.services.cache import SQLiteCache
from app.services.datahub_orderbook import OrderBookCoordinator
from app.services.datahub_runtime import ProviderRuntime


def test_order_book_timeout_is_wrapped_and_records_capability_failure() -> None:
    class TimeoutFutuProvider:
        async def order_book(self, symbol: str):
            raise asyncio.TimeoutError()

        def capability(self) -> ProviderCapability:
            return ProviderCapability(
                name="futu",
                installed=True,
                enabled=True,
                order_book=True,
                note="test futu",
            )

    async def run_check(path: Path) -> tuple[str, int, bool]:
        settings = Settings(provider_failure_cooldown_seconds=60)
        cache = SQLiteCache(path)
        runtime = ProviderRuntime(cache, settings)
        coordinator = OrderBookCoordinator(
            providers={"futu": TimeoutFutuProvider()},
            runtime=runtime,
            provider_index=lambda name: 6,
        )
        try:
            await coordinator.order_book("600519.SH")
        except Exception as exc:
            message = str(exc)
        else:
            message = "ok"
        status = next(item for item in cache.provider_capability_statuses() if item.name == "futu" and item.kind == "order_book")
        return message, status.failure_count, runtime.is_cooling("futu", "order_book")

    with TemporaryDirectory() as tmpdir:
        message, failure_count, cooling = asyncio.run(run_check(Path(tmpdir) / "cache.sqlite3"))

    assert message == "TimeoutError: 数据源响应超时"
    assert failure_count == 1
    assert cooling is True


def test_order_book_coordinator_offloads_runtime_status_writes_from_event_loop_thread() -> None:
    class ThreadTrackingOrderBookCache(SQLiteCache):
        def __init__(self, path: Path) -> None:
            super().__init__(path)
            self.io_threads: dict[str, set[int]] = {}

        def _track(self, operation: str) -> None:
            self.io_threads.setdefault(operation, set()).add(threading.get_ident())

        def update_provider_capability_success(
            self,
            name: str,
            kind: str,
            priority: int,
            latency_ms: float,
        ) -> None:
            self._track("provider_success")
            super().update_provider_capability_success(name, kind, priority, latency_ms)

        def update_provider_capability_failure(
            self,
            name: str,
            kind: str,
            priority: int,
            error: str,
        ) -> None:
            self._track("provider_failure")
            super().update_provider_capability_failure(name, kind, priority, error)

    class MixedFutuProvider:
        async def order_book(self, symbol: str) -> OrderBook:
            return OrderBook(
                symbol="600519.SH",
                code="600519",
                market="SH",
                bid=[OrderBookLevel(price=100.0, volume=10.0)],
                ask=[OrderBookLevel(price=100.1, volume=8.0)],
                source="Futu OpenAPI",
                updated_at="2026-07-14 10:00:00",
            )

        async def ping(self) -> str:
            raise RuntimeError("OpenD down")

        def capability(self) -> ProviderCapability:
            return ProviderCapability(
                name="futu",
                installed=True,
                enabled=True,
                order_book=True,
                note="test futu",
            )

    async def run_check(path: Path) -> tuple[str, dict[str, object], dict[str, set[int]], int]:
        settings = Settings(provider_failure_cooldown_seconds=60)
        cache = ThreadTrackingOrderBookCache(path)
        coordinator = OrderBookCoordinator(
            providers={"futu": MixedFutuProvider()},
            runtime=ProviderRuntime(cache, settings),
            provider_index=lambda name: 6,
        )
        event_loop_thread = threading.get_ident()

        book = await coordinator.order_book("600519.SH")
        ping = await coordinator.futu_ping()
        return book.symbol, ping, cache.io_threads, event_loop_thread

    with TemporaryDirectory() as tmpdir:
        symbol, ping, io_threads, event_loop_thread = asyncio.run(run_check(Path(tmpdir) / "cache.sqlite3"))

    assert symbol == "600519.SH"
    assert ping == {"ok": False, "message": "OpenD down", "latency_ms": None}
    assert {"provider_success", "provider_failure"} <= io_threads.keys()
    assert all(event_loop_thread not in thread_ids for thread_ids in io_threads.values())


def test_futu_status_redacts_credentials_from_provider_errors() -> None:
    class FailingFutuProvider:
        async def ping(self) -> str:
            raise RuntimeError("OpenD down api_key=secret-key")

        def capability(self) -> ProviderCapability:
            return ProviderCapability(name="futu", installed=True, enabled=True, order_book=True, note="test")

    async def run_check(path: Path) -> dict[str, object]:
        settings = Settings(provider_failure_cooldown_seconds=60)
        cache = SQLiteCache(path)
        coordinator = OrderBookCoordinator(
            providers={"futu": FailingFutuProvider()},
            runtime=ProviderRuntime(cache, settings),
            provider_index=lambda _name: 6,
        )
        return await coordinator.futu_ping()

    with TemporaryDirectory() as tmpdir:
        result = asyncio.run(run_check(Path(tmpdir) / "cache.sqlite3"))

    assert result["message"] == "OpenD down api_key=<redacted>"
