"""Shared deterministic execution-model constants."""

# A stable research assumption covering basic A-share statutory fees plus a
# small execution allowance, not an exact broker commission or slippage model.
MODELLED_ROUND_TRIP_FRICTION_PCT = 0.10


__all__ = ["MODELLED_ROUND_TRIP_FRICTION_PCT"]
