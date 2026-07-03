from __future__ import annotations

import json
from dataclasses import dataclass
from datetime import date, datetime, timedelta
from functools import lru_cache
from pathlib import Path
from typing import Iterable

from app.config import env_bool


BASE_DIR = Path(__file__).resolve().parent.parent.parent
CALENDAR_PATH = BASE_DIR / "data" / "trading_calendar.json"


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


def latest_expected_trade_date(now: datetime | None = None) -> date:
    current = now or datetime.now()
    candidate = current.date()
    if current.hour < 15:
        candidate -= timedelta(days=1)
    return previous_trade_date(candidate)


def expected_quote_date(now: datetime | None = None) -> date:
    current = now or datetime.now()
    minutes = current.hour * 60 + current.minute
    if is_trading_day(current.date()) and minutes >= 9 * 60 + 15:
        return current.date()
    return previous_trade_date(current.date() - timedelta(days=1))


def is_trading_session(now: datetime | None = None) -> bool:
    current = now or datetime.now()
    if not is_trading_day(current.date()):
        return False
    minutes = current.hour * 60 + current.minute
    return (9 * 60 + 30 <= minutes <= 11 * 60 + 30) or (13 * 60 <= minutes <= 15 * 60)


def is_midday_break(now: datetime | None = None) -> bool:
    current = now or datetime.now()
    if not is_trading_day(current.date()):
        return False
    minutes = current.hour * 60 + current.minute
    return 11 * 60 + 30 < minutes < 13 * 60


def is_after_close(now: datetime | None = None) -> bool:
    current = now or datetime.now()
    if not is_trading_day(current.date()):
        return False
    minutes = current.hour * 60 + current.minute
    return minutes > 15 * 60


def is_trading_day(value: date) -> bool:
    days = _trade_days()
    if days:
        return value in days
    return value.weekday() < 5


def previous_trade_date(value: date) -> date:
    current = value
    while not is_trading_day(current):
        current -= timedelta(days=1)
    return current


def trading_day_gap(start: date, end: date) -> int:
    if start >= end:
        return 0
    days = _trade_days()
    if days:
        return sum(1 for item in days if start < item <= end)
    current = start
    count = 0
    while current < end:
        current += timedelta(days=1)
        if current.weekday() < 5:
            count += 1
    return count


def calendar_source() -> str:
    return "交易日历缓存" if _trade_days() else "工作日兜底"


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
            return fetched
    return set()


def _load_cached_days() -> set[date]:
    if not CALENDAR_PATH.exists():
        return set()
    try:
        raw = json.loads(CALENDAR_PATH.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return set()
    values = raw.get("trade_dates") if isinstance(raw, dict) else raw
    if not isinstance(values, list):
        return set()
    return _parse_dates(values)


def _save_days(days: Iterable[date]) -> None:
    ordered = sorted(set(days))
    if not ordered:
        return
    CALENDAR_PATH.parent.mkdir(parents=True, exist_ok=True)
    payload = {
        "updated_at": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
        "source": "akshare.tool_trade_date_hist_sina",
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


def _exception_text(exc: Exception) -> str:
    text = str(exc).strip()
    return f"{type(exc).__name__}: {text}" if text else type(exc).__name__
