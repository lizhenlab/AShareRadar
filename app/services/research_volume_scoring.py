"""One volume-confirmation rule for current and historical factor scores."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Callable

from app.services.scoring import clamp_score


@dataclass(frozen=True)
class VolumeConfirmationContext:
    ratio: float
    change_pct: float


@dataclass(frozen=True)
class VolumeConfirmationRule:
    name: str
    adjustment: Callable[[VolumeConfirmationContext], int]
    matches: Callable[[VolumeConfirmationContext], bool]


VOLUME_CONFIRMATION_BASE_SCORE = 52


def volume_confirmation_score(change_pct: float, volume_ratio: float) -> int:
    """Score observed inputs; callers establish availability and ratio precision."""
    context = VolumeConfirmationContext(ratio=volume_ratio, change_pct=change_pct)
    adjustment = next(
        (rule.adjustment(context) for rule in VOLUME_CONFIRMATION_RULES if rule.matches(context)),
        0,
    )
    return clamp_score(VOLUME_CONFIRMATION_BASE_SCORE + adjustment)


def _positive_volume_adjustment(context: VolumeConfirmationContext) -> int:
    return 18 + _volume_expansion_bonus(context.ratio)


def _negative_volume_adjustment(context: VolumeConfirmationContext) -> int:
    return -18 - _volume_expansion_bonus(context.ratio)


def _volume_expansion_bonus(ratio: float) -> int:
    return round(min(10, (ratio - 1.2) * 8))


VOLUME_CONFIRMATION_RULES = (
    VolumeConfirmationRule("positive_volume_expansion", _positive_volume_adjustment, lambda context: context.change_pct > 0 and context.ratio >= 1.2),
    VolumeConfirmationRule("negative_volume_expansion", _negative_volume_adjustment, lambda context: context.change_pct < 0 and context.ratio >= 1.2),
    VolumeConfirmationRule("low_volume_large_move", lambda context: -8, lambda context: context.ratio < 0.7 and abs(context.change_pct) >= 2),
    VolumeConfirmationRule("normal_volume", lambda context: 4, lambda context: 0.85 <= context.ratio <= 1.25),
)


__all__ = ["VOLUME_CONFIRMATION_RULES", "volume_confirmation_score"]
