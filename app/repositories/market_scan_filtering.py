from __future__ import annotations

from app.models.market_scan import MarketScanFilterValues, MarketScanResultStatus
from app.repositories.market_scan_mapping import escaped_like


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
    clauses = ["run_id = ?"]
    parameters: list[object] = [run_id]
    _append_exact(clauses, parameters, "status", status)
    _append_in(clauses, parameters, "market", market)
    _append_like_any(clauses, parameters, "industry", industry)
    _append_exact(clauses, parameters, "is_st", int(is_st) if is_st is not None else None)
    _append_exact(clauses, parameters, "is_new", int(is_new) if is_new is not None else None)
    for column, minimum, maximum in (
        ("score", min_score, max_score),
        ("trend_score", min_trend_score, max_trend_score),
        ("change_pct", min_change_pct, max_change_pct),
        ("turnover_rate", min_turnover_rate, max_turnover_rate),
        ("amount", min_amount, max_amount),
        ("data_quality_score", min_data_quality_score, max_data_quality_score),
        (_score_dimension_sql("confidence"), min_confidence, None),
        (_score_dimension_sql("risk"), None, max_risk),
        (_score_dimension_sql("tradability"), min_tradability, None),
    ):
        _append_range(clauses, parameters, column, minimum, maximum)
    _append_keyword(clauses, parameters, keyword)
    _append_symbol_scope(clauses, parameters, symbols)
    return " AND ".join(clauses), parameters


def _score_dimension_sql(name: str) -> str:
    return f"json_extract(metrics_json, '$.score_details.components.score_dimensions.scores.{name}')"


def normalized_filter_values(value: MarketScanFilterValues, *, maximum: int) -> tuple[str, ...]:
    candidates = [value] if isinstance(value, str) else list(value or ())
    normalized = tuple(dict.fromkeys(" ".join(str(item).split()).strip() for item in candidates))
    return tuple(item for item in normalized if item)[:maximum]


def _append_exact(
    clauses: list[str],
    parameters: list[object],
    column: str,
    value: object | None,
) -> None:
    if value is not None:
        clauses.append(f"{column} = ?")
        parameters.append(value)


def _append_in(
    clauses: list[str],
    parameters: list[object],
    column: str,
    values: MarketScanFilterValues,
) -> None:
    normalized = normalized_filter_values(values, maximum=20)
    if not normalized:
        return
    clauses.append(f"{column} IN ({','.join('?' for _value in normalized)})")
    parameters.extend(normalized)


def _append_like_any(
    clauses: list[str],
    parameters: list[object],
    column: str,
    values: MarketScanFilterValues,
) -> None:
    normalized = normalized_filter_values(values, maximum=20)
    if not normalized:
        return
    clauses.append("(" + " OR ".join(f"{column} LIKE ? ESCAPE '\\'" for _value in normalized) + ")")
    parameters.extend(f"%{escaped_like(value)}%" for value in normalized)


def _append_range(
    clauses: list[str],
    parameters: list[object],
    column: str,
    minimum: int | float | None,
    maximum: int | float | None,
) -> None:
    if minimum is not None:
        clauses.append(f"{column} >= ?")
        parameters.append(minimum)
    if maximum is not None:
        clauses.append(f"{column} <= ?")
        parameters.append(maximum)


def _append_keyword(clauses: list[str], parameters: list[object], keyword: str | None) -> None:
    normalized = " ".join((keyword or "").split()).strip()
    if not normalized:
        return
    like = f"%{escaped_like(normalized)}%"
    clauses.append("(symbol LIKE ? ESCAPE '\\' OR code LIKE ? ESCAPE '\\' OR name LIKE ? ESCAPE '\\')")
    parameters.extend((like, like, like))


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


__all__ = ["market_scan_result_filter_sql", "normalized_filter_values"]
