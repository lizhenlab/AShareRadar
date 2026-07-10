from __future__ import annotations

from collections.abc import Callable, Iterable
from dataclasses import dataclass
import math

from app.services.scoring import clamp_score
from app.utils.parsing import safe_float


STRONG_CHANGE_PCT = 3.0
WEAK_CHANGE_PCT = -3.0
BASE_BREADTH_SCORE = 45
UP_RATIO_SCORE_WEIGHT = 70
AVERAGE_CHANGE_SCORE_WEIGHT = 6
STRONG_WEAK_SCORE_WEIGHT = 35


@dataclass(frozen=True)
class MarketBreadthSnapshot:
    label: str
    score: int
    up_count: int
    down_count: int
    strong_count: int
    weak_count: int
    avg_change_pct: float
    risk_adjustment: float
    summary: str
    sample_count: int
    warnings: tuple[str, ...] = ()

    @property
    def degraded(self) -> bool:
        return bool(self.warnings)


@dataclass(frozen=True)
class MarketBreadthStats:
    sample_count: int
    changes: list[float]
    up_count: int
    down_count: int
    strong_count: int
    weak_count: int
    avg_change_pct: float

    @property
    def up_ratio(self) -> float:
        return self.up_count / self.sample_count if self.sample_count else 0

    @property
    def strong_weak_spread_ratio(self) -> float:
        return (self.strong_count - self.weak_count) / self.sample_count if self.sample_count else 0


@dataclass(frozen=True)
class MarketBreadthBand:
    name: str
    label: str
    risk_adjustment: float
    matches: Callable[[int], bool]


MARKET_BREADTH_BANDS = (
    MarketBreadthBand("strong", "市场宽度强", -0.08, lambda score: score >= 68),
    MarketBreadthBand("warm", "市场宽度偏暖", -0.03, lambda score: score >= 56),
    MarketBreadthBand("weak", "市场宽度弱", 0.12, lambda score: score <= 32),
    MarketBreadthBand("cold", "市场宽度偏冷", 0.06, lambda score: score <= 44),
)
NEUTRAL_BREADTH_BAND = MarketBreadthBand("neutral", "市场宽度中性", 0, lambda score: True)


def build_market_breadth_snapshot(quotes: list, *, warnings: Iterable[str] = ()) -> MarketBreadthSnapshot:
    clean_warnings = _clean_warnings(warnings)
    stats = _market_breadth_stats(quotes)
    if stats.sample_count == 0:
        return _empty_market_breadth_snapshot(clean_warnings)
    score = _market_breadth_score(stats)
    band = _market_breadth_band(score)
    return MarketBreadthSnapshot(
        label=band.label,
        score=score,
        up_count=stats.up_count,
        down_count=stats.down_count,
        strong_count=stats.strong_count,
        weak_count=stats.weak_count,
        avg_change_pct=round(stats.avg_change_pct, 2),
        risk_adjustment=round(band.risk_adjustment + (0.03 if clean_warnings else 0), 2),
        summary=_market_breadth_summary(band.label, stats),
        sample_count=stats.sample_count,
        warnings=clean_warnings,
    )


def _empty_market_breadth_snapshot(warnings: tuple[str, ...] = ()) -> MarketBreadthSnapshot:
    source_degraded = bool(warnings)
    return MarketBreadthSnapshot(
        label="市场宽度数据降级" if source_degraded else "市场宽度待确认",
        score=45 if source_degraded else 50,
        up_count=0,
        down_count=0,
        strong_count=0,
        weak_count=0,
        avg_change_pct=0,
        risk_adjustment=0.05 if source_degraded else 0,
        summary=(
            "市场宽度数据源暂不可用，环境判断已降级并以个股和行业为主。"
            if source_degraded
            else "市场宽度样本不足，环境判断暂以个股和行业为主。"
        ),
        sample_count=0,
        warnings=warnings,
    )


def _clean_warnings(values: Iterable[str]) -> tuple[str, ...]:
    warnings: list[str] = []
    for value in values:
        if not isinstance(value, str):
            continue
        text = " ".join(value.split())[:160]
        if text and text not in warnings:
            warnings.append(text)
    return tuple(warnings[:3])


def _market_breadth_stats(quotes: list) -> MarketBreadthStats:
    changes = [change for quote in quotes if (change := _valid_quote_change_pct(quote)) is not None]
    return MarketBreadthStats(
        sample_count=len(changes),
        changes=changes,
        up_count=sum(1 for change in changes if change > 0),
        down_count=sum(1 for change in changes if change < 0),
        strong_count=sum(1 for change in changes if change >= STRONG_CHANGE_PCT),
        weak_count=sum(1 for change in changes if change <= WEAK_CHANGE_PCT),
        avg_change_pct=sum(changes) / len(changes) if changes else 0,
    )


def _valid_quote_change_pct(quote) -> float | None:
    if safe_float(getattr(quote, "price", None), default=0) <= 0:
        return None
    change = safe_float(getattr(quote, "change_pct", None), default=math.nan)
    return change if math.isfinite(change) else None


def _market_breadth_score(stats: MarketBreadthStats) -> int:
    raw_score = (
        BASE_BREADTH_SCORE
        + (stats.up_ratio - 0.5) * UP_RATIO_SCORE_WEIGHT
        + stats.avg_change_pct * AVERAGE_CHANGE_SCORE_WEIGHT
        + stats.strong_weak_spread_ratio * STRONG_WEAK_SCORE_WEIGHT
    )
    return clamp_score(raw_score, round_value=True)


def _market_breadth_band(score: int) -> MarketBreadthBand:
    return next((band for band in MARKET_BREADTH_BANDS if band.matches(score)), NEUTRAL_BREADTH_BAND)


def _market_breadth_summary(label: str, stats: MarketBreadthStats) -> str:
    return f"{label}：样本 {stats.sample_count} 只，上涨 {stats.up_count}、下跌 {stats.down_count}，平均涨跌幅 {stats.avg_change_pct:.2f}%。"
