from __future__ import annotations

from collections.abc import Callable
import time

from app.models.schemas import OrderBook
from app.services.datahub_runtime import ProviderRuntime
from app.services.datahub_status import _provider_error_text
from app.services.provider_errors import sanitize_provider_error
from app.utils.symbols import standard_symbol


FUTU_UNAVAILABLE_MESSAGE = "Futu OpenAPI 未启用。设置 ASHARE_RADAR_FUTU_ENABLED=1 并启动 Futu OpenD 后再使用"


class OrderBookCoordinator:
    def __init__(
        self,
        *,
        providers: dict,
        runtime: ProviderRuntime,
        provider_index: Callable[[str], int],
    ) -> None:
        self.providers = providers
        self.runtime = runtime
        self.provider_index = provider_index

    async def order_book(self, symbol: str) -> OrderBook:
        provider = self._futu_provider()
        if provider is None or not hasattr(provider, "order_book"):
            raise RuntimeError("当前没有可用盘口数据源")
        self._ensure_futu_enabled(provider)
        if self.runtime.is_cooling("futu", "order_book"):
            raise RuntimeError("Futu 盘口最近失败，短暂冷却中")
        started = time.perf_counter()
        try:
            result = await self.runtime.call_provider(
                "futu",
                "order_book",
                lambda: provider.order_book(symbol),  # type: ignore[attr-defined]
                request_key=("order_book", standard_symbol(symbol)),
            )
            latency_ms = (time.perf_counter() - started) * 1000
            await self.runtime.record_success_async(
                "futu",
                self.provider_index("futu"),
                round(latency_ms, 2),
                "order_book",
            )
            return result
        except Exception as exc:
            await self.runtime.record_failure_async("futu", self.provider_index("futu"), exc, "order_book")
            raise RuntimeError(sanitize_provider_error(_provider_error_text(exc))) from exc

    async def futu_ping(self) -> dict[str, object]:
        provider = self._futu_provider()
        if provider is None or not hasattr(provider, "ping"):
            return {"ok": False, "message": "当前没有可用 Futu 数据源", "latency_ms": None}
        try:
            self._ensure_futu_enabled(provider)
        except RuntimeError as exc:
            return {"ok": False, "message": str(exc), "latency_ms": None}
        started = time.perf_counter()
        try:
            message = await self.runtime.call_provider(
                "futu",
                "order_book",
                lambda: provider.ping(),  # type: ignore[attr-defined]
                request_key=("ping",),
            )
            latency_ms = round((time.perf_counter() - started) * 1000, 2)
            await self.runtime.record_success_async("futu", self.provider_index("futu"), latency_ms, "order_book")
            return {"ok": True, "message": message, "latency_ms": latency_ms}
        except Exception as exc:
            await self.runtime.record_failure_async("futu", self.provider_index("futu"), exc, "order_book")
            return {"ok": False, "message": sanitize_provider_error(exc), "latency_ms": None}

    def _futu_provider(self):
        return self.providers.get("futu")

    @staticmethod
    def _ensure_futu_enabled(provider) -> None:
        capability = getattr(provider, "capability", None)
        if not callable(capability):
            return
        status = capability()
        if not status.enabled or not status.order_book:
            raise RuntimeError(FUTU_UNAVAILABLE_MESSAGE)
