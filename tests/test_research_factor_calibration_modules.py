from __future__ import annotations

import math

from app.services.research_factor_calibration import (
    CalibrationBucketStats,
    _bucket_note,
    _bucket_stats,
    _bucket_summary,
    _calibrate_factor,
    _calibration_buckets,
    _calibration_confidence_level,
    _calibration_expected_level,
    _factor_percentile,
)
from app.services.research_factor_specs import FactorSpec
from tests.factories import make_kline


def test_calibrate_factor_returns_insufficient_sample_before_minimum_rows() -> None:
    calibration = _calibrate_factor(_rows([100 + index for index in range(35)]), _spec(), 60)

    assert calibration.sample_count == 0
    assert calibration.confidence_level == "样本不足"
    assert calibration.note == "少于36根日K，暂不能形成历史校准样本。"


def test_calibrate_factor_scans_first_valid_sample_at_36_rows() -> None:
    calibration = _calibrate_factor(_rows([100 + index for index in range(36)]), _spec(), 60)

    assert calibration.sample_count == 1
    assert calibration.confidence_level == "偏低"


def test_calibrate_factor_returns_no_similar_sample_when_trigger_never_matches() -> None:
    calibration = _calibrate_factor(_rows([100 + index for index in range(50)]), _spec(trigger=lambda *_: False), 60)

    assert calibration.sample_count == 0
    assert calibration.expected_level == "待确认"
    assert calibration.confidence_level == "无相似样本"
    assert "测试因子" in calibration.note


def test_calibrate_factor_collects_matching_samples_and_statistics() -> None:
    calibration = _calibrate_factor(_rows([100 + index for index in range(50)]), _spec(), 60)

    assert calibration.sample_count == 15
    assert calibration.win_rate == 100
    assert calibration.avg_forward_5d_return > 3
    assert calibration.avg_forward_10d_return > 7
    assert calibration.expected_level == "较强"
    assert calibration.confidence_level == "较高"


def test_calibrate_factor_skips_invalid_entry_or_forward_rows() -> None:
    rows = _rows([100 + index for index in range(50)])
    rows[30] = rows[30].model_copy(update={"high": math.inf})

    calibration = _calibrate_factor(rows, _spec(), 60)

    assert calibration.sample_count == 13
    assert math.isfinite(calibration.avg_forward_5d_return)
    assert math.isfinite(calibration.avg_forward_10d_return)


def test_calibration_expected_level_normalizes_reverse_direction() -> None:
    assert _calibration_expected_level("反向", 60, -2.0, -1.0) == "较强"
    assert _calibration_expected_level("正向", 40, -1.2, -1.0) == "风险"
    assert _calibration_expected_level("正向", 50, -0.4, 0.2) == "偏弱"
    assert _calibration_expected_level("正向", 50, 0.1, 0.0) == "观察"


def test_calibration_confidence_level_priority_is_stable() -> None:
    assert _calibration_confidence_level(12, 60, 1.0) == "较高"
    assert _calibration_confidence_level(8, 53, 0.0) == "中等"
    assert _calibration_confidence_level(4, 80, 3.0) == "偏低"
    assert _calibration_confidence_level(8, 44, 1.0) == "偏弱"
    assert _calibration_confidence_level(8, 48, 0.1) == "观察"


def test_bucket_summary_notes_follow_sample_and_return_boundaries() -> None:
    assert _bucket_summary("强趋势", [(1.0, 1.5)]).note == "样本偏少，只作参考。"
    assert _bucket_summary("强趋势", [(1.2, 1.0)] * 5).note == "该场景历史表现偏正。"
    assert _bucket_summary("弱趋势", [(-0.5, -0.4)] * 5).note == "该场景历史表现偏弱。"
    neutral_values = [(0.2, 0.1), (0.1, 0), (0.05, 0), (-0.05, 0), (-0.05, 0), (-0.05, 0)]
    assert _bucket_summary("支撑附近", neutral_values).note == "该场景历史表现中性。"


def test_bucket_stats_and_note_rules_are_independent() -> None:
    stats = _bucket_stats([(1.0, 2.0), (-0.5, 1.0), (0.5, -1.0), (-0.2, 0.0), (0.4, 0.5)])

    assert stats.sample_count == 5
    assert stats.win_rate == 60
    assert round(stats.avg_5d, 2) == 0.24
    assert round(stats.avg_10d, 2) == 0.5
    assert _bucket_note(CalibrationBucketStats(sample_count=4, win_rate=100, avg_5d=2.0, avg_10d=2.0)) == "样本偏少，只作参考。"
    assert _bucket_note(CalibrationBucketStats(sample_count=5, win_rate=60, avg_5d=0.1, avg_10d=0.0)) == "该场景历史表现偏正。"
    assert _bucket_note(CalibrationBucketStats(sample_count=5, win_rate=40, avg_5d=0.1, avg_10d=0.0)) == "该场景历史表现偏弱。"
    assert _bucket_note(CalibrationBucketStats(sample_count=5, win_rate=50, avg_5d=0.1, avg_10d=0.0)) == "该场景历史表现中性。"


def test_calibration_buckets_builds_named_scene_summaries() -> None:
    buckets = _calibration_buckets(_rows([100 + index * 0.8 for index in range(55)]), _spec(), 60)

    assert buckets
    assert buckets[0].name == "强趋势"
    assert buckets[0].sample_count > 0
    assert all(bucket.name in {"强趋势", "弱趋势", "支撑附近", "压力附近"} for bucket in buckets)


def test_factor_percentile_skips_non_numeric_and_failed_evaluator_values() -> None:
    def evaluator(_rows, index: int):
        if index == 20:
            return None
        if index == 21:
            return float("inf")
        if index == 22:
            raise ValueError("missing factor input")
        return index

    assert _factor_percentile(_rows([100 + index for index in range(40)]), evaluator, 30) == 50.0


def _spec(*, trigger=None) -> FactorSpec:
    return FactorSpec(
        id="test_factor",
        name="测试因子",
        category="测试",
        weight=1.0,
        direction="正向",
        evaluator=lambda rows, index: 60,
        trigger=trigger or (lambda rows, index, current_score: True),
    )


def _rows(closes: list[float]):
    return [
        make_kline(
            date=f"2026-05-{(index % 28) + 1:02d}",
            close=close,
            high=close + 1,
            low=close - 1,
            volume=1000 + index * 10,
        )
        for index, close in enumerate(closes)
    ]
