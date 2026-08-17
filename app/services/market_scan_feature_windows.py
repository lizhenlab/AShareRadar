"""Shared point-in-time price-window semantics for full-market scoring.

Completed snapshots (official and preopen) already contain the snapshot session
as their last daily bar.  Intraday snapshots end at the previous completed
session.  Keeping that one-bar distinction in one place prevents nominal
1/5/20/60-session returns from silently changing horizon across scan modes.
"""

from __future__ import annotations

import math
from collections.abc import Sequence

from app.models.market_scan import MarketScanMode


MARKET_SCAN_FEATURE_WINDOW_CONTRACT_VERSION = "market-scan-feature-windows-v1"
MARKET_SCAN_RETURN_HORIZONS: tuple[int, ...] = (1, 5, 20, 60)


def snapshot_reference_offset(mode: MarketScanMode, horizon: int) -> int:
    """Return the positive offset from the end for a snapshot return reference."""
    _require_mode(mode)
    _require_positive_window(horizon, "收益窗口")
    return horizon if mode == "intraday" else horizon + 1


def snapshot_return_pct(
    current: float,
    closes: Sequence[float],
    *,
    horizon: int,
    mode: MarketScanMode,
) -> float:
    """Compute a point-in-time return with an explicit scan-mode contract."""
    reference = _close_at_offset(
        closes,
        snapshot_reference_offset(mode, horizon),
        label=f"{horizon}日收益",
    )
    return _pct_change(current, reference)


def snapshot_skip_return_pct(
    closes: Sequence[float],
    *,
    skip_sessions: int,
    lookback_sessions: int,
    mode: MarketScanMode,
) -> float:
    """Return over completed bars ending ``skip_sessions`` before the snapshot."""
    _require_mode(mode)
    _require_positive_window(skip_sessions, "跳过窗口")
    _require_positive_window(lookback_sessions, "回看窗口")
    anchor_offset = skip_sessions if mode == "intraday" else skip_sessions + 1
    anchor = _close_at_offset(closes, anchor_offset, label="跳过窗口终点")
    reference = _close_at_offset(
        closes,
        anchor_offset + lookback_sessions,
        label="跳过窗口起点",
    )
    return _pct_change(anchor, reference)


def _close_at_offset(closes: Sequence[float], offset: int, *, label: str) -> float:
    if len(closes) < offset:
        raise ValueError(f"{label}至少需要 {offset} 根完整日K，当前 {len(closes)} 根")
    value = float(closes[-offset])
    if not math.isfinite(value) or value <= 0:
        raise ValueError(f"{label}参考收盘价无效")
    return value


def _pct_change(current: float, reference: float) -> float:
    current_value = float(current)
    if not math.isfinite(current_value) or current_value <= 0:
        raise ValueError("快照价格无效")
    return (current_value / reference - 1) * 100


def _require_mode(mode: object) -> None:
    if mode not in {"official", "intraday", "preopen"}:
        raise ValueError(f"未知全市场扫描模式：{mode!r}")


def _require_positive_window(value: int, label: str) -> None:
    if isinstance(value, bool) or not isinstance(value, int) or value <= 0:
        raise ValueError(f"{label}必须是正整数")


__all__ = [
    "MARKET_SCAN_FEATURE_WINDOW_CONTRACT_VERSION",
    "MARKET_SCAN_RETURN_HORIZONS",
    "snapshot_reference_offset",
    "snapshot_return_pct",
    "snapshot_skip_return_pct",
]
