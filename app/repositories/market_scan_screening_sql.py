"""SQL projection of the canonical frozen-row screening contract."""

from __future__ import annotations

from app.market_scan_screening import CompiledScreenCondition, compile_screen_conditions
from app.models.market_scan import MARKET_SCAN_RANK_TIE_BREAK
from app.models.market_scan_screening import ScreenSortField, ScreenSpecV2
from app.repositories.market_scan_mapping import escaped_like


_DIMENSION_PREFIX = "$.score_details.components.score_dimensions.scores"


def _numeric_json_dimension(name: str) -> str:
    path = f"{_DIMENSION_PREFIX}.{name}"
    return (
        f"CASE WHEN json_type(metrics_json, '{path}') IN ('integer','real') "
        f"THEN json_extract(metrics_json, '{path}') END"
    )


_FIELD_EXPRESSIONS: dict[ScreenSortField, str] = {
    "rank": "rank", "score": "score", "raw_score": "raw_score",
    "trend_score": "trend_score", "change_pct": "change_pct", "amount": "amount",
    "turnover_rate": "turnover_rate", "data_quality_score": "data_quality_score",
    "alpha_5d": _numeric_json_dimension("alpha_5d"),
    "confidence": _numeric_json_dimension("confidence"),
    "risk": _numeric_json_dimension("risk"),
    "tradability": _numeric_json_dimension("tradability"),
    "symbol": "symbol", "market": "market", "industry": "industry",
    "is_st": "is_st", "is_new": "is_new",
}
_FILTER_FIELD_EXPRESSIONS: dict[str, str] = {
    str(field): expression for field, expression in _FIELD_EXPRESSIONS.items()
}
_FILTER_FIELD_EXPRESSIONS["status"] = "status"


def screen_spec_filter_sql(
    spec: ScreenSpecV2,
    *,
    alias: str | None = None,
) -> tuple[str, list[object]]:
    clauses: list[str] = []
    parameters: list[object] = []
    for condition in compile_screen_conditions(spec):
        clause, values = screen_condition_sql(condition, alias=alias)
        clauses.append(clause)
        parameters.extend(values)
    return (" AND ".join(clauses) if clauses else "1 = 1"), parameters


def screen_condition_sql(
    condition: CompiledScreenCondition,
    *,
    alias: str | None = None,
) -> tuple[str, list[object]]:
    prefix = f"{alias}." if alias else ""
    if condition.kind == "keyword":
        keyword_value = f"%{escaped_like(str(condition.values[0]))}%"
        columns = (f"{prefix}symbol", f"{prefix}code", f"{prefix}name")
        return "(" + " OR ".join(f"{column} LIKE ? ESCAPE '\\'" for column in columns) + ")", [keyword_value] * 3
    expression = _aliased_expression(condition.field, prefix)
    if condition.kind == "exact":
        exact_value = condition.values[0]
        return f"{expression} = ?", [int(exact_value) if isinstance(exact_value, bool) else exact_value]
    if condition.kind == "in":
        placeholders = ",".join("?" for _value in condition.values)
        return f"{expression} IN ({placeholders})", list(condition.values)
    if condition.kind == "contains_any":
        clause = " OR ".join(f"{expression} LIKE ? ESCAPE '\\'" for _value in condition.values)
        return f"({clause})", [f"%{escaped_like(str(value))}%" for value in condition.values]
    clauses: list[str] = []
    values: list[object] = []
    if condition.minimum is not None:
        clauses.append(f"{expression} >= ?")
        values.append(condition.minimum)
    if condition.maximum is not None:
        clauses.append(f"{expression} <= ?")
        values.append(condition.maximum)
    return " AND ".join(clauses), values


def screen_spec_order_sql(spec: ScreenSpecV2, *, alias: str | None = None) -> str:
    prefix = f"{alias}." if alias else ""
    parts: list[str] = []
    for item in spec.sort:
        expression = _aliased_expression(item.field, prefix)
        direction = "ASC" if item.order == "asc" else "DESC"
        parts.extend((f"{expression} IS NULL ASC", f"{expression} {direction}"))
    if len(spec.sort) == 1 and spec.sort[0].field == "rank":
        parts.append(f"{prefix}symbol ASC")
    else:
        parts.extend(
            f"{prefix}{field} {direction.upper()}"
            for field, direction in MARKET_SCAN_RANK_TIE_BREAK
        )
    return ", ".join(parts)


def _aliased_expression(field: str, prefix: str) -> str:
    expression = _FILTER_FIELD_EXPRESSIONS[field]
    if "metrics_json" in expression:
        return expression.replace("metrics_json", f"{prefix}metrics_json")
    return f"{prefix}{expression}"


__all__ = ["screen_condition_sql", "screen_spec_filter_sql", "screen_spec_order_sql"]
