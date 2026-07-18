from __future__ import annotations

import asyncio
import math
import random
import re
from dataclasses import dataclass
from datetime import date, datetime, timedelta
from typing import Iterable

import httpx

from app.models.market import (
    DAILY_KLINE_CONTRACT_VERSION,
    KlineAdjustmentMode,
)
from app.models.schemas import Kline, Quote
from app.services.provider_errors import (
    ProviderCoverageMiss,
    ProviderError,
    ProviderProtocolError,
    ProviderTransportError,
    sanitize_provider_error,
)
from app.services.provider_utils import ensure_positive_limit, valid_ohlc
from app.utils.market_data import valid_kline
from app.utils.parsing import MISSING_NUMERIC_VALUES, required_float
from app.utils.symbols import normalize_symbol, tencent_symbol
from app.utils.time import now_text


TENCENT_QUOTE_MIN_FIELDS = 45
TENCENT_MARKET_MAP = {"1": "SH", "0": "SZ", "2": "SZ", "51": "SZ", "52": "SZ"}
TENCENT_AMOUNT_SCALE = 10000
TENCENT_MARKET_CAP_SCALE = 100000000
TENCENT_QUOTE_PAYLOAD_RE = re.compile(r'="([^"]*)"')


@dataclass(frozen=True)
class _TencentQuoteNumbers:
    price: float
    prev_close: float
    open_price: float
    high: float
    low: float
    volume: float
    amount: float


class MarketDataError(ProviderError):
    """行情数据源调用失败。"""


class MarketDataCoverageMiss(ProviderCoverageMiss, MarketDataError):
    """腾讯行情未覆盖请求标的。"""


class MarketDataTransportError(ProviderTransportError, MarketDataError):
    """腾讯行情网络调用失败。"""


class MarketDataProtocolError(ProviderProtocolError, MarketDataError):
    """腾讯行情返回内容无法安全使用。"""


class TencentMarketDataProvider:
    source_name = "腾讯行情"

    def __init__(self, *, timeout: float) -> None:
        self.timeout = timeout

    async def quote(self, symbol: str) -> Quote:
        return (await self.quotes([symbol]))[0]

    async def quotes(self, symbols: Iterable[str]) -> list[Quote]:
        url = _tencent_quote_url(symbols)
        if not url:
            return []
        text = await _fetch_tencent_quote_text(url, self.timeout)
        quotes = _tencent_quotes_from_text(text, self.source_name)
        if not quotes:
            if _tencent_quote_response_is_coverage_miss(text):
                raise MarketDataCoverageMiss("实时行情未覆盖请求股票")
            raise MarketDataProtocolError("实时行情返回为空或格式异常")
        return quotes

    async def kline(self, symbol: str, limit: int = 120) -> list[Kline]:
        ensure_positive_limit(limit)
        code = tencent_symbol(symbol)
        url = f"https://web.ifzq.gtimg.cn/appstock/app/fqkline/get?param={code},day,,,{limit},qfq"
        try:
            async with httpx.AsyncClient(timeout=self.timeout) as client:
                response = await client.get(url)
                response.raise_for_status()
                data = response.json()
        except httpx.HTTPError as exc:
            raise MarketDataTransportError(f"K线请求失败：{sanitize_provider_error(exc)}") from exc
        except ValueError as exc:
            raise MarketDataProtocolError(f"K线响应解析失败：{sanitize_provider_error(exc)}") from exc

        rows = _tencent_kline_rows(data, code)
        if not rows:
            if _tencent_kline_response_is_coverage_miss(data, code):
                raise MarketDataCoverageMiss(f"K线未覆盖请求股票：{symbol}")
            raise MarketDataProtocolError("K线返回结构异常")
        klines = _tencent_klines_from_rows(rows)
        if not klines:
            raise MarketDataProtocolError("K线有效数据为空")
        return stamp_daily_kline_contract(
            klines,
            adjustment_mode="qfq",
            source=self.source_name,
        )


async def _fetch_tencent_quote_text(url: str, timeout: float) -> str:
    try:
        async with httpx.AsyncClient(timeout=timeout) as client:
            response = await client.get(url)
            response.encoding = "gbk"
            response.raise_for_status()
            return response.text
    except httpx.HTTPError as exc:
        raise MarketDataTransportError(f"实时行情请求失败：{sanitize_provider_error(exc)}") from exc


def _tencent_quote_url(symbols: Iterable[str]) -> str:
    query_symbols = [tencent_symbol(symbol) for symbol in symbols]
    if not query_symbols:
        return ""
    return "https://qt.gtimg.cn/q=" + ",".join(query_symbols)


def _tencent_quotes_from_text(text: str, source_name: str) -> list[Quote]:
    return [
        quote
        for quote in (_parse_tencent_quote_payload(payload, source_name) for payload in _tencent_quote_payloads(text))
        if quote is not None
    ]


def _tencent_kline_rows(data: object, code: str) -> Iterable[object]:
    if not isinstance(data, dict):
        return []
    data_block = data.get("data")
    if not isinstance(data_block, dict):
        return []
    item = data_block.get(code)
    if not isinstance(item, dict):
        return []
    rows = item.get("qfqday") or []
    return rows if isinstance(rows, (list, tuple)) else []


def _tencent_kline_response_is_coverage_miss(data: object, code: str) -> bool:
    if not isinstance(data, dict):
        return False
    data_block = data.get("data")
    if not isinstance(data_block, dict):
        return False
    if code not in data_block:
        return True
    item = data_block.get(code)
    if not isinstance(item, dict):
        return False
    if "qfqday" not in item:
        return False
    rows = item["qfqday"]
    return isinstance(rows, (list, tuple)) and not rows


def _tencent_klines_from_rows(rows: Iterable[object]) -> list[Kline]:
    return [kline for kline in (_parse_tencent_kline_row(row) for row in rows) if kline is not None]


def _parse_tencent_kline_row(row: object) -> Kline | None:
    if not isinstance(row, (list, tuple)):
        return None
    if len(row) < 6 or row[0] is None:
        return None
    date = str(row[0]).strip()
    if not date:
        return None
    try:
        open_price = required_float(row[1], "腾讯K线开盘价", positive=True)
        close = required_float(row[2], "腾讯K线收盘价", positive=True)
        high = required_float(row[3], "腾讯K线最高价", positive=True)
        low = required_float(row[4], "腾讯K线最低价", positive=True)
        _ensure_kline_price_bounds(open_price, close, high, low)
        volume = _non_negative_value(row[5], "腾讯K线成交量")
    except ValueError:
        return None
    kline = Kline(
        date=date,
        open=open_price,
        close=close,
        high=high,
        low=low,
        volume=volume,
    )
    return kline if valid_kline(kline) else None


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
        run_minute = datetime.now().minute
        result = [_demo_quote(symbol, self._names, now, run_minute, self.source_name) for symbol in symbols]
        await asyncio.sleep(0)
        return result

    async def kline(self, symbol: str, limit: int = 120) -> list[Kline]:
        ensure_positive_limit(limit)
        self._ensure_enabled()
        code, market_code = normalize_symbol(symbol)
        _, _, base = _demo_stock_profile(code, market_code, self._names)
        rng = random.Random(int(code))
        rows: list[Kline] = []
        close = base
        today = datetime.now().date()
        trading_days = _previous_weekdays(today, limit)
        for index, day in enumerate(trading_days):
            if day.weekday() >= 5:
                continue
            drift = math.sin(index / 6) * 0.01 + rng.uniform(-0.025, 0.028)
            open_price = close * (1 + rng.uniform(-0.012, 0.012))
            close = max(0.8, open_price * (1 + drift))
            high = max(open_price, close) * (1 + rng.uniform(0.002, 0.018))
            low = min(open_price, close) * (1 - rng.uniform(0.002, 0.018))
            rounded_open, rounded_close, rounded_high, rounded_low = _rounded_demo_ohlc(
                open_price, close, high, low
            )
            rows.append(
                Kline(
                    date=day.isoformat(),
                    open=rounded_open,
                    close=rounded_close,
                    high=rounded_high,
                    low=rounded_low,
                    volume=rng.randint(100000, 8000000),
                )
            )
        return stamp_daily_kline_contract(
            rows[-limit:],
            adjustment_mode="qfq",
            source=self.source_name,
        )

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
            raise RuntimeError("本地演示数据源默认关闭；如需离线演示，请设置 ASHARE_RADAR_DEMO_PROVIDER_ENABLED=1")


def _demo_quote(
    symbol: str,
    names: dict[str, tuple[str, str, float]],
    timestamp: str,
    run_minute: int,
    source_name: str,
) -> Quote:
    code, market_code = normalize_symbol(symbol)
    name, market, base = _demo_stock_profile(code, market_code, names)
    rng = random.Random(int(code) + run_minute)
    prices, change_pct, raw_price, raw_prev_close = _demo_quote_prices(base, rng)
    volume = rng.randint(200000, 6000000)
    return Quote(
        code=code,
        name=name,
        market=market,
        price=prices["price"],
        prev_close=prices["prev_close"],
        open=prices["open"],
        high=prices["high"],
        low=prices["low"],
        volume=volume,
        amount=round(volume * raw_price * 100, 2),
        change=round(raw_price - raw_prev_close, 2),
        change_pct=change_pct,
        turnover_rate=round(rng.uniform(0.4, 8.5), 2),
        pe=round(rng.uniform(8, 58), 2),
        pb=round(rng.uniform(0.8, 9), 2),
        market_cap=round(rng.uniform(300, 20000) * 100000000, 2),
        timestamp=timestamp,
        source=source_name,
    )


def _demo_stock_profile(
    code: str,
    market_code: str,
    names: dict[str, tuple[str, str, float]],
) -> tuple[str, str, float]:
    market = market_code.upper()
    return names.get(f"{code}.{market}", (f"演示股票{code[-3:]}", market, 20.0))


def _demo_quote_prices(base: float, rng: random.Random) -> tuple[dict[str, float], float, float, float]:
    change_pct = round(rng.uniform(-3.2, 4.5), 2)
    prev_close = base * (1 + rng.uniform(-0.02, 0.02))
    price = prev_close * (1 + change_pct / 100)
    open_price = prev_close * (1 + rng.uniform(-0.01, 0.01))
    high = max(price, prev_close, open_price) * (1 + rng.uniform(0.002, 0.018))
    low = min(price, prev_close, open_price) * (1 - rng.uniform(0.002, 0.018))
    return _rounded_demo_quote_prices(price, prev_close, open_price, high, low), change_pct, price, prev_close


def _format_timestamp(raw: str) -> str:
    text = str(raw).strip()
    if len(text) >= 14 and text[:14].isdigit():
        timestamp = f"{text[:4]}-{text[4:6]}-{text[6:8]} {text[8:10]}:{text[10:12]}:{text[12:14]}"
        try:
            datetime.strptime(timestamp, "%Y-%m-%d %H:%M:%S")
        except ValueError as exc:
            raise MarketDataProtocolError("腾讯行情报价事件时间无法解析") from exc
        return timestamp
    raise MarketDataProtocolError("腾讯行情缺少有效报价事件时间")


def _previous_weekdays(today: date, limit: int) -> list[date]:
    days: list[date] = []
    candidate = today - timedelta(days=1)
    while len(days) < limit:
        if candidate.weekday() < 5:
            days.append(candidate)
        candidate -= timedelta(days=1)
    return list(reversed(days))


def _tencent_quote_payloads(text: str) -> list[str]:
    payloads: list[str] = []
    for match in TENCENT_QUOTE_PAYLOAD_RE.finditer(text):
        payload = match.group(1).strip()
        if payload:
            payloads.append(payload)
    return payloads


def _tencent_quote_response_is_coverage_miss(text: str) -> bool:
    matches = list(TENCENT_QUOTE_PAYLOAD_RE.finditer(text))
    return bool(matches) and all(not match.group(1).strip() for match in matches)


def _parse_tencent_quote_payload(payload: str, source_name: str) -> Quote | None:
    parts = _tencent_quote_parts(payload)
    if not _valid_tencent_quote_parts(parts):
        return None
    market = _tencent_market(parts[0])
    if market is None:
        return None
    try:
        numbers = _parse_tencent_quote_numbers(parts)
    except ValueError:
        return None
    market_cap = _optional_scaled_non_negative(parts, 44, TENCENT_MARKET_CAP_SCALE)
    return Quote(
        code=parts[2].strip(),
        name=parts[1].strip(),
        market=market,
        price=numbers.price,
        prev_close=numbers.prev_close,
        open=numbers.open_price,
        high=numbers.high,
        low=numbers.low,
        volume=numbers.volume,
        amount=numbers.amount,
        change=_number_or_default(parts, 31, numbers.price - numbers.prev_close, "腾讯涨跌额"),
        change_pct=_tencent_change_pct(parts, numbers.price, numbers.prev_close),
        turnover_rate=_optional_non_negative(parts, 38),
        pe=_optional_number(parts, 39),
        pb=_optional_non_negative(parts, 46),
        market_cap=market_cap,
        timestamp=_format_timestamp(parts[30]),
        source=source_name,
    )


def _valid_tencent_quote_parts(parts: list[str]) -> bool:
    return (
        len(parts) >= TENCENT_QUOTE_MIN_FIELDS
        and bool(parts[1].strip())
        and _valid_tencent_quote_code(parts[2])
    )


def _valid_tencent_quote_code(value: str) -> bool:
    code = value.strip()
    return code.isdigit() and len(code) == 6 and code != "000000"


def _tencent_quote_parts(payload: str) -> list[str]:
    return [part.strip() for part in payload.strip().split("~")]


def _parse_tencent_quote_numbers(parts: list[str]) -> _TencentQuoteNumbers:
    price = required_float(parts[3], "腾讯现价", positive=True)
    prev_close = required_float(parts[4], "腾讯昨收", positive=True)
    open_price = _required_number_or_default(parts, 5, price, "腾讯开盘价")
    high = _first_required_number(parts, 33, 41, field="腾讯最高价")
    low = _first_required_number(parts, 34, 42, field="腾讯最低价")
    _ensure_quote_price_bounds(open_price, price, high, low)
    volume = _non_negative_part(parts, 36, "腾讯成交量")
    amount = _non_negative_part(parts, 37, "腾讯成交额") * TENCENT_AMOUNT_SCALE
    return _TencentQuoteNumbers(
        price=price,
        prev_close=prev_close,
        open_price=open_price,
        high=high,
        low=low,
        volume=volume,
        amount=amount,
    )


def _tencent_market(flag: str) -> str | None:
    return TENCENT_MARKET_MAP.get(flag)


def _first_required_number(parts: list[str], *indices: int, field: str) -> float:
    for index in indices:
        if index < len(parts) and parts[index] not in MISSING_NUMERIC_VALUES:
            return required_float(parts[index], field, positive=True)
    raise ValueError(f"{field}缺失")


def _required_number_or_default(parts: list[str], index: int, default: float, field: str) -> float:
    if index >= len(parts) or parts[index] in MISSING_NUMERIC_VALUES:
        return default
    return required_float(parts[index], field, positive=True)


def _ensure_quote_price_bounds(open_price: float, price: float, high: float, low: float) -> None:
    if not valid_ohlc(open_price, price, high, low):
        raise ValueError("开盘价或现价超出最高/最低价范围")


def _ensure_kline_price_bounds(open_price: float, close: float, high: float, low: float) -> None:
    if not valid_ohlc(open_price, close, high, low):
        raise ValueError("开收盘价超出最高/最低价范围")


def _optional_number(parts: list[str], index: int) -> float | None:
    if index >= len(parts) or parts[index] in MISSING_NUMERIC_VALUES:
        return None
    try:
        value = required_float(parts[index])
    except ValueError:
        return None
    return value or None


def _optional_non_negative(parts: list[str], index: int) -> float | None:
    value = _optional_number(parts, index)
    return value if value is not None and value >= 0 else None


def _optional_scaled_non_negative(parts: list[str], index: int, scale: float) -> float | None:
    value = _optional_non_negative(parts, index)
    return value * scale if value is not None else None


def _number_or_default(parts: list[str], index: int, default: float, field: str) -> float:
    if index >= len(parts) or parts[index] in MISSING_NUMERIC_VALUES:
        return default
    try:
        return required_float(parts[index], field)
    except ValueError:
        return default


def _tencent_change_pct(parts: list[str], price: float, prev_close: float) -> float:
    explicit = _number_or_default(parts, 32, math.nan, "腾讯涨跌幅")
    if math.isfinite(explicit):
        return explicit
    return (price - prev_close) / prev_close * 100 if prev_close > 0 else 0.0


def _non_negative_part(parts: list[str], index: int, field: str) -> float:
    if index >= len(parts):
        raise ValueError(f"{field}缺失")
    return _non_negative_value(parts[index], field)


def _non_negative_value(value: object, field: str) -> float:
    parsed = required_float(value, field)
    if parsed < 0:
        raise ValueError(f"{field}不能为负数")
    return parsed


def _rounded_demo_quote_prices(
    price: float, prev_close: float, open_price: float, high: float, low: float
) -> dict[str, float]:
    rounded = {
        "price": round(price, 2),
        "prev_close": round(prev_close, 2),
        "open": round(open_price, 2),
        "high": round(high, 2),
        "low": round(low, 2),
    }
    rounded["high"] = max(
        rounded["high"], rounded["price"], rounded["prev_close"], rounded["open"]
    )
    rounded["low"] = min(
        rounded["low"], rounded["price"], rounded["prev_close"], rounded["open"]
    )
    return rounded


def _rounded_demo_ohlc(
    open_price: float, close: float, high: float, low: float
) -> tuple[float, float, float, float]:
    rounded_open = round(open_price, 2)
    rounded_close = round(close, 2)
    rounded_high = max(round(high, 2), rounded_open, rounded_close)
    rounded_low = min(round(low, 2), rounded_open, rounded_close)
    return rounded_open, rounded_close, rounded_high, rounded_low


def stamp_daily_kline_contract(
    rows: list[Kline],
    *,
    adjustment_mode: KlineAdjustmentMode,
    source: str,
) -> list[Kline]:
    if not rows:
        return []
    sources = {str(item.source or source).strip() for item in rows}
    if "" in sources or len(sources) != 1:
        raise ProviderProtocolError("日K序列来源不一致")
    as_of = _daily_kline_as_of(rows)
    series_source = next(iter(sources))
    data_version = "|".join(
        (DAILY_KLINE_CONTRACT_VERSION, adjustment_mode, series_source, as_of)
    )
    return [
        item.model_copy(
            update={
                "adjustment_mode": adjustment_mode,
                "as_of": as_of,
                "data_version": data_version,
                "contract_version": DAILY_KLINE_CONTRACT_VERSION,
                "source": item.source or source,
            }
        )
        for item in rows
    ]


def _daily_kline_as_of(rows: list[Kline]) -> str:
    parsed_dates: list[date] = []
    for item in rows:
        try:
            parsed_dates.append(datetime.fromisoformat(str(item.date)[:10]).date())
        except ValueError as exc:
            raise ProviderProtocolError(f"日K日期无法解析：{item.date}") from exc
    return max(parsed_dates).isoformat()
