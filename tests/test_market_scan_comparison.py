from __future__ import annotations

from collections.abc import Iterator
from contextlib import contextmanager
from copy import deepcopy
import json
from pathlib import Path
import sqlite3
from types import SimpleNamespace
from typing import cast

from fastapi import FastAPI
from fastapi.exceptions import RequestValidationError
from fastapi.testclient import TestClient
from pydantic import ValidationError
import pytest

from app.api.deps import get_market_scan_heavy_read_admission, get_market_scanner
from app.api.errors import validation_exception_handler
from app.api.market_scan_read_admission import MarketScanHeavyReadAdmission
from app.api.routes import market_scan
from app.artifacts.io import canonical_json_bytes, sha256_hex
from app.db import market_scan_integrity
from app.db.market_scan_integrity import MarketScanSnapshotSealError, create_market_scan_immutability_triggers, market_scan_snapshot_digest
from app.models.market_scan import MarketScanResultItem, MarketScanResultPage, MarketScanRun
from app.models.market_scan_comparison import MarketScanComparisonRequest, MarketScanComparisonResponse
from app.models.market_scan_snapshot import MarketScanSnapshotIntegrityError
from app.services.market_scan_comparison import MarketScanComparisonConflict, MarketScanComparisonService, MarketScanComparisonUnavailable
from app.services.market_scan_contracts import MarketScanVerifiedReadProtocol
from app.services.market_scan_query_service import MarketScanQueryService
from app.services.market_scan_research_stores import MarketScanResearchStores
from tests.test_strategy_execution import _disable_market_scan_immutability, _environment


_FIXTURE = Path(__file__).parent / "fixtures" / "market_scan_comparison_v1.json"


def _request(run: MarketScanRun, symbols: list[str] | None = None) -> MarketScanComparisonRequest:
    assert run.snapshot_digest is not None
    return MarketScanComparisonRequest(symbols=symbols or ["688001.SH", "600001.SH"], expected_snapshot_digest=run.snapshot_digest)


def _rehashed(payload: dict[str, object]) -> dict[str, object]:
    payload["canonical_digest"] = sha256_hex(canonical_json_bytes({key: value for key, value in payload.items() if key != "canonical_digest"}))
    return payload


@pytest.mark.parametrize(
    "symbols",
    [[], ["600001.SH"], ["600001.SH"] * 2, ["600001.SH", "688001.SH", "300001.SZ", "920001.BJ", "000001.SZ"],
     ["600001.SH", "000001.sz"], ["600001.SH", "000001.US"], ["600001.SH", " 000001.SZ"],
     ["600001.SH", "０００００１.SZ"], ["600001.SH", 600002], "600001.SH,688001.SH"],
)
def test_comparison_request_rejects_invalid_selections(symbols: object) -> None:
    with pytest.raises(ValidationError):
        MarketScanComparisonRequest.model_validate({"symbols": symbols, "expected_snapshot_digest": "a" * 64})


@pytest.mark.parametrize("digest", [None, "", "a" * 63, "A" * 64, "g" * 64, True, 1])
def test_comparison_request_requires_exact_digest(digest: object) -> None:
    with pytest.raises(ValidationError):
        MarketScanComparisonRequest.model_validate({"symbols": ["600001.SH", "688001.SH"], "expected_snapshot_digest": digest})


@pytest.mark.parametrize("count", [2, 3, 4])
@pytest.mark.parametrize("eligible", [True, False])
def test_comparison_preserves_frozen_fields_order_and_single_full_verification(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, count: int, eligible: bool,
) -> None:
    cache, _, _, run_id = _environment(tmp_path, action_eligible=eligible)
    run = cache.market_scan_run(run_id)
    request = _request(run, ["920001.BJ", "600001.SH", "688001.SH", "300001.SZ"][:count])
    calls: list[int] = []
    original = market_scan_integrity.verify_market_scan_snapshot

    def tracked(conn: sqlite3.Connection, selected_run: int, **kwargs: object) -> str:
        calls.append(selected_run)
        return original(conn, selected_run, **kwargs)  # type: ignore[arg-type]

    monkeypatch.setattr(market_scan_integrity, "verify_market_scan_snapshot", tracked)
    query = MarketScanQueryService(cache, cast(MarketScanResearchStores, object()))
    response = query.compare_candidates(run_id, request)
    assert calls == [run_id]
    assert response.requested_symbols == [item.symbol for item in response.items] == request.symbols
    assert response.action_source_eligible is eligible
    assert response.audit_only is True and response.ranking_basis == "frozen_base"
    assert response.evidence.snapshot_digest == run.snapshot_digest
    assert response.items[0].board == "北交所"
    with sqlite3.connect(cache.path) as conn:
        conn.row_factory = sqlite3.Row
        for item in response.items:
            stored = conn.execute("SELECT * FROM market_scan_result WHERE run_id = ? AND symbol = ?", (run_id, item.symbol)).fetchone()
            for field in ("rank", "score", "raw_score", "trend_score", "leader_score", "data_quality_score", "price", "change_pct", "amount"):
                assert getattr(item, field) == stored[field]
    assert "upside_probabilities" not in response.items[0].model_dump()


def test_comparison_rejects_conflicting_digest_and_unknown_symbol(tmp_path: Path) -> None:
    cache, _, _, run_id = _environment(tmp_path)
    run = cache.market_scan_run(run_id)
    service = MarketScanComparisonService(cache)
    with pytest.raises(MarketScanComparisonConflict):
        service.compare(run_id, _request(run).model_copy(update={"expected_snapshot_digest": "b" * 64}))
    with pytest.raises(MarketScanComparisonUnavailable, match="不完全属于"):
        service.compare(run_id, _request(run, ["600001.SH", "000999.SZ"]))


def test_comparison_allows_legacy_backfill_for_audit_without_action_authority(tmp_path: Path) -> None:
    cache, _, _, run_id = _environment(tmp_path)
    with sqlite3.connect(cache.path) as conn:
        conn.row_factory = sqlite3.Row
        _disable_market_scan_immutability(conn)
        conn.execute("UPDATE market_scan_run SET snapshot_seal_origin = 'legacy_backfill' WHERE id = ?", (run_id,))
        conn.execute("UPDATE market_scan_run SET snapshot_digest = ? WHERE id = ?", (market_scan_snapshot_digest(conn, run_id), run_id))
    response = MarketScanComparisonService(cache).compare(run_id, _request(cache.market_scan_run(run_id)))
    assert response.evidence.snapshot_seal_origin == "legacy_backfill"
    assert response.action_source_eligible is False and response.audit_only is True


def test_new_listing_comparison_never_refreshes_calendar_even_when_enabled(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    from app.services import market_scan_skip_contract, trading_calendar
    from app.services.cache import SQLiteCache
    from tests.test_market_scan_skip_contract import _valid_action_source_run

    monkeypatch.setenv("ASHARE_RADAR_TRADE_CALENDAR_AUTO_FETCH", "false")
    repo, settings, run_id, skip_symbol, _diagnostics = _valid_action_source_run(tmp_path)
    request = _request(repo.run(run_id), [skip_symbol, "600000.SH"])
    original_range = trading_calendar.trading_date_range
    verification_flags: list[bool] = []

    def observed_range(start, end, *, allow_auto_refresh=True):
        verification_flags.append(allow_auto_refresh)
        return original_range(start, end, allow_auto_refresh=allow_auto_refresh)

    def unexpected_refresh() -> bool:
        pytest.fail("frozen comparison must not schedule supplier calendar refresh")

    monkeypatch.setenv("ASHARE_RADAR_TRADE_CALENDAR_AUTO_FETCH", "true")
    monkeypatch.setattr(trading_calendar, "_should_auto_refresh", lambda *_args: True)
    monkeypatch.setattr(trading_calendar, "_trigger_auto_refresh", unexpected_refresh)
    monkeypatch.setattr(market_scan_skip_contract, "trading_date_range", observed_range)
    response = MarketScanComparisonService(SQLiteCache(settings=settings)).compare(run_id, request)
    assert verification_flags and not any(verification_flags)
    assert response.action_source_eligible is True and response.audit_only is True
    assert response.items[0].symbol == skip_symbol and response.items[0].status == "skipped"
    assert response.items[0].score is None and response.items[0].reason


def test_comparison_rejects_tampering_after_trigger_recreation(tmp_path: Path) -> None:
    cache, _, _, run_id = _environment(tmp_path)
    request = _request(cache.market_scan_run(run_id))
    with sqlite3.connect(cache.path) as conn:
        _disable_market_scan_immutability(conn)
        conn.execute("UPDATE market_scan_result SET name = 'changed' WHERE run_id = ? AND symbol = '600001.SH'", (run_id,))
        create_market_scan_immutability_triggers(conn)
    with pytest.raises(MarketScanSnapshotSealError):
        MarketScanComparisonService(cache).compare(run_id, request)


def test_comparison_reads_header_hash_and_selected_rows_from_one_snapshot(tmp_path: Path) -> None:
    cache, _, _, run_id = _environment(tmp_path)
    request = _request(cache.market_scan_run(run_id))

    class MutatingRepository:
        @contextmanager
        def verified_market_scan_read(self, candidate: int) -> Iterator[MarketScanVerifiedReadProtocol]:
            with cache.verified_market_scan_read(candidate) as verified:
                with sqlite3.connect(cache.path) as writer:
                    _disable_market_scan_immutability(writer)
                    writer.execute("UPDATE market_scan_result SET name = 'external mutation' WHERE run_id = ? AND symbol = '600001.SH'", (candidate,))
                    create_market_scan_immutability_triggers(writer)
                yield verified

    response = MarketScanComparisonService(MutatingRepository()).compare(run_id, request)
    assert response.items[1].name == "沪市样本"
    with pytest.raises(MarketScanSnapshotSealError):
        MarketScanComparisonService(cache).compare(run_id, request)


class _Repository:
    def __init__(self, run: MarketScanRun, items: list[MarketScanResultItem]) -> None:
        self.run, self.items = run, items
        self.queries: list[dict[str, object]] = []
        self.total = len(items)

    @contextmanager
    def verified_market_scan_read(self, _run_id: int) -> Iterator[MarketScanVerifiedReadProtocol]:
        yield cast(MarketScanVerifiedReadProtocol, SimpleNamespace(
            run=self.run, snapshot_digest=self.run.snapshot_digest, action_source_digest=None, results_page=self.page,
        ))

    def page(self, **query: object) -> MarketScanResultPage:
        self.queries.append(query)
        return MarketScanResultPage.model_construct(run=self.run, items=self.items, total=self.total, page=1, page_size=2, page_count=1)


def _repository(tmp_path: Path) -> tuple[_Repository, MarketScanComparisonRequest]:
    cache, _, _, run_id = _environment(tmp_path, action_eligible=False)
    run = cache.market_scan_run(run_id)
    with cache.verified_market_scan_read(run_id) as verified:
        from app.services.market_scan_comparison import _comparison_query
        items = verified.results_page(**_comparison_query(["600001.SH", "688001.SH"])).items
    return _Repository(run, items), _request(run, ["600001.SH", "688001.SH"])


@pytest.mark.parametrize("state", ["missing", "skipped"])
def test_comparison_keeps_missing_results_and_does_not_fill_scores(tmp_path: Path, state: str) -> None:
    repository, request = _repository(tmp_path)
    cleared = {field: None for field in ("rank", "score", "raw_score", "trend_score", "leader_score", "data_quality_score", "price", "amount")}
    repository.items[1] = repository.items[1].model_copy(update={**cleared, "status": state, "reason": "冻结来源缺口", "score_details": {}})
    response = MarketScanComparisonService(repository).compare(repository.run.id, request)
    item = response.items[1]
    assert item.status == state and item.reason == "冻结来源缺口"
    assert item.score is None and item.price is None and item.confidence is None
    assert len(repository.queries) == 1
    assert repository.queries[0]["status"] is None
    assert repository.queries[0]["symbols"] == tuple(request.symbols)
    assert repository.queries[0]["page_size"] == 2


@pytest.mark.parametrize("value", [None, True, "80", {}, [], -1, 101, float("nan"), float("inf")])
def test_legacy_unavailable_dimensions_remain_null(tmp_path: Path, value: object) -> None:
    repository, request = _repository(tmp_path)
    details = {"components": {"score_dimensions": {"scores": {"confidence": value, "risk": value, "tradability": value}}}}
    repository.items[0] = repository.items[0].model_copy(update={"score_details": details})
    item = MarketScanComparisonService(repository).compare(repository.run.id, request).items[0]
    assert item.confidence is None and item.risk is None and item.tradability is None


def test_zero_dimension_remains_present_and_risk_is_not_inverted(tmp_path: Path) -> None:
    repository, request = _repository(tmp_path)
    details = {"components": {"score_dimensions": {"scores": {"confidence": 0, "risk": 100, "tradability": 75.5}}}}
    repository.items[0] = repository.items[0].model_copy(update={"score_details": details})
    item = MarketScanComparisonService(repository).compare(repository.run.id, request).items[0]
    assert (item.confidence, item.risk, item.tradability) == (0, 100, 75.5)


@pytest.mark.parametrize("mutation", ["duplicate", "different_run", "unexpected_symbol", "truncated", "empty", "changed_header"])
def test_comparison_rejects_invalid_page_identity(tmp_path: Path, mutation: str) -> None:
    repository, request = _repository(tmp_path)
    if mutation == "duplicate":
        repository.items[1] = repository.items[0]
    elif mutation == "different_run":
        repository.items[1] = repository.items[1].model_copy(update={"run_id": 999})
    elif mutation == "unexpected_symbol":
        repository.items[1] = repository.items[1].model_copy(update={"symbol": "000999.SZ"})
    elif mutation in {"empty", "truncated"}:
        repository.items = repository.items[:0 if mutation == "empty" else 1]
    else:
        original = repository.page
        repository.page = lambda **kwargs: original(**kwargs).model_copy(update={"run": repository.run.model_copy(update={"data_date": "2000-01-01"})})
    with pytest.raises(MarketScanSnapshotIntegrityError):
        MarketScanComparisonService(repository).compare(repository.run.id, request)


@pytest.mark.parametrize("change", [{"status": "running"}, {"scope": "top100_refresh"}, {"snapshot_digest": None}])
def test_comparison_requires_complete_publication_before_reading_items(tmp_path: Path, change: dict[str, object]) -> None:
    repository, request = _repository(tmp_path)
    repository.run = repository.run.model_copy(update=change)
    with pytest.raises(MarketScanComparisonUnavailable):
        MarketScanComparisonService(repository).compare(repository.run.id, request)
    assert repository.queries == []


@pytest.mark.parametrize("mutation", ["digest", "order", "duplicate", "run", "unknown", "nan", "bool_score", "missing_score", "different_date", "legacy_authority"])
def test_response_schema_rejects_corruption_even_with_recomputed_digest(mutation: str) -> None:
    payload = json.loads(_FIXTURE.read_text())
    if mutation == "digest":
        payload["canonical_digest"] = "b" * 64
    elif mutation == "order":
        payload["items"].reverse()
    elif mutation == "duplicate":
        payload["requested_symbols"][1] = payload["requested_symbols"][0]
    elif mutation == "run":
        payload["items"][0]["run_id"] += 1
    elif mutation == "unknown":
        payload["unknown_field"] = True
    elif mutation == "nan":
        payload["items"][0]["risk"] = float("nan")
    elif mutation == "bool_score":
        payload["items"][0]["score"] = True
    elif mutation == "missing_score":
        payload["items"][0]["score"] = None
    elif mutation == "different_date":
        payload["items"][0]["data_date"] = "2000-01-01"
    else:
        payload["evidence"]["snapshot_seal_origin"] = "legacy_backfill"
        payload["action_source_eligible"] = True
    if mutation not in {"digest", "nan"}:
        _rehashed(payload)
    with pytest.raises(ValidationError):
        MarketScanComparisonResponse.model_validate(payload)


def _client(scanner: object) -> TestClient:
    application = FastAPI()
    application.add_exception_handler(RequestValidationError, validation_exception_handler)
    application.include_router(market_scan.router)
    application.dependency_overrides[get_market_scanner] = lambda: scanner
    application.dependency_overrides[get_market_scan_heavy_read_admission] = lambda: MarketScanHeavyReadAdmission()
    return TestClient(application)


def test_comparison_api_binds_request_and_no_store_response(tmp_path: Path) -> None:
    cache, _, _, run_id = _environment(tmp_path, action_eligible=False)
    request = _request(cache.market_scan_run(run_id))
    query = MarketScanQueryService(cache, cast(MarketScanResearchStores, object()))
    client = _client(query)
    response = client.post(f"/api/market-scans/{run_id}/compare", json=request.model_dump())
    assert response.status_code == 200
    assert response.headers["cache-control"] == "no-store"
    parsed = MarketScanComparisonResponse.model_validate(response.json())
    assert parsed.requested_symbols == request.symbols
    assert parsed.action_source_eligible is False
    invalid = client.post(f"/api/market-scans/{run_id}/compare", json={**request.model_dump(), "extra": True})
    assert invalid.status_code == 422
    assert invalid.headers["cache-control"] == "no-store"


@pytest.mark.parametrize("error,status", [(MarketScanComparisonConflict("榜单已变化"), 409), (MarketScanComparisonUnavailable("选择不完整"), 422), (MarketScanSnapshotSealError("private path"), 409)])
def test_comparison_api_maps_failures_without_caching(error: Exception, status: int) -> None:
    def fail(*_args: object) -> None:
        raise error
    client = _client(SimpleNamespace(compare_candidates=fail))
    response = client.post("/api/market-scans/1/compare", json={"symbols": ["600001.SH", "688001.SH"], "expected_snapshot_digest": "a" * 64})
    assert response.status_code == status
    assert response.headers["cache-control"] == "no-store"
    assert "private path" not in response.text


def test_shared_comparison_fixture_is_valid_and_has_no_large_research_payloads() -> None:
    payload = json.loads(_FIXTURE.read_text())
    parsed = MarketScanComparisonResponse.model_validate(deepcopy(payload))
    assert parsed.audit_only is True
    assert all("score_details" not in item and "upside_probabilities" not in item for item in payload["items"])
