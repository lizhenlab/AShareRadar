from __future__ import annotations

import asyncio

from app.models.schemas import Kline, ProviderCapability, StockInfo
from app.services.provider_utils import ensure_positive_limit, is_installed, ts_symbol, valid_ohlc
from app.services.provider_stock_mappers import stock_info_from_tushare_row
from app.utils.parsing import required_float, safe_float
from app.utils.time import now_text


class TushareProvider:
    source_name = "Tushare Pro"

    def __init__(self, token: str | None = None) -> None:
        stripped = token.strip() if token else ""
        self.token = stripped or None

    async def kline(self, symbol: str, limit: int = 120) -> list[Kline]:
        ensure_positive_limit(limit)
        pro = self._client()

        def load() -> list[Kline]:
            df = pro.daily(ts_code=ts_symbol(symbol))
            rows = df.sort_values("trade_date").tail(limit)
            return [item for _, row in rows.iterrows() if (item := _tushare_kline_from_row(row)) is not None]

        return await asyncio.to_thread(load)

    async def stock_pool(self) -> list[StockInfo]:
        pro = self._client()

        def load() -> list[StockInfo]:
            df = pro.stock_basic(exchange="", list_status="L", fields="ts_code,symbol,name,industry,list_date")
            stamp = now_text()
            result = []
            for _, row in df.iterrows():
                item = stock_info_from_tushare_row(row, stamp=stamp, source_name=self.source_name)
                if item:
                    result.append(item)
            return result

        return await asyncio.to_thread(load)

    def capability(self) -> ProviderCapability:
        installed = is_installed("tushare")
        has_token = bool(self.token)
        return ProviderCapability(
            name="tushare",
            installed=installed,
            enabled=installed and has_token,
            reliability_level="准正式",
            daily_kline=installed and has_token,
            stock_pool=installed and has_token,
            note="需要 ASHARE_RADAR_TUSHARE_TOKEN 环境变量；兼容 TUSHARE_TOKEN。适合个股基础信息、日线和财务数据。",
        )

    def _client(self):
        if not is_installed("tushare"):
            raise RuntimeError("未安装 tushare，请执行 python3 -m pip install tushare")
        if not self.token:
            raise RuntimeError("未配置 ASHARE_RADAR_TUSHARE_TOKEN，跳过 Tushare 数据源")
        import tushare as ts

        ts.set_token(self.token)
        return ts.pro_api()


def _tushare_kline_from_row(row) -> Kline | None:
    try:
        trade_date = str(row["trade_date"]).strip()
        open_price = required_float(row["open"], "Tushare日K开盘价", positive=True)
        close = required_float(row["close"], "Tushare日K收盘价", positive=True)
        high = required_float(row["high"], "Tushare日K最高价", positive=True)
        low = required_float(row["low"], "Tushare日K最低价", positive=True)
        volume = safe_float(str(row["vol"]))
    except (KeyError, ValueError):
        return None
    if len(trade_date) < 8 or not valid_ohlc(open_price, close, high, low):
        return None
    return Kline(
        date=f"{trade_date[:4]}-{trade_date[4:6]}-{trade_date[6:8]}",
        open=open_price,
        close=close,
        high=high,
        low=low,
        volume=volume,
    )
