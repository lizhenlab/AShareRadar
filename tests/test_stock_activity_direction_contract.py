from __future__ import annotations

from datetime import date, timedelta

import pytest

from app.models.analysis import AnalysisResult, DataQuality
from app.models.market import Kline
from app.services.analysis import build_analysis
from app.services.stock_activity import build_fund_flow_analysis
from app.services.stock_insights import build_stock_insight_bundle
from tests.factories import make_kline, make_quote


def _rows(*, count: int = 5, scale: float = 1.0) -> list[Kline]:
    end = date(2026, 9, 11)
    return [
        make_kline(date=(end - timedelta(days=count - index - 1)).isoformat(), volume=1000 * scale)
        for index in range(count)
    ]


def _analysis(
    *, change: float = 3.0, ratio: float = 1.8, amount: float = 8_000_000_000,
    scale: float = 1.0, rows: list[Kline] | None = None, timestamp: str = "2026-09-14 15:00:00",
    quote_updates: dict | None = None,
) -> AnalysisResult:
    quote = make_quote(
        price=100 + change, prev_close=100, high=max(100 + change, 100) + 1,
        low=min(100 + change, 100) - 1, change_pct=change, turnover_rate=5, timestamp=timestamp,
    ).model_copy(update={"open": 100, "volume": 1000 * ratio * scale, "amount": amount, **(quote_updates or {})})
    klines = _rows(scale=scale) if rows is None else rows
    return build_analysis(
        quote, klines,
        data_quality=DataQuality(level="可信", source=quote.source, quote_time=quote.timestamp, kline_count=len(klines), score=100),
    )


def _assert_unavailable(analysis: AnalysisResult) -> None:
    report = build_fund_flow_analysis(analysis)
    assert report.available is False
    assert report.data_nature == "unavailable"
    assert report.overall_score == 50
    assert report.level == "不可用"
    assert report.price_volume_relation == "量价代理输入不足，方向不可用。"
    assert all(window.score == 50 and "未生成窗口方向结论" in window.summary for window in report.windows)


@pytest.mark.parametrize(("change", "expected"), [(-3, 31), (0, 50), (3, 69)])
def test_price_direction_survives_high_absolute_activity(change: float, expected: int) -> None:
    report = build_fund_flow_analysis(_analysis(change=change))
    assert report.available is True
    assert report.overall_score == expected
    assert report.score_rule_version == "current-price-volume.v2"


@pytest.mark.parametrize("ratio", [0.1, 0.5, 0.7, 1.0, 1.25, 1.8, 3.1, 100.0])
def test_relative_volume_only_scales_price_direction(ratio: float) -> None:
    positive = build_fund_flow_analysis(_analysis(change=3, ratio=ratio)).overall_score
    negative = build_fund_flow_analysis(_analysis(change=-3, ratio=ratio)).overall_score
    neutral = build_fund_flow_analysis(_analysis(change=0, ratio=ratio)).overall_score
    expected = round(50 + 15 * min(1.25, max(0.5, ratio)))
    assert positive == expected
    assert negative == 100 - expected
    assert neutral == 50


@pytest.mark.parametrize("change", [-3.0, 0.0, 3.0])
def test_amount_capitalization_and_turnover_do_not_change_direction(change: float) -> None:
    baseline = build_fund_flow_analysis(_analysis(change=change, amount=100_000_000))
    for scale, amount, turnover in [(80, 8_000_000_000, 99), (1, 0, None), (1, -1000, -1), (1, float("nan"), 0)]:
        report = build_fund_flow_analysis(_analysis(change=change, amount=amount, scale=scale, quote_updates={"turnover_rate": turnover}))
        assert report.available is True
        assert report.data_nature == "derived"
        assert report.overall_score == baseline.overall_score


def test_missing_amount_preserves_valid_volume_evidence_in_real_bundle() -> None:
    bundle = build_stock_insight_bundle(_analysis(amount=0))
    assert bundle.fund_flow.available is True
    assert bundle.fund_flow.data_nature == "derived"
    assert bundle.fund_flow.overall_score > 50
    flow_factor = next(factor for factor in bundle.overview.factors if factor.name == "量价热度（衍生）")
    assert flow_factor.score_available is True
    assert flow_factor.data_nature == "derived"


@pytest.mark.parametrize(("change", "text"), [(3, "量价配合偏积极"), (-3, "放量下跌")])
def test_high_relative_volume_wording_uses_ratio_even_above_three(change: float, text: str) -> None:
    report = build_fund_flow_analysis(_analysis(change=change, ratio=3.1))
    assert text in report.price_volume_relation


@pytest.mark.parametrize("count", [0, 1, 4])
def test_five_completed_baseline_days_are_required(count: int) -> None:
    _assert_unavailable(_analysis(rows=_rows(count=count)))


@pytest.mark.parametrize("value", [0, -1, float("nan"), float("inf"), None])
def test_missing_last_five_volume_cannot_be_backfilled_from_older_history(value: float | None) -> None:
    rows = _rows(count=6)
    rows[-3] = rows[-3].model_copy(update={"volume": value})
    analysis = _analysis().model_copy(update={"klines": rows})
    _assert_unavailable(analysis)


def test_zero_volume_survives_build_analysis_and_remains_missing_evidence() -> None:
    rows = _rows(count=6)
    rows[-3] = rows[-3].model_copy(update={"volume": 0})
    analysis = _analysis(rows=rows)
    assert len(analysis.klines) == 6
    _assert_unavailable(analysis)


@pytest.mark.parametrize("bad_date", [None, "", "2026-02-30", "20260910", "2026-09-10 15:00:00"])
def test_missing_or_invalid_daily_date_is_not_an_independent_baseline(bad_date: str | None) -> None:
    rows = _rows()
    rows[-2] = rows[-2].model_copy(update={"date": bad_date})
    _assert_unavailable(_analysis().model_copy(update={"klines": rows}))


def test_duplicate_baseline_dates_are_rejected_even_with_sufficient_rows() -> None:
    rows = _rows(count=6)
    rows[-2] = rows[-2].model_copy(update={"date": rows[-3].date})
    _assert_unavailable(_analysis(rows=rows))


def test_unsorted_baseline_rows_are_not_silently_reordered() -> None:
    rows = _rows()
    rows[-2], rows[-3] = rows[-3], rows[-2]
    _assert_unavailable(_analysis(rows=rows))


@pytest.mark.parametrize("timestamp", ["", "2026-09-14", "20260914", "15:00:00", "2026-09-14 14:59:59"])
def test_missing_full_or_completed_quote_time_is_unavailable(timestamp: str) -> None:
    _assert_unavailable(_analysis(timestamp=timestamp))


@pytest.mark.parametrize("timestamp", [None, "2026-02-30 15:00:00"])
def test_invalid_quote_timestamp_is_unavailable_without_raising(timestamp: str | None) -> None:
    analysis = _analysis()
    quote = analysis.quote.model_copy(update={"timestamp": timestamp})
    _assert_unavailable(analysis.model_copy(update={"quote": quote}))


@pytest.mark.parametrize("timestamp", ["2026-09-14T07:00:00Z", "2026-09-14T15:00:00+08:00"])
def test_quote_completion_time_is_normalized_to_the_exchange_timezone(timestamp: str) -> None:
    report = build_fund_flow_analysis(_analysis(timestamp=timestamp))
    assert report.available is True
    assert report.overall_score == 69


def test_same_day_valid_daily_volume_can_supply_missing_quote_volume() -> None:
    rows = [*_rows(), make_kline(date="2026-09-14", close=103, volume=1800)]
    report = build_fund_flow_analysis(_analysis(rows=rows, ratio=0))
    assert report.available is True
    assert report.overall_score == 69


def test_same_day_bar_is_excluded_from_completed_volume_baseline() -> None:
    rows = [*_rows(), make_kline(date="2026-09-14", close=103, volume=100_000)]
    report = build_fund_flow_analysis(_analysis(rows=rows))
    assert report.overall_score == 69


def test_future_bar_never_repairs_old_quote_volume() -> None:
    rows = [*_rows(), make_kline(date="2026-09-15", close=103, volume=1800)]
    _assert_unavailable(_analysis(rows=rows, ratio=0))


def test_future_history_is_rejected_even_with_valid_quote_volume() -> None:
    rows = [*_rows(), make_kline(date="2026-09-15", close=103, volume=1800)]
    _assert_unavailable(_analysis(rows=rows))


def test_duplicate_same_day_bars_are_rejected() -> None:
    same_day = make_kline(date="2026-09-14", close=103, volume=1800)
    _assert_unavailable(_analysis(rows=[*_rows(), same_day, same_day], ratio=0))


@pytest.mark.parametrize("field", ["price", "prev_close", "change_pct"])
def test_missing_price_direction_inputs_are_unavailable(field: str) -> None:
    analysis = _analysis()
    quote = analysis.quote.model_copy(update={field: float("nan")})
    _assert_unavailable(analysis.model_copy(update={"quote": quote}))


@pytest.mark.parametrize("actual,reported", [(-10, 10), (10, -10)])
def test_reversed_provider_return_is_unavailable_in_the_real_bundle(actual, reported):
    analysis = _analysis(change=actual, quote_updates={"change_pct": reported})
    _assert_unavailable(analysis)
    bundle = build_stock_insight_bundle(analysis)
    factor = next(item for item in bundle.overview.factors if item.name == "量价热度（衍生）")
    assert factor.score_available is False
    assert factor.participates_in_total_score is False
    assert factor.data_nature == "unavailable"


@pytest.mark.parametrize("reported", [2.7, 2.7000000001, 3.0, 3.2999999999, 3.3])
def test_reported_return_within_existing_rounding_tolerance_remains_available(reported):
    result = build_fund_flow_analysis(_analysis(change=3, quote_updates={"change_pct": reported}))
    assert result.available is True
    assert result.data_nature == "derived" and result.overall_score == 69


@pytest.mark.parametrize("reported", [2.699999, 3.300001, -1e308])
def test_reported_return_outside_tolerance_cannot_supply_direction(reported):
    _assert_unavailable(_analysis(change=3, quote_updates={"change_pct": reported}))


@pytest.mark.parametrize("actual,reported", [(0, 0.2), (0, -0.2), (-0.1, 0.1), (0.1, -0.1)])
def test_accepted_rounding_uses_price_direction_not_the_reported_sign(actual, reported):
    clean = build_fund_flow_analysis(_analysis(change=actual))
    rounded = build_fund_flow_analysis(_analysis(change=actual, quote_updates={"change_pct": reported}))
    assert rounded.available is True
    assert rounded.overall_score == clean.overall_score
    assert rounded.price_volume_relation == clean.price_volume_relation


@pytest.mark.parametrize("updates", [
    {"price": 1e308, "prev_close": 1e-308, "change_pct": 1e308},
    {"price": 1e308, "prev_close": 1, "change_pct": 1e308},
    {"price": 1e308, "prev_close": 100, "change_pct": -1e308},
    {"price": float("inf")}, {"prev_close": float("inf")}, {"change_pct": float("inf")},
    {"change_pct": float("-inf")},
])
def test_nonfinite_or_overflowing_return_evidence_is_rejected_without_raising(updates):
    analysis = _analysis()
    _assert_unavailable(analysis.model_copy(update={"quote": analysis.quote.model_copy(update=updates)}))


@pytest.mark.parametrize("mode", ["preopen", "intraday"])
def test_partial_session_modes_never_use_full_daily_volume_baselines(mode: str) -> None:
    _assert_unavailable(_analysis().model_copy(update={"research_mode": mode}))


def test_flat_history_continuity_is_neutral_and_ten_days_requires_ten_dates() -> None:
    report = build_fund_flow_analysis(_analysis())
    assert report.windows[1].score == 50
    assert report.windows[2].score == 50
    assert "数据不足" in report.windows[2].summary


def test_full_window_continuity_uses_strict_up_and_down_symmetrically() -> None:
    reports = []
    for closes in [list(range(100, 110)), list(range(110, 100, -1)), [100] * 10]:
        rows = [row.model_copy(update={"open": close, "close": close, "high": close + 1, "low": close - 1}) for row, close in zip(_rows(count=10), closes, strict=True)]
        reports.append(build_fund_flow_analysis(_analysis(rows=rows)))
    for index in (1, 2):
        up, down, flat = [report.windows[index].score for report in reports]
        assert up > 50 > down
        assert up + down == 100
        assert flat == 50


@pytest.mark.parametrize("change", [-20, 20])
def test_extreme_price_changes_remain_bounded_with_excess_volume(change: float) -> None:
    report = build_fund_flow_analysis(_analysis(change=change, ratio=100))
    assert report.overall_score == (0 if change < 0 else 100)


def test_incomplete_ten_day_prices_do_not_become_direction_from_nine_valid_prices() -> None:
    analysis = _analysis(rows=_rows(count=10))
    rows = analysis.klines.copy()
    rows[0] = rows[0].model_copy(update={"close": float("nan")})
    report = build_fund_flow_analysis(analysis.model_copy(update={"klines": rows}))
    assert report.available is True
    assert report.windows[2].score == 50
    assert "数据不足" in report.windows[2].summary
