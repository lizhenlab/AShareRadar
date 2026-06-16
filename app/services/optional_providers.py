from __future__ import annotations

import asyncio
from contextlib import contextmanager
import os
from typing import Any

import requests

from app.models.schemas import Kline, MinuteKline, OrderBook, OrderBookLevel, PlateItem, ProviderCapability, Quote, StockConceptItem, StockInfo
from app.services.provider_utils import ak_symbol, bs_symbol, is_installed, pick, ts_symbol
from app.utils.parsing import safe_float
from app.utils.symbols import normalize_symbol, standard_symbol
from app.utils.time import now_text


EASTMONEY_NO_PROXY_HOSTS = (
    "eastmoney.com",
    ".eastmoney.com",
    "*.eastmoney.com",
    "82.push2.eastmoney.com",
    "23.push2.eastmoney.com",
    "53.push2.eastmoney.com",
    "push2his.eastmoney.com",
)
EASTMONEY_QUOTE_HOSTS = ("82.push2.eastmoney.com", "23.push2.eastmoney.com", "53.push2.eastmoney.com")
EASTMONEY_SCHEMES = ("https", "http")
EASTMONEY_HIST_HOST = "push2his.eastmoney.com"
EASTMONEY_UT_PARAM = "bd1d9ddb04089700cf9c27f6f7426281"
EASTMONEY_HEADERS = {
    "User-Agent": "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/126.0 Safari/537.36",
    "Referer": "https://quote.eastmoney.com/",
    "Accept": "application/json,text/plain,*/*",
    "Connection": "close",
}


class AKShareProvider:
    source_name = "AKShare"

    async def quote(self, symbol: str) -> Quote:
        return (await self.quotes([symbol]))[0]

    async def quotes(self, symbols) -> list[Quote]:
        self._ensure_installed()

        def load() -> list[Quote]:
            direct_error = ""
            try:
                result = _eastmoney_quotes(symbols)
                if result:
                    return result
            except Exception as exc:
                direct_error = str(exc)
            import akshare as ak

            try:
                with _eastmoney_no_proxy():
                    df = ak.stock_zh_a_spot_em()
            except Exception as exc:
                detail = f"；轻量直连失败：{direct_error}" if direct_error else ""
                raise RuntimeError(f"AKShare实时行情失败：{exc}{detail}") from exc
            wanted = {ak_symbol(symbol) for symbol in symbols}
            rows = df[df["代码"].astype(str).isin(wanted)]
            stamp = now_text()
            fallback_result = []
            for _, row in rows.iterrows():
                code = str(row["代码"]).zfill(6)
                market = normalize_symbol(code)[1].upper()
                price = safe_float(str(pick(row, "最新价", default=0)))
                prev_close = safe_float(str(pick(row, "昨收", default=0))) or price
                change = safe_float(str(pick(row, "涨跌额", default=price - prev_close)))
                fallback_result.append(
                    Quote(
                        code=code,
                        name=str(pick(row, "名称", default=code)),
                        market=market,
                        price=price,
                        prev_close=prev_close,
                        open=safe_float(str(pick(row, "今开", default=price))),
                        high=safe_float(str(pick(row, "最高", default=price))),
                        low=safe_float(str(pick(row, "最低", default=price))),
                        volume=safe_float(str(pick(row, "成交量", default=0))),
                        amount=safe_float(str(pick(row, "成交额", default=0))),
                        change=change,
                        change_pct=safe_float(str(pick(row, "涨跌幅", default=0))),
                        turnover_rate=safe_float(str(pick(row, "换手率", default=0))) or None,
                        pe=safe_float(str(pick(row, "市盈率-动态", "市盈率", default=0))) or None,
                        pb=safe_float(str(pick(row, "市净率", default=0))) or None,
                        market_cap=safe_float(str(pick(row, "总市值", default=0))) or None,
                        timestamp=stamp,
                        source=self.source_name,
                    )
                )
            if len(fallback_result) != len(wanted):
                missing = wanted - {item.code for item in fallback_result}
                raise RuntimeError(f"AKShare实时行情缺少代码：{','.join(sorted(missing))}")
            return fallback_result

        return await asyncio.to_thread(load)

    async def kline(self, symbol: str, limit: int = 120) -> list[Kline]:
        self._ensure_installed()

        def load() -> list[Kline]:
            import akshare as ak

            try:
                with _eastmoney_no_proxy():
                    df = ak.stock_zh_a_hist(symbol=ak_symbol(symbol), period="daily", adjust="qfq")
                rows = df.tail(limit)
                return [
                    Kline(
                        date=str(row["日期"]),
                        open=safe_float(str(row["开盘"])),
                        close=safe_float(str(row["收盘"])),
                        high=safe_float(str(row["最高"])),
                        low=safe_float(str(row["最低"])),
                        volume=safe_float(str(row["成交量"])),
                    )
                    for _, row in rows.iterrows()
                ]
            except Exception:
                return _eastmoney_kline(symbol, period="101", limit=limit)

        return await asyncio.to_thread(load)

    async def minute_kline(self, symbol: str, interval: str = "5m", limit: int = 120) -> list[MinuteKline]:
        self._ensure_installed()
        period = _minute_period(interval)

        def load() -> list[MinuteKline]:
            import akshare as ak

            try:
                with _eastmoney_no_proxy():
                    df = ak.stock_zh_a_hist_min_em(symbol=ak_symbol(symbol), period=period, adjust="qfq")
                rows = df.tail(limit)
                result = []
                for _, row in rows.iterrows():
                    result.append(
                        MinuteKline(
                            timestamp=str(pick(row, "时间", default="")),
                            open=safe_float(str(pick(row, "开盘", default=0))),
                            close=safe_float(str(pick(row, "收盘", default=0))),
                            high=safe_float(str(pick(row, "最高", default=0))),
                            low=safe_float(str(pick(row, "最低", default=0))),
                            volume=safe_float(str(pick(row, "成交量", default=0))),
                            amount=safe_float(str(pick(row, "成交额", default=0))) or None,
                            turnover_rate=safe_float(str(pick(row, "换手率", default=0))) or None,
                            interval=interval,
                            source=self.source_name,
                        )
                    )
                return [item for item in result if item.timestamp and item.close > 0]
            except Exception:
                return _eastmoney_minute_kline(symbol, period=period, interval=interval, limit=limit)

        return await asyncio.to_thread(load)

    async def stock_pool(self) -> list[StockInfo]:
        self._ensure_installed()

        def load() -> list[StockInfo]:
            import akshare as ak

            with _eastmoney_no_proxy():
                df = ak.stock_info_a_code_name()
            stamp = now_text()
            result = []
            for _, row in df.iterrows():
                code = str(pick(row, "code", "代码", default="")).zfill(6)
                if len(code) != 6:
                    continue
                market = normalize_symbol(code)[1].upper()
                result.append(
                    StockInfo(
                        symbol=standard_symbol(code),
                        code=code,
                        market=market,
                        name=str(pick(row, "name", "名称", default=code)),
                        source=self.source_name,
                        updated_at=stamp,
                    )
                )
            return result

        return await asyncio.to_thread(load)

    async def plate_rank(self, limit: int = 20) -> list[PlateItem]:
        self._ensure_installed()

        def load() -> list[PlateItem]:
            import akshare as ak

            with _eastmoney_no_proxy():
                df = ak.stock_board_industry_name_em()
            stamp = now_text()
            result = []
            for index, (_, row) in enumerate(df.head(limit).iterrows(), start=1):
                result.append(
                    PlateItem(
                        rank=index,
                        name=str(pick(row, "板块名称", "名称", default="--")),
                        change_pct=safe_float(str(pick(row, "涨跌幅", default=0))),
                        amount=safe_float(str(pick(row, "成交额", default=0))) or None,
                        turnover_rate=safe_float(str(pick(row, "换手率", default=0))) or None,
                        leading_stock=str(pick(row, "领涨股票", default="")) or None,
                        leading_stock_change_pct=safe_float(str(pick(row, "领涨股票-涨跌幅", default=0))) or None,
                        source=self.source_name,
                        updated_at=stamp,
                    )
                )
            return result

        return await asyncio.to_thread(load)

    async def stock_concepts(self, symbol: str, limit: int = 8) -> list[StockConceptItem]:
        self._ensure_installed()
        normalized = standard_symbol(symbol)
        code, _ = normalize_symbol(symbol)

        def load() -> list[StockConceptItem]:
            import akshare as ak

            stamp = now_text()
            errors: list[str] = []
            for loader in (_stock_concepts_from_em, _stock_concepts_from_sina):
                try:
                    with _eastmoney_no_proxy():
                        result = loader(ak, normalized, code, stamp, limit)
                except Exception as exc:
                    errors.append(str(exc))
                    continue
                if result:
                    return result
            if errors:
                raise RuntimeError("概念公开源不可用：" + "；".join(errors[:2]))
            return []

        return await asyncio.to_thread(load)

    def capability(self) -> ProviderCapability:
        installed = is_installed("akshare")
        return ProviderCapability(
            name="akshare",
            installed=installed,
            enabled=installed,
            reliability_level="公开源",
            realtime_quote=installed,
            daily_kline=installed,
            minute_kline=installed,
            stock_pool=installed,
            plate_rank=installed,
            concept_board=installed,
            note="免费公开数据源，适合个人研究；实时性和稳定性取决于源站。",
        )

    @staticmethod
    def _ensure_installed() -> None:
        if not is_installed("akshare"):
            raise RuntimeError("未安装 akshare，请执行 python3 -m pip install akshare")


@contextmanager
def _eastmoney_no_proxy():
    previous = {key: os.environ.get(key) for key in ("NO_PROXY", "no_proxy")}
    merged = _merge_no_proxy(previous["NO_PROXY"] or previous["no_proxy"])
    os.environ["NO_PROXY"] = merged
    os.environ["no_proxy"] = merged
    try:
        yield
    finally:
        for key, value in previous.items():
            if value is None:
                os.environ.pop(key, None)
            else:
                os.environ[key] = value


def _merge_no_proxy(existing: str | None) -> str:
    values = [item.strip() for item in str(existing or "").split(",") if item.strip()]
    seen = {item.lower() for item in values}
    for host in EASTMONEY_NO_PROXY_HOSTS:
        if host.lower() not in seen:
            values.append(host)
            seen.add(host.lower())
    return ",".join(values)


def _eastmoney_session() -> requests.Session:
    session = requests.Session()
    session.trust_env = False
    return session


def _eastmoney_get_json(url: str, params: dict[str, Any], timeout: float = 8) -> dict[str, Any]:
    with _eastmoney_no_proxy(), _eastmoney_session() as session:
        response = session.get(url, params=params, headers=EASTMONEY_HEADERS, timeout=timeout)
        response.raise_for_status()
        data = response.json()
    if data.get("rc") != 0:
        raise RuntimeError(f"东方财富接口返回异常：rc={data.get('rc')} {data.get('rt')}")
    return data


def _eastmoney_quote_secid(symbol: str) -> str:
    code, market = normalize_symbol(symbol)
    market_code = "1" if market == "sh" else "0"
    return f"{market_code}.{code}"


def _eastmoney_quotes(symbols) -> list[Quote]:
    requested = [standard_symbol(symbol) for symbol in symbols]
    secids = ",".join(_eastmoney_quote_secid(symbol) for symbol in requested)
    params = {
        "fltt": "2",
        "invt": "2",
        "ut": EASTMONEY_UT_PARAM,
        "fields": "f1,f2,f3,f4,f5,f6,f7,f8,f9,f10,f12,f13,f14,f15,f16,f17,f18,f20,f21,f23,f115,f152",
        "secids": secids,
    }
    errors: list[str] = []
    for host in EASTMONEY_QUOTE_HOSTS:
        for scheme in EASTMONEY_SCHEMES:
            url = f"{scheme}://{host}/api/qt/ulist.np/get"
            try:
                data = _eastmoney_get_json(url, params)
            except Exception as exc:
                errors.append(str(exc))
                continue
            rows = data.get("data", {}).get("diff") or []
            quotes = [_eastmoney_quote_from_row(row) for row in rows]
            by_symbol = {standard_symbol(f"{item.code}.{item.market}"): item for item in quotes}
            if all(symbol in by_symbol for symbol in requested):
                return [by_symbol[symbol] for symbol in requested]
    if errors:
        raise RuntimeError("东方财富轻量行情不可用：" + "；".join(errors[:2]))
    return []


def _eastmoney_quote_from_row(row: dict[str, Any]) -> Quote:
    code = str(row.get("f12") or "").zfill(6)
    market = normalize_symbol(code)[1].upper()
    price = safe_float(str(row.get("f2") or 0))
    prev_close = safe_float(str(row.get("f18") or 0)) or price
    change = safe_float(str(row.get("f4") or price - prev_close))
    return Quote(
        code=code,
        name=str(row.get("f14") or code),
        market=market,
        price=price,
        prev_close=prev_close,
        open=safe_float(str(row.get("f17") or price)),
        high=safe_float(str(row.get("f15") or price)),
        low=safe_float(str(row.get("f16") or price)),
        volume=safe_float(str(row.get("f5") or 0)),
        amount=safe_float(str(row.get("f6") or 0)),
        change=change,
        change_pct=safe_float(str(row.get("f3") or 0)),
        turnover_rate=safe_float(str(row.get("f8") or 0)) or None,
        pe=safe_float(str(row.get("f9") or row.get("f115") or 0)) or None,
        pb=safe_float(str(row.get("f23") or 0)) or None,
        market_cap=safe_float(str(row.get("f20") or 0)) or None,
        timestamp=now_text(),
        source=AKShareProvider.source_name,
    )


def _eastmoney_kline(symbol: str, period: str, limit: int) -> list[Kline]:
    data = _eastmoney_history_json(symbol, period=period, include_market_cap=True)
    rows = (data.get("data") or {}).get("klines") or []
    result = []
    for item in rows[-limit:]:
        values = str(item).split(",")
        if len(values) < 6:
            continue
        result.append(
            Kline(
                date=values[0],
                open=safe_float(values[1]),
                close=safe_float(values[2]),
                high=safe_float(values[3]),
                low=safe_float(values[4]),
                volume=safe_float(values[5]),
            )
        )
    if not result:
        raise RuntimeError("东方财富日K返回为空")
    return result


def _eastmoney_minute_kline(symbol: str, period: str, interval: str, limit: int) -> list[MinuteKline]:
    data = _eastmoney_history_json(symbol, period=period, include_market_cap=False)
    rows = (data.get("data") or {}).get("klines") or []
    result = []
    for item in rows[-limit:]:
        values = str(item).split(",")
        if len(values) < 7:
            continue
        result.append(
            MinuteKline(
                timestamp=values[0],
                open=safe_float(values[1]),
                close=safe_float(values[2]),
                high=safe_float(values[3]),
                low=safe_float(values[4]),
                volume=safe_float(values[5]),
                amount=safe_float(values[6]) or None,
                turnover_rate=safe_float(values[10]) if len(values) > 10 else None,
                interval=interval,
                source=AKShareProvider.source_name,
            )
        )
    result = [item for item in result if item.timestamp and item.close > 0]
    if not result:
        raise RuntimeError("东方财富分钟K线返回为空")
    return result


def _eastmoney_history_json(symbol: str, period: str, include_market_cap: bool) -> dict[str, Any]:
    fields2 = "f51,f52,f53,f54,f55,f56,f57,f58,f59,f60,f61"
    if include_market_cap:
        fields2 += ",f116"
    params = {
        "fields1": "f1,f2,f3,f4,f5,f6",
        "fields2": fields2,
        "ut": "7eea3edcaed734bea9cbfc24409ed989",
        "klt": period,
        "fqt": "1",
        "secid": _eastmoney_quote_secid(symbol),
        "beg": "19700101" if period == "101" else "0",
        "end": "20500101" if period == "101" else "20500000",
    }
    urls = [
        f"http://{EASTMONEY_HIST_HOST}/api/qt/stock/kline/get",
        f"https://{EASTMONEY_HIST_HOST}/api/qt/stock/kline/get",
    ]
    errors: list[str] = []
    for url in urls:
        try:
            return _eastmoney_get_json(url, params)
        except Exception as exc:
            errors.append(str(exc))
    raise RuntimeError("东方财富历史行情不可用：" + "；".join(errors[:2]))


class TushareProvider:
    source_name = "Tushare Pro"

    async def kline(self, symbol: str, limit: int = 120) -> list[Kline]:
        pro = self._client()

        def load() -> list[Kline]:
            df = pro.daily(ts_code=ts_symbol(symbol))
            rows = df.sort_values("trade_date").tail(limit)
            return [
                Kline(
                    date=f"{str(row['trade_date'])[:4]}-{str(row['trade_date'])[4:6]}-{str(row['trade_date'])[6:8]}",
                    open=safe_float(str(row["open"])),
                    close=safe_float(str(row["close"])),
                    high=safe_float(str(row["high"])),
                    low=safe_float(str(row["low"])),
                    volume=safe_float(str(row["vol"])),
                )
                for _, row in rows.iterrows()
            ]

        return await asyncio.to_thread(load)

    async def stock_pool(self) -> list[StockInfo]:
        pro = self._client()

        def load() -> list[StockInfo]:
            df = pro.stock_basic(exchange="", list_status="L", fields="ts_code,symbol,name,industry,list_date")
            stamp = now_text()
            result = []
            for _, row in df.iterrows():
                symbol = str(row["ts_code"])
                code, market = normalize_symbol(symbol)
                list_date = str(pick(row, "list_date", default=""))
                if len(list_date) == 8:
                    list_date = f"{list_date[:4]}-{list_date[4:6]}-{list_date[6:8]}"
                result.append(
                    StockInfo(
                        symbol=standard_symbol(symbol),
                        code=code,
                        market=market.upper(),
                        name=str(row["name"]),
                        industry=str(pick(row, "industry", default="")) or None,
                        list_date=list_date or None,
                        source=self.source_name,
                        updated_at=stamp,
                    )
                )
            return result

        return await asyncio.to_thread(load)

    def capability(self) -> ProviderCapability:
        installed = is_installed("tushare")
        has_token = bool(os.getenv("TUSHARE_TOKEN"))
        return ProviderCapability(
            name="tushare",
            installed=installed,
            enabled=installed and has_token,
            reliability_level="准正式",
            daily_kline=installed and has_token,
            stock_pool=installed and has_token,
            note="需要 TUSHARE_TOKEN 环境变量；适合个股基础信息、日线和财务数据。",
        )

    @staticmethod
    def _client():
        if not is_installed("tushare"):
            raise RuntimeError("未安装 tushare，请执行 python3 -m pip install tushare")
        token = os.getenv("TUSHARE_TOKEN")
        if not token:
            raise RuntimeError("未配置 TUSHARE_TOKEN，跳过 Tushare 数据源")
        import tushare as ts

        ts.set_token(token)
        return ts.pro_api()


class BaoStockProvider:
    source_name = "BaoStock"

    async def kline(self, symbol: str, limit: int = 120) -> list[Kline]:
        self._ensure_installed()

        def load() -> list[Kline]:
            import baostock as bs

            lg = bs.login()
            if lg.error_code != "0":
                raise RuntimeError(f"BaoStock登录失败：{lg.error_msg}")
            try:
                rs = bs.query_history_k_data_plus(
                    bs_symbol(symbol),
                    "date,open,high,low,close,volume",
                    frequency="d",
                    adjustflag="2",
                )
                rows = []
                while rs.next():
                    rows.append(rs.get_row_data())
                if rs.error_code != "0":
                    raise RuntimeError(f"BaoStock K线失败：{rs.error_msg}")
                return [
                    Kline(
                        date=row[0],
                        open=safe_float(row[1]),
                        high=safe_float(row[2]),
                        low=safe_float(row[3]),
                        close=safe_float(row[4]),
                        volume=safe_float(row[5]),
                    )
                    for row in rows[-limit:]
                ]
            finally:
                bs.logout()

        return await asyncio.to_thread(load)

    async def stock_pool(self) -> list[StockInfo]:
        self._ensure_installed()

        def load() -> list[StockInfo]:
            import baostock as bs

            lg = bs.login()
            if lg.error_code != "0":
                raise RuntimeError(f"BaoStock登录失败：{lg.error_msg}")
            try:
                rs = bs.query_stock_basic()
                rows = []
                while rs.next():
                    rows.append(rs.get_row_data())
                if rs.error_code != "0":
                    raise RuntimeError(f"BaoStock股票池失败：{rs.error_msg}")
                fields = rs.fields
                stamp = now_text()
                result = []
                for row in rows:
                    data = dict(zip(fields, row))
                    raw_code = data.get("code", "")
                    if "." not in raw_code:
                        continue
                    market, code = raw_code.split(".", 1)
                    if len(code) != 6:
                        continue
                    result.append(
                        StockInfo(
                            symbol=standard_symbol(f"{market}{code}"),
                            code=code,
                            market=market.upper(),
                            name=data.get("code_name") or code,
                            industry=None,
                            list_date=data.get("ipoDate") or None,
                            source=self.source_name,
                            updated_at=stamp,
                        )
                    )
                return result
            finally:
                bs.logout()

        return await asyncio.to_thread(load)

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


class FutuProvider:
    source_name = "Futu OpenAPI"

    def __init__(self, host: str = "127.0.0.1", port: int = 11111, enabled: bool = False) -> None:
        self.host = host
        self.port = port
        self.enabled = enabled

    async def quote(self, symbol: str) -> Quote:
        return (await self.quotes([symbol]))[0]

    async def quotes(self, symbols) -> list[Quote]:
        self._ensure_ready()

        def load() -> list[Quote]:
            from futu import OpenQuoteContext, RET_OK

            ctx = OpenQuoteContext(host=self.host, port=self.port)
            try:
                futu_symbols = [self._futu_symbol(symbol) for symbol in symbols]
                ret, data = ctx.get_market_snapshot(futu_symbols)
                if ret != RET_OK:
                    raise RuntimeError(str(data))
                stamp = now_text()
                result = []
                for _, row in data.iterrows():
                    code = str(row["code"]).split(".")[-1]
                    market = "SH" if str(row["code"]).startswith("SH.") else "SZ"
                    price = safe_float(str(pick(row, "last_price", default=0)))
                    prev_close = safe_float(str(pick(row, "prev_close_price", default=0))) or price
                    result.append(
                        Quote(
                            code=code,
                            name=str(pick(row, "stock_name", default=code)),
                            market=market,
                            price=price,
                            prev_close=prev_close,
                            open=safe_float(str(pick(row, "open_price", default=price))),
                            high=safe_float(str(pick(row, "high_price", default=price))),
                            low=safe_float(str(pick(row, "low_price", default=price))),
                            volume=safe_float(str(pick(row, "volume", default=0))),
                            amount=safe_float(str(pick(row, "turnover", default=0))),
                            change=round(price - prev_close, 4),
                            change_pct=safe_float(str(pick(row, "change_rate", default=0))),
                            turnover_rate=safe_float(str(pick(row, "turnover_rate", default=0))) or None,
                            pe=safe_float(str(pick(row, "pe_ratio", default=0))) or None,
                            pb=None,
                            market_cap=None,
                            timestamp=stamp,
                            source=self.source_name,
                        )
                    )
                return result
            finally:
                ctx.close()

        return await asyncio.to_thread(load)

    async def order_book(self, symbol: str) -> OrderBook:
        self._ensure_ready()

        def load() -> OrderBook:
            from futu import OpenQuoteContext, RET_OK

            ctx = OpenQuoteContext(host=self.host, port=self.port)
            try:
                futu_symbol = self._futu_symbol(symbol)
                ret, data = ctx.get_order_book(futu_symbol)
                if ret != RET_OK:
                    raise RuntimeError(str(data))
                code, market = normalize_symbol(symbol)
                bid = [
                    OrderBookLevel(price=safe_float(str(item[0])), volume=safe_float(str(item[1])))
                    for item in data.get("Bid", [])[:5]
                ]
                ask = [
                    OrderBookLevel(price=safe_float(str(item[0])), volume=safe_float(str(item[1])))
                    for item in data.get("Ask", [])[:5]
                ]
                return OrderBook(
                    symbol=standard_symbol(symbol),
                    code=code,
                    market=market.upper(),
                    bid=bid,
                    ask=ask,
                    source=self.source_name,
                    updated_at=now_text(),
                )
            finally:
                ctx.close()

        return await asyncio.to_thread(load)

    async def minute_kline(self, symbol: str, interval: str = "5m", limit: int = 120) -> list[MinuteKline]:
        self._ensure_ready()

        def load() -> list[MinuteKline]:
            from futu import KLType, OpenQuoteContext, RET_OK

            ctx = OpenQuoteContext(host=self.host, port=self.port)
            try:
                ktype = _futu_kltype(KLType, interval)
                ret, data = ctx.get_cur_kline(self._futu_symbol(symbol), num=limit, ktype=ktype)
                if ret != RET_OK:
                    raise RuntimeError(str(data))
                result = []
                for _, row in data.iterrows():
                    result.append(
                        MinuteKline(
                            timestamp=str(pick(row, "time_key", default="")),
                            open=safe_float(str(pick(row, "open", default=0))),
                            close=safe_float(str(pick(row, "close", default=0))),
                            high=safe_float(str(pick(row, "high", default=0))),
                            low=safe_float(str(pick(row, "low", default=0))),
                            volume=safe_float(str(pick(row, "volume", default=0))),
                            amount=safe_float(str(pick(row, "turnover", default=0))) or None,
                            turnover_rate=safe_float(str(pick(row, "turnover_rate", default=0))) or None,
                            interval=interval,
                            source=self.source_name,
                        )
                    )
                return [item for item in result if item.timestamp and item.close > 0]
            finally:
                ctx.close()

        return await asyncio.to_thread(load)

    async def ping(self) -> str:
        self._ensure_ready()

        def load() -> str:
            from futu import OpenQuoteContext

            ctx = OpenQuoteContext(host=self.host, port=self.port)
            try:
                return f"OpenD连接正常：{self.host}:{self.port}"
            finally:
                ctx.close()

        return await asyncio.to_thread(load)

    def capability(self) -> ProviderCapability:
        installed = is_installed("futu")
        return ProviderCapability(
            name="futu",
            installed=installed,
            enabled=installed and self.enabled,
            reliability_level="客户端授权源",
            realtime_quote=installed and self.enabled,
            minute_kline=installed and self.enabled,
            order_book=installed and self.enabled,
            note="需要启动 Futu OpenD；当前只用于个股实时行情和盘口观察，不接交易。",
        )

    def _ensure_ready(self) -> None:
        if not is_installed("futu"):
            raise RuntimeError("未安装 futu-api，请执行 python3 -m pip install futu-api")
        if not self.enabled:
            raise RuntimeError("Futu OpenAPI 未启用。设置 FUTU_ENABLED=1 并启动 Futu OpenD 后再使用")

    @staticmethod
    def _futu_symbol(symbol: str) -> str:
        code, market = normalize_symbol(symbol)
        return f"{market.upper()}.{code}"


def _minute_period(interval: str) -> str:
    mapping = {"1m": "1", "5m": "5", "15m": "15", "30m": "30", "60m": "60"}
    normalized = interval.lower()
    if normalized not in mapping:
        raise ValueError("分钟周期只支持 1m、5m、15m、30m、60m")
    return mapping[normalized]


def _futu_kltype(kltype, interval: str):
    mapping = {
        "1m": "K_1M",
        "3m": "K_3M",
        "5m": "K_5M",
        "10m": "K_10M",
        "15m": "K_15M",
        "30m": "K_30M",
        "60m": "K_60M",
    }
    normalized = interval.lower()
    if normalized not in mapping:
        raise ValueError("Futu 分钟周期只支持 1m、3m、5m、10m、15m、30m、60m")
    return getattr(kltype, mapping[normalized])


class LocalIndividualStockProvider:
    source_name = "本地个股基础数据"

    _stocks = [
        ("600519.SH", "贵州茅台", "白酒"),
        ("000001.SZ", "平安银行", "银行"),
        ("300750.SZ", "宁德时代", "电池"),
        ("601318.SH", "中国平安", "保险"),
        ("000858.SZ", "五粮液", "白酒"),
        ("002594.SZ", "比亚迪", "汽车整车"),
        ("600036.SH", "招商银行", "银行"),
        ("600900.SH", "长江电力", "电力"),
        ("000333.SZ", "美的集团", "家电"),
        ("002475.SZ", "立讯精密", "消费电子"),
    ]

    _plates = [
        ("电池", 2.8, "宁德时代"),
        ("汽车整车", 2.2, "比亚迪"),
        ("消费电子", 1.9, "立讯精密"),
        ("电力", 1.1, "长江电力"),
        ("银行", 0.6, "招商银行"),
        ("白酒", -0.4, "五粮液"),
        ("保险", -0.8, "中国平安"),
        ("家电", 0.3, "美的集团"),
    ]

    _concepts = {
        "600519.SH": [("白酒概念", -0.4, "贵州茅台"), ("MSCI中国", 0.2, "贵州茅台"), ("消费龙头", 0.1, "五粮液")],
        "000001.SZ": [("互联金融", 0.8, "平安银行"), ("破净股", 0.3, "招商银行")],
        "300750.SZ": [("动力电池", 2.8, "宁德时代"), ("储能", 1.6, "阳光电源"), ("新能源汽车", 2.2, "比亚迪")],
        "601318.SH": [("保险", -0.8, "中国平安"), ("大金融", 0.4, "东方财富")],
        "000858.SZ": [("白酒概念", -0.4, "五粮液"), ("消费龙头", 0.1, "贵州茅台")],
        "002594.SZ": [("新能源汽车", 2.2, "比亚迪"), ("刀片电池", 2.1, "比亚迪")],
        "600036.SH": [("银行", 0.6, "招商银行"), ("破净股", 0.3, "招商银行")],
        "600900.SH": [("水电", 1.1, "长江电力"), ("高股息", 0.7, "中国神华")],
        "000333.SZ": [("家电", 0.3, "美的集团"), ("机器人概念", 1.2, "汇川技术")],
        "002475.SZ": [("消费电子", 1.9, "立讯精密"), ("苹果概念", 1.4, "立讯精密")],
    }

    async def stock_pool(self) -> list[StockInfo]:
        stamp = now_text()
        rows = []
        for symbol, name, industry in self._stocks:
            code, market = normalize_symbol(symbol)
            rows.append(
                StockInfo(
                    symbol=standard_symbol(symbol),
                    code=code,
                    market=market.upper(),
                    name=name,
                    industry=industry,
                    source=self.source_name,
                    updated_at=stamp,
                )
            )
        return rows

    async def plate_rank(self, limit: int = 20) -> list[PlateItem]:
        stamp = now_text()
        return [
            PlateItem(
                rank=index,
                name=name,
                change_pct=change_pct,
                amount=None,
                turnover_rate=None,
                leading_stock=leading_stock,
                leading_stock_change_pct=None,
                source=self.source_name,
                updated_at=stamp,
            )
            for index, (name, change_pct, leading_stock) in enumerate(self._plates[:limit], start=1)
        ]

    async def stock_concepts(self, symbol: str, limit: int = 8) -> list[StockConceptItem]:
        normalized = standard_symbol(symbol)
        stamp = now_text()
        return [
            StockConceptItem(
                symbol=normalized,
                rank=index,
                name=name,
                change_pct=change_pct,
                amount=None,
                turnover_rate=None,
                leading_stock=leading_stock,
                leading_stock_change_pct=None,
                match_reason="本地兜底概念归属",
                source=self.source_name,
                updated_at=stamp,
            )
            for index, (name, change_pct, leading_stock) in enumerate(self._concepts.get(normalized, [])[:limit], start=1)
        ]

    def capability(self) -> ProviderCapability:
        return ProviderCapability(
            name="local",
            installed=True,
            enabled=True,
            reliability_level="本地基础数据",
            stock_pool=True,
            plate_rank=True,
            concept_board=True,
            note="本地兜底数据，只用于外部数据源失败时保持个股界面可用。",
        )


def _stock_concepts_from_em(ak, normalized: str, code: str, stamp: str, limit: int) -> list[StockConceptItem]:
    board_df = ak.stock_board_concept_name_em()
    result: list[StockConceptItem] = []
    for rank, (_, row) in enumerate(board_df.head(80).iterrows(), start=1):
        name = str(pick(row, "板块名称", "名称", default="")).strip()
        if not name:
            continue
        board_code = str(pick(row, "板块代码", default="")).strip()
        try:
            cons = ak.stock_board_concept_cons_em(symbol=board_code or name)
        except Exception:
            continue
        if not _concept_constituents_contain(cons, code):
            continue
        result.append(
            StockConceptItem(
                symbol=normalized,
                rank=len(result) + 1,
                name=name,
                change_pct=safe_float(str(pick(row, "涨跌幅", default=0))),
                amount=safe_float(str(pick(row, "成交额", "总市值", default=0))) or None,
                turnover_rate=safe_float(str(pick(row, "换手率", default=0))) or None,
                leading_stock=str(pick(row, "领涨股票", default="")) or None,
                leading_stock_change_pct=safe_float(str(pick(row, "领涨股票-涨跌幅", default=0))) or None,
                match_reason="东方财富概念成分匹配",
                source="AKShare·东方财富概念",
                updated_at=stamp,
            )
        )
        if len(result) >= limit:
            break
    return result


def _stock_concepts_from_sina(ak, normalized: str, code: str, stamp: str, limit: int) -> list[StockConceptItem]:
    board_df = ak.stock_sector_spot(indicator="概念")
    result: list[StockConceptItem] = []
    for rank, (_, row) in enumerate(board_df.iterrows(), start=1):
        label = str(pick(row, "label", default="")).strip()
        name = str(pick(row, "板块", "name", default="")).strip()
        if not label or not name:
            continue
        try:
            cons = ak.stock_sector_detail(sector=label)
        except Exception:
            continue
        if not _concept_constituents_contain(cons, code):
            continue
        result.append(
            StockConceptItem(
                symbol=normalized,
                rank=len(result) + 1,
                name=name,
                change_pct=safe_float(str(pick(row, "涨跌幅", default=0))),
                amount=safe_float(str(pick(row, "总成交额", default=0))) or None,
                turnover_rate=None,
                leading_stock=str(pick(row, "股票名称", default="")) or None,
                leading_stock_change_pct=safe_float(str(pick(row, "个股-涨跌幅", default=0))) or None,
                match_reason="新浪概念成分匹配",
                source="AKShare·新浪概念",
                updated_at=stamp,
            )
        )
        if len(result) >= limit:
            break
    return result


def _concept_constituents_contain(df, code: str) -> bool:
    if df is None or getattr(df, "empty", True):
        return False
    candidates = {"代码", "股票代码", "symbol", "代码代码", "证券代码"}
    for column in df.columns:
        if str(column) not in candidates and "代码" not in str(column).lower() and "symbol" not in str(column).lower():
            continue
        values = df[column].astype(str).str.extract(r"(\d{6})", expand=False).dropna()
        if code in set(values):
            return True
    return False
