from __future__ import annotations

from datetime import date, timedelta

import pytest

from app.services.market_scan_universe import build_market_scan_universe
from tests.factories import make_stock_info


DATA_DATE = date(2026, 7, 28)


def _stock(code: str, *, name: str, list_date: object):
    return make_stock_info(code=code, market="SH").model_copy(
        update={"name": name, "list_date": list_date}
    )


def test_market_scan_universe_distinguishes_unknown_future_and_new_listing_dates() -> None:
    boundary = DATA_DATE - timedelta(days=120)
    rows = [
        _stock("600001", name="边界新股", list_date=boundary.isoformat()),
        _stock("600002", name="已过新股期", list_date=(boundary - timedelta(days=1)).strftime("%Y%m%d")),
        _stock("600003", name="日期未知", list_date=None),
        _stock("600004", name="日期无效", list_date="2026-02-30"),
        _stock("600005", name="尚未上市", list_date=(DATA_DATE + timedelta(days=1)).isoformat()),
    ]

    universe = build_market_scan_universe(rows, data_date=DATA_DATE, new_stock_days=120)

    by_symbol = {item.symbol: item for item in universe.seeds}
    assert set(by_symbol) == {"600001.SH", "600002.SH", "600003.SH", "600004.SH"}
    assert by_symbol["600001.SH"].is_new is True
    assert by_symbol["600002.SH"].is_new is False
    assert by_symbol["600003.SH"].list_date is None
    assert by_symbol["600003.SH"].is_new is False
    assert by_symbol["600004.SH"].list_date is None
    assert universe.unknown_list_date_count == 2
    assert universe.future_list_date_count == 1
    assert universe.excluded_count == 1


@pytest.mark.parametrize("name", ["ST样本", "*ST样本", "S*ST样本", "SST样本"])
def test_market_scan_universe_recognizes_only_risk_warning_st_prefixes(name: str) -> None:
    universe = build_market_scan_universe(
        [_stock("600011", name=name, list_date="20000101")],
        data_date=DATA_DATE,
        new_stock_days=120,
    )

    assert universe.seeds[0].is_st is True


@pytest.mark.parametrize("name", ["BEST科技", "STELLAR", "First Solar", "测试ST公司"])
def test_market_scan_universe_does_not_match_st_inside_regular_names(name: str) -> None:
    universe = build_market_scan_universe(
        [_stock("600012", name=name, list_date="20000101")],
        data_date=DATA_DATE,
        new_stock_days=120,
    )

    assert universe.seeds[0].is_st is False


@pytest.mark.parametrize("new_stock_days", [-1, True])
def test_market_scan_universe_rejects_invalid_new_stock_window(new_stock_days: int) -> None:
    with pytest.raises(ValueError, match="新股天数必须为非负整数"):
        build_market_scan_universe([], data_date=DATA_DATE, new_stock_days=new_stock_days)


def test_market_scan_universe_rejects_conflicting_duplicate_regardless_of_order() -> None:
    first = _stock("600021", name="普通样本", list_date="20000101")
    conflicting = first.model_copy(update={"name": "*ST冲突样本", "industry": "冲突行业"})

    for rows in ([first, conflicting], [conflicting, first]):
        with pytest.raises(ValueError, match="同一股票存在冲突元数据：600021.SH"):
            build_market_scan_universe(
                list(rows),
                data_date=DATA_DATE,
                new_stock_days=120,
            )
