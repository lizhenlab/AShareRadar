from __future__ import annotations

from dataclasses import dataclass
from datetime import date, datetime

from app.models.market_scan import MarketScanMode
from app.services.data_quality_time import latest_expected_daily_kline_date
from app.services.trading_calendar import (
    DAILY_KLINE_PUBLISH_TIME,
    MORNING_SESSION_START_TIME,
    is_trading_day,
)


OFFICIAL_SCAN_WINDOW_MESSAGE = "盘后正式扫描仅使用当日完整日K，请在交易日 15:15 后启动"
INTRADAY_SCAN_WINDOW_MESSAGE = "盘中临时扫描仅在交易日 09:30 至 15:15 之间可用"


@dataclass(frozen=True)
class MarketScanTemporalContract:
    mode: MarketScanMode
    data_date: date
    quote_date: date


def market_scan_temporal_contract(
    value: datetime,
    mode: MarketScanMode,
) -> MarketScanTemporalContract:
    if mode not in {"official", "intraday"}:
        raise ValueError(f"未知全市场扫描模式：{mode}")
    if mode == "intraday":
        if (
            not is_trading_day(value.date())
            or value.time() < MORNING_SESSION_START_TIME
            or value.time() >= DAILY_KLINE_PUBLISH_TIME
        ):
            raise ValueError(INTRADAY_SCAN_WINDOW_MESSAGE)
        return MarketScanTemporalContract(
            mode=mode,
            data_date=latest_expected_daily_kline_date(value),
            quote_date=value.date(),
        )

    if is_trading_day(value.date()) and value.time() < DAILY_KLINE_PUBLISH_TIME:
        raise ValueError(OFFICIAL_SCAN_WINDOW_MESSAGE)
    data_date = latest_expected_daily_kline_date(value)
    return MarketScanTemporalContract(mode=mode, data_date=data_date, quote_date=data_date)


__all__ = [
    "INTRADAY_SCAN_WINDOW_MESSAGE",
    "MarketScanTemporalContract",
    "OFFICIAL_SCAN_WINDOW_MESSAGE",
    "market_scan_temporal_contract",
]
