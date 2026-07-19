from __future__ import annotations

import sys


_SAFE_FAILURE_MARKERS = (
    "database is locked",
    "database table is locked",
    "database or disk is full",
    "readonly database",
    "disk i/o error",
    "unable to open database file",
)


def report_persistence_failure(context: str, exc: BaseException) -> None:
    normalized = " ".join(str(exc).lower().split())
    category = next((marker for marker in _SAFE_FAILURE_MARKERS if marker in normalized), "unclassified persistence error")
    print(f"{context}: {type(exc).__name__}: {category}", file=sys.stderr)


__all__ = ["report_persistence_failure"]
