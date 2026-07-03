from __future__ import annotations

from collections.abc import Sequence

from app.services.scoring import bounded_int
from app.utils.market_data import finite_float


_MISSING_TEXTS = frozenset({
    "",
    "none",
    "null",
    "nan",
    "+nan",
    "-nan",
    "inf",
    "+inf",
    "-inf",
    "infinity",
    "+infinity",
    "-infinity",
})


def clean_text(value: object, *, fallback: str = "", max_length: int | None = None) -> str:
    if value is None:
        return fallback
    if isinstance(value, (int, float)) and not isinstance(value, bool) and finite_float(value) is None:
        return fallback
    text = " ".join(str(value).strip().split())
    if text.casefold() in _MISSING_TEXTS:
        return fallback
    if max_length is not None:
        text = text[: max(0, max_length)]
    return text


def clean_items(items: object) -> list[str]:
    seen: set[str] = set()
    result: list[str] = []
    for item in _iter_items(items):
        cleaned = clean_text(item)
        key = cleaned.casefold()
        if not cleaned or key in seen:
            continue
        seen.add(key)
        result.append(cleaned)
    return result


def dedupe(items: object) -> list[str]:
    return clean_items(items)


def first_clean_items(items: object, limit: int) -> list[str]:
    return clean_items(items)[: max(0, limit)]


def _iter_items(items: object) -> Sequence[object]:
    if items is None:
        return ()
    if isinstance(items, str):
        return (items,)
    if isinstance(items, Sequence):
        return items
    return (items,)


__all__ = ["bounded_int", "clean_items", "clean_text", "dedupe", "first_clean_items"]
