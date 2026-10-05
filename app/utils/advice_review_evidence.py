"""Canonical review-result evidence bytes, shared by storage and portability.

Version admission remains with callers: live repositories require v2 while imports
may retain v1 audit history. Portable JSON normalizes REAL values to SQLite's float
representation; stored rows are hashed verbatim to preserve existing digests.
"""

from __future__ import annotations

from collections.abc import Mapping
import hashlib
import json
import math


ADVICE_REVIEW_EVIDENCE_CONTRACT_VERSION = "advice-review-evidence.v2"
REVIEW_EVIDENCE_DIGEST_VERSIONS = frozenset(
    {"advice-review-evidence.v1", ADVICE_REVIEW_EVIDENCE_CONTRACT_VERSION}
)
REVIEW_RESULT_INSERT_FIELDS = (
    "plan_id",
    "plan_revision",
    "advice_id",
    "symbol",
    "snapshot_market_time",
    "as_of",
    "evaluated_at",
    "status",
    "conclusion",
    "rule_version",
    "trigger_basis",
    "invalidation_basis",
    "snapshot_adjustment_mode",
    "snapshot_anchor_date",
    "snapshot_anchor_close",
    "snapshot_data_version",
    "snapshot_contract_version",
    "evaluation_adjustment_mode",
    "evaluation_data_version",
    "evaluation_contract_version",
    "anchor_evaluation_close",
    "price_scale_factor",
    "normalized_entry_price",
    "normalized_target_price",
    "normalized_stop_price",
    "entry_price",
    "target_price",
    "stop_price",
    "horizon_days",
    "visible_bar_count",
    "visible_start_date",
    "visible_end_date",
    "available_forward_days",
    "forward_start_date",
    "forward_end_date",
    "return_pct",
    "max_favorable_excursion_pct",
    "max_adverse_excursion_pct",
    "target_hit",
    "target_hit_date",
    "stop_hit",
    "stop_hit_date",
    "attempt",
    "plan_payload_digest",
    "input_digest",
    "result_digest",
    "evidence_contract_version",
    "source_window_digest",
    "source_session_count",
    "expected_session_count",
    "observation_basis",
)
REVIEW_RESULT_OUTCOME_FIELDS = (
    "status",
    "conclusion",
    "return_pct",
    "max_favorable_excursion_pct",
    "max_adverse_excursion_pct",
    "target_hit",
    "target_hit_date",
    "stop_hit",
    "stop_hit_date",
)
_RESULT_DIGEST_FIELDS = frozenset({"plan_payload_digest", "input_digest", "result_digest"})
_RESULT_INPUT_FIELDS = tuple(
    field
    for field in REVIEW_RESULT_INSERT_FIELDS
    if field not in _RESULT_DIGEST_FIELDS and field not in REVIEW_RESULT_OUTCOME_FIELDS
)
_RESULT_V1_INPUT_FIELDS = tuple(
    field for field in _RESULT_INPUT_FIELDS if field not in {"evaluated_at", "attempt"}
)
_RESULT_REAL_FIELDS = frozenset(
    {
        "snapshot_anchor_close",
        "anchor_evaluation_close",
        "price_scale_factor",
        "normalized_entry_price",
        "normalized_target_price",
        "normalized_stop_price",
        "entry_price",
        "target_price",
        "stop_price",
        "return_pct",
        "max_favorable_excursion_pct",
        "max_adverse_excursion_pct",
    }
)


def review_result_input_digest(
    values: Mapping[str, object], *, normalize_reals: bool = False,
) -> str:
    """Hash versioned input fields after the caller has admitted the version."""
    fields = (
        _RESULT_V1_INPUT_FIELDS
        if values["evidence_contract_version"] == "advice-review-evidence.v1"
        else _RESULT_INPUT_FIELDS
    )
    return _result_digest(values, fields, normalize_reals=normalize_reals)


def review_result_outcome_digest(
    values: Mapping[str, object], *, normalize_reals: bool = False,
) -> str:
    """Hash the result fields shared by both admitted evidence versions."""
    return _result_digest(values, REVIEW_RESULT_OUTCOME_FIELDS, normalize_reals=normalize_reals)


def _result_digest(
    values: Mapping[str, object], fields: tuple[str, ...], *, normalize_reals: bool,
) -> str:
    payload = {
        field: _result_value(field, values[field], normalize_reals=normalize_reals)
        for field in fields
    }
    canonical = json.dumps(
        payload, ensure_ascii=False, sort_keys=True, separators=(",", ":"), allow_nan=False,
    )
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()


def _result_value(field: str, value: object, *, normalize_reals: bool) -> object:
    if not normalize_reals or field not in _RESULT_REAL_FIELDS or value is None:
        return value
    if isinstance(value, bool) or not isinstance(value, int | float):
        raise ValueError("复盘计划数值载荷无效")
    parsed = float(value)
    if not math.isfinite(parsed):
        raise ValueError("复盘计划数值载荷无效")
    return parsed
