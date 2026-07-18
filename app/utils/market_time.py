from __future__ import annotations

from datetime import date, datetime
from typing import Any
from zoneinfo import ZoneInfo


ASHARE_TIMEZONE = ZoneInfo("Asia/Shanghai")
MARKET_DATETIME_FORMAT = "%Y-%m-%d %H:%M:%S"


def normalize_market_datetime(value: Any, *, event_date: Any = None) -> str | None:
    if isinstance(value, datetime):
        return market_local_naive(value).strftime(MARKET_DATETIME_FORMAT)

    text = str(value or "").strip()
    if not text or text.lower() in {"nan", "nat", "none", "null", "n/a", "--"}:
        return None
    compact = text.replace("/", "-").replace("T", " ")

    parsed = _parse_compact_datetime(compact)
    compact_digits = compact.split(".", 1)[0]
    if compact_digits.isdigit() and len(compact_digits) in {12, 14}:
        return market_local_naive(parsed).strftime(MARKET_DATETIME_FORMAT) if parsed is not None else None
    if parsed is None and ":" in compact:
        parsed = _parse_iso_datetime(compact, event_date)
    if parsed is None:
        parsed = _parse_epoch_datetime(compact)
    return market_local_naive(parsed).strftime(MARKET_DATETIME_FORMAT) if parsed is not None else None


def market_datetime_epoch(value: Any) -> float | None:
    normalized = normalize_market_datetime(value)
    if normalized is None:
        return None
    parsed = datetime.strptime(normalized, MARKET_DATETIME_FORMAT).replace(tzinfo=ASHARE_TIMEZONE)
    return parsed.timestamp()


def market_local_naive(value: datetime) -> datetime:
    if value.tzinfo is None or value.utcoffset() is None:
        return value.replace(tzinfo=None)
    return value.astimezone(ASHARE_TIMEZONE).replace(tzinfo=None)


def _parse_compact_datetime(value: str) -> datetime | None:
    digits = value.split(".", 1)[0]
    date_format = {14: "%Y%m%d%H%M%S", 12: "%Y%m%d%H%M"}.get(len(digits))
    if date_format is None or not digits.isdigit():
        return None
    try:
        return datetime.strptime(digits, date_format)
    except ValueError:
        return None


def _parse_iso_datetime(value: str, event_date: Any) -> datetime | None:
    candidate = value
    if _looks_like_time_only(value):
        date_text = _event_date_text(event_date)
        if date_text is None:
            return None
        candidate = f"{date_text} {value}"
    try:
        return datetime.fromisoformat(candidate)
    except ValueError:
        return None


def _parse_epoch_datetime(value: str) -> datetime | None:
    try:
        timestamp = float(value)
    except ValueError:
        return None
    if timestamp > 10**12:
        timestamp /= 1000
    if timestamp <= 0 or timestamp >= 10**11:
        return None
    try:
        return datetime.fromtimestamp(timestamp, ASHARE_TIMEZONE).replace(tzinfo=None)
    except (OSError, OverflowError, ValueError):
        return None


def _looks_like_time_only(value: str) -> bool:
    return "-" not in value and len(value.split(" ", 1)[0]) <= 8


def _event_date_text(value: Any) -> str | None:
    if isinstance(value, datetime):
        return market_local_naive(value).strftime("%Y-%m-%d")
    if isinstance(value, date):
        return value.strftime("%Y-%m-%d")
    text = str(value or "").strip().replace("/", "-")
    if len(text) == 8 and text.isdigit():
        text = f"{text[:4]}-{text[4:6]}-{text[6:]}"
    try:
        return datetime.fromisoformat(text[:10]).date().isoformat()
    except ValueError:
        return None


__all__ = [
    "ASHARE_TIMEZONE",
    "MARKET_DATETIME_FORMAT",
    "market_datetime_epoch",
    "market_local_naive",
    "normalize_market_datetime",
]
