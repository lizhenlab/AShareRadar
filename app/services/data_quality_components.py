from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
from math import isclose, isfinite
from typing import Callable

from app.models.schemas import DataQuality, Kline, KlineQuality, Quote
from app.services.data_quality_kline import assess_kline_quality, kline_quality_penalty
from app.services.data_quality_time import quote_delay_seconds, quote_freshness_penalty
from app.utils.time import datetime_to_text


CHANGE_PCT_TOLERANCE = 0.3
PRICE_BOUNDARY_REL_TOLERANCE = 1e-12
PRICE_BOUNDARY_ABS_TOLERANCE = 1e-9
MIN_KLINE_COUNT_FOR_ANALYSIS = 60
CONSISTENCY_ANOMALY_LEVELS = {"存在差异", "字段异常"}


@dataclass
class DataQualityScoreState:
    score: int = 100
    notes: list[str] = field(default_factory=list)
    anomalies: list[str] = field(default_factory=list)

    def penalize(self, points: int, *, note: str | None = None, anomaly: str | None = None) -> None:
        self.score -= normalized_penalty(points)
        if note:
            self.notes.append(note)
        if anomaly:
            self.anomalies.append(anomaly)


@dataclass(frozen=True)
class QuoteFieldRule:
    name: str
    penalty: int
    note: str
    anomaly: str
    matches: Callable[[Quote], bool]


def build_data_quality_report(
    quote: Quote,
    klines: list[Kline],
    *,
    consistency_level: str,
    consistency_notes: list[str] | None,
    consistency_penalty: int,
    require_kline: bool,
    now: datetime,
) -> DataQuality:
    state = DataQualityScoreState()
    kline_quality = assess_kline_quality(klines, now=now) if require_kline or klines else None

    apply_source_quality(state, quote)
    apply_kline_quality(state, klines, kline_quality, require_kline=require_kline)
    apply_quote_field_quality(state, quote)
    apply_quote_freshness(state, quote, now)
    apply_consistency_quality(state, consistency_level, consistency_notes, consistency_penalty)
    if not state.notes:
        state.notes.append(default_quality_note(require_kline))

    score = clamp_quality_score(state.score)
    return DataQuality(
        level=data_quality_level(score),
        source=quote.source,
        quote_time=quote.timestamp,
        kline_count=len(klines),
        score=score,
        checked_at=datetime_to_text(now),
        quote_delay_seconds=quote_delay_seconds(quote.timestamp, now=now),
        consistency_level=consistency_level,
        kline_quality=kline_quality,
        anomalies=state.anomalies,
        notes=state.notes,
    )


def apply_source_quality(state: DataQualityScoreState, quote: Quote) -> None:
    source = quote.source or ""
    if is_fallback_quote_source(source):
        state.penalize(16, note="当前报价来自兜底缓存，说明实时行情源本轮不可用。", anomaly="报价兜底缓存")
    elif "短时缓存" in source:
        state.penalize(8, note="当前报价来自短时缓存，需结合报价时间确认新鲜度。")
    elif "缓存" in source:
        state.penalize(8, note="当前报价来自缓存，已结合报价时间评估新鲜度。")
    if is_demo_quote_source(source):
        state.penalize(45, note="当前报价来自演示数据，不能作为真实行情依据。", anomaly="演示行情")


def apply_kline_quality(
    state: DataQualityScoreState,
    klines: list[Kline],
    kline_quality: KlineQuality | None,
    *,
    require_kline: bool,
) -> None:
    if not require_kline:
        return
    if 0 < len(klines) < MIN_KLINE_COUNT_FOR_ANALYSIS:
        state.penalize(20, note="K线数量偏少，趋势判断可靠性下降。", anomaly="K线数量不足")
    if kline_quality is None:
        return
    kline_penalty, kline_anomalies = kline_quality_penalty(kline_quality)
    state.score -= normalized_penalty(kline_penalty)
    state.anomalies.extend(kline_anomalies)
    state.notes.extend(kline_quality.notes)


def apply_quote_field_quality(state: DataQualityScoreState, quote: Quote) -> None:
    for rule in QUOTE_FIELD_RULES:
        if rule.matches(quote):
            state.penalize(rule.penalty, note=rule.note, anomaly=rule.anomaly)


def _quote_price_invalid(quote: Quote) -> bool:
    return not positive_finite(quote.price)


def _quote_prev_close_missing(quote: Quote) -> bool:
    return not positive_finite(quote.prev_close)


def _quote_high_low_invalid(quote: Quote) -> bool:
    return not (positive_finite(quote.high) and positive_finite(quote.low))


def _quote_high_low_inverted(quote: Quote) -> bool:
    return quote_range_fields_usable(quote) and quote.high < quote.low


def _quote_price_above_high(quote: Quote) -> bool:
    return quote_range_usable(quote) and greater_than_boundary(quote.price, quote.high * 1.01)


def _quote_price_below_low(quote: Quote) -> bool:
    return quote_range_usable(quote) and less_than_boundary(quote.price, quote.low * 0.99)


def _quote_change_pct_invalid(quote: Quote) -> bool:
    return not isfinite(quote.change_pct)


def _quote_change_pct_mismatch(quote: Quote) -> bool:
    if not (positive_finite(quote.price) and positive_finite(quote.prev_close) and isfinite(quote.change_pct)):
        return False
    expected_change_pct = (quote.price - quote.prev_close) / quote.prev_close * 100
    return greater_than_boundary(abs(expected_change_pct - quote.change_pct), CHANGE_PCT_TOLERANCE)


def normalized_penalty(points: int) -> int:
    return max(0, points)


def positive_finite(value: float) -> bool:
    return isfinite(value) and value > 0


def quote_range_fields_usable(quote: Quote) -> bool:
    return positive_finite(quote.high) and positive_finite(quote.low)


def quote_range_usable(quote: Quote) -> bool:
    return positive_finite(quote.price) and quote_range_fields_usable(quote) and quote.high >= quote.low


def greater_than_boundary(value: float, boundary: float) -> bool:
    return value > boundary and not isclose(
        value,
        boundary,
        rel_tol=PRICE_BOUNDARY_REL_TOLERANCE,
        abs_tol=PRICE_BOUNDARY_ABS_TOLERANCE,
    )


def less_than_boundary(value: float, boundary: float) -> bool:
    return value < boundary and not isclose(
        value,
        boundary,
        rel_tol=PRICE_BOUNDARY_REL_TOLERANCE,
        abs_tol=PRICE_BOUNDARY_ABS_TOLERANCE,
    )


def is_fallback_quote_source(source: str) -> bool:
    normalized = source.lower()
    return "兜底缓存" in source or "fallback" in normalized


def is_demo_quote_source(source: str) -> bool:
    return "演示" in source or "demo" in source.lower()


QUOTE_FIELD_RULES = (
    QuoteFieldRule("price_invalid", 35, "现价缺失或异常，报价可靠性下降。", "报价价格异常", _quote_price_invalid),
    QuoteFieldRule("prev_close_missing", 10, "昨收价缺失或异常，涨跌幅校验能力下降。", "昨收价缺失", _quote_prev_close_missing),
    QuoteFieldRule("high_low_invalid", 12, "最高价或最低价缺失异常，价格区间校验能力下降。", "高低价异常", _quote_high_low_invalid),
    QuoteFieldRule("high_low_inverted", 30, "最高价低于最低价，行情字段异常。", "高低价倒挂", _quote_high_low_inverted),
    QuoteFieldRule("price_above_high", 20, "现价明显高于最高价，行情字段可能不同步。", "现价高于最高价", _quote_price_above_high),
    QuoteFieldRule("price_below_low", 20, "现价明显低于最低价，行情字段可能不同步。", "现价低于最低价", _quote_price_below_low),
    QuoteFieldRule("change_pct_invalid", 8, "涨跌幅字段缺失或异常，涨跌幅校验能力下降。", "涨跌幅异常", _quote_change_pct_invalid),
    QuoteFieldRule("change_pct_mismatch", 12, "现价、昨收和涨跌幅之间存在偏差。", "涨跌幅口径偏差", _quote_change_pct_mismatch),
)


def apply_quote_freshness(state: DataQualityScoreState, quote: Quote, now: datetime) -> None:
    freshness_penalty, freshness_notes, freshness_anomalies = quote_freshness_penalty(quote.timestamp, now)
    state.score -= normalized_penalty(freshness_penalty)
    state.notes.extend(freshness_notes)
    state.anomalies.extend(freshness_anomalies)


def apply_consistency_quality(
    state: DataQualityScoreState,
    consistency_level: str,
    consistency_notes: list[str] | None,
    consistency_penalty: int,
) -> None:
    if consistency_notes:
        state.notes.extend(consistency_notes)
        if consistency_level in CONSISTENCY_ANOMALY_LEVELS:
            state.anomalies.extend(consistency_notes)
    elif consistency_level in CONSISTENCY_ANOMALY_LEVELS:
        state.anomalies.append(consistency_anomaly_label(consistency_level))
    state.score -= normalized_penalty(consistency_penalty)


def consistency_anomaly_label(consistency_level: str) -> str:
    if consistency_level == "字段异常":
        return "多源字段异常"
    return "多源一致性异常"


def clamp_quality_score(score: int) -> int:
    return max(0, min(100, score))


def data_quality_level(score: int) -> str:
    if score >= 85:
        return "优秀"
    if score >= 70:
        return "良好"
    if score >= 50:
        return "一般"
    return "较弱"


def default_quality_note(require_kline: bool) -> str:
    return "报价和K线数据可用于当前个股分析。" if require_kline else "报价数据可用于当前提醒评估。"


__all__ = [
    "DataQualityScoreState",
    "QUOTE_FIELD_RULES",
    "QuoteFieldRule",
    "apply_consistency_quality",
    "apply_kline_quality",
    "apply_quote_field_quality",
    "apply_quote_freshness",
    "apply_source_quality",
    "build_data_quality_report",
    "clamp_quality_score",
    "data_quality_level",
    "default_quality_note",
]
