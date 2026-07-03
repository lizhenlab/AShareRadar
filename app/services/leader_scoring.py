from __future__ import annotations

from dataclasses import dataclass
from typing import Callable

from app.services.scoring import clamp_score


@dataclass(frozen=True)
class LeaderScoreInput:
    trend_score: int
    change_pct: float
    volume_ratio: float
    amount: float
    turnover_rate: float | None = None
    fund_flow_score: int | None = None
    industry_change_pct: float | None = None
    abnormal_level: str | None = None
    data_quality_score: int | None = None


@dataclass(frozen=True)
class LeaderScoreProfile:
    base: int
    trend_weight: float
    rules: tuple["LeaderScoreRule", ...]


@dataclass(frozen=True)
class LeaderScoreRule:
    name: str
    delta: Callable[[LeaderScoreInput], int]


@dataclass(frozen=True)
class LeaderTagRule:
    label: str
    applies: Callable[[LeaderScoreInput, int], bool]


def leader_score(inputs: LeaderScoreInput, profile: LeaderScoreProfile) -> int:
    score = profile.base + round((inputs.trend_score - 50) * profile.trend_weight)
    for rule in profile.rules:
        score += rule.delta(inputs)
    return clamp_score(score)


def leader_tags(inputs: LeaderScoreInput, score: int, rules: tuple[LeaderTagRule, ...], fallback: str) -> list[str]:
    tags = [rule.label for rule in rules if rule.applies(inputs, score)]
    return tags or [fallback]


def _change_delta(strong: int, mid: int, weak: int) -> LeaderScoreRule:
    return LeaderScoreRule(
        "change_pct",
        lambda row: _high_low_delta(row.change_pct, high_steps=((5, strong), (2, mid)), low_steps=((-3, weak),)),
    )


def _volume_delta(threshold: float, positive: int, negative: int) -> LeaderScoreRule:
    return LeaderScoreRule("volume_ratio", lambda row: _signed_volume_delta(row, threshold, positive, negative))


def _amount_delta(large: int, mid: int, small: int = 0) -> LeaderScoreRule:
    return LeaderScoreRule(
        "amount",
        lambda row: _high_delta(row.amount, ((1_000_000_000, large), (300_000_000, mid)), default=small),
    )


def _turnover_delta(active: int, overheated: int = 0) -> LeaderScoreRule:
    return LeaderScoreRule("turnover", lambda row: _turnover_score_delta(row.turnover_rate, active, overheated))


def _fund_flow_delta(weight: float) -> LeaderScoreRule:
    return LeaderScoreRule("fund_flow", lambda row: round(((row.fund_flow_score or 50) - 50) * weight))


def _industry_delta(threshold: float, bonus: int) -> LeaderScoreRule:
    return LeaderScoreRule("industry", lambda row: bonus if row.industry_change_pct is not None and row.industry_change_pct > threshold else 0)


def _abnormal_risk_delta(penalty: int) -> LeaderScoreRule:
    return LeaderScoreRule("abnormal_risk", lambda row: penalty if row.abnormal_level == "风险" else 0)


def _data_quality_delta(threshold: int, penalty: int) -> LeaderScoreRule:
    return LeaderScoreRule("data_quality", lambda row: penalty if row.data_quality_score is not None and row.data_quality_score < threshold else 0)


def _high_low_delta(value: float, *, high_steps: tuple[tuple[float, int], ...], low_steps: tuple[tuple[float, int], ...]) -> int:
    high_delta = _high_delta(value, high_steps)
    if high_delta:
        return high_delta
    return _low_delta(value, low_steps)


def _high_delta(value: float, steps: tuple[tuple[float, int], ...], default: int = 0) -> int:
    for threshold, delta in steps:
        if value >= threshold:
            return delta
    return default


def _low_delta(value: float, steps: tuple[tuple[float, int], ...]) -> int:
    for threshold, delta in steps:
        if value <= threshold:
            return delta
    return 0


def _signed_volume_delta(row: LeaderScoreInput, threshold: float, positive: int, negative: int) -> int:
    if row.volume_ratio < threshold:
        return 0
    return positive if row.change_pct > 0 else negative if row.change_pct < 0 else 0


def _turnover_score_delta(turnover_rate: float | None, active: int, overheated: int) -> int:
    if not turnover_rate:
        return 0
    if 2 <= turnover_rate <= 10:
        return active
    if turnover_rate > 15:
        return overheated
    return 0


FEATURE_LEADER_PROFILE = LeaderScoreProfile(
    base=40,
    trend_weight=0.45,
    rules=(
        _change_delta(strong=14, mid=8, weak=-6),
        _volume_delta(threshold=1.5, positive=10, negative=-8),
        _amount_delta(large=8, mid=3, small=-4),
        _fund_flow_delta(weight=0.2),
        _industry_delta(threshold=1, bonus=6),
        _abnormal_risk_delta(penalty=-12),
        _data_quality_delta(threshold=70, penalty=-10),
    ),
)

STRONG_STOCK_LEADER_PROFILE = LeaderScoreProfile(
    base=38,
    trend_weight=0.48,
    rules=(
        _change_delta(strong=10, mid=5, weak=-5),
        _volume_delta(threshold=1.4, positive=8, negative=-6),
        _turnover_delta(active=6, overheated=-4),
        _amount_delta(large=8, mid=3),
    ),
)

FEATURE_TAG_RULES: tuple[LeaderTagRule, ...] = (
    LeaderTagRule("龙头候选", lambda _row, score: score >= 70),
    LeaderTagRule("趋势强", lambda row, _score: row.trend_score >= 70),
    LeaderTagRule("情绪强", lambda row, _score: row.change_pct >= 5),
    LeaderTagRule("量能放大", lambda row, _score: row.volume_ratio >= 1.5),
    LeaderTagRule("换手活跃", lambda row, _score: bool(row.turnover_rate and row.turnover_rate >= 8)),
    LeaderTagRule("资金配合", lambda row, _score: bool(row.fund_flow_score is not None and row.fund_flow_score >= 65)),
    LeaderTagRule("风险异动", lambda row, _score: row.abnormal_level == "风险"),
    LeaderTagRule("数据降权", lambda row, _score: bool(row.data_quality_score is not None and row.data_quality_score < 70)),
)

STRONG_STOCK_TAG_RULES: tuple[LeaderTagRule, ...] = (
    LeaderTagRule("龙头候选", lambda _row, score: score >= 70),
    LeaderTagRule("趋势强", lambda row, _score: row.trend_score >= 75),
    LeaderTagRule("涨幅强", lambda row, _score: row.change_pct >= 5),
    LeaderTagRule("量能放大", lambda row, _score: row.volume_ratio >= 1.4),
    LeaderTagRule("换手活跃", lambda row, _score: bool(row.turnover_rate and row.turnover_rate >= 6)),
)


__all__ = [
    "FEATURE_LEADER_PROFILE",
    "FEATURE_TAG_RULES",
    "LeaderScoreInput",
    "LeaderScoreProfile",
    "LeaderScoreRule",
    "LeaderTagRule",
    "STRONG_STOCK_LEADER_PROFILE",
    "STRONG_STOCK_TAG_RULES",
    "leader_score",
    "leader_tags",
]
