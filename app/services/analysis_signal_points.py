from __future__ import annotations

from dataclasses import dataclass
from math import isfinite
from typing import Callable

from app.models.schemas import DataQuality, Quote, SignalItem


QUALITY_HIGH_RISK_MAX = 50
QUALITY_MEDIUM_RISK_MAX = 70
RISK_BREAKDOWN_CHANGE_PCT = -5.0
WEAK_TREND_SCORE_MAX = 45
WEAK_DROP_CHANGE_PCT = -2.0
LOW_RISK_SCORE_MIN = 75

TREND_PULLBACK_SCORE_MIN = 70
SUPPORT_PULLBACK_SCORE_MIN = 45
BREAKOUT_NEAR_RESISTANCE_RATIO = 0.99
BREAKOUT_CHANGE_PCT_MIN = 1.0
STOP_LOSS_SUPPORT_BUFFER = 1.01
PRESSURE_TAKE_PROFIT_RATIO = 0.985
PRESSURE_TAKE_PROFIT_SCORE_MAX = 70

T_INTRADAY_RANGE_FLOOR_PCT = 0.012
T_AREA_RANGE_RATIO = 0.4
T_NARROW_WIDTH_PCT = 1.2
T_TREND_CHANGE_PCT_MIN = 2.0
T_TREND_RESISTANCE_RATIO = 0.97
T_UNKNOWN_WIDTH_PCT = 100.0

STRONG_TREND_SCORE_MIN = 80
STRONG_GAIN_CHANGE_PCT_MIN = 5.0
ACTIVE_TURNOVER_MIN = 3.0
LARGE_AMOUNT_MIN = 5_000_000_000


@dataclass(frozen=True)
class RiskLevelContext:
    quote: Quote
    score: int
    support: float
    quality_score: int | None


@dataclass(frozen=True)
class RiskLevelRule:
    name: str
    level: str
    matches: Callable[[RiskLevelContext], bool]


@dataclass(frozen=True)
class BuyPointContext:
    quote: Quote
    score: int
    ma5: float
    ma10: float
    support: float
    resistance: float


@dataclass(frozen=True)
class SellPointContext:
    quote: Quote
    score: int
    ma5: float
    ma20: float
    support: float
    resistance: float


@dataclass(frozen=True)
class BuyPointRule:
    name: str
    build: Callable[[BuyPointContext], SignalItem | None]


@dataclass(frozen=True)
class SellPointRule:
    name: str
    build: Callable[[SellPointContext], SignalItem | None]


@dataclass(frozen=True)
class StrengthTagContext:
    quote: Quote
    score: int


@dataclass(frozen=True)
class TStyleContext:
    quote: Quote
    support: float
    resistance: float
    width_pct: float


@dataclass(frozen=True)
class TStyleRule:
    name: str
    style: str
    matches: Callable[[TStyleContext], bool]


@dataclass(frozen=True)
class StrengthTagRule:
    name: str
    tag: str
    matches: Callable[[StrengthTagContext], bool]


def _finite_number(value: object) -> float | None:
    try:
        number = float(value)
    except (TypeError, ValueError):
        return None
    return number if isfinite(number) else None


def _positive_number(value: object) -> float:
    number = _finite_number(value)
    return number if number is not None and number > 0 else 0.0


def _non_negative_number(value: object) -> float:
    number = _finite_number(value)
    return number if number is not None and number >= 0 else 0.0


def _width_pct_value(value: object) -> float:
    number = _finite_number(value)
    return number if number is not None and number >= 0 else T_UNKNOWN_WIDTH_PCT


def _score_value(score: object) -> int:
    number = _finite_number(score)
    if number is None:
        return 0
    return int(min(max(number, 0), 100))


def _quality_score(quality: DataQuality | None) -> int | None:
    if quality is None:
        return None
    number = _finite_number(quality.score)
    return int(number) if number is not None else None


def _quote_price(quote: Quote) -> float:
    return _positive_number(quote.price)


def _quote_change_pct(quote: Quote) -> float:
    number = _finite_number(quote.change_pct)
    return number if number is not None else 0.0


def _unique_signal_items(items: list[SignalItem]) -> list[SignalItem]:
    seen: set[tuple[str, str, str]] = set()
    unique: list[SignalItem] = []
    for item in items:
        key = (item.title, item.level, item.reason)
        if key in seen:
            continue
        seen.add(key)
        unique.append(item)
    return unique


def _unique_tags(tags: list[str]) -> list[str]:
    seen: set[str] = set()
    unique: list[str] = []
    for tag in tags:
        if tag in seen:
            continue
        seen.add(tag)
        unique.append(tag)
    return unique


def risk_level(
    quote: Quote, score: int, support: float, quality: DataQuality | None = None
) -> str:
    context = RiskLevelContext(
        quote=quote,
        score=_score_value(score),
        support=_positive_number(support),
        quality_score=_quality_score(quality),
    )
    for rule in RISK_LEVEL_RULES:
        if rule.matches(context):
            return rule.level
    return "可控观察"


def _low_quality_blocks_analysis(context: RiskLevelContext) -> bool:
    return (
        context.quality_score is not None
        and context.quality_score < QUALITY_HIGH_RISK_MAX
    )


def _medium_quality_limits_analysis(context: RiskLevelContext) -> bool:
    return (
        context.quality_score is not None
        and context.quality_score < QUALITY_MEDIUM_RISK_MAX
    )


def _price_breaks_risk_line(context: RiskLevelContext) -> bool:
    price = _quote_price(context.quote)
    change_pct = _quote_change_pct(context.quote)
    breaks_support = bool(context.support and price and price < context.support)
    return change_pct <= RISK_BREAKDOWN_CHANGE_PCT or breaks_support


def _trend_or_drop_is_weak(context: RiskLevelContext) -> bool:
    return (
        context.score < WEAK_TREND_SCORE_MAX
        or _quote_change_pct(context.quote) < WEAK_DROP_CHANGE_PCT
    )


def _trend_is_low_risk(context: RiskLevelContext) -> bool:
    return (
        bool(_quote_price(context.quote))
        and context.score >= LOW_RISK_SCORE_MIN
        and _quote_change_pct(context.quote) > 0
    )


RISK_LEVEL_RULES = (
    RiskLevelRule("low_quality", "高风险", _low_quality_blocks_analysis),
    RiskLevelRule("medium_quality", "中等风险", _medium_quality_limits_analysis),
    RiskLevelRule("price_breakdown", "高风险", _price_breaks_risk_line),
    RiskLevelRule("weak_trend_or_drop", "中等风险", _trend_or_drop_is_weak),
    RiskLevelRule("strong_low_risk", "低风险", _trend_is_low_risk),
)


def buy_points(
    quote: Quote,
    score: int,
    ma5: float,
    ma10: float,
    support: float,
    resistance: float,
) -> list[SignalItem]:
    context = BuyPointContext(
        quote=quote,
        score=_score_value(score),
        ma5=_positive_number(ma5),
        ma10=_positive_number(ma10),
        support=_positive_number(support),
        resistance=_positive_number(resistance),
    )
    return _collect_buy_points(context)


def sell_points(
    quote: Quote,
    score: int,
    ma5: float,
    ma20: float,
    support: float,
    resistance: float,
) -> list[SignalItem]:
    context = SellPointContext(
        quote=quote,
        score=_score_value(score),
        ma5=_positive_number(ma5),
        ma20=_positive_number(ma20),
        support=_positive_number(support),
        resistance=_positive_number(resistance),
    )
    return _collect_sell_points(context)


def _collect_buy_points(context: BuyPointContext) -> list[SignalItem]:
    items = _unique_signal_items(
        [item for rule in BUY_POINT_RULES if (item := rule.build(context))]
    )
    return items or [_fallback_buy_point()]


def _collect_sell_points(context: SellPointContext) -> list[SignalItem]:
    items = _unique_signal_items(
        [item for rule in SELL_POINT_RULES if (item := rule.build(context))]
    )
    return items or [_fallback_sell_point()]


def _buy_trend_pullback(context: BuyPointContext) -> SignalItem | None:
    price = _quote_price(context.quote)
    if context.score < TREND_PULLBACK_SCORE_MIN or not (
        price > context.ma5 > context.ma10
    ):
        return None
    return SignalItem(
        title="趋势试仓点",
        level="积极",
        reason=f"价格站上5日线，5日线高于10日线，可等回踩不破 {context.ma5:.2f} 再考虑分批参与。",
    )


def _buy_breakout_watch(context: BuyPointContext) -> SignalItem | None:
    price = _quote_price(context.quote)
    change_pct = _quote_change_pct(context.quote)
    near_resistance = bool(
        context.resistance
        and price
        and price >= context.resistance * BREAKOUT_NEAR_RESISTANCE_RATIO
    )
    if not near_resistance or change_pct <= BREAKOUT_CHANGE_PCT_MIN:
        return None
    return SignalItem(
        title="突破观察点",
        level="观察",
        reason=f"价格接近20日压力位 {context.resistance:.2f}，若放量突破并收稳，可作为右侧确认信号。",
    )


def _buy_support_pullback(context: BuyPointContext) -> SignalItem | None:
    price = _quote_price(context.quote)
    if (
        not context.support
        or not (context.support < price < context.ma10)
        or context.score < SUPPORT_PULLBACK_SCORE_MIN
    ):
        return None
    return SignalItem(
        title="支撑低吸点",
        level="谨慎",
        reason=f"当前靠近支撑区 {context.support:.2f}，只适合小仓位观察，跌破支撑应停止低吸。",
    )


def _fallback_buy_point() -> SignalItem:
    return SignalItem(
        title="暂不追买",
        level="谨慎",
        reason="趋势和价格位置没有形成清晰共振，新手更适合等待回踩确认或突破确认。",
    )


def _sell_short_term_reduce(context: SellPointContext) -> SignalItem | None:
    price = _quote_price(context.quote)
    if not price or not context.ma5 or price >= context.ma5:
        return None
    return SignalItem(
        title="短线减仓点",
        level="观察",
        reason=f"价格跌破5日线 {context.ma5:.2f}，短线强度下降，可考虑降低仓位。",
    )


def _sell_stop_loss_guard(context: SellPointContext) -> SignalItem | None:
    price = _quote_price(context.quote)
    near_support = bool(
        context.support
        and price
        and price <= context.support * STOP_LOSS_SUPPORT_BUFFER
    )
    if not near_support:
        return None
    return SignalItem(
        title="止损保护点",
        level="风险",
        reason=f"价格贴近20日支撑 {context.support:.2f}，若有效跌破，原趋势判断失效。",
    )


def _sell_pressure_take_profit(context: SellPointContext) -> SignalItem | None:
    price = _quote_price(context.quote)
    near_resistance = bool(
        context.resistance
        and price
        and price >= context.resistance * PRESSURE_TAKE_PROFIT_RATIO
    )
    if not near_resistance or context.score >= PRESSURE_TAKE_PROFIT_SCORE_MAX:
        return None
    return SignalItem(
        title="压力止盈点",
        level="谨慎",
        reason=f"价格接近压力区 {context.resistance:.2f}，但趋势分不足，适合先保护利润。",
    )


def _sell_swing_risk(context: SellPointContext) -> SignalItem | None:
    price = _quote_price(context.quote)
    if not price or not context.ma20 or price >= context.ma20:
        return None
    return SignalItem(
        title="波段风控点",
        level="风险",
        reason=f"价格低于20日线 {context.ma20:.2f}，中短期趋势偏弱，避免扩大亏损。",
    )


def _fallback_sell_point() -> SignalItem:
    return SignalItem(
        title="持有观察",
        level="观察",
        reason="暂未触发明显卖出信号，继续关注5日线和量能变化。",
    )


BUY_POINT_RULES = (
    BuyPointRule("trend_pullback", _buy_trend_pullback),
    BuyPointRule("breakout_watch", _buy_breakout_watch),
    BuyPointRule("support_pullback", _buy_support_pullback),
)


SELL_POINT_RULES = (
    SellPointRule("short_term_reduce", _sell_short_term_reduce),
    SellPointRule("stop_loss_guard", _sell_stop_loss_guard),
    SellPointRule("pressure_take_profit", _sell_pressure_take_profit),
    SellPointRule("swing_risk", _sell_swing_risk),
)


def t_plan(quote: Quote, support: float, resistance: float) -> list[SignalItem]:
    price = _quote_price(quote)
    support_level = _positive_number(support)
    resistance_level = _positive_number(resistance)
    intraday_range = _intraday_range(quote, price)
    low_area = t_low_area(price, support_level, intraday_range)
    high_area = t_high_area(price, resistance_level, intraday_range)
    width_pct = _t_width_pct(low_area, high_area, price)
    zones_confirmed = _t_plan_zones_confirmed(price, low_area, high_area)
    style = t_style(quote, support_level, resistance_level, width_pct)
    stop_line = support_level or low_area
    change_pct = _quote_change_pct(quote)
    return [
        SignalItem(
            title="已有底仓才做T",
            level="观察",
            reason="A股普通股票通常是T+1，今日买入部分不能当日卖出；做T应只使用已有可卖底仓。",
        ),
        SignalItem(
            title=f"{style}低吸区",
            level="谨慎" if style != "趋势型" else "观察",
            reason=_t_low_area_reason(low_area, zones_confirmed),
        ),
        SignalItem(
            title=f"{style}高抛区",
            level="积极" if change_pct > 0 else "观察",
            reason=_t_high_area_reason(high_area, zones_confirmed),
        ),
        SignalItem(
            title="做T失效条件",
            level="风险",
            reason=_t_stop_condition_reason(width_pct, stop_line, zones_confirmed),
        ),
    ]


def _intraday_range(quote: Quote, price: float) -> float:
    high = _positive_number(quote.high)
    low = _positive_number(quote.low)
    quoted_range = high - low if high >= low else 0.0
    return max(quoted_range, price * T_INTRADAY_RANGE_FLOOR_PCT)


def _t_width_pct(low_area: float, high_area: float, price: float) -> float:
    if not price:
        return 0.0
    return round(max(high_area - low_area, 0.0) / price * 100, 2)


def _t_plan_zones_confirmed(price: float, low_area: float, high_area: float) -> bool:
    return bool(price > 0 and low_area > 0 and high_area > low_area)


def _t_low_area_reason(low_area: float, zones_confirmed: bool) -> str:
    if not zones_confirmed:
        return "低吸区待确认；等待有效现价、日内高低点和支撑位恢复后，再考虑小额接回。"
    return f"参考 {low_area:.2f} 附近；只有缩量止跌、跌速放缓且未有效跌破支撑时，才考虑用已卖出部分小额接回。"


def _t_high_area_reason(high_area: float, zones_confirmed: bool) -> str:
    if not zones_confirmed:
        return "高抛区待确认；压力位或日内区间无效时，优先等待分时信号清晰。"
    return f"参考 {high_area:.2f} 附近；冲高量能不足、接近压力或分时转弱时，优先卖出可卖底仓降低成本。"


def _t_stop_condition_reason(width_pct: float, stop_line: float, zones_confirmed: bool) -> str:
    if not zones_confirmed:
        return "若做T区间待确认、价格或支撑位无效，或冲高回落放量转弱，当天停止做T。"
    return f"若区间宽度不足约 {width_pct:.2f}%、跌破 {stop_line:.2f} 后不能快速收回，或冲高回落放量转弱，当天停止做T。"


def t_low_area(price: float, support: float, intraday_range: float) -> float:
    price = _positive_number(price)
    support = _positive_number(support)
    intraday_range = _non_negative_number(intraday_range)
    volatility_floor = max(0.0, price - intraday_range * T_AREA_RANGE_RATIO)
    if support and support < price:
        return round(max(support, volatility_floor), 2)
    return round(volatility_floor, 2)


def t_high_area(price: float, resistance: float, intraday_range: float) -> float:
    price = _positive_number(price)
    resistance = _positive_number(resistance)
    intraday_range = _non_negative_number(intraday_range)
    volatility_ceiling = price + intraday_range * T_AREA_RANGE_RATIO
    if resistance and resistance > price:
        return round(min(resistance, volatility_ceiling), 2)
    return round(volatility_ceiling, 2)


def t_style(quote: Quote, support: float, resistance: float, width_pct: float) -> str:
    context = TStyleContext(
        quote=quote,
        support=_positive_number(support),
        resistance=_positive_number(resistance),
        width_pct=_width_pct_value(width_pct),
    )
    return next(
        (rule.style for rule in T_STYLE_RULES if rule.matches(context)), "波动型"
    )


def _narrow_t_style(context: TStyleContext) -> bool:
    return bool(_quote_price(context.quote) and context.width_pct < T_NARROW_WIDTH_PCT)


def _trend_t_style(context: TStyleContext) -> bool:
    price = _quote_price(context.quote)
    return bool(
        _quote_change_pct(context.quote) >= T_TREND_CHANGE_PCT_MIN
        and context.resistance
        and price
        and price >= context.resistance * T_TREND_RESISTANCE_RATIO
    )


def _range_t_style(context: TStyleContext) -> bool:
    price = _quote_price(context.quote)
    return bool(
        context.support
        and context.resistance
        and price
        and context.support < price < context.resistance
    )


def strength_tags(quote: Quote, score: int) -> list[str]:
    context = StrengthTagContext(quote=quote, score=_score_value(score))
    tags = _unique_tags(
        [rule.tag for rule in STRENGTH_TAG_RULES if rule.matches(context)]
    )
    return tags or ["观察中"]


def _strong_trend_tag(context: StrengthTagContext) -> bool:
    return context.score >= STRONG_TREND_SCORE_MIN


def _strong_gain_tag(context: StrengthTagContext) -> bool:
    return _quote_change_pct(context.quote) >= STRONG_GAIN_CHANGE_PCT_MIN


def _active_turnover_tag(context: StrengthTagContext) -> bool:
    turnover_rate = _finite_number(context.quote.turnover_rate)
    return turnover_rate is not None and turnover_rate >= ACTIVE_TURNOVER_MIN


def _large_amount_tag(context: StrengthTagContext) -> bool:
    amount = _finite_number(context.quote.amount)
    return amount is not None and amount >= LARGE_AMOUNT_MIN


STRENGTH_TAG_RULES = (
    StrengthTagRule("strong_trend", "趋势强", _strong_trend_tag),
    StrengthTagRule("strong_gain", "涨幅强", _strong_gain_tag),
    StrengthTagRule("active_turnover", "换手活跃", _active_turnover_tag),
    StrengthTagRule("large_amount", "成交额大", _large_amount_tag),
)

T_STYLE_RULES = (
    TStyleRule("narrow", "窄幅", _narrow_t_style),
    TStyleRule("trend", "趋势型", _trend_t_style),
    TStyleRule("range", "区间型", _range_t_style),
)


__all__ = [
    "BUY_POINT_RULES",
    "SELL_POINT_RULES",
    "STRENGTH_TAG_RULES",
    "risk_level",
    "buy_points",
    "sell_points",
    "t_plan",
    "t_low_area",
    "t_high_area",
    "t_style",
    "strength_tags",
]
