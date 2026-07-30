from __future__ import annotations

from collections.abc import Sequence
import json
from typing import Any

from pydantic import BaseModel

from app.models.discovery import DiscoveryCriteria, DiscoverySort


_FIELD_COLUMNS = {
    "rank": "rank",
    "symbol": "symbol",
    "market": "market",
    "industry": "industry",
    "is_st": "is_st",
    "is_new": "is_new",
    "quality": "data_quality_score",
    "trend": "trend_score",
    "change": "change_pct",
    "turnover": "turnover_rate",
    "amount": "amount",
    "score": "score",
    "raw_score": "raw_score",
}


def discovery_filter_sql(criteria: DiscoveryCriteria, *, alias: str = "r") -> tuple[str, list[object]]:
    clauses = [f"{alias}.status = 'success'"]
    parameters: list[object] = []
    if criteria.market is not None:
        clauses.append(f"{alias}.market IN ({_placeholders(criteria.market)})")
        parameters.extend(criteria.market)
    if criteria.industry is not None:
        clauses.append(
            "(" + " OR ".join(
                f"{alias}.industry LIKE ? ESCAPE '\\'" for _industry in criteria.industry
            ) + ")"
        )
        parameters.extend(f"%{_escaped_like(industry)}%" for industry in criteria.industry)
    if criteria.is_st is not None:
        clauses.append(f"{alias}.is_st = ?")
        parameters.append(int(criteria.is_st))
    if criteria.is_new is not None:
        clauses.append(f"{alias}.is_new = ?")
        parameters.append(int(criteria.is_new))
    for field in ("quality", "trend", "change", "turnover", "amount", "score"):
        bounds = getattr(criteria, field)
        if bounds is None:
            continue
        column = f"{alias}.{_FIELD_COLUMNS[field]}"
        if bounds.min is not None:
            clauses.append(f"{column} >= ?")
            parameters.append(bounds.min)
        if bounds.max is not None:
            clauses.append(f"{column} <= ?")
            parameters.append(bounds.max)
    return " AND ".join(clauses), parameters


def discovery_order_sql(sort: list[DiscoverySort], *, alias: str = "r") -> str:
    parts: list[str] = []
    for item in sort:
        column = f"{alias}.{_FIELD_COLUMNS[item.field]}"
        direction = "ASC" if item.order == "asc" else "DESC"
        parts.extend((f"({column} IS NULL) ASC", f"{column} {direction}"))
    parts.append(f"{alias}.symbol ASC")
    return ", ".join(parts)


def canonical_model_json(value: BaseModel) -> str:
    return json.dumps(
        value.model_dump(mode="json"),
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    )


def canonical_json(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


def _placeholders(values: Sequence[object]) -> str:
    return ",".join("?" for _ in values)


def _escaped_like(value: str) -> str:
    return value.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")


__all__ = [
    "canonical_json",
    "canonical_model_json",
    "discovery_filter_sql",
    "discovery_order_sql",
]
