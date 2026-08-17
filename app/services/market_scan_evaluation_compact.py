"""Compact the large shadow-evaluation report for retained product evidence."""

from __future__ import annotations

from collections.abc import Mapping
from typing import cast


_REPORT_FIELDS = (
    "schema_version",
    "generated_at",
    "status",
    "promotion",
)
_PRODUCTION_FIELDS = (
    "schema_version",
    "generated_at",
    "status",
    "config",
    "source",
    "exposure_audit",
    "evaluation_quality",
    "limitations",
)
_CANDIDATE_FIELDS = (
    "schema_version",
    "generated_at",
    "status",
    "config",
    "robustness",
    "evaluation_quality",
    "rank_delta_vs_production",
    "production_comparison",
    "limitations",
)


def compact_shadow_comparison_report(report: Mapping[str, object]) -> dict[str, object]:
    """Return the bounded read-model used by the strategy evidence center.

    The offline evaluator may contain millions of per-stock probability and
    factor records.  None of those records are required by the evidence-center
    projection, so the retained compact artifact keeps only audit, aggregate,
    robustness, comparison, and promotion evidence.
    """

    production = _mapping(report.get("production"))
    candidates = _mapping(report.get("candidates"))
    compact = _select(report, _REPORT_FIELDS)
    compact["artifact_projection"] = {
        "schema_version": "market-scan-shadow-comparison-compact-v1",
        "source_schema_version": report.get("schema_version"),
        "omitted_sections": [
            "runs",
            "monotonicity",
            "factor_diagnostics",
            "calibration",
            "regime_overlay",
            "promotion_evidence",
            "probability_research",
            "per_stock_records",
        ],
        "semantics": (
            "bounded product projection; full evaluation is reproducible from "
            "the attested read-only SQLite snapshot and frozen CLI arguments"
        ),
    }
    compact["production"] = _compact_production(production)
    compact["candidates"] = _compact_candidates(candidates)
    return compact


def _compact_production(production: Mapping[str, object]) -> dict[str, object]:
    compact = _select(production, _PRODUCTION_FIELDS)
    compact["source"] = _compact_source(_mapping(production.get("source")))
    for field in ("cohorts", "rank_ic", "deciles", "stability"):
        compact[field] = _full_contract_rows(production.get(field))
    return compact


def _compact_candidates(candidates: Mapping[str, object]) -> dict[str, object]:
    output: dict[str, object] = {}
    for candidate_id, value in sorted(candidates.items(), key=lambda item: str(item[0])):
        candidate = _mapping(value)
        compact = _select(candidate, _CANDIDATE_FIELDS)
        compact["source"] = _compact_source(_mapping(candidate.get("source")))
        compact["shadow"] = _compact_shadow(_mapping(candidate.get("shadow")))
        compact["cohorts"] = [
            row
            for row in _full_contract_rows(candidate.get("cohorts"))
            if row.get("top_n") in {20, 50, 100}
            and row.get("horizon_trading_days") == 5
        ]
        compact["stability"] = _full_contract_rows(candidate.get("stability"))
        compact["exposure_audit"] = [
            _select(row, ("run_id", "quote_date", "top_n", "rule_version"))
            for row in _mapping_rows(candidate.get("exposure_audit"))
        ]
        output[str(candidate_id)] = compact
    return output


def _compact_source(source: Mapping[str, object]) -> dict[str, object]:
    return {
        key: value
        for key, value in source.items()
        if key != "database"
    }


def _compact_shadow(shadow: Mapping[str, object]) -> dict[str, object]:
    run_evidence = shadow.get("run_evidence")
    rows = run_evidence if isinstance(run_evidence, list) else []
    return {
        key: value
        for key, value in shadow.items()
        if key not in {"run_evidence"}
    } | {
        "run_evidence": [
            _select(_mapping(row), ("run_id", "candidate_id", "scored_count", "ranking_digest"))
            for row in rows
        ]
    }


def _full_contract_rows(value: object) -> list[dict[str, object]]:
    return [
        dict(row)
        for row in _mapping_rows(value)
        if _is_full_contract_row(row)
    ]


def _is_full_contract_row(row: Mapping[str, object]) -> bool:
    dimensions = _mapping(row.get("dimensions"))
    if len(dimensions) == 3:
        return dimensions.get("scope") != "TOP100快速更新评分"
    return (
        all(row.get(field) not in {None, ""} for field in ("mode", "scope", "rule_version"))
        and row.get("scope") != "TOP100快速更新评分"
    )


def _mapping_rows(value: object) -> list[Mapping[str, object]]:
    if not isinstance(value, list):
        return []
    return [row for row in value if isinstance(row, Mapping)]


def _select(value: Mapping[str, object], fields: tuple[str, ...]) -> dict[str, object]:
    return {field: value[field] for field in fields if field in value}


def _mapping(value: object) -> Mapping[str, object]:
    return cast(Mapping[str, object], value) if isinstance(value, Mapping) else {}


__all__ = ["compact_shadow_comparison_report"]
