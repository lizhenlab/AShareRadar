from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
import math

from app.models.schemas import CalibrationBucket, FactorCalibration
from app.services.indicators import pct_change
from app.services.research_factor_specs import FactorSpec, _trend_proxy_score_at
from app.services.scoring import clamp_score as _clamp
from app.utils.market_data import valid_kline

MIN_CALIBRATION_ROWS = 35
MIN_BUCKET_ROWS = 45
CALIBRATION_SCAN_START = 25
FORWARD_5D_OFFSET = 5
FORWARD_10D_OFFSET = 10
LOCAL_LEVEL_WINDOW = 20
STRONG_TREND_THRESHOLD = 65
WEAK_TREND_THRESHOLD = 45
NEAR_SUPPORT_MULTIPLIER = 1.035
NEAR_RESISTANCE_MULTIPLIER = 0.985


@dataclass(frozen=True)
class CalibrationSample:
    forward_5d: float
    forward_10d: float
    adverse_return: float


@dataclass(frozen=True)
class CalibrationStats:
    sample_count: int
    win_rate: float
    avg_5d: float
    avg_10d: float
    max_adverse: float


@dataclass(frozen=True)
class CalibrationBucketStats:
    sample_count: int
    win_rate: float
    avg_5d: float
    avg_10d: float


@dataclass(frozen=True)
class CalibrationBucketContext:
    trend: float
    entry: float
    support: float
    resistance: float


@dataclass(frozen=True)
class CalibrationBucketRule:
    name: str
    matches: Callable[[CalibrationBucketContext], bool]


@dataclass(frozen=True)
class CalibrationBucketNoteRule:
    note: str
    matches: Callable[[CalibrationBucketStats], bool]


@dataclass(frozen=True)
class CalibrationLevelRule:
    level: str
    matches: Callable[[float, float, float], bool]


@dataclass(frozen=True)
class CalibrationConfidenceRule:
    level: str
    matches: Callable[[int, float, float], bool]


CALIBRATION_BUCKET_RULES = (
    CalibrationBucketRule("强趋势", lambda context: context.trend >= STRONG_TREND_THRESHOLD),
    CalibrationBucketRule("弱趋势", lambda context: context.trend <= WEAK_TREND_THRESHOLD),
    CalibrationBucketRule("支撑附近", lambda context: bool(context.support) and context.entry <= context.support * NEAR_SUPPORT_MULTIPLIER),
    CalibrationBucketRule("压力附近", lambda context: bool(context.resistance) and context.entry >= context.resistance * NEAR_RESISTANCE_MULTIPLIER),
)

CALIBRATION_BUCKET_NOTE_RULES = (
    CalibrationBucketNoteRule("样本偏少，只作参考。", lambda stats: stats.sample_count < 5),
    CalibrationBucketNoteRule("该场景历史表现偏正。", lambda stats: stats.win_rate >= 58 and stats.avg_5d > 0),
    CalibrationBucketNoteRule("该场景历史表现偏弱。", lambda stats: stats.win_rate < 45 or stats.avg_5d < 0),
    CalibrationBucketNoteRule("该场景历史表现中性。", lambda _: True),
)

CALIBRATION_LEVEL_RULES = (
    CalibrationLevelRule("较强", lambda win_rate, avg_5d, avg_10d: win_rate >= 58 and avg_5d > 1 and avg_10d >= 0),
    CalibrationLevelRule("偏正", lambda win_rate, avg_5d, avg_10d: win_rate >= 52 and avg_5d >= 0),
    CalibrationLevelRule("风险", lambda win_rate, avg_5d, avg_10d: win_rate < 42 and avg_5d < -0.8),
    CalibrationLevelRule("偏弱", lambda win_rate, avg_5d, avg_10d: avg_5d < -0.3 or avg_10d < -0.6),
)

CALIBRATION_CONFIDENCE_RULES = (
    CalibrationConfidenceRule("较高", lambda sample_count, win_rate, avg_return: sample_count >= 12 and win_rate >= 58 and avg_return > 0.8),
    CalibrationConfidenceRule("中等", lambda sample_count, win_rate, avg_return: sample_count >= 8 and win_rate >= 52 and avg_return >= 0),
    CalibrationConfidenceRule("偏低", lambda sample_count, win_rate, avg_return: sample_count < 5),
    CalibrationConfidenceRule("偏弱", lambda sample_count, win_rate, avg_return: win_rate < 45 or avg_return < -0.5),
)


def _calibrate_factor(rows: list, spec: FactorSpec, current_score: int) -> FactorCalibration:
    if len(rows) < MIN_CALIBRATION_ROWS:
        return _insufficient_calibration()
    samples = _matching_calibration_samples(rows, spec, current_score)
    if not samples:
        return _no_similar_sample_calibration(spec.name)
    stats = _calibration_stats(samples)
    return FactorCalibration(
        sample_count=stats.sample_count,
        win_rate=round(stats.win_rate, 1),
        avg_forward_5d_return=round(stats.avg_5d, 2),
        avg_forward_10d_return=round(stats.avg_10d, 2),
        max_adverse_return=round(stats.max_adverse, 2),
        stability_score=_calibration_stability_score(stats),
        expected_level=_calibration_expected_level(spec.direction, stats.win_rate, stats.avg_5d, stats.avg_10d),
        confidence_level=_calibration_confidence_level(stats.sample_count, stats.win_rate, stats.avg_5d),
        note=_calibration_note(spec.name, stats.sample_count, stats.win_rate, stats.avg_5d),
    )


def _calibration_buckets(rows: list, spec: FactorSpec, current_score: int) -> list[CalibrationBucket]:
    if len(rows) < MIN_BUCKET_ROWS:
        return []
    buckets = _empty_calibration_buckets()
    for index in _calibration_indexes(rows):
        sample = _calibration_sample_at(rows, spec, current_score, index)
        if not sample:
            continue
        _append_bucket_samples(buckets, _bucket_context(rows, index), sample)
    return [_bucket_summary(name, values) for name, values in buckets.items() if values][:4]


def _insufficient_calibration() -> FactorCalibration:
    return FactorCalibration(
        sample_count=0,
        win_rate=0,
        avg_forward_5d_return=0,
        avg_forward_10d_return=0,
        max_adverse_return=0,
        confidence_level="样本不足",
        note="少于35根日K，暂不能形成稳定历史校准。",
    )


def _no_similar_sample_calibration(name: str) -> FactorCalibration:
    return FactorCalibration(
        sample_count=0,
        win_rate=0,
        avg_forward_5d_return=0,
        avg_forward_10d_return=0,
        max_adverse_return=0,
        stability_score=0,
        expected_level="待确认",
        confidence_level="无相似样本",
        note=f"历史中没有找到足够接近当前「{name}」状态的样本。",
    )


def _matching_calibration_samples(rows: list, spec: FactorSpec, current_score: int) -> list[CalibrationSample]:
    return [
        sample
        for index in _calibration_indexes(rows)
        if (sample := _calibration_sample_at(rows, spec, current_score, index))
    ]


def _calibration_indexes(rows: list) -> range:
    return range(CALIBRATION_SCAN_START, len(rows) - FORWARD_10D_OFFSET)


def _calibration_sample_at(rows: list, spec: FactorSpec, current_score: int, index: int) -> CalibrationSample | None:
    entry_row = _valid_row_at(rows, index)
    forward_5d_row = _valid_row_at(rows, index + FORWARD_5D_OFFSET)
    forward_10d_row = _valid_row_at(rows, index + FORWARD_10D_OFFSET)
    if entry_row is None or forward_5d_row is None or forward_10d_row is None:
        return None
    if not _trigger_matches(spec, rows, index, current_score):
        return None
    entry = entry_row.close
    return CalibrationSample(
        forward_5d=pct_change(forward_5d_row.close, entry),
        forward_10d=pct_change(forward_10d_row.close, entry),
        adverse_return=_adverse_return(rows, index, entry),
    )


def _valid_row_at(rows: list, index: int):
    if index < 0 or index >= len(rows):
        return None
    row = rows[index]
    return row if valid_kline(row) else None


def _trigger_matches(spec: FactorSpec, rows: list, index: int, current_score: int) -> bool:
    try:
        return spec.trigger(rows, index, current_score)
    except (ValueError, ZeroDivisionError):
        return False


def _adverse_return(rows: list, index: int, entry: float) -> float:
    lows = [item.low for item in rows[index + 1 : index + FORWARD_5D_OFFSET + 1] if valid_kline(item)]
    return min(pct_change(low, entry) for low in lows) if lows else 0


def _calibration_stats(samples: list[CalibrationSample]) -> CalibrationStats:
    sample_count = len(samples)
    forward_5d = [item.forward_5d for item in samples]
    forward_10d = [item.forward_10d for item in samples]
    adverse_returns = [item.adverse_return for item in samples]
    return CalibrationStats(
        sample_count=sample_count,
        win_rate=sum(1 for item in forward_5d if item > 0) / sample_count * 100,
        avg_5d=sum(forward_5d) / sample_count,
        avg_10d=sum(forward_10d) / sample_count,
        max_adverse=min(adverse_returns) if adverse_returns else 0,
    )


def _calibration_stability_score(stats: CalibrationStats) -> int:
    return _clamp(round(50 + stats.win_rate * 0.28 + stats.avg_5d * 3 + stats.avg_10d * 1.5 + stats.max_adverse * 1.1))


def _empty_calibration_buckets() -> dict[str, list[tuple[float, float]]]:
    return {rule.name: [] for rule in CALIBRATION_BUCKET_RULES}


def _append_bucket_samples(
    buckets: dict[str, list[tuple[float, float]]],
    context: CalibrationBucketContext,
    sample: CalibrationSample,
) -> None:
    for rule in CALIBRATION_BUCKET_RULES:
        if rule.matches(context):
            buckets[rule.name].append((sample.forward_5d, sample.forward_10d))


def _bucket_context(rows: list, index: int) -> CalibrationBucketContext:
    support, resistance = _local_support_resistance(rows, index)
    return CalibrationBucketContext(
        trend=_trend_proxy_score_at(rows, index),
        entry=rows[index].close,
        support=support,
        resistance=resistance,
    )


def _bucket_summary(name: str, values: list[tuple[float, float]]) -> CalibrationBucket:
    stats = _bucket_stats(values)
    return CalibrationBucket(
        name=name,
        sample_count=stats.sample_count,
        win_rate=round(stats.win_rate, 1),
        avg_forward_5d_return=round(stats.avg_5d, 2),
        avg_forward_10d_return=round(stats.avg_10d, 2),
        note=_bucket_note(stats),
    )


def _bucket_stats(values: list[tuple[float, float]]) -> CalibrationBucketStats:
    forward_5d = [item[0] for item in values]
    forward_10d = [item[1] for item in values]
    sample_count = len(values)
    return CalibrationBucketStats(
        sample_count=sample_count,
        win_rate=sum(1 for item in forward_5d if item > 0) / sample_count * 100,
        avg_5d=sum(forward_5d) / sample_count,
        avg_10d=sum(forward_10d) / sample_count,
    )


def _bucket_note(stats: CalibrationBucketStats) -> str:
    return next(rule.note for rule in CALIBRATION_BUCKET_NOTE_RULES if rule.matches(stats))


def _local_support_resistance(rows: list, index: int) -> tuple[float, float]:
    window = [item for item in rows[max(0, index - LOCAL_LEVEL_WINDOW + 1) : index + 1] if valid_kline(item)]
    if len(window) < 5:
        return 0, 0
    lows = sorted(item.low for item in window if item.low > 0)
    highs = sorted(item.high for item in window if item.high > 0)
    if not lows or not highs:
        return 0, 0
    support = lows[max(0, round((len(lows) - 1) * 0.18))]
    resistance = highs[min(len(highs) - 1, round((len(highs) - 1) * 0.82))]
    return support, resistance


def _factor_percentile(rows: list, evaluator: Callable[[list, int], float], current_score: int) -> float | None:
    if len(rows) < 30:
        return None
    values: list[float] = []
    for index in range(20, len(rows) - 1):
        if not valid_kline(rows[index]):
            continue
        try:
            value = evaluator(rows, index)
        except (ValueError, ZeroDivisionError):
            continue
        if math.isfinite(value):
            values.append(value)
    if not values:
        return None
    below = sum(1 for item in values if item <= current_score)
    return round(below / len(values) * 100, 1)


def _calibration_confidence_level(sample_count: int, win_rate: float, avg_return: float) -> str:
    return next(
        (rule.level for rule in CALIBRATION_CONFIDENCE_RULES if rule.matches(sample_count, win_rate, avg_return)),
        "观察",
    )


def _calibration_expected_level(direction: str, win_rate: float, avg_5d: float, avg_10d: float) -> str:
    effective_5d, effective_10d = _directional_returns(direction, avg_5d, avg_10d)
    return next(
        (rule.level for rule in CALIBRATION_LEVEL_RULES if rule.matches(win_rate, effective_5d, effective_10d)),
        "观察",
    )


def _directional_returns(direction: str, avg_5d: float, avg_10d: float) -> tuple[float, float]:
    if direction == "反向":
        return -avg_5d, -avg_10d
    return avg_5d, avg_10d


def _calibration_note(name: str, sample_count: int, win_rate: float, avg_return: float) -> str:
    if sample_count < 5:
        return f"「{name}」相似样本只有 {sample_count} 次，暂不宜提高权重。"
    if win_rate >= 58 and avg_return > 0:
        return f"「{name}」在该股历史中表现偏正，可作为辅助确认。"
    if win_rate < 45 or avg_return < 0:
        return f"「{name}」历史表现不稳，当前触发时要降低信号权重。"
    return f"「{name}」历史表现中性，适合与价位、量能一起确认。"
