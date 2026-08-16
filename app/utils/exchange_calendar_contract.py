"""Deterministic bundled A-share exchange-calendar checks for lower layers.

Domain models cannot import the service-layer calendar resolver.  Public
contracts nevertheless need a fail-closed answer for historical session
identity, so this adapter validates the immutable bundled baseline only.  A
date beyond that baseline is unavailable until the repository ships a newer
calendar, rather than silently degrading to a weekday approximation.
"""

from __future__ import annotations

from datetime import date
from functools import lru_cache
import json
from pathlib import Path


_CALENDAR_PATH = Path(__file__).resolve().parents[1] / "resources" / "trading_calendar.json"


@lru_cache(maxsize=1)
def bundled_exchange_sessions() -> frozenset[date]:
    try:
        raw = json.loads(_CALENDAR_PATH.read_text(encoding="utf-8"))
        values = raw["trade_dates"]
        sessions = tuple(date.fromisoformat(value) for value in values)
    except (OSError, KeyError, TypeError, ValueError, json.JSONDecodeError):
        return frozenset()
    if (
        not sessions
        or list(values) != sorted(set(values))
        or raw.get("trade_date_count") != len(sessions)
        or raw.get("min_date") != sessions[0].isoformat()
        or raw.get("max_date") != sessions[-1].isoformat()
    ):
        return frozenset()
    return frozenset(sessions)


def is_bundled_exchange_session(value: date) -> bool:
    """Return true only when the bundled exchange calendar attests the date."""
    return value in bundled_exchange_sessions()


__all__ = ["bundled_exchange_sessions", "is_bundled_exchange_session"]
