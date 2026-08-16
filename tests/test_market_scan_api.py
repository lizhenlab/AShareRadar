from __future__ import annotations

import asyncio
from datetime import datetime
import gc
from pathlib import Path
from threading import Event, Thread

import pytest
from fastapi import FastAPI, HTTPException
from fastapi.exceptions import RequestValidationError
from fastapi.testclient import TestClient
from pydantic import ValidationError

from app.api.deps import get_market_scan_heavy_read_admission, get_market_scanner
from app.api.errors import MARKET_SCAN_BUSY_DETAIL, MARKET_SCAN_INTEGRITY_DETAIL, validation_exception_handler
from app.api.market_scan_read_admission import MarketScanHeavyReadAdmission, run_admitted_market_scan_read
from app.api.routes import market_scan
from app.config import Settings
from app.db.market_scan_integrity import MarketScanSnapshotSealError
from app.main import create_app
from app.models.market_scan import (
    MARKET_SCAN_TOP100_REFRESH_SCOPE,
    MarketScanPublicationDiagnostic,
    MarketScanPublicationDiagnostics,
    MarketScanResultItem,
    MarketScanResultPage,
    MarketScanRun,
    MarketScanRunPage,
    MarketScanStartResponse,
)
from app.models.market_scan_snapshot import MarketScanSnapshotIntegrityError
from app.models.market_scan_polling import (
    MarketScanPollingIdentity,
    MarketScanPollingRunToken,
)
from app.services.market_scan_export import (
    XLSX_MEDIA_TYPE,
    MarketScanExportFilters,
    MarketScanWorkbookExport,
)
from app.services.market_scan_future_range_artifact import FutureRangeArtifactError
from app.services.market_scan_probability_artifact import ProbabilityArtifactError
from app.services.market_scan_probability_outcomes import ProbabilityOutcomeError
from app.services.market_scan_probability_source import ProbabilitySourceError
from app.services.market_scan_future_range_store import FutureRangeResearchUnavailable
from app.services.market_scan_probability_store import MarketScanProbabilityStore, ProbabilityFilterUnavailable
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
    assert first.headers["cache-control"] == "no-store"
    assert first.json()["accepted"] is True
    assert first.json()["deduplicated"] is False
    assert first.json()["run"]["status"] == "queued"
    assert duplicate.status_code == 202
    assert duplicate.headers["cache-control"] == "no-store"
    assert duplicate.json()["accepted"] is False
    assert duplicate.json()["deduplicated"] is True
    assert duplicate.json()["run"]["id"] == first.json()["run"]["id"]
    assert scanner.create_calls == [
        (datetime.fromisoformat("2026-07-17T16:30:00+08:00"), "official"),
        (None, "official"),
    ]


def test_public_run_and_result_models_reject_cross_field_corruption() -> None:
    active_payload = _run().model_dump()
    active_payload["processed_count"] = 1
    with pytest.raises(ValidationError, match="计数不守恒"):
        MarketScanRun.model_validate(active_payload)

    published_payload = _run(status="success").model_dump()
    published_payload.update({"current_stage": "scoring", "stage_started_at": "2026-07-17 16:30:30"})
    with pytest.raises(ValidationError, match="运行中阶段"):
        MarketScanRun.model_validate(published_payload)

    run = _run(status="success")
    valid = _valid_result_item(run.id)
    malformed = valid.model_copy(update={"status": "missing", "error": "行情缺失"})
    with pytest.raises(ValidationError, match="非 success.*rank"):
        MarketScanResultPage(
            run=run,
            items=[malformed],
            total=1,
            page=1,
            page_size=100,
            page_count=1,
        )
    missing_provenance = valid.model_copy(update={"quote_observed_at": None})
    with pytest.raises(ValidationError, match="quote_observed_at"):
        MarketScanResultPage(
            run=run,
            items=[missing_provenance],
            total=1,
            page=1,
            page_size=100,
            page_count=1,
        )


def test_public_pages_require_exact_current_page_item_count_and_allow_empty_overflow() -> None:
    with pytest.raises(ValidationError, match="当前分页"):
        MarketScanRunPage(
            items=[_run()],
            total=2,
            page=1,
            page_size=2,
            page_count=1,
        )

    overflow = MarketScanRunPage(
        items=[],
        total=1,
        page=2,
        page_size=10,
        page_count=1,
    )
    assert overflow.items == []


@pytest.mark.parametrize("mode", ("intraday", "preopen"))
def test_create_scan_forwards_explicit_non_official_mode(mode: str) -> None:
    scanner = _ScannerStub()
    client = _client(scanner)

    response = client.post("/api/market-scans", json={"mode": mode})

    assert response.status_code == 202
    assert scanner.create_calls == [(None, mode)]


def test_latest_list_detail_cancel_and_retry_routes_expose_lifecycle() -> None:
    scanner = _ScannerStub()
    scanner.previous = scanner.previous.model_copy(
        update={
            "publication_diagnostics": MarketScanPublicationDiagnostics(
                headline="盘后正式扫描未达到发布可信度",
                blockers=[
                    MarketScanPublicationDiagnostic(
                        code="publication.snapshot.span_exceeded",
                        label="报价快照跨度超限",
                        detail="全市场报价快照跨度 1918 秒超过 1200 秒门槛",
                        severity="error",
                    )
                ],
            )
        }
    )
    client = _client(scanner)

    latest = client.get("/api/market-scans/latest")
    published = client.get("/api/market-scans/latest-published")
    history = client.get("/api/market-scans", params={"page": 2, "page_size": 1})
    detail = client.get(f"/api/market-scans/{scanner.active.id}")
    cancelled = client.post(f"/api/market-scans/{scanner.active.id}/cancel")
    retried = client.post(f"/api/market-scans/{scanner.active.id}/retry")
    refreshed = client.post(f"/api/market-scans/{scanner.previous.id}/refresh-top100")

    assert latest.status_code == 200
    assert latest.headers["cache-control"] == "no-store"
    assert latest.json()["id"] == scanner.active.id
    assert published.status_code == 200
    assert published.headers["cache-control"] == "no-store"
    assert published.json()["id"] == scanner.previous.id
    assert published.json()["publication_diagnostics"]["blockers"][0]["code"] == (
        "publication.snapshot.span_exceeded"
    )
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
    assert cancelled.headers["cache-control"] == "no-store"
    assert cancelled.json()["status"] == "cancelled"
    assert cancelled.json()["cancel_requested_at"] is not None
    assert retried.status_code == 202
    assert retried.headers["cache-control"] == "no-store"
    assert retried.json()["accepted"] is True
    assert retried.json()["run"]["status"] == "queued"
    assert retried.json()["run"]["trigger"] == "retry"
    assert refreshed.status_code == 202
    assert refreshed.headers["cache-control"] == "no-store"
    assert refreshed.json()["run"]["scope"] == MARKET_SCAN_TOP100_REFRESH_SCOPE
    assert refreshed.json()["run"]["retry_of_run_id"] == scanner.previous.id
    assert scanner.latest_calls == [(None, False), (None, True)]
    assert scanner.list_calls == [(2, 1, None, None, None)]
    assert scanner.detail_calls == [scanner.active.id]
    assert scanner.cancel_calls == [scanner.active.id]
    assert scanner.retry_calls == [scanner.active.id]
    assert scanner.top100_refresh_calls == [scanner.previous.id]


def test_latest_and_history_routes_forward_mode_status_and_date_filters() -> None:
    scanner = _ScannerStub()
    client = _client(scanner)

    latest = client.get("/api/market-scans/latest", params={"mode": "intraday"})
    published = client.get("/api/market-scans/latest-published", params={"mode": "official"})
    preopen = client.get("/api/market-scans/latest", params={"mode": "preopen"})
    history = client.get(
        "/api/market-scans",
        params={
            "page": 1,
            "page_size": 50,
            "mode": "intraday",
            "status": "published",
            "data_date": "2026-07-16",
        },
    )

    assert latest.status_code == 200
    assert published.status_code == 200
    assert preopen.status_code == 200
    assert history.status_code == 200
    assert scanner.latest_calls == [
        ("intraday", False),
        ("official", True),
        ("preopen", False),
    ]
    assert scanner.list_calls == [(1, 50, "intraday", "published", "2026-07-16")]


def test_polling_identity_route_is_explicit_non_authorizing_and_no_store() -> None:
    scanner = _ScannerStub()
    client = _client(scanner)

    response = client.get(
        "/api/market-scans/polling-identity",
        params={"mode": "intraday"},
    )

    assert response.status_code == 200
    assert response.headers["cache-control"] == "no-store"
    assert response.json() == {
        "schema_version": "market-scan-polling-identity-v1",
        "authorization": "change_detection_only",
        "request_mode": "intraday",
        "latest": {"run_id": scanner.active.id, "token": "a" * 64},
        "latest_published": {"run_id": scanner.previous.id, "token": "b" * 64},
        "fingerprint": "c" * 64,
    }
    assert scanner.polling_identity_calls == ["intraday"]
    assert client.get(
        "/api/market-scans/polling-identity",
        params={"mode": "invalid"},
    ).status_code == 422


def test_polling_identity_contract_rejects_impossible_slot_ordering() -> None:
    def token(run_id: int | None, digest: str) -> MarketScanPollingRunToken:
        return MarketScanPollingRunToken(run_id=run_id, token=digest)

    common = {
        "request_mode": "official",
        "fingerprint": "f" * 64,
    }

    with pytest.raises(ValidationError, match="不能脱离"):
        MarketScanPollingIdentity(
            **common,
            latest=token(None, "a" * 64),
            latest_published=token(1, "b" * 64),
        )
    with pytest.raises(ValidationError, match="不能晚于"):
        MarketScanPollingIdentity(
            **common,
            latest=token(1, "a" * 64),
            latest_published=token(2, "b" * 64),
        )
    with pytest.raises(ValidationError, match="必须一致"):
        MarketScanPollingIdentity(
            **common,
            latest=token(None, "a" * 64),
            latest_published=token(None, "b" * 64),
        )


def test_four_snapshot_read_routes_share_one_nonblocking_admission_slot(tmp_path: Path) -> None:
    scanner = _ScannerStub()
    admission = MarketScanHeavyReadAdmission()
    started, release = Event(), Event()
    original_latest = scanner.latest_run

    def blocked_latest(*, mode: str | None = None) -> MarketScanRun:
        started.set()
        assert release.wait(timeout=10)
        return original_latest(mode=mode)

    scanner.latest_run = blocked_latest  # type: ignore[method-assign]
    client = _production_handler_client(scanner, admission, tmp_path)
    responses: list[object] = []
    first = Thread(target=lambda: responses.append(client.get("/api/market-scans/latest")))
    first.start()
    assert started.wait(timeout=5)
    assert admission.active_count == 1

    busy = [
        client.get("/api/market-scans/latest-published", params={"mode": "official"}),
        client.get(f"/api/market-scans/{scanner.active.id}"),
        client.get(f"/api/market-scans/{scanner.active.id}/results"),
    ]
    identity = client.get("/api/market-scans/polling-identity", params={"mode": "official"})

    assert [response.status_code for response in busy] == [503, 503, 503]
    assert all(response.headers["cache-control"] == "no-store" for response in busy)
    assert all(response.headers["retry-after"] == "2" for response in busy)
    assert all(response.json() == {"detail": MARKET_SCAN_BUSY_DETAIL} for response in busy)
    assert scanner.latest_calls == []
    assert scanner.detail_calls == []
    assert scanner.result_calls == []
    assert identity.status_code == 200
    assert scanner.polling_identity_calls == ["official"]

    release.set()
    first.join(timeout=5)
    assert not first.is_alive()
    assert responses[0].status_code == 200
    assert admission.active_count == 0
    assert client.get("/api/market-scans/latest-published").status_code == 200
    assert scanner.latest_calls == [(None, False), (None, True)]


def test_cancelled_heavy_read_keeps_slot_until_worker_finishes_and_consumes_late_error() -> None:
    started, release = Event(), Event()
    admission = MarketScanHeavyReadAdmission()

    def delayed_failure() -> None:
        started.set()
        assert release.wait(timeout=2)
        raise RuntimeError("late verifier failure")

    async def scenario() -> tuple[list[dict[str, object]], int]:
        loop = asyncio.get_running_loop()
        unhandled: list[dict[str, object]] = []
        previous_handler = loop.get_exception_handler()
        loop.set_exception_handler(lambda _loop, context: unhandled.append(context))
        try:
            task = asyncio.create_task(run_admitted_market_scan_read(admission, delayed_failure))
            assert await asyncio.to_thread(started.wait, 1)
            task.cancel()
            with pytest.raises(asyncio.CancelledError):
                await task
            assert admission.active_count == 1
            del task
            gc.collect()
            assert admission.worker_count == 1
            with pytest.raises(HTTPException) as busy:
                await run_admitted_market_scan_read(admission, lambda: None)
            assert busy.value.status_code == 503
            close_task = asyncio.create_task(admission.aclose())
            await asyncio.sleep(0)
            assert not close_task.done()
            close_task.cancel()
            await asyncio.sleep(0)
            assert not close_task.done()
            release.set()
            for _ in range(100):
                if admission.active_count == 0:
                    break
                await asyncio.sleep(0.01)
            with pytest.raises(asyncio.CancelledError):
                await close_task
            await asyncio.sleep(0)
            return unhandled, admission.active_count + admission.worker_count
        finally:
            loop.set_exception_handler(previous_handler)

    unhandled, active_count = asyncio.run(scenario())

    assert unhandled == []
    assert active_count == 0
    with pytest.raises(HTTPException) as closed:
        asyncio.run(run_admitted_market_scan_read(admission, lambda: 7))
    assert closed.value.status_code == 503


def test_admission_never_reuses_a_prior_success_when_next_snapshot_read_fails() -> None:
    scanner = _ScannerStub()
    admission = MarketScanHeavyReadAdmission()
    original_latest = scanner.latest_run
    calls = 0

    def mutable_latest(*, mode: str | None = None) -> MarketScanRun:
        nonlocal calls
        calls += 1
        if calls == 2:
            raise MarketScanSnapshotSealError("tampered after prior response")
        return original_latest(mode=mode)

    scanner.latest_run = mutable_latest  # type: ignore[method-assign]
    client = _client(scanner, admission=admission)

    assert client.get("/api/market-scans/latest").status_code == 200
    rejected = client.get("/api/market-scans/latest")

    assert rejected.status_code == 409
    assert rejected.headers["cache-control"] == "no-store"
    assert rejected.json() == {"detail": MARKET_SCAN_INTEGRITY_DETAIL}
    assert calls == 2


def test_admission_releases_lease_when_worker_task_creation_fails(monkeypatch: pytest.MonkeyPatch) -> None:
    admission = MarketScanHeavyReadAdmission()

    def rejected_create_task(coroutine: object) -> None:
        coroutine.close()  # type: ignore[attr-defined]
        raise RuntimeError("event loop rejected worker")

    monkeypatch.setattr("app.api.market_scan_read_admission.asyncio.create_task", rejected_create_task)

    with pytest.raises(RuntimeError, match="event loop rejected worker"):
        asyncio.run(run_admitted_market_scan_read(admission, lambda: 7))

    assert admission.active_count == 0
    assert admission.worker_count == 0


def test_history_route_rejects_invalid_mode_status_and_date() -> None:
    scanner = _ScannerStub()
    client = _client(scanner)

    assert client.get("/api/market-scans/latest", params={"mode": "bad"}).status_code == 422
    assert client.get("/api/market-scans", params={"status": "pending-result"}).status_code == 422
    assert client.get("/api/market-scans", params={"data_date": "2026-02-30"}).status_code == 422
    assert scanner.calls == []


def test_results_route_forwards_pagination_sorting_and_every_filter() -> None:
    scanner = _ScannerStub()
    client = _client(scanner)

    response = client.get(
        f"/api/market-scans/{scanner.active.id}/results",
        params={
            "page": 3,
            "page_size": 25,
            "status": "missing",
            "market": ["BJ", "SZ"],
            "industry": ["高端装备", "银行"],
            "is_st": "true",
            "is_new": "false",
            "min_score": 60,
            "max_score": 95,
            "min_trend_score": 55,
            "max_trend_score": 90,
            "min_change_pct": -2.5,
            "max_change_pct": 9.5,
            "min_turnover_rate": 1.5,
            "max_turnover_rate": 30,
            "min_amount": 1_000_000,
            "max_amount": 500_000_000,
            "min_data_quality_score": 77,
            "max_data_quality_score": 99,
            "min_confidence": 80,
            "max_risk": 35,
            "min_tradability": 70,
            "keyword": "920066",
            "sort": ["amount", "score", "symbol"],
            "order": ["desc", "desc", "asc"],
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
                "market": ("BJ", "SZ"),
                "industry": ("高端装备", "银行"),
                "is_st": True,
                "is_new": False,
                "min_score": 60,
                "max_score": 95,
                "min_trend_score": 55,
                "max_trend_score": 90,
                "min_change_pct": -2.5,
                "max_change_pct": 9.5,
                "min_turnover_rate": 1.5,
                "max_turnover_rate": 30.0,
                "min_amount": 1_000_000.0,
                "max_amount": 500_000_000.0,
                "min_data_quality_score": 77,
                "max_data_quality_score": 99,
                "min_confidence": 80.0,
                "max_risk": 35.0,
                    "min_tradability": 70.0,
                    "probability_horizon": 5,
                    "min_upside_probability": None,
                    "keyword": "920066",
                "sort": ("amount", "score", "symbol"),
                "order": ("desc", "desc", "asc"),
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


def test_results_route_forwards_probability_filter_and_validates_query_range() -> None:
    scanner = _ScannerStub()
    client = _client(scanner)

    response = client.get(
        f"/api/market-scans/{scanner.active.id}/results",
        params={"probability_horizon": 20, "min_upside_probability": 0.61},
    )

    assert response.status_code == 200
    assert scanner.result_calls[0][1]["probability_horizon"] == 20
    assert scanner.result_calls[0][1]["min_upside_probability"] == pytest.approx(0.61)
    assert client.get(
        f"/api/market-scans/{scanner.active.id}/results",
        params={"probability_horizon": 3},
    ).status_code == 422
    assert client.get(
        f"/api/market-scans/{scanner.active.id}/results",
        params={"min_upside_probability": 1.01},
    ).status_code == 422
    assert client.get(
        f"/api/market-scans/{scanner.active.id}/results",
        params={"sort": "upside_probability"},
    ).status_code == 422


def test_results_route_returns_422_when_probability_evidence_is_not_calibrated() -> None:
    scanner = _ScannerStub()

    def reject(_run_id: int, **_kwargs: object) -> MarketScanResultPage:
        raise ProbabilityFilterUnavailable("当前批次证据不足")

    scanner.results = reject  # type: ignore[method-assign]
    response = _client(scanner).get(
        f"/api/market-scans/{scanner.active.id}/results",
        params={"min_upside_probability": 0.6},
    )

    assert response.status_code == 422
    assert response.json()["detail"] == "当前批次证据不足"


def test_probability_research_route_is_read_only_and_run_bound() -> None:
    scanner = _ScannerStub()
    response = _client(scanner).get(f"/api/market-scans/{scanner.active.id}/probability-research")

    assert response.status_code == 200
    assert response.headers["cache-control"] == "no-store"
    assert response.json()["run_id"] == scanner.active.id
    assert response.json()["default_horizon"] == 5


@pytest.mark.parametrize(
    "error",
    [
        ProbabilityArtifactError("sensitive artifact path and digest mismatch"),
        ProbabilityOutcomeError("sensitive outcome path and digest mismatch"),
        ProbabilitySourceError("sensitive source path and digest mismatch"),
    ],
)
def test_probability_research_route_maps_corrupt_artifact_to_integrity_conflict(
    error: ValueError,
) -> None:
    scanner = _ScannerStub()

    def corrupted(_run_id: int) -> dict[str, object]:
        raise error

    scanner.probability_research = corrupted  # type: ignore[method-assign]
    response = _client(scanner).get("/api/market-scans/7/probability-research")

    assert response.status_code == 409
    assert response.json()["detail"] == "研究 artifact 完整性校验失败，已拒绝读取"
    assert "sensitive" not in response.text


@pytest.mark.parametrize(
    "error",
    [
        ProbabilityArtifactError("sensitive probability artifact path"),
        MarketScanSnapshotIntegrityError("sensitive database snapshot identity"),
    ],
)
def test_results_route_maps_artifact_or_snapshot_corruption_to_generic_conflict(
    error: ValueError,
) -> None:
    scanner = _ScannerStub()

    def corrupted(_run_id: int, **_kwargs: object) -> MarketScanResultPage:
        raise error

    scanner.results = corrupted  # type: ignore[method-assign]
    response = _client(scanner).get(f"/api/market-scans/{scanner.previous.id}/results")

    assert response.status_code == 409
    assert response.json()["detail"] == "研究 artifact 完整性校验失败，已拒绝读取"
    assert "sensitive" not in response.text


@pytest.mark.parametrize(
    ("method_name", "path"),
    [
        ("latest_run", "/api/market-scans/latest"),
        ("run", "/api/market-scans/6"),
        ("results", "/api/market-scans/6/results"),
    ],
)
def test_public_scan_reads_redact_snapshot_seal_failures(
    method_name: str,
    path: str,
) -> None:
    scanner = _ScannerStub()

    def corrupted(*_args: object, **_kwargs: object) -> object:
        raise MarketScanSnapshotSealError("摘要 abc123 与 /private/market.db 不一致")

    setattr(scanner, method_name, corrupted)
    response = _client(scanner).get(path)

    assert response.status_code == 409
    assert response.headers["cache-control"] == "no-store"
    assert response.json()["detail"] == MARKET_SCAN_INTEGRITY_DETAIL
    assert "abc123" not in response.text
    assert "private" not in response.text


def test_future_range_research_route_is_read_only_run_bound_and_paginated() -> None:
    scanner = _ScannerStub()
    response = _client(scanner).get(
        f"/api/market-scans/{scanner.previous.id}/future-range-research",
        params={
            "page": 2,
            "page_size": 20,
            "session_offset": 2,
            "symbol": "600519.SH",
            "include_research": False,
        },
    )

    assert response.status_code == 200
    assert response.headers["cache-control"] == "no-store"
    assert response.json()["generation_status"] == "not_generated"
    assert response.json()["research"] is None
    assert scanner.future_range_calls == [
        (scanner.previous.id, 2, 20, 2, "600519.SH", False)
    ]
    invalid = _client(scanner).get(
        f"/api/market-scans/{scanner.previous.id}/future-range-research",
        params={"page_size": 201, "session_offset": 5},
    )
    assert invalid.status_code == 422


def test_future_range_research_route_maps_ineligible_and_corrupt_artifacts() -> None:
    scanner = _ScannerStub()

    def unavailable(_run_id: int, **_kwargs: object) -> dict[str, object]:
        raise FutureRangeResearchUnavailable("未来区间研究仅支持盘后正式批次")

    scanner.future_range_research = unavailable  # type: ignore[method-assign]
    response = _client(scanner).get("/api/market-scans/7/future-range-research")
    assert response.status_code == 422
    assert response.json()["detail"] == "未来区间研究仅支持盘后正式批次"

    def corrupted(_run_id: int, **_kwargs: object) -> dict[str, object]:
        raise FutureRangeArtifactError("sensitive artifact path and digest mismatch")

    scanner.future_range_research = corrupted  # type: ignore[method-assign]
    response = _client(scanner).get("/api/market-scans/7/future-range-research")
    assert response.status_code == 409
    assert response.json()["detail"] == "研究 artifact 完整性校验失败，已拒绝读取"
    assert "sensitive" not in response.text


def test_future_range_research_route_rejects_official_top100_refresh_scope() -> None:
    scanner = _ScannerStub()

    def top100(_run_id: int, **_kwargs: object) -> dict[str, object]:
        raise FutureRangeResearchUnavailable("未来区间研究仅支持盘后正式全市场批次")

    scanner.future_range_research = top100  # type: ignore[method-assign]
    response = _client(scanner).get(
        f"/api/market-scans/{scanner.previous.id}/future-range-research"
    )

    assert response.status_code == 422
    assert response.json()["detail"] == "未来区间研究仅支持盘后正式全市场批次"


def test_probability_store_returns_explicit_not_generated_contract_for_historical_run(tmp_path) -> None:
    research, records = MarketScanProbabilityStore(tmp_path / "missing-artifacts").run_projection(77)

    assert research["run_id"] == 77
    assert research["status"] == "not_generated"
    assert research["default_horizon"] == 5
    assert research["primary_target"] == "net_excess_positive"
    assert research["production_ranking_effect"] == "none"
    assert records == {}
    for horizon in ("1", "5", "20"):
        for target in ("net_excess_positive", "absolute_net_positive"):
            summary = research["horizons"][horizon][target]  # type: ignore[index]
            assert summary["status"] == "not_generated"
            assert summary["probability"] is None


def test_export_route_returns_xlsx_attachment_and_forwards_current_filters() -> None:
    scanner = _ScannerStub()
    client = _client(scanner)

    response = client.get(
        f"/api/market-scans/{scanner.previous.id}/export.xlsx",
        params={
            "status": "all",
            "market": "SZ",
            "industry": "银行",
            "is_st": "false",
            "is_new": "true",
            "min_data_quality_score": 80,
            "keyword": "000001",
            "sort": "score",
            "order": "desc",
        },
    )

    assert response.status_code == 200
    assert response.content == b"xlsx-content"
    assert response.headers["content-type"] == XLSX_MEDIA_TYPE
    assert response.headers["cache-control"] == "no-store"
    assert response.headers["x-content-type-options"] == "nosniff"
    assert response.headers["content-disposition"] == 'attachment; filename="market-scan.xlsx"'
    assert scanner.export_calls == [
        (
            scanner.previous.id,
            MarketScanExportFilters(
                status=None,
                market=("SZ",),
                industry=("银行",),
                is_st=False,
                is_new=True,
                min_data_quality_score=80,
                keyword="000001",
                sort=("score",),
                order=("desc",),
            ),
        )
    ]


def test_export_route_fails_closed_when_future_range_artifact_is_corrupt() -> None:
    scanner = _ScannerStub()

    def corrupted(_run_id: int, *, filters: MarketScanExportFilters) -> MarketScanWorkbookExport:
        del filters
        raise FutureRangeArtifactError("sensitive artifact path")

    scanner.export_results = corrupted  # type: ignore[method-assign]
    response = _client(scanner).get(f"/api/market-scans/{scanner.previous.id}/export.xlsx")

    assert response.status_code == 409
    assert response.json()["detail"] == "研究 artifact 完整性校验失败，已拒绝读取"
    assert "sensitive" not in response.text


def test_export_route_preserves_probability_filter_unavailable_as_422() -> None:
    scanner = _ScannerStub()

    def unavailable(_run_id: int, *, filters: MarketScanExportFilters) -> MarketScanWorkbookExport:
        del filters
        raise ProbabilityFilterUnavailable("当前批次证据不足")

    scanner.export_results = unavailable  # type: ignore[method-assign]
    response = _client(scanner).get(
        f"/api/market-scans/{scanner.previous.id}/export.xlsx",
        params={"min_upside_probability": 0.6},
    )

    assert response.status_code == 422
    assert response.json()["detail"] == "当前批次证据不足"


def test_export_and_results_routes_share_the_same_filter_and_sort_contract() -> None:
    parameters = _client(_ScannerStub()).app.openapi()["paths"]
    results = {item["name"] for item in parameters["/api/market-scans/{run_id}/results"]["get"]["parameters"] if item["in"] == "query"}
    exported = {item["name"] for item in parameters["/api/market-scans/{run_id}/export.xlsx"]["get"]["parameters"] if item["in"] == "query"}

    assert exported == results - {"page", "page_size"}


@pytest.mark.parametrize(
    "query",
    [
        "min_score=90&max_score=80",
        "min_change_pct=5&max_change_pct=-1",
        "market=SH&market=SH",
        "sort=score&sort=score&order=desc&order=asc",
        "sort=score&sort=amount&order=desc",
        "sort=score&sort=amount&sort=symbol&sort=rank&order=desc&order=desc&order=asc&order=asc",
    ],
)
def test_advanced_filter_contract_rejects_inverted_duplicate_or_misaligned_values(query: str) -> None:
    scanner = _ScannerStub()
    response = _client(scanner).get(f"/api/market-scans/7/results?{query}")

    assert response.status_code == 422
    assert scanner.result_calls == []


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
        ("get", "/api/market-scans/7/results?min_confidence=101", None),
        ("get", "/api/market-scans/7/results?max_risk=-1", None),
        ("get", "/api/market-scans/7/results?min_tradability=101", None),
        ("get", "/api/market-scans/7/results?sort=unknown", None),
        ("get", "/api/market-scans/7/results?order=sideways", None),
        ("get", "/api/market-scans/7/results?is_st=perhaps", None),
        ("get", f"/api/market-scans/7/results?industry={'x' * 81}", None),
        ("get", f"/api/market-scans/7/results?keyword={'x' * 81}", None),
        ("get", "/api/market-scans/7/export.xlsx?status=unknown", None),
        ("get", "/api/market-scans/7/export.xlsx?market=HK", None),
        ("get", "/api/market-scans/7/export.xlsx?min_data_quality_score=101", None),
        ("get", f"/api/market-scans/7/export.xlsx?keyword={'x' * 81}", None),
        ("get", "/api/market-scans/7/export.xlsx?sort=unknown", None),
        ("post", "/api/market-scans", {"as_of": "not-a-datetime"}),
        ("post", "/api/market-scans", {"mode": "live"}),
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


def _client(
    scanner: _ScannerStub,
    *,
    admission: MarketScanHeavyReadAdmission | None = None,
) -> TestClient:
    app = FastAPI()
    app.add_exception_handler(RequestValidationError, validation_exception_handler)
    app.include_router(market_scan.router)
    app.dependency_overrides[get_market_scanner] = lambda: scanner
    shared_admission = admission or MarketScanHeavyReadAdmission()
    app.dependency_overrides[get_market_scan_heavy_read_admission] = lambda: shared_admission
    return TestClient(app)


def _production_handler_client(
    scanner: _ScannerStub,
    admission: MarketScanHeavyReadAdmission,
    tmp_path: Path,
) -> TestClient:
    static_dir = tmp_path / "static"
    static_dir.mkdir()
    (static_dir / "index.html").write_text("<!doctype html>", encoding="utf-8")
    application = create_app(
        settings=Settings(
            cache_path=tmp_path / "unused.sqlite3",
            cors_allow_origins=("http://testserver",),
            scheduler_enabled=False,
        ),
        static_dir=static_dir,
    )
    application.dependency_overrides[get_market_scanner] = lambda: scanner
    application.dependency_overrides[get_market_scan_heavy_read_admission] = lambda: admission
    return TestClient(application)


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
        coverage_pct=88.89 if terminal else 0.0,
        created_at="2026-07-17 16:30:00",
        updated_at="2026-07-17 16:31:00" if terminal else "2026-07-17 16:30:00",
        finished_at="2026-07-17 16:31:00" if terminal else None,
        duration_ms=60_000 if terminal else None,
        snapshot_digest="a" * 64 if status in {"success", "degraded"} else None,
        snapshot_seal_origin="publication" if status in {"success", "degraded"} else None,
        snapshot_sealed_at="2026-07-17 16:31:00" if status in {"success", "degraded"} else None,
        message="等待全市场扫描" if not terminal else "扫描结束",
    )


def _valid_result_item(run_id: int) -> MarketScanResultItem:
    return MarketScanResultItem(
        run_id=run_id,
        rank=1,
        symbol="600519.SH",
        code="600519",
        market="SH",
        name="贵州茅台",
        status="success",
        score=90,
        raw_score=90.1,
        trend_score=88,
        leader_score=87,
        data_quality_score=95,
        price=1500,
        data_date="2026-07-17",
        quote_timestamp="2026-07-17 15:00:00",
        quote_observed_at="2026-07-17T07:00:00Z",
        quote_source="fixture",
        kline_source="fixture",
        adjustment_mode="qfq",
        updated_at="2026-07-17 16:31:00",
    )


class _ScannerStub:
    def __init__(self) -> None:
        self.active = _run()
        self.previous = _run(6, status="degraded")
        self.create_calls: list[tuple[datetime | None, str]] = []
        self.latest_calls: list[tuple[str | None, bool]] = []
        self.list_calls: list[tuple[int, int, str | None, str | None, str | None]] = []
        self.detail_calls: list[int] = []
        self.result_calls: list[tuple[int, dict[str, object]]] = []
        self.export_calls: list[tuple[int, MarketScanExportFilters]] = []
        self.future_range_calls: list[tuple[int, int, int, int | None, str | None, bool]] = []
        self.cancel_calls: list[int] = []
        self.retry_calls: list[int] = []
        self.top100_refresh_calls: list[int] = []
        self.polling_identity_calls: list[str] = []

    @property
    def calls(self) -> list[object]:
        return [
            *self.create_calls,
            *self.latest_calls,
            *self.list_calls,
            *self.detail_calls,
            *self.result_calls,
            *self.export_calls,
            *self.future_range_calls,
            *self.cancel_calls,
            *self.retry_calls,
            *self.top100_refresh_calls,
        ]

    async def create_scan(
        self,
        *,
        as_of: datetime | None,
        trigger: str,
        mode: str,
    ) -> MarketScanStartResponse:
        assert trigger == "manual"
        self.create_calls.append((as_of, mode))
        if len(self.create_calls) > 1:
            return MarketScanStartResponse(
                accepted=False,
                deduplicated=True,
                run=self.active,
            )
        return MarketScanStartResponse(accepted=True, run=self.active)

    def latest_run(self, *, mode: str | None = None) -> MarketScanRun:
        self.latest_calls.append((mode, False))
        return self.active

    def latest_published_run(self, *, mode: str | None = None) -> MarketScanRun:
        self.latest_calls.append((mode, True))
        return self.previous

    def polling_identity(self, *, mode: str) -> MarketScanPollingIdentity:
        self.polling_identity_calls.append(mode)
        return MarketScanPollingIdentity(
            request_mode=mode,  # type: ignore[arg-type]
            latest=MarketScanPollingRunToken(run_id=self.active.id, token="a" * 64),
            latest_published=MarketScanPollingRunToken(
                run_id=self.previous.id,
                token="b" * 64,
            ),
            fingerprint="c" * 64,
        )

    def runs(
        self,
        *,
        page: int,
        page_size: int,
        mode: str | None = None,
        status: str | None = None,
        data_date: str | None = None,
    ) -> MarketScanRunPage:
        self.list_calls.append((page, page_size, mode, status, data_date))
        return MarketScanRunPage(
            items=[self.active] if page_size == 1 else [self.active, self.previous],
            total=2,
            page=page,
            page_size=page_size,
            page_count=(2 + page_size - 1) // page_size,
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

    def probability_research(self, run_id: int) -> dict[str, object]:
        return {
            "schema_version": "test-probability-v1",
            "run_id": run_id,
            "status": "insufficient_data",
            "default_horizon": 5,
            "primary_target": "net_excess_positive",
            "horizons": {"1": {}, "5": {}, "20": {}},
        }

    def future_range_research(
        self,
        run_id: int,
        *,
        page: int,
        page_size: int,
        session_offset: int | None,
        symbol: str | None,
        include_research: bool,
    ) -> dict[str, object]:
        self.future_range_calls.append((run_id, page, page_size, session_offset, symbol, include_research))
        return {
            "schema_version": "market-scan-future-range-api-v1",
            "generation_status": "not_generated",
            "artifact": None,
            "research": None,
            "record_page": {
                "page": page,
                "page_size": page_size,
                "total": 0,
                "page_count": 0,
                "session_offset": session_offset,
                "symbol": symbol,
                "items": [],
            },
        }

    def export_results(self, run_id: int, *, filters: MarketScanExportFilters) -> MarketScanWorkbookExport:
        self.export_calls.append((run_id, filters))
        return MarketScanWorkbookExport(content=b"xlsx-content", filename="market-scan.xlsx", row_count=0)

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

    async def refresh_top100_scores(self, run_id: int) -> MarketScanStartResponse:
        self.top100_refresh_calls.append(run_id)
        refreshed = self.previous.model_copy(
            update={
                "id": self.active.id + 1,
                "status": "queued",
                "trigger": "retry",
                "scope": MARKET_SCAN_TOP100_REFRESH_SCOPE,
                "retry_of_run_id": run_id,
                "finished_at": None,
                "duration_ms": None,
                "processed_count": 0,
                "success_count": 0,
                "missing_count": 0,
                "skipped_count": 0,
                "progress_pct": 0,
                "coverage_pct": 0,
                "snapshot_digest": None,
                "snapshot_seal_origin": None,
                "snapshot_sealed_at": None,
            }
        )
        return MarketScanStartResponse(accepted=True, run=refreshed)
