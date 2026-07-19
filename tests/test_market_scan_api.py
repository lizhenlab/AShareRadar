from __future__ import annotations

from datetime import datetime

import pytest
from fastapi import FastAPI
from fastapi.exceptions import RequestValidationError
from fastapi.testclient import TestClient

from app.api.deps import get_market_scanner
from app.api.errors import validation_exception_handler
from app.api.routes import market_scan
from app.models.market_scan import (
    MarketScanResultPage,
    MarketScanRun,
    MarketScanRunPage,
    MarketScanStartResponse,
)
from app.utils.errors import NotFoundError


def test_create_scan_returns_202_with_queued_run_and_deduplicates_active_request() -> None:
    scanner = _ScannerStub()
    client = _client(scanner)

    first = client.post(
        "/api/market-scans",
        json={"as_of": "2026-07-17T16:30:00+08:00"},
    )
    duplicate = client.post("/api/market-scans")

    assert first.status_code == 202
    assert first.json()["accepted"] is True
    assert first.json()["deduplicated"] is False
    assert first.json()["run"]["status"] == "queued"
    assert duplicate.status_code == 202
    assert duplicate.json()["accepted"] is False
    assert duplicate.json()["deduplicated"] is True
    assert duplicate.json()["run"]["id"] == first.json()["run"]["id"]
    assert scanner.create_calls == [
        datetime.fromisoformat("2026-07-17T16:30:00+08:00"),
        None,
    ]


def test_latest_list_detail_cancel_and_retry_routes_expose_lifecycle() -> None:
    scanner = _ScannerStub()
    client = _client(scanner)

    latest = client.get("/api/market-scans/latest")
    history = client.get("/api/market-scans", params={"page": 2, "page_size": 1})
    detail = client.get(f"/api/market-scans/{scanner.active.id}")
    cancelled = client.post(f"/api/market-scans/{scanner.active.id}/cancel")
    retried = client.post(f"/api/market-scans/{scanner.active.id}/retry")

    assert latest.status_code == 200
    assert latest.headers["cache-control"] == "no-store"
    assert latest.json()["id"] == scanner.active.id
    assert history.status_code == 200
    assert history.headers["cache-control"] == "no-store"
    assert history.json() == {
        "items": [scanner.active.model_dump()],
        "total": 2,
        "page": 2,
        "page_size": 1,
        "page_count": 2,
    }
    assert detail.status_code == 200
    assert detail.headers["cache-control"] == "no-store"
    assert detail.json()["id"] == scanner.active.id
    assert cancelled.status_code == 200
    assert cancelled.json()["status"] == "cancelled"
    assert cancelled.json()["cancel_requested_at"] is not None
    assert retried.status_code == 202
    assert retried.json()["accepted"] is True
    assert retried.json()["run"]["status"] == "queued"
    assert retried.json()["run"]["trigger"] == "retry"
    assert scanner.list_calls == [(2, 1)]
    assert scanner.detail_calls == [scanner.active.id]
    assert scanner.cancel_calls == [scanner.active.id]
    assert scanner.retry_calls == [scanner.active.id]


def test_results_route_forwards_pagination_sorting_and_every_filter() -> None:
    scanner = _ScannerStub()
    client = _client(scanner)

    response = client.get(
        f"/api/market-scans/{scanner.active.id}/results",
        params={
            "page": 3,
            "page_size": 25,
            "status": "missing",
            "market": "BJ",
            "industry": "高端装备",
            "is_st": "true",
            "is_new": "false",
            "min_data_quality_score": 77,
            "keyword": "920066",
            "sort": "amount",
            "order": "desc",
        },
    )

    assert response.status_code == 200
    assert response.headers["cache-control"] == "no-store"
    assert response.json()["page"] == 3
    assert response.json()["page_size"] == 25
    assert scanner.result_calls == [
        (
            scanner.active.id,
            {
                "page": 3,
                "page_size": 25,
                "status": "missing",
                "market": "BJ",
                "industry": "高端装备",
                "is_st": True,
                "is_new": False,
                "min_data_quality_score": 77,
                "keyword": "920066",
                "sort": "amount",
                "order": "desc",
            },
        )
    ]


def test_results_route_maps_all_status_filter_to_unfiltered_query() -> None:
    scanner = _ScannerStub()
    client = _client(scanner)

    response = client.get(
        f"/api/market-scans/{scanner.active.id}/results",
        params={"status": "all"},
    )

    assert response.status_code == 200
    assert scanner.result_calls[0][1]["status"] is None
    assert scanner.result_calls[0][1]["page_size"] == 100


@pytest.mark.parametrize(
    ("method", "path", "json_body"),
    [
        ("get", "/api/market-scans?page=0", None),
        ("get", "/api/market-scans?page_size=101", None),
        ("get", "/api/market-scans/7/results?page=0", None),
        ("get", "/api/market-scans/7/results?page_size=201", None),
        ("get", "/api/market-scans/7/results?status=unknown", None),
        ("get", "/api/market-scans/7/results?market=HK", None),
        ("get", "/api/market-scans/7/results?min_data_quality_score=-1", None),
        ("get", "/api/market-scans/7/results?min_data_quality_score=101", None),
        ("get", "/api/market-scans/7/results?sort=unknown", None),
        ("get", "/api/market-scans/7/results?order=sideways", None),
        ("get", "/api/market-scans/7/results?is_st=perhaps", None),
        ("get", f"/api/market-scans/7/results?industry={'x' * 81}", None),
        ("get", f"/api/market-scans/7/results?keyword={'x' * 81}", None),
        ("post", "/api/market-scans", {"as_of": "not-a-datetime"}),
        ("post", "/api/market-scans", {"as_fo": "2026-07-17T16:30:00+08:00"}),
    ],
)
def test_market_scan_routes_reject_invalid_parameters(
    method: str,
    path: str,
    json_body: dict[str, object] | None,
) -> None:
    scanner = _ScannerStub()
    client = _client(scanner)

    response = client.request(method, path, json=json_body)

    assert response.status_code == 422
    assert scanner.calls == []


def test_missing_scan_detail_is_mapped_to_404() -> None:
    scanner = _ScannerStub()
    client = _client(scanner)

    response = client.get("/api/market-scans/999")

    assert response.status_code == 404
    assert response.json() == {"detail": "全市场扫描批次不存在：999"}


def _client(scanner: _ScannerStub) -> TestClient:
    app = FastAPI()
    app.add_exception_handler(RequestValidationError, validation_exception_handler)
    app.include_router(market_scan.router)
    app.dependency_overrides[get_market_scanner] = lambda: scanner
    return TestClient(app)


def _run(run_id: int = 7, *, status: str = "queued") -> MarketScanRun:
    terminal = status in {"success", "degraded", "failed", "cancelled", "interrupted"}
    return MarketScanRun(
        id=run_id,
        status=status,
        trigger="manual",
        rule_version="full-market-score-v1",
        as_of="2026-07-17 16:30:00",
        data_date="2026-07-17",
        scope="SH/SZ/BJ listed A-shares",
        total_count=10,
        excluded_count=1,
        processed_count=10 if terminal else 0,
        success_count=8 if terminal else 0,
        missing_count=1 if terminal else 0,
        skipped_count=1 if terminal else 0,
        retry_count=0,
        progress_pct=100.0 if terminal else 0.0,
        coverage_pct=80.0 if terminal else 0.0,
        created_at="2026-07-17 16:30:00",
        updated_at="2026-07-17 16:30:00",
        finished_at="2026-07-17 16:31:00" if terminal else None,
        duration_ms=60_000 if terminal else None,
        message="等待全市场扫描" if not terminal else "扫描结束",
    )


class _ScannerStub:
    def __init__(self) -> None:
        self.active = _run()
        self.previous = _run(6, status="degraded")
        self.create_calls: list[datetime | None] = []
        self.list_calls: list[tuple[int, int]] = []
        self.detail_calls: list[int] = []
        self.result_calls: list[tuple[int, dict[str, object]]] = []
        self.cancel_calls: list[int] = []
        self.retry_calls: list[int] = []

    @property
    def calls(self) -> list[object]:
        return [
            *self.create_calls,
            *self.list_calls,
            *self.detail_calls,
            *self.result_calls,
            *self.cancel_calls,
            *self.retry_calls,
        ]

    async def create_scan(self, *, as_of: datetime | None, trigger: str) -> MarketScanStartResponse:
        assert trigger == "manual"
        self.create_calls.append(as_of)
        if len(self.create_calls) > 1:
            return MarketScanStartResponse(
                accepted=False,
                deduplicated=True,
                run=self.active,
            )
        return MarketScanStartResponse(accepted=True, run=self.active)

    def latest_run(self) -> MarketScanRun:
        return self.active

    def runs(self, *, page: int, page_size: int) -> MarketScanRunPage:
        self.list_calls.append((page, page_size))
        return MarketScanRunPage(
            items=[self.active],
            total=2,
            page=page,
            page_size=page_size,
            page_count=2,
        )

    def run(self, run_id: int) -> MarketScanRun:
        self.detail_calls.append(run_id)
        if run_id == 999:
            raise NotFoundError(f"全市场扫描批次不存在：{run_id}")
        return self.active

    def results(self, run_id: int, **kwargs: object) -> MarketScanResultPage:
        self.result_calls.append((run_id, kwargs))
        return MarketScanResultPage(
            run=self.active,
            items=[],
            total=0,
            page=int(kwargs["page"]),
            page_size=int(kwargs["page_size"]),
            page_count=0,
        )

    async def cancel_scan(self, run_id: int) -> MarketScanRun:
        self.cancel_calls.append(run_id)
        return self.active.model_copy(
            update={
                "status": "cancelled",
                "finished_at": "2026-07-17 16:30:01",
                "cancel_requested_at": "2026-07-17 16:30:01",
            }
        )

    async def retry_scan(self, run_id: int) -> MarketScanStartResponse:
        self.retry_calls.append(run_id)
        retried = self.active.model_copy(update={"status": "queued", "trigger": "retry", "retry_count": 1})
        return MarketScanStartResponse(accepted=True, run=retried)
