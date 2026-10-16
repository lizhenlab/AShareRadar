"""Resolve membership in the same read snapshot as the frozen result query."""
from __future__ import annotations

from collections.abc import Callable, Sequence
import sqlite3
from app.artifacts.io import canonical_json_text, sha256_hex
from app.models.market_scan_selection import MarketScanSelectionRequest, MarketScanSelectionScope


class MarketScanSelectionConflict(ValueError):
    """The caller must explicitly refresh membership before continuing."""


def watchlist_symbols(conn: sqlite3.Connection) -> tuple[str, ...]:
    return tuple(row[0] for row in conn.execute("SELECT symbol FROM watchlist ORDER BY symbol"))


def resolve_selection(
    request: MarketScanSelectionRequest,
    reader: Callable[[], tuple[str, ...]],
    *, require_digest: bool = False,
) -> tuple[MarketScanSelectionScope | None, tuple[str, ...] | None]:
    if request.universe == "all":
        return None, None
    symbols = tuple(sorted(set(reader())))
    digest = sha256_hex(canonical_json_text({"schema": "current-watchlist-v1", "symbols": list(symbols)}))
    if (require_digest and request.watchlist_digest is None) or (
        request.watchlist_digest is not None and request.watchlist_digest != digest
    ):
        raise MarketScanSelectionConflict("自选成员已变化或未绑定，请点击刷新自选范围后重试")
    return MarketScanSelectionScope(watchlist_digest=digest, member_count=len(symbols)), symbols


def intersect_selection(
    symbols: Sequence[str] | None, members: tuple[str, ...] | None,
) -> tuple[str, ...] | None:
    if members is None:
        return tuple(symbols) if symbols is not None else None
    return members if symbols is None else tuple(sorted(set(symbols).intersection(members)))
