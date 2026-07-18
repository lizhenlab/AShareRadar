from __future__ import annotations

from typing import Any, cast


INVALID_TEXT_LITERALS = frozenset(
    {"nan", "none", "null", "inf", "+inf", "-inf", "infinity", "+infinity", "-infinity"}
)


def clean_optional_text(value: object) -> str | None:
    """Normalize display text while rejecting null-like and numeric values."""
    if value is None:
        return None
    if isinstance(value, str):
        text = " ".join(value.split())
    else:
        try:
            float(cast(Any, value))
        except (TypeError, ValueError):
            text = " ".join(str(value).split())
        else:
            return None
    return text if text and text.casefold() not in INVALID_TEXT_LITERALS else None


__all__ = ["clean_optional_text"]
