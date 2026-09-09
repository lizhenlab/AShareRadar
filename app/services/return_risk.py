"""Daily lower partial moment with an explicit zero-return target."""

from collections.abc import Sequence
import math


DOWNSIDE_DEVIATION_ALGORITHM = "daily-target-semideviation-zero-v1"


def downside_deviation_spec() -> dict[str, object]:
    return {
        "algorithm": DOWNSIDE_DEVIATION_ALGORITHM,
        "formula": "100 * sqrt(sum(min(simple_return - target, 0)^2) / observation_count)",
        "target": 0.0,
        "return_window": 20,
        "required_closes": 21,
        "denominator": "all-20-completed-session-returns-including-zero-and-positive",
        "units": "daily-percentage-points-not-annualized",
        "invalid_prices": "reject-without-dropping-observations",
    }


def downside_deviation_pct(closes: Sequence[float]) -> float:
    """Root second lower partial moment; gains still count in the denominator."""
    if len(closes) < 2:
        raise ValueError("下行偏差至少需要两个收盘价")
    if any(isinstance(value, bool) or not math.isfinite(value) or value <= 0 for value in closes):
        raise ValueError("下行偏差收盘价必须为有限正数")
    returns = [current / previous - 1 for previous, current in zip(closes[:-1], closes[1:], strict=True)]
    if any(not math.isfinite(value) for value in returns):
        raise ValueError("下行偏差收益必须有限")
    return 100 * math.sqrt(math.fsum(min(value, 0.0) ** 2 for value in returns) / len(returns))


__all__ = ["DOWNSIDE_DEVIATION_ALGORITHM", "downside_deviation_pct", "downside_deviation_spec"]
