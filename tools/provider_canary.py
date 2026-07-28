from __future__ import annotations

import argparse
import asyncio
from collections.abc import Callable, Iterable, Mapping, Sequence
from contextlib import redirect_stderr
from datetime import date
import io
import json
import math
import os
from pathlib import Path
import sys
from tempfile import TemporaryDirectory
from typing import Any, Protocol, cast


ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from app.config import Settings, get_settings  # noqa: E402
from app.services.datahub import DataHub  # noqa: E402
from app.utils.audit_time import audit_now_text  # noqa: E402
from app.utils.clock import market_now, monotonic_now  # noqa: E402
from app.utils.market_time import market_datetime_epoch  # noqa: E402
from app.utils.provider_errors import sanitize_provider_error  # noqa: E402
from app.utils.symbols import normalize_symbol, standard_symbol  # noqa: E402


MARKETS = ("SH", "SZ", "BJ")
DEFAULT_SYMBOLS = {
    "SH": "600519.SH",
    "SZ": "000001.SZ",
    "BJ": "920066.BJ",
}
DEFAULT_REQUEST_TIMEOUT_SECONDS = 8.0
DEFAULT_STOCK_POOL_TIMEOUT_SECONDS = 75.0
DEFAULT_OVERALL_TIMEOUT_SECONDS = 20.0
DEFAULT_CLEANUP_TIMEOUT_SECONDS = 3.0
CANARY_KLINE_LIMIT = 5
MAX_DATA_AGE_DAYS = 10
MAX_FUTURE_SKEW_SECONDS = 5 * 60
MAX_ERROR_LENGTH = 500

EXIT_SUCCESS = 0
EXIT_FAILURE = 1
EXIT_PARTIAL_FAILURE = 2

_SENSITIVE_NAME_MARKERS = (
    "api_key",
    "authorization",
    "base_url",
    "credential",
    "password",
    "secret",
    "signature",
    "token",
)


class CanaryDataHub(Protocol):
    async def quote(self, symbol: str, use_cache: bool = True) -> object: ...

    async def kline(
        self,
        symbol: str,
        limit: int = 120,
        use_cache: bool = True,
        *,
        allow_stale: bool = False,
        require_provider_response: bool = False,
    ) -> list[object]: ...

    async def stock_pool(
        self,
        keyword: str | None = None,
        limit: int | None = 5000,
        refresh: bool = False,
        required_markets: Iterable[str] | None = None,
        minimum_market_counts: Mapping[str, int] | None = None,
    ) -> list[object]: ...

    async def aclose(self, timeout: float) -> bool: ...


DataHubFactory = Callable[[Settings], CanaryDataHub]


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Run an optional bounded canary against configured A-share quote providers.",
    )
    parser.add_argument(
        "--sh-symbol",
        default=os.getenv("ASHARE_RADAR_CANARY_SH_SYMBOL", DEFAULT_SYMBOLS["SH"]),
        help="Shanghai representative symbol",
    )
    parser.add_argument(
        "--sz-symbol",
        default=os.getenv("ASHARE_RADAR_CANARY_SZ_SYMBOL", DEFAULT_SYMBOLS["SZ"]),
        help="Shenzhen representative symbol",
    )
    parser.add_argument(
        "--bj-symbol",
        default=os.getenv("ASHARE_RADAR_CANARY_BJ_SYMBOL", DEFAULT_SYMBOLS["BJ"]),
        help="Beijing representative symbol",
    )
    parser.add_argument(
        "--request-timeout",
        type=_positive_float,
        default=None,
        metavar="SECONDS",
        help="Per-market timeout; defaults to Settings.provider_call_timeout_seconds",
    )
    parser.add_argument(
        "--stock-pool-timeout",
        type=_positive_float,
        default=None,
        metavar="SECONDS",
        help="Stock-pool timeout; defaults to Settings.stock_pool_provider_timeout_seconds",
    )
    parser.add_argument(
        "--overall-timeout",
        type=_positive_float,
        default=DEFAULT_OVERALL_TIMEOUT_SECONDS,
        metavar="SECONDS",
        help="Deadline for all concurrent probes",
    )
    return parser


def resolve_market_symbols(values: Mapping[str, str]) -> dict[str, str]:
    resolved: dict[str, str] = {}
    for market in MARKETS:
        symbol = standard_symbol(values[market])
        _, actual_market = normalize_symbol(symbol)
        if actual_market.upper() != market:
            raise ValueError(f"{market} representative symbol belongs to {actual_market.upper()}")
        resolved[market] = symbol
    return resolved


async def run_canary(
    settings: Settings,
    *,
    symbols: Mapping[str, str],
    request_timeout: float,
    overall_timeout: float,
    stock_pool_timeout: float | None = None,
    datahub_factory: DataHubFactory | None = None,
) -> dict[str, Any]:
    request_timeout = _validated_timeout(request_timeout, "request_timeout")
    stock_pool_timeout = _validated_timeout(
        stock_pool_timeout
        if stock_pool_timeout is not None
        else float(settings.stock_pool_provider_timeout_seconds),
        "stock_pool_timeout",
    )
    overall_timeout = _validated_timeout(overall_timeout, "overall_timeout")
    market_symbols = resolve_market_symbols(symbols)
    sensitive_values = _sensitive_setting_values(settings)
    factory = datahub_factory or _create_datahub
    started_at = audit_now_text()
    started = monotonic_now()
    datahub = factory(settings)

    try:
        results, stock_pool, overall_timed_out = await _run_contract_probes(
            datahub,
            market_symbols,
            request_timeout=request_timeout,
            stock_pool_timeout=stock_pool_timeout,
            overall_timeout=overall_timeout,
            sensitive_values=sensitive_values,
        )
    except BaseException:
        await _close_datahub(datahub, sensitive_values=sensitive_values)
        raise

    cleanup = await _close_datahub(
        datahub,
        sensitive_values=sensitive_values,
    )
    return _build_canary_summary(
        started_at=started_at,
        started=started,
        request_timeout=request_timeout,
        stock_pool_timeout=stock_pool_timeout,
        overall_timeout=overall_timeout,
        overall_timed_out=overall_timed_out,
        results=results,
        stock_pool=stock_pool,
        cleanup=cleanup,
    )


def _build_canary_summary(
    *,
    started_at: str,
    started: float,
    request_timeout: float,
    stock_pool_timeout: float,
    overall_timeout: float,
    overall_timed_out: bool,
    results: Mapping[str, dict[str, Any]],
    stock_pool: dict[str, Any],
    cleanup: dict[str, Any],
) -> dict[str, Any]:
    success_count = sum(result["status"] == "success" for result in results.values())
    degraded_count = sum(result["status"] == "degraded" for result in results.values())
    available_count = success_count + degraded_count
    failure_count = len(MARKETS) - available_count
    exit_code = _contract_exit_code(
        available_market_count=available_count,
        stock_pool_available=stock_pool["status"] in {"success", "degraded"},
    )
    if cleanup["status"] != "success":
        exit_code = EXIT_FAILURE
    return {
        "schema_version": 1,
        "tool": "provider_canary",
        "started_at": started_at,
        "finished_at": audit_now_text(),
        "duration_ms": _elapsed_ms(started),
        "request_timeout_seconds": request_timeout,
        "stock_pool_timeout_seconds": stock_pool_timeout,
        "overall_timeout_seconds": overall_timeout,
        "overall_timed_out": overall_timed_out,
        "success_count": success_count,
        "degraded_count": degraded_count,
        "available_count": available_count,
        "failure_count": failure_count,
        "exit_code": exit_code,
        "markets": results,
        "stock_pool": stock_pool,
        "cleanup": cleanup,
    }


async def _run_contract_probes(
    datahub: CanaryDataHub,
    symbols: Mapping[str, str],
    *,
    request_timeout: float,
    stock_pool_timeout: float,
    overall_timeout: float,
    sensitive_values: tuple[object, ...],
) -> tuple[dict[str, dict[str, Any]], dict[str, Any], bool]:
    started = monotonic_now()
    market_tasks = {
        market: asyncio.create_task(
            _probe_market(
                datahub,
                market=market,
                symbol=symbols[market],
                request_timeout=request_timeout,
                sensitive_values=sensitive_values,
            ),
            name=f"provider-canary-{market.lower()}",
        )
        for market in MARKETS
    }
    stock_pool_task = asyncio.create_task(
        _probe_stock_pool(datahub, stock_pool_timeout=stock_pool_timeout, sensitive_values=sensitive_values),
        name="provider-canary-stock-pool",
    )
    all_tasks = (*market_tasks.values(), stock_pool_task)
    try:
        _, pending = await asyncio.wait(all_tasks, timeout=overall_timeout)
        timed_out = frozenset(pending)
        if pending:
            for task in pending:
                task.cancel()
            await asyncio.gather(*pending, return_exceptions=True)
        results = {
            market: (
                _timeout_result(
                    market=market,
                    symbol=symbols[market],
                    timeout_scope="overall",
                    latency_ms=_elapsed_ms(started),
                )
                if task in timed_out
                else task.result()
            )
            for market, task in market_tasks.items()
        }
        stock_pool = (
            _stock_pool_timeout_result("overall", _elapsed_ms(started))
            if stock_pool_task in timed_out
            else stock_pool_task.result()
        )
        return results, stock_pool, bool(timed_out)
    except BaseException:
        await _cancel_and_drain(all_tasks)
        raise


async def _probe_market(
    datahub: CanaryDataHub,
    *,
    market: str,
    symbol: str,
    request_timeout: float,
    sensitive_values: tuple[object, ...],
) -> dict[str, Any]:
    started = monotonic_now()
    try:
        async with asyncio.timeout(request_timeout):
            quote = await datahub.quote(symbol, use_cache=False)
            klines = await datahub.kline(
                symbol,
                limit=CANARY_KLINE_LIMIT,
                use_cache=False,
                allow_stale=False,
                require_provider_response=True,
            )
        return _available_result(
            market=market,
            symbol=symbol,
            quote=quote,
            klines=klines,
            latency_ms=_elapsed_ms(started),
            sensitive_values=sensitive_values,
        )
    except TimeoutError:
        return _timeout_result(
            market=market,
            symbol=symbol,
            timeout_scope="request",
            latency_ms=_elapsed_ms(started),
        )
    except Exception as exc:
        return {
            "market": market,
            "symbol": symbol,
            "status": "error",
            "latency_ms": _elapsed_ms(started),
            "error_type": type(exc).__name__,
            "error": _sanitized_text(exc, sensitive_values=sensitive_values),
        }


def _available_result(
    *,
    market: str,
    symbol: str,
    quote: object,
    klines: Sequence[object],
    latency_ms: float,
    sensitive_values: tuple[object, ...],
) -> dict[str, Any]:
    code, expected_market = normalize_symbol(symbol)
    quote_code = str(getattr(quote, "code", "")).strip()
    quote_market = str(getattr(quote, "market", "")).strip().upper()
    if quote_code != code or quote_market != expected_market.upper():
        raise ValueError(f"quote identity mismatch for {symbol}")
    quote_timestamp = _validate_quote_timestamp(getattr(quote, "timestamp", ""))
    kline_summary = _validate_klines(klines)
    from_cache = bool(getattr(quote, "from_cache", False)) or bool(kline_summary["from_cache"])
    fallback_used = bool(getattr(quote, "fallback_used", False)) or bool(kline_summary["fallback_used"])
    return {
        "market": market,
        "symbol": symbol,
        "status": "degraded" if from_cache or fallback_used else "success",
        "latency_ms": latency_ms,
        "source": _sanitized_text(getattr(quote, "source", "unknown"), sensitive_values=sensitive_values),
        "price": getattr(quote, "price", None),
        "timestamp": quote_timestamp,
        "from_cache": from_cache,
        "fallback_used": fallback_used,
        "kline": kline_summary,
    }


async def _probe_stock_pool(
    datahub: CanaryDataHub,
    *,
    stock_pool_timeout: float,
    sensitive_values: tuple[object, ...],
) -> dict[str, Any]:
    started = monotonic_now()
    try:
        async with asyncio.timeout(stock_pool_timeout):
            rows = await datahub.stock_pool(
                limit=None,
                refresh=True,
                required_markets=MARKETS,
                minimum_market_counts={market: 1 for market in MARKETS},
            )
        return _stock_pool_result(
            rows,
            latency_ms=_elapsed_ms(started),
            sensitive_values=sensitive_values,
        )
    except TimeoutError:
        return _stock_pool_timeout_result("request", _elapsed_ms(started))
    except Exception as exc:
        return {
            "status": "error",
            "latency_ms": _elapsed_ms(started),
            "error_type": type(exc).__name__,
            "error": _sanitized_text(exc, sensitive_values=sensitive_values),
        }


def _stock_pool_result(
    rows: Sequence[object],
    *,
    latency_ms: float,
    sensitive_values: tuple[object, ...],
) -> dict[str, Any]:
    if not rows:
        raise ValueError("stock pool is empty")
    symbols: set[str] = set()
    market_counts = {market: 0 for market in MARKETS}
    fallback_used = False
    for row in rows:
        symbol = standard_symbol(str(getattr(row, "symbol", "")))
        code, market = normalize_symbol(symbol)
        row_code = str(getattr(row, "code", "")).strip()
        row_market = str(getattr(row, "market", "")).strip().upper()
        if row_code != code or row_market != market.upper():
            raise ValueError(f"stock pool identity mismatch for {symbol}")
        if symbol in symbols:
            raise ValueError(f"stock pool contains duplicate symbol {symbol}")
        symbols.add(symbol)
        if row_market in market_counts:
            market_counts[row_market] += 1
        fallback_used = fallback_used or bool(getattr(row, "fallback_used", False))
    missing_markets = [market for market, count in market_counts.items() if count == 0]
    if missing_markets:
        raise ValueError(f"stock pool is missing markets: {','.join(missing_markets)}")
    sources = sorted(
        {
            _sanitized_text(getattr(row, "source", "unknown"), sensitive_values=sensitive_values)
            for row in rows
        }
    )
    return {
        "status": "degraded" if fallback_used else "success",
        "latency_ms": latency_ms,
        "row_count": len(rows),
        "market_counts": market_counts,
        "source_count": len(sources),
        "fallback_used": fallback_used,
    }


def _validate_quote_timestamp(value: object) -> str:
    timestamp = str(value or "").strip()
    epoch = market_datetime_epoch(timestamp)
    if epoch is None:
        raise ValueError("quote timestamp is missing or invalid")
    age_seconds = market_now().timestamp() - epoch
    if age_seconds < -MAX_FUTURE_SKEW_SECONDS:
        raise ValueError("quote timestamp is in the future")
    if age_seconds > MAX_DATA_AGE_DAYS * 24 * 60 * 60:
        raise ValueError("quote timestamp is stale")
    return timestamp


def _validate_klines(rows: Sequence[object]) -> dict[str, Any]:
    if len(rows) != CANARY_KLINE_LIMIT:
        detail = "truncated" if len(rows) < CANARY_KLINE_LIMIT else "exceeded requested limit"
        raise ValueError(
            f"daily kline is {detail}: expected exactly {CANARY_KLINE_LIMIT} rows, got {len(rows)}"
        )
    validated = [_validate_kline_row(row) for row in rows]
    dates = [item[0] for item in validated]
    _validate_kline_dates(dates)
    from_cache = any(item[1] for item in validated)
    fallback_used = any(item[2] for item in validated)
    sources = {item[3] for item in validated}
    return {
        "row_count": len(rows),
        "first_date": dates[0].isoformat(),
        "latest_date": dates[-1].isoformat(),
        "source_count": len(sources),
        "from_cache": from_cache,
        "fallback_used": fallback_used,
    }


def _validate_kline_row(row: object) -> tuple[date, bool, bool, str]:
    try:
        row_date = date.fromisoformat(str(getattr(row, "date", ""))[:10])
    except ValueError as exc:
        raise ValueError("daily kline contains an invalid date") from exc
    values = [getattr(row, field, None) for field in ("open", "close", "high", "low", "volume")]
    if any(isinstance(value, bool) or not _is_finite_number(value) for value in values):
        raise ValueError("daily kline contains a non-finite OHLCV value")
    if float(cast(Any, values[4])) < 0:
        raise ValueError("daily kline contains a negative volume")
    return (
        row_date,
        bool(getattr(row, "from_cache", False)),
        bool(getattr(row, "fallback_used", False)),
        str(getattr(row, "source", "unknown") or "unknown"),
    )


def _validate_kline_dates(dates: Sequence[date]) -> None:
    if dates != sorted(set(dates)):
        raise ValueError("daily kline dates are duplicated or not strictly increasing")
    today = market_now().date()
    if dates[-1] > today:
        raise ValueError("daily kline latest date is in the future")
    if (today - dates[-1]).days > MAX_DATA_AGE_DAYS:
        raise ValueError("daily kline latest date is stale")


def _is_finite_number(value: object) -> bool:
    try:
        return math.isfinite(float(cast(Any, value)))
    except (TypeError, ValueError):
        return False


def _timeout_result(*, market: str, symbol: str, timeout_scope: str, latency_ms: float) -> dict[str, Any]:
    return {
        "market": market,
        "symbol": symbol,
        "status": "timeout",
        "timeout_scope": timeout_scope,
        "latency_ms": latency_ms,
        "error_type": "TimeoutError",
        "error": f"{timeout_scope} timeout exceeded",
    }


def _stock_pool_timeout_result(timeout_scope: str, latency_ms: float) -> dict[str, Any]:
    return {
        "status": "timeout",
        "timeout_scope": timeout_scope,
        "latency_ms": latency_ms,
        "error_type": "TimeoutError",
        "error": f"{timeout_scope} timeout exceeded",
    }


async def _cancel_and_drain(tasks: Iterable[asyncio.Task[dict[str, Any]]]) -> None:
    task_list = tuple(tasks)
    for task in task_list:
        if not task.done():
            task.cancel()
    if task_list:
        await asyncio.gather(*task_list, return_exceptions=True)


async def _close_datahub(
    datahub: CanaryDataHub,
    *,
    sensitive_values: tuple[object, ...],
) -> dict[str, Any]:
    close_timeout = DEFAULT_CLEANUP_TIMEOUT_SECONDS
    started = monotonic_now()
    try:
        async with asyncio.timeout(close_timeout):
            closed = await datahub.aclose(timeout=close_timeout)
        if not closed:
            return {
                "status": "error",
                "latency_ms": _elapsed_ms(started),
                "error_type": "TimeoutError",
                "error": "provider cleanup did not finish within its deadline",
            }
        return {"status": "success", "latency_ms": _elapsed_ms(started)}
    except TimeoutError:
        return {
            "status": "error",
            "latency_ms": _elapsed_ms(started),
            "error_type": "TimeoutError",
            "error": "provider cleanup timeout exceeded",
        }
    except Exception as exc:
        return {
            "status": "error",
            "latency_ms": _elapsed_ms(started),
            "error_type": type(exc).__name__,
            "error": _sanitized_text(exc, sensitive_values=sensitive_values),
        }


def _contract_exit_code(*, available_market_count: int, stock_pool_available: bool) -> int:
    if available_market_count == len(MARKETS) and stock_pool_available:
        return EXIT_SUCCESS
    if available_market_count > 0:
        return EXIT_PARTIAL_FAILURE
    return EXIT_FAILURE


def _sensitive_setting_values(settings: object) -> tuple[object, ...]:
    model_dump = getattr(settings, "model_dump", None)
    values = model_dump() if callable(model_dump) else vars(settings)
    if not isinstance(values, Mapping):
        return _environment_sensitive_values()
    configured = tuple(
        value
        for name, value in values.items()
        if value not in (None, "") and any(marker in str(name).lower() for marker in _SENSITIVE_NAME_MARKERS)
    )
    return tuple(dict.fromkeys((*configured, *_environment_sensitive_values())))


def _environment_sensitive_values() -> tuple[object, ...]:
    return tuple(
        value
        for name, value in os.environ.items()
        if value and any(marker in name.lower() for marker in _SENSITIVE_NAME_MARKERS)
    )


def _sanitized_text(value: object, *, sensitive_values: tuple[object, ...]) -> str:
    text = sanitize_provider_error(value, sensitive_values=sensitive_values).strip()
    return (text or type(value).__name__)[:MAX_ERROR_LENGTH]


def _elapsed_ms(started: float) -> float:
    return round(max(0.0, monotonic_now() - started) * 1000, 3)


def _validated_timeout(value: float, name: str) -> float:
    numeric = float(value)
    if not 0 < numeric <= 300:
        raise ValueError(f"{name} must be greater than 0 and at most 300 seconds")
    return numeric


def _positive_float(value: str) -> float:
    try:
        return _validated_timeout(float(value), "timeout")
    except ValueError as exc:
        raise argparse.ArgumentTypeError(str(exc)) from exc


def _create_datahub(settings: Settings) -> CanaryDataHub:
    return DataHub(settings=settings)


def _startup_failure_summary(
    exc: Exception,
    *,
    symbols: Mapping[str, str],
    request_timeout: float,
    stock_pool_timeout: float,
    overall_timeout: float,
    sensitive_values: tuple[object, ...],
) -> dict[str, Any]:
    error = _sanitized_text(exc, sensitive_values=sensitive_values)
    error_type = type(exc).__name__
    return {
        "schema_version": 1,
        "tool": "provider_canary",
        "started_at": audit_now_text(),
        "finished_at": audit_now_text(),
        "duration_ms": 0.0,
        "request_timeout_seconds": request_timeout,
        "stock_pool_timeout_seconds": stock_pool_timeout,
        "overall_timeout_seconds": overall_timeout,
        "overall_timed_out": False,
        "success_count": 0,
        "degraded_count": 0,
        "available_count": 0,
        "failure_count": len(MARKETS),
        "exit_code": EXIT_FAILURE,
        "markets": {
            market: {
                "market": market,
                "symbol": _sanitized_text(
                    symbols.get(market, DEFAULT_SYMBOLS[market]),
                    sensitive_values=sensitive_values,
                ),
                "status": "error",
                "latency_ms": 0.0,
                "error_type": error_type,
                "error": error,
            }
            for market in MARKETS
        },
        "stock_pool": {
            "status": "not_started",
            "latency_ms": 0.0,
            "error_type": error_type,
            "error": error,
        },
        "cleanup": {"status": "not_started", "latency_ms": 0.0},
    }


def main(
    argv: Sequence[str] | None = None,
    *,
    settings_factory: Callable[[], Settings] = get_settings,
    datahub_factory: DataHubFactory | None = None,
) -> int:
    args = build_parser().parse_args(argv)
    raw_symbols = {
        "SH": args.sh_symbol,
        "SZ": args.sz_symbol,
        "BJ": args.bj_symbol,
    }
    settings: Settings | None = None
    request_timeout = args.request_timeout or DEFAULT_REQUEST_TIMEOUT_SECONDS
    stock_pool_timeout = args.stock_pool_timeout or DEFAULT_STOCK_POOL_TIMEOUT_SECONDS
    # Provider SDK progress output must not corrupt the JSON-only CLI contract.
    with redirect_stderr(io.StringIO()):
        try:
            settings = settings_factory()
            request_timeout = args.request_timeout or float(settings.provider_call_timeout_seconds)
            stock_pool_timeout = args.stock_pool_timeout or float(
                settings.stock_pool_provider_timeout_seconds
            )
            with TemporaryDirectory(prefix="ashare-radar-canary-") as temporary_directory:
                runtime_settings = settings.model_copy(
                    update={
                        "cache_path": Path(temporary_directory) / "canary.sqlite3",
                        "scheduler_enabled": False,
                    }
                )
                summary = asyncio.run(
                    run_canary(
                        runtime_settings,
                        symbols=raw_symbols,
                        request_timeout=request_timeout,
                        stock_pool_timeout=stock_pool_timeout,
                        overall_timeout=args.overall_timeout,
                        datahub_factory=datahub_factory,
                    )
                )
        except Exception as exc:
            sensitive_values = (
                _sensitive_setting_values(settings)
                if settings is not None
                else _environment_sensitive_values()
            )
            summary = _startup_failure_summary(
                exc,
                symbols=raw_symbols,
                request_timeout=request_timeout,
                stock_pool_timeout=stock_pool_timeout,
                overall_timeout=args.overall_timeout,
                sensitive_values=sensitive_values,
            )
    print(json.dumps(summary, ensure_ascii=False, sort_keys=True, separators=(",", ":")))
    return int(summary["exit_code"])


if __name__ == "__main__":
    raise SystemExit(main())
