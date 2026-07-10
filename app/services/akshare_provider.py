from __future__ import annotations

import asyncio
from collections.abc import Callable, Iterable
from contextlib import redirect_stderr
from dataclasses import dataclass
import importlib
import io

from app.models.schemas import Kline, MinuteKline, PlateItem, ProviderCapability, Quote, StockConceptItem, StockInfo
from app.runtime_environment import isolate_user_site_packages
from app.services.akshare_mappers import minute_klines_from_hist_rows, quote_from_spot_row, stock_info_from_code_name_row
from app.services.eastmoney_client import (
    eastmoney_kline as _eastmoney_kline,
    eastmoney_minute_kline as _eastmoney_minute_kline,
    eastmoney_no_proxy as _eastmoney_no_proxy,
    eastmoney_quotes as _eastmoney_quotes,
)
from app.services.provider_utils import ak_symbol, ensure_positive_limit, is_installed, pick, valid_ohlc
from app.utils.parsing import required_float, safe_float
from app.utils.symbols import normalize_symbol, standard_symbol
from app.utils.time import now_text

isolate_user_site_packages()

AKSHARE_MINUTE_PERIODS = {"1m": "1", "5m": "5", "15m": "15", "30m": "30", "60m": "60"}


class AKShareFetchError(RuntimeError):
    """AKShare import or upstream request failed before row parsing."""


@dataclass(frozen=True)
class ConceptBoardCandidate:
    name: str
    lookup_key: str
    change_pct: float
    amount: float | None
    turnover_rate: float | None
    leading_stock: str | None
    leading_stock_change_pct: float | None
    match_reason: str
    source: str


@dataclass
class ConceptMatchStats:
    attempted: int = 0
    failures: list[str] | None = None

    def __post_init__(self) -> None:
        if self.failures is None:
            self.failures = []


class AKShareProvider:
    source_name = "AKShare"

    async def quote(self, symbol: str) -> Quote:
        return (await self.quotes([symbol]))[0]

    async def quotes(self, symbols) -> list[Quote]:
        self._ensure_installed()

        def load() -> list[Quote]:
            direct_quotes, direct_error = _try_eastmoney_quotes(symbols)
            if direct_quotes:
                return direct_quotes
            return _akshare_spot_quotes(_import_akshare(), symbols, self.source_name, direct_error)

        return await asyncio.to_thread(load)

    async def kline(self, symbol: str, limit: int = 120) -> list[Kline]:
        ensure_positive_limit(limit)
        self._ensure_installed()

        def load() -> list[Kline]:
            try:
                df = _akshare_daily_hist_frame(symbol)
            except AKShareFetchError:
                return _eastmoney_kline(symbol, period="101", limit=limit)
            return _akshare_daily_klines_from_frame(df, limit)

        return await asyncio.to_thread(load)

    async def minute_kline(self, symbol: str, interval: str = "5m", limit: int = 120) -> list[MinuteKline]:
        ensure_positive_limit(limit)
        self._ensure_installed()
        period = _minute_period(interval)

        def load() -> list[MinuteKline]:
            try:
                return _akshare_minute_klines(symbol, period, interval, limit, self.source_name)
            except AKShareFetchError:
                return _eastmoney_minute_kline(symbol, period=period, interval=interval, limit=limit)

        return await asyncio.to_thread(load)

    async def stock_pool(self) -> list[StockInfo]:
        self._ensure_installed()

        def load() -> list[StockInfo]:
            ak = _import_akshare()

            with _eastmoney_no_proxy():
                df = ak.stock_info_a_code_name()
            stamp = now_text()
            result = []
            for _, row in df.iterrows():
                item = stock_info_from_code_name_row(row, stamp=stamp, source_name=self.source_name)
                if item:
                    result.append(item)
            return result

        return await asyncio.to_thread(load)

    async def plate_rank(self, limit: int = 20) -> list[PlateItem]:
        ensure_positive_limit(limit)
        self._ensure_installed()

        def load() -> list[PlateItem]:
            ak = _import_akshare()

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
        ensure_positive_limit(limit)
        self._ensure_installed()
        normalized = standard_symbol(symbol)
        code, _ = normalize_symbol(symbol)

        def load() -> list[StockConceptItem]:
            ak = _import_akshare()

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


def _import_akshare():
    try:
        with redirect_stderr(io.StringIO()):
            return importlib.import_module("akshare")
    except Exception as exc:
        raise AKShareFetchError(f"AKShare 依赖不可用：{exc}") from exc


def _minute_period(interval: str) -> str:
    normalized = interval.lower()
    if normalized not in AKSHARE_MINUTE_PERIODS:
        raise ValueError("分钟周期只支持 1m、5m、15m、30m、60m")
    return AKSHARE_MINUTE_PERIODS[normalized]


def _try_eastmoney_quotes(symbols) -> tuple[list[Quote] | None, str]:
    try:
        result = _eastmoney_quotes(symbols)
    except Exception as exc:
        return None, str(exc)
    return (result if result else None), ""


def _akshare_minute_klines(symbol: str, period: str, interval: str, limit: int, source_name: str) -> list[MinuteKline]:
    ensure_positive_limit(limit)
    df = _akshare_minute_hist_frame(symbol, period)
    return minute_klines_from_hist_rows((row for _, row in df.tail(limit).iterrows()), interval=interval, source_name=source_name)


def _akshare_daily_hist_frame(symbol: str):
    try:
        ak = _import_akshare()
    except Exception as exc:
        raise AKShareFetchError(f"AKShare 依赖不可用：{exc}") from exc
    try:
        with _eastmoney_no_proxy():
            return ak.stock_zh_a_hist(symbol=ak_symbol(symbol), period="daily", adjust="qfq")
    except Exception as exc:
        raise AKShareFetchError(f"AKShare日K请求失败：{exc}") from exc


def _akshare_minute_hist_frame(symbol: str, period: str):
    try:
        ak = _import_akshare()
    except Exception as exc:
        raise AKShareFetchError(f"AKShare 依赖不可用：{exc}") from exc
    try:
        with _eastmoney_no_proxy():
            return ak.stock_zh_a_hist_min_em(symbol=ak_symbol(symbol), period=period, adjust="qfq")
    except Exception as exc:
        raise AKShareFetchError(f"AKShare分钟K线请求失败：{exc}") from exc


def _akshare_daily_klines_from_frame(frame, limit: int) -> list[Kline]:
    ensure_positive_limit(limit)
    rows = [_akshare_daily_kline_from_row(row) for _, row in frame.tail(limit).iterrows()]
    result = [row for row in rows if row is not None]
    if not result:
        raise RuntimeError("AKShare日K返回为空或全部无效")
    return result


def _akshare_daily_kline_from_row(row) -> Kline | None:
    try:
        item = Kline(
            date=str(row["日期"]),
            open=required_float(row["开盘"], "AKShare日K开盘价", positive=True),
            close=required_float(row["收盘"], "AKShare日K收盘价", positive=True),
            high=required_float(row["最高"], "AKShare日K最高价", positive=True),
            low=required_float(row["最低"], "AKShare日K最低价", positive=True),
            volume=safe_float(str(row["成交量"])),
            source="AKShare",
        )
    except KeyError as exc:
        raise RuntimeError(f"AKShare日K字段缺失：{exc}") from exc
    except ValueError:
        return None
    return item if valid_ohlc(item.open, item.close, item.high, item.low) else None


def _akshare_spot_quotes(ak, symbols, source_name: str, direct_error: str = "") -> list[Quote]:
    try:
        with _eastmoney_no_proxy():
            df = ak.stock_zh_a_spot_em()
    except Exception as exc:
        detail = f"；轻量直连失败：{direct_error}" if direct_error else ""
        raise RuntimeError(f"AKShare实时行情失败：{exc}{detail}") from exc
    return _ordered_spot_quotes(df, _requested_ak_codes(symbols), source_name)


def _requested_ak_codes(symbols) -> list[str]:
    return list(dict.fromkeys(ak_symbol(symbol) for symbol in symbols))


def _ordered_spot_quotes(df, wanted_codes: list[str], source_name: str) -> list[Quote]:
    by_code = _spot_quotes_by_code(df, set(wanted_codes), source_name)
    missing = [code for code in wanted_codes if code not in by_code]
    if missing:
        raise RuntimeError(f"AKShare实时行情缺少代码：{','.join(sorted(missing))}")
    return [by_code[code] for code in wanted_codes]


def _spot_quotes_by_code(df, wanted: set[str], source_name: str) -> dict[str, Quote]:
    stamp = now_text()
    by_code: dict[str, Quote] = {}
    for _, row in df.iterrows():
        quote = quote_from_spot_row(row, stamp=stamp, source_name=source_name)
        if quote and quote.code in wanted and quote.code not in by_code:
            by_code[quote.code] = quote
    return by_code


def _stock_concepts_from_em(ak, normalized: str, code: str, stamp: str, limit: int) -> list[StockConceptItem]:
    candidates = (_em_concept_candidate(row) for _, row in ak.stock_board_concept_name_em().head(80).iterrows())
    return _matched_concept_items(
        candidates,
        lambda candidate: ak.stock_board_concept_cons_em(symbol=candidate.lookup_key),
        normalized,
        code,
        stamp,
        limit,
    )


def _stock_concepts_from_sina(ak, normalized: str, code: str, stamp: str, limit: int) -> list[StockConceptItem]:
    candidates = (_sina_concept_candidate(row) for _, row in ak.stock_sector_spot(indicator="概念").iterrows())
    return _matched_concept_items(
        candidates,
        lambda candidate: ak.stock_sector_detail(sector=candidate.lookup_key),
        normalized,
        code,
        stamp,
        limit,
    )


def _em_concept_candidate(row) -> ConceptBoardCandidate | None:
    return _concept_candidate(
        row,
        name_fields=("板块名称", "名称"),
        lookup_fields=("板块代码",),
        amount_fields=("成交额", "总市值"),
        turnover_fields=("换手率",),
        leading_stock_fields=("领涨股票",),
        leading_stock_change_fields=("领涨股票-涨跌幅",),
        match_reason="东方财富概念成分匹配",
        source="AKShare·东方财富概念",
    )


def _sina_concept_candidate(row) -> ConceptBoardCandidate | None:
    return _concept_candidate(
        row,
        name_fields=("板块", "name"),
        lookup_fields=("label",),
        amount_fields=("总成交额",),
        turnover_fields=(),
        leading_stock_fields=("股票名称",),
        leading_stock_change_fields=("个股-涨跌幅",),
        match_reason="新浪概念成分匹配",
        source="AKShare·新浪概念",
        require_lookup=True,
    )


def _concept_candidate(
    row,
    *,
    name_fields: tuple[str, ...],
    lookup_fields: tuple[str, ...],
    amount_fields: tuple[str, ...],
    turnover_fields: tuple[str, ...],
    leading_stock_fields: tuple[str, ...],
    leading_stock_change_fields: tuple[str, ...],
    match_reason: str,
    source: str,
    require_lookup: bool = False,
) -> ConceptBoardCandidate | None:
    name = _row_text(row, *name_fields)
    explicit_lookup = _row_text(row, *lookup_fields)
    if not name or (require_lookup and not explicit_lookup):
        return None
    lookup_key = explicit_lookup or name
    return ConceptBoardCandidate(
        name=name,
        lookup_key=lookup_key,
        change_pct=_row_number(row, "涨跌幅"),
        amount=_optional_row_number(row, *amount_fields),
        turnover_rate=_optional_row_number(row, *turnover_fields),
        leading_stock=_row_text(row, *leading_stock_fields) or None,
        leading_stock_change_pct=_optional_row_number(row, *leading_stock_change_fields),
        match_reason=match_reason,
        source=source,
    )


def _row_text(row, *fields: str) -> str:
    return str(pick(row, *fields, default="")).strip()


def _row_number(row, *fields: str) -> float:
    return safe_float(str(pick(row, *fields, default=0)))


def _optional_row_number(row, *fields: str) -> float | None:
    if not fields:
        return None
    return _row_number(row, *fields) or None


def _matched_concept_items(
    candidates: Iterable[ConceptBoardCandidate | None],
    constituents_loader: Callable[[ConceptBoardCandidate], object],
    normalized: str,
    code: str,
    stamp: str,
    limit: int,
) -> list[StockConceptItem]:
    result: list[StockConceptItem] = []
    stats = ConceptMatchStats()
    for candidate in candidates:
        if candidate is None:
            continue
        if not _concept_candidate_matches(candidate, constituents_loader, code, stats):
            continue
        result.append(_stock_concept_item(candidate, normalized, stamp, len(result) + 1))
        if len(result) >= limit:
            break
    _raise_if_all_concept_loads_failed(stats, result)
    return result


def _concept_candidate_matches(
    candidate: ConceptBoardCandidate,
    constituents_loader: Callable[[ConceptBoardCandidate], object],
    code: str,
    stats: ConceptMatchStats,
) -> bool:
    stats.attempted += 1
    try:
        constituents = constituents_loader(candidate)
    except Exception as exc:
        assert stats.failures is not None
        stats.failures.append(f"{candidate.name}: {_short_error(exc)}")
        return False
    return _concept_constituents_contain(constituents, code)


def _raise_if_all_concept_loads_failed(stats: ConceptMatchStats, result: list[StockConceptItem]) -> None:
    failures = stats.failures or []
    if stats.attempted and failures and len(failures) == stats.attempted and not result:
        raise RuntimeError("概念成分源不可用：" + "；".join(failures[:3]))


def _stock_concept_item(candidate: ConceptBoardCandidate, normalized: str, stamp: str, rank: int) -> StockConceptItem:
    return StockConceptItem(
        symbol=normalized,
        rank=rank,
        name=candidate.name,
        change_pct=candidate.change_pct,
        amount=candidate.amount,
        turnover_rate=candidate.turnover_rate,
        leading_stock=candidate.leading_stock,
        leading_stock_change_pct=candidate.leading_stock_change_pct,
        match_reason=candidate.match_reason,
        source=candidate.source,
        updated_at=stamp,
    )


def _concept_constituents_contain(df, code: str) -> bool:
    if df is None or getattr(df, "empty", True):
        return False
    for column in df.columns:
        if not _concept_code_column(column):
            continue
        values = df[column].astype(str).str.extract(r"(\d{6})", expand=False).dropna()
        if code in set(values):
            return True
    return False


def _concept_code_column(column) -> bool:
    name = str(column)
    lowered = name.lower()
    return name in CONCEPT_CODE_COLUMNS or "代码" in lowered or "symbol" in lowered


def _short_error(exc: Exception) -> str:
    text = str(exc).strip()
    return text[:140] if text else exc.__class__.__name__


CONCEPT_CODE_COLUMNS = {"代码", "股票代码", "symbol", "代码代码", "证券代码"}
