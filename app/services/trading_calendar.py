from __future__ import annotations

import json
import logging
from dataclasses import dataclass
from datetime import date, datetime, time, timedelta
from enum import StrEnum
from functools import lru_cache
from pathlib import Path
from typing import Iterable

from app.config import env_bool
from app.services.provider_errors import sanitize_provider_error


BASE_DIR = Path(__file__).resolve().parent.parent.parent
CALENDAR_PATH = BASE_DIR / "data" / "trading_calendar.json"
LOGGER = logging.getLogger(__name__)
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


class _TradeDays(set[date]):
    def __init__(
        self,
        values: Iterable[date] = (),
        *,
        updated_at: datetime | None = None,
        source: str | None = None,
        load_warning: str | None = None,
    ) -> None:
        super().__init__(values)
        self.min_date = min(self) if self else None
        self.max_date = max(self) if self else None
        self.updated_at = updated_at
        self.source = source
        self.load_warning = load_warning


@dataclass(frozen=True)
class _CalendarCoverage:
    min_date: date | None
    max_date: date | None
    updated_at: datetime | None
    covered: bool
    warning: str | None = None


def latest_expected_trade_date(now: datetime | None = None) -> date:
    current = now or datetime.now()
    candidate = current.date()
    if current.time() < MARKET_CLOSE_TIME:
        candidate -= timedelta(days=1)
    return previous_trade_date(candidate)


def latest_expected_daily_kline_date(now: datetime | None = None) -> date:
    current = now or datetime.now()
    candidate = current.date()
    if is_trading_day(candidate) and current.time() < DAILY_KLINE_PUBLISH_TIME:
        candidate -= timedelta(days=1)
    return previous_trade_date(candidate)


def expected_quote_date(now: datetime | None = None) -> date:
    current = now or datetime.now()
    if is_trading_day(current.date()) and current.time() >= CALL_AUCTION_START_TIME:
        return current.date()
    return previous_trade_date(current.date() - timedelta(days=1))


def market_session_phase(now: datetime | None = None) -> MarketSessionPhase:
    current = now or datetime.now()
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
    days = _trade_days()
    return _is_trading_day(value, days)


def previous_trade_date(value: date) -> date:
    days = _trade_days()
    current = value
    while not _is_trading_day(current, days):
        current -= timedelta(days=1)
    return current


def trading_day_gap(start: date, end: date) -> int:
    if start >= end:
        return 0
    days = _trade_days()
    current = start
    count = 0
    while current < end:
        current += timedelta(days=1)
        if _is_trading_day(current, days):
            count += 1
    return count


def calendar_source() -> str:
    coverage = _calendar_coverage(date.today(), _trade_days())
    if coverage.warning:
        _log_coverage_warning(coverage.warning)
    return "交易日历缓存" if coverage.covered else "工作日兜底"


def refresh_trade_calendar() -> int:
    result = _fetch_akshare_trade_dates_result()
    days = result.days
    if days:
        _save_days(days)
        _trade_days.cache_clear()
    return len(days)


def refresh_trade_calendar_result() -> TradeCalendarRefreshResult:
    result = _fetch_akshare_trade_dates_result()
    if result.days:
        _save_days(result.days)
        _trade_days.cache_clear()
    return TradeCalendarRefreshResult(
        trade_date_count=len(result.days),
        source=calendar_source(),
        error=result.error if not result.days else None,
    )


@lru_cache(maxsize=1)
def _trade_days() -> set[date]:
    cached = _load_cached_days()
    if cached:
        return cached
    if env_bool("ASHARE_RADAR_TRADE_CALENDAR_AUTO_FETCH", False, aliases=("TRADE_CALENDAR_AUTO_FETCH",)):
        fetched = _fetch_akshare_trade_dates()
        if fetched:
            _save_days(fetched)
            return _load_cached_days()
    return cached


def _load_cached_days() -> set[date]:
    if not CALENDAR_PATH.exists():
        return _TradeDays(load_warning="交易日历缓存不存在，当前按周一至周五兜底。")
    try:
        raw = json.loads(CALENDAR_PATH.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return _TradeDays(load_warning="交易日历缓存无法读取或格式损坏，当前按周一至周五兜底。")
    values = raw.get("trade_dates") if isinstance(raw, dict) else raw
    if not isinstance(values, list):
        return _TradeDays(load_warning="交易日历缓存缺少有效的 trade_dates 列表，当前按周一至周五兜底。")
    days = _parse_dates(values)
    if not days:
        return _TradeDays(load_warning="交易日历缓存为空，当前按周一至周五兜底。")

    updated_at = _parse_datetime(raw.get("updated_at")) if isinstance(raw, dict) else None
    source = _optional_text(raw.get("source")) if isinstance(raw, dict) else None
    warning = _cache_metadata_warning(raw, days, updated_at) if isinstance(raw, dict) else None
    return _TradeDays(days, updated_at=updated_at, source=source, load_warning=warning)


def _save_days(days: Iterable[date]) -> None:
    ordered = sorted(set(days))
    if not ordered:
        return
    CALENDAR_PATH.parent.mkdir(parents=True, exist_ok=True)
    payload = {
        "updated_at": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
        "source": "akshare.tool_trade_date_hist_sina",
        "min_date": ordered[0].isoformat(),
        "max_date": ordered[-1].isoformat(),
        "trade_dates": [item.isoformat() for item in ordered],
    }
    CALENDAR_PATH.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")


def _fetch_akshare_trade_dates() -> set[date]:
    return _fetch_akshare_trade_dates_result().days


def _fetch_akshare_trade_dates_result() -> TradeDateFetchResult:
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


def _is_trading_day(value: date, days: set[date]) -> bool:
    min_date, max_date = _coverage_bounds(days)
    if min_date is not None and max_date is not None and min_date <= value <= max_date:
        return value in days
    return value.weekday() < 5


def _calendar_coverage(value: date, days: set[date]) -> _CalendarCoverage:
    min_date, max_date = _coverage_bounds(days)
    updated_at = days.updated_at if isinstance(days, _TradeDays) else None
    load_warning = days.load_warning if isinstance(days, _TradeDays) else None
    if min_date is None or max_date is None:
        return _CalendarCoverage(None, None, updated_at, False, load_warning or "交易日历缓存不可用，当前按周一至周五兜底。")
    if min_date <= value <= max_date:
        return _CalendarCoverage(min_date, max_date, updated_at, True, load_warning)
    updated_text = updated_at.strftime("%Y-%m-%d %H:%M:%S") if updated_at else "未知"
    warning = (
        f"交易日历缓存未覆盖 {value.isoformat()}（覆盖 {min_date.isoformat()} 至 {max_date.isoformat()}，"
        f"更新于 {updated_text}），当前按周一至周五兜底。"
    )
    return _CalendarCoverage(min_date, max_date, updated_at, False, warning)


def _coverage_bounds(days: set[date]) -> tuple[date | None, date | None]:
    if isinstance(days, _TradeDays):
        return days.min_date, days.max_date
    if not days:
        return None, None
    return min(days), max(days)


@lru_cache(maxsize=16)
def _log_coverage_warning(message: str) -> None:
    LOGGER.warning(message)


def _cache_metadata_warning(raw: dict[object, object], days: set[date], updated_at: datetime | None) -> str | None:
    actual_min = min(days)
    actual_max = max(days)
    stored_min = _parse_date(raw.get("min_date"))
    stored_max = _parse_date(raw.get("max_date"))
    warnings: list[str] = []
    if stored_min != actual_min or stored_max != actual_max:
        warnings.append(
            f"交易日历覆盖元数据缺失或不一致，已按有效日期重建为 {actual_min.isoformat()} 至 {actual_max.isoformat()}。"
        )
    if updated_at is None:
        warnings.append("交易日历缓存缺少有效的 updated_at。")
    return " ".join(warnings) or None


def _parse_date(value: object) -> date | None:
    if value is None:
        return None
    try:
        return datetime.fromisoformat(str(value).strip()[:10]).date()
    except ValueError:
        return None


def _parse_datetime(value: object) -> datetime | None:
    if value is None:
        return None
    try:
        return datetime.fromisoformat(str(value).strip())
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
