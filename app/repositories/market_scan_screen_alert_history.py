"""Read stored membership only; missing historical spec prevents digest replay."""

from __future__ import annotations

from dataclasses import dataclass
import sqlite3

from pydantic import TypeAdapter

from app.models.market_scan_screen_alert import (
    MarketScanScreenAlertChangeKind,
    MarketScanScreenAlertEventSummary,
    ScreenAlertSymbol,
)
from app.utils.symbols import standard_symbol


_SYMBOL_LIST = TypeAdapter(list[ScreenAlertSymbol])


@dataclass(frozen=True)
class StoredScreenAlertEvent:
    summary: MarketScanScreenAlertEventSummary
    memberships: tuple[tuple[MarketScanScreenAlertChangeKind, tuple[str, ...]], ...]


def stored_screen_alert_event(row: sqlite3.Row) -> StoredScreenAlertEvent:
    try:
        groups: tuple[tuple[MarketScanScreenAlertChangeKind, tuple[str, ...]], ...] = (
            ("entered", _stored_symbols(row["entered_symbols_json"])),
            ("exited", _stored_symbols(row["exited_symbols_json"])),
            ("unrankable", _stored_symbols(row["suppressed_unrankable_symbols_json"])),
        )
        symbols = tuple(symbol for _, group in groups for symbol in group)
        if len(set(symbols)) != len(symbols):
            raise ValueError("筛选变化事件股票集合重复或重叠")
        summary = MarketScanScreenAlertEventSummary(
            **{key: row[key] for key in ("id", "preset_id", "preset_revision", "current_run_id", "previous_run_id", "event_digest", "created_at")},
            entered_count=len(groups[0][1]), exited_count=len(groups[1][1]),
            suppressed_unrankable_count=len(groups[2][1]),
        )
        return StoredScreenAlertEvent(summary, groups)
    except (ValueError, TypeError, OverflowError) as exc:
        raise RuntimeError("筛选变化历史记录校验失败，已拒绝读取") from exc


def _stored_symbols(value: object) -> tuple[str, ...]:
    if not isinstance(value, str):
        raise ValueError("筛选变化事件成员存储无效")
    symbols = _SYMBOL_LIST.validate_json(value, strict=True)
    if any(standard_symbol(symbol) != symbol for symbol in symbols):
        raise ValueError("筛选变化事件股票代码不是规范格式")
    return tuple(sorted(symbols))
