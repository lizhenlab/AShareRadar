from __future__ import annotations

import asyncio
from collections import Counter
from collections.abc import Awaitable, Iterable, Sequence
from dataclasses import dataclass
from datetime import date, datetime
import math

from app.models.market import Kline, Quote
from app.models.market_scan import MarketScanSeed
from app.services.data_quality_time import (
    latest_expected_daily_kline_date,
    quote_event_time_error,
)
from app.services.market_scan_completion import short_scan_error
from app.services.market_scan_contracts import MarketScanDataHubProtocol
from app.services.market_scan_scoring import completed_market_scan_klines
from app.services.market_scan_universe import (
    FULL_MARKET_MARKETS,
    build_market_scan_universe,
)
from app.services.market_scan_validation import (
    minimum_market_counts,
    resolve_market_scan_stock_pool,
)
from app.utils.market_time import market_local_naive
from app.utils.symbols import standard_symbol


DEFAULT_MARKET_SCAN_PREFLIGHT_TIMEOUT_SECONDS = 30.0
PREFLIGHT_KLINE_ROWS = 5
_PREFERRED_REPRESENTATIVES = {
    "SH": "600519.SH",
    "SZ": "000001.SZ",
    "BJ": "920066.BJ",
}


@dataclass(frozen=True)
class MarketScanPreflightCheck:
    capability: str
    ok: bool
    detail: str


@dataclass(frozen=True)
class MarketScanPreflightReport:
    checks: tuple[MarketScanPreflightCheck, ...]
    representatives: tuple[str, ...] = ()

    @property
    def ok(self) -> bool:
        return bool(self.checks) and all(check.ok for check in self.checks)


async def run_market_scan_preflight(
    datahub: MarketScanDataHubProtocol,
    *,
    current: datetime,
    timeout_seconds: float,
    sensitive_values: Iterable[object] = (),
) -> MarketScanPreflightReport:
    current = market_local_naive(current)
    timeout = positive_preflight_timeout(timeout_seconds)
    deadline = asyncio.get_running_loop().time() + timeout
    pool_check, representatives = await _bounded_pool_check(
        datahub,
        current=current,
        deadline=deadline,
        timeout=timeout,
        sensitive_values=tuple(sensitive_values),
    )
    if not pool_check.ok:
        return _blocked_preflight_report(pool_check)
    checks = await _representative_checks(
        datahub,
        representatives,
        current=current,
        deadline=deadline,
        timeout=timeout,
        sensitive_values=tuple(sensitive_values),
    )
    return MarketScanPreflightReport(
        checks=(pool_check, *checks),
        representatives=tuple(representatives[market] for market in sorted(representatives)),
    )


async def _bounded_pool_check(
    datahub: MarketScanDataHubProtocol,
    *,
    current: datetime,
    deadline: float,
    timeout: float,
    sensitive_values: tuple[object, ...],
) -> tuple[MarketScanPreflightCheck, dict[str, str]]:
    remaining = deadline - asyncio.get_running_loop().time()
    if remaining <= 0:
        return _timeout_check("stock_pool", timeout), {}
    try:
        representatives, detail = await asyncio.wait_for(
            _load_preflight_representatives(datahub, current=current),
            timeout=remaining,
        )
    except TimeoutError:
        return _timeout_check("stock_pool", timeout), {}
    except asyncio.CancelledError:
        raise
    except Exception as exc:
        return _failed_check("stock_pool", exc, sensitive_values), {}
    return MarketScanPreflightCheck("stock_pool", True, detail), representatives


async def _load_preflight_representatives(
    datahub: MarketScanDataHubProtocol,
    *,
    current: datetime,
) -> tuple[dict[str, str], str]:
    counts = minimum_market_counts(datahub.settings)
    rows, source, resolved = await resolve_market_scan_stock_pool(
        datahub,
        required_markets=FULL_MARKET_MARKETS,
        minimum_counts=counts,
    )
    if not resolved or source == "stale-fallback":
        raise RuntimeError(f"股票池刷新未获得三市场实时结果：{source or 'unresolved'}")
    universe = build_market_scan_universe(
        rows,
        data_date=latest_expected_daily_kline_date(current),
        new_stock_days=int(getattr(datahub.settings, "market_scan_new_stock_days")),
    )
    _validate_preflight_universe(universe.seeds, counts, datahub.settings)
    representatives = _select_market_representatives(universe.seeds)
    market_counts = Counter(seed.market for seed in universe.seeds)
    detail = f"刷新 {len(universe.seeds)} 只（SH {market_counts['SH']} / SZ {market_counts['SZ']} / BJ {market_counts['BJ']}），来源 {source or 'provider-refresh'}"
    return representatives, detail


def _validate_preflight_universe(
    seeds: Sequence[MarketScanSeed],
    minimum_counts: dict[str, int],
    settings: object,
) -> None:
    market_counts = Counter(seed.market for seed in seeds)
    missing = sorted(FULL_MARKET_MARKETS - set(market_counts))
    if missing:
        raise RuntimeError("刷新股票池缺少市场：" + ",".join(missing))
    minimum_total = int(getattr(settings, "market_scan_min_universe_count"))
    if len(seeds) < minimum_total:
        raise RuntimeError(f"刷新股票池有效数量不足：{len(seeds)}/{minimum_total}")
    insufficient = [
        f"{market} {market_counts[market]}/{minimum}"
        for market, minimum in minimum_counts.items()
        if market_counts[market] < minimum
    ]
    if insufficient:
        raise RuntimeError("刷新股票池分市场覆盖不足：" + "，".join(insufficient))


def _select_market_representatives(seeds: Sequence[MarketScanSeed]) -> dict[str, str]:
    selected: dict[str, str] = {}
    for market in sorted(FULL_MARKET_MARKETS):
        candidates = [seed for seed in seeds if seed.market == market and not seed.is_st and not seed.is_new]
        if not candidates:
            candidates = [seed for seed in seeds if seed.market == market and not seed.is_st]
        if not candidates:
            raise RuntimeError(f"{market} 股票池没有可用于预检的代表股")
        preferred = _PREFERRED_REPRESENTATIVES[market]
        candidates.sort(key=lambda seed: (seed.symbol != preferred, seed.list_date is None, seed.list_date or "", seed.symbol))
        selected[market] = candidates[0].symbol
    return selected


async def _representative_checks(
    datahub: MarketScanDataHubProtocol,
    representatives: dict[str, str],
    *,
    current: datetime,
    deadline: float,
    timeout: float,
    sensitive_values: tuple[object, ...],
) -> tuple[MarketScanPreflightCheck, ...]:
    tasks = _create_representative_tasks(
        datahub,
        representatives,
        current=current,
        sensitive_values=sensitive_values,
    )
    remaining = max(0.0, deadline - asyncio.get_running_loop().time())
    done, pending = await asyncio.wait(tasks.values(), timeout=remaining)
    results = {name: task.result() for name, task in tasks.items() if task in done}
    for name, task in tasks.items():
        if task in pending:
            task.cancel()
            results[name] = _timeout_check(name, timeout)
    if pending:
        await asyncio.gather(*pending, return_exceptions=True)
    return tuple(results[name] for name in tasks)


def _create_representative_tasks(
    datahub: MarketScanDataHubProtocol,
    representatives: dict[str, str],
    *,
    current: datetime,
    sensitive_values: tuple[object, ...],
) -> dict[str, asyncio.Task[MarketScanPreflightCheck]]:
    symbols = tuple(representatives[market] for market in sorted(representatives))
    tasks = {
        "quote": asyncio.create_task(
            _safe_preflight_check(
                "quote",
                _check_representative_quotes(datahub, symbols, current=current),
                sensitive_values,
            )
        )
    }
    for market, symbol in sorted(representatives.items()):
        name = f"kline.{market}"
        tasks[name] = asyncio.create_task(
            _safe_preflight_check(
                name,
                _check_representative_kline(datahub, symbol, current=current),
                sensitive_values,
            )
        )
    return tasks


async def _safe_preflight_check(
    capability: str,
    check: Awaitable[str],
    sensitive_values: tuple[object, ...],
) -> MarketScanPreflightCheck:
    try:
        detail = await check
    except asyncio.CancelledError:
        raise
    except Exception as exc:
        return _failed_check(capability, exc, sensitive_values)
    return MarketScanPreflightCheck(capability, True, str(detail))


async def _check_representative_quotes(
    datahub: MarketScanDataHubProtocol,
    symbols: tuple[str, ...],
    *,
    current: datetime,
) -> str:
    quotes, provider_errors = await datahub.partial_quotes_with_errors(symbols, use_cache=False)
    by_symbol = _quotes_by_symbol(quotes)
    missing = sorted(set(symbols) - set(by_symbol))
    if missing:
        suffix = f"；源诊断 {provider_errors[0]}" if provider_errors else ""
        raise RuntimeError(f"代表股报价缺失：{','.join(missing)}{suffix}")
    stale = [
        f"{symbol}: {error}"
        for symbol, quote in by_symbol.items()
        if (error := quote_event_time_error(quote.timestamp, now=current)) is not None
    ]
    if stale:
        raise RuntimeError("代表股报价不新鲜：" + "；".join(stale))
    sources = "/".join(sorted({quote.source for quote in by_symbol.values()}))
    return f"{len(by_symbol)}/{len(symbols)} 只新鲜报价，来源 {sources or 'unknown'}"


def _quotes_by_symbol(quotes: Iterable[Quote]) -> dict[str, Quote]:
    result: dict[str, Quote] = {}
    for quote in quotes:
        try:
            result[standard_symbol(f"{quote.code}.{quote.market}")] = quote
        except ValueError:
            continue
    return result


async def _check_representative_kline(
    datahub: MarketScanDataHubProtocol,
    symbol: str,
    *,
    current: datetime,
) -> str:
    expected_date = latest_expected_daily_kline_date(current)
    rows = await datahub.kline(
        symbol,
        limit=PREFLIGHT_KLINE_ROWS,
        use_cache=False,
        allow_stale=False,
        require_provider_response=True,
    )
    completed = completed_market_scan_klines(rows, expected_date)
    _validate_completed_klines(symbol, completed, expected_date)
    latest = completed[-1]
    return f"{symbol} 已完成 {len(completed)} 条，截止 {latest.date}，来源 {latest.source or 'unknown'}"


def _validate_completed_klines(symbol: str, rows: list[Kline], expected_date: date) -> None:
    if len(rows) < PREFLIGHT_KLINE_ROWS:
        raise RuntimeError(f"{symbol} 已完成日K不足：{len(rows)}/{PREFLIGHT_KLINE_ROWS}")
    latest = datetime.fromisoformat(rows[-1].date).date()
    if latest != expected_date:
        raise RuntimeError(f"{symbol} 日K截止 {latest.isoformat()}，应为 {expected_date.isoformat()}")
    modes = {row.adjustment_mode for row in rows[-PREFLIGHT_KLINE_ROWS:]}
    if modes != {"qfq"}:
        raise RuntimeError(f"{symbol} 最近日K不是一致的前复权序列：{','.join(sorted(modes))}")


def _blocked_preflight_report(pool_check: MarketScanPreflightCheck) -> MarketScanPreflightReport:
    blocked = tuple(
        MarketScanPreflightCheck(name, False, "股票池预检未通过，未调用该能力")
        for name in ("quote", "kline.SH", "kline.SZ", "kline.BJ")
    )
    return MarketScanPreflightReport(checks=(pool_check, *blocked))


def _failed_check(
    capability: str,
    exc: Exception,
    sensitive_values: tuple[object, ...],
) -> MarketScanPreflightCheck:
    return MarketScanPreflightCheck(
        capability,
        False,
        short_scan_error(exc, sensitive_values=sensitive_values),
    )


def _timeout_check(capability: str, timeout: float) -> MarketScanPreflightCheck:
    return MarketScanPreflightCheck(capability, False, f"预检总预算 {timeout:g} 秒已耗尽")


def positive_preflight_timeout(value: object) -> float:
    try:
        parsed = float(str(value).strip())
    except (TypeError, ValueError):
        return DEFAULT_MARKET_SCAN_PREFLIGHT_TIMEOUT_SECONDS
    if not math.isfinite(parsed) or parsed <= 0:
        return DEFAULT_MARKET_SCAN_PREFLIGHT_TIMEOUT_SECONDS
    return parsed


__all__ = [
    "MarketScanPreflightCheck",
    "MarketScanPreflightReport",
    "positive_preflight_timeout",
    "run_market_scan_preflight",
]
