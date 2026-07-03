from __future__ import annotations

from datetime import date, datetime

from app.services import trading_calendar
from app.utils.time import seconds_since_text


FreshnessPenalty = tuple[int, list[str], list[str]]


def latest_expected_trade_date(now: datetime | None = None) -> date:
    return trading_calendar.latest_expected_trade_date(now)


def quote_delay_seconds(value: str, *, now: datetime | None = None) -> int | None:
    if now is None:
        seconds = seconds_since_text(value)
        if seconds is None or seconds < 0:
            return None
        return int(seconds)
    parsed = parse_quote_time(value)
    if parsed is None:
        return None
    delay_seconds = int((now - parsed).total_seconds())
    if delay_seconds < 0:
        return None
    return delay_seconds


def quote_freshness_penalty(value: str, now: datetime) -> FreshnessPenalty:
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
    try:
        return datetime.fromisoformat(value[:19])
    except ValueError:
        return None


def expected_quote_date(now: datetime) -> date:
    return trading_calendar.expected_quote_date(now)


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
    "latest_expected_trade_date",
    "parse_quote_time",
    "quote_delay_seconds",
    "quote_freshness_penalty",
    "weekday_gap",
]
