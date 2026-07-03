from __future__ import annotations

import pytest

from app.utils.symbols import normalize_symbol, standard_symbol, tencent_symbol


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
