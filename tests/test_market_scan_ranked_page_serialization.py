from __future__ import annotations

from copy import deepcopy

import pytest

from app.models.market_scan import MarketScanResultPage
from app.services import market_scan_query_service as query_module
from tests import test_market_scan_ranking_maintenance_snapshot as ranking_fixtures
from tests.test_market_scan_ranking_maintenance_snapshot import (
    _CapturedRankingStore,
    _count_page_reads,
    _query_service,
)
from tests.test_market_scan_snapshot_integrity import _result_query


published_ranking_source = ranking_fixtures.published_ranking_source


def _count_serialized_page_items(monkeypatch):
    original = MarketScanResultPage.model_dump
    counts = []

    def counted(self, *args, **kwargs):
        payload = original(self, *args, **kwargs)
        counts.append(len(payload.get("items", [])))
        return payload

    monkeypatch.setattr(MarketScanResultPage, "model_dump", counted)
    return counts


@pytest.mark.parametrize(("page", "page_size"), [(1, 7), (2, 7), (15, 7), (100, 7), (1, 101)])
def test_ranked_page_serializes_only_returned_items(published_ranking_source, monkeypatch, page, page_size):
    cache, run, _context, _records = published_ranking_source
    base = _query_service(cache).results(run.id, **{**_result_query(), "page_size": 101})
    ordered = list(reversed(base.items))
    expected_items = ordered[(page - 1) * page_size : page * page_size]
    counts = _count_serialized_page_items(monkeypatch)

    result = query_module._ranked_result_page(
        base, ordered, {"status": "inactive"}, page=page, page_size=page_size,
    )
    payload = result.model_dump(mode="python")

    assert result.items == expected_items
    assert payload["total"] == len(ordered)
    assert payload["page_count"] == (len(ordered) + page_size - 1) // page_size
    # Count actual result dictionaries produced by the real Pydantic serializer,
    # including final response serialization. Full-cohort ranking is separate.
    assert sum(counts) == len(expected_items)


@pytest.mark.parametrize("status", ["ranking_active", "maintenance_pending", "maintenance_failed"])
@pytest.mark.parametrize("query_changes", [
    {"page": 1, "page_size": 7},
    {"page": 2, "page_size": 7, "min_score": 60, "max_score": 90, "market": ("SH", "SZ")},
    {"page": 2, "page_size": 5, "sort": ("score", "symbol"), "order": ("desc", "asc")},
    {"page": 100, "page_size": 7},
    {"status": "skipped", "page_size": 2},
    {"min_score": 100, "max_score": 100},
])
def test_ranked_query_and_maintenance_fallback_dump_no_off_page_items(
    published_ranking_source, monkeypatch, status, query_changes,
):
    cache, run, context, records = published_ranking_source
    query = {**_result_query(), **query_changes}
    service = _query_service(cache, _CapturedRankingStore(context, records, status))
    reference_service = service if status == "ranking_active" else _query_service(cache)
    full = reference_service.results(run.id, **{**query, "page": 1, "page_size": 101})
    start = (query["page"] - 1) * query["page_size"]
    expected_items = full.items[start : start + query["page_size"]]
    reads = _count_page_reads(monkeypatch)
    counts = _count_serialized_page_items(monkeypatch)

    result = service.results(run.id, **query)
    payload = result.model_dump(mode="python")

    assert len(reads) == 1
    assert payload["items"] == [item.model_dump(mode="python") for item in expected_items]
    assert result.total == full.total
    assert result.page == query["page"] and result.page_size == query["page_size"]
    assert sum(counts) == len(result.items)
    if status == "ranking_active":
        assert result.production_ranking["status"] == "active"
        assert all(item.base_production_score is not None for item in result.items if item.status == "success")
    else:
        assert result.production_ranking["reason"] == status
        assert result.probability_research["availability"] == status


@pytest.mark.parametrize("violation", ["foreign_run", "wrong_date", "duplicate_symbol"])
def test_ranked_page_still_validates_every_returned_item(published_ranking_source, violation):
    cache, run, _context, _records = published_ranking_source
    base = _query_service(cache).results(run.id, **{**_result_query(), "page_size": 101})
    ordered = deepcopy(base.items[:2])
    if violation == "foreign_run":
        ordered[0].run_id = run.id + 1
    elif violation == "wrong_date":
        ordered[0].data_date = "2000-01-01"
    else:
        ordered[1] = ordered[0]

    with pytest.raises(ValueError):
        query_module._ranked_result_page(base, ordered, {"status": "inactive"}, page=1, page_size=2)
