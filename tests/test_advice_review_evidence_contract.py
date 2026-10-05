from __future__ import annotations

from copy import deepcopy
import json
from pathlib import Path

import pytest

from app.utils.advice_review_evidence import (
    review_result_input_digest,
    review_result_outcome_digest,
)


_GOLDEN = json.loads(
    (Path(__file__).parent / "fixtures" / "advice_review_evidence_golden_v1.json").read_text()
)["cases"]


@pytest.mark.parametrize("case", _GOLDEN, ids=["v1", "v2"])
def test_review_evidence_matches_pre_refactor_stored_bytes(case: dict) -> None:
    assert review_result_input_digest(case["row"]) == case["input_digest"]
    assert review_result_outcome_digest(case["row"]) == case["result_digest"]
    assert review_result_input_digest(case["row"], normalize_reals=True) == case["input_digest"]
    assert review_result_outcome_digest(case["row"], normalize_reals=True) == case["result_digest"]


@pytest.mark.parametrize("case", _GOLDEN, ids=["v1", "v2"])
def test_portable_real_numbers_preserve_sqlite_digest(case: dict) -> None:
    row = deepcopy(case["row"])
    assert row["entry_price"] == 100.0
    row["entry_price"] = 100

    assert review_result_input_digest(row, normalize_reals=True) == case["input_digest"]
    assert review_result_input_digest(row) != case["input_digest"]


@pytest.mark.parametrize("case", _GOLDEN, ids=["v1", "v2"])
def test_review_input_identity_remap_changes_only_input_digest(case: dict) -> None:
    row = {**case["row"], "advice_id": 9, "plan_id": 11, "plan_payload_digest": "f" * 64}

    assert review_result_input_digest(row) != case["input_digest"]
    assert review_result_outcome_digest(row) == case["result_digest"]


@pytest.mark.parametrize("field,value", [("evaluated_at", "2026-07-18T08:01:00.000000Z"), ("attempt", 2)])
def test_v1_keeps_legacy_ordering_boundary_while_v2_binds_it(field: str, value: object) -> None:
    for case in _GOLDEN:
        row = {**case["row"], field: value}
        changed = review_result_input_digest(row) != case["input_digest"]
        assert changed == (row["evidence_contract_version"] == "advice-review-evidence.v2")


def test_outcome_mutation_does_not_change_input_identity() -> None:
    case = _GOLDEN[1]
    row = {**case["row"], "target_hit": 1, "target_hit_date": "2026-07-17"}

    assert review_result_input_digest(row) == case["input_digest"]
    assert review_result_outcome_digest(row) != case["result_digest"]


@pytest.mark.parametrize("value", [True, "100", float("nan"), float("inf"), -float("inf")])
@pytest.mark.parametrize("field", ["entry_price", "return_pct"])
def test_portable_real_values_reject_invalid_numbers(field: str, value: object) -> None:
    row = {**_GOLDEN[1]["row"], field: value}
    digest = review_result_input_digest if field == "entry_price" else review_result_outcome_digest

    with pytest.raises(ValueError, match="复盘计划数值载荷无效"):
        digest(row, normalize_reals=True)


@pytest.mark.parametrize("value", [float("nan"), float("inf")])
def test_stored_nonfinite_values_cannot_receive_a_digest(value: float) -> None:
    row = {**_GOLDEN[1]["row"], "return_pct": value}

    with pytest.raises(ValueError):
        review_result_outcome_digest(row)


def test_nullable_real_evidence_remains_null_in_portable_digest() -> None:
    row = {**_GOLDEN[1]["row"], "return_pct": None, "anchor_evaluation_close": None}

    assert review_result_input_digest(row, normalize_reals=True) == review_result_input_digest(row)
    assert review_result_outcome_digest(row, normalize_reals=True) == review_result_outcome_digest(row)
