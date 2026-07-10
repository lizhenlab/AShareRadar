from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass

from app.models.schemas import AnalysisResult, FactorLabReport, FeatureSnapshot, Kline, TimeframeAlignmentReport, TimeframeTrend
from app.services.indicators import max_drawdown, pct_change
from app.services.scoring import clamp_score as _clamp, score_level as _score_level


@dataclass(frozen=True)
class TimeframeMetrics:
    row_count: int
    latest: float
    return_pct: float
    ma_window: int
    ma_value: float
    drawdown: float
    above_ma: bool


@dataclass(frozen=True)
class MetricScoreRule:
    name: str
    applies: Callable[[TimeframeMetrics], bool]
    delta: int


@dataclass(frozen=True)
class TimeframeConflictSnapshot:
    scores: tuple[int, ...]
    spread: int


@dataclass(frozen=True)
class TimeframeConflictRule:
    level: str
    applies: Callable[[TimeframeConflictSnapshot], bool]


@dataclass(frozen=True)
class AlignmentLabelRule:
    label: str
    applies: Callable[[int, str], bool]


RETURN_SCORE_RULES: tuple[MetricScoreRule, ...] = (
    MetricScoreRule("strong_return", lambda metrics: metrics.return_pct > 5, 12),
    MetricScoreRule("positive_return", lambda metrics: metrics.return_pct > 1, 6),
    MetricScoreRule("large_loss", lambda metrics: metrics.return_pct < -5, -10),
    MetricScoreRule("small_loss", lambda metrics: metrics.return_pct < -1, -4),
)

DRAWDOWN_SCORE_RULES: tuple[MetricScoreRule, ...] = (
    MetricScoreRule("large_drawdown", lambda metrics: metrics.drawdown < -12, -8),
    MetricScoreRule("medium_drawdown", lambda metrics: metrics.drawdown < -7, -4),
)

TIMEFRAME_CONFLICT_RULES: tuple[TimeframeConflictRule, ...] = (
    TimeframeConflictRule("多周期顺向", lambda snapshot: all(score >= 55 for score in snapshot.scores)),
    TimeframeConflictRule("多周期偏弱", lambda snapshot: all(score <= 48 for score in snapshot.scores)),
    TimeframeConflictRule("高冲突", lambda snapshot: snapshot.spread >= 35),
    TimeframeConflictRule("中冲突", lambda snapshot: any(score >= 62 for score in snapshot.scores) and any(score <= 45 for score in snapshot.scores)),
)

ALIGNMENT_LABEL_RULES: tuple[AlignmentLabelRule, ...] = (
    AlignmentLabelRule("周期仍需确认", lambda score, conflict: conflict == "待确认"),
    AlignmentLabelRule("周期冲突明显", lambda score, conflict: conflict == "高冲突"),
    AlignmentLabelRule("多周期偏弱", lambda score, conflict: conflict == "多周期偏弱" or score <= 45),
    AlignmentLabelRule("多周期共振", lambda score, conflict: conflict == "多周期顺向" and score >= 65),
    AlignmentLabelRule("多周期顺向", lambda score, conflict: conflict == "多周期顺向"),
)


def build_timeframe_alignment_report(
    analysis: AnalysisResult,
    feature: FeatureSnapshot,
    factor_lab: FactorLabReport,
) -> TimeframeAlignmentReport:
    frames = [
        _timeframe_trend(analysis, feature, "短线", 20),
        _timeframe_trend(analysis, feature, "波段", 60),
        _timeframe_trend(analysis, feature, "中期", 120),
    ]
    valid_frames = [item for item in frames if item.window_days <= len(analysis.klines)]
    alignment_score = _timeframe_alignment_score(valid_frames, factor_lab)
    conflict_level = _timeframe_conflict_level(valid_frames)
    alignment_label = _timeframe_alignment_label(alignment_score, conflict_level)
    return TimeframeAlignmentReport(
        symbol=feature.symbol,
        updated_at=feature.updated_at,
        alignment_score=alignment_score,
        alignment_label=alignment_label,
        conflict_level=conflict_level,
        summary=_timeframe_summary(alignment_label, conflict_level, valid_frames),
        timeframes=valid_frames,
        suggestions=_timeframe_suggestions(valid_frames, conflict_level),
    )


def _timeframe_trend(
    analysis: AnalysisResult,
    feature: FeatureSnapshot,
    name: str,
    window_days: int,
) -> TimeframeTrend:
    rows = _timeframe_rows(analysis, window_days)
    if len(rows) < 5:
        return _insufficient_timeframe(name, window_days)
    metrics = _timeframe_metrics(rows, feature.price)
    score = _timeframe_score(name, metrics, feature.trend_score)
    return TimeframeTrend(
        name=name,
        window_days=window_days,
        score=score,
        label=_score_level(score),
        return_pct=round(metrics.return_pct, 2),
        max_drawdown_pct=round(metrics.drawdown, 2),
        above_ma=metrics.above_ma,
        ma_value=round(metrics.ma_value, 2),
        evidence=_timeframe_evidence(metrics),
    )


def _timeframe_alignment_score(frames: list[TimeframeTrend], factor_lab: FactorLabReport) -> int:
    if not frames:
        return 50
    weights = {"短线": 0.45, "波段": 0.35, "中期": 0.2}
    total_weight = sum(weights.get(item.name, 0.25) for item in frames) or 1
    raw = sum(item.score * weights.get(item.name, 0.25) for item in frames) / total_weight
    if factor_lab.total_score >= 60:
        raw += 4
    if factor_lab.total_score <= 45:
        raw -= 5
    return _clamp(round(raw))


def _timeframe_rows(analysis: AnalysisResult, window_days: int) -> list[Kline]:
    if window_days <= 0:
        return []
    return analysis.klines[-window_days:] if len(analysis.klines) >= window_days else analysis.klines[:]


def _insufficient_timeframe(name: str, window_days: int) -> TimeframeTrend:
    return TimeframeTrend(
        name=name,
        window_days=window_days,
        score=50,
        label="样本不足",
        return_pct=0,
        max_drawdown_pct=0,
        above_ma=False,
        ma_value=0,
        evidence=["K线样本不足，暂按中性处理。"],
    )


def _timeframe_metrics(rows: list[Kline], latest: float) -> TimeframeMetrics:
    ma_window = min(20, len(rows))
    ma_value = sum(item.close for item in rows[-ma_window:]) / ma_window
    closes = [item.close for item in rows]
    effective_latest = _effective_latest_price(rows, latest)
    return TimeframeMetrics(
        row_count=len(rows),
        latest=effective_latest,
        return_pct=pct_change(effective_latest, rows[0].close),
        ma_window=ma_window,
        ma_value=ma_value,
        drawdown=max_drawdown(closes) if closes else 0,
        above_ma=effective_latest >= ma_value,
    )


def _effective_latest_price(rows: list[Kline], latest: float) -> float:
    return latest if latest > 0 else rows[-1].close


def _timeframe_score(name: str, metrics: TimeframeMetrics, feature_trend_score: int) -> int:
    score = 50 + _ma_position_delta(metrics) + _metric_rule_delta(metrics, RETURN_SCORE_RULES) + _metric_rule_delta(metrics, DRAWDOWN_SCORE_RULES, default=3)
    if name == "短线":
        score = round(score * 0.55 + feature_trend_score * 0.45)
    return _clamp(score)


def _ma_position_delta(metrics: TimeframeMetrics) -> int:
    return 16 if metrics.above_ma else -14


def _metric_rule_delta(metrics: TimeframeMetrics, rules: tuple[MetricScoreRule, ...], default: int = 0) -> int:
    for rule in rules:
        if rule.applies(metrics):
            return rule.delta
    return default


def _timeframe_evidence(metrics: TimeframeMetrics) -> list[str]:
    return [
        f"区间涨跌幅 {metrics.return_pct:.2f}%。",
        f"现价 {'高于' if metrics.above_ma else '低于'} {metrics.ma_window}日均线 {metrics.ma_value:.2f}。",
        f"区间最大回撤 {metrics.drawdown:.2f}%。",
    ]


def _timeframe_conflict_level(frames: list[TimeframeTrend]) -> str:
    snapshot = _timeframe_conflict_snapshot(frames)
    if snapshot is None:
        return "待确认"
    return next((rule.level for rule in TIMEFRAME_CONFLICT_RULES if rule.applies(snapshot)), "轻微分歧")


def _timeframe_conflict_snapshot(frames: list[TimeframeTrend]) -> TimeframeConflictSnapshot | None:
    scores = tuple(item.score for item in frames)
    if len(scores) < 2:
        return None
    return TimeframeConflictSnapshot(scores=scores, spread=max(scores) - min(scores))


def _timeframe_alignment_label(score: int, conflict_level: str) -> str:
    return next((rule.label for rule in ALIGNMENT_LABEL_RULES if rule.applies(score, conflict_level)), "周期仍需确认")


def _timeframe_summary(label: str, conflict_level: str, frames: list[TimeframeTrend]) -> str:
    frame_text = "；".join(f"{item.name}{item.score}分/{item.label}" for item in frames) or "暂无达到样本要求的周期"
    return f"{label}：{frame_text}。冲突级别为「{conflict_level}」。"


def _timeframe_suggestions(frames: list[TimeframeTrend], conflict_level: str) -> list[str]:
    suggestions: list[str] = []
    weak_frames = [item.name for item in frames if item.score <= 45]
    strong_frames = [item.name for item in frames if item.score >= 62]
    if conflict_level in {"高冲突", "中冲突"}:
        suggestions.append("周期冲突时降低信号级别，先等弱周期修复。")
    if weak_frames:
        suggestions.append(f"重点观察{'、'.join(weak_frames)}周期能否重新站回均线。")
    if strong_frames:
        suggestions.append(f"{'、'.join(strong_frames)}周期相对占优，可作为确认后的辅助支撑。")
    if not suggestions:
        suggestions.append("多周期没有明显共振，继续按支撑、压力和量能等待确认。")
    return suggestions[:4]
