"""Completed market-scan snapshots keep their trend context across the close."""

from copy import deepcopy
import asyncio
from contextlib import closing
from dataclasses import fields
from datetime import datetime
import json
import gzip
from pathlib import Path
import sqlite3
from types import SimpleNamespace

import pytest

from app.models.market import Kline, Quote
from app.config import Settings
from app.db.market_scan_integrity import seal_market_scan_snapshot
from app.models.market_scan import (
    MarketScanMode, MarketScanProductionScoreContract, MarketScanResultItem,
    MarketScanResultWrite, MarketScanRun,
)
from app.repositories.market_scan import MarketScanSeed
from app.repositories.market_scan_rule_contracts import register_market_scan_rule_contract
from app.repositories.market_scan_results import assign_result_ranks
from app.services.cache import SQLiteCache
from app.services.indicator_trend import trend_score
from app.services.market_scan_manager import market_scan_rule_contract
from app.services.market_scan_probability_capture import process_market_scan_probability_capture_outbox
from app.services.market_scan_probability_source import (
    is_current_writable_production_score_contract,
    is_registered_production_score_contract,
)
import app.services.market_scan_probability_source as probability_source
from app.services.market_scan_score_contract import (
    MARKET_SCAN_LEGACY_TREND_ALGORITHM_VERSION,
    MARKET_SCAN_TREND_ALGORITHM_VERSION,
    market_scan_trend_context_mode,
)
from app.services.market_scan_scoring import (
    MarketScanReplayError,
    is_current_market_scan_score_spec,
    market_scan_score_spec,
    market_scan_score_spec_legacy_v5,
    market_scan_score_spec_v4,
    replay_score_details,
    score_market_scan_item,
    stable_score_spec_hash,
    verify_persisted_market_scan_result,
)
from tests.test_market_scan_repository import _results
from tests.test_market_scan_probability_capture import _OutboxFakeCache, _run as _capture_run
from tests.test_market_scan_scoring import (
    AS_OF, DATA_DATE, PREOPEN_AS_OF, _item, _quote, _rows,
)


def _snapshot_inputs(change_pct: float, recent_volume: float) -> tuple[Quote, list[Kline]]:
    quote = _quote()
    previous_close = quote.price / (1 + change_pct / 100)
    quote = quote.model_copy(update={
        "prev_close": previous_close,
        "change": quote.price - previous_close,
        "change_pct": change_pct,
    })
    rows = _rows(DATA_DATE, 80)
    rows = [
        row.model_copy(update={"volume": recent_volume if index >= len(rows) - 5 else 1_000_000})
        for index, row in enumerate(rows)
    ]
    rows[-2] = rows[-2].model_copy(update={
        "open": previous_close, "close": previous_close,
        "high": previous_close * 1.01, "low": previous_close * .99,
    })
    return quote, rows


def _score(quote: Quote, rows: list[Kline], *, mode: MarketScanMode, as_of: datetime):
    return score_market_scan_item(
        _item(), quote, rows, as_of=as_of, mode=mode,
        completed_cutoff=DATA_DATE, expected_data_date=DATA_DATE,
        expected_quote_date=DATA_DATE, min_history_rows=61, min_data_quality_score=50,
    )


@pytest.mark.parametrize("change_pct,recent_volume", [
    (5.0, 5_000_000), (-5.0, 5_000_000), (5.0, 200_000),
])
def test_previous_completed_snapshot_preserves_all_rank_inputs(change_pct, recent_volume):
    quote, rows = _snapshot_inputs(change_pct, recent_volume)
    official = _score(quote, rows, mode="official", as_of=AS_OF)
    preopen = _score(quote, rows, mode="preopen", as_of=PREOPEN_AS_OF)

    assert preopen.trend_score == official.trend_score
    assert preopen.leader_score == official.leader_score
    assert preopen.raw_score == official.raw_score
    assert preopen.score == official.score
    assert preopen.score_details["inputs"] == official.score_details["inputs"]
    assert preopen.data_date == official.data_date
    assert preopen.quote_timestamp == official.quote_timestamp


def _legacy_snapshot() -> tuple[MarketScanResultItem, MarketScanRun]:
    payload = json.loads((Path(__file__).parent / "fixtures/market_scan_preopen_legacy_v5.json").read_text())
    return MarketScanResultItem.model_validate(payload["item"]), MarketScanRun.model_validate(payload["run"])


def test_real_frozen_legacy_preopen_replays_old_mode_and_registered_hash():
    item, run = _legacy_snapshot()
    original = deepcopy(item.score_details)
    spec_hash = item.score_details["score_spec_hash"]
    assert spec_hash == "62176a5cffa6d248da3841617fda2f7aad40c9042d8a77c10955682d53e1c486"
    assert item.trend_score == 88
    assert replay_score_details(item.score_details).raw_score == item.raw_score
    verify_persisted_market_scan_result(
        item, run, expected_score_rule_version="full-market-score-v5",
        expected_score_spec_hash=spec_hash,
    )
    assert item.score_details == original


def test_legacy_algorithm_cannot_be_relabelled_as_current_with_a_new_hash():
    item, run = _legacy_snapshot()
    details = item.score_details
    details["score_spec"] = market_scan_score_spec(min_data_quality_score=50)
    details["score_spec_hash"] = stable_score_spec_hash(details["score_spec"])
    # Its downstream arithmetic is still self-consistent; raw-input replay
    # must notice that the new completed-snapshot algorithm would produce 91.
    assert replay_score_details(details).raw_score == item.raw_score
    with pytest.raises(MarketScanReplayError, match="逐时点证据"):
        verify_persisted_market_scan_result(
            item, run, expected_score_rule_version="full-market-score-v5",
            expected_score_spec_hash=details["score_spec_hash"],
        )


def test_old_score_hash_stays_readable_but_cannot_be_registered_for_new_production():
    legacy = market_scan_score_spec_legacy_v5(min_data_quality_score=50)
    legacy_hash = stable_score_spec_hash(legacy)
    current = market_scan_score_spec(min_data_quality_score=50)
    current_hash = stable_score_spec_hash(current)
    assert legacy_hash != current_hash
    assert is_registered_production_score_contract("full-market-score-v5", legacy_hash)
    assert not is_current_writable_production_score_contract("full-market-score-v5", legacy_hash)
    assert not is_current_market_scan_score_spec(legacy, legacy_hash)
    assert is_current_writable_production_score_contract("full-market-score-v5", current_hash)
    assert is_current_market_scan_score_spec(current, current_hash)
    assert stable_score_spec_hash(market_scan_score_spec_v4(min_data_quality_score=50)) == (
        "30c5abb10b676fc71b5fa6c621cce809a6c2d054113fa578d77eccf28fb5955a"
    )
    settings = SimpleNamespace(
        market_scan_min_data_quality_score=50, market_scan_kline_limit=120,
        market_scan_min_history_rows=61, market_scan_new_stock_days=120,
    )
    contract = market_scan_rule_contract(settings)
    contract["score_spec"] = legacy
    with sqlite3.connect(":memory:") as conn, pytest.raises(ValueError, match="当前可写"):
        register_market_scan_rule_contract(
            conn, rule_version=f"full-market-scan-v6:{stable_score_spec_hash(contract)}",
            contract=contract, stamp=AS_OF.isoformat(),
        )


def test_individual_preopen_and_intraday_still_neutralize_unaligned_volume():
    quote, rows = _snapshot_inputs(5.0, 5_000_000)
    assert trend_score(quote, rows, mode="official")[0] == 89
    assert trend_score(quote, rows, mode="preopen")[0] == 86
    assert trend_score(quote, rows, mode="intraday")[0] == 86
    for algorithm in (MARKET_SCAN_LEGACY_TREND_ALGORITHM_VERSION, MARKET_SCAN_TREND_ALGORITHM_VERSION):
        assert market_scan_trend_context_mode("intraday", algorithm_version=algorithm) == "intraday"
        assert market_scan_trend_context_mode("official", algorithm_version=algorithm) == "official"


@pytest.mark.parametrize("algorithm", [None, "unknown", "trend-score-v999"])
def test_unknown_context_algorithm_cannot_assume_completed_snapshot(algorithm):
    with pytest.raises(MarketScanReplayError, match="未知全市场趋势算法"):
        market_scan_trend_context_mode("preopen", algorithm_version=algorithm)


def _load_legacy_read_fixture(tmp_path: Path, *, snapshot=None):
    item, frozen_run = snapshot if snapshot is not None else _legacy_snapshot()
    path = tmp_path / "legacy-scan.sqlite3"
    repo = SQLiteCache(settings=Settings(_env_file=None, cache_path=path, scheduler_enabled=False)).market_scan_repo
    run = repo.create_run(
        trigger="manual", mode=frozen_run.mode, rule_version="legacy-import-fixture",
        as_of=frozen_run.as_of, data_date=frozen_run.data_date,
        quote_date=frozen_run.quote_date, scope=frozen_run.scope,
    )
    repo.start_run(run.id)
    repo.seed_results(run.id, [MarketScanSeed(
        item.symbol, item.code, item.market, item.name, item.industry, item.list_date,
    )], excluded_count=0)
    values = item.model_dump()
    write = MarketScanResultWrite(**{field.name: values[field.name] for field in fields(MarketScanResultWrite)})
    repo.save_result_batch(run.id, [write])
    with closing(sqlite3.connect(path)) as conn, conn:
        conn.row_factory = sqlite3.Row
        assign_result_ranks(conn, run.id)
        conn.execute(
            "UPDATE market_scan_run SET rule_version = ?, status = 'degraded', "
            "quote_capture_started_at = ?, quote_capture_finished_at = ?, "
            "quote_capture_duration_ms = 0, quote_capture_count = 1, "
            "current_stage = NULL, stage_started_at = NULL WHERE id = ?",
            (frozen_run.rule_version, frozen_run.as_of, frozen_run.as_of, run.id),
        )
        conn.execute(
            "INSERT INTO market_scan_rule_contract VALUES (?, ?, ?, ?, ?)",
            (frozen_run.rule_version, "{}", "full-market-score-v5", item.score_details["score_spec_hash"], frozen_run.as_of),
        )
        seal_market_scan_snapshot(conn, run.id, origin="legacy_backfill")
    return repo, run.id, path


def test_sqlite_sealed_historical_read_uses_its_registered_legacy_hash(tmp_path):
    repo, run_id, path = _load_legacy_read_fixture(tmp_path)
    before = path.read_bytes()
    page = _results(repo, run_id)
    assert len(page.items) == 1
    assert page.items[0].trend_score == 88
    assert page.items[0].raw_score == 89.3033
    assert page.run.snapshot_seal_origin == "legacy_backfill"
    assert page.items[0].score_details == _legacy_snapshot()[0].score_details
    assert path.read_bytes() == before


def test_old_v5_official_capture_is_skipped_without_retry_or_promotion(tmp_path):
    run = _capture_run(71)
    legacy_hash = stable_score_spec_hash(market_scan_score_spec_legacy_v5(min_data_quality_score=50))
    cache = _OutboxFakeCache(
        tmp_path, run, candidates=[run], items=[object(), object()],
        score_contract=MarketScanProductionScoreContract("full-market-score-v5", legacy_hash, run.success_count),
    )
    summary = asyncio.run(process_market_scan_probability_capture_outbox(cache, owner="old-v5", limit=1))
    assert summary == {"captured": 0, "skipped": 1, "failed": 0}
    assert cache.finished[0]["status"] == "skipped"
    assert "历史只读评分合同" in cache.finished[0]["message"]
    assert cache.retried == []
    assert cache.events == [(
        "info", "research", f"上涨概率PIT样本归档跳过：{cache.finished[0]['message']}", None,
    )]


def _legacy_source(monkeypatch):
    # Match the compact, explicitly synthetic population used by the original
    # producer; score/spec/digest and record validation remain production code.
    scopes = ("ALL", "SH", "SZ", "BJ")
    monkeypatch.setattr(probability_source, "PROBABILITY_SOURCE_MINIMUM_POPULATION", dict.fromkeys(scopes, 1))
    monkeypatch.setattr(probability_source, "PROBABILITY_SOURCE_MINIMUM_COVERAGE", dict.fromkeys(scopes, 0.0))
    monkeypatch.setattr(probability_source, "PROBABILITY_SOURCE_MINIMUM_ELIGIBLE_RATIO", dict.fromkeys(scopes, 0.0))
    return json.loads((Path(__file__).parent / "fixtures/market_scan_probability_source_v3_legacy_v5.json").read_text())


def test_frozen_v3_source_retains_legacy_v5_readability_and_integrity(monkeypatch, tmp_path):
    artifact = _legacy_source(monkeypatch)
    original = deepcopy(artifact)
    assert probability_source.verify_probability_source_snapshot(artifact) == artifact
    path = tmp_path / probability_source.probability_source_snapshot_filename(70, artifact)
    path.write_bytes(gzip.compress(probability_source.canonical_probability_source_json(artifact).encode(), mtime=0))
    assert probability_source.load_probability_source_snapshot(path) == artifact
    assert artifact == original


@pytest.mark.parametrize("mutation", ["v4", "unknown_hash", "changed_record"])
def test_frozen_v3_readability_does_not_accept_another_family_or_damaged_record(monkeypatch, mutation):
    artifact = _legacy_source(monkeypatch)
    payload = artifact["payload"]
    if mutation == "v4":
        payload["run"]["production_score_rule_version"] = "full-market-score-v4"
        payload["run"]["production_score_spec_hash"] = stable_score_spec_hash(market_scan_score_spec_v4(min_data_quality_score=50))
        payload["score_semantics"]["production_rule_version"] = "full-market-score-v4"
        payload["score_semantics"]["production_score_spec_hash"] = payload["run"]["production_score_spec_hash"]
        message = "只接受已注册的 v5"
    elif mutation == "unknown_hash":
        payload["run"]["production_score_spec_hash"] = "f" * 64
        message = "未注册"
    else:
        payload["records"][0]["source_rank"] = 999
        message = ".*"
    artifact["integrity"]["integrity_digest"] = probability_source.probability_source_payload_digest(payload)
    with pytest.raises(probability_source.ProbabilitySourceError, match=message):
        probability_source.verify_probability_source_snapshot(artifact)


def test_legacy_v5_source_run_still_cannot_enter_the_new_builder(monkeypatch):
    artifact = _legacy_source(monkeypatch)
    with pytest.raises(probability_source.ProbabilitySourceError, match="未注册"):
        probability_source._normalize_run(  # noqa: SLF001
            artifact["payload"]["run"], captured_at=artifact["captured_at"], exact_keys=False,
        )
