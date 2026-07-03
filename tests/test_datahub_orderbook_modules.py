from __future__ import annotations

import asyncio
from pathlib import Path
from tempfile import TemporaryDirectory

from app.config import Settings
from app.models.schemas import ProviderCapability
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
