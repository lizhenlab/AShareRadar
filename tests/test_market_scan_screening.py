from __future__ import annotations

import sqlite3
from typing import Any, Callable, cast

import pytest
from fastapi import FastAPI
from fastapi.exceptions import RequestValidationError
from fastapi.testclient import TestClient

from app.api.deps import get_market_scanner
from app.api.errors import validation_exception_handler
from app.api.routes import market_scan
from app.artifacts.io import canonical_json_text, sha256_hex
from app.market_scan_screening import (
    screen_spec_from_discovery,
    screen_spec_from_market_scan_filters,
)
from app.models.discovery import DiscoveryCriteria, DiscoverySort
from app.models.market_scan import MarketScanResultItem, MarketScanResultStatus, MarketScanRun
from app.models.market_scan_delta import (
    MarketScanDeltaCohort,
    MarketScanDeltaResponse,
    MarketScanDeltaRunRef,
    MarketScanDeltaSummary,
)
from app.models.market_scan_screening import (
    MarketBreadthV1,
    MarketScanScreenEvaluateRequest,
    MarketScanScreenEvaluationV1,
)
from app.repositories.market_scan_screening import (
    MarketScanBreadthRow,
    MarketScanScreeningRow,
)
from app.repositories.market_scan_screening_sql import screen_spec_filter_sql
from app.services.market_scan_screening import (
    MarketScanScreeningService,
    MarketScanScreeningUnavailable,
)
from app.services.market_scan_universe import FULL_MARKET_SCOPE


class _FrozenRepository:
    def __init__(self, run: MarketScanRun, rows: list[MarketScanResultItem]) -> None:
        self.run = run
        self.rows = rows
        self.breadth_calls: list[int] = []
        self.evaluation_calls: list[int] = []
        self.hydrated_symbols: list[tuple[str, ...]] = []

    def market_scan_screening_breadth_snapshot(
        self,
        run_id: int,
    ) -> tuple[MarketScanRun, list[MarketScanBreadthRow]]:
        self.breadth_calls.append(run_id)
        return self.run, [_breadth_projection(item) for item in self.rows]

    def market_scan_screening_evaluation_snapshot(
        self,
        run_id: int,
    ) -> tuple[MarketScanRun, list[MarketScanScreeningRow]]:
        self.evaluation_calls.append(run_id)
        return self.run, [_evaluation_projection(item) for item in self.rows]

    def market_scan_screening_result_items(
        self,
        run_id: int,
        symbols: list[str] | tuple[str, ...],
    ) -> list[MarketScanResultItem]:
        assert run_id == self.run.id
        selected = tuple(symbols)
        self.hydrated_symbols.append(selected)
        by_symbol = {item.symbol: item for item in self.rows}
        return [by_symbol[symbol] for symbol in selected]


def test_breadth_is_complete_null_safe_and_content_addressed() -> None:
    repository = _FrozenRepository(_run(), _rows())
    service = MarketScanScreeningService(repository)

    first = service.breadth(41)
    replay = service.breadth(41)

    assert first == replay
    assert len(first.canonical_digest) == 64
    assert first.canonical_digest == sha256_hex(
        canonical_json_text(first.model_dump(mode="json", exclude={"canonical_digest"}))
    )
    assert first.population.total == 3
    assert first.population.by_status == {"missing": 1, "success": 2}
    assert first.population.by_market == {"SH": 2, "SZ": 1}
    assert first.score.present_count == 2
    assert first.score.missing_count == 1
    assert first.score.mean == 80
    assert first.score.percentiles["p50"] == 80
    assert sum(item.count for item in first.score.bins) == 2
    assert first.change.model_dump() == {
        "advancing": 1,
        "flat": 0,
        "declining": 1,
        "missing": 1,
    }
    missing_industry = next(item for item in first.industries if item.industry is None)
    assert missing_industry.count == 2
    assert missing_industry.score_present_count == 1
    assert missing_industry.average_score == 70
    assert repository.breadth_calls == [41, 41]
    assert repository.evaluation_calls == []
    assert repository.hydrated_symbols == []


def test_evaluation_explains_sequential_funnel_missing_evidence_and_near_miss() -> None:
    repository = _FrozenRepository(_run(), _rows())
    service = MarketScanScreeningService(repository)
    request = MarketScanScreenEvaluateRequest.model_validate(
        {
            "spec": {
                "status": "success",
                "ranges": {
                    "score": {"min": 80},
                    "confidence": {"min": 70},
                },
                "sort": [{"field": "score", "order": "desc"}],
            },
            "near_miss_limit": 10,
        }
    )

    result = service.evaluate(41, request)

    assert len(result.spec_digest) == 64
    assert len(result.canonical_digest) == 64
    assert result.canonical_digest == sha256_hex(
        canonical_json_text(result.model_dump(mode="json", exclude={"canonical_digest"}))
    )
    assert result.population_count == 3
    assert result.matched_count == 1
    assert [item.symbol for item in result.matched.items] == ["600001.SH"]
    assert result.matched_explanations[0].model_dump() == {
        "symbol": "600001.SH",
        "passed_conditions": ["status", "range.score", "range.confidence"],
    }
    assert [
        (step.condition_code, step.input_count, step.matched_count, step.missing_count)
        for step in result.funnel
    ] == [
        ("status", 3, 2, 0),
        ("range.score", 2, 1, 0),
        ("range.confidence", 1, 1, 0),
    ]
    assert [item.item.symbol for item in result.near_misses] == ["000002.SZ"]
    assert result.near_misses[0].failed_conditions[0].code == "range.score"
    score_reason = next(item for item in result.exclusion_reasons if item.code == "range.score")
    assert score_reason.count == 2
    assert score_reason.missing_count == 1
    confidence_reason = next(
        item for item in result.exclusion_reasons if item.code == "range.confidence"
    )
    assert confidence_reason.count == 1
    assert confidence_reason.missing_count == 1
    assert repository.breadth_calls == []
    assert repository.evaluation_calls == [41]
    assert repository.hydrated_symbols == [("600001.SH", "000002.SZ")]


def test_evaluation_hydrates_only_ordered_page_and_bounded_near_misses() -> None:
    rows = [
        _item(
            f"{index:06d}.SH",
            name=f"样本 {index}",
            industry="测试",
            score=100 - index,
            change_pct=float(index),
            confidence=90,
        )
        for index in range(1, 21)
    ]
    repository = _FrozenRepository(_run().model_copy(update={"total_count": len(rows)}), rows)
    request = MarketScanScreenEvaluateRequest.model_validate(
        {
            "spec": {
                "ranges": {"score": {"min": 85}},
                "sort": [{"field": "score", "order": "desc"}],
            },
            "page": 2,
            "page_size": 3,
            "near_miss_limit": 2,
        }
    )

    result = MarketScanScreeningService(repository).evaluate(41, request)

    assert [item.symbol for item in result.matched.items] == [
        "000004.SH",
        "000005.SH",
        "000006.SH",
    ]
    assert [item.item.symbol for item in result.near_misses] == ["000016.SH", "000017.SH"]
    assert repository.hydrated_symbols == [
        ("000004.SH", "000005.SH", "000006.SH", "000016.SH", "000017.SH")
    ]


def test_breadth_contract_rejects_resealed_count_inconsistency() -> None:
    payload = MarketScanScreeningService(_FrozenRepository(_run(), _rows())).breadth(41).model_dump(
        mode="json"
    )
    payload["population"]["by_status"]["success"] = 3
    _reseal(payload)

    with pytest.raises(ValueError, match="by_status.*不守恒"):
        MarketBreadthV1.model_validate(payload)


@pytest.mark.parametrize(
    ("mutation", "message"),
    [
        (lambda payload: payload["funnel"][0].update({"matched_count": 0}), "漏斗.*不守恒"),
        (lambda payload: payload["matched"].update({"page_count": 2}), "page_count"),
        (lambda payload: payload["matched"]["items"][0].update({"run_id": 99}), "归属"),
        (lambda payload: payload.update({"spec_digest": "f" * 64}), "规则摘要"),
        (
            lambda payload: payload["matched_explanations"][0].update(
                {"passed_conditions": ["status"]}
            ),
            "命中解释",
        ),
        (
            lambda payload: payload["near_misses"][0].update(
                {"item": payload["matched"]["items"][0]}
            ),
            "不能与命中分页重叠",
        ),
    ],
)
def test_screen_evaluation_contract_rejects_resealed_inconsistent_payloads(
    mutation: Callable[[dict[str, Any]], object],
    message: str,
) -> None:
    service = MarketScanScreeningService(_FrozenRepository(_run(), _rows()))
    payload = service.evaluate(
        41,
        MarketScanScreenEvaluateRequest.model_validate(
            {"spec": {"status": "success", "ranges": {"score": {"min": 80}}}}
        ),
    ).model_dump(mode="json")
    mutation(payload)
    _reseal(payload)

    with pytest.raises(ValueError, match=message):
        MarketScanScreenEvaluationV1.model_validate(payload)


def test_screen_evaluation_contract_rejects_unsealed_payload_mutation() -> None:
    payload = MarketScanScreeningService(_FrozenRepository(_run(), _rows())).evaluate(
        41,
        MarketScanScreenEvaluateRequest(),
    ).model_dump(mode="json")
    payload["funnel"][0]["label"] = "伪造筛选标签"

    with pytest.raises(ValueError, match="canonical_digest"):
        MarketScanScreenEvaluationV1.model_validate(payload)


def _reseal(payload: dict[str, Any]) -> None:
    payload["canonical_digest"] = sha256_hex(
        canonical_json_text({key: value for key, value in payload.items() if key != "canonical_digest"})
    )


@pytest.mark.parametrize(
    "status,scope,message",
    [
        ("running", FULL_MARKET_SCOPE, "已发布"),
        ("success", "TOP100快速更新评分", "完整全市场"),
    ],
)
def test_screening_fails_closed_for_non_frozen_or_partial_runs(
    status: str,
    scope: str,
    message: str,
) -> None:
    run = _run().model_copy(update={"status": status, "scope": scope})
    service = MarketScanScreeningService(_FrozenRepository(run, _rows()))

    with pytest.raises(MarketScanScreeningUnavailable, match=message):
        service.breadth(run.id)
    with pytest.raises(MarketScanScreeningUnavailable, match=message):
        service.evaluate(run.id, MarketScanScreenEvaluateRequest())


def test_legacy_results_export_and_discovery_adapters_share_one_spec() -> None:
    legacy = screen_spec_from_market_scan_filters(
        status="success",
        market=("SH", "SZ"),
        industry=("半导体",),
        is_st=False,
        is_new=None,
        min_score=80,
        max_score=98,
        min_trend_score=60,
        max_trend_score=None,
        min_change_pct=-2,
        max_change_pct=9,
        min_turnover_rate=1,
        max_turnover_rate=30,
        min_amount=1_000_000,
        max_amount=None,
        min_data_quality_score=75,
        max_data_quality_score=None,
        min_confidence=70,
        max_risk=40,
        min_tradability=65,
        keyword="芯片",
        sort=("score", "symbol"),
        order=("desc", "asc"),
    )
    discovery = screen_spec_from_discovery(
        DiscoveryCriteria.model_validate(
            {
                "market": ["SH", "SZ"],
                "industry": ["半导体"],
                "is_st": False,
                "score": {"min": 80, "max": 98},
                "trend": {"min": 60},
                "change": {"min": -2, "max": 9},
                "turnover": {"min": 1, "max": 30},
                "amount": {"min": 1_000_000},
                "quality": {"min": 75},
                "confidence": {"min": 70},
                "risk": {"max": 40},
                "tradability": {"min": 65},
                "keyword": "芯片",
            }
        ),
        [DiscoverySort(field="score", order="desc"), DiscoverySort(field="symbol", order="asc")],
    )

    assert discovery == legacy


@pytest.mark.parametrize(
    ("keyword", "expected"),
    [
        ("sh", ["600001.SH", "600003.SH"]),
        ("%", []),
        ("_", []),
        ("芯片", ["600001.SH"]),
    ],
)
def test_pure_screen_keyword_matches_sql_like_ascii_fold_and_literal_wildcards(
    keyword: str,
    expected: list[str],
) -> None:
    spec = screen_spec_from_market_scan_filters(
        status=None,
        market=None,
        industry=None,
        is_st=None,
        is_new=None,
        min_data_quality_score=None,
        keyword=keyword,
        sort="symbol",
        order="asc",
    )
    service = MarketScanScreeningService(_FrozenRepository(_run(), _rows()))
    evaluated = service.evaluate(
        41,
        MarketScanScreenEvaluateRequest(spec=spec, page_size=200, near_miss_limit=0),
    )

    assert [item.symbol for item in evaluated.matched.items] == expected
    sql, parameters = screen_spec_filter_sql(spec)
    assert "ESCAPE '\\'" in sql
    escaped = keyword.replace("%", "\\%").replace("_", "\\_")
    assert parameters[-1] == f"%{escaped}%"
    with sqlite3.connect(":memory:") as conn:
        conn.execute("CREATE TABLE result (symbol TEXT, code TEXT, name TEXT)")
        conn.executemany(
            "INSERT INTO result (symbol, code, name) VALUES (?, ?, ?)",
            [(item.symbol, item.code, item.name) for item in _rows()],
        )
        sql_symbols = [
            str(row[0])
            for row in conn.execute(
                f"SELECT symbol FROM result WHERE {sql} ORDER BY symbol ASC",
                parameters,
            ).fetchall()
        ]
    assert sql_symbols == expected


def test_screening_routes_return_typed_payloads_and_map_unavailable_to_422() -> None:
    scanner = _ScreeningScanner()
    app = FastAPI()
    app.add_exception_handler(RequestValidationError, validation_exception_handler)
    app.include_router(market_scan.router)
    app.dependency_overrides[get_market_scanner] = lambda: scanner
    client = TestClient(app)

    breadth = client.get("/api/market-scans/41/breadth")
    evaluated = client.post(
        "/api/market-scans/41/screen/evaluate",
        json={"spec": {"ranges": {"score": {"min": 80}}}},
    )
    unavailable = client.get("/api/market-scans/42/breadth")
    delta = client.get("/api/market-scans/41/delta")
    invalid = client.post(
        "/api/market-scans/41/screen/evaluate",
        json={"spec": {"unsupported": True}},
    )

    assert breadth.status_code == 200
    assert breadth.headers["cache-control"] == "no-store"
    assert breadth.json()["schema_version"] == "market-scan-breadth-v1"
    assert evaluated.status_code == 200
    assert evaluated.json()["matched_count"] == 1
    assert unavailable.status_code == 422
    assert "已发布" in unavailable.json()["detail"]
    assert delta.status_code == 200
    assert delta.json()["schema_version"] == "market-scan-delta-v1"
    assert invalid.status_code == 422
    assert "不支持的字段" in invalid.json()["detail"]


class _ScreeningScanner:
    def __init__(self) -> None:
        self.ready = MarketScanScreeningService(_FrozenRepository(_run(), _rows()))
        running = _run().model_copy(update={"id": 42, "status": "running"})
        self.unavailable = MarketScanScreeningService(_FrozenRepository(running, _rows()))

    def breadth(self, run_id: int):
        return (self.ready if run_id == 41 else self.unavailable).breadth(run_id)

    def evaluate_screen(self, run_id: int, request: MarketScanScreenEvaluateRequest):
        return self.ready.evaluate(run_id, request)

    def delta(self, run_id: int) -> MarketScanDeltaResponse:
        values = {
                "status": "unavailable",
                "unavailable_reason": "previous_same_cohort_not_found",
                "current": {
                    "run_id": run_id,
                    "status": "success",
                    "mode": "official",
                    "scope": FULL_MARKET_SCOPE,
                    "rule_version": "full-market-score-v4",
                    "data_date": "2026-08-11",
                    "finished_at": "2026-08-11 16:10:00",
                    "snapshot_digest": "a" * 64,
                    "snapshot_seal_origin": "publication",
                    "snapshot_sealed_at": "2026-08-11 16:10:00",
                },
                "cohort": {
                    "mode": "official",
                    "scope": FULL_MARKET_SCOPE,
                    "rule_version": "full-market-score-v4",
                },
                "summary": {
                    "previous_present_count": 0,
                    "current_present_count": 3,
                    "compared_symbol_count": 0,
                },
                "canonical_digest": "0" * 64,
            }
        values["current"] = MarketScanDeltaRunRef.model_validate(values["current"])
        values["cohort"] = MarketScanDeltaCohort.model_validate(values["cohort"])
        values["summary"] = MarketScanDeltaSummary.model_validate(values["summary"])
        draft = MarketScanDeltaResponse.model_construct(**values)
        payload = draft.model_dump(mode="json", exclude={"canonical_digest"})
        payload["canonical_digest"] = sha256_hex(canonical_json_text(payload))
        return MarketScanDeltaResponse.model_validate(payload)


def _run() -> MarketScanRun:
    return MarketScanRun(
        id=41,
        status="success",
        trigger="manual",
        mode="official",
        rule_version="full-market-score-v4",
        as_of="2026-08-11 16:00:00",
        data_date="2026-08-11",
        quote_date="2026-08-11",
        scope=FULL_MARKET_SCOPE,
        total_count=3,
        excluded_count=0,
        processed_count=3,
        success_count=2,
        missing_count=1,
        skipped_count=0,
        retry_count=0,
        progress_pct=100,
        coverage_pct=66.67,
        created_at="2026-08-11 16:00:00",
        updated_at="2026-08-11 16:10:00",
        finished_at="2026-08-11 16:10:00",
        snapshot_digest="a" * 64,
        snapshot_seal_origin="publication",
        snapshot_sealed_at="2026-08-11 16:10:00",
    )


def _rows() -> list[MarketScanResultItem]:
    return [
        _item(
            "600001.SH",
            name="芯片龙头",
            industry="半导体",
            score=90,
            change_pct=2,
            confidence=80,
        ),
        _item(
            "000002.SZ",
            name="低分样本",
            industry=None,
            score=70,
            change_pct=-1,
            confidence=90,
        ),
        _item(
            "600003.SH",
            name="缺失样本",
            industry=None,
            status="missing",
            score=None,
            change_pct=None,
            confidence=None,
        ),
    ]


def _item(
    symbol: str,
    *,
    name: str,
    industry: str | None,
    status: str = "success",
    score: int | None,
    change_pct: float | None,
    confidence: float | None,
) -> MarketScanResultItem:
    code, market = symbol.split(".")
    scores = {} if confidence is None else {"confidence": confidence, "risk": 20, "tradability": 90}
    return MarketScanResultItem(
        run_id=41,
        symbol=symbol,
        code=code,
        market=market,
        name=name,
        industry=industry,
        status=cast(MarketScanResultStatus, status),
        score=score,
        change_pct=change_pct,
        score_details={"components": {"score_dimensions": {"scores": scores}}},
        updated_at="2026-08-11 16:10:00",
    )


def _breadth_projection(item: MarketScanResultItem) -> MarketScanBreadthRow:
    return MarketScanBreadthRow(
        status=item.status,
        market=item.market,
        score=item.score,
        change_pct=item.change_pct,
        industry=item.industry,
    )


def _evaluation_projection(item: MarketScanResultItem) -> MarketScanScreeningRow:
    components = item.score_details.get("components")
    dimensions = components.get("score_dimensions") if isinstance(components, dict) else None
    scores = dimensions.get("scores") if isinstance(dimensions, dict) else None
    values = scores if isinstance(scores, dict) else {}
    return MarketScanScreeningRow(
        symbol=item.symbol,
        code=item.code,
        market=item.market,
        name=item.name,
        industry=item.industry,
        is_st=item.is_st,
        is_new=item.is_new,
        status=item.status,
        rank=item.rank,
        score=item.score,
        raw_score=item.raw_score,
        trend_score=item.trend_score,
        data_quality_score=item.data_quality_score,
        change_pct=item.change_pct,
        turnover_rate=item.turnover_rate,
        amount=item.amount,
        alpha_5d=cast(float | None, values.get("alpha_5d")),
        confidence=cast(float | None, values.get("confidence")),
        risk=cast(float | None, values.get("risk")),
        tradability=cast(float | None, values.get("tradability")),
    )
