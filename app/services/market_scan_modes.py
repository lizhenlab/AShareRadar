from __future__ import annotations

from dataclasses import dataclass
from datetime import date, datetime

from app.models.market_scan import MarketScanMode
from app.services.data_quality_time import latest_expected_daily_kline_date
from app.services.trading_calendar import (
    CALL_AUCTION_START_TIME,
    DAILY_KLINE_PUBLISH_TIME,
    MORNING_SESSION_START_TIME,
    TradingCalendarCoverageError,
    is_trading_day,
)
from app.utils.market_time import market_local_naive


OFFICIAL_SCAN_WINDOW_MESSAGE = "盘后正式扫描仅使用当日完整日K，请在交易日 15:15 后启动"
INTRADAY_SCAN_WINDOW_MESSAGE = "盘中临时扫描仅在交易日 09:30 至 15:15 之间可用"
PREOPEN_SCAN_WINDOW_MESSAGE = "盘前复盘仅可在交易日 00:00（含）至 09:15（不含）运行"


@dataclass(frozen=True)
class MarketScanTemporalContract:
    mode: MarketScanMode
    data_date: date
    quote_date: date


def market_scan_temporal_contract(
    value: datetime,
    mode: MarketScanMode,
) -> MarketScanTemporalContract:
    current = market_local_naive(value)
    if mode not in {"official", "intraday", "preopen"}:
        raise ValueError(f"未知全市场扫描模式：{mode}")
    if mode == "preopen":
        if not is_trading_day(current.date()) or current.time() >= CALL_AUCTION_START_TIME:
            raise ValueError(PREOPEN_SCAN_WINDOW_MESSAGE)
        data_date = latest_expected_daily_kline_date(current)
        return MarketScanTemporalContract(
            mode=mode,
            data_date=data_date,
            quote_date=data_date,
        )
    if mode == "intraday":
        if (
            not is_trading_day(current.date())
            or current.time() < MORNING_SESSION_START_TIME
            or current.time() >= DAILY_KLINE_PUBLISH_TIME
        ):
            raise ValueError(INTRADAY_SCAN_WINDOW_MESSAGE)
        return MarketScanTemporalContract(
            mode=mode,
            data_date=latest_expected_daily_kline_date(current),
            quote_date=current.date(),
        )

    if is_trading_day(current.date()) and current.time() < DAILY_KLINE_PUBLISH_TIME:
        raise ValueError(OFFICIAL_SCAN_WINDOW_MESSAGE)
    data_date = latest_expected_daily_kline_date(current)
    return MarketScanTemporalContract(mode=mode, data_date=data_date, quote_date=data_date)


def current_official_source_temporal_contract_matches(
    source_date: date,
    *,
    as_of: datetime,
    captured_at: datetime,
) -> bool:
    """Bind a current probability source to the official session at its decision time."""
    if (
        as_of.tzinfo is None
        or as_of.utcoffset() is None
        or captured_at.tzinfo is None
        or captured_at.utcoffset() is None
        or captured_at < as_of
    ):
        return False
    try:
        temporal = market_scan_temporal_contract(as_of, "official")
    except (TradingCalendarCoverageError, ValueError):
        return False
    return temporal.data_date == source_date and temporal.quote_date == source_date


__all__ = [
    "INTRADAY_SCAN_WINDOW_MESSAGE",
    "MarketScanTemporalContract",
    "OFFICIAL_SCAN_WINDOW_MESSAGE",
    "PREOPEN_SCAN_WINDOW_MESSAGE",
    "current_official_source_temporal_contract_matches",
    "market_scan_temporal_contract",
]
