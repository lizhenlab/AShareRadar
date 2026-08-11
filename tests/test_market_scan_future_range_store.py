from __future__ import annotations

from copy import deepcopy
import json
from pathlib import Path

import pytest

from app.models.market_scan import MARKET_SCAN_TOP100_REFRESH_SCOPE, MarketScanRun
from app.services.market_scan_future_range_artifact import (
    FutureRangeArtifactError,
    build_future_range_artifact,
    future_range_artifact_filename,
    write_future_range_artifact,
)
from app.services.market_scan_future_range_store import (
    FutureRangeResearchUnavailable,
    MarketScanFutureRangeStore,
)
from app.services.market_scan_manager import MarketScanManager
from app.services.market_scan_universe import FULL_MARKET_SCOPE


GENERATED_AT = "2026-08-11T18:00:00+08:00"


def test_missing_and_legacy_artifacts_use_explicit_null_projection(tmp_path: Path) -> None:
    directory = tmp_path / "future-range"
    directory.mkdir()
    (directory / "market-scan-future-range-legacy.json").write_text("{}", encoding="utf-8")

    projection = MarketScanFutureRangeStore(directory).research_projection(77, page_size=20)

    assert projection == {
        "schema_version": "market-scan-future-range-api-v1",
        "generation_status": "not_generated",
        "artifact": None,
        "research": None,
        "record_page": {
            "page": 1,
            "page_size": 20,
            "total": 0,
            "page_count": 0,
            "session_offset": None,
            "symbol": None,
            "items": [],
        },
    }


def test_store_pages_filters_and_projects_only_requested_session_offset(tmp_path: Path) -> None:
    directory = tmp_path / "future-range"
    artifact = _artifact()
    write_future_range_artifact(
        directory / future_range_artifact_filename(29, artifact),
        artifact,
        database_path=tmp_path / "cache.sqlite3",
    )
    store = MarketScanFutureRangeStore(directory)

    projection = store.research_projection(
        29,
        page=1,
        page_size=1,
        session_offset=2,
        symbol="600519sh",
    )

    assert projection["generation_status"] == "ready"
    research = projection["research"]
    assert isinstance(research, dict)
    assert "records" not in research
    assert research["record_count"] == 2
    page = projection["record_page"]
    assert isinstance(page, dict)
    assert page["total"] == 1
    assert page["page_count"] == 1
    assert page["session_offset"] == 2
    assert page["symbol"] == "600519SH"
    records = page["items"]
    assert isinstance(records, list)
    assert records[0]["symbol"] == "600519.SH"
    assert len(records[0]["offsets"]) == 1
    projected_offset = records[0]["offsets"][0]
    assert projected_offset["session_offset"] == 2
    assert projected_offset["target_session_date"] == "2026-08-05"
    assert projected_offset["fixed_session_status"] == "not_mature"
    assert projected_offset["level_shift"] is None

    second_page = store.research_projection(29, page=2, page_size=1)
    assert second_page["record_page"]["items"][0]["symbol"] == "000001.SZ"  # type: ignore[index]
    records_only = store.research_projection(29, page_size=1, include_research=False)
    assert records_only["generation_status"] == "ready"
    assert records_only["research"] is None
    assert records_only["record_page"]["total"] == 2  # type: ignore[index]


def test_current_artifact_corruption_fails_closed(tmp_path: Path) -> None:
    directory = tmp_path / "future-range"
    artifact = _artifact()
    path = directory / future_range_artifact_filename(29, artifact)
    write_future_range_artifact(path, artifact, database_path=tmp_path / "cache.sqlite3")
    corrupted = deepcopy(artifact)
    corrupted["payload"]["records"][0]["trend_score"] = 1  # type: ignore[index]
    path.write_text(json.dumps(corrupted, ensure_ascii=False), encoding="utf-8")

    with pytest.raises(FutureRangeArtifactError, match="digest"):
        MarketScanFutureRangeStore(directory).research_projection(29)


def test_manager_allows_only_published_official_runs() -> None:
    manager = object.__new__(MarketScanManager)
    calls: list[tuple[int, dict[str, object]]] = []
    current_run = _run()

    class _Store:
        def research_projection(self, run_id: int, **kwargs: object) -> dict[str, object]:
            calls.append((run_id, kwargs))
            return {"generation_status": "not_generated"}

    class _Cache:
        def market_scan_run(self, _run_id: int) -> MarketScanRun:
            return current_run

    manager._future_range_store = _Store()  # type: ignore[assignment]
    manager.cache = _Cache()  # type: ignore[assignment]

    assert manager.future_range_research(29, page_size=20, session_offset=2) == {
        "generation_status": "not_generated"
    }
    assert calls == [
        (
            29,
            {
                "page": 1,
                "page_size": 20,
                "session_offset": 2,
                "symbol": None,
                "include_research": True,
            },
        )
    ]

    current_run = _run(mode="intraday")
    with pytest.raises(FutureRangeResearchUnavailable, match="盘后正式"):
        manager.future_range_research(29)
    current_run = _run(scope=MARKET_SCAN_TOP100_REFRESH_SCOPE)
    with pytest.raises(FutureRangeResearchUnavailable, match="正式全市场"):
        manager.future_range_research(29)
    current_run = _run(status="running")
    with pytest.raises(FutureRangeResearchUnavailable, match="已发布"):
        manager.future_range_research(29)
    assert len(calls) == 1


def _artifact() -> dict[str, object]:
    return build_future_range_artifact(_payload(), generated_at=GENERATED_AT)


def _run(
    *,
    mode: str = "official",
    status: str = "success",
    scope: str = FULL_MARKET_SCOPE,
) -> MarketScanRun:
    return MarketScanRun(
        id=29,
        status=status,
        trigger="manual",
        mode=mode,
        rule_version="scan-v1",
        as_of="2026-08-03 16:00:00",
        data_date="2026-08-03",
        quote_date="2026-08-03",
        scope=scope,
        total_count=1,
        excluded_count=0,
        processed_count=1,
        success_count=1,
        missing_count=0,
        skipped_count=0,
        retry_count=0,
        progress_pct=100,
        coverage_pct=100,
        created_at="2026-08-03 16:00:00",
        updated_at="2026-08-03 16:10:00",
    )


def _payload() -> dict[str, object]:
    return {
        "report_contract_version": "market-scan-future-range-report-v1",
        "status": "ok",
        "generated_at": GENERATED_AT,
        "run": {
            "run_id": 29,
            "mode": "official",
            "scope": FULL_MARKET_SCOPE,
            "rule_version": "scan-v1",
            "quote_date": "2026-08-03",
            "data_date": "2026-08-03",
        },
        "config": {
            "session_offsets": [1, 2, 3],
            "center_proxy": "HLC3_proxy_not_VWAP",
        },
        "source": {"read_only": True, "adjustment_mode": "qfq"},
        "records": [_record("600519.SH", 1), _record("000001.SZ", 2)],
        "groups": [],
        "rank_ic": [],
        "monotonicity": [],
        "probability_context": {
            "status": "not_available",
            "source": "persisted_oos_calibrated_shadow_only",
        },
        "limitations": [],
    }


def _record(symbol: str, rank: int) -> dict[str, object]:
    return {
        "run_id": 29,
        "quote_date": "2026-08-03",
        "symbol": symbol,
        "name": symbol,
        "market": symbol.split(".")[1],
        "industry": "测试",
        "rank": rank,
        "score": 91 - rank,
        "raw_score": 91.0 - rank,
        "trend_score": 91 - rank,
        "d_bar": {
            "date": "2026-08-03", "open": 9.9, "high": 10.2, "low": 9.8,
            "close": 10.0, "volume": 1000.0, "hlc3_proxy": 10.0,
            "adjustment_mode": "qfq", "data_version": "test-v1",
            "contract_version": "daily-kline.v1",
        },
        "source_evidence": {
            "status": "verified", "payload_digest": "a" * 64,
            "contract_version": "market-scan-point-in-time-feature-evidence-v2",
            "target_adjustment_continuity": "verified",
        },
        "probability": {"status": "not_available", "predictions": []},
        "offsets": [
            _not_mature_offset(1, "2026-08-04"),
            _not_mature_offset(2, "2026-08-05"),
            _not_mature_offset(3, "2026-08-06"),
        ],
    }


def _not_mature_offset(session_offset: int, target_date: str) -> dict[str, object]:
    execution_reason = (
        "A_share_T_plus_1_no_same_session_exit"
        if session_offset == 1
        else "target_exchange_session_not_ingested_yet"
    )
    return {
        "session_offset": session_offset,
        "target_session_date": target_date,
        "fixed_session_status": "not_mature",
        "reason": "target_exchange_session_not_ingested_yet",
        "target_bar": None,
        "target_bar_digest": None,
        "level_shift": None,
        "d_close_reference": None,
        "d1_open_reference": None,
        "execution": {"status": "data_unavailable", "reason": execution_reason},
        "daily_bar_path_unknown": None,
        "interval_structure": None,
    }
