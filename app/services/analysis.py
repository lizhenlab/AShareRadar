from __future__ import annotations

from dataclasses import dataclass

from app.models.analysis import (
    ActionAdvice,
    AnalysisResult,
    DataQuality,
    IndividualReview,
    PeerSampleInfo,
    SignalContribution,
    SignalItem,
    SignalSnapshot,
)
from app.models.market import (
    Kline,
    PlateItem,
    Quote,
    StockInfo,
)
from app.models.market_scan import MarketScanMode
from app.services.analysis_signal_advice import action_advice, beginner_summary
from app.services.analysis_signal_points import buy_points, risk_level, sell_points, strength_tags, t_plan
from app.services.analysis_signal_quality import gate_signal_items
from app.services.analysis_signal_snapshot import signal_snapshot
from app.services.data_quality import build_data_quality
from app.services.indicators import moving_average, support_resistance, trend_score_snapshot
from app.services.strong_stocks import build_strong_stock_watch
from app.utils.market_data import filter_valid_klines


__all__ = ["build_analysis", "build_strong_stock_watch"]
BUY_SIGNAL_KIND = "buy"
SELL_SIGNAL_KIND = "sell"
T_SIGNAL_KIND = "t"


@dataclass(frozen=True)
class TrendMetrics:
    score: int
    label: str
    contributions: list[SignalContribution]
    support: float
    resistance: float
    ma5: float
    ma10: float
    ma20: float
    support_available: bool
    resistance_available: bool
    ma20_available: bool


@dataclass(frozen=True)
class AnalysisSignals:
    risk_level: str
    action_advice: ActionAdvice
    signal_snapshot: SignalSnapshot
    beginner_summary: str
    buy_points: list[SignalItem]
    sell_points: list[SignalItem]
    t_plan: list[SignalItem]
    strength_tags: list[str]


@dataclass(frozen=True)
class AnalysisSignalPoints:
    buy: list[SignalItem]
    sell: list[SignalItem]
    t: list[SignalItem]


def build_analysis(
    quote: Quote,
    klines: list[Kline],
    stock_profile: StockInfo | None = None,
    industry_context: PlateItem | None = None,
    review: IndividualReview | None = None,
    data_quality: DataQuality | None = None,
    quote_history: list[dict[str, float | str | None]] | None = None,
    peer_quotes: list[Quote] | None = None,
    peer_sample: PeerSampleInfo | None = None,
    *,
    mode: MarketScanMode = "official",
) -> AnalysisResult:
    valid_klines = filter_valid_klines(klines)
    metrics = _trend_metrics(quote, valid_klines, mode=mode)
    quality = data_quality or build_data_quality(quote, klines)
    signals = _analysis_signals(quote, metrics, quality, stock_profile, industry_context)
    return _analysis_result(
        quote=quote,
        klines=valid_klines,
        metrics=metrics,
        quality=quality,
        signals=signals,
        stock_profile=stock_profile,
        industry_context=industry_context,
        review=review,
        quote_history=quote_history,
        peer_quotes=peer_quotes,
        peer_sample=peer_sample,
        mode=mode,
    )


def _trend_metrics(
    quote: Quote,
    klines: list[Kline],
    *,
    mode: MarketScanMode,
) -> TrendMetrics:
    score, label, contributions = trend_score_snapshot(quote, klines, mode=mode)
    ma5 = moving_average(klines, 5)
    ma10 = moving_average(klines, 10)
    ma20 = moving_average(klines, 20)
    support, resistance = support_resistance(klines, current_price=quote.price)
    structural_history_available = len(klines) >= 20
    return TrendMetrics(
        score=score,
        label=label,
        contributions=contributions,
        support=support,
        resistance=resistance,
        ma5=ma5,
        ma10=ma10,
        ma20=ma20,
        support_available=structural_history_available and support > 0,
        resistance_available=structural_history_available and resistance > 0,
        ma20_available=structural_history_available and ma20 > 0,
    )


def _analysis_signals(
    quote: Quote,
    metrics: TrendMetrics,
    quality: DataQuality,
    stock_profile: StockInfo | None,
    industry_context: PlateItem | None,
) -> AnalysisSignals:
    support = metrics.support if metrics.support_available else 0.0
    resistance = metrics.resistance if metrics.resistance_available else 0.0
    risk_level_value = risk_level(quote, metrics.score, support, quality)
    action_advice_item = _analysis_action_advice(quote, metrics, quality, risk_level_value)
    point_sets = _analysis_signal_points(quote, metrics, quality)
    return AnalysisSignals(
        risk_level=risk_level_value,
        action_advice=action_advice_item,
        signal_snapshot=signal_snapshot(metrics.score, metrics.label, metrics.contributions, quality, risk_level_value),
        beginner_summary=beginner_summary(
            quote,
            metrics.score,
            metrics.label,
            risk_level_value,
            support,
            resistance,
            stock_profile,
            industry_context,
            action_advice_item,
        ),
        buy_points=point_sets.buy,
        sell_points=point_sets.sell,
        t_plan=point_sets.t,
        strength_tags=strength_tags(quote, metrics.score),
    )


def _analysis_action_advice(
    quote: Quote,
    metrics: TrendMetrics,
    quality: DataQuality,
    risk_level_value: str,
) -> ActionAdvice:
    return action_advice(
        quote,
        metrics.score,
        risk_level_value,
        metrics.support if metrics.support_available else 0.0,
        metrics.resistance if metrics.resistance_available else 0.0,
        quality,
    )


def _analysis_signal_points(quote: Quote, metrics: TrendMetrics, quality: DataQuality) -> AnalysisSignalPoints:
    support = metrics.support if metrics.support_available else 0.0
    resistance = metrics.resistance if metrics.resistance_available else 0.0
    ma20 = metrics.ma20 if metrics.ma20_available else 0.0
    structure_available = metrics.support_available and metrics.resistance_available
    t_items = t_plan(quote, support, resistance) if structure_available else [_unavailable_t_plan_item()]
    return AnalysisSignalPoints(
        buy=gate_signal_items(
            buy_points(quote, metrics.score, metrics.ma5, metrics.ma10, support, resistance),
            quality,
            BUY_SIGNAL_KIND,
        ),
        sell=gate_signal_items(
            sell_points(quote, metrics.score, metrics.ma5, ma20, support, resistance),
            quality,
            SELL_SIGNAL_KIND,
        ),
        t=gate_signal_items(t_items, quality, T_SIGNAL_KIND) if structure_available else t_items,
    )


def _unavailable_t_plan_item() -> SignalItem:
    return SignalItem(
        title="做T区间待确认",
        level="观察",
        reason="有效日K不足20根，支撑与压力区间不可用，不给出结构动作价位。",
    )


def _analysis_result(
    *,
    quote: Quote,
    klines: list[Kline],
    metrics: TrendMetrics,
    quality: DataQuality,
    signals: AnalysisSignals,
    stock_profile: StockInfo | None,
    industry_context: PlateItem | None,
    review: IndividualReview | None,
    quote_history: list[dict[str, float | str | None]] | None,
    peer_quotes: list[Quote] | None,
    peer_sample: PeerSampleInfo | None,
    mode: MarketScanMode,
) -> AnalysisResult:
    return AnalysisResult(
        quote=quote,
        research_mode=mode,
        stock_profile=stock_profile,
        industry_context=industry_context,
        action_advice=signals.action_advice,
        data_quality=quality,
        signal_snapshot=signals.signal_snapshot,
        review=review,
        trend_score=metrics.score,
        trend_label=metrics.label,
        support=metrics.support,
        resistance=metrics.resistance,
        support_available=metrics.support_available,
        resistance_available=metrics.resistance_available,
        ma5=metrics.ma5,
        ma10=metrics.ma10,
        ma20=metrics.ma20,
        ma20_available=metrics.ma20_available,
        risk_level=signals.risk_level,
        beginner_summary=signals.beginner_summary,
        buy_points=signals.buy_points,
        sell_points=signals.sell_points,
        t_plan=signals.t_plan,
        strength_tags=signals.strength_tags,
        klines=klines,
        quote_history=_list_or_empty(quote_history),
        peer_quotes=_list_or_empty(peer_quotes),
        peer_sample=peer_sample or _inferred_peer_sample(peer_quotes),
    )


def _inferred_peer_sample(peer_quotes: list[Quote] | None) -> PeerSampleInfo:
    count = len(peer_quotes or [])
    return PeerSampleInfo(status="available" if count else "not_requested", requested_count=count)


def _list_or_empty(values: list | None) -> list:
    return list(values or [])
