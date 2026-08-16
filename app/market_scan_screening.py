"""Canonical screening semantics shared by API, exports and saved discovery presets."""

from __future__ import annotations

from dataclasses import dataclass
import hashlib
import json
from typing import Literal, Sequence, cast

from app.models.discovery import DiscoveryCriteria, DiscoverySort
from app.models.market_scan import (
    MarketScanFilterValues,
    MarketScanResultStatus,
    MarketScanSortOrderValues,
    MarketScanSortValues,
)
from app.models.market_scan_screening import (
    ScreenAmountRange,
    ScreenBoundedScoreRange,
    ScreenChangeRange,
    ScreenNumericRange,
    ScreenRangeField,
    ScreenRangesV2,
    ScreenSortField,
    ScreenSortV2,
    ScreenSpecV2,
    ScreenTurnoverRange,
)


ConditionKind = Literal["exact", "in", "contains_any", "range", "keyword"]


@dataclass(frozen=True)
class CompiledScreenCondition:
    code: str
    label: str
    field: str
    kind: ConditionKind
    values: tuple[object, ...] = ()
    minimum: float | None = None
    maximum: float | None = None


_RANGE_LABELS: dict[ScreenRangeField, str] = {
    "score": "趋势强度",
    "trend_score": "趋势分",
    "change_pct": "涨跌幅",
    "turnover_rate": "换手率",
    "amount": "成交额",
    "data_quality_score": "数据质量",
    "confidence": "置信度",
    "risk": "风险分",
    "tradability": "可交易性",
}
_DISCOVERY_SORT_MAP: dict[str, ScreenSortField] = {
    "rank": "rank", "symbol": "symbol", "market": "market",
    "industry": "industry", "is_st": "is_st", "is_new": "is_new",
    "quality": "data_quality_score", "trend": "trend_score",
    "change": "change_pct", "turnover": "turnover_rate", "amount": "amount",
    "score": "score", "raw_score": "raw_score",
}


def compile_screen_conditions(spec: ScreenSpecV2) -> tuple[CompiledScreenCondition, ...]:
    conditions: list[CompiledScreenCondition] = []
    if spec.status is not None:
        conditions.append(CompiledScreenCondition("status", "结果状态", "status", "exact", (spec.status,)))
    if spec.markets:
        conditions.append(CompiledScreenCondition("market", "市场", "market", "in", tuple(spec.markets)))
    if spec.industries:
        conditions.append(
            CompiledScreenCondition("industry", "行业", "industry", "contains_any", tuple(spec.industries))
        )
    if spec.is_st is not None:
        conditions.append(CompiledScreenCondition("is_st", "ST 状态", "is_st", "exact", (spec.is_st,)))
    if spec.is_new is not None:
        conditions.append(CompiledScreenCondition("is_new", "新股状态", "is_new", "exact", (spec.is_new,)))
    for field in cast(tuple[ScreenRangeField, ...], tuple(ScreenRangesV2.model_fields)):
        bounds = getattr(spec.ranges, field)
        if bounds is not None:
            conditions.append(
                CompiledScreenCondition(
                    code=f"range.{field}",
                    label=_RANGE_LABELS[field],
                    field=field,
                    kind="range",
                    minimum=bounds.min,
                    maximum=bounds.max,
                )
            )
    if spec.keyword:
        conditions.append(
            CompiledScreenCondition("keyword", "股票搜索", "keyword", "keyword", (spec.keyword,))
        )
    return tuple(conditions)


def screen_spec_digest(spec: ScreenSpecV2) -> str:
    payload = json.dumps(
        spec.model_dump(mode="json"),
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    )
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()


def screen_spec_from_market_scan_filters(
    *,
    status: MarketScanResultStatus | None,
    market: MarketScanFilterValues,
    industry: MarketScanFilterValues,
    is_st: bool | None,
    is_new: bool | None,
    min_score: int | None = None,
    max_score: int | None = None,
    min_trend_score: int | None = None,
    max_trend_score: int | None = None,
    min_change_pct: float | None = None,
    max_change_pct: float | None = None,
    min_turnover_rate: float | None = None,
    max_turnover_rate: float | None = None,
    min_amount: float | None = None,
    max_amount: float | None = None,
    min_data_quality_score: int | None = None,
    max_data_quality_score: int | None = None,
    min_confidence: float | None = None,
    max_risk: float | None = None,
    min_tradability: float | None = None,
    keyword: str | None,
    sort: MarketScanSortValues,
    order: MarketScanSortOrderValues,
) -> ScreenSpecV2:
    sorts = (sort,) if isinstance(sort, str) else tuple(sort)
    orders = (order,) if isinstance(order, str) else tuple(order)
    if len(sorts) != len(orders):
        raise ValueError("排序字段和方向必须一一对应")
    ranges = ScreenRangesV2(
        score=_bounded_range(min_score, max_score),
        trend_score=_bounded_range(min_trend_score, max_trend_score),
        change_pct=_range(ScreenChangeRange, min_change_pct, max_change_pct),
        turnover_rate=_range(ScreenTurnoverRange, min_turnover_rate, max_turnover_rate),
        amount=_range(ScreenAmountRange, min_amount, max_amount),
        data_quality_score=_bounded_range(min_data_quality_score, max_data_quality_score),
        confidence=_bounded_range(min_confidence, None),
        risk=_bounded_range(None, max_risk),
        tradability=_bounded_range(min_tradability, None),
    )
    return ScreenSpecV2(
        status=status,
        markets=list(_normalized_values(market, maximum=3)),
        industries=list(_normalized_values(industry, maximum=20)),
        is_st=is_st,
        is_new=is_new,
        ranges=ranges,
        keyword=_normalized_text(keyword),
        sort=[ScreenSortV2(field=cast(ScreenSortField, field), order=direction) for field, direction in zip(sorts, orders, strict=True)],
    )


def screen_spec_from_discovery(
    criteria: DiscoveryCriteria,
    sort: list[DiscoverySort],
) -> ScreenSpecV2:
    return ScreenSpecV2(
        status="success",
        markets=list(criteria.market or ()),
        industries=list(criteria.industry or ()),
        is_st=criteria.is_st,
        is_new=criteria.is_new,
        ranges=ScreenRangesV2(
            score=_copy_range(criteria.score, ScreenBoundedScoreRange),
            trend_score=_copy_range(criteria.trend, ScreenBoundedScoreRange),
            change_pct=_copy_range(criteria.change, ScreenChangeRange),
            turnover_rate=_copy_range(criteria.turnover, ScreenTurnoverRange),
            amount=_copy_range(criteria.amount, ScreenAmountRange),
            data_quality_score=_copy_range(criteria.quality, ScreenBoundedScoreRange),
            confidence=_copy_range(getattr(criteria, "confidence", None), ScreenBoundedScoreRange),
            risk=_copy_range(getattr(criteria, "risk", None), ScreenBoundedScoreRange),
            tradability=_copy_range(getattr(criteria, "tradability", None), ScreenBoundedScoreRange),
        ),
        keyword=getattr(criteria, "keyword", None),
        sort=[
            ScreenSortV2(field=_DISCOVERY_SORT_MAP[item.field], order=item.order)
            for item in sort
        ],
    )


def _copy_range(value: object, cls: type[ScreenNumericRange]) -> ScreenNumericRange | None:
    if value is None:
        return None
    return cls(min=getattr(value, "min"), max=getattr(value, "max"))


def _bounded_range(minimum: float | None, maximum: float | None) -> ScreenBoundedScoreRange | None:
    return _range(ScreenBoundedScoreRange, minimum, maximum)


def _range(
    cls: type[ScreenNumericRange],
    minimum: float | None,
    maximum: float | None,
) -> ScreenNumericRange | None:
    return None if minimum is None and maximum is None else cls(min=minimum, max=maximum)


def _normalized_values(value: MarketScanFilterValues, *, maximum: int) -> tuple[str, ...]:
    candidates: Sequence[str] = (value,) if isinstance(value, str) else tuple(value or ())
    normalized = tuple(dict.fromkeys(_normalized_text(item) for item in candidates))
    return tuple(item for item in normalized if item is not None)[:maximum]


def _normalized_text(value: object | None) -> str | None:
    normalized = " ".join(str(value or "").split()).strip()
    return normalized or None


__all__ = [
    "CompiledScreenCondition",
    "compile_screen_conditions",
    "screen_spec_digest",
    "screen_spec_from_discovery",
    "screen_spec_from_market_scan_filters",
]
