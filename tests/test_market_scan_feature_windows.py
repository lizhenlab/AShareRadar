from __future__ import annotations

import pytest

from app.services.market_scan_feature_windows import (
    MARKET_SCAN_FEATURE_WINDOW_CONTRACT_VERSION,
    snapshot_reference_offset,
    snapshot_return_pct,
    snapshot_skip_return_pct,
)


def test_price_windows_cover_all_horizons_for_completed_and_intraday_snapshots() -> None:
    closes = [float(value) for value in range(1, 62)]

    assert MARKET_SCAN_FEATURE_WINDOW_CONTRACT_VERSION == "market-scan-feature-windows-v1"
    for mode in ("official", "preopen"):
        assert [snapshot_reference_offset(mode, horizon) for horizon in (1, 5, 20, 60)] == [2, 6, 21, 61]
        assert [
            snapshot_return_pct(61.0, closes, horizon=horizon, mode=mode)
            for horizon in (1, 5, 20, 60)
        ] == pytest.approx(
            [(61 / reference - 1) * 100 for reference in (60, 56, 41, 1)]
        )

    assert [snapshot_reference_offset("intraday", horizon) for horizon in (1, 5, 20, 60)] == [1, 5, 20, 60]
    assert [
        snapshot_return_pct(62.0, closes, horizon=horizon, mode="intraday")
        for horizon in (1, 5, 20, 60)
    ] == pytest.approx(
        [(62 / reference - 1) * 100 for reference in (61, 57, 42, 2)]
    )


def test_skip_windows_shift_exactly_one_bar_for_intraday() -> None:
    closes = [float(value) for value in range(1, 62)]

    official_20 = snapshot_skip_return_pct(
        closes,
        skip_sessions=5,
        lookback_sessions=20,
        mode="official",
    )
    intraday_20 = snapshot_skip_return_pct(
        closes,
        skip_sessions=5,
        lookback_sessions=20,
        mode="intraday",
    )
    official_55 = snapshot_skip_return_pct(
        closes,
        skip_sessions=5,
        lookback_sessions=55,
        mode="official",
    )
    intraday_55 = snapshot_skip_return_pct(
        closes,
        skip_sessions=5,
        lookback_sessions=55,
        mode="intraday",
    )

    assert official_20 == pytest.approx((56 / 36 - 1) * 100)
    assert intraday_20 == pytest.approx((57 / 37 - 1) * 100)
    assert official_55 == pytest.approx((56 / 1 - 1) * 100)
    assert intraday_55 == pytest.approx((57 / 2 - 1) * 100)


def test_price_window_contract_fails_closed_on_invalid_mode_window_or_history() -> None:
    with pytest.raises(ValueError, match="未知全市场扫描模式"):
        snapshot_return_pct(10, [9, 10], horizon=1, mode="bad")  # type: ignore[arg-type]
    with pytest.raises(ValueError, match="必须是正整数"):
        snapshot_reference_offset("official", 0)
    with pytest.raises(ValueError, match="60日收益至少需要 61 根"):
        snapshot_return_pct(10, [9.0] * 60, horizon=60, mode="official")


@pytest.mark.parametrize("reference", (0.0, float("nan"), float("inf")))
def test_price_window_contract_rejects_nonpositive_or_nonfinite_reference(
    reference: float,
) -> None:
    with pytest.raises(ValueError, match="参考收盘价无效"):
        snapshot_return_pct(
            10.0,
            [reference, 9.0],
            horizon=1,
            mode="official",
        )


@pytest.mark.parametrize("current", (0.0, float("nan"), float("inf")))
def test_price_window_contract_rejects_nonpositive_or_nonfinite_snapshot(
    current: float,
) -> None:
    with pytest.raises(ValueError, match="快照价格无效"):
        snapshot_return_pct(
            current,
            [8.0, 9.0],
            horizon=1,
            mode="official",
        )
