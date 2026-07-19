from __future__ import annotations

import pytest

from app.utils.symbols import (
    is_a_share_stock_code,
    normalize_symbol,
    standard_symbol,
    standard_symbol_list,
    tencent_symbol,
)


def test_normalize_symbol_accepts_common_market_forms() -> None:
    assert normalize_symbol("600519") == ("600519", "sh")
    assert normalize_symbol("000001") == ("000001", "sz")
    assert normalize_symbol("600519.SH") == ("600519", "sh")
    assert normalize_symbol("SH.600519") == ("600519", "sh")
    assert normalize_symbol("sz000001") == ("000001", "sz")
    assert normalize_symbol("SZ-300750") == ("300750", "sz")


@pytest.mark.parametrize("code", ["430047", "830799", "872392", "880001", "920066"])
def test_normalize_symbol_infers_beijing_stock_prefixes(code: str) -> None:
    assert normalize_symbol(code) == (code, "bj")
    assert standard_symbol(code) == f"{code}.BJ"


@pytest.mark.parametrize("symbol", ["430047.BJ", "BJ.830799", "bj872392", "BJ-920066"])
def test_normalize_symbol_accepts_explicit_beijing_market_forms(symbol: str) -> None:
    code, market = normalize_symbol(symbol)

    assert market == "bj"
    assert standard_symbol(symbol) == f"{code}.BJ"


def test_symbol_format_helpers_keep_provider_conventions() -> None:
    assert standard_symbol("sh600519") == "600519.SH"
    assert tencent_symbol("000001.SZ") == "sz000001"
    assert tencent_symbol("920066.BJ") == "bj920066"


def test_beijing_inference_does_not_change_shanghai_or_shenzhen_rules() -> None:
    assert normalize_symbol("688001") == ("688001", "sh")
    assert normalize_symbol("900901") == ("900901", "sh")
    assert normalize_symbol("300750") == ("300750", "sz")
    assert normalize_symbol("002594") == ("002594", "sz")


@pytest.mark.parametrize(
    ("code", "market", "expected"),
    [
        ("600519", "SH", True),
        ("688001", "sh", True),
        ("000001", "SZ", True),
        ("300750", "SZ", True),
        ("920066", "BJ", True),
        ("900901", "SH", False),
        ("200002", "SZ", False),
        ("600519", "SZ", False),
        ("000000", "SZ", False),
        ("bad", "SH", False),
    ],
)
def test_is_a_share_stock_code_excludes_b_shares_and_market_mismatches(
    code: str,
    market: str,
    expected: bool,
) -> None:
    assert is_a_share_stock_code(code, market) is expected


@pytest.mark.parametrize("symbol", ["430047.SZ", "sh920066", "600519.BJ", "BJ.000001"])
def test_normalize_symbol_rejects_beijing_market_mismatches(symbol: str) -> None:
    with pytest.raises(ValueError, match="市场标识不一致"):
        normalize_symbol(symbol)


def test_standard_symbol_list_dedupes_and_counts_invalid_or_duplicate_values() -> None:
    result = standard_symbol_list(
        [" SZ000001 ", "bad", None, "", "000001.SZ", "sh600519", "000000"],
        skip_invalid=True,
        count_duplicates_as_skipped=True,
    )

    assert result.symbols == ["000001.SZ", "600519.SH"]
    assert result.skipped_count == 5


def test_standard_symbol_list_limit_can_error_or_truncate() -> None:
    symbols = ["600000", "600001", "600002"]

    with pytest.raises(ValueError, match="一次最多查询 2 个股票代码"):
        standard_symbol_list(symbols, max_items=2)
    assert standard_symbol_list(symbols, max_items=2, truncate=True).symbols == ["600000.SH", "600001.SH"]


def test_normalize_symbol_rejects_malformed_codes() -> None:
    with pytest.raises(ValueError, match="6位数字"):
        normalize_symbol("60051")
    with pytest.raises(ValueError, match="6位数字"):
        normalize_symbol("abc001")
    with pytest.raises(ValueError, match="不能全为0"):
        normalize_symbol("000000")
    with pytest.raises(ValueError, match="6位数字"):
        normalize_symbol("sh600519.sz")
    with pytest.raises(ValueError, match="6位数字"):
        normalize_symbol("s.h600519")
