"""Frozen rounded-MA v3 artifacts remain readable without becoming new writes."""

import asyncio
from copy import deepcopy
import gzip
import json
from pathlib import Path
from types import SimpleNamespace
import sqlite3

import pytest

from app.models.market_scan import MarketScanProductionScoreContract, MarketScanResultItem, MarketScanRun
from app.repositories.market_scan_rule_contracts import register_market_scan_rule_contract
from app.services.market_scan_manager import market_scan_rule_contract
from app.services.market_scan_probability_capture import process_market_scan_probability_capture_outbox
import app.services.market_scan_probability_source as source
from app.services.market_scan_research_challengers import prepare_research_scores
from app.services.market_scan_score_contract import (
    MARKET_SCAN_LEGACY_TREND_ALGORITHM_VERSION,
    MARKET_SCAN_ROUNDED_MA_TREND_ALGORITHM_VERSION,
    MARKET_SCAN_TREND_ALGORITHM_VERSION,
    market_scan_shared_trend_algorithm,
    market_scan_trend_context_mode,
)
from app.services.indicator_trend import TREND_SCORE_ALGORITHM_VERSION, TREND_SCORE_LEGACY_ALGORITHM_VERSION
from app.services.market_scan_scoring import (
    MarketScanReplayError, is_current_market_scan_score_spec, market_scan_score_spec,
    market_scan_score_spec_trend_v3, replay_score_details, stable_score_spec_hash,
    verify_persisted_market_scan_result,
)
from tests.test_market_scan_completed_snapshot_trend import _load_legacy_read_fixture
from tests.test_market_scan_probability_capture import _OutboxFakeCache, _run
from tests.test_market_scan_repository import _results


OLD_V3_HASH = "050522861315ee430ee8b9d3e147d572eabc7b237c006c8082a477d3970e2543"
FIXTURES = Path(__file__).parent / "fixtures"


def _frozen_score():
    value = json.loads((FIXTURES / "market_scan_trend_v3_rounded_ma.json").read_text())
    return MarketScanResultItem.model_validate(value["item"]), MarketScanRun.model_validate(value["run"])


def _frozen_source(monkeypatch):
    scopes = ("ALL", "SH", "SZ", "BJ")
    monkeypatch.setattr(source, "PROBABILITY_SOURCE_MINIMUM_POPULATION", dict.fromkeys(scopes, 1))
    monkeypatch.setattr(source, "PROBABILITY_SOURCE_MINIMUM_COVERAGE", dict.fromkeys(scopes, 0.0))
    monkeypatch.setattr(source, "PROBABILITY_SOURCE_MINIMUM_ELIGIBLE_RATIO", dict.fromkeys(scopes, 0.0))
    return json.loads((FIXTURES / "market_scan_probability_source_trend_v3.json").read_text())


def test_real_baseline_v3_artifact_replays_rounded_ma_and_original_hash():
    item, run = _frozen_score()
    before = deepcopy(item.score_details)
    assert item.trend_score == 86
    assert item.score_details["score_spec_hash"] == OLD_V3_HASH
    assert stable_score_spec_hash(market_scan_score_spec_trend_v3(min_data_quality_score=50)) == OLD_V3_HASH
    assert replay_score_details(item.score_details).raw_score == item.raw_score
    verify_persisted_market_scan_result(
        item, run, expected_score_rule_version="full-market-score-v5", expected_score_spec_hash=OLD_V3_HASH,
    )
    assert item.score_details == before


def test_baseline_v3_sealed_sqlite_read_is_read_only(tmp_path):
    item, _run_item = _frozen_score()
    repo, run_id, path = _load_legacy_read_fixture(tmp_path, snapshot=_frozen_score())
    before = path.read_bytes()
    page = _results(repo, run_id)
    assert page.items[0].trend_score == 86
    assert page.items[0].raw_score == item.raw_score
    assert page.items[0].score_details == item.score_details
    assert path.read_bytes() == before


def test_rounded_ma_inputs_cannot_be_relabelled_with_current_hash():
    item, run = _frozen_score()
    details = item.score_details
    details["score_spec"] = market_scan_score_spec(min_data_quality_score=50)
    details["score_spec_hash"] = stable_score_spec_hash(details["score_spec"])
    assert replay_score_details(details).raw_score == item.raw_score
    with pytest.raises(MarketScanReplayError, match="逐时点证据"):
        verify_persisted_market_scan_result(
            item, run, expected_score_rule_version="full-market-score-v5",
            expected_score_spec_hash=details["score_spec_hash"],
        )


def test_registered_v3_hash_cannot_become_new_writable_contract():
    old = market_scan_score_spec_trend_v3(min_data_quality_score=50)
    current = market_scan_score_spec(min_data_quality_score=50)
    assert source.is_registered_production_score_contract("full-market-score-v5", OLD_V3_HASH)
    assert not source.is_current_writable_production_score_contract("full-market-score-v5", OLD_V3_HASH)
    assert not is_current_market_scan_score_spec(old, OLD_V3_HASH)
    assert is_current_market_scan_score_spec(current, stable_score_spec_hash(current))
    settings = SimpleNamespace(
        market_scan_min_data_quality_score=50, market_scan_kline_limit=120,
        market_scan_min_history_rows=61, market_scan_new_stock_days=120,
    )
    contract = market_scan_rule_contract(settings)
    contract["score_spec"] = old
    with sqlite3.connect(":memory:") as conn, pytest.raises(ValueError, match="当前可写"):
        register_market_scan_rule_contract(
            conn, rule_version=f"full-market-scan-v6:{stable_score_spec_hash(contract)}",
            contract=contract, stamp="2026-07-17T16:30:00",
        )


def test_baseline_v3_source_archive_retains_read_only_integrity(monkeypatch, tmp_path):
    artifact = _frozen_source(monkeypatch)
    before = deepcopy(artifact)
    assert artifact["payload"]["run"]["production_score_spec_hash"] == OLD_V3_HASH
    assert source.verify_probability_source_snapshot(artifact) == artifact
    path = tmp_path / source.probability_source_snapshot_filename(70, artifact)
    path.write_bytes(gzip.compress(source.canonical_probability_source_json(artifact).encode(), mtime=0))
    assert source.load_probability_source_snapshot(path) == artifact
    assert artifact == before
    with pytest.raises(source.ProbabilitySourceError, match="未注册"):
        source._normalize_run(artifact["payload"]["run"], captured_at=artifact["captured_at"], exact_keys=False)  # noqa: SLF001


def test_old_v3_capture_is_skipped_without_retry_or_retag(tmp_path):
    run = _run(71)
    cache = _OutboxFakeCache(
        tmp_path, run, candidates=[run], items=[object(), object()],
        score_contract=MarketScanProductionScoreContract("full-market-score-v5", OLD_V3_HASH, run.success_count),
    )
    summary = asyncio.run(process_market_scan_probability_capture_outbox(cache, owner="old-v3", limit=1))
    assert summary == {"captured": 0, "skipped": 1, "failed": 0}
    assert cache.finished[0]["status"] == "skipped"
    assert "历史只读评分合同" in cache.finished[0]["message"]
    assert cache.retried == []


def test_old_v3_research_comparison_rejects_before_rebuilding_current_contributions(monkeypatch):
    item, run = _frozen_score()
    monkeypatch.setattr(
        "app.services.market_scan_research_challengers._turnover_inputs",
        lambda _payload: pytest.fail("old hash reached current contribution builder"),
    )
    with pytest.raises(ValueError, match="frozen v5 quality threshold 50 specification"):
        prepare_research_scores([item], run)


@pytest.mark.parametrize("algorithm,shared,preopen", [
    (MARKET_SCAN_LEGACY_TREND_ALGORITHM_VERSION, TREND_SCORE_LEGACY_ALGORITHM_VERSION, "preopen"),
    (MARKET_SCAN_ROUNDED_MA_TREND_ALGORITHM_VERSION, TREND_SCORE_LEGACY_ALGORITHM_VERSION, "official"),
    (MARKET_SCAN_TREND_ALGORITHM_VERSION, TREND_SCORE_ALGORITHM_VERSION, "official"),
])
def test_each_frozen_algorithm_binds_its_mode_and_precision(algorithm, shared, preopen):
    assert market_scan_shared_trend_algorithm(algorithm) == shared
    assert market_scan_trend_context_mode("preopen", algorithm_version=algorithm) == preopen
    assert market_scan_trend_context_mode("official", algorithm_version=algorithm) == "official"
    assert market_scan_trend_context_mode("intraday", algorithm_version=algorithm) == "intraday"


@pytest.mark.parametrize("algorithm", [None, "unknown", []])
def test_unknown_full_market_precision_algorithm_fails_closed(algorithm):
    with pytest.raises(MarketScanReplayError, match="未知全市场趋势算法"):
        market_scan_shared_trend_algorithm(algorithm)
    with pytest.raises(MarketScanReplayError, match="未知全市场趋势算法"):
        market_scan_trend_context_mode("official", algorithm_version=algorithm)


@pytest.mark.parametrize("policy", [None, {}, {"relative_pct_decimals": 2}])
def test_current_algorithm_rejects_missing_or_altered_dimensionless_precision(policy):
    item, _run_item = _frozen_score()
    details = item.score_details
    details["score_spec"] = market_scan_score_spec(min_data_quality_score=50)
    details["score_spec"]["rounding"]["trend_calculation"] = policy
    details["score_spec_hash"] = stable_score_spec_hash(details["score_spec"])
    with pytest.raises(MarketScanReplayError, match="趋势计算精度合同"):
        replay_score_details(details)
