from __future__ import annotations

from dataclasses import dataclass
from typing import Callable

from app.models.schemas import AnalysisResult
from app.utils.market_data import finite_float

MIN_VALUATION_HISTORY_ROWS = 30
MIN_PEER_VALUATION_ROWS = 15


@dataclass(frozen=True)
class ValuationAnchorBasis:
    percentile: float
    prefix: str


@dataclass(frozen=True)
class ValuationAnchorBand:
    name: str
    label: str
    matches: Callable[[float], bool]


def price_percentile_from_klines(analysis: AnalysisResult) -> float | None:
    closes = [value for item in analysis.klines[-120:] if (value := _positive_float(item.close)) is not None]
    current_price = _positive_float(analysis.quote.price)
    if len(closes) < 20 or current_price is None:
        return None
    below_or_equal = sum(1 for item in closes if item <= current_price)
    return round(below_or_equal / len(closes) * 100, 1)


def valuation_percentile_from_history(analysis: AnalysisResult, field: str) -> float | None:
    rows = daily_quote_history_rows(getattr(analysis, "quote_history", []) or [])
    current = getattr(analysis.quote, field, None)
    current_value = _positive_float(current)
    if current_value is None:
        return None
    values = _positive_field_values(rows, field)
    if len(values) < MIN_VALUATION_HISTORY_ROWS:
        return None
    below_or_equal = sum(1 for item in values if item <= current_value)
    return round(below_or_equal / len(values) * 100, 1)


def daily_quote_history_rows(rows: list[dict[str, float | str | None]]) -> list[dict[str, float | str | None]]:
    by_day: dict[str, tuple[tuple[str, str, int], dict[str, float | str | None]]] = {}
    for index, row in enumerate(rows):
        day = _history_day_key(row, index)
        latest_key = _history_latest_key(row, index)
        current = by_day.get(day)
        if current is None or latest_key >= current[0]:
            by_day[day] = (latest_key, row)
    return [item[1] for item in by_day.values()]


def peer_valuation_percentile(analysis: AnalysisResult, field: str) -> float | None:
    current = _positive_float(getattr(analysis.quote, field, None))
    if current is None:
        return None
    values = peer_valuation_values(analysis, field)
    if len(values) < MIN_PEER_VALUATION_ROWS:
        return None
    below_or_equal = sum(1 for item in values if item <= current)
    return round(below_or_equal / len(values) * 100, 1)


def peer_valuation_sample_count(analysis: AnalysisResult) -> int:
    peers = getattr(analysis, "peer_quotes", []) or []
    return len(
        [
            item
            for item in peers
            if _positive_float(getattr(item, "pe", None)) is not None
            or _positive_float(getattr(item, "pb", None)) is not None
        ]
    )


def peer_valuation_values(analysis: AnalysisResult, field: str) -> list[float]:
    peers = getattr(analysis, "peer_quotes", []) or []
    return [value for item in peers if (value := _positive_float(getattr(item, field, None))) is not None]


def _positive_field_values(rows: list[dict[str, float | str | None]], field: str) -> list[float]:
    return [
        value
        for row in rows
        if (value := _positive_float(row.get(field))) is not None
    ]


def _positive_float(value: float | str | None) -> float | None:
    if isinstance(value, bool):
        return None
    number = finite_float(value)
    if number is None:
        return None
    return number if number > 0 else None


def valuation_anchor_label(
    price_percentile: float | None,
    pe_percentile: float | None = None,
    pb_percentile: float | None = None,
    peer_pe_percentile: float | None = None,
    peer_pb_percentile: float | None = None,
) -> str:
    basis = _valuation_anchor_basis(price_percentile, pe_percentile, pb_percentile, peer_pe_percentile, peer_pb_percentile)
    if basis is None:
        return "历史锚待确认"
    return f"{_valuation_anchor_band_label(basis.percentile)}{basis.prefix}锚"


def _valuation_anchor_basis(
    price_percentile: float | None,
    pe_percentile: float | None,
    pb_percentile: float | None,
    peer_pe_percentile: float | None,
    peer_pb_percentile: float | None,
) -> ValuationAnchorBasis | None:
    valuation_percentiles = _known_percentiles(pe_percentile, pb_percentile, peer_pe_percentile, peer_pb_percentile)
    if valuation_percentiles:
        return ValuationAnchorBasis(percentile=sum(valuation_percentiles) / len(valuation_percentiles), prefix="估值")
    clean_price_percentile = _percentile_float(price_percentile)
    if clean_price_percentile is not None:
        return ValuationAnchorBasis(percentile=clean_price_percentile, prefix="价格位置")
    return None


def _known_percentiles(*items: float | None) -> list[float]:
    return [value for item in items if (value := _percentile_float(item)) is not None]


def _percentile_float(value: float | None) -> float | None:
    if isinstance(value, bool):
        return None
    number = finite_float(value)
    if number is None or number < 0 or number > 100:
        return None
    return number


def _history_day_key(row: dict[str, float | str | None], index: int) -> str:
    raw_date = _history_text(row.get("trade_date") or row.get("quote_timestamp") or row.get("fetched_at"))
    return raw_date[:10] or f"row-{index}"


def _history_latest_key(row: dict[str, float | str | None], index: int) -> tuple[str, str, int]:
    return (_history_text(row.get("quote_timestamp")), _history_text(row.get("fetched_at")), index)


def _history_text(value: object) -> str:
    return str(value or "").strip()


def _valuation_anchor_band_label(percentile: float) -> str:
    for rule in VALUATION_ANCHOR_BANDS:
        if rule.matches(percentile):
            return rule.label
    return "中性"


VALUATION_ANCHOR_BANDS = (
    ValuationAnchorBand("high", "高位", lambda value: value >= 85),
    ValuationAnchorBand("elevated", "偏高", lambda value: value >= 65),
    ValuationAnchorBand("low", "低位", lambda value: value <= 20),
    ValuationAnchorBand("discount", "偏低", lambda value: value <= 35),
)


__all__ = [
    "MIN_PEER_VALUATION_ROWS",
    "MIN_VALUATION_HISTORY_ROWS",
    "VALUATION_ANCHOR_BANDS",
    "daily_quote_history_rows",
    "peer_valuation_percentile",
    "peer_valuation_sample_count",
    "peer_valuation_values",
    "price_percentile_from_klines",
    "valuation_anchor_label",
    "valuation_percentile_from_history",
]
