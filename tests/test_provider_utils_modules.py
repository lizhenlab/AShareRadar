from __future__ import annotations

import math

import pytest

from app.services.provider_errors import ProviderCoverageMiss
from app.services.provider_utils import ak_symbol, bs_symbol, ensure_positive_limit, pick, ts_symbol, valid_ohlc


def test_pick_uses_first_present_non_nan_value() -> None:
    row = {"missing": math.nan, "name": "贵州茅台"}

    assert pick(row, "unknown", "missing", "name", default="--") == "贵州茅台"


def test_pick_returns_default_for_expected_missing_field_errors() -> None:
    assert pick({}, "missing", default="--") == "--"
    assert pick(None, "missing", default="--") == "--"


def test_pick_does_not_swallow_unexpected_row_errors() -> None:
    class BrokenRow:
        def __getitem__(self, _name: str) -> object:
            raise RuntimeError("adapter broke")

    with pytest.raises(RuntimeError, match="adapter broke"):
        pick(BrokenRow(), "name", default="--")


def test_ensure_positive_limit_rejects_zero_and_negative_values() -> None:
    ensure_positive_limit(1)
    with pytest.raises(ValueError, match="limit 必须大于 0"):
        ensure_positive_limit(0)
    with pytest.raises(ValueError, match="rows 必须大于 0"):
        ensure_positive_limit(-1, label="rows")


def test_valid_ohlc_requires_positive_prices_and_bounds() -> None:
    assert valid_ohlc(100, 101, 102, 99) is True
    assert valid_ohlc(100, 101, 99, 98) is False
    assert valid_ohlc(100, 101, 102, 101.5) is False
    assert valid_ohlc(100, 0, 102, 99) is False
    assert valid_ohlc("bad", 101, 102, 99) is False
    assert valid_ohlc(math.inf, math.inf, math.inf, 99) is False
    assert valid_ohlc(100, 101, math.inf, 99) is False
    assert valid_ohlc(100, 101, 102, -math.inf) is False
    assert valid_ohlc(math.nan, 101, 102, 99) is False


def test_provider_symbol_helpers_route_beijing_without_changing_sh_sz() -> None:
    assert ak_symbol("920066.BJ") == "920066"
    assert ts_symbol("430047") == "430047.BJ"
    assert ts_symbol("920066.BJ") == "920066.BJ"
    assert ts_symbol("600519") == "600519.SH"
    assert ts_symbol("000001") == "000001.SZ"
    assert bs_symbol("600519.SH") == "sh.600519"
    assert bs_symbol("000001.SZ") == "sz.000001"


def test_baostock_symbol_helper_explicitly_rejects_beijing_market() -> None:
    with pytest.raises(ProviderCoverageMiss, match="BaoStock.*不覆盖北交所.*920066.BJ"):
        bs_symbol("920066.BJ")
