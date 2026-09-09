from __future__ import annotations

from copy import deepcopy

import pytest

from app.repositories import market_scan_verified_read as verified_read
from app.services.cache import SQLiteCache
from app.services.market_scan_export import MarketScanExportFilters
from app.services.market_scan_probability_ranking import (
    PROBABILITY_RANKING_SCORE_RULE_VERSION,
    probability_ranking_score_spec_hash,
)
from app.services.market_scan_query_service import MarketScanQueryService
from app.services.market_scan_research_stores import MarketScanResearchStores
from tests.test_market_scan_query_service import _ranking_record
from tests.test_market_scan_skip_contract import _valid_action_source_run
from tests.test_market_scan_snapshot_integrity import _result_query


@pytest.fixture(scope="module")
def published_ranking_source(tmp_path_factory):
    _repo, settings, run_id, _skip, _diagnostics = _valid_action_source_run(tmp_path_factory.mktemp("ranking-race"))
    cache = SQLiteCache(settings=settings)
    query = {**_result_query(), "page_size": 101}
    page = _query_service(cache).results(run_id, **query)
    records = {}
    for item in page.items:
        if item.status != "success":
            continue
        adjustment = 6.0 if item.rank % 2 else -6.0
        raw = min(100.0, max(0.0, item.raw_score + adjustment))
        records[item.symbol] = _ranking_record(
            item.symbol, base_rank=item.rank, base_score=item.score, base_raw_score=item.raw_score,
            rank=item.rank, score=round(raw), raw_score=raw, adjustment=adjustment,
        )
        records[item.symbol]["run_id"] = run_id
    for rank, record in enumerate(sorted(records.values(), key=lambda row: (-row["raw_score"], row["symbol"])), 1):
        record["rank"] = rank
    context = {
        "contract_version": "market-scan-probability-ranking-projection-v1",
        "status": "active", "run_id": run_id,
        "score_rule_version": PROBABILITY_RANKING_SCORE_RULE_VERSION,
        "score_spec_hash": probability_ranking_score_spec_hash(),
        "artifact_digest": "d" * 64, "promotion_digest": "e" * 64,
        "record_count": len(records), "base_snapshot_digest": page.run.snapshot_digest,
        "base_v5_mutated": False, "historical_ranks_mutated": False,
    }
    return cache, page.run, context, records


class _CapturedRankingStore:
    def __init__(self, context, records, status):
        self.context = context
        self.records = records
        self.status = status

    def production_ranking_projection(self, run_id):
        assert run_id == self.context["run_id"]
        return deepcopy(self.context), deepcopy(self.records)

    def status_projection(self):
        return {"status": self.status, "filter_ready": False, "blockers": [self.status]}


def _query_service(cache, joint=None):
    return MarketScanQueryService(cache, MarketScanResearchStores(
        probability=None, probability_source=None, future_range=None, joint_probability=joint,
    ))


def _count_page_reads(monkeypatch):
    reads = []
    original = verified_read._verified_market_scan_result_page

    def counted(*args, **query):
        reads.append(query)
        return original(*args, **query)

    monkeypatch.setattr(verified_read, "_verified_market_scan_result_page", counted)
    return reads


@pytest.mark.parametrize("status", ["maintenance_pending", "maintenance_failed"])
@pytest.mark.parametrize("query_changes", [
    {"page": 1, "page_size": 7},
    {"page": 2, "page_size": 7, "min_score": 60, "max_score": 90, "market": ("SH", "SZ")},
    {"page": 2, "page_size": 5, "sort": ("score", "symbol"), "order": ("desc", "asc")},
    {"page": 1, "page_size": 5, "sort": ("change_pct", "rank"), "order": ("asc", "desc")},
    {"page": 100, "page_size": 7},
    {"status": "skipped", "page_size": 2},
    {"min_score": 100, "max_score": 100},
])
def test_maintenance_fallback_matches_base_sql_with_one_verified_read(
    published_ranking_source, monkeypatch, status, query_changes,
):
    cache, run, context, records = published_ranking_source
    query = {**_result_query(), **query_changes}
    expected = _query_service(cache).results(run.id, **query)
    reads = _count_page_reads(monkeypatch)
    service = _query_service(cache, _CapturedRankingStore(context, records, status))

    actual = service.results(run.id, **query)

    assert len(reads) == 1
    assert [item.model_dump() for item in actual.items] == [item.model_dump() for item in expected.items]
    assert (actual.total, actual.page, actual.page_size, actual.page_count) == (
        expected.total, expected.page, expected.page_size, expected.page_count,
    )
    assert actual.production_ranking["status"] == "inactive"
    assert actual.production_ranking["reason"] == status
    assert actual.probability_research["availability"] == status


@pytest.mark.parametrize("status", ["maintenance_pending", "maintenance_failed"])
def test_export_maintenance_fallback_keeps_complete_filtered_base_set(published_ranking_source, monkeypatch, status):
    cache, run, context, records = published_ranking_source
    filters = MarketScanExportFilters(min_score=60, market=("SH", "SZ"), sort=("score", "symbol"), order=("desc", "asc"))
    expected, _ = _query_service(cache).export_projection(run.id, filters=filters)
    reads = _count_page_reads(monkeypatch)

    actual, _ = _query_service(cache, _CapturedRankingStore(context, records, status)).export_projection(run.id, filters=filters)

    assert len(reads) == 1 and actual.total == len(actual.items) == expected.total
    assert [item.model_dump() for item in actual.items] == [item.model_dump() for item in expected.items]
    assert actual.production_ranking["reason"] == status


def test_stable_ranking_remains_active_with_one_real_snapshot_read(published_ranking_source, monkeypatch):
    cache, run, context, records = published_ranking_source
    reads = _count_page_reads(monkeypatch)
    page = _query_service(cache, _CapturedRankingStore(context, records, "ranking_active")).results(run.id, **_result_query())

    assert len(reads) == 1
    assert page.production_ranking["status"] == "active"
    assert [item.symbol for item in page.items if item.status == "success"] == [
        record["symbol"] for record in sorted(records.values(), key=lambda row: row["rank"])
    ]
    assert all(item.base_production_score is not None for item in page.items if item.status == "success")
