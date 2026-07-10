from __future__ import annotations

import pytest

from app.utils.symbols import normalize_symbol, standard_symbol, standard_symbol_list, tencent_symbol


def test_normalize_symbol_accepts_common_market_forms() -> None:
    assert normalize_symbol("600519") == ("600519", "sh")
    assert normalize_symbol("000001") == ("000001", "sz")
    assert normalize_symbol("600519.SH") == ("600519", "sh")
    assert normalize_symbol("SH.600519") == ("600519", "sh")
    assert normalize_symbol("sz000001") == ("000001", "sz")
    assert normalize_symbol("SZ-300750") == ("300750", "sz")


def test_symbol_format_helpers_keep_provider_conventions() -> None:
    assert standard_symbol("sh600519") == "600519.SH"
    assert tencent_symbol("000001.SZ") == "sz000001"


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
