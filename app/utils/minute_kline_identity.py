"""Exact minute-bar instants shared by provider cleanup and cache reads."""

from datetime import datetime

from app.models.market import MinuteKline
from app.utils.market_data import filter_valid_minute_klines
from app.utils.market_time import market_local_naive


def parse_minute_timestamp(value: object) -> datetime | None:
    if not isinstance(value, str):
        return None
    try:
        return market_local_naive(datetime.fromisoformat(value))
    except (ValueError, OverflowError):
        return None


def minute_timestamp_key(value: object) -> str | None:
    parsed = parse_minute_timestamp(value)
    return parsed.isoformat(timespec="microseconds") if parsed is not None else None


def deduplicate_minute_klines(rows: list[MinuteKline]) -> list[MinuteKline]:
    by_instant: dict[datetime, MinuteKline] = {}
    for row in filter_valid_minute_klines(rows):
        instant = parse_minute_timestamp(row.timestamp)
        if instant is not None:
            by_instant[instant] = row
    return [by_instant[instant] for instant in sorted(by_instant)]
