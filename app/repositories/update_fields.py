from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Callable


@dataclass(frozen=True)
class FieldUpdate:
    column: str
    value: Any


FieldCleaner = Callable[[Any], Any]


def present_updates(payload, cleaners: dict[str, FieldCleaner]) -> list[FieldUpdate]:
    raw_updates = payload.model_dump(exclude_unset=True)
    updates: list[FieldUpdate] = []
    for field, cleaner in cleaners.items():
        if field not in raw_updates:
            continue
        updates.append(FieldUpdate(column=field, value=cleaner(getattr(payload, field))))
    return updates


def update_sql_parts(updates: list[FieldUpdate]) -> tuple[list[str], list[Any]]:
    assignments = [f"{item.column} = ?" for item in updates]
    params = [item.value for item in updates]
    return assignments, params
