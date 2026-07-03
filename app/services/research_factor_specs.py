from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from types import MappingProxyType

from app.models.schemas import Kline
from app.services.indicator_volume import positive_volume_ratio
from app.services.indicators import pct_change
from app.services.scoring import clamp_score as _clamp
from app.utils.market_data import finite_float, valid_kline


NEUTRAL_SCORE = 50

SCORE_CONTEXT_MIN_INDEX = 20
MA_SHORT_WINDOW = 5
MA_MEDIUM_WINDOW = 10
MA_LONG_WINDOW = 20
PREVIOUS_MA_OFFSET = MA_SHORT_WINDOW
PRICE_RANGE_WINDOW = MA_LONG_WINDOW
VOLUME_RECENT_WINDOW = 5
VOLUME_BASE_WINDOW = 20

TREND_BASE_SCORE = NEUTRAL_SCORE
TREND_NEAR_HIGH_RATIO = 0.985
TREND_NEAR_LOW_RATIO = 1.03
TREND_STRONG_CURRENT_SCORE = 58
TREND_WEAK_CURRENT_SCORE = 48
TREND_TRIGGER_TOLERANCE = 8
TREND_TRIGGER_NEUTRAL_LOW = 45
TREND_TRIGGER_NEUTRAL_HIGH = 62

VOLUME_BASE_SCORE = 52
VOLUME_CONFIRMATION_RATIO = 1.2
VOLUME_EXPANSION_SCORE = 16
VOLUME_DISTRIBUTION_SCORE = 18
VOLUME_EXPANSION_CAP = 12
VOLUME_EXPANSION_SCALE = 10
VOLUME_SHRINK_RATIO = 0.7
VOLUME_LARGE_MOVE_PCT = 2
VOLUME_NORMAL_LOW_RATIO = 0.85
VOLUME_NORMAL_HIGH_RATIO = 1.25
VOLUME_STRONG_CURRENT_SCORE = 58
VOLUME_WEAK_CURRENT_SCORE = 45
VOLUME_TRIGGER_TOLERANCE = 10
VOLUME_TRIGGER_NEUTRAL_HIGH = 60

RISK_BASE_SCORE = 72
RISK_FALLBACK_SCORE = 58
RISK_LARGE_DROP_PCT = -3
RISK_VOLUME_DROP_RATIO = 1.5
RISK_WIDE_AMPLITUDE_PCT = 6
RISK_HEALTHY_CHANGE_PCT = 1
RISK_WEAK_CURRENT_SCORE = 50
RISK_WEAK_SCORE_CEILING = 48
RISK_STRONG_CURRENT_SCORE = 60
RISK_STRONG_SCORE_FLOOR = 65
RISK_TRIGGER_TOLERANCE = 8
RISK_TRIGGER_NEUTRAL_LOW = 48
RISK_TRIGGER_NEUTRAL_HIGH = 65

FLOW_MIN_INDEX = 10
FLOW_LOOKBACK_WINDOW = 5
FLOW_MIN_VALID_ROWS = 3
FLOW_PRESSURE_WEIGHT = 32
FLOW_CONTINUITY_ANCHOR = 2.5
FLOW_CONTINUITY_WEIGHT = 4
FLOW_STRONG_CURRENT_SCORE = 58
FLOW_WEAK_CURRENT_SCORE = 45
FLOW_TRIGGER_TOLERANCE = 12
FLOW_TRIGGER_NEUTRAL_HIGH = 60

CHIP_BASE_SCORE = 58
CHIP_MIN_INDEX = 30
CHIP_LOOKBACK_WINDOW = 60
CHIP_DISTANCE_NEAR_LOW = -3
CHIP_DISTANCE_NEAR_HIGH = 8
CHIP_DISTANCE_MODERATE_HIGH = 16
CHIP_DISTANCE_DEEP_LOW = -8
CHIP_TRIGGER_TOLERANCE = 12

LEADERSHIP_BASE_SCORE = 40
LEADERSHIP_FALLBACK_SCORE = 45
LEADERSHIP_TREND_WEIGHT = 0.45
LEADERSHIP_STRONG_CHANGE_PCT = 5
LEADERSHIP_POSITIVE_CHANGE_PCT = 2
LEADERSHIP_NEGATIVE_CHANGE_PCT = -3
LEADERSHIP_VOLUME_RATIO = 1.4
LEADERSHIP_STRONG_CURRENT_SCORE = 58
LEADERSHIP_WEAK_CURRENT_SCORE = 48
LEADERSHIP_TRIGGER_TOLERANCE = 10
LEADERSHIP_TRIGGER_NEUTRAL_LOW = 45
LEADERSHIP_TRIGGER_NEUTRAL_HIGH = 60


@dataclass(frozen=True)
class FactorSpec:
    id: str
    name: str
    category: str
    weight: float
    direction: str
    evaluator: Callable[[list[Kline], int], float]
    trigger: Callable[[list[Kline], int, float], bool]


@dataclass(frozen=True)
class FactorScoreContext:
    rows: list[Kline]
    index: int
    current: Kline
    previous: Kline
    change_pct: float
    volume_ratio: float
    ma5: float
    ma10: float
    ma20: float
    prev_ma5: float
    high_20: float
    low_20: float
    amplitude_pct: float


@dataclass(frozen=True)
class FactorScoreRows:
    current: Kline
    previous: Kline


@dataclass(frozen=True)
class FactorMovingAverages:
    ma5: float
    ma10: float
    ma20: float
    prev_ma5: float


@dataclass(frozen=True)
class FactorPriceRange:
    high_20: float
    low_20: float


@dataclass(frozen=True)
class FlowMetrics:
    up_amount: float
    down_amount: float
    continuity: int


@dataclass(frozen=True)
class BinaryScoreRule:
    name: str
    matches: Callable[[FactorScoreContext], bool]
    positive_delta: int
    negative_delta: int


@dataclass(frozen=True)
class ScoreAdjustmentRule:
    name: str
    matches: Callable[[FactorScoreContext], bool]
    adjustment: Callable[[FactorScoreContext], int]


@dataclass(frozen=True)
class DistanceScoreRule:
    name: str
    matches: Callable[[float], bool]
    adjustment: int


TREND_SCORE_RULES: tuple[BinaryScoreRule, ...] = (
    BinaryScoreRule(
        "close_above_ma5",
        lambda context: context.current.close > context.ma5,
        12,
        -8,
    ),
    BinaryScoreRule(
        "ma5_above_ma10",
        lambda context: context.ma5 > context.ma10,
        10,
        -6,
    ),
    BinaryScoreRule(
        "ma10_above_ma20",
        lambda context: context.ma10 > context.ma20,
        10,
        -8,
    ),
    BinaryScoreRule(
        "ma5_rising",
        lambda context: context.ma5 >= context.prev_ma5,
        7,
        -5,
    ),
)

TREND_POSITION_RULES: tuple[ScoreAdjustmentRule, ...] = (
    ScoreAdjustmentRule(
        "near_20d_high",
        lambda context: context.current.close >= context.high_20 * TREND_NEAR_HIGH_RATIO,
        lambda _context: 10,
    ),
    ScoreAdjustmentRule(
        "near_20d_low",
        lambda context: context.current.close <= context.low_20 * TREND_NEAR_LOW_RATIO,
        lambda _context: -10,
    ),
)

VOLUME_SCORE_RULES: tuple[ScoreAdjustmentRule, ...] = (
    ScoreAdjustmentRule(
        "positive_volume",
        lambda context: context.change_pct > 0 and context.volume_ratio >= VOLUME_CONFIRMATION_RATIO,
        lambda context: VOLUME_EXPANSION_SCORE
        + min(
            VOLUME_EXPANSION_CAP,
            round((context.volume_ratio - VOLUME_CONFIRMATION_RATIO) * VOLUME_EXPANSION_SCALE),
        ),
    ),
    ScoreAdjustmentRule(
        "negative_volume",
        lambda context: context.change_pct < 0 and context.volume_ratio >= VOLUME_CONFIRMATION_RATIO,
        lambda context: -(
            VOLUME_DISTRIBUTION_SCORE
            + min(
                VOLUME_EXPANSION_CAP,
                round((context.volume_ratio - VOLUME_CONFIRMATION_RATIO) * VOLUME_EXPANSION_SCALE),
            )
        ),
    ),
    ScoreAdjustmentRule(
        "shrinking_large_move",
        lambda context: context.volume_ratio < VOLUME_SHRINK_RATIO and abs(context.change_pct) >= VOLUME_LARGE_MOVE_PCT,
        lambda _context: -8,
    ),
    ScoreAdjustmentRule(
        "normal_volume",
        lambda context: VOLUME_NORMAL_LOW_RATIO <= context.volume_ratio <= VOLUME_NORMAL_HIGH_RATIO,
        lambda _context: 4,
    ),
)

RISK_SCORE_RULES: tuple[ScoreAdjustmentRule, ...] = (
    ScoreAdjustmentRule(
        "below_ma20",
        lambda context: context.current.close < context.ma20,
        lambda _context: -16,
    ),
    ScoreAdjustmentRule(
        "large_drop",
        lambda context: context.change_pct <= RISK_LARGE_DROP_PCT,
        lambda _context: -14,
    ),
    ScoreAdjustmentRule(
        "volume_drop",
        lambda context: context.change_pct < 0 and context.volume_ratio >= RISK_VOLUME_DROP_RATIO,
        lambda _context: -12,
    ),
    ScoreAdjustmentRule(
        "wide_amplitude",
        lambda context: context.amplitude_pct >= RISK_WIDE_AMPLITUDE_PCT,
        lambda _context: -6,
    ),
    ScoreAdjustmentRule(
        "healthy_above_ma20",
        lambda context: context.current.close > context.ma20 and context.change_pct >= RISK_HEALTHY_CHANGE_PCT,
        lambda _context: 5,
    ),
)

CHIP_DISTANCE_RULES: tuple[DistanceScoreRule, ...] = (
    DistanceScoreRule(
        "near_cost_center",
        lambda distance: CHIP_DISTANCE_NEAR_LOW <= distance <= CHIP_DISTANCE_NEAR_HIGH,
        16,
    ),
    DistanceScoreRule(
        "moderately_above_center",
        lambda distance: CHIP_DISTANCE_NEAR_HIGH < distance <= CHIP_DISTANCE_MODERATE_HIGH,
        4,
    ),
    DistanceScoreRule("overheated_above_center", lambda distance: distance > CHIP_DISTANCE_MODERATE_HIGH, -14),
    DistanceScoreRule("deep_below_center", lambda distance: distance < CHIP_DISTANCE_DEEP_LOW, -12),
)


def _factor_specs() -> dict[str, FactorSpec]:
    return dict(_REGISTERED_FACTOR_SPEC_MAP)


def _factor_spec_map(specs: tuple[FactorSpec, ...]) -> dict[str, FactorSpec]:
    mapping: dict[str, FactorSpec] = {}
    for spec in specs:
        spec_id = _validated_factor_spec_id(spec)
        if spec_id in mapping:
            raise ValueError(f"duplicate factor spec id: {spec_id}")
        _validate_factor_spec(spec)
        mapping[spec_id] = spec
    return mapping


def _validated_factor_spec_id(spec: FactorSpec) -> str:
    spec_id = getattr(spec, "id", None)
    if not isinstance(spec_id, str) or not spec_id.strip():
        raise ValueError("factor spec id must be a non-empty string")
    if spec_id != spec_id.strip():
        raise ValueError("factor spec id must not contain leading or trailing whitespace")
    return spec_id


def _validate_factor_spec(spec: FactorSpec) -> None:
    for field in ("name", "category", "direction"):
        value = getattr(spec, field, None)
        if not isinstance(value, str) or not value.strip():
            raise ValueError(f"factor spec {spec.id} {field} must be a non-empty string")
    weight = finite_float(getattr(spec, "weight", None))
    if weight is None or weight <= 0:
        raise ValueError(f"factor spec {spec.id} weight must be a positive finite number")
    if not callable(getattr(spec, "evaluator", None)):
        raise ValueError(f"factor spec {spec.id} evaluator must be callable")
    if not callable(getattr(spec, "trigger", None)):
        raise ValueError(f"factor spec {spec.id} trigger must be callable")


def _trend_proxy_score_at(rows: list[Kline], index: int) -> float:
    context = _score_context(rows, index, min_index=SCORE_CONTEXT_MIN_INDEX)
    if context is None:
        return NEUTRAL_SCORE
    return _trend_proxy_score_from_context(context)


def _trend_proxy_score_from_context(context: FactorScoreContext) -> float:
    score = TREND_BASE_SCORE
    score += _binary_rule_delta(context, TREND_SCORE_RULES)
    score += _score_rule_delta(context, TREND_POSITION_RULES)
    return _clamp(score)


def _volume_proxy_score_at(rows: list[Kline], index: int) -> float:
    context = _score_context(rows, index, min_index=SCORE_CONTEXT_MIN_INDEX)
    if context is None or context.current.volume <= 0:
        return NEUTRAL_SCORE
    return _clamp(VOLUME_BASE_SCORE + _first_score_rule_delta(context, VOLUME_SCORE_RULES))


def _risk_proxy_score_at(rows: list[Kline], index: int) -> float:
    context = _score_context(rows, index, min_index=SCORE_CONTEXT_MIN_INDEX)
    if context is None:
        return RISK_FALLBACK_SCORE
    return _clamp(RISK_BASE_SCORE + _score_rule_delta(context, RISK_SCORE_RULES))


def _fund_flow_proxy_score_at(rows: list[Kline], index: int) -> float:
    recent = _recent_valid_flow_rows(rows, index)
    if len(recent) < FLOW_MIN_VALID_ROWS:
        return NEUTRAL_SCORE
    metrics = _flow_metrics(recent)
    total = metrics.up_amount + metrics.down_amount
    if total <= 0:
        return NEUTRAL_SCORE
    pressure = (metrics.up_amount - metrics.down_amount) / total
    return _clamp(
        round(
            NEUTRAL_SCORE
            + pressure * FLOW_PRESSURE_WEIGHT
            + (metrics.continuity - FLOW_CONTINUITY_ANCHOR) * FLOW_CONTINUITY_WEIGHT
        )
    )


def _chip_position_score_at(rows: list[Kline], index: int) -> float:
    if index < CHIP_MIN_INDEX or not _has_valid_index(rows, index):
        return NEUTRAL_SCORE
    current_close = _current_close_at(rows, index)
    center = _volume_weighted_typical_price(_window_rows(rows, index, CHIP_LOOKBACK_WINDOW))
    if center <= 0 or current_close <= 0:
        return NEUTRAL_SCORE
    distance = pct_change(current_close, center)
    return _clamp(CHIP_BASE_SCORE + _chip_distance_delta(distance))


def _leadership_proxy_score_at(rows: list[Kline], index: int) -> float:
    context = _score_context(rows, index, min_index=SCORE_CONTEXT_MIN_INDEX)
    if context is None:
        return LEADERSHIP_FALLBACK_SCORE
    trend = _trend_proxy_score_from_context(context)
    score = LEADERSHIP_BASE_SCORE + round((trend - NEUTRAL_SCORE) * LEADERSHIP_TREND_WEIGHT)
    score += _leadership_change_delta(context.change_pct)
    score += _leadership_volume_delta(context)
    return _clamp(score)


def _score_context(rows: list[Kline], index: int, *, min_index: int) -> FactorScoreContext | None:
    score_rows = _score_rows(rows, index, min_index=min_index)
    if score_rows is None:
        return None
    averages = _moving_averages(rows, index)
    if averages is None:
        return None
    price_range = _price_range(rows, index)
    if price_range is None:
        return None
    return _build_score_context(rows, index, score_rows, averages, price_range)


def _score_rows(rows: list[Kline], index: int, *, min_index: int) -> FactorScoreRows | None:
    if not _score_index_is_valid(rows, index, min_index=min_index):
        return None
    current = rows[index]
    previous = rows[index - 1]
    if not _valid_ohlc(current) or not _valid_ohlc(previous):
        return None
    return FactorScoreRows(current=current, previous=previous)


def _score_index_is_valid(rows: list[Kline], index: int, *, min_index: int) -> bool:
    return _has_valid_index(rows, index) and index >= max(1, min_index)


def _moving_averages(rows: list[Kline], index: int) -> FactorMovingAverages | None:
    ma5 = _window_average_close(rows, index, MA_SHORT_WINDOW, min_count=MA_SHORT_WINDOW)
    ma10 = _window_average_close(rows, index, MA_MEDIUM_WINDOW, min_count=MA_MEDIUM_WINDOW)
    ma20 = _window_average_close(rows, index, MA_LONG_WINDOW, min_count=MA_LONG_WINDOW)
    prev_ma5 = (
        _window_average_close(
            rows,
            index - PREVIOUS_MA_OFFSET,
            MA_SHORT_WINDOW,
            min_count=MA_SHORT_WINDOW,
        )
        if index >= MA_LONG_WINDOW + PREVIOUS_MA_OFFSET
        else ma5
    )
    if _any_non_positive(ma5, ma10, ma20, prev_ma5):
        return None
    return FactorMovingAverages(ma5=ma5, ma10=ma10, ma20=ma20, prev_ma5=prev_ma5)


def _price_range(rows: list[Kline], index: int) -> FactorPriceRange | None:
    high_20, low_20 = _window_high_low(rows, index, PRICE_RANGE_WINDOW, min_count=PRICE_RANGE_WINDOW)
    if _any_non_positive(high_20, low_20):
        return None
    return FactorPriceRange(high_20=high_20, low_20=low_20)


def _any_non_positive(*values: float) -> bool:
    return any((parsed := finite_float(value)) is None or parsed <= 0 for value in values)


def _build_score_context(
    rows: list[Kline],
    index: int,
    score_rows: FactorScoreRows,
    averages: FactorMovingAverages,
    price_range: FactorPriceRange,
) -> FactorScoreContext:
    return FactorScoreContext(
        rows=rows,
        index=index,
        current=score_rows.current,
        previous=score_rows.previous,
        change_pct=pct_change(score_rows.current.close, score_rows.previous.close),
        volume_ratio=_volume_ratio_at(rows, index),
        ma5=averages.ma5,
        ma10=averages.ma10,
        ma20=averages.ma20,
        prev_ma5=averages.prev_ma5,
        high_20=price_range.high_20,
        low_20=price_range.low_20,
        amplitude_pct=pct_change(score_rows.current.high, score_rows.current.low),
    )


def _valid_ohlc(row: Kline) -> bool:
    return valid_kline(row)


def _valid_positive_volume_row(row: Kline) -> bool:
    return _valid_ohlc(row) and row.volume > 0


def _has_valid_index(rows: list[Kline], index: int) -> bool:
    return 0 <= index < len(rows)


def _window_rows(rows: list[Kline], index: int, window: int) -> list[Kline]:
    if window <= 0 or not _has_valid_index(rows, index):
        return []
    start = max(0, index - window + 1)
    return rows[start : index + 1]


def _valid_window_rows(rows: list[Kline], index: int, window: int) -> list[Kline]:
    return [item for item in _window_rows(rows, index, window) if _valid_ohlc(item)]


def _current_close_at(rows: list[Kline], index: int) -> float:
    if not _has_valid_index(rows, index) or not _valid_ohlc(rows[index]):
        return 0
    parsed = finite_float(rows[index].close)
    return parsed if parsed is not None and parsed > 0 else 0


def _recent_valid_flow_rows(rows: list[Kline], index: int) -> list[Kline]:
    if index < FLOW_MIN_INDEX or not _has_valid_index(rows, index) or not _valid_positive_volume_row(rows[index]):
        return []
    return [item for item in _window_rows(rows, index, FLOW_LOOKBACK_WINDOW) if _valid_positive_volume_row(item)]


def _flow_amounts(rows: list[Kline]) -> tuple[float, float]:
    metrics = _flow_metrics(rows)
    return metrics.up_amount, metrics.down_amount


def _flow_metrics(rows: list[Kline]) -> FlowMetrics:
    up_amount = 0.0
    down_amount = 0.0
    continuity = 0
    for item in rows:
        if not _valid_positive_volume_row(item):
            continue
        amount = item.close * item.volume
        if item.close >= item.open:
            up_amount += amount
            continuity += 1
        else:
            down_amount += amount
    return FlowMetrics(up_amount=up_amount, down_amount=down_amount, continuity=continuity)


def _window_high_low(
    rows: list[Kline],
    index: int,
    window: int,
    *,
    min_count: int = 1,
) -> tuple[float, float]:
    valid_rows = _valid_window_rows(rows, index, window)
    if len(valid_rows) < min_count:
        return (0, 0)
    high: float | None = None
    low: float | None = None
    for item in valid_rows:
        high = item.high if high is None else max(high, item.high)
        low = item.low if low is None else min(low, item.low)
    return (high or 0, low or 0)


def _binary_rule_delta(context: FactorScoreContext, rules: tuple[BinaryScoreRule, ...]) -> int:
    return sum(rule.positive_delta if rule.matches(context) else rule.negative_delta for rule in rules)


def _score_rule_delta(context: FactorScoreContext, rules: tuple[ScoreAdjustmentRule, ...]) -> int:
    return sum(rule.adjustment(context) for rule in rules if rule.matches(context))


def _first_score_rule_delta(context: FactorScoreContext, rules: tuple[ScoreAdjustmentRule, ...]) -> int:
    return next((rule.adjustment(context) for rule in rules if rule.matches(context)), 0)


def _volume_weighted_typical_price(rows: list[Kline]) -> float:
    total_volume = 0.0
    weighted_price = 0.0
    for item in rows:
        if not _valid_positive_volume_row(item):
            continue
        total_volume += item.volume
        weighted_price += ((item.high + item.low + item.close) / 3) * item.volume
    if total_volume <= 0:
        return 0
    return weighted_price / total_volume


def _chip_distance_delta(distance: float) -> int:
    return next((rule.adjustment for rule in CHIP_DISTANCE_RULES if rule.matches(distance)), 0)


def _leadership_change_delta(change: float) -> int:
    if change >= LEADERSHIP_STRONG_CHANGE_PCT:
        return 12
    if change >= LEADERSHIP_POSITIVE_CHANGE_PCT:
        return 7
    if change <= LEADERSHIP_NEGATIVE_CHANGE_PCT:
        return -6
    return 0


def _leadership_volume_delta(context: FactorScoreContext) -> int:
    if context.volume_ratio < LEADERSHIP_VOLUME_RATIO:
        return 0
    return 8 if context.change_pct > 0 else -6 if context.change_pct < 0 else 0


def _trigger_score(current_score: float) -> float | None:
    parsed = finite_float(current_score)
    if parsed is None:
        return None
    return max(0.0, min(100.0, parsed))


def _trend_trigger(rows: list[Kline], index: int, current_score: float) -> bool:
    current_score = _trigger_score(current_score)
    if current_score is None:
        return False
    score = _trend_proxy_score_at(rows, index)
    if current_score >= TREND_STRONG_CURRENT_SCORE:
        return score >= max(TREND_STRONG_CURRENT_SCORE, current_score - TREND_TRIGGER_TOLERANCE)
    if current_score <= TREND_WEAK_CURRENT_SCORE:
        return score <= min(TREND_WEAK_CURRENT_SCORE, current_score + TREND_TRIGGER_TOLERANCE)
    return TREND_TRIGGER_NEUTRAL_LOW < score < TREND_TRIGGER_NEUTRAL_HIGH


def _volume_trigger(rows: list[Kline], index: int, current_score: float) -> bool:
    current_score = _trigger_score(current_score)
    if current_score is None:
        return False
    score = _volume_proxy_score_at(rows, index)
    if current_score >= VOLUME_STRONG_CURRENT_SCORE or current_score <= VOLUME_WEAK_CURRENT_SCORE:
        return abs(score - current_score) <= VOLUME_TRIGGER_TOLERANCE and (
            score >= VOLUME_STRONG_CURRENT_SCORE or score <= VOLUME_WEAK_CURRENT_SCORE
        )
    return VOLUME_WEAK_CURRENT_SCORE < score < VOLUME_TRIGGER_NEUTRAL_HIGH


def _risk_trigger(rows: list[Kline], index: int, current_score: float) -> bool:
    current_score = _trigger_score(current_score)
    if current_score is None:
        return False
    score = _risk_proxy_score_at(rows, index)
    if current_score <= RISK_WEAK_CURRENT_SCORE:
        return score <= max(RISK_WEAK_SCORE_CEILING, current_score + RISK_TRIGGER_TOLERANCE)
    if current_score >= RISK_STRONG_CURRENT_SCORE:
        return score >= min(RISK_STRONG_SCORE_FLOOR, current_score - RISK_TRIGGER_TOLERANCE)
    return RISK_TRIGGER_NEUTRAL_LOW < score < RISK_TRIGGER_NEUTRAL_HIGH


def _fund_flow_trigger(rows: list[Kline], index: int, current_score: float) -> bool:
    current_score = _trigger_score(current_score)
    if current_score is None:
        return False
    score = _fund_flow_proxy_score_at(rows, index)
    if current_score >= FLOW_STRONG_CURRENT_SCORE or current_score <= FLOW_WEAK_CURRENT_SCORE:
        return abs(score - current_score) <= FLOW_TRIGGER_TOLERANCE and (
            score >= FLOW_STRONG_CURRENT_SCORE or score <= FLOW_WEAK_CURRENT_SCORE
        )
    return FLOW_WEAK_CURRENT_SCORE < score < FLOW_TRIGGER_NEUTRAL_HIGH


def _chip_trigger(rows: list[Kline], index: int, current_score: float) -> bool:
    current_score = _trigger_score(current_score)
    if current_score is None:
        return False
    score = _chip_position_score_at(rows, index)
    return abs(score - current_score) <= CHIP_TRIGGER_TOLERANCE


def _leadership_trigger(rows: list[Kline], index: int, current_score: float) -> bool:
    current_score = _trigger_score(current_score)
    if current_score is None:
        return False
    score = _leadership_proxy_score_at(rows, index)
    if current_score >= LEADERSHIP_STRONG_CURRENT_SCORE:
        return score >= max(LEADERSHIP_STRONG_CURRENT_SCORE, current_score - LEADERSHIP_TRIGGER_TOLERANCE)
    if current_score <= LEADERSHIP_WEAK_CURRENT_SCORE:
        return score <= min(LEADERSHIP_WEAK_CURRENT_SCORE, current_score + LEADERSHIP_TRIGGER_TOLERANCE)
    return LEADERSHIP_TRIGGER_NEUTRAL_LOW < score < LEADERSHIP_TRIGGER_NEUTRAL_HIGH


def _window_average_close(rows: list[Kline], index: int, window: int, *, min_count: int = 1) -> float:
    values = [item.close for item in _valid_window_rows(rows, index, window)]
    if len(values) < min_count:
        return 0
    return sum(values) / len(values) if values else 0


def _volume_ratio_at(
    rows: list[Kline],
    index: int,
    recent_window: int = VOLUME_RECENT_WINDOW,
    base_window: int = VOLUME_BASE_WINDOW,
) -> float:
    if not _volume_ratio_index_is_valid(rows, index, recent_window, base_window):
        return 1.0
    volumes = [item.volume for item in rows[: index + 1] if _valid_ohlc(item)]
    return positive_volume_ratio(volumes, recent_window, base_window, min_count=recent_window)


def _volume_ratio_index_is_valid(
    rows: list[Kline],
    index: int,
    recent_window: int,
    base_window: int = VOLUME_BASE_WINDOW,
) -> bool:
    return (
        recent_window > 0
        and base_window > 0
        and _has_valid_index(rows, index)
        and index + 1 >= recent_window
        and _valid_positive_volume_row(rows[index])
    )


TECHNICAL_FACTOR_SPECS: tuple[FactorSpec, ...] = (
    FactorSpec(
        id="trend_momentum",
        name="趋势动量",
        category="技术",
        weight=1.35,
        direction="正向",
        evaluator=_trend_proxy_score_at,
        trigger=_trend_trigger,
    ),
    FactorSpec(
        id="volume_confirmation",
        name="量价确认",
        category="技术",
        weight=1.1,
        direction="正向",
        evaluator=_volume_proxy_score_at,
        trigger=_volume_trigger,
    ),
)

RISK_FACTOR_SPECS: tuple[FactorSpec, ...] = (
    FactorSpec(
        id="risk_pressure",
        name="风险压力",
        category="风控",
        weight=1.25,
        direction="正向",
        evaluator=_risk_proxy_score_at,
        trigger=_risk_trigger,
    ),
)

POSITION_FACTOR_SPECS: tuple[FactorSpec, ...] = (
    FactorSpec(
        id="chip_position",
        name="筹码位置",
        category="筹码",
        weight=0.95,
        direction="正向",
        evaluator=_chip_position_score_at,
        trigger=_chip_trigger,
    ),
)

FLOW_FACTOR_SPECS: tuple[FactorSpec, ...] = (
    FactorSpec(
        id="fund_flow_proxy",
        name="资金连续性",
        category="资金",
        weight=1.1,
        direction="正向",
        evaluator=_fund_flow_proxy_score_at,
        trigger=_fund_flow_trigger,
    ),
)

STRENGTH_FACTOR_SPECS: tuple[FactorSpec, ...] = (
    FactorSpec(
        id="leadership_strength",
        name="龙头强度",
        category="强弱",
        weight=1.05,
        direction="正向",
        evaluator=_leadership_proxy_score_at,
        trigger=_leadership_trigger,
    ),
)

REGISTERED_FACTOR_SPECS: tuple[FactorSpec, ...] = (
    *TECHNICAL_FACTOR_SPECS,
    *RISK_FACTOR_SPECS,
    *FLOW_FACTOR_SPECS,
    *POSITION_FACTOR_SPECS,
    *STRENGTH_FACTOR_SPECS,
)

_REGISTERED_FACTOR_SPEC_MAP = MappingProxyType(_factor_spec_map(REGISTERED_FACTOR_SPECS))
