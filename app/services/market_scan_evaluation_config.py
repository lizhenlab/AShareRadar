"""Validated, immutable parameters for offline market-scan evaluation."""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass
import math

from app.models.paper_trading import CostProfileName


DEFAULT_TOP_SIZES = (20, 50, 100)
DEFAULT_HORIZONS = (1, 3, 5, 10, 20)
DEFAULT_MINIMUM_SESSION_COUNT = 20
DEFAULT_MINIMUM_MULTIPLE_TEST_SESSION_COUNT = 40
DEFAULT_BOOTSTRAP_SAMPLES = 1_000
DEFAULT_EXECUTION_NOTIONAL = 100_000.0
DEFAULT_MAX_EXIT_DELAY_SESSIONS = 5
DEFAULT_MAX_DAILY_PARTICIPATION_RATE = 0.01


@dataclass(frozen=True)
class EvaluationConfig:
    top_sizes: tuple[int, ...] = DEFAULT_TOP_SIZES
    horizons: tuple[int, ...] = DEFAULT_HORIZONS
    minimum_sample_size: int = 30
    minimum_session_count: int = DEFAULT_MINIMUM_SESSION_COUNT
    complete_day_coverage: float = 0.95
    bootstrap_samples: int = DEFAULT_BOOTSTRAP_SAMPLES
    cost_profile: CostProfileName = "base"
    execution_notional: float = DEFAULT_EXECUTION_NOTIONAL
    max_exit_delay_sessions: int = DEFAULT_MAX_EXIT_DELAY_SESSIONS
    max_daily_participation_rate: float = DEFAULT_MAX_DAILY_PARTICIPATION_RATE
    hysteresis_buffer_ratio: float = 0.20

    def __post_init__(self) -> None:
        object.__setattr__(self, "top_sizes", _positive_sequence(self.top_sizes, "top_sizes"))
        object.__setattr__(self, "horizons", _positive_sequence(self.horizons, "horizons"))
        _integer(self.minimum_sample_size, 1, "minimum_sample_size")
        _integer(self.minimum_session_count, 1, "minimum_session_count")
        _integer(self.bootstrap_samples, 100, "bootstrap_samples")
        _integer(self.max_exit_delay_sessions, 0, "max_exit_delay_sessions")
        if _finite_number(self.execution_notional, "execution_notional") <= 0:
            raise ValueError("execution_notional 必须大于 0")
        _fraction(self.complete_day_coverage, "complete_day_coverage")
        _fraction(self.max_daily_participation_rate, "max_daily_participation_rate")
        _fraction(self.hysteresis_buffer_ratio, "hysteresis_buffer_ratio", allow_zero=True)
        if self.cost_profile not in ("base", "conservative", "stress"):
            raise ValueError("cost_profile 必须是已声明的成本档")


def _positive_sequence(value: object, name: str) -> tuple[int, ...]:
    if not isinstance(value, Sequence) or isinstance(value, (str, bytes)) or not value:
        raise ValueError(f"{name} 必须是非空正整数序列")
    parsed = tuple(_integer(item, 1, name) for item in value)
    if len(set(parsed)) != len(parsed):
        raise ValueError(f"{name} 不得包含重复值")
    return parsed


def _integer(value: object, minimum: int, name: str) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or value < minimum:
        raise ValueError(f"{name} 必须是大于等于 {minimum} 的整数")
    return value


def _finite_number(value: object, name: str) -> float:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise ValueError(f"{name} 必须是有限数值")
    try:
        parsed = float(value)
    except OverflowError as exc:
        raise ValueError(f"{name} 必须是有限数值") from exc
    if not math.isfinite(parsed):
        raise ValueError(f"{name} 必须是有限数值")
    return parsed


def _fraction(value: object, name: str, *, allow_zero: bool = False) -> None:
    parsed = _finite_number(value, name)
    if not 0 <= parsed <= 1 or (not allow_zero and parsed == 0):
        bounds = "[0, 1]" if allow_zero else "(0, 1]"
        raise ValueError(f"{name} 必须在 {bounds} 范围内")
