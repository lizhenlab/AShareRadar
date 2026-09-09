"""Nominal price must not change otherwise equal relative trend evidence."""

import sqlite3

import pytest

from app.services.indicator_trend import (
    TREND_SCORE_LEGACY_ALGORITHM_VERSION, trend_score, trend_score_snapshot,
)
from app.services.indicator_trend_components import build_trend_context, continuous_relative_impact
from app.repositories.market_scan_results import assign_result_ranks
from app.services.market_scan_rank_refinement import market_scan_rank_refinement
from app.services.market_scan_scoring import score_market_scan_item
from tests.test_market_scan_scoring import AS_OF, DATA_DATE, _item, _quote, _rows


def _inputs(scale: int, change: float):
    closes = [1.20] * 79 + [round(1.20 + change, 2)]
    rows = [row.model_copy(update={
        "open": round(close * scale, 2), "close": round(close * scale, 2),
        "high": round((close + .01) * scale, 2),
        "low": round((close - .01) * scale, 2),
        "volume": 1_000_000 / scale,
    }) for row, close in zip(_rows(DATA_DATE, 80), closes, strict=True)]
    current, previous = round(closes[-1] * scale, 2), round(1.20 * scale, 2)
    quote = _quote().model_copy(update={
        "price": current, "open": current, "high": rows[-1].high,
        "low": rows[-1].low, "prev_close": previous,
        "change": current - previous, "change_pct": change / 1.20 * 100,
        "volume": 1_000_000 / scale,
    })
    return quote, rows


def _score(quote, rows):
    return score_market_scan_item(
        _item(), quote, rows, as_of=AS_OF, completed_cutoff=DATA_DATE,
        expected_data_date=DATA_DATE, min_history_rows=61, min_data_quality_score=50,
    )


@pytest.mark.parametrize("change", [-.04, .02, .04])
def test_equal_two_decimal_price_paths_keep_public_trend_contributions(change):
    snapshots = [trend_score_snapshot(*_inputs(scale, change)) for scale in (1, 10, 100)]
    assert len({score for score, _label, _contributions in snapshots}) == 1
    assert all(
        [row.impact for row in snapshot[2]] == [row.impact for row in snapshots[0][2]]
        for snapshot in snapshots
    )


@pytest.mark.parametrize("change", [-.04, .02, .04])
def test_equal_two_decimal_price_paths_keep_full_market_rank_inputs(change):
    inputs = [_inputs(scale, change) for scale in (1, 10, 100)]
    results = [_score(*pair) for pair in inputs]
    refinements = [market_scan_rank_refinement(*pair) for pair in inputs]
    assert all(result.status == "success" for result in results)
    assert refinements[1:] == refinements[:-1]
    assert len({result.data_quality_score for result in results}) == 1
    assert len({result.trend_score for result in results}) == 1
    assert len({result.raw_score for result in results}) == 1


def test_qfq_subcent_history_keeps_relative_trend_scale():
    snapshots = []
    for scale in (1, 10, 100):
        quote, rows = _inputs(scale, .04)
        for index in range(len(rows) - 1):
            close = round((1.2001 if index % 2 else 1.2019) * scale, 4)
            rows[index] = rows[index].model_copy(update={"open": close, "close": close})
        snapshots.append(_score(quote, rows))
    assert len({row.trend_score for row in snapshots}) == 1
    assert len({row.raw_score for row in snapshots}) == 1


@pytest.mark.parametrize("change,expected", [(-.04, [26, 28]), (.02, [72, 71]), (.04, [86, 84])])
def test_explicit_legacy_algorithm_keeps_original_cent_rounding(change, expected):
    assert [
        trend_score(*_inputs(scale, change), algorithm_version=TREND_SCORE_LEGACY_ALGORITHM_VERSION)[0]
        for scale in (1, 10)
    ] == expected


@pytest.mark.parametrize("count", [0, 10, 20, 80])
@pytest.mark.parametrize("algorithm", [None, "unknown", "market-scan-trend-v999", []])
def test_public_trend_rejects_unknown_algorithm_even_with_short_samples(count, algorithm):
    quote, rows = _inputs(1, .04)
    for function in (build_trend_context, trend_score, trend_score_snapshot):
        with pytest.raises(ValueError, match="未知趋势评分算法"):
            function(quote, rows[:count], algorithm_version=algorithm)


def test_equal_scaled_scores_use_symbol_tie_break_in_sqlite():
    values = [(symbol, _score(*_inputs(scale, .04))) for symbol, scale in (
        ("600003.SH", 1), ("600001.SH", 10), ("600002.SH", 100),
    )]
    with sqlite3.connect(":memory:") as conn:
        conn.execute("CREATE TABLE market_scan_result (run_id INTEGER, symbol TEXT, status TEXT, score REAL, raw_score REAL, rank INTEGER)")
        conn.executemany(
            "INSERT INTO market_scan_result VALUES (1, ?, 'success', ?, ?, NULL)",
            [(symbol, row.score, row.raw_score) for symbol, row in values],
        )
        assign_result_ranks(conn, 1)
        assert conn.execute("SELECT symbol, rank FROM market_scan_result ORDER BY rank").fetchall() == [
            ("600001.SH", 1), ("600002.SH", 2), ("600003.SH", 3),
        ]


def _boundary_inputs(scale):
    quote, rows = _inputs(scale, .04)
    closes = [6.40] * 75 + [6.37, 6.38, 6.38, 6.38, 6.49]
    rows = [row.model_copy(update={
        "open": round(close * scale, 2), "close": round(close * scale, 2),
        "high": round((close + .01) * scale, 2), "low": round((close - .01) * scale, 2),
    }) for row, close in zip(rows, closes, strict=True)]
    quote = quote.model_copy(update={
        "price": round(6.49 * scale, 2), "open": round(6.49 * scale, 2),
        "high": round(6.50 * scale, 2), "low": round(6.48 * scale, 2),
        "prev_close": round(6.38 * scale, 2), "change": .11 * scale,
        "change_pct": .11 / 6.38 * 100,
    })
    return quote, rows


def test_legal_price_path_at_half_impact_boundary_has_stable_full_market_rank():
    results = [_score(*_boundary_inputs(scale)) for scale in (1, 10, 100)]
    assert [result.trend_score for result in results] == [71, 71, 71]
    assert len({result.raw_score for result in results}) == 1
    assert [trend_score_snapshot(*_boundary_inputs(scale))[2][0].impact for scale in (1, 10, 100)] == [6, 6, 6]
    assert [trend_score_snapshot(
        *_boundary_inputs(scale), algorithm_version=TREND_SCORE_LEGACY_ALGORITHM_VERSION,
    )[2][0].impact for scale in (1, 10, 100)] == [5, 6, 6]


@pytest.mark.parametrize("cap", [5, 6, 7, 8, 10, 12])
@pytest.mark.parametrize("sign", [-1, 1])
def test_dimensionless_rounding_stabilizes_thresholds_without_erasing_nearby_sides(cap, sign):
    for integer in range(cap):
        threshold = .10 + (integer + .5) / cap * 1.90
        for displacement, magnitude in ((-1e-8, integer), (0.0, integer + 1), (1e-8, integer + 1)):
            for scale in (1, 10, 100):
                reference = 6.4 * scale
                impact, _ = continuous_relative_impact(
                    reference * (1 + sign * (threshold + displacement) / 100), reference,
                    positive_impact=cap, negative_impact=-cap,
                )
                assert impact == sign * magnitude


@pytest.mark.parametrize("sign", [-1, 1])
def test_neutral_band_preserves_exact_relative_boundary_across_price_scales(sign):
    for scale in (1, 10, 100):
        impact, relative = continuous_relative_impact(
            (10.00 + sign * .01) * scale, 10.00 * scale, positive_impact=8, negative_impact=-8,
        )
        assert impact == 0
        assert relative == sign * .10
