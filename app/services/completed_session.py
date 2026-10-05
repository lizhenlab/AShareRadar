"""Shared close-time and ordered daily-observation admission for stock scoring."""

from __future__ import annotations

from datetime import date, datetime, time
import re

from app.models.market import Kline
from app.utils.market_time import market_local_naive


def completed_quote_day(timestamp: str) -> date | None:
    if not isinstance(timestamp, str):
        return None
    if re.fullmatch(r"\d{4}-\d{2}-\d{2}[ T]\d{2}:\d{2}:\d{2}(?:\.\d+)?(?:Z|[+-]\d{2}:\d{2})?", timestamp) is None:
        return None
    try:
        event = market_local_naive(datetime.fromisoformat(timestamp))
    except ValueError:
        return None
    return event.date() if event.time() >= time(15) else None


def dated_rows_through_quote(rows: list[Kline], quote_day: date) -> list[Kline] | None:
    previous: date | None = None
    for row in rows:
        if not isinstance(row.date, str) or re.fullmatch(r"\d{4}-\d{2}-\d{2}", row.date) is None:
            return None
        try:
            day = date.fromisoformat(row.date)
        except ValueError:
            return None
        if day > quote_day or (previous is not None and day <= previous):
            return None
        previous = day
    return rows
