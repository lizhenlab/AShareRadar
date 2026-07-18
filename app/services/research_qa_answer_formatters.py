from __future__ import annotations

from app.services.research_qa_utils import bounded_int, clean_items, clean_text
from app.utils.market_data import finite_float


def _answer_items(
    items: object,
    *,
    limit: int,
    fallback: object,
    empty_fallback: object,
) -> list[str]:
    cleaned = _clean_answer_items(items)
    if cleaned:
        return cleaned[: max(0, limit)]
    fallback_items = _clean_answer_items(fallback)
    empty_fallback_items = _clean_answer_items(empty_fallback)
    return (fallback_items or empty_fallback_items)[: max(0, limit)]


def _clean_answer_items(items: object) -> list[str]:
    return clean_items(items)


def _clean_answer_item(item: object) -> str:
    return clean_text(item)


def _first_clean_items(items: object, limit: int) -> list[str]:
    return _clean_answer_items(items)[: max(0, limit)]


def _display_text(value: object, *, fallback: str = "待确认") -> str:
    return _clean_answer_item(value) or fallback


def _clean_topic(topic: object) -> str:
    return _clean_answer_item(topic)


def _format_number(value: object, *, suffix: str = "", fallback: str = "待确认", precision: int = 2) -> str:
    parsed = finite_float(value)
    if parsed is None:
        return fallback
    return f"{parsed:.{precision}f}{suffix}"


def _format_score(value: object) -> str:
    parsed = finite_float(value)
    if parsed is None:
        return "待确认"
    return str(bounded_int(parsed, 0, 100, round_value=True))
