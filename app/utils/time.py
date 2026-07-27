from __future__ import annotations

from datetime import datetime, timedelta

from app.utils.clock import market_now_naive
from app.utils.market_time import market_local_naive


DEFAULT_TEXT_FORMAT = "%Y-%m-%d %H:%M:%S"


def now_text() -> str:
    return market_now_naive().strftime(DEFAULT_TEXT_FORMAT)


def datetime_to_text(value: datetime | None) -> str | None:
    return market_local_naive(value).strftime(DEFAULT_TEXT_FORMAT) if value else None


def seconds_ago_text(seconds: int) -> str:
    return (market_now_naive() - timedelta(seconds=seconds)).strftime(DEFAULT_TEXT_FORMAT)


def parse_text_time(value: str) -> datetime:
    text = str(value or "").strip()
    if not text:
        raise ValueError("timestamp is empty")
    try:
        parsed = datetime.fromisoformat(text.replace("Z", "+00:00"))
    except ValueError:
        parsed = datetime.strptime(text, DEFAULT_TEXT_FORMAT)
    return market_local_naive(parsed)


def seconds_since_text(value: str | None) -> float | None:
    if not value:
        return None
    try:
        parsed = parse_text_time(value)
    except ValueError:
        return None
    return (market_now_naive() - parsed).total_seconds()


def non_negative_seconds_since_text(value: str | None) -> float | None:
    seconds = seconds_since_text(value)
    if seconds is None or seconds < 0:
        return None
    return seconds
