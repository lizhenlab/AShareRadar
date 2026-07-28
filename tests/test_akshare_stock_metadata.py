from __future__ import annotations

import pytest

from app.services.akshare_mappers import (
    AKSHARE_STOCK_INDUSTRY_FIELDS,
    stock_info_from_code_name_row,
)


BASE_ROW = {
    "证券代码": "600519",
    "证券简称": "贵州茅台",
    "上市日期": "20010827",
}


@pytest.mark.parametrize("field", AKSHARE_STOCK_INDUSTRY_FIELDS)
def test_akshare_stock_mapper_recognizes_industry_aliases(field: str) -> None:
    item = stock_info_from_code_name_row(
        {**BASE_ROW, field: "  电力 　设备  "},
        stamp="2026-07-28 16:00:00",
        source_name="AKShare",
    )

    assert item is not None
    assert item.industry == "电力设备"


def test_akshare_stock_mapper_normalizes_field_names_and_skips_empty_aliases() -> None:
    item = stock_info_from_code_name_row(
        {**BASE_ROW, "industry": " -- ", " 所属 行业 名称 ": "  银 行  "},
        stamp="2026-07-28 16:00:00",
        source_name="AKShare",
    )

    assert item is not None
    assert item.industry == "银行"


@pytest.mark.parametrize("placeholder", [None, "", "  ", "NaN", "<NA>", "null", "N/A", "--", "—", "暂无", "未知", "未分类"])
def test_akshare_stock_mapper_does_not_treat_placeholder_as_industry(placeholder: object) -> None:
    item = stock_info_from_code_name_row(
        {**BASE_ROW, "所属行业": placeholder},
        stamp="2026-07-28 16:00:00",
        source_name="AKShare",
    )

    assert item is not None
    assert item.industry is None


@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("list_date", 20010827.0),
        ("上市时间", "2001/08/27"),
        ("挂牌日期", "2001-08-27 00:00:00"),
        ("首发上市日期", "20010827"),
    ],
)
def test_akshare_stock_mapper_normalizes_listing_date_aliases(field: str, value: object) -> None:
    row = {key: value for key, value in BASE_ROW.items() if key != "上市日期"}
    row[field] = value

    item = stock_info_from_code_name_row(
        row,
        stamp="2026-07-28 16:00:00",
        source_name="AKShare",
    )

    assert item is not None
    assert item.list_date == "2001-08-27"


def test_akshare_stock_mapper_rejects_invalid_listing_date() -> None:
    item = stock_info_from_code_name_row(
        {**BASE_ROW, "上市日期": "2001-02-30"},
        stamp="2026-07-28 16:00:00",
        source_name="AKShare",
    )

    assert item is not None
    assert item.list_date is None


def test_akshare_stock_mapper_uses_valid_listing_date_after_invalid_alias() -> None:
    item = stock_info_from_code_name_row(
        {**BASE_ROW, "list_date": "2001-02-30", "上市日期": "20010827"},
        stamp="2026-07-28 16:00:00",
        source_name="AKShare",
    )

    assert item is not None
    assert item.list_date == "2001-08-27"
