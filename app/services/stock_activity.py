from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass

from app.models.schemas import AnalysisResult, FundFlowAnalysis, FundFlowWindow, Kline, OrderBook, OrderPressure
from app.services.indicators import pct_change
from app.services.scoring import clamp_score, score_level
from app.services.stock_abnormal_context import current_volume_metrics
from app.utils.market_data import finite_float


@dataclass(frozen=True)
class OrderBookPressureMetrics:
    bid_amount: float
    ask_amount: float
    spread_pct: float | None
    bid_ask_ratio: float | None


@dataclass(frozen=True)
class RangePressureMetrics:
    intraday_range_pct: float
    distance_to_high: float
    distance_to_low: float


@dataclass(frozen=True)
class FundFlowScoreContext:
    amount: float
    turnover: float
    amount_score: int
    turnover_score: int
    direction_score: int
    volume_score: int


@dataclass(frozen=True)
class NumericScoreRule:
    score: int
    matches: Callable[[float], bool]


@dataclass(frozen=True)
class RelationRule:
    summary: str
    matches: Callable[[AnalysisResult, int], bool]


@dataclass(frozen=True)
class OrderBookPressureRule:
    level: str
    matches: Callable[[float], bool]


@dataclass(frozen=True)
class RangePressureRule:
    level: str
    matches: Callable[[RangePressureMetrics, float], bool]


DEFAULT_RULE_SCORE = 50
DATA_QUALITY_DOWNGRADE_THRESHOLD = 70
FUND_FLOW_AMOUNT_UNIT = 100_000_000
FUND_FLOW_AMOUNT_SCORE_CAP = 80
TODAY_FUND_FLOW_LABEL = "今日量价热度"
NEUTRAL_PRICE_VOLUME_SUMMARY = "量价关系中性，等待更明确方向。"
ORDER_BOOK_NEUTRAL_LEVEL = "盘口均衡"
ORDER_BOOK_STRONG_BID_RATIO = 1.25
ORDER_BOOK_STRONG_ASK_RATIO = 0.8
ORDER_BOOK_INSUFFICIENT_DEPTH_SUMMARY = "盘口深度不足，暂不能判断买卖盘强弱。"
FUND_FLOW_BASE_NOTES = (
    "数据性质：derived（衍生）。当前指标使用成交额、涨跌幅、换手率和量价关系，不是真实资金流。",
    "未接入逐笔成交或正式资金流前，不输出大单/特大单净流入结论。",
    "接入可核验资金流或逐笔成交后，必须与当前量价衍生指标分开展示。",
)
FUND_FLOW_UNAVAILABLE_NOTES = (
    "数据性质：unavailable（不可用）。缺少有效量价输入，不生成方向性量价代理结论。",
    "未接入逐笔成交或正式资金流，不输出真实资金流、大单或主力净流入结论。",
)
FUND_FLOW_DERIVED_METHODOLOGY = "成交额、涨跌幅、换手率和量价关系的规则衍生指标；不等于真实资金流。"
FUND_FLOW_UNAVAILABLE_METHODOLOGY = "有效量价输入不足，衍生指标不可用；未观测真实资金流。"
ORDER_BOOK_REALTIME_NOTE = "数据性质：observed（实测）。来自实时盘口挂单深度，仅反映当前时点订单压力，不代表已成交资金。"
RANGE_PRESSURE_FALLBACK_NOTE = "数据性质：estimated（估算）。实时盘口不可用，当前仅用日内高低价位置估算订单压力。"
ORDER_PRESSURE_UNAVAILABLE_NOTE = "数据性质：unavailable（不可用）。实时盘口和有效日内价格区间均缺失。"
ORDER_BOOK_METHODOLOGY = "实时盘口挂单金额比与价差观测；挂单不等于成交或资金流。"
RANGE_PRESSURE_METHODOLOGY = "由现价在日内高低区间的位置估算；不是真实盘口观测。"
UNAVAILABLE_PRESSURE_METHODOLOGY = "实时盘口与日内区间输入均不可用。"
FUND_FLOW_WEIGHTS = {
    "amount": 0.25,
    "turnover": 0.25,
    "direction": 0.3,
    "volume": 0.2,
}
TURNOVER_SCORE_RULES = (
    NumericScoreRule(60, lambda turnover: 2 <= turnover <= 8),
    NumericScoreRule(45, lambda turnover: turnover < 2),
    NumericScoreRule(50, lambda _turnover: True),
)
DIRECTION_SCORE_RULES = (
    NumericScoreRule(62, lambda change_pct: change_pct > 0),
    NumericScoreRule(42, lambda change_pct: change_pct < 0),
    NumericScoreRule(50, lambda _change_pct: True),
)
VOLUME_SCORE_RULES = (
    NumericScoreRule(68, lambda ratio: 1.2 <= ratio <= 2.5),
    NumericScoreRule(45, lambda ratio: ratio > 3),
    NumericScoreRule(42, lambda ratio: ratio < 0.7),
    NumericScoreRule(56, lambda _ratio: True),
)
ORDER_BOOK_PRESSURE_RULES = (
    OrderBookPressureRule("买盘偏强", lambda ratio: ratio > ORDER_BOOK_STRONG_BID_RATIO),
    OrderBookPressureRule("卖压偏强", lambda ratio: ratio < ORDER_BOOK_STRONG_ASK_RATIO),
)
RANGE_PRESSURE_RULES = (
    RangePressureRule(
        "上方卖压待消化",
        lambda metrics, change_pct: metrics.distance_to_high < metrics.distance_to_low and change_pct < 0,
    ),
    RangePressureRule(
        "下方承接较近",
        lambda metrics, change_pct: metrics.distance_to_low < metrics.distance_to_high and change_pct > 0,
    ),
)
PRICE_VOLUME_RELATION_RULES = (
    RelationRule("量价配合偏积极。", lambda analysis, volume_score: analysis.quote.change_pct > 1 and volume_score >= 60),
    RelationRule("价格上涨但量能跟随不足。", lambda analysis, volume_score: analysis.quote.change_pct > 1 and volume_score < 50),
    RelationRule("放量下跌，量价关系承压。", lambda analysis, volume_score: analysis.quote.change_pct < -1 and volume_score >= 60),
    RelationRule("价格回落，量能未明显放大。", lambda analysis, _volume_score: analysis.quote.change_pct < -1),
    RelationRule(NEUTRAL_PRICE_VOLUME_SUMMARY, lambda _analysis, _volume_score: True),
)


def build_fund_flow_analysis(analysis: AnalysisResult) -> FundFlowAnalysis:
    quote = analysis.quote
    context = _fund_flow_score_context(analysis)
    data_nature = _fund_flow_data_nature(analysis, context)
    overall = _fund_flow_overall_score(context, analysis.data_quality.score) if data_nature == "derived" else DEFAULT_RULE_SCORE
    relation = _price_volume_relation(analysis, volume_score=context.volume_score) if data_nature == "derived" else "量价代理输入不足，方向不可用。"
    return FundFlowAnalysis(
        symbol=_analysis_symbol(analysis),
        available=context.amount > 0,
        source=f"{quote.source}·{'量价衍生指标（非真实资金流）' if data_nature == 'derived' else '量价代理不可用'}",
        data_nature=data_nature,
        methodology=FUND_FLOW_DERIVED_METHODOLOGY if data_nature == "derived" else FUND_FLOW_UNAVAILABLE_METHODOLOGY,
        updated_at=quote.timestamp,
        overall_score=overall,
        level=score_level(overall) if data_nature == "derived" else "不可用",
        estimated_main_net_inflow=None,
        price_volume_relation=relation,
        windows=_fund_flow_windows(analysis, overall, relation),
        notes=_fund_flow_notes(analysis, data_nature),
    )


def build_order_pressure(
    analysis: AnalysisResult,
    *,
    order_book: OrderBook | None = None,
    order_book_error: str | None = None,
) -> OrderPressure:
    if order_book is not None:
        return _order_book_pressure(analysis, order_book)
    return _range_estimated_pressure(analysis, order_book_error=order_book_error)


def _fund_flow_score_context(analysis: AnalysisResult) -> FundFlowScoreContext:
    quote = analysis.quote
    amount = _positive_number(quote.amount)
    turnover = _positive_number(quote.turnover_rate)
    volume_score = _volume_score(analysis)
    return FundFlowScoreContext(
        amount=amount,
        turnover=turnover,
        amount_score=clamp_score(min(amount / FUND_FLOW_AMOUNT_UNIT, FUND_FLOW_AMOUNT_SCORE_CAP)),
        turnover_score=_turnover_score(quote.turnover_rate),
        direction_score=_score_from_rules(quote.change_pct, DIRECTION_SCORE_RULES),
        volume_score=volume_score,
    )


def _fund_flow_overall_score(context: FundFlowScoreContext, data_quality_score: int) -> int:
    raw_score = clamp_score(
        context.amount_score * FUND_FLOW_WEIGHTS["amount"]
        + context.turnover_score * FUND_FLOW_WEIGHTS["turnover"]
        + context.direction_score * FUND_FLOW_WEIGHTS["direction"]
        + context.volume_score * FUND_FLOW_WEIGHTS["volume"],
        round_value=True,
    )
    if data_quality_score < DATA_QUALITY_DOWNGRADE_THRESHOLD:
        return clamp_score(raw_score * 0.8 + data_quality_score * 0.2, round_value=True)
    return raw_score


def _fund_flow_windows(analysis: AnalysisResult, overall: int, relation: str) -> list[FundFlowWindow]:
    return [
        FundFlowWindow(
            label=TODAY_FUND_FLOW_LABEL,
            score=overall,
            estimated_net_inflow=None,
            summary=relation,
        ),
        _recent_fund_flow_window(analysis.klines, 5),
        _recent_fund_flow_window(analysis.klines, 10),
    ]


def _recent_fund_flow_window(klines: list[Kline], window: int) -> FundFlowWindow:
    return FundFlowWindow(
        label=f"{window}日连续性",
        score=_recent_momentum_score(klines, window),
        estimated_net_inflow=None,
        summary=_recent_window_summary(klines, window),
    )


def _fund_flow_notes(analysis: AnalysisResult, data_nature: str) -> list[str]:
    notes = list(FUND_FLOW_BASE_NOTES if data_nature == "derived" else FUND_FLOW_UNAVAILABLE_NOTES)
    if _should_downgrade_quality(analysis):
        notes.append(_data_quality_note(analysis, "量价热度评分已降权。"))
    return notes


def _order_book_pressure(analysis: AnalysisResult, order_book: OrderBook) -> OrderPressure:
    metrics = _order_book_metrics(order_book, analysis.quote.price)
    level = _quality_adjusted_level(_order_book_pressure_level(metrics.bid_ask_ratio), analysis)
    return OrderPressure(
        symbol=_analysis_symbol(analysis),
        available=True,
        source=order_book.source,
        data_nature="observed",
        methodology=ORDER_BOOK_METHODOLOGY,
        updated_at=order_book.updated_at,
        pressure_level=level,
        spread_pct=_rounded_optional(metrics.spread_pct, 4),
        bid_ask_ratio=_rounded_optional(metrics.bid_ask_ratio, 2),
        bid_amount=round(metrics.bid_amount, 2),
        ask_amount=round(metrics.ask_amount, 2),
        summary=_order_book_summary(level, metrics.bid_ask_ratio),
        notes=_order_book_notes(analysis),
    )


def _range_estimated_pressure(analysis: AnalysisResult, *, order_book_error: str | None) -> OrderPressure:
    metrics = _range_pressure_metrics(analysis)
    range_available = _range_pressure_available(analysis)
    level = _quality_adjusted_level(_range_pressure_level(analysis, metrics), analysis) if range_available else "订单压力不可用"
    summary = f"{level}，日内振幅约 {metrics.intraday_range_pct:.2f}%。" if range_available else "缺少有效实时盘口和日内价格区间，订单压力不可用。"
    return OrderPressure(
        symbol=_analysis_symbol(analysis),
        available=False,
        source=f"{analysis.quote.source}·{'日内区间估算' if range_available else '订单压力不可用'}",
        data_nature="estimated" if range_available else "unavailable",
        methodology=RANGE_PRESSURE_METHODOLOGY if range_available else UNAVAILABLE_PRESSURE_METHODOLOGY,
        updated_at=analysis.quote.timestamp,
        pressure_level=level,
        spread_pct=None,
        bid_ask_ratio=None,
        summary=summary,
        notes=_range_pressure_notes(analysis, order_book_error, range_available=range_available),
    )


def _order_book_metrics(order_book: OrderBook, quote_price: float) -> OrderBookPressureMetrics:
    bid_levels = _valid_order_levels(order_book.bid)
    ask_levels = _valid_order_levels(order_book.ask)
    bid_amount = _order_side_amount(bid_levels)
    ask_amount = _order_side_amount(ask_levels)
    ratio = bid_amount / ask_amount if ask_amount > 0 else None
    return OrderBookPressureMetrics(
        bid_amount=bid_amount,
        ask_amount=ask_amount,
        spread_pct=_spread_pct(bid_levels, ask_levels, quote_price),
        bid_ask_ratio=ratio,
    )


def _valid_order_levels(levels) -> tuple[tuple[float, float], ...]:
    clean_levels: list[tuple[float, float]] = []
    for item in levels:
        price = finite_float(getattr(item, "price", None))
        volume = finite_float(getattr(item, "volume", None))
        if price is not None and price > 0 and volume is not None and volume >= 0:
            clean_levels.append((price, volume))
    return tuple(clean_levels)


def _order_side_amount(levels: tuple[tuple[float, float], ...]) -> float:
    return sum(price * volume for price, volume in levels)


def _spread_pct(
    bid_levels: tuple[tuple[float, float], ...],
    ask_levels: tuple[tuple[float, float], ...],
    quote_price: float,
) -> float | None:
    best_bid = bid_levels[0][0] if bid_levels else None
    best_ask = ask_levels[0][0] if ask_levels else None
    price = finite_float(quote_price)
    if best_bid and best_ask and price and price > 0 and best_ask >= best_bid:
        return (best_ask - best_bid) / price * 100
    return None


def _order_book_pressure_level(ratio: float | None) -> str:
    if ratio is None:
        return ORDER_BOOK_NEUTRAL_LEVEL
    for rule in ORDER_BOOK_PRESSURE_RULES:
        if rule.matches(ratio):
            return rule.level
    return ORDER_BOOK_NEUTRAL_LEVEL


def _order_book_summary(level: str, ratio: float | None) -> str:
    if ratio is None:
        return ORDER_BOOK_INSUFFICIENT_DEPTH_SUMMARY
    return f"{level}，买卖盘金额比约 {ratio:.2f}。"


def _order_book_notes(analysis: AnalysisResult) -> list[str]:
    notes = [ORDER_BOOK_REALTIME_NOTE]
    if _should_downgrade_quality(analysis):
        notes.append(_data_quality_note(analysis, "盘口结论仅作低可信等级参考。"))
    return notes


def _range_pressure_metrics(analysis: AnalysisResult) -> RangePressureMetrics:
    quote = analysis.quote
    price = finite_float(quote.price)
    high = finite_float(quote.high)
    low = finite_float(quote.low)
    if price is None or price <= 0 or high is None or low is None or high < low:
        return RangePressureMetrics(intraday_range_pct=0, distance_to_high=0, distance_to_low=0)
    return RangePressureMetrics(
        intraday_range_pct=(high - low) / price * 100,
        distance_to_high=(high - price) / price * 100,
        distance_to_low=(price - low) / price * 100,
    )


def _range_pressure_level(analysis: AnalysisResult, metrics: RangePressureMetrics) -> str:
    change_pct = finite_float(analysis.quote.change_pct) or 0
    for rule in RANGE_PRESSURE_RULES:
        if rule.matches(metrics, change_pct):
            return rule.level
    return "盘口需实时源确认"


def _range_pressure_notes(analysis: AnalysisResult, order_book_error: str | None, *, range_available: bool) -> list[str]:
    notes = [RANGE_PRESSURE_FALLBACK_NOTE if range_available else ORDER_PRESSURE_UNAVAILABLE_NOTE]
    if order_book_error:
        notes.append(order_book_error[:160])
    if _should_downgrade_quality(analysis):
        notes.append(_data_quality_note(analysis, "盘口估算结论已降权。"))
    return notes


def _fund_flow_data_nature(analysis: AnalysisResult, context: FundFlowScoreContext) -> str:
    quote = analysis.quote
    has_turnover = (turnover := finite_float(quote.turnover_rate)) is not None and turnover >= 0
    has_change = finite_float(quote.change_pct) is not None
    has_history = len(analysis.klines) >= 2
    return "derived" if context.amount > 0 or has_turnover or has_change or has_history else "unavailable"


def _range_pressure_available(analysis: AnalysisResult) -> bool:
    quote = analysis.quote
    price = finite_float(quote.price)
    high = finite_float(quote.high)
    low = finite_float(quote.low)
    return price is not None and price > 0 and high is not None and low is not None and high >= low


def _quality_adjusted_level(level: str, analysis: AnalysisResult) -> str:
    return f"{level}（降权）" if _should_downgrade_quality(analysis) else level


def _should_downgrade_quality(analysis: AnalysisResult) -> bool:
    return analysis.data_quality.score < DATA_QUALITY_DOWNGRADE_THRESHOLD


def _data_quality_note(analysis: AnalysisResult, suffix: str) -> str:
    return f"数据质量为{analysis.data_quality.level}，{suffix}"


def _analysis_symbol(analysis: AnalysisResult) -> str:
    quote = analysis.quote
    return f"{quote.code}.{quote.market}"


def _rounded_optional(value: float | None, digits: int) -> float | None:
    parsed = finite_float(value)
    return round(parsed, digits) if parsed is not None else None


def _positive_number(value: float | None) -> float:
    parsed = finite_float(value)
    return parsed if parsed is not None and parsed > 0 else 0


def _turnover_score(value: float | None) -> int:
    parsed = finite_float(value)
    if parsed is None or parsed < 0:
        return DEFAULT_RULE_SCORE
    return _score_from_rules(parsed, TURNOVER_SCORE_RULES)


def _score_from_rules(value: float | None, rules: tuple[NumericScoreRule, ...]) -> int:
    parsed = finite_float(value)
    if parsed is None:
        return DEFAULT_RULE_SCORE
    for rule in rules:
        if rule.matches(parsed):
            return rule.score
    return DEFAULT_RULE_SCORE


def _volume_score(analysis: AnalysisResult) -> int:
    rows = analysis.klines[-10:]
    metrics = current_volume_metrics(analysis.quote, rows)
    ratio = _current_volume_ratio(metrics.latest_volume, metrics.avg_volume)
    if metrics.history_count < 5 or ratio is None:
        return DEFAULT_RULE_SCORE
    return _score_from_rules(ratio, VOLUME_SCORE_RULES)


def _current_volume_ratio(latest_volume: float | None, avg_volume: float | None) -> float | None:
    latest = finite_float(latest_volume)
    average = finite_float(avg_volume)
    if latest is None or latest < 0 or average is None or average <= 0:
        return None
    return latest / average


def _recent_momentum_score(klines: list[Kline], window: int) -> int:
    rows = klines[-window:]
    if len(rows) < 2:
        return DEFAULT_RULE_SCORE
    positive = sum(1 for index in range(1, len(rows)) if rows[index].close >= rows[index - 1].close)
    return clamp_score(35 + positive / (len(rows) - 1) * 45, round_value=True)


def _recent_window_summary(klines: list[Kline], window: int) -> str:
    rows = klines[-window:]
    if len(rows) < 2:
        return f"{window}日数据不足。"
    change = pct_change(rows[-1].close, rows[0].close)
    return f"近{len(rows)}日区间涨跌 {change:.2f}%。"


def _price_volume_relation(analysis: AnalysisResult, *, volume_score: int | None = None) -> str:
    score = volume_score if volume_score is not None else _volume_score(analysis)
    if finite_float(analysis.quote.change_pct) is None:
        return NEUTRAL_PRICE_VOLUME_SUMMARY
    for rule in PRICE_VOLUME_RELATION_RULES:
        if rule.matches(analysis, score):
            return rule.summary
    return NEUTRAL_PRICE_VOLUME_SUMMARY
