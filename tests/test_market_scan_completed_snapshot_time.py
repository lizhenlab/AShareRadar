"""Explicit intraday bar snapshots cannot become completed-session evidence."""

from __future__ import annotations

import pytest

from app.models.market_scan import MarketScanMode
from app.services.market_scan_scoring import MarketScanDataMissing
from tests.test_market_scan_input_admission import _assert_current_score_contract_and_frozen_dimension_v4, _score
from tests.test_market_scan_scoring import DATA_DATE, _rows


@pytest.mark.parametrize("mode", ["official", "preopen", "intraday"])
@pytest.mark.parametrize("timestamp", ["2026-07-17T00:00:00", "2026-07-17T12:00:00",
                                       "2026-07-17T06:59:59+00:00"])
def test_same_day_explicit_intraday_snapshot_cannot_prove_completed_daily_bar(
    mode: MarketScanMode,
    timestamp: str,
) -> None:
    rows = [row.model_copy(update={"as_of": timestamp}) for row in _rows(DATA_DATE, 80)]

    with pytest.raises(MarketScanDataMissing, match="日K快照.*收盘"):
        _score(rows=rows, mode=mode)


@pytest.mark.parametrize("mode", ["official", "preopen", "intraday"])
@pytest.mark.parametrize("timestamp", ["2026-07-17", "2026-07-17T15:00:00", "2026-07-17T07:00:00+00:00"])
def test_retained_coarse_date_and_completed_explicit_snapshot_preserve_v5_score(
    mode: MarketScanMode,
    timestamp: str,
) -> None:
    baseline = _score(mode=mode)
    rows = [row.model_copy(update={"as_of": timestamp}) for row in _rows(DATA_DATE, 80)]
    result = _score(rows=rows, mode=mode)

    assert result.raw_score == baseline.raw_score
    assert result.score == baseline.score
    assert result.score_details["score_spec_hash"] == baseline.score_details["score_spec_hash"]
    _assert_current_score_contract_and_frozen_dimension_v4(result.score_details["score_spec"])
    assert result.score_details["score_spec_hash"] == "2fca8cd2e3a6dbefbb2c424dd7fb07d7ed8e1b88c35334e8e1256d9d5e1d7f88"


def test_known_incomplete_historical_bar_is_rejected_even_outside_recent_features() -> None:
    rows = [row.model_copy(update={"as_of": f"{row.date}T15:00:00"}) for row in _rows(DATA_DATE, 80)]
    rows[0] = rows[0].model_copy(update={"as_of": f"{rows[0].date}T12:00:00"})

    with pytest.raises(MarketScanDataMissing, match="日K快照.*收盘"):
        _score(rows=rows)


def test_new_admission_identity_does_not_change_score_identity() -> None:
    from copy import deepcopy
    import sqlite3

    from app.repositories.market_scan_rule_contracts import register_market_scan_rule_contract
    from app.services.market_scan_scoring import stable_score_spec_hash
    from tests.test_market_scan_input_admission import _rule_contract
    from tests.test_market_scan_scoring import AS_OF

    current = _rule_contract()
    prior = deepcopy(current)
    prior["input_admission"]["contract_version"] = "market-scan-input-admission-v3"
    prior["input_admission"].pop("completed_daily_snapshot_time")
    assert stable_score_spec_hash(current) != stable_score_spec_hash(prior)
    assert current["score_spec"] == prior["score_spec"]
    with sqlite3.connect(":memory:") as conn:
        with pytest.raises(ValueError, match="准入"):
            register_market_scan_rule_contract(
                conn, rule_version=f"full-market-scan-v6:{stable_score_spec_hash(prior)}",
                contract=prior, stamp=AS_OF.isoformat(),
            )


def test_historical_explicit_intraday_snapshot_replay_does_not_apply_new_admission(monkeypatch) -> None:
    from copy import deepcopy

    from app.services import market_scan_scoring as scoring
    from tests.test_market_scan_raw_score_replay import _snapshot

    original_score = scoring.score_market_scan_item

    def previous_admission(item, quote, rows, **kwargs):
        historical_rows = [row.model_copy(update={"as_of": "2026-07-17T12:00:00"}) for row in rows]
        return original_score(item, quote, historical_rows, **kwargs)

    with monkeypatch.context() as patch:
        patch.setattr(scoring, "_require_completed_snapshot_time", lambda *_args: None)
        patch.setattr(scoring, "score_market_scan_item", previous_admission)
        item, run = _snapshot(patch, mutation="none", mode="official")
    frozen = deepcopy(item.score_details)
    scoring.verify_persisted_market_scan_result(item, run)
    assert item.score_details == frozen


def test_real_scan_keeps_incomplete_snapshot_missing_without_justified_skip(tmp_path) -> None:
    import asyncio

    from app.services.market_scan_skip_contract import MARKET_SCAN_SKIP_EVIDENCE_KEY
    from tests.test_market_scan_failure_isolation import _Hub, _evaluator, _scan_one
    from tests.test_market_scan_scoring import _quote
    from tests.test_market_scan_skip_contract import _running_repository

    repo, run, item, settings, rule_version = _running_repository(tmp_path)
    rows = [row.model_copy(update={"as_of": "2026-07-17T12:00:00"}) for row in _rows(DATA_DATE, 80)]
    hub = _Hub(rows)
    hub.settings = settings
    result = asyncio.run(_scan_one(_evaluator(hub), item, _quote(), rule_version,
                                  observed_at="2026-07-17 15:01:00"))
    assert result.status == "missing"
    assert "收盘" in (result.error or "")
    assert MARKET_SCAN_SKIP_EVIDENCE_KEY not in result.score_details
    updated = repo.save_result_batch(run.id, [result])
    assert updated.total_count == 1
    assert updated.missing_count == 1 and updated.skipped_count == 0
    assert updated.success_count == 0
