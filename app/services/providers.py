from __future__ import annotations

import asyncio
import math
import random
import re
from datetime import datetime, timedelta
from typing import Iterable

import httpx

from app.config import get_settings
from app.models.schemas import Kline, Quote
from app.utils.parsing import safe_float
from app.utils.symbols import normalize_symbol, standard_symbol, tencent_symbol
from app.utils.time import now_text


class MarketDataError(RuntimeError):
    """行情数据源调用失败。"""


class TencentMarketDataProvider:
    source_name = "腾讯行情"

    def __init__(self) -> None:
        settings = get_settings()
        self.timeout = settings.request_timeout_seconds

    async def quote(self, symbol: str) -> Quote:
        return (await self.quotes([symbol]))[0]

    async def quotes(self, symbols: Iterable[str]) -> list[Quote]:
        query_symbols = [tencent_symbol(symbol) for symbol in symbols]
        if not query_symbols:
            return []
        url = "http://qt.gtimg.cn/q=" + ",".join(query_symbols)
        try:
            async with httpx.AsyncClient(timeout=self.timeout) as client:
                response = await client.get(url)
                response.encoding = "gbk"
                response.raise_for_status()
        except httpx.HTTPError as exc:
            raise MarketDataError(f"实时行情请求失败：{exc}") from exc

        quotes: list[Quote] = []
        for payload in re.findall(r'v_[^=]+="([^"]*)"', response.text):
            parts = payload.split("~")
            if len(parts) < 45 or not parts[2]:
                continue
            market = "SH" if parts[0] == "1" else "SZ"
            quotes.append(
                Quote(
                    code=parts[2],
                    name=parts[1],
                    market=market,
                    price=safe_float(parts[3]),
                    prev_close=safe_float(parts[4]),
                    open=safe_float(parts[5]),
                    high=safe_float(parts[33] or parts[41]),
                    low=safe_float(parts[34] or parts[42]),
                    volume=safe_float(parts[36]),
                    amount=safe_float(parts[37]) * 10000,
                    change=safe_float(parts[31]),
                    change_pct=safe_float(parts[32]),
                    turnover_rate=safe_float(parts[38]) or None,
                    pe=safe_float(parts[39]) or None,
                    pb=safe_float(parts[46]) or None,
                    market_cap=safe_float(parts[44]) * 100000000 if safe_float(parts[44]) else None,
                    timestamp=_format_timestamp(parts[30]),
                    source=self.source_name,
                )
            )
        if not quotes:
            raise MarketDataError("实时行情返回为空")
        return quotes

    async def kline(self, symbol: str, limit: int = 120) -> list[Kline]:
        code = tencent_symbol(symbol)
        url = f"https://web.ifzq.gtimg.cn/appstock/app/fqkline/get?param={code},day,,,{limit},qfq"
        try:
            async with httpx.AsyncClient(timeout=self.timeout) as client:
                response = await client.get(url)
                response.raise_for_status()
                data = response.json()
        except (httpx.HTTPError, ValueError) as exc:
            raise MarketDataError(f"K线请求失败：{exc}") from exc

        item = data.get("data", {}).get(code, {})
        rows = item.get("qfqday") or item.get("day") or []
        if not rows:
            raise MarketDataError("K线返回为空")
        return [
            Kline(
                date=row[0],
                open=safe_float(row[1]),
                close=safe_float(row[2]),
                high=safe_float(row[3]),
                low=safe_float(row[4]),
                volume=safe_float(row[5]),
            )
            for row in rows
        ]


class DemoMarketDataProvider:
    source_name = "本地演示数据"

    def __init__(self, enabled: bool = False) -> None:
        self.enabled = enabled

    _names = {
        "000001.SH": ("上证指数", "SH", 4200.0),
        "399001.SZ": ("深证成指", "SZ", 15800.0),
        "399006.SZ": ("创业板指", "SZ", 3950.0),
        "600519.SH": ("贵州茅台", "SH", 1340.0),
        "000001.SZ": ("平安银行", "SZ", 11.2),
        "300750.SZ": ("宁德时代", "SZ", 245.0),
        "601318.SH": ("中国平安", "SH", 56.0),
        "000858.SZ": ("五粮液", "SZ", 132.0),
        "002594.SZ": ("比亚迪", "SZ", 315.0),
        "600036.SH": ("招商银行", "SH", 43.0),
        "600900.SH": ("长江电力", "SH", 31.0),
        "000333.SZ": ("美的集团", "SZ", 76.0),
        "002475.SZ": ("立讯精密", "SZ", 42.0),
    }

    async def quote(self, symbol: str) -> Quote:
        self._ensure_enabled()
        return (await self.quotes([symbol]))[0]

    async def quotes(self, symbols: Iterable[str]) -> list[Quote]:
        self._ensure_enabled()
        now = now_text()
        result: list[Quote] = []
        for symbol in symbols:
            code, market_code = normalize_symbol(symbol)
            normalized = f"{code}.{market_code.upper()}"
            name, market, base = self._names.get(normalized, (f"演示股票{code[-3:]}", market_code.upper(), 20.0))
            seed = int(code) + datetime.now().minute
            random.seed(seed)
            change_pct = round(random.uniform(-3.2, 4.5), 2)
            prev_close = base * (1 + random.uniform(-0.02, 0.02))
            price = prev_close * (1 + change_pct / 100)
            high = max(price, prev_close) * (1 + random.uniform(0.002, 0.018))
            low = min(price, prev_close) * (1 - random.uniform(0.002, 0.018))
            volume = random.randint(200000, 6000000)
            amount = volume * price * 100
            result.append(
                Quote(
                    code=code,
                    name=name,
                    market=market,
                    price=round(price, 2),
                    prev_close=round(prev_close, 2),
                    open=round(prev_close * (1 + random.uniform(-0.01, 0.01)), 2),
                    high=round(high, 2),
                    low=round(low, 2),
                    volume=volume,
                    amount=round(amount, 2),
                    change=round(price - prev_close, 2),
                    change_pct=change_pct,
                    turnover_rate=round(random.uniform(0.4, 8.5), 2),
                    pe=round(random.uniform(8, 58), 2),
                    pb=round(random.uniform(0.8, 9), 2),
                    market_cap=round(random.uniform(300, 20000) * 100000000, 2),
                    timestamp=now,
                    source=self.source_name,
                )
            )
        await asyncio.sleep(0)
        return result

    async def kline(self, symbol: str, limit: int = 120) -> list[Kline]:
        self._ensure_enabled()
        code, market_code = normalize_symbol(symbol)
        normalized = f"{code}.{market_code.upper()}"
        _, _, base = self._names.get(normalized, (f"演示股票{code[-3:]}", market_code.upper(), 20.0))
        random.seed(int(code))
        rows: list[Kline] = []
        close = base
        today = datetime.now().date()
        for index in range(limit * 2):
            day = today - timedelta(days=(limit * 2 - index))
            if day.weekday() >= 5:
                continue
            drift = math.sin(index / 6) * 0.01 + random.uniform(-0.025, 0.028)
            open_price = close * (1 + random.uniform(-0.012, 0.012))
            close = max(0.8, open_price * (1 + drift))
            high = max(open_price, close) * (1 + random.uniform(0.002, 0.018))
            low = min(open_price, close) * (1 - random.uniform(0.002, 0.018))
            rows.append(
                Kline(
                    date=day.isoformat(),
                    open=round(open_price, 2),
                    close=round(close, 2),
                    high=round(high, 2),
                    low=round(low, 2),
                    volume=random.randint(100000, 8000000),
                )
            )
        return rows[-limit:]

    def capability(self):
        from app.models.schemas import ProviderCapability

        return ProviderCapability(
            name="demo",
            installed=True,
            enabled=self.enabled,
            reliability_level="演示",
            realtime_quote=self.enabled,
            daily_kline=self.enabled,
            note="本地随机演示数据，仅用于离线开发；默认关闭，不能作为真实行情依据。",
        )

    def _ensure_enabled(self) -> None:
        if not self.enabled:
            raise RuntimeError("本地演示数据源默认关闭；如需离线演示，请设置 DEMO_PROVIDER_ENABLED=1")


def _format_timestamp(raw: str) -> str:
    if len(raw) >= 14 and raw.isdigit():
        return f"{raw[:4]}-{raw[4:6]}-{raw[6:8]} {raw[8:10]}:{raw[10:12]}:{raw[12:14]}"
    return now_text()
