from __future__ import annotations

from app.services.baostock_provider import _baostock_kline_from_row
from app.services.tushare_provider import _tushare_kline_from_row


def test_tushare_kline_from_row_filters_malformed_ohlc_rows() -> None:
    invalid = {
        "trade_date": "20260527",
        "open": 100,
        "close": 101,
        "high": 99,
        "low": 98,
        "vol": 1000,
    }
    valid = {
        "trade_date": "20260528",
        "open": 100,
        "close": 101,
        "high": 102,
        "low": 99,
        "vol": 2000,
    }

    assert _tushare_kline_from_row(invalid) is None
    parsed = _tushare_kline_from_row(valid)

    assert parsed is not None
    assert parsed.date == "2026-05-28"
    assert parsed.close == 101.0


def test_baostock_kline_from_row_filters_malformed_ohlc_rows() -> None:
    invalid = ["2026-05-27", "100", "99", "98", "101", "1000"]
    valid = ["2026-05-28", "100", "102", "99", "101", "2000"]

    assert _baostock_kline_from_row(invalid) is None
    parsed = _baostock_kline_from_row(valid)

    assert parsed is not None
    assert parsed.date == "2026-05-28"
    assert parsed.close == 101.0
