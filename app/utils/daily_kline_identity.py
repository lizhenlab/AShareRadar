"""Same-session equality shared by daily providers, persistence and scoring."""

from app.models.market import Kline


DAILY_KLINE_ADMISSION_FIELDS = (
    "open", "close", "high", "low", "volume", "adjustment_mode",
    "source", "from_cache", "fallback_used", "as_of", "fetched_at",
    "data_version", "contract_version", "session_status", "open_execution_status",
    "corporate_action_status", "adjustment_factor", "point_in_time", "execution_metadata_version",
)


def daily_kline_signature(row: Kline) -> tuple[object, ...]:
    return tuple(getattr(row, field) for field in DAILY_KLINE_ADMISSION_FIELDS)


def deduplicate_daily_klines(rows: list[Kline], *, limit: int | None = None) -> list[Kline]:
    """Preserve order and compare every observation in the selected date window.

    When a limit is supplied, the caller must provide chronological rows.
    The limit counts distinct dates, so duplicates cannot consume history slots.
    """
    if limit is not None and limit <= 0:
        raise ValueError("daily Kline date limit must be positive")
    dates = list(dict.fromkeys(row.date for row in rows))
    selected = set(dates[-limit:] if limit is not None else dates)
    by_date: dict[str, Kline] = {}
    for row in rows:
        if row.date not in selected:
            continue
        existing = by_date.get(row.date)
        if existing is not None and daily_kline_signature(existing) != daily_kline_signature(row):
            raise ValueError(f"同一交易日 {row.date} 存在冲突日K")
        by_date[row.date] = row
    return list(by_date.values())
