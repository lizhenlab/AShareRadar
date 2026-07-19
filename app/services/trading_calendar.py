from __future__ import annotations

import json
import logging
import os
import queue
import tempfile
import threading
from dataclasses import dataclass
from datetime import date, datetime, time, timedelta
from enum import StrEnum
from functools import lru_cache
from pathlib import Path
from typing import Iterable

from app.config import env_bool
from app.services.provider_errors import sanitize_provider_error
from app.utils.market_time import ASHARE_TIMEZONE, market_local_naive


BASE_DIR = Path(__file__).resolve().parent.parent.parent
CALENDAR_PATH = BASE_DIR / "data" / "trading_calendar.json"
BUNDLED_CALENDAR_PATH = BASE_DIR / "app" / "resources" / "trading_calendar.json"
LOGGER = logging.getLogger(__name__)
TRADE_CALENDAR_FETCH_TIMEOUT_SECONDS = 15.0
CALL_AUCTION_START_TIME = time(9, 15)
MORNING_SESSION_START_TIME = time(9, 30)
MORNING_CLOSE_SNAPSHOT_START_TIME = time(11, 25)
MORNING_SESSION_END_TIME = time(11, 30)
AFTERNOON_SESSION_START_TIME = time(13, 0)
AFTERNOON_REOPEN_GRACE_END_TIME = time(13, 15)
CLOSING_SNAPSHOT_START_TIME = time(14, 55)
MARKET_CLOSE_TIME = time(15, 0)
DAILY_KLINE_PUBLISH_TIME = time(15, 15)
LIVE_MARKET_EVENT_MAX_DELAY = timedelta(minutes=15)
_SAVE_LOCK = threading.Lock()
_FETCH_LOCK = threading.Lock()
_AUTO_REFRESH_LOCK = threading.Lock()
_AUTO_REFRESH_ATTEMPTED = False
_AUTO_REFRESH_SUCCEEDED = False
_AUTO_REFRESH_THREAD: threading.Thread | None = None


class MarketSessionPhase(StrEnum):
    CLOSED = "closed"
    PRE_OPEN = "pre_open"
    CALL_AUCTION = "call_auction"
    MORNING = "morning"
    MIDDAY_BREAK = "midday_break"
    AFTERNOON_REOPEN_GRACE = "afternoon_reopen_grace"
    AFTERNOON = "afternoon"
    CLOSE_PUBLISH_BUFFER = "close_publish_buffer"
    AFTER_CLOSE = "after_close"


class TradeCalendarSource(StrEnum):
    RUNTIME_CACHE = "runtime_cache"
    BUNDLED_BASELINE = "bundled_baseline"
    OUT_OF_COVERAGE = "out_of_coverage"
    UNAVAILABLE = "unavailable"


class TradingCalendarCoverageError(RuntimeError):
    """Raised when a concrete trade date cannot be derived from trusted coverage."""


@dataclass(frozen=True)
class TradeCalendarRefreshResult:
    trade_date_count: int
    source: str
    error: str | None = None

    @property
    def ok(self) -> bool:
        return self.trade_date_count > 0


@dataclass(frozen=True)
class TradeDateFetchResult:
    days: set[date]
    error: str | None = None


@dataclass(frozen=True)
class TradeCalendarStatus:
    target_date: date
    source: TradeCalendarSource
    covered: bool
    min_date: date | None = None
    max_date: date | None = None
    updated_at: datetime | None = None
    provider_source: str | None = None
    warning: str | None = None


class _TradeDays(set[date]):
    def __init__(
        self,
        values: Iterable[date] = (),
        *,
        updated_at: datetime | None = None,
        provider_source: str | None = None,
        kind: TradeCalendarSource | None = None,
        load_warnings: Iterable[str] = (),
        candidates: Iterable[_TradeDays] = (),
    ) -> None:
        super().__init__(values)
        self.min_date = min(self) if self else None
        self.max_date = max(self) if self else None
        self.updated_at = updated_at
        self.provider_source = provider_source
        self.kind = kind
        self.load_warnings = tuple(load_warnings)
        self.candidates = tuple(candidates)


def latest_expected_trade_date(now: datetime | None = None) -> date:
    current = _market_datetime(now)
    candidate = current.date()
    if current.time() < MARKET_CLOSE_TIME:
        candidate -= timedelta(days=1)
    return previous_trade_date(candidate)


def latest_expected_daily_kline_date(now: datetime | None = None) -> date:
    current = _market_datetime(now)
    candidate = current.date()
    if is_trading_day(candidate) and current.time() < DAILY_KLINE_PUBLISH_TIME:
        candidate -= timedelta(days=1)
    return previous_trade_date(candidate)


def expected_quote_date(now: datetime | None = None) -> date:
    current = _market_datetime(now)
    if is_trading_day(current.date()) and current.time() >= CALL_AUCTION_START_TIME:
        return current.date()
    return previous_trade_date(current.date() - timedelta(days=1))


def market_session_phase(now: datetime | None = None) -> MarketSessionPhase:
    current = _market_datetime(now)
    if not is_trading_day(current.date()):
        return MarketSessionPhase.CLOSED
    clock = current.time()
    if clock < CALL_AUCTION_START_TIME:
        return MarketSessionPhase.PRE_OPEN
    if clock < MORNING_SESSION_START_TIME:
        return MarketSessionPhase.CALL_AUCTION
    if clock <= MORNING_SESSION_END_TIME:
        return MarketSessionPhase.MORNING
    if clock < AFTERNOON_SESSION_START_TIME:
        return MarketSessionPhase.MIDDAY_BREAK
    if clock <= AFTERNOON_REOPEN_GRACE_END_TIME:
        return MarketSessionPhase.AFTERNOON_REOPEN_GRACE
    if clock <= MARKET_CLOSE_TIME:
        return MarketSessionPhase.AFTERNOON
    if clock < DAILY_KLINE_PUBLISH_TIME:
        return MarketSessionPhase.CLOSE_PUBLISH_BUFFER
    return MarketSessionPhase.AFTER_CLOSE


def is_trading_session(now: datetime | None = None) -> bool:
    return market_session_phase(now) in {
        MarketSessionPhase.MORNING,
        MarketSessionPhase.AFTERNOON_REOPEN_GRACE,
        MarketSessionPhase.AFTERNOON,
    }


def is_midday_break(now: datetime | None = None) -> bool:
    return market_session_phase(now) is MarketSessionPhase.MIDDAY_BREAK


def is_after_close(now: datetime | None = None) -> bool:
    return market_session_phase(now) in {
        MarketSessionPhase.CLOSE_PUBLISH_BUFFER,
        MarketSessionPhase.AFTER_CLOSE,
    }


def is_trading_day(value: date) -> bool:
    days, status = _calendar_resolution(value)
    if not status.covered:
        return False
    return value in days


def previous_trade_date(value: date) -> date:
    days, status = _calendar_resolution(value)
    _require_coverage(status)
    candidates = [item for item in days if item <= value]
    if not candidates:
        raise TradingCalendarCoverageError(f"可信交易日历无法推导 {value.isoformat()} 当日或之前的交易日。")
    return max(candidates)


def trading_day_gap(start: date, end: date) -> int:
    if start >= end:
        return 0
    days, status = _calendar_resolution(end, range_start=start)
    _require_coverage(status, range_start=start)
    return sum(start < item <= end for item in days)


def calendar_status(value: date | None = None) -> TradeCalendarStatus:
    target = value or _market_now().date()
    _days, status = _calendar_resolution(target)
    return status


def calendar_source(value: date | None = None) -> str:
    return calendar_status(value).source.value


def refresh_trade_calendar() -> int:
    return refresh_trade_calendar_result().trade_date_count


def refresh_trade_calendar_result() -> TradeCalendarRefreshResult:
    result = _fetch_akshare_trade_dates_result()
    if not result.days:
        return TradeCalendarRefreshResult(
            trade_date_count=0,
            source=_calendar_source_without_auto_refresh(),
            error=result.error or "AKShare 交易日历返回为空",
        )
    try:
        _save_days(result.days)
    except Exception as exc:
        return TradeCalendarRefreshResult(
            trade_date_count=0,
            source=_calendar_source_without_auto_refresh(),
            error=f"保存交易日历失败：{_exception_text(exc)}",
        )
    _reset_calendar_caches()
    return TradeCalendarRefreshResult(
        trade_date_count=len(result.days),
        source=_calendar_source_without_auto_refresh(),
    )


@lru_cache(maxsize=1)
def _trade_days() -> set[date]:
    candidates: list[_TradeDays] = []
    warnings: list[str] = []
    runtime, runtime_warning = _load_calendar_file(CALENDAR_PATH, TradeCalendarSource.RUNTIME_CACHE)
    bundled, bundled_warning = _load_calendar_file(BUNDLED_CALENDAR_PATH, TradeCalendarSource.BUNDLED_BASELINE)
    if runtime is not None:
        candidates.append(runtime)
    if bundled is not None:
        candidates.append(bundled)
    if runtime_warning:
        warnings.append(runtime_warning)
    if bundled_warning:
        warnings.append(bundled_warning)

    selected = _select_candidate(candidates, _market_now().date())
    return _catalog_days(selected, candidates, warnings)


def _calendar_resolution(
    value: date,
    *,
    range_start: date | None = None,
    allow_auto_refresh: bool = True,
) -> tuple[set[date], TradeCalendarStatus]:
    catalog = _trade_days()
    selected = _select_from_catalog(catalog, value, range_start=range_start)
    if allow_auto_refresh and _should_auto_refresh(catalog, selected, value):
        _trigger_auto_refresh()
    status = _status_for_resolution(value, catalog, selected)
    if status.warning:
        _log_coverage_warning(status.warning)
    return (selected or set()), status


def _catalog_days(
    selected: _TradeDays | None,
    candidates: Iterable[_TradeDays],
    warnings: Iterable[str],
) -> _TradeDays:
    return _TradeDays(
        selected or (),
        updated_at=selected.updated_at if selected is not None else None,
        provider_source=selected.provider_source if selected is not None else None,
        kind=selected.kind if selected is not None else None,
        load_warnings=warnings,
        candidates=candidates,
    )


def _select_from_catalog(
    catalog: set[date],
    value: date,
    *,
    range_start: date | None = None,
) -> set[date] | None:
    if isinstance(catalog, _TradeDays) and catalog.candidates:
        return _select_candidate(catalog.candidates, value, range_start=range_start)
    min_date, max_date = _coverage_bounds(catalog)
    required_start = range_start or value
    if min_date is not None and max_date is not None and min_date <= required_start <= value <= max_date:
        return catalog
    return None


def _select_candidate(
    candidates: Iterable[_TradeDays],
    value: date,
    *,
    range_start: date | None = None,
) -> _TradeDays | None:
    required_start = range_start or value
    covered = [
        item
        for item in candidates
        if item.min_date is not None
        and item.max_date is not None
        and item.min_date <= required_start <= value <= item.max_date
    ]
    return max(covered, key=_candidate_rank) if covered else None


def _candidate_rank(days: _TradeDays) -> tuple[datetime, int, date, int]:
    min_date = days.min_date or date.max
    return (
        days.updated_at or datetime.min,
        1 if days.kind is TradeCalendarSource.RUNTIME_CACHE else 0,
        days.max_date or date.min,
        -min_date.toordinal(),
    )


def _status_for_resolution(
    value: date,
    catalog: set[date],
    selected: set[date] | None,
) -> TradeCalendarStatus:
    warnings = catalog.load_warnings if isinstance(catalog, _TradeDays) else ()
    if selected is not None:
        kind = selected.kind if isinstance(selected, _TradeDays) else TradeCalendarSource.RUNTIME_CACHE
        warning = " ".join(warnings) or None
        return TradeCalendarStatus(
            target_date=value,
            source=kind or TradeCalendarSource.RUNTIME_CACHE,
            covered=True,
            min_date=_coverage_bounds(selected)[0],
            max_date=_coverage_bounds(selected)[1],
            updated_at=selected.updated_at if isinstance(selected, _TradeDays) else None,
            provider_source=selected.provider_source if isinstance(selected, _TradeDays) else None,
            warning=warning,
        )

    candidates = catalog.candidates if isinstance(catalog, _TradeDays) else ()
    if candidates or catalog:
        warning = _out_of_coverage_warning(value, candidates or (catalog,))
        if warnings:
            warning = f"{' '.join(warnings)} {warning}"
        return TradeCalendarStatus(
            target_date=value,
            source=TradeCalendarSource.OUT_OF_COVERAGE,
            covered=False,
            warning=warning,
        )
    warning = " ".join((*warnings, "没有可用的可信交易日历；交易日判断已保守关闭。"))
    return TradeCalendarStatus(
        target_date=value,
        source=TradeCalendarSource.UNAVAILABLE,
        covered=False,
        warning=warning,
    )


def _out_of_coverage_warning(value: date, candidates: Iterable[set[date]]) -> str:
    ranges = []
    for candidate in candidates:
        min_date, max_date = _coverage_bounds(candidate)
        if min_date is None or max_date is None:
            continue
        kind = candidate.kind.value if isinstance(candidate, _TradeDays) and candidate.kind is not None else "calendar"
        ranges.append(f"{kind} {min_date.isoformat()} 至 {max_date.isoformat()}")
    coverage = "；".join(ranges) or "无有效覆盖"
    return f"可信交易日历未覆盖 {value.isoformat()}（{coverage}）；交易日判断已保守关闭。"


def _require_coverage(status: TradeCalendarStatus, *, range_start: date | None = None) -> None:
    if status.covered:
        return
    target = status.target_date.isoformat()
    scope = f"{range_start.isoformat()} 至 {target}" if range_start is not None else target
    raise TradingCalendarCoverageError(
        f"可信交易日历未覆盖 {scope}，无法推导交易日期；请刷新运行时交易日历或更新 bundled baseline。"
    )


def _should_auto_refresh(catalog: set[date], selected: set[date] | None, value: date) -> bool:
    if not env_bool("ASHARE_RADAR_TRADE_CALENDAR_AUTO_FETCH", False, aliases=("TRADE_CALENDAR_AUTO_FETCH",)):
        return False
    candidates = catalog.candidates if isinstance(catalog, _TradeDays) else ()
    runtime = next((item for item in candidates if item.kind is TradeCalendarSource.RUNTIME_CACHE), None)
    runtime_covers_current = runtime is not None and _covers(runtime, _market_now().date())
    return selected is None or not runtime_covers_current or not _covers(runtime, value)


def _trigger_auto_refresh() -> bool:
    global _AUTO_REFRESH_ATTEMPTED, _AUTO_REFRESH_THREAD
    with _AUTO_REFRESH_LOCK:
        if _AUTO_REFRESH_ATTEMPTED:
            return False
        _AUTO_REFRESH_ATTEMPTED = True
        worker = threading.Thread(target=_run_auto_refresh, name="trade-calendar-auto-refresh", daemon=True)
        _AUTO_REFRESH_THREAD = worker
        try:
            worker.start()
        except RuntimeError as exc:
            _AUTO_REFRESH_ATTEMPTED = False
            _AUTO_REFRESH_THREAD = None
            _log_coverage_warning(f"无法启动交易日历自动刷新：{_exception_text(exc)}")
            return False
        return True


def _run_auto_refresh() -> None:
    global _AUTO_REFRESH_SUCCEEDED, _AUTO_REFRESH_THREAD
    succeeded = False
    try:
        result = _fetch_akshare_trade_dates_result()
        if not result.days:
            _log_coverage_warning(f"自动刷新交易日历失败：{result.error or 'AKShare 返回为空'}")
            return
        try:
            _save_days(result.days)
        except Exception as exc:
            _log_coverage_warning(f"自动保存交易日历失败：{_exception_text(exc)}")
            return
        _trade_days.cache_clear()
        succeeded = True
    finally:
        with _AUTO_REFRESH_LOCK:
            _AUTO_REFRESH_SUCCEEDED = succeeded
            _AUTO_REFRESH_THREAD = None


def _load_calendar_file(path: Path, kind: TradeCalendarSource) -> tuple[_TradeDays | None, str | None]:
    if not path.exists():
        if kind is TradeCalendarSource.RUNTIME_CACHE:
            return None, None
        return None, "内置交易日历基线不存在。"
    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        return None, _load_warning(kind, f"无法读取或 JSON 损坏：{type(exc).__name__}")
    if not isinstance(raw, dict):
        return None, _load_warning(kind, "根节点必须是对象")
    values = raw.get("trade_dates")
    if not isinstance(values, list) or not values:
        return None, _load_warning(kind, "trade_dates 必须是非空列表")
    days = _strict_calendar_dates(values)
    if days is None:
        return None, _load_warning(kind, "trade_dates 必须是按日期升序排列的唯一 YYYY-MM-DD 字符串")

    updated_at = _parse_datetime(raw.get("updated_at"))
    provider_source = _optional_text(raw.get("source"))
    stored_min = _parse_date(raw.get("min_date"))
    stored_max = _parse_date(raw.get("max_date"))
    stored_count = raw.get("trade_date_count")
    actual_min = days[0]
    actual_max = days[-1]
    metadata_valid = (
        raw.get("schema_version") == 1
        and updated_at is not None
        and provider_source is not None
        and stored_min == actual_min
        and stored_max == actual_max
        and isinstance(stored_count, int)
        and not isinstance(stored_count, bool)
        and stored_count == len(days)
    )
    if not metadata_valid:
        return None, _load_warning(kind, "版本、来源、更新时间或覆盖元数据缺失/不一致")
    return (
        _TradeDays(
            days,
            updated_at=updated_at,
            provider_source=provider_source,
            kind=kind,
        ),
        None,
    )


def _load_warning(kind: TradeCalendarSource, detail: str) -> str:
    label = "运行时交易日历" if kind is TradeCalendarSource.RUNTIME_CACHE else "内置交易日历基线"
    return f"{label}{detail}，已忽略该快照。"


def _strict_calendar_dates(values: list[object]) -> list[date] | None:
    parsed: list[date] = []
    for value in values:
        if not isinstance(value, str):
            return None
        text = value.strip()
        try:
            item = date.fromisoformat(text)
        except ValueError:
            return None
        if text != item.isoformat():
            return None
        parsed.append(item)
    if parsed != sorted(set(parsed)):
        return None
    return parsed


def _save_days(days: Iterable[date]) -> None:
    ordered = sorted(set(days))
    if not ordered:
        return
    payload = {
        "schema_version": 1,
        "source": "akshare.tool_trade_date_hist_sina",
        "updated_at": _market_now().strftime("%Y-%m-%d %H:%M:%S"),
        "min_date": ordered[0].isoformat(),
        "max_date": ordered[-1].isoformat(),
        "trade_date_count": len(ordered),
        "trade_dates": [item.isoformat() for item in ordered],
    }
    encoded = json.dumps(payload, ensure_ascii=False, indent=2) + "\n"
    with _SAVE_LOCK:
        CALENDAR_PATH.parent.mkdir(parents=True, exist_ok=True)
        file_descriptor, temporary_name = tempfile.mkstemp(
            prefix=f".{CALENDAR_PATH.name}.",
            suffix=".tmp",
            dir=CALENDAR_PATH.parent,
        )
        temporary_path = Path(temporary_name)
        try:
            with os.fdopen(file_descriptor, "w", encoding="utf-8") as handle:
                handle.write(encoded)
                handle.flush()
                os.fsync(handle.fileno())
            os.replace(temporary_path, CALENDAR_PATH)
        except BaseException:
            temporary_path.unlink(missing_ok=True)
            raise


def _fetch_akshare_trade_dates() -> set[date]:
    return _fetch_akshare_trade_dates_result().days


def _fetch_akshare_trade_dates_result() -> TradeDateFetchResult:
    if not _FETCH_LOCK.acquire(blocking=False):
        return TradeDateFetchResult(set(), "已有交易日历刷新仍在进行，请稍后重试")
    result_queue: queue.Queue[TradeDateFetchResult] = queue.Queue(maxsize=1)

    def fetch() -> None:
        try:
            result = _fetch_akshare_trade_dates_blocking()
        finally:
            _FETCH_LOCK.release()
        result_queue.put(result)

    worker = threading.Thread(target=fetch, name="trade-calendar-fetch", daemon=True)
    try:
        worker.start()
    except RuntimeError as exc:
        _FETCH_LOCK.release()
        return TradeDateFetchResult(set(), _exception_text(exc))
    try:
        return result_queue.get(timeout=TRADE_CALENDAR_FETCH_TIMEOUT_SECONDS)
    except queue.Empty:
        return TradeDateFetchResult(
            set(),
            f"AKShare 交易日历刷新超过 {TRADE_CALENDAR_FETCH_TIMEOUT_SECONDS:g} 秒，已停止等待",
        )


def _fetch_akshare_trade_dates_blocking() -> TradeDateFetchResult:
    try:
        import akshare as ak

        frame = ak.tool_trade_date_hist_sina()
        days = _parse_trade_date_frame(frame)
        if not days:
            return TradeDateFetchResult(set(), "AKShare 交易日历返回为空")
        return TradeDateFetchResult(days)
    except Exception as exc:
        return TradeDateFetchResult(set(), _exception_text(exc))


def _parse_trade_date_frame(frame: object) -> set[date]:
    values: list[object] = []
    for column in getattr(frame, "columns", []):
        values.extend(frame[column].dropna().tolist())
    return _parse_dates(values)


def _parse_dates(values: Iterable[object]) -> set[date]:
    result: set[date] = set()
    for value in values:
        text = str(value).strip()
        if not text:
            continue
        if len(text) == 8 and text.isdigit():
            text = f"{text[:4]}-{text[4:6]}-{text[6:]}"
        try:
            result.add(datetime.fromisoformat(text[:10]).date())
        except ValueError:
            continue
    return result


def _covers(days: set[date] | None, value: date) -> bool:
    if days is None:
        return False
    min_date, max_date = _coverage_bounds(days)
    return min_date is not None and max_date is not None and min_date <= value <= max_date


def _coverage_bounds(days: set[date]) -> tuple[date | None, date | None]:
    if isinstance(days, _TradeDays):
        return days.min_date, days.max_date
    if not days:
        return None, None
    return min(days), max(days)


@lru_cache(maxsize=32)
def _log_coverage_warning(message: str) -> None:
    LOGGER.warning(message)


def _parse_date(value: object) -> date | None:
    if value is None:
        return None
    text = str(value).strip()
    try:
        parsed = date.fromisoformat(text)
    except ValueError:
        return None
    return parsed if text == parsed.isoformat() else None


def _parse_datetime(value: object) -> datetime | None:
    if value is None:
        return None
    try:
        return market_local_naive(datetime.fromisoformat(str(value).strip()))
    except ValueError:
        return None


def _optional_text(value: object) -> str | None:
    if value is None:
        return None
    text = str(value).strip()
    return text or None


def _exception_text(exc: Exception) -> str:
    text = sanitize_provider_error(exc).strip()
    return f"{type(exc).__name__}: {text}" if text else type(exc).__name__


def _calendar_source_without_auto_refresh() -> str:
    _days, status = _calendar_resolution(_market_now().date(), allow_auto_refresh=False)
    return status.source.value


def _reset_calendar_caches() -> None:
    global _AUTO_REFRESH_ATTEMPTED, _AUTO_REFRESH_SUCCEEDED
    _trade_days.cache_clear()
    with _AUTO_REFRESH_LOCK:
        if _AUTO_REFRESH_THREAD is None:
            _AUTO_REFRESH_ATTEMPTED = False
            _AUTO_REFRESH_SUCCEEDED = False


def _market_datetime(value: datetime | None) -> datetime:
    return market_local_naive(value) if value is not None else _market_now()


def _market_now() -> datetime:
    return market_local_naive(datetime.now(ASHARE_TIMEZONE))
