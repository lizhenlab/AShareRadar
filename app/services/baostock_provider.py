from __future__ import annotations

import asyncio
from collections.abc import Callable
from concurrent.futures import ThreadPoolExecutor
import threading
from typing import TypeVar

from app.models.schemas import Kline, ProviderCapability, StockInfo
from app.runtime_environment import isolate_user_site_packages
from app.services.provider_utils import bs_symbol, ensure_positive_limit, is_installed, valid_ohlc
from app.services.provider_stock_mappers import stock_info_from_baostock_row
from app.services.providers import stamp_daily_kline_contract
from app.utils.parsing import required_float, safe_float
from app.utils.time import now_text

isolate_user_site_packages()

_T = TypeVar("_T")
_BAOSTOCK_SESSION_LOCK = threading.Lock()
BAOSTOCK_QFQ_ADJUST_FLAG = "2"


class BaoStockProvider:
    source_name = "BaoStock"

    def __init__(self) -> None:
        self._session_executor = ThreadPoolExecutor(max_workers=1, thread_name_prefix="baostock-session")
        self._closed = False

    async def kline(self, symbol: str, limit: int = 120) -> list[Kline]:
        ensure_positive_limit(limit)
        query_symbol = bs_symbol(symbol)
        self._ensure_installed()

        def load() -> list[Kline]:
            import baostock as bs

            lg = bs.login()
            if lg.error_code != "0":
                raise RuntimeError(f"BaoStock登录失败：{lg.error_msg}")
            try:
                rs = bs.query_history_k_data_plus(
                    query_symbol,
                    "date,open,high,low,close,volume",
                    frequency="d",
                    adjustflag=BAOSTOCK_QFQ_ADJUST_FLAG,
                )
                rows = []
                while rs.next():
                    rows.append(rs.get_row_data())
                if rs.error_code != "0":
                    raise RuntimeError(f"BaoStock K线失败：{rs.error_msg}")
                klines = [
                    item
                    for row in rows[-limit:]
                    if (item := _baostock_kline_from_row(row)) is not None
                ]
                return stamp_daily_kline_contract(
                    klines,
                    adjustment_mode="qfq",
                    source=self.source_name,
                )
            finally:
                bs.logout()

        return await self._run_session(load)

    async def stock_pool(self) -> list[StockInfo]:
        self._ensure_installed()
        return await self._run_session(lambda: _load_baostock_stock_pool(self.source_name))

    async def aclose(self) -> None:
        if self._closed:
            return
        self._closed = True
        self._session_executor.shutdown(wait=False, cancel_futures=True)

    async def _run_session(self, call: Callable[[], _T]) -> _T:
        if self._closed:
            raise RuntimeError("BaoStockProvider 已关闭")
        return await _run_baostock_session(self._session_executor, call)

    def capability(self) -> ProviderCapability:
        installed = is_installed("baostock")
        return ProviderCapability(
            name="baostock",
            installed=installed,
            enabled=installed,
            reliability_level="公开历史源",
            daily_kline=installed,
            stock_pool=installed,
            note="免费历史行情源，适合个股历史K线备份。",
        )

    @staticmethod
    def _ensure_installed() -> None:
        if not is_installed("baostock"):
            raise RuntimeError("未安装 baostock，请执行 python3 -m pip install baostock")


async def _run_baostock_session(executor: ThreadPoolExecutor, call: Callable[[], _T]) -> _T:
    worker = executor.submit(_call_with_baostock_session_lock, call)
    try:
        return await asyncio.wrap_future(worker)
    except asyncio.CancelledError:
        worker.cancel()
        raise


def _call_with_baostock_session_lock(call: Callable[[], _T]) -> _T:
    with _BAOSTOCK_SESSION_LOCK:
        return call()


def _load_baostock_stock_pool(source_name: str) -> list[StockInfo]:
    import baostock as bs

    _login_baostock(bs)
    try:
        result = bs.query_stock_basic()
        rows = _baostock_result_rows(result)
        _raise_baostock_error(result, "BaoStock股票池失败")
        return _stock_pool_from_rows(rows, result.fields, source_name, now_text())
    finally:
        bs.logout()


def _login_baostock(bs) -> None:
    login_result = bs.login()
    _raise_baostock_error(login_result, "BaoStock登录失败")


def _raise_baostock_error(result, message: str) -> None:
    if result.error_code != "0":
        raise RuntimeError(f"{message}：{result.error_msg}")


def _baostock_result_rows(result) -> list[list[str]]:
    rows = []
    while result.next():
        rows.append(result.get_row_data())
    return rows


def _stock_pool_from_rows(rows: list[list[str]], fields: list[str], source_name: str, stamp: str) -> list[StockInfo]:
    result: list[StockInfo] = []
    for row in rows:
        item = stock_info_from_baostock_row(dict(zip(fields, row)), stamp=stamp, source_name=source_name)
        if item:
            result.append(item)
    return result


def _baostock_kline_from_row(row) -> Kline | None:
    try:
        date = str(row[0]).strip()
        open_price = required_float(row[1], "BaoStock日K开盘价", positive=True)
        high = required_float(row[2], "BaoStock日K最高价", positive=True)
        low = required_float(row[3], "BaoStock日K最低价", positive=True)
        close = required_float(row[4], "BaoStock日K收盘价", positive=True)
        volume = safe_float(row[5])
    except (IndexError, TypeError, ValueError):
        return None
    if not date or not valid_ohlc(open_price, close, high, low):
        return None
    return Kline(date=date, open=open_price, high=high, low=low, close=close, volume=volume)
