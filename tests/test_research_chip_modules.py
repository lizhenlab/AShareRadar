from __future__ import annotations

from datetime import datetime
import math

from app.services.analysis import build_analysis
from app.services.data_quality import build_data_quality
from app.services.research_chip import _pressure_bands, _support_bands, _volume_price_bins, build_chip_analysis
from app.services.research_features import build_feature_snapshot
from app.services.stock_insights import build_stock_insight_bundle
from tests.factories import make_kline, make_quote


def test_chip_analysis_filters_invalid_rows_and_keeps_distribution_available() -> None:
    rows = [_chip_kline(index, close=100 + index, volume=1000 + index * 20) for index in range(10)]
    rows.extend(
        [
            make_kline(date="2026-05-20", close=110, high=0, low=109, volume=5000),
            make_kline(date="2026-05-21", close=111, high=112, low=110, volume=0),
            make_kline(date="2026-05-22", close=112, high=113, low=111, volume=5000).model_copy(
                update={"high": math.inf}
            ),
        ]
    )
    analysis, feature = _chip_inputs(rows, price=109)

    chip = build_chip_analysis(analysis, feature)

    assert chip.center_price > 0
    assert chip.distribution_label in {"筹码相对集中", "筹码分布适中", "筹码较分散"}
    assert "10根有效日K" in chip.summary
    assert any("异常" in note for note in chip.notes)


def test_chip_analysis_requires_enough_valid_rows_after_filtering() -> None:
    rows = [_chip_kline(index, close=100 + index, volume=1000) for index in range(9)]
    rows.append(make_kline(date="2026-05-20", close=110, high=111, low=109, volume=0))
    analysis, feature = _chip_inputs(rows, price=108)

    chip = build_chip_analysis(analysis, feature)

    assert chip.distribution_label == "筹码样本不足"
    assert chip.center_price == 108
    assert chip.support_bands == []
    assert chip.pressure_bands == []


def test_volume_price_bins_ignore_invalid_rows_and_bad_bucket_count() -> None:
    rows = [
        make_kline(date="2026-05-01", close=100, high=102, low=98, volume=1000),
        make_kline(date="2026-05-02", close=101, high=0, low=99, volume=5000),
        make_kline(date="2026-05-03", close=102, high=103, low=101, volume=0),
    ]

    bins = _volume_price_bins(rows, bucket_count=4)

    assert sum(item[2] for item in bins) == 1000
    assert _volume_price_bins(rows, bucket_count=0) == []


def test_volume_price_bins_keep_flat_price_range_volume() -> None:
    rows = [
        make_kline(date="2026-05-01", close=10, high=10, low=10, volume=100),
        make_kline(date="2026-05-02", close=10, high=10, low=10, volume=200),
        make_kline(date="2026-05-03", close=10, high=10, low=10, volume=300),
    ]

    bins = _volume_price_bins(rows, bucket_count=4)

    assert len(bins) == 1
    assert bins[0][:2] == (10.0, 10.0)
    assert sum(item[2] for item in bins) == 600


def test_volume_price_bins_clamp_prices_on_upper_boundary() -> None:
    rows = [
        make_kline(date="2026-05-01", close=10, high=10, low=10, volume=100),
        make_kline(date="2026-05-02", close=12, high=12, low=12, volume=200),
    ]

    bins = _volume_price_bins(rows, bucket_count=4)

    assert (11.5, 12.0, 200) in bins
    assert sum(item[2] for item in bins) == 300


def test_chip_bands_choose_nearest_support_and_pressure_before_largest_volume() -> None:
    bins = [(80.0, 85.0, 9000.0), (95.0, 96.0, 1000.0), (104.0, 105.0, 1000.0), (115.0, 120.0, 10000.0)]
    total_volume = sum(item[2] for item in bins)

    support = _support_bands(bins, total_volume, current_price=100)
    pressure = _pressure_bands(bins, total_volume, current_price=100)

    assert (support[0].low, support[0].high) == (95.0, 96.0)
    assert (pressure[0].low, pressure[0].high) == (104.0, 105.0)


def test_chip_bands_include_current_price_bucket() -> None:
    bins = [(95.0, 98.0, 1000.0), (100.0, 105.0, 2000.0), (110.0, 112.0, 500.0)]
    total_volume = sum(item[2] for item in bins)

    support = _support_bands(bins, total_volume, current_price=102)
    pressure = _pressure_bands(bins, total_volume, current_price=102)

    assert (support[0].low, support[0].high) == (100.0, 105.0)
    assert (pressure[0].low, pressure[0].high) == (100.0, 105.0)
    assert "现价所在" in support[0].note
    assert "现价所在" in pressure[0].note


def test_chip_analysis_uses_last_valid_close_when_feature_price_is_invalid() -> None:
    rows = [_chip_kline(index, close=100 + index, volume=1000) for index in range(12)]
    analysis, feature = _chip_inputs(rows, price=111)
    feature = feature.model_copy(update={"price": math.inf})

    chip = build_chip_analysis(analysis, feature)

    assert chip.center_price > 0
    assert "-100.00%" not in chip.summary
    assert "inf" not in chip.summary.lower()


def _chip_kline(index: int, *, close: float, volume: float):
    return make_kline(date=f"2026-05-{index + 1:02d}", close=close, high=close + 1, low=close - 1, volume=volume)


def _chip_inputs(klines, *, price: float):
    quote = make_quote(price=price, prev_close=klines[-1].close, high=price + 1, low=max(0.01, price - 1), change_pct=0.0)
    quality = build_data_quality(quote, klines, now=datetime(2026, 5, 13, 16, 0, 0))
    analysis = build_analysis(quote, klines, data_quality=quality)
    feature = build_feature_snapshot(analysis, build_stock_insight_bundle(analysis))
    return analysis, feature
