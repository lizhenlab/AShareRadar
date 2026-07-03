from __future__ import annotations

import asyncio

from app.models.schemas import ProviderCapability
from app.workflows.workbench_pipeline import _order_book_or_error


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


def _capability(*, enabled: bool) -> ProviderCapability:
    return ProviderCapability(
        name="futu",
        installed=True,
        enabled=enabled,
        order_book=enabled,
        note="测试盘口能力",
    )
