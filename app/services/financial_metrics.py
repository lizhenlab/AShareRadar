from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from math import isfinite

from app.models.schemas import FinancialMetric
from app.services.scoring import clamp_score


@dataclass(frozen=True)
class MetricBand:
    score: int
    level: str
    summary: str
    matches: Callable[[float], bool]


@dataclass(frozen=True)
class LiquidityAmountBand:
    matches: Callable[[float], bool]
    delta: int


@dataclass(frozen=True)
class TurnoverAdjustmentRule:
    matches: Callable[[float], bool]
    delta: int


PE_BANDS: tuple[MetricBand, ...] = (
    MetricBand(28, "弱", "PE 为负或无有效意义，需检查盈利质量。", lambda value: value <= 0),
    MetricBand(68, "偏强", "PE 较低，可能有安全边际，也可能反映增长预期不足。", lambda value: value < 12),
    MetricBand(62, "偏强", "PE 处在较容易解释的区间。", lambda value: value <= 35),
    MetricBand(48, "中性", "PE 偏高，需要业绩增长继续配合。", lambda value: value <= 60),
)

PB_BANDS: tuple[MetricBand, ...] = (
    MetricBand(30, "弱", "PB 异常，需确认净资产或行情字段。", lambda value: value <= 0),
    MetricBand(66, "偏强", "PB 较低，资产价格锚相对清晰。", lambda value: value < 1.2),
    MetricBand(60, "偏强", "PB 处在较常见区间。", lambda value: value <= 4),
    MetricBand(46, "中性", "PB 偏高，需要盈利能力或成长性支撑。", lambda value: value <= 8),
)

MARKET_CAP_BANDS: tuple[MetricBand, ...] = (
    MetricBand(64, "偏强", "超大市值，流动性和机构关注度通常更好。", lambda value: value >= 2000),
    MetricBand(60, "偏强", "中大型市值，交易容量相对友好。", lambda value: value >= 500),
    MetricBand(52, "中性", "中等市值，弹性和波动都需要兼顾。", lambda value: value >= 100),
    MetricBand(45, "观察", "小市值弹性较高，但波动和流动性风险也更高。", lambda value: value >= 30),
)

LIQUIDITY_AMOUNT_RULES: tuple[LiquidityAmountBand, ...] = (
    LiquidityAmountBand(lambda amount: amount >= 1_000_000_000, 22),
    LiquidityAmountBand(lambda amount: amount >= 300_000_000, 14),
    LiquidityAmountBand(lambda amount: amount >= 80_000_000, 7),
)

TURNOVER_ADJUSTMENT_RULES: tuple[TurnoverAdjustmentRule, ...] = (
    TurnoverAdjustmentRule(lambda turnover: 1 <= turnover <= 8, 8),
    TurnoverAdjustmentRule(lambda turnover: turnover > 15, -4),
    TurnoverAdjustmentRule(lambda turnover: turnover < 0.5, -5),
)


def missing_metric(name: str, summary: str) -> FinancialMetric:
    return FinancialMetric(name=name, value="待接入", level="观察", summary=summary, source="数据缺失")


def pe_view(pe: float) -> tuple[int, str, str]:
    if not _usable_number(pe):
        return 28, "弱", "PE 字段异常，需确认行情源或盈利口径。"
    return _metric_band_view(pe, PE_BANDS, (30, "偏弱", "PE 明显偏高，估值压缩风险需要重点关注。"))


def pb_view(pb: float) -> tuple[int, str, str]:
    if not _usable_number(pb):
        return 30, "弱", "PB 字段异常，需确认净资产或行情字段。"
    return _metric_band_view(pb, PB_BANDS, (32, "偏弱", "PB 较高，市场对盈利和成长要求更苛刻。"))


def market_cap_view(market_cap: float) -> tuple[int, str, str]:
    if not _usable_number(market_cap) or market_cap <= 0:
        return 34, "偏弱", "总市值字段异常，规模和流动性判断需降权。"
    yi = market_cap / 100_000_000
    return _metric_band_view(yi, MARKET_CAP_BANDS, (36, "偏弱", "微小市值更容易受流动性和情绪冲击。"))


def liquidity_view(amount: float, turnover_rate: float | None) -> tuple[int, str, str]:
    if not _usable_number(amount) or amount <= 0:
        return 34, "偏弱", "成交额字段异常或缺失，价格信号可靠性需要降权。"
    score = clamp_score(45 + _liquidity_amount_delta(amount) + _turnover_delta(turnover_rate))
    return score, *_liquidity_level_text(score)


def financial_summary(score: int, missing: list[str]) -> str:
    base = "基础财务体检偏稳" if score >= 65 else "基础财务体检中性" if score >= 50 else "基础财务体检偏弱"
    if missing:
        return f"{base}，但仍缺少{missing[0]}等正式财报字段。"
    return base


def format_amount_text(value: float | None) -> str:
    if value is None or not _usable_number(value):
        return "--"
    if abs(value) >= 100_000_000:
        return f"{value / 100_000_000:.1f}亿"
    if abs(value) >= 10_000:
        return f"{value / 10_000:.1f}万"
    return f"{value:.0f}"


def _metric_band_view(value: float, bands: tuple[MetricBand, ...], fallback: tuple[int, str, str]) -> tuple[int, str, str]:
    return next(((rule.score, rule.level, rule.summary) for rule in bands if rule.matches(value)), fallback)


def _usable_number(value: float) -> bool:
    return isfinite(value)


def _liquidity_amount_delta(amount: float) -> int:
    return next((rule.delta for rule in LIQUIDITY_AMOUNT_RULES if rule.matches(amount)), -6)


def _turnover_delta(turnover_rate: float | None) -> int:
    if turnover_rate is None or not _usable_number(turnover_rate):
        return 0
    return next((rule.delta for rule in TURNOVER_ADJUSTMENT_RULES if rule.matches(turnover_rate)), 0)


def _liquidity_level_text(score: int) -> tuple[str, str]:
    if score >= 65:
        return "偏强", "交易活跃度较好，个股分析参考性更高。"
    if score >= 50:
        return "中性", "交易活跃度尚可，需要结合盘口和成交连续性。"
    return "偏弱", "交易活跃度偏弱，价格信号可能更容易失真。"


__all__ = [
    "financial_summary",
    "format_amount_text",
    "liquidity_view",
    "market_cap_view",
    "missing_metric",
    "pb_view",
    "pe_view",
]
