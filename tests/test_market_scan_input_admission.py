"""Production admission preserves provenance without changing frozen scores."""

from __future__ import annotations

from copy import deepcopy
import sqlite3
from types import SimpleNamespace

import pytest

from app.models.market_scan import MarketScanMode
from app.repositories.market_scan_rule_contracts import register_market_scan_rule_contract
from app.services.market_scan_manager import market_scan_rule_contract
from app.services import market_scan_scoring as scoring
from tests.test_market_scan_raw_score_replay import _snapshot
from tests.test_market_scan_scoring import AS_OF, DATA_DATE, PREOPEN_AS_OF, _item, _quote, _rows


def _score(*, quote=None, rows=None, mode: MarketScanMode = "official", minimum: int = 50):
    quote = quote if quote is not None else _quote()
    now = AS_OF if mode == "official" else PREOPEN_AS_OF
    if mode == "intraday":
        now = PREOPEN_AS_OF.replace(hour=10)
        quote = quote.model_copy(update={
            "timestamp": "2026-07-20 10:00:00", "prev_close": 10.5,
            "price": 10.605, "change": 0.105, "change_pct": 1.0,
        })
    return scoring.score_market_scan_item(
        _item(), quote, rows if rows is not None else _rows(DATA_DATE, 80),
        as_of=now, completed_cutoff=DATA_DATE, expected_data_date=DATA_DATE,
        expected_quote_date=now.date() if mode == "intraday" else DATA_DATE,
        min_history_rows=60, min_data_quality_score=minimum, mode=mode,
    )


@pytest.mark.parametrize("field,value", [
    ("source", "second-vendor"), ("fallback_used", True), ("from_cache", True),
    ("as_of", AS_OF.isoformat()), ("fetched_at", AS_OF.isoformat()),
    ("data_version", "other-revision"), ("contract_version", "other-contract"),
    ("session_status", "suspended"), ("open_execution_status", "blocked"),
    ("corporate_action_status", "event"), ("adjustment_factor", 2.0),
    ("point_in_time", True), ("execution_metadata_version", "other-execution-contract"),
])
@pytest.mark.parametrize("reverse", [False, True])
def test_same_price_duplicate_cannot_erase_conflicting_provenance(field, value, reverse):
    rows = _rows(DATA_DATE, 80)
    duplicate = rows[-5].model_copy(update={field: value})
    observations = [duplicate, *rows] if reverse else [*rows, duplicate]
    with pytest.raises(scoring.MarketScanDataMissing, match="冲突日K"):
        _score(rows=observations)


def test_exact_duplicate_is_order_independent_and_preserves_legal_score():
    rows = _rows(DATA_DATE, 80)
    baseline = _score(rows=rows)
    assert _score(rows=[*rows, rows[-5]]) == baseline
    assert _score(rows=[rows[-5], *reversed(rows)]) == baseline
    assert baseline.raw_score == 88.3484
    assert baseline.data_quality_score == 100


@pytest.mark.parametrize("mode", ["official", "preopen", "intraday"])
@pytest.mark.parametrize("source", ["演示行情", "DEMO provider", "vendor demo·缓存"])
@pytest.mark.parametrize("minimum", [0, 50])
def test_demo_quote_is_not_admitted_by_lowering_quality_threshold(mode, source, minimum):
    quote = _quote().model_copy(update={"source": source})
    with pytest.raises(scoring.MarketScanDataMissing, match="演示"):
        _score(quote=quote, mode=mode, minimum=minimum)


@pytest.mark.parametrize("mode", ["official", "preopen", "intraday"])
@pytest.mark.parametrize("position", [0, -1])
def test_demo_bar_cannot_hide_behind_a_real_latest_source(mode, position):
    rows = scoring.completed_market_scan_klines(_rows(DATA_DATE, 80), DATA_DATE)
    rows[position] = rows[position].model_copy(update={"source": "演示日K"})
    with pytest.raises(scoring.MarketScanDataMissing, match="演示"):
        _score(rows=rows, mode=mode, minimum=0)


def test_future_demo_bar_is_outside_the_admitted_snapshot():
    rows = _rows(DATA_DATE, 80)
    future = rows[-1].model_copy(update={"date": "2026-07-20", "source": "demo"})
    assert _score(rows=[*rows, future]) == _score(rows=rows)


def _rule_contract():
    settings = SimpleNamespace(
        market_scan_min_data_quality_score=50, market_scan_kline_limit=120,
        market_scan_min_history_rows=61, market_scan_new_stock_days=120,
    )
    return market_scan_rule_contract(settings)


def _assert_current_score_contract_and_frozen_dimension_v4(spec):
    assert spec["research_dimensions"]["algorithm"] == "full-market-dimensions-v5-target-semideviation"
    assert scoring.stable_score_spec_hash(spec) == "2fca8cd2e3a6dbefbb2c424dd7fb07d7ed8e1b88c35334e8e1256d9d5e1d7f88"
    frozen = scoring.market_scan_score_spec_dimension_v4(min_data_quality_score=50)
    assert frozen["research_dimensions"]["algorithm"] == "full-market-dimensions-v4-session-coverage"
    assert scoring.stable_score_spec_hash(frozen) == "17c0e6b9ed6de9b39cad0d9f9d1fe14c8638749dd08796bed827869d3554e2c7"
    # The research risk generation changes; trend and ranking math stay exact.
    expected = deepcopy(frozen)
    expected["research_dimensions"]["algorithm"] = "full-market-dimensions-v5-target-semideviation"
    assert spec == expected


def test_new_admission_identity_changes_run_hash_with_registered_score_hash():
    current = _rule_contract()
    legacy = deepcopy(current)
    admission = legacy.pop("input_admission", None)
    assert isinstance(admission, dict)
    assert admission["contract_version"] == "market-scan-input-admission-v5"
    assert scoring.stable_score_spec_hash(current) != scoring.stable_score_spec_hash(legacy)
    assert current["score_spec"] == legacy["score_spec"]
    _assert_current_score_contract_and_frozen_dimension_v4(current["score_spec"])
    assert scoring.stable_score_spec_hash(scoring.market_scan_score_spec_v4(min_data_quality_score=50)) == (
        "30c5abb10b676fc71b5fa6c621cce809a6c2d054113fa578d77eccf28fb5955a"
    )


@pytest.mark.parametrize("mutation", ["missing", "relaxed"])
def test_new_rule_registration_requires_current_admission_contract(mutation):
    contract = _rule_contract()
    if mutation == "missing":
        contract.pop("input_admission", None)
    else:
        contract["input_admission"] = {"demo_sources": "allow"}
    version = f"full-market-scan-v6:{scoring.stable_score_spec_hash(contract)}"
    with sqlite3.connect(":memory:") as conn:
        with pytest.raises(ValueError, match="准入"):
            register_market_scan_rule_contract(conn, rule_version=version, contract=contract, stamp=AS_OF.isoformat())


def test_existing_frozen_score_replay_does_not_reapply_current_admission(monkeypatch):
    item, run = _snapshot(monkeypatch, mutation="none", mode="official")
    frozen = deepcopy(item.score_details)
    # Reading frozen evidence does not call the new input-admission path.
    def reject_current_inputs(*_args, **_kwargs):
        raise AssertionError("historical replay must not rescore raw production input")

    monkeypatch.setattr(scoring, "completed_market_scan_klines", reject_current_inputs)
    scoring.verify_persisted_market_scan_result(item, run)
    assert scoring.replay_score_details(frozen).raw_score == item.raw_score
    assert item.score_details == frozen
