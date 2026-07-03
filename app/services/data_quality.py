from __future__ import annotations

from datetime import datetime

from app.models.schemas import DataQuality, Kline, Quote
from app.services.data_quality_components import build_data_quality_report
from app.services.data_quality_kline import (
    assess_kline_quality,
    kline_quality_penalty as _kline_quality_penalty,
    kline_source as _kline_source,
    latest_kline_date as _latest_kline_date,
    parse_kline_date as _parse_kline_date,
)
from app.services.data_quality_time import (
    expected_quote_date as _expected_quote_date,
    is_after_close as _is_after_close,
    is_midday_break as _is_midday_break,
    is_trading_session as _is_trading_session,
    latest_expected_trade_date,
    parse_quote_time as _parse_quote_time,
    quote_delay_seconds as _quote_delay_seconds,
    quote_freshness_penalty as _quote_freshness_penalty,
    weekday_gap as _weekday_gap,
)


def build_data_quality(
    quote: Quote,
    klines: list[Kline],
    *,
    consistency_level: str = "未校验",
    consistency_notes: list[str] | None = None,
    consistency_penalty: int = 0,
    require_kline: bool = True,
    now: datetime | None = None,
) -> DataQuality:
    current = now or datetime.now()
    return build_data_quality_report(
        quote,
        klines,
        consistency_level=consistency_level,
        consistency_notes=consistency_notes,
        consistency_penalty=consistency_penalty,
        require_kline=require_kline,
        now=current,
    )


__all__ = [
    "_expected_quote_date",
    "_is_after_close",
    "_is_midday_break",
    "_is_trading_session",
    "_kline_quality_penalty",
    "_kline_source",
    "_latest_kline_date",
    "_parse_kline_date",
    "_parse_quote_time",
    "_quote_delay_seconds",
    "_quote_freshness_penalty",
    "_weekday_gap",
    "assess_kline_quality",
    "build_data_quality",
    "latest_expected_trade_date",
]
