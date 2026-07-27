from __future__ import annotations

from datetime import UTC, datetime
import time
from zoneinfo import ZoneInfo


ASHARE_TIMEZONE = ZoneInfo("Asia/Shanghai")


def utc_now() -> datetime:
    """Return an aware UTC audit timestamp."""

    return datetime.now(UTC)


def market_now() -> datetime:
    """Return an aware Shanghai market timestamp."""

    return utc_now().astimezone(ASHARE_TIMEZONE)


def market_now_naive() -> datetime:
    """Return the legacy SQLite/UI representation of Shanghai market time."""

    return market_now().replace(tzinfo=None)


def monotonic_now() -> float:
    """Return a process-local clock suitable for TTLs and deadlines."""

    return time.monotonic()


def performance_now() -> float:
    """Return a high-resolution process-local duration clock."""

    return time.perf_counter()


__all__ = [
    "ASHARE_TIMEZONE",
    "market_now",
    "market_now_naive",
    "monotonic_now",
    "performance_now",
    "utc_now",
]
