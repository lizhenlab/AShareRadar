from __future__ import annotations

import asyncio
from copy import deepcopy
from dataclasses import replace
from pathlib import Path
import sqlite3

import pytest

from app.models.market_scan import MarketScanMode, MarketScanResultWrite, MarketScanSeed
from app.repositories.market_scan_rule_contracts import register_market_scan_rule_contract
from app.services.market_scan_scoring import (
    MarketScanDataMissing,
    market_scan_input_admission_spec,
    stable_score_spec_hash,
)
from app.services.market_scan_skip_contract import MARKET_SCAN_SKIP_EVIDENCE_KEY
from app.services.market_scan_validation import raise_batch_outcome_error
from tests.test_market_scan_failure_isolation import _Hub, _evaluator, _scan_one
from tests.test_market_scan_input_admission import _assert_current_score_contract_and_frozen_dimension_v4, _rule_contract, _score
from tests.test_market_scan_scoring import AS_OF, DATA_DATE, _quote, _rows
from tests.test_market_scan_skip_contract import _running_repository


@pytest.mark.parametrize("mode", ["official", "preopen", "intraday"])
@pytest.mark.parametrize("update", [{"high": 1.0}, {"close": float("nan")}, {"volume": -1.0}])
def test_invalid_completed_observation_cannot_become_a_justified_gap_or_success(
    mode: MarketScanMode,
    update: dict[str, object],
) -> None:
    rows = _rows(DATA_DATE, 80)
    rows[-5] = rows[-5].model_copy(update=update)

    with pytest.raises(MarketScanDataMissing, match="日K.*无效"):
        _score(rows=rows, mode=mode)


@pytest.mark.parametrize("position", [0, -1])
def test_invalid_completed_observation_is_not_silently_dropped_outside_recent_features(position: int) -> None:
    rows = _rows(DATA_DATE, 80)
    rows[position] = rows[position].model_copy(update={"volume": -1.0})

    with pytest.raises(MarketScanDataMissing, match="日K.*无效"):
        _score(rows=rows)


@pytest.mark.parametrize("reverse", [False, True])
def test_valid_duplicate_does_not_erase_an_invalid_completed_observation(reverse: bool) -> None:
    rows = _rows(DATA_DATE, 80)
    bad = rows[-5].model_copy(update={"high": 1.0})
    observations = [bad, *rows] if reverse else [*rows, bad]

    with pytest.raises(MarketScanDataMissing, match="日K.*无效"):
        _score(rows=observations)


@pytest.mark.parametrize("out_of_scope_date", ["2026-07-20", "2026-07-12", "2026/07/17"])
def test_non_snapshot_invalid_bars_remain_outside_completed_admission(out_of_scope_date: str) -> None:
    rows = _rows(DATA_DATE, 80)
    outside = rows[-1].model_copy(update={"date": out_of_scope_date, "high": 1.0})

    assert _score(rows=[*rows, outside]) == _score(rows=rows)


@pytest.mark.parametrize("quote_present", [False, True])
def test_bad_daily_observation_stays_in_coverage_denominator_after_batch_persistence(
    tmp_path: Path,
    quote_present: bool,
) -> None:
    repo, run, item, settings, rule_version = _running_repository(tmp_path)
    seeds = [
        MarketScanSeed(f"{code}.SH", code, "SH", item.name, industry="白酒",
                       list_date="2001-08-27", metadata_source=item.metadata_source)
        for code in ("600519", "600520")
    ]
    repo.refresh_pending_metadata(run.id, seeds[:1])
    repo.seed_results(run.id, seeds[1:], excluded_count=0)
    rows = _rows(DATA_DATE, 80)
    rows[-5] = rows[-5].model_copy(update={"high": 1.0})
    hub = _Hub(rows)
    hub.settings = settings

    async def scenario() -> list[MarketScanResultWrite | BaseException]:
        evaluator = _evaluator(hub)
        return await asyncio.gather(
            *[_scan_one(evaluator, entry, _quote(code=entry.code) if quote_present or entry.code == "600520" else None,
                        rule_version, observed_at="2026-07-17 15:01:00") for entry in repo.pending_items(run.id)],
            return_exceptions=True,
        )

    outcomes = asyncio.run(scenario())
    raise_batch_outcome_error(outcomes)
    writes = [replace(result, quote_observed_at="2026-07-17 15:01:00")
              for result in outcomes if isinstance(result, MarketScanResultWrite)]
    updated = repo.save_result_batch(run.id, writes)
    bad = next(result for result in writes if result.symbol == "600519.SH")
    assert bad.status == "missing"
    assert "日K" in (bad.error or "") and "无效" in (bad.error or "")
    assert MARKET_SCAN_SKIP_EVIDENCE_KEY not in bad.score_details
    assert updated.total_count == 2
    assert updated.success_count == 1
    assert updated.missing_count == 1
    assert updated.skipped_count == 0
    assert updated.coverage_pct == 50.0


def test_truly_absent_session_keeps_its_existing_verified_skip_contract(tmp_path: Path) -> None:
    repo, run, item, settings, rule_version = _running_repository(tmp_path)
    repo.refresh_pending_metadata(run.id, [MarketScanSeed(
        item.symbol, item.code, item.market, item.name, industry="白酒",
        list_date="2001-08-27", metadata_source=item.metadata_source,
    )])
    item = repo.pending_items(run.id)[0]
    rows = _rows(DATA_DATE, 80)
    rows.pop(-5)
    hub = _Hub(rows)
    hub.settings = settings

    result = asyncio.run(_scan_one(_evaluator(hub), item, _quote(), rule_version, observed_at="2026-07-17 15:01:00"))

    assert result.status == "skipped"
    assert MARKET_SCAN_SKIP_EVIDENCE_KEY in result.score_details
    updated = repo.save_result_batch(run.id, [result])
    assert updated.skipped_count == 1
    assert updated.missing_count == 0


def test_invalid_completed_bar_policy_changes_run_admission_without_changing_score_math() -> None:
    current = _rule_contract()
    old = deepcopy(current)
    old["input_admission"]["contract_version"] = "market-scan-input-admission-v2"
    old["input_admission"].pop("invalid_completed_daily_bars", None)

    assert market_scan_input_admission_spec()["contract_version"] == "market-scan-input-admission-v5"
    assert stable_score_spec_hash(current) != stable_score_spec_hash(old)
    assert stable_score_spec_hash(current["score_spec"]) == stable_score_spec_hash(old["score_spec"])
    _assert_current_score_contract_and_frozen_dimension_v4(current["score_spec"])
    with sqlite3.connect(":memory:") as conn:
        with pytest.raises(ValueError, match="准入"):
            register_market_scan_rule_contract(
                conn, rule_version=f"full-market-scan-v6:{stable_score_spec_hash(old)}",
                contract=old, stamp=AS_OF.isoformat(),
            )
