from __future__ import annotations

from datetime import datetime, timedelta


DEFAULT_TEXT_FORMAT = "%Y-%m-%d %H:%M:%S"


def now_text() -> str:
    return datetime.now().strftime(DEFAULT_TEXT_FORMAT)


def datetime_to_text(value: datetime | None) -> str | None:
    return value.strftime(DEFAULT_TEXT_FORMAT) if value else None


def seconds_ago_text(seconds: int) -> str:
    return (datetime.now() - timedelta(seconds=seconds)).strftime(DEFAULT_TEXT_FORMAT)


def parse_text_time(value: str) -> datetime:
    return datetime.strptime(value, DEFAULT_TEXT_FORMAT)


def seconds_since_text(value: str | None) -> float | None:
    if not value:
        return None
    try:
        parsed = parse_text_time(value)
    except ValueError:
        return None
    return (datetime.now() - parsed).total_seconds()


def non_negative_seconds_since_text(value: str | None) -> float | None:
    seconds = seconds_since_text(value)
    if seconds is None or seconds < 0:
        return None
    return seconds
