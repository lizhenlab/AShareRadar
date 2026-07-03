from __future__ import annotations

import asyncio

import pytest

from app.services.futu_provider import (
    FutuProvider,
    _futu_kltype,
    _minute_klines_from_response,
    _order_book_from_response,
    _order_book_levels,
    _ordered_snapshot_quotes,
)


class _Frame:
    def __init__(self, rows):
        self.rows = rows

    def iterrows(self):
        return iter(enumerate(self.rows))


def test_futu_quotes_empty_request_does_not_require_optional_dependency() -> None:
    provider = FutuProvider(enabled=False)

    assert asyncio.run(provider.quotes([])) == []


def test_order_book_levels_skip_malformed_prices_keep_zero_volume_and_cap_depth() -> None:
    rows = [
        ("bad", 100),
        (99.9,),
        (99.8, -1),
        (99.7, 0),
        (99.6, 10),
        (99.5, 20),
        (99.4, 30),
        (99.3, 40),
        (99.2, 50),
        (99.1, 60),
    ]

    levels = _order_book_levels(rows)

    assert [(item.price, item.volume) for item in levels] == [
        (99.7, 0.0),
        (99.6, 10.0),
        (99.5, 20.0),
        (99.4, 30.0),
        (99.3, 40.0),
    ]


def test_order_book_response_rejects_empty_depth_after_filtering() -> None:
    with pytest.raises(RuntimeError, match="Futu盘口深度为空"):
        _order_book_from_response("600519.SH", {"Bid": [("bad", 1)], "Ask": []}, source_name="Futu OpenAPI")

    book = _order_book_from_response("600519.SH", {"Bid": [(99.9, 100)], "Ask": []}, source_name="Futu OpenAPI")
    assert [(item.price, item.volume) for item in book.bid] == [(99.9, 100.0)]
    assert book.ask == []


def test_ordered_snapshot_quotes_reports_requested_a_share_missing_after_filtering() -> None:
    frame = _Frame(
        [
            {"code": "HK.00700", "stock_name": "腾讯控股", "last_price": 400.0},
            {"code": "SH.600519", "stock_name": "贵州茅台", "last_price": 1303.0, "prev_close_price": 1273.38},
        ]
    )

    with pytest.raises(RuntimeError, match="000001.SZ"):
        _ordered_snapshot_quotes(["600519.SH", "000001.SZ"], frame, source_name="Futu OpenAPI")


def test_ordered_snapshot_quotes_filters_invalid_critical_prices() -> None:
    frame = _Frame(
        [
            {"code": "SH.600519", "stock_name": "贵州茅台", "last_price": "bad", "prev_close_price": 1273.38},
            {"code": "SZ.000001", "stock_name": "平安银行", "last_price": 11.2, "prev_close_price": 11.0, "high_price": 10.8},
        ]
    )

    with pytest.raises(RuntimeError, match="600519.SH,000001.SZ"):
        _ordered_snapshot_quotes(["600519.SH", "000001.SZ"], frame, source_name="Futu OpenAPI")


def test_minute_klines_from_response_filters_invalid_rows_and_uses_normalized_interval() -> None:
    frame = _Frame(
        [
            {"time_key": "", "open": 10, "close": 10, "high": 11, "low": 9, "volume": 100},
            {"time_key": "2026-05-13 09:35:00", "open": 10, "close": 0, "high": 11, "low": 9, "volume": 100},
            {"time_key": "2026-05-13 09:38:00", "open": 10, "close": 10.5, "high": 10.2, "low": 9, "volume": 100},
            {"time_key": "2026-05-13 09:40:00", "open": 10, "close": 10.5, "high": 11, "low": 9, "volume": 100},
        ]
    )

    rows = _minute_klines_from_response(frame, interval="5m", source_name="Futu OpenAPI")

    assert len(rows) == 1
    assert rows[0].interval == "5m"
    assert rows[0].timestamp == "2026-05-13 09:40:00"


def test_futu_kltype_normalizes_interval_and_rejects_unknown() -> None:
    class KLType:
        K_5M = "5 minute"

    assert _futu_kltype(KLType, " 5M ") == "5 minute"
    with pytest.raises(ValueError, match="Futu 分钟周期"):
        _futu_kltype(KLType, "2m")
