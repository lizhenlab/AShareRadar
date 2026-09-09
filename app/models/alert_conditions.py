"""Pure alert condition contracts shared by API and persistence admission."""

from collections.abc import Callable
import math


def _validate_positive_price(threshold: float) -> None:
    if threshold <= 0:
        raise ValueError("价格预警阈值必须大于0。")


def _validate_trend_score(threshold: float) -> None:
    if not 0 <= threshold <= 100:
        raise ValueError("趋势评分预警阈值应在0到100之间。")


def _validate_change_pct(threshold: float) -> None:
    if not -100 <= threshold <= 100:
        raise ValueError("涨跌幅预警阈值应在-100%到100%之间。")


def _validate_dynamic_level(threshold: float) -> None:
    if threshold < 0:
        raise ValueError("支撑/压力预警阈值不能小于0；填0表示使用系统动态支撑/压力。")


_CONDITION_VALIDATORS: dict[str, Callable[[float], None]] = {
    "price_above": _validate_positive_price,
    "price_below": _validate_positive_price,
    "change_pct_above": _validate_change_pct,
    "change_pct_below": _validate_change_pct,
    "trend_score_above": _validate_trend_score,
    "trend_score_below": _validate_trend_score,
    "break_support": _validate_dynamic_level,
    "break_resistance": _validate_dynamic_level,
}


def validate_alert_condition(condition_type: str, threshold: float | None = None) -> None:
    validator = _CONDITION_VALIDATORS.get(condition_type)
    if validator is None:
        allowed = "、".join(sorted(_CONDITION_VALIDATORS))
        raise ValueError(f"不支持的预警条件：{condition_type}。可用条件：{allowed}")
    if threshold is not None:
        if not math.isfinite(threshold):
            raise ValueError("预警阈值必须是有效数字")
        validator(threshold)
