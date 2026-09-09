from __future__ import annotations

from dataclasses import asdict
import hashlib
import json

import pytest

from app.models.market import Kline
from app.services.market_scan_probability_labels import (
    ProbabilityLabelConfig,
    build_probability_label_outcomes,
    probability_label_contract,
)


def _rows() -> list[Kline]:
    return [
        Kline(
            date=day, open=opening, close=close, high=high, low=low,
            volume=1_000, adjustment_mode="qfq", source="test",
            data_version="probability-test-v1", contract_version="daily-kline.v1",
        )
        for day, opening, close, high, low in (
            ("2026-01-05", 100, 100, 101, 99),
            ("2026-01-06", 100, 101, 103, 99),
            ("2026-01-07", 105, 110, 112, 104),
        )
    ]


def _outcome(rows: list[Kline], dates: tuple[str, ...] = ("2026-01-06", "2026-01-07")):
    return build_probability_label_outcomes(
        symbol="600001.SH", market="SH", list_date="2020-01-02", is_st=False,
        quote_date="2026-01-05", amount=1_000_000_000, rows=rows,
        eligible_dates=dates, config=ProbabilityLabelConfig(horizons=(1,)),
    )[1]


@pytest.mark.parametrize("dates", [
    ("2026-01-07", "2026-01-06"),
    ("2026-01-06", "2026-01-06", "2026-01-07"),
    ("20260106", "2026-01-07"),
    ("2026-01-06", "2026-02-30"),
])
def test_probability_labels_reject_ambiguous_fixed_session_order(dates: tuple[str, ...]) -> None:
    with pytest.raises(ValueError, match="probability label.*date"):
        _outcome(_rows(), dates)


@pytest.mark.parametrize("update", [
    {"session_status": "suspended"},
    {"open_execution_status": "unavailable"},
    {"open_execution_status": "locked_limit_up"},
    {"data_version": "different-vintage"},
    {"source": "other-provider"},
    {"as_of": "2026-01-06T15:01:00+08:00"},
    {"fallback_used": True},
    {"point_in_time": True},
    {"execution_metadata_version": "different-contract"},
])
@pytest.mark.parametrize("reverse", [False, True])
def test_conflicting_same_day_evidence_cannot_choose_label_by_row_order(update: dict, reverse: bool) -> None:
    rows = _rows()
    duplicate = rows[1].model_copy(update=update)
    pair = [rows[1], duplicate]
    if reverse:
        pair.reverse()
    with pytest.raises(ValueError, match="conflicting probability label bar"):
        _outcome([rows[0], *pair, rows[2]])


def test_identical_rows_and_unsorted_bars_preserve_valid_label_and_contract() -> None:
    rows = _rows()
    expected = _outcome(rows)
    assert asdict(_outcome([rows[2], rows[1], rows[0], rows[1].model_copy()])) == asdict(expected)
    assert expected.entry_date == "2026-01-06" and expected.exit_date == "2026-01-07"
    assert expected.status == "modelled" and expected.label == 1
    contract = probability_label_contract(ProbabilityLabelConfig(horizons=(1,)))
    encoded = json.dumps(contract, sort_keys=True, separators=(",", ":")).encode()
    assert hashlib.sha256(encoded).hexdigest() == "fc16876fff7ef4ea19c28dd7347f9c169912c76e5f3e0e39edf5b46e019afa21"


def test_evaluator_adapter_preserves_unavailable_labels_for_conflicting_source_evidence() -> None:
    from app.services import market_scan_evaluation as evaluation
    from tests.test_market_scan_evaluation_price_basis import _inputs

    _run, result, bars = _inputs()
    duplicate = {**bars[1], "source": "conflicting-provider"}
    outcomes = evaluation._probability_label_outcomes(
        result=result, symbol="600001.SH", market="SH", is_st=False,
        quote_date="2026-01-05", amount=1_000_000_000,
        bars=(*bars[:2], duplicate, bars[2]), eligible_dates=("2026-01-06", "2026-01-07"),
        config=evaluation.EvaluationConfig(horizons=(1,)),
    )
    assert set(outcomes) == {1, 5, 20}
    assert all(item.status == "data_unavailable" and item.label is None for item in outcomes.values())
    assert {item.reason for item in outcomes.values()} == {"label_input_invalid:ValueError"}
