from __future__ import annotations

from datetime import UTC, datetime, timedelta, tzinfo
from zoneinfo import ZoneInfo

from app.utils.clock import ASHARE_TIMEZONE, utc_now


def audit_now_text() -> str:
    return audit_datetime_to_text(utc_now())


def audit_datetime_to_text(
    value: datetime,
    *,
    legacy_timezone: str | tzinfo = ASHARE_TIMEZONE,
) -> str:
    normalized = _audit_datetime(value, legacy_timezone=legacy_timezone)
    return normalized.isoformat(timespec="microseconds").replace("+00:00", "Z")


def audit_seconds_ago_text(seconds: int) -> str:
    return audit_datetime_to_text(utc_now() - timedelta(seconds=seconds))


def audit_time_window(seconds: int) -> tuple[str, str] | None:
    if seconds <= 0:
        return None
    current = utc_now()
    return (
        audit_datetime_to_text(current - timedelta(seconds=seconds)),
        audit_datetime_to_text(current),
    )


def parse_audit_time(
    value: str,
    *,
    legacy_timezone: str | tzinfo = ASHARE_TIMEZONE,
) -> datetime:
    text = str(value or "").strip()
    if not text:
        raise ValueError("audit timestamp is empty")
    if len(text) >= 10 and text[4] == "/" and text[7] == "/":
        text = f"{text[:4]}-{text[5:7]}-{text[8:]}"
    parsed = datetime.fromisoformat(text.replace("Z", "+00:00"))
    return _audit_datetime(parsed, legacy_timezone=legacy_timezone)


def normalize_audit_time_text(
    value: str,
    *,
    legacy_timezone: str | tzinfo = ASHARE_TIMEZONE,
) -> str:
    return audit_datetime_to_text(
        parse_audit_time(value, legacy_timezone=legacy_timezone),
    )


def audit_time_epoch(value: object) -> float | None:
    if not isinstance(value, str):
        return None
    try:
        return parse_audit_time(value).timestamp()
    except (TypeError, ValueError):
        return None


def _audit_datetime(
    value: datetime,
    *,
    legacy_timezone: str | tzinfo,
) -> datetime:
    if value.tzinfo is None or value.utcoffset() is None:
        value = value.replace(tzinfo=_timezone(legacy_timezone))
    return value.astimezone(UTC)


def _timezone(value: str | tzinfo) -> tzinfo:
    return ZoneInfo(value) if isinstance(value, str) else value


__all__ = [
    "audit_datetime_to_text",
    "audit_now_text",
    "audit_seconds_ago_text",
    "audit_time_epoch",
    "audit_time_window",
    "normalize_audit_time_text",
    "parse_audit_time",
]
