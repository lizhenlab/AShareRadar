from __future__ import annotations

import math
from dataclasses import dataclass
from typing import Callable

from app.models.market import (
    Kline,
    Quote,
)
from app.models.analysis import (
    SignalContribution,
)
from app.services.indicator_math import moving_average
from app.services.indicator_volume import recent_volume_ratio
from app.utils.market_data import filter_valid_klines


@dataclass(frozen=True)
class TrendContext:
    quote: Quote
    klines: list[Kline]
    ma5: float
    ma10: float
    ma20: float
    prev_ma5: float
    prev_ma20: float
    recent_high: float
    recent_low: float
    volume_ratio: float


@dataclass(frozen=True)
class MovingAverageRule:
    name: str
    left_label: str
    right_label: str
    positive_word: str
    negative_word: str
    neutral_word: str
    positive_impact: int
    negative_impact: int
    left_value: Callable[[TrendContext], float]
    right_value: Callable[[TrendContext], float]


@dataclass(frozen=True)
class VolumeSignalRule:
    name: str
    impact: int
    reason: Callable[[float], str]
    matches: Callable[[float, float], bool]


RELATIVE_NEUTRAL_BAND_PCT = 0.10
RELATIVE_FULL_EFFECT_PCT = 2.00


def build_trend_context(quote: Quote, klines: list[Kline]) -> TrendContext:
    valid_klines = filter_valid_klines(klines)
    recent_rows = valid_klines[-20:]
    ma5 = moving_average(valid_klines, 5)
    ma10 = moving_average(valid_klines, 10)
    ma20 = moving_average(valid_klines, 20)
    return TrendContext(
        quote=quote,
        klines=valid_klines,
        ma5=ma5,
        ma10=ma10,
        ma20=ma20,
        prev_ma5=moving_average(valid_klines[:-5], 5) if len(valid_klines) >= 10 else ma5,
        prev_ma20=moving_average(valid_klines[:-5], 20) if len(valid_klines) >= 25 else ma20,
        recent_high=max((item.high for item in recent_rows), default=0),
        recent_low=min((item.low for item in recent_rows), default=0),
        volume_ratio=recent_volume_ratio(valid_klines),
    )


def insufficient_sample_contributions() -> list[SignalContribution]:
    return [
        SignalContribution(
            category="数据",
            name="K线样本不足",
            impact=0,
            level="谨慎",
            reason="少于20根日K，趋势评分暂按中性处理。",
        )
    ]


def trend_contributions(context: TrendContext) -> list[SignalContribution]:
    return [
        *moving_average_contributions(context),
        *slope_contributions(context),
        price_change_contribution(context),
        *position_contributions(context),
        turnover_contribution(context),
        volume_confirmation_contribution(context),
    ]


def moving_average_contributions(context: TrendContext) -> list[SignalContribution]:
    return [_moving_average_contribution(rule, context) for rule in MOVING_AVERAGE_RULES]


def _moving_average_contribution(rule: MovingAverageRule, context: TrendContext) -> SignalContribution:
    left = rule.left_value(context)
    right = rule.right_value(context)
    impact, relative_pct = continuous_relative_impact(
        left,
        right,
        positive_impact=rule.positive_impact,
        negative_impact=rule.negative_impact,
    )
    word = relative_direction_word(
        relative_pct,
        positive_word=rule.positive_word,
        negative_word=rule.negative_word,
        neutral_word=rule.neutral_word,
    )
    reason = f"{rule.left_label} {left:.2f} {word} {rule.right_label} {right:.2f}（相对差 {format_relative_pct(relative_pct)}）。"
    return contribution("均线", rule.name, impact, reason)


def slope_contributions(context: TrendContext) -> list[SignalContribution]:
    short_impact, short_relative_pct = continuous_relative_impact(
        context.ma5,
        context.prev_ma5,
        positive_impact=7,
        negative_impact=-5,
    )
    wave_impact, wave_relative_pct = continuous_relative_impact(
        context.ma20,
        context.prev_ma20,
        positive_impact=6,
        negative_impact=-6,
    )
    return [
        contribution(
            "斜率",
            "短线斜率",
            short_impact,
            "5日线较前5日 "
            f"{relative_direction_word(short_relative_pct, positive_word='抬升', negative_word='走弱', neutral_word='基本持平')}"
            f"（相对变化 {format_relative_pct(short_relative_pct)}）。",
        ),
        contribution(
            "斜率",
            "波段斜率",
            wave_impact,
            "20日线较前5日 "
            f"{relative_direction_word(wave_relative_pct, positive_word='抬升', negative_word='下行', neutral_word='基本持平')}"
            f"（相对变化 {format_relative_pct(wave_relative_pct)}）。",
        ),
    ]


def continuous_relative_impact(
    value: float,
    reference: float,
    *,
    positive_impact: int,
    negative_impact: int,
    neutral_band_pct: float = RELATIVE_NEUTRAL_BAND_PCT,
    full_effect_pct: float = RELATIVE_FULL_EFFECT_PCT,
) -> tuple[int, float | None]:
    """Map a relative distance to a bounded integer impact without an equality cliff."""
    if not math.isfinite(neutral_band_pct) or not math.isfinite(full_effect_pct) or neutral_band_pct < 0 or full_effect_pct <= neutral_band_pct:
        raise ValueError("relative impact thresholds must satisfy 0 <= neutral < full effect")
    if positive_impact < 0 or negative_impact > 0:
        raise ValueError("relative impact bounds must satisfy negative <= 0 <= positive")

    relative_pct = relative_change_pct(value, reference)
    if relative_pct is None or abs(relative_pct) <= neutral_band_pct:
        return 0, relative_pct

    progress = min(1.0, (abs(relative_pct) - neutral_band_pct) / (full_effect_pct - neutral_band_pct))
    cap = positive_impact if relative_pct > 0 else abs(negative_impact)
    magnitude = min(cap, int(math.floor(cap * progress + 0.5)))
    return (magnitude if relative_pct > 0 else -magnitude), relative_pct


def relative_change_pct(value: float, reference: float) -> float | None:
    if not math.isfinite(value) or not math.isfinite(reference) or reference == 0:
        return None
    return (value - reference) / abs(reference) * 100


def relative_direction_word(
    relative_pct: float | None,
    *,
    positive_word: str,
    negative_word: str,
    neutral_word: str,
    neutral_band_pct: float = RELATIVE_NEUTRAL_BAND_PCT,
) -> str:
    if relative_pct is None or abs(relative_pct) <= neutral_band_pct:
        return neutral_word
    return positive_word if relative_pct > 0 else negative_word


def format_relative_pct(relative_pct: float | None) -> str:
    if relative_pct is None:
        return "不可用"
    normalized = 0.0 if abs(relative_pct) < 0.005 else relative_pct
    return f"{normalized:+.2f}%"


def price_change_contribution(context: TrendContext) -> SignalContribution:
    return contribution(
        "价格",
        "日内涨跌",
        change_impact(context.quote.change_pct),
        f"当前涨跌幅 {context.quote.change_pct:.2f}%。",
    )


def position_contributions(context: TrendContext) -> list[SignalContribution]:
    quote = context.quote
    contributions: list[SignalContribution] = []
    if quote.price >= context.recent_high * 0.985:
        contributions.append(
            contribution(
                "位置",
                "接近20日高位",
                12,
                f"现价接近20日高点 {context.recent_high:.2f}，右侧强度较好。",
            )
        )
    if quote.price <= context.recent_low * 1.03:
        contributions.append(
            contribution(
                "位置",
                "接近20日低位",
                -10,
                f"现价接近20日低点 {context.recent_low:.2f}，需要防止弱势延续。",
            )
        )
    return contributions


def turnover_contribution(context: TrendContext) -> SignalContribution:
    turnover_rate = context.quote.turnover_rate
    if turnover_rate:
        impact, reason = turnover_signal(turnover_rate)
        return contribution("活跃度", "换手率", impact, reason)
    return contribution("活跃度", "换手率", 0, "缺少换手率字段，活跃度不加分也不扣分。")


def volume_confirmation_contribution(context: TrendContext) -> SignalContribution:
    impact, reason = volume_signal(context.quote.change_pct, context.volume_ratio)
    return contribution("量能", "量价确认", impact, reason)


def contribution(category: str, name: str, impact: int, reason: str) -> SignalContribution:
    return SignalContribution(
        category=category,
        name=name,
        impact=impact,
        level=impact_level(impact),
        reason=reason,
    )


def change_impact(change_pct: float) -> int:
    if change_pct > 3:
        return 10
    if change_pct > 1:
        return 6
    if change_pct < -3:
        return -12
    if change_pct < -1:
        return -6
    return 0


def turnover_signal(turnover_rate: float) -> tuple[int, str]:
    if 2 <= turnover_rate <= 8:
        return 8, f"换手率 {turnover_rate:.2f}% 处于相对活跃区间。"
    if turnover_rate > 15:
        return -5, f"换手率 {turnover_rate:.2f}% 偏高，短线分歧较大。"
    return 0, f"换手率 {turnover_rate:.2f}% 暂未形成明显加分。"


def volume_signal(change_pct: float, volume_ratio: float) -> tuple[int, str]:
    for rule in VOLUME_SIGNAL_RULES:
        if rule.matches(change_pct, volume_ratio):
            return rule.impact, rule.reason(volume_ratio)
    return 0, _volume_reason(volume_ratio, "量价暂未明显偏离。")


def _volume_reason(volume_ratio: float, suffix: str) -> str:
    return f"近5日量能约为20日均量 {volume_ratio:.2f} 倍，{suffix}"


MOVING_AVERAGE_RULES = (
    MovingAverageRule("现价与5日线", "现价", "5日线", "高于", "低于", "贴近", 8, -8, lambda context: context.quote.price, lambda context: context.ma5),
    MovingAverageRule("短线均线排列", "5日线", "10日线", "高于", "低于", "基本持平于", 10, -6, lambda context: context.ma5, lambda context: context.ma10),
    MovingAverageRule("波段均线排列", "10日线", "20日线", "高于", "低于", "基本持平于", 12, -8, lambda context: context.ma10, lambda context: context.ma20),
)


VOLUME_SIGNAL_RULES = (
    VolumeSignalRule("positive_volume_expansion", 6, lambda ratio: _volume_reason(ratio, "上涨有量能确认。"), lambda change, ratio: change > 0 and ratio >= 1.25),
    VolumeSignalRule("negative_volume_expansion", -7, lambda ratio: _volume_reason(ratio, "下跌放量需要谨慎。"), lambda change, ratio: change < 0 and ratio >= 1.25),
    VolumeSignalRule("low_volume_large_move", -4, lambda ratio: _volume_reason(ratio, "价格波动缺少量能配合。"), lambda change, ratio: ratio < 0.65 and abs(change) > 2),
)


def impact_level(impact: int) -> str:
    if impact >= 6:
        return "积极"
    if impact <= -8:
        return "风险"
    if impact < 0:
        return "谨慎"
    return "观察"


def trend_label(score: int) -> str:
    if score >= 80:
        return "强势上行"
    if score >= 65:
        return "偏强震荡"
    if score >= 45:
        return "中性观察"
    if score >= 30:
        return "偏弱调整"
    return "风险释放中"


__all__ = [
    "MOVING_AVERAGE_RULES",
    "RELATIVE_FULL_EFFECT_PCT",
    "RELATIVE_NEUTRAL_BAND_PCT",
    "TrendContext",
    "VOLUME_SIGNAL_RULES",
    "build_trend_context",
    "change_impact",
    "continuous_relative_impact",
    "contribution",
    "impact_level",
    "insufficient_sample_contributions",
    "moving_average_contributions",
    "position_contributions",
    "price_change_contribution",
    "relative_change_pct",
    "relative_direction_word",
    "slope_contributions",
    "trend_contributions",
    "trend_label",
    "turnover_contribution",
    "turnover_signal",
    "volume_confirmation_contribution",
    "volume_signal",
]
