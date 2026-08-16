from __future__ import annotations

from app.market_scan_screening import screen_spec_from_market_scan_filters
from app.models.market_scan import MarketScanFilterValues, MarketScanResultStatus
from app.models.market_scan_screening import ScreenSpecV2
from app.repositories.market_scan_screening_sql import screen_spec_filter_sql, screen_spec_order_sql


def market_scan_result_filter_sql(
    run_id: int,
    *,
    status: MarketScanResultStatus | None,
    market: MarketScanFilterValues,
    industry: MarketScanFilterValues,
    is_st: bool | None,
    is_new: bool | None,
    min_score: int | None,
    max_score: int | None,
    min_trend_score: int | None,
    max_trend_score: int | None,
    min_change_pct: float | None,
    max_change_pct: float | None,
    min_turnover_rate: float | None,
    max_turnover_rate: float | None,
    min_amount: float | None,
    max_amount: float | None,
    min_data_quality_score: int | None,
    max_data_quality_score: int | None,
    min_confidence: float | None,
    max_risk: float | None,
    min_tradability: float | None,
    keyword: str | None,
    symbols: MarketScanFilterValues = None,
) -> tuple[str, list[object]]:
    spec = screen_spec_from_market_scan_filters(
        status=status, market=market, industry=industry, is_st=is_st, is_new=is_new,
        min_score=min_score, max_score=max_score,
        min_trend_score=min_trend_score, max_trend_score=max_trend_score,
        min_change_pct=min_change_pct, max_change_pct=max_change_pct,
        min_turnover_rate=min_turnover_rate, max_turnover_rate=max_turnover_rate,
        min_amount=min_amount, max_amount=max_amount,
        min_data_quality_score=min_data_quality_score,
        max_data_quality_score=max_data_quality_score,
        min_confidence=min_confidence, max_risk=max_risk,
        min_tradability=min_tradability, keyword=keyword,
        sort="rank", order="asc",
    )
    where, parameters, _order = market_scan_result_screen_sql(run_id, spec, symbols=symbols)
    return where, parameters


def market_scan_result_screen_sql(
    run_id: int,
    spec: ScreenSpecV2,
    *,
    symbols: MarketScanFilterValues = None,
) -> tuple[str, list[object], str]:
    filter_sql, filter_parameters = screen_spec_filter_sql(spec)
    clauses = ["run_id = ?", filter_sql]
    parameters: list[object] = [run_id, *filter_parameters]
    _append_symbol_scope(clauses, parameters, symbols)
    return " AND ".join(clauses), parameters, screen_spec_order_sql(spec)


def normalized_filter_values(value: MarketScanFilterValues, *, maximum: int) -> tuple[str, ...]:
    candidates = [value] if isinstance(value, str) else list(value or ())
    normalized = tuple(dict.fromkeys(" ".join(str(item).split()).strip() for item in candidates))
    return tuple(item for item in normalized if item)[:maximum]


def _append_symbol_scope(
    clauses: list[str],
    parameters: list[object],
    symbols: MarketScanFilterValues,
) -> None:
    if symbols is None:
        return
    normalized = normalized_filter_values(symbols, maximum=10_000)
    if not normalized:
        clauses.append("0 = 1")
        return
    clauses.append(f"symbol IN ({','.join('?' for _value in normalized)})")
    parameters.extend(normalized)


__all__ = [
    "market_scan_result_filter_sql",
    "market_scan_result_screen_sql",
    "normalized_filter_values",
]
