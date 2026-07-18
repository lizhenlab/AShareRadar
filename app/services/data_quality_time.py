from __future__ import annotations

from datetime import date, datetime
from typing import Any

from app.services import trading_calendar
from app.utils.market_time import market_local_naive, normalize_market_datetime
from app.utils.time import seconds_since_text


FreshnessPenalty = tuple[int, list[str], list[str]]
QUOTE_LIVE_MAX_DELAY_SECONDS = 15 * 60
QUOTE_AUCTION_START_MINUTE = 9 * 60 + 15
QUOTE_MORNING_CLOSE_START_MINUTE = 11 * 60 + 25
QUOTE_MORNING_CLOSE_MINUTE = 11 * 60 + 30
QUOTE_AFTERNOON_OPEN_MINUTE = 13 * 60
QUOTE_CLOSE_START_MINUTE = 14 * 60 + 55


def latest_expected_trade_date(now: datetime | None = None) -> date:
    return trading_calendar.latest_expected_trade_date(_local_naive_datetime(now) if now is not None else None)


def latest_expected_daily_kline_date(now: datetime | None = None) -> date:
    return trading_calendar.latest_expected_daily_kline_date(
        _local_naive_datetime(now) if now is not None else None
    )


def market_local_datetime(value: datetime | None = None) -> datetime:
    return _local_naive_datetime(value or datetime.now())


def quote_delay_seconds(value: str, *, now: datetime | None = None) -> int | None:
    if now is None:
        seconds = seconds_since_text(value)
        if seconds is None or seconds < 0:
            return None
        return int(seconds)
    parsed = parse_quote_time(value)
    if parsed is None:
        return None
    delay_seconds = int((_local_naive_datetime(now) - parsed).total_seconds())
    if delay_seconds < 0:
        return None
    return delay_seconds


def quote_freshness_penalty(value: str, now: datetime) -> FreshnessPenalty:
    now = _local_naive_datetime(now)
    parsed = parse_quote_time(value)
    if parsed is None:
        return _invalid_quote_time_penalty()

    expected_date = expected_quote_date(now)
    quote_date = parsed.date()
    if quote_date < expected_date:
        return _stale_quote_date_penalty(quote_date, expected_date, now)
    if quote_date > expected_date:
        return _future_quote_date_penalty(quote_date)
    if parsed > now:
        return _future_quote_time_penalty(parsed, now)

    return _same_day_quote_penalty(parsed, now)


def quote_event_time_error(value: str, *, now: datetime | None = None) -> str | None:
    current = _local_naive_datetime(now or datetime.now())
    parsed = parse_quote_time(value)
    if parsed is None:
        return "报价事件时间无法解析"
    if parsed > current:
        return f"报价事件时间 {parsed.strftime('%Y-%m-%d %H:%M:%S')} 晚于抓取检查时间"

    quote_date = parsed.date()
    if not trading_calendar.is_trading_day(quote_date):
        return f"报价事件日期 {quote_date.isoformat()} 不是 A 股交易日"

    expected_date = expected_quote_date(current)
    if quote_date < expected_date:
        return f"报价事件日期 {quote_date.isoformat()} 早于应参考交易日 {expected_date.isoformat()}"
    if quote_date > expected_date:
        return f"报价事件日期 {quote_date.isoformat()} 晚于应参考交易日 {expected_date.isoformat()}"
    if current.date() != expected_date:
        return _closing_snapshot_error(parsed)
    if is_trading_session(current):
        return _trading_session_event_time_error(parsed, current)
    if is_midday_break(current):
        return _morning_close_snapshot_error(parsed)
    if is_after_close(current):
        return _closing_snapshot_error(parsed)
    return _call_auction_event_time_error(parsed, current)


def quote_cache_lookup_seconds(now: datetime | None = None) -> int:
    current = _local_naive_datetime(now or datetime.now())
    expected_start = datetime.combine(expected_quote_date(current), datetime.min.time())
    seconds_since_expected_start = max(0, int((current - expected_start).total_seconds()))
    return max(24 * 60 * 60, seconds_since_expected_start + 60 * 60)


def normalize_quote_event_time(value: Any, *, event_date: Any = None) -> str | None:
    return normalize_market_datetime(value, event_date=event_date)


def _trading_session_event_time_error(parsed: datetime, current: datetime) -> str | None:
    current_minutes = _minute_of_day(current)
    event_minutes = _minute_of_day(parsed)
    if current_minutes < QUOTE_AFTERNOON_OPEN_MINUTE:
        if not QUOTE_AUCTION_START_MINUTE <= event_minutes <= QUOTE_MORNING_CLOSE_MINUTE:
            return f"交易时段报价事件时刻 {parsed.strftime('%H:%M:%S')} 不在上午有效行情窗口"
    elif event_minutes < QUOTE_AFTERNOON_OPEN_MINUTE:
        if current_minutes <= QUOTE_AFTERNOON_OPEN_MINUTE + 15 and _is_morning_close_snapshot(parsed):
            return None
        return "午后交易时段仍在使用上午收盘快照"
    delay_seconds = int((current - parsed).total_seconds())
    if delay_seconds > QUOTE_LIVE_MAX_DELAY_SECONDS:
        return f"交易时段报价事件已滞后约 {max(1, delay_seconds // 60)} 分钟"
    return None


def _call_auction_event_time_error(parsed: datetime, current: datetime) -> str | None:
    if _minute_of_day(parsed) < QUOTE_AUCTION_START_MINUTE:
        return f"集合竞价阶段报价事件时刻 {parsed.strftime('%H:%M:%S')} 早于有效行情窗口"
    delay_seconds = int((current - parsed).total_seconds())
    if delay_seconds > QUOTE_LIVE_MAX_DELAY_SECONDS:
        return f"集合竞价阶段报价事件已滞后约 {max(1, delay_seconds // 60)} 分钟"
    return None


def _morning_close_snapshot_error(parsed: datetime) -> str | None:
    if _minute_of_day(parsed) >= QUOTE_MORNING_CLOSE_START_MINUTE:
        return None
    return "午间休市仅接受接近上午收盘的行情快照"


def _closing_snapshot_error(parsed: datetime) -> str | None:
    if _minute_of_day(parsed) >= QUOTE_CLOSE_START_MINUTE:
        return None
    return "非交易时段仅接受最近有效交易日的尾盘行情快照"


def _is_morning_close_snapshot(value: datetime) -> bool:
    minutes = _minute_of_day(value)
    return QUOTE_MORNING_CLOSE_START_MINUTE <= minutes < QUOTE_AFTERNOON_OPEN_MINUTE


def _minute_of_day(value: datetime) -> int:
    return value.hour * 60 + value.minute


def _local_naive_datetime(value: datetime) -> datetime:
    return market_local_naive(value)


def _invalid_quote_time_penalty() -> FreshnessPenalty:
    return 12, ["报价时间无法识别，需确认行情源时间字段。"], ["报价时间异常"]


def _future_quote_date_penalty(quote_date: date) -> FreshnessPenalty:
    return 12, [f"报价日期为 {quote_date.isoformat()}，晚于当前应参考交易日，需核对行情时间。"], ["报价时间超前"]


def _future_quote_time_penalty(parsed: datetime, now: datetime) -> FreshnessPenalty:
    return (
        12,
        [f"报价时间 {parsed.strftime('%H:%M:%S')} 晚于当前检查时间 {now.strftime('%H:%M:%S')}，需核对行情源时间。"],
        ["报价时间超前"],
    )


def _stale_quote_date_penalty(quote_date: date, expected_date: date, now: datetime) -> FreshnessPenalty:
    days = weekday_gap(quote_date, expected_date)
    if is_trading_session(now):
        return _trading_session_stale_quote_penalty(quote_date, days)
    return _non_session_stale_quote_penalty(quote_date, days)


def _trading_session_stale_quote_penalty(quote_date: date, days: int) -> FreshnessPenalty:
    if days >= 5:
        return 30, [_stale_quote_note(quote_date, days)], ["报价严重滞后"]
    if days >= 1:
        return 24, [f"交易时段仍在使用 {quote_date.isoformat()} 的报价，落后当前应参考交易日约 {days} 个交易日。"], ["报价滞后"]
    return _non_session_stale_quote_penalty(quote_date, days)


def _non_session_stale_quote_penalty(quote_date: date, days: int) -> FreshnessPenalty:
    if days >= 5:
        return 30, [_stale_quote_note(quote_date, days)], ["报价严重滞后"]
    if days >= 2:
        return 20, [_stale_quote_note(quote_date, days)], ["报价滞后"]
    return 8, [_stale_quote_note(quote_date, days)], ["报价轻微滞后"]


def _stale_quote_note(quote_date: date, days: int) -> str:
    return f"报价日期为 {quote_date.isoformat()}，落后当前应参考交易日约 {days} 个交易日。"


def _same_day_quote_penalty(parsed: datetime, now: datetime) -> FreshnessPenalty:
    if is_trading_session(now):
        return _trading_session_quote_delay_penalty(parsed, now)
    if is_midday_break(now):
        return _midday_break_quote_penalty(parsed)
    if is_after_close(now):
        return _after_close_quote_penalty(parsed)
    return 0, ["非交易时段使用最近交易日行情快照。"], []


def _trading_session_quote_delay_penalty(parsed: datetime, now: datetime) -> FreshnessPenalty:
    delay_seconds = max(0, int((now - parsed).total_seconds()))
    if delay_seconds > 60 * 60:
        return 18, [f"交易时段内报价约 {delay_seconds // 60} 分钟未更新，需确认是否延迟。"], ["交易时段报价滞后"]
    if delay_seconds > 15 * 60:
        return 8, [f"交易时段内报价约 {delay_seconds // 60} 分钟未更新，短线判断需降权。"], []
    return 0, [], []


def _midday_break_quote_penalty(parsed: datetime) -> FreshnessPenalty:
    if _before_morning_close_snapshot(parsed):
        return 8, ["午间休市阶段报价未接近上午收盘时间，需确认是否延迟。"], []
    return 0, ["午间休市阶段使用上午最新行情快照。"], []


def _after_close_quote_penalty(parsed: datetime) -> FreshnessPenalty:
    if _before_closing_snapshot(parsed):
        return 8, ["盘后报价时间早于尾盘，收盘参考需要降权。"], []
    return 0, ["报价日期为当前交易日，盘后使用当天行情快照。"], []


def _before_morning_close_snapshot(parsed: datetime) -> bool:
    return parsed.hour < 11 or (parsed.hour == 11 and parsed.minute < 25)


def _before_closing_snapshot(parsed: datetime) -> bool:
    return parsed.hour < 14 or (parsed.hour == 14 and parsed.minute < 55)


def parse_quote_time(value: str) -> datetime | None:
    normalized = normalize_quote_event_time(value)
    return datetime.fromisoformat(normalized) if normalized is not None else None


def expected_quote_date(now: datetime) -> date:
    return trading_calendar.expected_quote_date(_local_naive_datetime(now))


def is_trading_session(now: datetime) -> bool:
    return trading_calendar.is_trading_session(now)


def is_midday_break(now: datetime) -> bool:
    return trading_calendar.is_midday_break(now)


def is_after_close(now: datetime) -> bool:
    return trading_calendar.is_after_close(now)


def weekday_gap(start: date, end: date) -> int:
    return trading_calendar.trading_day_gap(start, end)


__all__ = [
    "expected_quote_date",
    "is_after_close",
    "is_midday_break",
    "is_trading_session",
    "latest_expected_daily_kline_date",
    "latest_expected_trade_date",
    "market_local_datetime",
    "normalize_quote_event_time",
    "parse_quote_time",
    "quote_cache_lookup_seconds",
    "quote_delay_seconds",
    "quote_event_time_error",
    "quote_freshness_penalty",
    "weekday_gap",
]
