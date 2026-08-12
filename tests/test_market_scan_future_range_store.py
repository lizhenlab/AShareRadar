from __future__ import annotations

from copy import deepcopy
import json
from pathlib import Path

import pytest

import app.services.market_scan_future_range_store as future_range_store_module
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


def test_future_range_store_rejects_symlink_root_and_keeps_missing_root_empty(
    tmp_path: Path,
) -> None:
    directory = tmp_path / "future-range-real"
    artifact = _artifact()
    write_future_range_artifact(
        directory / future_range_artifact_filename(29, artifact),
        artifact,
        database_path=tmp_path / "cache.sqlite3",
    )
    alias = tmp_path / "future-range-alias"
    alias.symlink_to(directory, target_is_directory=True)

    with pytest.raises(FutureRangeArtifactError, match="不是普通目录"):
        MarketScanFutureRangeStore(alias).research_projection(29)

    loop = tmp_path / "future-range-loop"
    loop.symlink_to(loop, target_is_directory=True)
    with pytest.raises(FutureRangeArtifactError, match="目录无法读取"):
        MarketScanFutureRangeStore(loop / "nested").research_projection(29)

    assert MarketScanFutureRangeStore(directory).research_projection(29)["generation_status"] == "ready"
    missing = MarketScanFutureRangeStore(tmp_path / "missing-future-range")
    assert missing.research_projection(29)["generation_status"] == "not_generated"


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


def test_store_validates_query_and_export_boundaries(tmp_path: Path) -> None:
    directory = tmp_path / "future-range"
    store = MarketScanFutureRangeStore(directory)
    for kwargs, message in (
        ({"run_id": False}, "run_id"),
        ({"run_id": 1, "page": 0}, "page"),
        ({"run_id": 1, "page_size": 201}, "page_size"),
        ({"run_id": 1, "symbol": "bad\x00code"}, "symbol"),
    ):
        with pytest.raises(ValueError, match=message):
            store.research_projection(**kwargs)  # type: ignore[arg-type]
    assert store.export_projection(29)["generation_status"] == "not_generated"

    artifact = _artifact()
    write_future_range_artifact(
        directory / future_range_artifact_filename(29, artifact),
        artifact,
        database_path=tmp_path / "cache.sqlite3",
    )
    exported = store.export_projection(29)
    assert exported["generation_status"] == "ready"
    assert exported["record_page"]["total"] == 2  # type: ignore[index]


def test_store_projection_helpers_fail_closed_on_malformed_cached_contracts() -> None:
    artifact = _artifact()
    for path, value, message in (
        (("payload", "run", "run_id"), 30, "请求的 run_id"),
        (("payload", "status"), "unknown", "研究状态"),
        (("payload", "records"), {}, "records 无效"),
    ):
        changed = deepcopy(artifact)
        _set_nested(changed, path, value)
        with pytest.raises(FutureRangeArtifactError, match=message):
            future_range_store_module._artifact_projection(changed, 29)

    with pytest.raises(FutureRangeArtifactError, match="record_page contract"):
        future_range_store_module._paged_projection(
            {"record_page": None}, page=1, page_size=1,
            session_offset=None, symbol=None, include_research=True,
        )
    with pytest.raises(FutureRangeArtifactError, match="records contract"):
        future_range_store_module._paged_projection(
            {"record_page": {"items": None}}, page=1, page_size=1,
            session_offset=None, symbol=None, include_research=True,
        )
    with pytest.raises(FutureRangeArtifactError, match="record.offsets"):
        future_range_store_module._record_offset_projection({"offsets": None}, 1)


@pytest.mark.parametrize(
    ("records", "message"),
    [
        ([None], "record 与 run_id"),
        ([{"run_id": 29, "offsets": None}], "record.offsets"),
        ([{"run_id": 29, "offsets": [{"session_offset": False}]}], "session_offset"),
        (
            [{"run_id": 29, "offsets": [{"session_offset": 1}, {"session_offset": 1}]}],
            "session_offset",
        ),
    ],
)
def test_store_rejects_malformed_record_page_rows(records: list[object], message: str) -> None:
    with pytest.raises(FutureRangeArtifactError, match=message):
        future_range_store_module._validate_run_records(records, 29)


def test_store_filesystem_helpers_reject_nonregular_and_mismatched_candidates(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    regular_file = tmp_path / "not-a-directory"
    regular_file.write_text("x", encoding="utf-8")
    with pytest.raises(FutureRangeArtifactError, match="不是普通目录"):
        future_range_store_module._directory_snapshot(regular_file)
    with pytest.raises(FutureRangeArtifactError, match="无法读取"):
        future_range_store_module._file_fingerprint(tmp_path / "missing.json")
    linked = tmp_path / "linked.json"
    linked.symlink_to(regular_file)
    with pytest.raises(FutureRangeArtifactError, match="不是普通文件"):
        future_range_store_module._file_fingerprint(linked)

    directory = tmp_path / "artifacts"
    artifact = _artifact()
    path = directory / future_range_artifact_filename(29, artifact)
    write_future_range_artifact(path, artifact, database_path=tmp_path / "cache.sqlite3")
    fingerprint = future_range_store_module._file_fingerprint(path)
    loaded = future_range_store_module._load_candidates((fingerprint,), {}, 29)
    assert future_range_store_module._load_candidates((fingerprint,), loaded, 29) == loaded
    assert future_range_store_module._record_matches_symbol({"symbol": "600519.SH"}, "600519.SH") is True

    monkeypatch.setattr(
        future_range_store_module,
        "future_range_artifact_filename",
        lambda _run_id, _artifact: "different.json",
    )
    with pytest.raises(FutureRangeArtifactError, match="文件名与内容摘要"):
        future_range_store_module._load_candidates((fingerprint,), {}, 29)


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


def _set_nested(root: object, path: tuple[str | int, ...], value: object) -> None:
    current = root
    for key in path[:-1]:
        current = current[key]  # type: ignore[index]
    current[path[-1]] = value  # type: ignore[index]
