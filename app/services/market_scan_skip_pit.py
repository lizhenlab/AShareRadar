"""Point-in-time quote and partial-bar contract for justified scan skips."""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from datetime import date
import math

from app.models.market import (
    DAILY_KLINE_CONTRACT_VERSION,
    UNKNOWN_KLINE_DATA_VERSION,
    Kline,
    Quote,
)
from app.services.market_scan_score_contract import stable_score_spec_hash
from app.utils.market_data import valid_kline, valid_quote
from app.utils.market_time import market_datetime_epoch, normalize_market_datetime
from app.utils.symbols import standard_symbol


MARKET_SCAN_SKIP_PIT_CONTRACT_VERSION = "market-scan-skip-pit-v1"
_PIT_KEYS = {
    "contract_version",
    "quote_contract",
    "quote_contract_digest",
    "quote_observed_at",
    "bar_contract",
    "bar_contract_digest",
}
_QUOTE_KEYS = {
    "code",
    "name",
    "market",
    "price",
    "prev_close",
    "open",
    "high",
    "low",
    "volume",
    "amount",
    "change",
    "change_pct",
    "turnover_rate",
    "timestamp",
    "source",
    "from_cache",
    "fallback_used",
}
_BAR_ROW_LENGTH = 13


def build_market_scan_skip_pit(
    quote: Quote,
    rows: Sequence[Kline],
    *,
    quote_observed_at: str,
) -> dict[str, object]:
    quote_contract = _quote_contract(quote)
    bar_contract = _bar_contract(rows)
    return {
        "contract_version": MARKET_SCAN_SKIP_PIT_CONTRACT_VERSION,
        "quote_contract": quote_contract,
        "quote_contract_digest": stable_score_spec_hash(quote_contract),
        "quote_observed_at": _required_timestamp(quote_observed_at),
        "bar_contract": bar_contract,
        "bar_contract_digest": stable_score_spec_hash(bar_contract),
    }


def verify_market_scan_skip_pit(
    value: object,
    *,
    expected_symbol: str,
    expected_data_date: str,
    expected_quote_date: str,
    expected_as_of: str,
    expected_bar_dates: Sequence[str],
    expected_quote_timestamp: object,
    expected_quote_observed_at: object,
    expected_quote_source: object,
    expected_kline_source: object,
    expected_adjustment_mode: object,
) -> bool:
    parts = _verified_pit_contract_parts(value)
    if parts is None or not isinstance(value, Mapping):
        return False
    quote_value, bars_value = parts
    quote = _verified_quote(
        quote_value,
        expected_symbol=expected_symbol,
        expected_quote_date=expected_quote_date,
    )
    bars = _verified_bars(
        bars_value,
        expected_dates=expected_bar_dates,
        decision_as_of=expected_as_of,
    )
    observed_at = normalize_market_datetime(value.get("quote_observed_at"))
    decision_epoch = market_datetime_epoch(expected_as_of)
    event_epoch = market_datetime_epoch(quote.timestamp) if quote is not None else None
    observed_epoch = market_datetime_epoch(observed_at)
    if (
        quote is None
        or bars is None
        or observed_at is None
        or decision_epoch is None
        or event_epoch is None
        or observed_epoch is None
        or event_epoch > observed_epoch
        or observed_epoch > decision_epoch
        or bars[-1].date != expected_data_date
        or not _same_quote_close(quote.price, bars[-1].close)
    ):
        return False
    return (
        quote.timestamp == expected_quote_timestamp
        and observed_at == normalize_market_datetime(expected_quote_observed_at)
        and quote.source == expected_quote_source
        and bars[-1].source == expected_kline_source
        and expected_adjustment_mode == "qfq"
    )


def _verified_pit_contract_parts(
    value: object,
) -> tuple[Mapping[str, object], Sequence[object]] | None:
    if not isinstance(value, Mapping) or set(value) != _PIT_KEYS:
        return None
    quote = value.get("quote_contract")
    bars = value.get("bar_contract")
    if (
        value.get("contract_version") != MARKET_SCAN_SKIP_PIT_CONTRACT_VERSION
        or not isinstance(quote, Mapping)
        or value.get("quote_contract_digest") != stable_score_spec_hash(dict(quote))
        or not isinstance(bars, Sequence)
        or isinstance(bars, str | bytes)
        or value.get("bar_contract_digest") != stable_score_spec_hash(list(bars))
    ):
        return None
    return quote, bars


def _verified_quote(
    value: Mapping[str, object],
    *,
    expected_symbol: str,
    expected_quote_date: str,
) -> Quote | None:
    if set(value) != _QUOTE_KEYS:
        return None
    try:
        quote = Quote.model_validate(value)
    except (TypeError, ValueError):
        return None
    if (
        not valid_quote(quote)
        or quote.from_cache
        or quote.fallback_used
        or quote.volume <= 0
        or quote.amount <= 0
        or quote.turnover_rate is None
        or quote.turnover_rate < 0
        or _single_price_quote(quote)
        or not quote.source.strip()
        or str(quote.timestamp)[:10] != expected_quote_date
        or standard_symbol(f"{quote.code}.{quote.market}") != expected_symbol
    ):
        return None
    return quote


def _verified_bars(
    value: Sequence[object],
    *,
    expected_dates: Sequence[str],
    decision_as_of: str,
) -> list[Kline] | None:
    expected = list(expected_dates)
    if not expected or len(value) != len(expected):
        return None
    decision_epoch = market_datetime_epoch(decision_as_of)
    result: list[Kline] = []
    previous_snapshot_epoch: float | None = None
    for raw in value:
        row = _bar_from_contract(raw)
        if row is None:
            return None
        snapshot_epoch = _kline_snapshot_epoch(row.as_of)
        row_start_epoch = market_datetime_epoch(f"{row.date} 00:00:00")
        if (
            snapshot_epoch is None
            or row_start_epoch is None
            or decision_epoch is None
            or snapshot_epoch < row_start_epoch
            or snapshot_epoch > decision_epoch
            or previous_snapshot_epoch is not None
            and snapshot_epoch < previous_snapshot_epoch
        ):
            return None
        previous_snapshot_epoch = snapshot_epoch
        result.append(row)
    return result if [row.date for row in result] == expected else None


def _kline_snapshot_epoch(value: object) -> float | None:
    timestamp = market_datetime_epoch(value)
    if timestamp is not None:
        return timestamp
    text = str(value or "")
    try:
        parsed = date.fromisoformat(text)
    except ValueError:
        return None
    if parsed.isoformat() != text:
        return None
    return market_datetime_epoch(f"{text} 00:00:00")


def _bar_from_contract(value: object) -> Kline | None:
    if (
        not isinstance(value, Sequence)
        or isinstance(value, str | bytes)
        or len(value) != _BAR_ROW_LENGTH
    ):
        return None
    try:
        row = Kline(
            date=str(value[0]),
            open=_finite_number(value[1]),
            close=_finite_number(value[2]),
            high=_finite_number(value[3]),
            low=_finite_number(value[4]),
            volume=_finite_number(value[5]),
            adjustment_mode=str(value[6]),
            data_version=str(value[7]),
            contract_version=str(value[8]),
            as_of=str(value[9]),
            source=str(value[10]),
            from_cache=bool(value[11]),
            fallback_used=bool(value[12]),
        )
    except (TypeError, ValueError):
        return None
    if (
        not valid_kline(row)
        or row.adjustment_mode != "qfq"
        or not row.data_version.strip()
        or row.data_version == UNKNOWN_KLINE_DATA_VERSION
        or row.contract_version != DAILY_KLINE_CONTRACT_VERSION
        or not str(row.source or "").strip()
        or row.fallback_used
        or row.volume <= 0
        or not isinstance(value[11], bool)
        or not isinstance(value[12], bool)
    ):
        return None
    return row


def _quote_contract(quote: Quote) -> dict[str, object]:
    return {key: getattr(quote, key) for key in _QUOTE_KEYS}


def _bar_contract(rows: Sequence[Kline]) -> list[list[object]]:
    return [
        [
            row.date,
            float(row.open),
            float(row.close),
            float(row.high),
            float(row.low),
            float(row.volume),
            row.adjustment_mode,
            row.data_version,
            row.contract_version,
            row.as_of,
            row.source,
            bool(row.from_cache),
            bool(row.fallback_used),
        ]
        for row in rows
    ]


def _same_quote_close(price: float, close: float) -> bool:
    absolute_gap = abs(price - close)
    relative_limit = max(price, close) * 0.5 / 100
    return absolute_gap <= max(0.02, relative_limit)


def _single_price_quote(quote: Quote) -> bool:
    prices = (quote.open, quote.high, quote.low, quote.price)
    tolerance = max(0.0001, quote.price * 1e-8)
    return max(prices) - min(prices) <= tolerance


def _finite_number(value: object) -> float:
    if isinstance(value, bool) or not isinstance(value, int | float):
        raise TypeError("PIT 数值字段无效")
    result = float(value)
    if not math.isfinite(result):
        raise ValueError("PIT 数值字段不是有限数")
    return result


def _required_timestamp(value: object) -> str:
    normalized = normalize_market_datetime(value)
    if normalized is None:
        raise ValueError("报价观测时点无效")
    return normalized


__all__ = [
    "MARKET_SCAN_SKIP_PIT_CONTRACT_VERSION",
    "build_market_scan_skip_pit",
    "verify_market_scan_skip_pit",
]
