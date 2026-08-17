from __future__ import annotations

import json
from typing import Any

from pydantic import BaseModel

from app.market_scan_screening import screen_spec_from_discovery
from app.models.discovery import DiscoveryCriteria, DiscoverySort
from app.repositories.market_scan_screening_sql import screen_spec_filter_sql, screen_spec_order_sql


def discovery_filter_sql(criteria: DiscoveryCriteria, *, alias: str = "r") -> tuple[str, list[object]]:
    spec = screen_spec_from_discovery(criteria, [DiscoverySort(field="rank", order="asc")])
    return screen_spec_filter_sql(spec, alias=alias)


def discovery_order_sql(sort: list[DiscoverySort], *, alias: str = "r") -> str:
    spec = screen_spec_from_discovery(DiscoveryCriteria(), sort)
    return screen_spec_order_sql(spec, alias=alias)


def canonical_model_json(value: BaseModel) -> str:
    return json.dumps(
        value.model_dump(mode="json"),
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    )


def canonical_json(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


__all__ = [
    "canonical_json",
    "canonical_model_json",
    "discovery_filter_sql",
    "discovery_order_sql",
]
