from __future__ import annotations

from dataclasses import dataclass
from datetime import date, datetime

from app.models.schemas import AnalysisResult, Kline, Quote
from app.services.indicators import average_volume
from app.utils.market_data import finite_float, valid_positive_number


@dataclass(frozen=True)
class CurrentVolumeMetrics:
    latest_volume: float
    avg_volume: float | None
    volume_ratio: float | None
    history_count: int


@dataclass(frozen=True)
class AbnormalEventContext:
    quote: Quote
    latest_date: str
    prev_close: float
    change_pct: float
    avg_volume: float | None
    latest_volume: float
    volume_ratio: float | None
    amplitude_pct: float
    upper_shadow_pct: float
    lower_shadow_pct: float


def build_abnormal_context(analysis: AnalysisResult) -> AbnormalEventContext:
    quote = analysis.quote
    rows = analysis.klines[-25:]
    prev_close = _previous_close(quote, rows)
    volume_metrics = current_volume_metrics(quote, rows)
    amplitude_pct = _price_range_pct(quote.high, quote.low, prev_close)
    upper_shadow_pct, lower_shadow_pct = _shadow_percentages(quote, prev_close)

    return AbnormalEventContext(
        quote=quote,
        latest_date=quote.timestamp,
        prev_close=prev_close,
        change_pct=quote.change_pct,
        avg_volume=volume_metrics.avg_volume,
        latest_volume=volume_metrics.latest_volume,
        volume_ratio=volume_metrics.volume_ratio,
        amplitude_pct=amplitude_pct,
        upper_shadow_pct=upper_shadow_pct,
        lower_shadow_pct=lower_shadow_pct,
    )


def current_volume_metrics(quote: Quote, rows: list[Kline], *, avg_window: int = 5) -> CurrentVolumeMetrics:
    if _quote_volume_is_usable(quote) and _quote_volume_matches_rows(quote, rows):
        completed_rows = completed_kline_rows(quote, rows)
        return _volume_metrics(quote.volume, average_volume(completed_rows, avg_window), len(completed_rows))
    return _fallback_volume_metrics(quote, rows, avg_window)


def completed_kline_rows(quote: Quote, rows: list[Kline]) -> list[Kline]:
    if not rows:
        return []
    quote_date = _date_part(quote.timestamp)
    latest_kline_date = _date_part(rows[-1].date)
    if quote_date is None or latest_kline_date is None:
        return rows[:-1]
    return rows[:-1] if latest_kline_date >= quote_date else rows


def _previous_close(quote: Quote, rows: list[Kline]) -> float:
    prev_close = _positive_number(quote.prev_close)
    if prev_close is not None:
        return prev_close
    if len(rows) >= 2:
        fallback_close = _positive_number(rows[-2].close)
        if fallback_close is not None:
            return fallback_close
    return _positive_number(quote.open) or 0


def _quote_volume_is_usable(quote: Quote) -> bool:
    return valid_positive_number(quote.volume)


def _quote_volume_matches_rows(quote: Quote, rows: list[Kline]) -> bool:
    if not rows:
        return True
    quote_date = _date_part(quote.timestamp)
    latest_kline_date = _date_part(rows[-1].date)
    return quote_date is not None and latest_kline_date is not None and quote_date >= latest_kline_date


def _fallback_volume_metrics(quote: Quote, rows: list[Kline], avg_window: int) -> CurrentVolumeMetrics:
    latest_volume = _non_negative_number(rows[-1].volume) if rows else _fallback_quote_volume(quote)
    if latest_volume is None:
        latest_volume = _fallback_quote_volume(quote)
    history_rows = rows[:-1]
    return _volume_metrics(latest_volume, average_volume(history_rows, avg_window), len(history_rows))


def _fallback_quote_volume(quote: Quote) -> float:
    return _non_negative_number(quote.volume) or 0


def _volume_metrics(latest_volume: float, avg_volume: float | None, history_count: int) -> CurrentVolumeMetrics:
    return CurrentVolumeMetrics(
        latest_volume=latest_volume,
        avg_volume=avg_volume,
        volume_ratio=_volume_ratio(latest_volume, avg_volume),
        history_count=history_count,
    )


def _volume_ratio(latest_volume: float | None, avg_volume: float | None) -> float | None:
    latest = _positive_number(latest_volume)
    average = _positive_number(avg_volume)
    return latest / average if latest is not None and average is not None else None


def _date_part(value: str | None) -> date | None:
    if not value:
        return None
    text = str(value).strip()
    for parser in (_iso_date_part, _compact_date_part):
        parsed = parser(text)
        if parsed is not None:
            return parsed
    return None


def _iso_date_part(value: str) -> date | None:
    try:
        return datetime.fromisoformat(value[:10]).date()
    except ValueError:
        return None


def _compact_date_part(value: str) -> date | None:
    try:
        return datetime.strptime(value[:8], "%Y%m%d").date()
    except ValueError:
        return None


def _price_range_pct(high: float, low: float, base: float) -> float:
    high_value = finite_float(high)
    low_value = finite_float(low)
    if high_value is None or low_value is None:
        return 0
    return _safe_pct(high_value - low_value, base)


def _shadow_percentages(quote: Quote, base: float) -> tuple[float, float]:
    values = [finite_float(item) for item in (quote.open, quote.price, quote.high, quote.low)]
    if any(item is None for item in values):
        return 0, 0
    open_value, price, high, low = values
    assert open_value is not None and price is not None and high is not None and low is not None
    body_high = max(open_value, price)
    body_low = min(open_value, price)
    return _safe_pct(high - body_high, base), _safe_pct(body_low - low, base)


def _safe_pct(value: float, base: float) -> float:
    clean_value = finite_float(value)
    clean_base = _positive_number(base)
    return clean_value / clean_base * 100 if clean_value is not None and clean_base is not None else 0


def _positive_number(value: float | None) -> float | None:
    parsed = finite_float(value)
    return parsed if parsed is not None and parsed > 0 else None


def _non_negative_number(value: float | None) -> float | None:
    parsed = finite_float(value)
    return parsed if parsed is not None and parsed >= 0 else None


__all__ = [
    "AbnormalEventContext",
    "CurrentVolumeMetrics",
    "build_abnormal_context",
    "completed_kline_rows",
    "current_volume_metrics",
]
