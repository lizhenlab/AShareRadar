from __future__ import annotations

import math
from typing import Any

from app.models.schemas import PlateItem, StockConceptItem, StockInfo
from app.services.datahub_cache import _normalize_stock_concepts
from app.services.datahub_metadata_provider import _non_empty_metadata_rows
from app.services.datahub_runtime import ProviderAttempt, ProviderCoverageMiss


def _match_stock_pool_keyword(rows: list[StockInfo], keyword: str) -> list[StockInfo]:
    keyword_lower = keyword.lower()
    return [item for item in rows if keyword_lower in item.code.lower() or keyword_lower in item.name.lower() or keyword_lower in item.symbol.lower()]


def _stock_profile_match(rows: list[StockInfo], target: str) -> StockInfo | None:
    return next((item for item in rows if item.symbol == target), None)


def _profile_with_local_industry(
    profile: StockInfo | None,
    local_profile: StockInfo | None,
    *,
    allow_local_only: bool = False,
) -> StockInfo | None:
    if profile is None and allow_local_only:
        return local_profile
    if profile and local_profile and not profile.industry:
        return profile.model_copy(update={"industry": local_profile.industry})
    return profile


def _stock_profile_resolution_allows_local_only(reason: str) -> bool:
    return reason not in {
        "fresh-authoritative-empty",
        "provider-authoritative-empty",
        "stale-authoritative-empty",
    }


def _prepare_plate_rows(rows: list[PlateItem], limit: int) -> list[PlateItem]:
    _non_empty_metadata_rows(rows, "板块排行返回为空")
    return _non_empty_metadata_rows(_clean_plate_rows(rows), "板块排行字段无效")[:limit]


def _clean_plate_rows(rows: list[PlateItem]) -> list[PlateItem]:
    cleaned: list[PlateItem] = []
    for row in rows or []:
        rank = _positive_rank(row.rank)
        name = _required_text(row.name)
        change_pct = _finite_float(row.change_pct)
        source = _required_text(row.source)
        updated_at = _required_text(row.updated_at)
        if rank is None or change_pct is None or not all((name, source, updated_at)):
            continue
        cleaned.append(
            row.model_copy(
                update={
                    "rank": rank,
                    "name": name,
                    "change_pct": change_pct,
                    "amount": _optional_non_negative_float(row.amount),
                    "turnover_rate": _optional_non_negative_float(row.turnover_rate),
                    "leading_stock": _optional_text(row.leading_stock),
                    "leading_stock_change_pct": _optional_finite_float(row.leading_stock_change_pct),
                    "source": source,
                    "updated_at": updated_at,
                }
            )
        )
    return cleaned


def _prepare_concept_rows(
    attempt: ProviderAttempt,
    normalized: str,
    rows: list[StockConceptItem],
    limit: int,
) -> list[StockConceptItem]:
    normalized_rows = _clean_stock_concept_rows(_normalize_stock_concepts(normalized, rows, limit))
    if normalized_rows:
        return normalized_rows[:limit]
    if attempt.name == "local":
        raise ProviderCoverageMiss
    raise RuntimeError("概念归属返回为空")


def _clean_stock_concept_rows(rows: list[StockConceptItem]) -> list[StockConceptItem]:
    cleaned: list[StockConceptItem] = []
    seen_names: set[str] = set()
    for row in rows or []:
        rank = _positive_rank(row.rank)
        name = _required_text(row.name)
        change_pct = _finite_float(row.change_pct)
        source = _required_text(row.source)
        updated_at = _required_text(row.updated_at)
        if rank is None or change_pct is None or not all((name, source, updated_at)) or name in seen_names:
            continue
        seen_names.add(name)
        cleaned.append(
            row.model_copy(
                update={
                    "rank": rank,
                    "name": name,
                    "change_pct": change_pct,
                    "amount": _optional_non_negative_float(row.amount),
                    "turnover_rate": _optional_non_negative_float(row.turnover_rate),
                    "leading_stock": _optional_text(row.leading_stock),
                    "leading_stock_change_pct": _optional_finite_float(row.leading_stock_change_pct),
                    "match_reason": _required_text(row.match_reason) or "概念成分匹配",
                    "source": source,
                    "updated_at": updated_at,
                }
            )
        )
    return cleaned


def _positive_rank(value: Any) -> int | None:
    try:
        rank = int(value)
    except (TypeError, ValueError):
        return None
    return rank if rank > 0 else None


def _finite_float(value: Any) -> float | None:
    try:
        number = float(value)
    except (TypeError, ValueError):
        return None
    return number if math.isfinite(number) else None


def _optional_finite_float(value: object) -> float | None:
    if value is None:
        return None
    return _finite_float(value)


def _optional_non_negative_float(value: object) -> float | None:
    number = _optional_finite_float(value)
    return number if number is not None and number >= 0 else None


def _required_text(value: object) -> str:
    return str(value or "").strip()


def _optional_text(value: object) -> str | None:
    text = _required_text(value)
    return text or None
