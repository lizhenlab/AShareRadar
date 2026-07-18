from __future__ import annotations

import asyncio
import threading
from unittest.mock import patch

import pytest

from app.services.baostock_provider import BaoStockProvider
from app.services.tushare_provider import TushareProvider


class _FakeResult:
    error_code = "0"
    error_msg = ""

    def __init__(self, rows: list[list[str]], fields: list[str] | None = None) -> None:
        self.rows = rows
        self.fields = fields or []
        self.index = -1

    def next(self) -> bool:
        self.index += 1
        return self.index < len(self.rows)

    def get_row_data(self) -> list[str]:
        return self.rows[self.index]


class _FakeLoginResult:
    error_code = "0"
    error_msg = ""


class _ControlledBaoStock:
    def __init__(self) -> None:
        self.history_started = threading.Event()
        self.release_history = threading.Event()
        self._counter_lock = threading.Lock()
        self.active_sessions = 0
        self.max_active_sessions = 0
        self.login_calls = 0
        self.logout_calls = 0

    def login(self) -> _FakeLoginResult:
        with self._counter_lock:
            self.login_calls += 1
            self.active_sessions += 1
            self.max_active_sessions = max(self.max_active_sessions, self.active_sessions)
        return _FakeLoginResult()

    def logout(self) -> None:
        with self._counter_lock:
            self.logout_calls += 1
            self.active_sessions -= 1

    def query_history_k_data_plus(self, *args, **kwargs) -> _FakeResult:
        self.history_started.set()
        if not self.release_history.wait(timeout=2):
            raise RuntimeError("test timed out waiting to release BaoStock history query")
        return _FakeResult([["2026-07-14", "10", "11", "9", "10.5", "1000"]])

    def query_stock_basic(self) -> _FakeResult:
        return _FakeResult(
            [["sh.600519", "贵州茅台", "2001-08-27"]],
            fields=["code", "code_name", "ipoDate"],
        )


async def _run_overlapping_capabilities(
    fake: _ControlledBaoStock,
    kline_provider: BaoStockProvider,
    pool_provider: BaoStockProvider,
) -> None:
    kline_task = asyncio.create_task(kline_provider.kline("600519.SH", limit=1))
    pool_task: asyncio.Task | None = None
    try:
        assert await asyncio.to_thread(fake.history_started.wait, 1)
        pool_task = asyncio.create_task(pool_provider.stock_pool())
        await asyncio.sleep(0.05)
        assert fake.login_calls == 1
        fake.release_history.set()
        klines, stocks = await asyncio.gather(kline_task, pool_task)
        assert [item.date for item in klines] == ["2026-07-14"]
        assert [item.symbol for item in stocks] == ["600519.SH"]
    finally:
        fake.release_history.set()
        pending = [task for task in (kline_task, pool_task) if task is not None and not task.done()]
        if pending:
            await asyncio.gather(*pending, return_exceptions=True)


def test_baostock_serializes_kline_and_stock_pool_on_one_provider() -> None:
    fake = _ControlledBaoStock()
    provider = BaoStockProvider()

    with patch("app.services.baostock_provider.is_installed", return_value=True), patch.dict(
        "sys.modules", {"baostock": fake}
    ):
        asyncio.run(_run_overlapping_capabilities(fake, provider, provider))

    assert fake.max_active_sessions == 1
    assert fake.login_calls == fake.logout_calls == 2


def test_baostock_serializes_sessions_across_provider_instances() -> None:
    fake = _ControlledBaoStock()

    with patch("app.services.baostock_provider.is_installed", return_value=True), patch.dict(
        "sys.modules", {"baostock": fake}
    ):
        asyncio.run(_run_overlapping_capabilities(fake, BaoStockProvider(), BaoStockProvider()))

    assert fake.max_active_sessions == 1
    assert fake.login_calls == fake.logout_calls == 2


def test_baostock_cancelled_lock_waiter_never_starts_sdk_session() -> None:
    fake = _ControlledBaoStock()
    provider = BaoStockProvider()

    async def run_check() -> None:
        active = asyncio.create_task(provider.kline("600519.SH", limit=1))
        waiting: asyncio.Task | None = None
        try:
            assert await asyncio.to_thread(fake.history_started.wait, 1)
            waiting = asyncio.create_task(provider.stock_pool())
            await asyncio.sleep(0.05)
            waiting.cancel()
            with pytest.raises(asyncio.CancelledError):
                await waiting
            assert fake.login_calls == 1
        finally:
            fake.release_history.set()
            pending = [task for task in (active, waiting) if task is not None and not task.done()]
            if pending:
                await asyncio.gather(*pending, return_exceptions=True)

        await asyncio.sleep(0.05)
        assert fake.login_calls == fake.logout_calls == 1

    with patch("app.services.baostock_provider.is_installed", return_value=True), patch.dict(
        "sys.modules", {"baostock": fake}
    ):
        asyncio.run(run_check())


def test_baostock_running_cancellation_keeps_session_serialized_until_logout() -> None:
    fake = _ControlledBaoStock()
    provider = BaoStockProvider()

    async def run_check() -> None:
        cancelled = asyncio.create_task(provider.kline("600519.SH", limit=1))
        follow_up: asyncio.Task | None = None
        try:
            assert await asyncio.to_thread(fake.history_started.wait, 1)
            cancelled.cancel()
            with pytest.raises(asyncio.CancelledError):
                await cancelled

            follow_up = asyncio.create_task(provider.stock_pool())
            await asyncio.sleep(0.05)
            assert fake.login_calls == 1
            fake.release_history.set()
            stocks = await follow_up
            assert [item.symbol for item in stocks] == ["600519.SH"]
        finally:
            fake.release_history.set()
            pending = [task for task in (cancelled, follow_up) if task is not None and not task.done()]
            if pending:
                await asyncio.gather(*pending, return_exceptions=True)

    with patch("app.services.baostock_provider.is_installed", return_value=True), patch.dict(
        "sys.modules", {"baostock": fake}
    ):
        asyncio.run(run_check())

    assert fake.max_active_sessions == 1
    assert fake.login_calls == fake.logout_calls == 2


def test_tushare_client_uses_clean_token_without_mutating_global_state() -> None:
    clients: list[str] = []
    expected_client = object()

    class FakeTs:
        @staticmethod
        def set_token(token: str) -> None:
            raise AssertionError(f"global token must not be changed: {token}")

        @staticmethod
        def pro_api(token: str):
            clients.append(token)
            return expected_client

    provider = TushareProvider(token="  cleaned-token\n")
    with patch("app.services.tushare_provider.is_installed", return_value=True), patch.dict(
        "sys.modules", {"tushare": FakeTs}
    ):
        client = provider._client()

    assert client is expected_client
    assert clients == ["cleaned-token"]
