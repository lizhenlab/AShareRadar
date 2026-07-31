from __future__ import annotations

from app.models.execution import MODELLED_ROUND_TRIP_FRICTION_PCT
from app.services.indicators import pct_change
from app.utils.market_data import finite_float, valid_kline

def next_session_open(rows: list, signal_index: int) -> float | None:
    entry_index = signal_index + 1
    if entry_index >= len(rows):
        return None
    entry_row = rows[entry_index]
    open_price = finite_float(getattr(entry_row, "open", None))
    volume = finite_float(getattr(entry_row, "volume", None))
    if not valid_kline(entry_row) or open_price is None or open_price <= 0 or volume is None or volume <= 0:
        return None
    return open_price


def net_forward_return(exit_price: float, entry_price: float) -> float:
    return pct_change(exit_price, entry_price) - MODELLED_ROUND_TRIP_FRICTION_PCT


__all__ = [
    "MODELLED_ROUND_TRIP_FRICTION_PCT",
    "net_forward_return",
    "next_session_open",
]
