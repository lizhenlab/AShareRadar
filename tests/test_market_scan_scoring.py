from __future__ import annotations

from datetime import date, datetime, timedelta

import pytest

from app.models.market_scan import MarketScanResultItem
from app.models.schemas import Kline, StockInfo
from app.services.market_scan_scoring import (
    MarketScanDataMissing,
    MarketScanSkipped,
    completed_market_scan_klines,
    score_market_scan_item,
)
from app.services.market_scan_universe import build_market_scan_universe
from tests.factories import make_kline, make_quote, make_stock_info


AS_OF = datetime(2026, 7, 17, 16, 30)
DATA_DATE = date(2026, 7, 17)


def test_market_scan_score_is_deterministic_and_keeps_metadata_tags() -> None:
    item = _item(is_st=True, is_new=True, list_date=None)
    quote = _quote(fallback_used=True)
    rows = _rows(DATA_DATE, 80)
    rows[-1] = rows[-1].model_copy(update={"fallback_used": True})

    first = score_market_scan_item(
        item,
        quote,
        rows,
        as_of=AS_OF,
        completed_cutoff=DATA_DATE,
        expected_data_date=DATA_DATE,
        min_history_rows=60,
        min_data_quality_score=0,
    )
    second = score_market_scan_item(
        item,
        quote,
        rows,
        as_of=AS_OF,
        completed_cutoff=DATA_DATE,
        expected_data_date=DATA_DATE,
        min_history_rows=60,
        min_data_quality_score=0,
    )

    assert first == second
    assert first.status == "success"
    assert all(0 <= value <= 100 for value in (first.score, first.trend_score, first.leader_score, first.data_quality_score))  # type: ignore[operator]
    assert {"ST", "新股", "兜底行情", "兜底K线", "上市日期未知"}.issubset(first.tags)
    assert {"close", "ma5", "ma20", "ma60", "high20", "low20", "volume_ratio"} == set(first.metrics)
    assert first.data_date == DATA_DATE.isoformat()
    assert first.adjustment_mode == "qfq"
    assert first.quote_fallback_used is True
    assert first.kline_fallback_used is True
    assert first.metadata_degraded is True
    assert first.degradation_reasons == (
        "quote_fallback",
        "kline_fallback",
        "metadata_incomplete",
    )
    assert "综合分" in (first.reason or "")


def test_completed_market_scan_klines_excludes_future_invalid_and_deduplicates() -> None:
    earlier = make_kline(date="2026-07-16", close=10)
    replacement = make_kline(date="2026-07-16", close=11)
    current = make_kline(date="2026-07-17", close=12)
    future = make_kline(date="2026-07-18", close=13)
    invalid_date = make_kline(date="2026/07/17", close=14)

    rows = completed_market_scan_klines(
        [earlier, future, invalid_date, replacement, current],
        DATA_DATE,
    )

    assert [(row.date, row.close) for row in rows] == [
        ("2026-07-16", 11),
        ("2026-07-17", 12),
    ]


def test_market_scan_rejects_provider_bar_after_expected_trading_date() -> None:
    rows = [*_rows(DATA_DATE, 79), make_kline(date="2026-07-20", close=13)]

    with pytest.raises(MarketScanDataMissing, match="晚于应有交易日"):
        score_market_scan_item(
            _item(),
            _quote(),
            rows,
            as_of=datetime(2026, 7, 20, 16, 30),
            completed_cutoff=date(2026, 7, 20),
            expected_data_date=DATA_DATE,
            min_history_rows=60,
            min_data_quality_score=0,
        )


@pytest.mark.parametrize(
    ("item_factory", "quote_factory", "rows_factory", "expected_exception", "message"),
    [
        (
            lambda: _item(),
            lambda: _quote(code="000001", market="SZ"),
            lambda: _rows(DATA_DATE, 80),
            MarketScanDataMissing,
            "代码不匹配",
        ),
        (lambda: _item(), lambda: _quote(), lambda: _rows(DATA_DATE, 59), MarketScanSkipped, "日K不足"),
        (
            lambda: _item(),
            lambda: _quote(),
            lambda: _rows(date(2026, 7, 16), 80),
            MarketScanSkipped,
            "可能停牌",
        ),
        (
            lambda: _item(),
            lambda: _quote(timestamp="2026-07-16 15:00:00"),
            lambda: _rows(DATA_DATE, 80),
            MarketScanSkipped,
            "与完整交易日",
        ),
        (
            lambda: _item(),
            lambda: _quote(),
            lambda: [row.model_copy(update={"adjustment_mode": "none"}) for row in _rows(DATA_DATE, 80)],
            MarketScanDataMissing,
            "不是一致的前复权",
        ),
    ],
)
def test_market_scan_score_rejects_non_comparable_data(
    item_factory,
    quote_factory,
    rows_factory,
    expected_exception: type[Exception],
    message: str,
) -> None:
    with pytest.raises(expected_exception, match=message):
        score_market_scan_item(
            item_factory(),
            quote_factory(),
            rows_factory(),
            as_of=AS_OF,
            completed_cutoff=DATA_DATE,
            expected_data_date=DATA_DATE,
            min_history_rows=60,
            min_data_quality_score=0,
        )


def test_market_scan_uses_data_date_as_the_end_of_day_snapshot_boundary() -> None:
    result = score_market_scan_item(
        _item(),
        _quote(timestamp="2026-07-17 17:00:00"),
        _rows(DATA_DATE, 80),
        as_of=AS_OF,
        completed_cutoff=DATA_DATE,
        expected_data_date=DATA_DATE,
        min_history_rows=60,
        min_data_quality_score=0,
    )

    assert result.status == "success"
    assert result.data_date == DATA_DATE.isoformat()


@pytest.mark.parametrize(
    ("quote_update", "message"),
    [
        ({"volume": 0}, "成交量或成交额"),
        ({"amount": 0}, "成交量或成交额"),
        ({"turnover_rate": None}, "缺少换手率"),
    ],
)
def test_market_scan_score_rejects_missing_rankable_quote_liquidity(
    quote_update: dict[str, object],
    message: str,
) -> None:
    with pytest.raises(MarketScanDataMissing, match=message):
        score_market_scan_item(
            _item(),
            _quote().model_copy(update=quote_update),
            _rows(DATA_DATE, 80),
            as_of=AS_OF,
            completed_cutoff=DATA_DATE,
            expected_data_date=DATA_DATE,
            min_history_rows=60,
            min_data_quality_score=0,
        )


def test_market_scan_score_rejects_missing_recent_kline_volume() -> None:
    rows = _rows(DATA_DATE, 80)
    rows[-5] = rows[-5].model_copy(update={"volume": 0})

    with pytest.raises(MarketScanDataMissing, match="连续有效成交量"):
        score_market_scan_item(
            _item(),
            _quote(),
            rows,
            as_of=AS_OF,
            completed_cutoff=DATA_DATE,
            expected_data_date=DATA_DATE,
            min_history_rows=60,
            min_data_quality_score=0,
        )


def test_market_scan_universe_deduplicates_and_marks_st_new_and_delisted() -> None:
    rows = [
        make_stock_info("600519", "SH").model_copy(
            update={"name": "*ST茅台", "industry": "白酒", "list_date": "2026-07-01"}
        ),
        make_stock_info("600519", "SH").model_copy(update={"name": "重复股"}),
        make_stock_info("000001", "SZ").model_copy(update={"name": "退市样本"}),
        make_stock_info("920066", "BJ").model_copy(update={"name": "北交样本", "list_date": "20240703"}),
        make_stock_info("600000", "SH").model_copy(update={"symbol": "000001.SZ", "name": "字段冲突"}),
        make_stock_info("600001", "SH").model_copy(
            update={"symbol": "600001.SZ", "market": "SZ", "name": "交易所错配"}
        ),
        make_stock_info("900901", "SH").model_copy(update={"name": "沪市B股"}),
        make_stock_info("200002", "SZ").model_copy(update={"name": "深市B股"}),
        StockInfo(
            symbol="123456.HK",
            code="123456",
            market="HK",
            name="非A股",
            source="test",
            updated_at="2026-07-17 16:00:00",
        ),
    ]

    universe = build_market_scan_universe(rows, data_date=DATA_DATE, new_stock_days=120)

    assert [seed.symbol for seed in universe.seeds] == ["920066.BJ", "600519.SH"]
    assert universe.excluded_count == 7
    by_symbol = {seed.symbol: seed for seed in universe.seeds}
    assert by_symbol["600519.SH"].is_st is True
    assert by_symbol["600519.SH"].is_new is True
    assert by_symbol["600519.SH"].list_date == "2026-07-01"
    assert by_symbol["600519.SH"].metadata_source == rows[0].source
    assert by_symbol["920066.BJ"].is_new is False
    assert by_symbol["920066.BJ"].list_date == "2024-07-03"


def _item(
    *,
    is_st: bool = False,
    is_new: bool = False,
    list_date: str | None = "2001-08-27",
) -> MarketScanResultItem:
    return MarketScanResultItem(
        run_id=1,
        symbol="600519.SH",
        code="600519",
        market="SH",
        name="贵州茅台",
        industry="白酒",
        list_date=list_date,
        is_st=is_st,
        is_new=is_new,
        status="pending",
        updated_at="2026-07-17 16:30:00",
    )


def _quote(
    *,
    code: str = "600519",
    market: str = "SH",
    timestamp: str = "2026-07-17 15:00:00",
    fallback_used: bool = False,
):
    return make_quote(
        price=10.5,
        prev_close=10.0,
        high=10.8,
        low=9.9,
        change_pct=5.0,
        turnover_rate=4.5,
        timestamp=timestamp,
    ).model_copy(
        update={
            "code": code,
            "market": market,
            "name": "贵州茅台",
            "amount": 900_000_000,
            "change": 0.5,
            "fallback_used": fallback_used,
        }
    )


def _rows(latest: date, count: int) -> list[Kline]:
    days: list[date] = []
    cursor = latest
    while len(days) < count:
        if cursor.weekday() < 5:
            days.append(cursor)
        cursor -= timedelta(days=1)
    days.reverse()
    return [
        make_kline(
            date=day.isoformat(),
            close=8 + index * 0.04,
            volume=1_000_000 + index * 20_000,
            source="test-qfq",
            as_of=latest.isoformat(),
            data_version=f"test|qfq|{latest.isoformat()}",
        )
        for index, day in enumerate(days)
    ]
