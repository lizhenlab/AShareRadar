from __future__ import annotations

from collections import Counter
from collections.abc import Mapping
from copy import deepcopy
from datetime import date, datetime
import gzip
import json
from pathlib import Path
from types import SimpleNamespace
from typing import cast

import pytest

import app.services.market_scan_probability_source as probability_source_module
import app.services.market_scan_score_dimensions as score_dimensions_module
import app.services.market_scan_scoring as scoring_module
from app.models.market import Kline, Quote
from app.services.market_scan_probability import stable_probability_hash
from app.services.market_scan_probability_research import probability_feature_vector
from app.services.market_scan_probability_source import (
    PROBABILITY_SOURCE_ARTIFACT_SCHEMA_VERSION,
    PROBABILITY_SOURCE_PAYLOAD_CONTRACT_VERSION,
    ProbabilitySourceError,
    build_probability_source_snapshot,
    canonical_probability_source_json,
    capture_source_snapshot,
    list_probability_source_snapshots,
    load_probability_source_snapshot,
    load_probability_source_snapshot_for_run,
    probability_source_payload_digest,
    probability_source_snapshot_filename,
    project_probability_source_capture,
    verify_probability_source_snapshot,
)
from app.services.market_scan_probability_source_research import (
    MarketScanProbabilitySourceResearchStore,
)
from app.services.market_scan_score_dimensions import (
    MARKET_SCAN_EVIDENCE_CONTRACT_VERSION,
    MARKET_SCAN_EVIDENCE_SCHEMA_VERSION,
    market_scan_dimension_spec,
)
from app.services.market_scan_session_coverage import build_market_scan_session_coverage
from app.services.market_scan_scoring import (
    FULL_MARKET_SCORE_RULE_VERSION,
    market_scan_score_spec,
    stable_score_spec_hash,
)
from app.services.market_scan_universe import FULL_MARKET_SCOPE
from app.services.trading_calendar import trading_dates_between


CAPTURED_AT = "2026-08-11T16:01:00+08:00"
QUOTE_DATE = "2026-08-11"
PRODUCTION_SCORE_SPEC = market_scan_score_spec(min_data_quality_score=50)
PRODUCTION_SCORE_SPEC_HASH = stable_score_spec_hash(PRODUCTION_SCORE_SPEC)
PRODUCTION_MINIMUM_POPULATION = {
    "ALL": 4000,
    "SH": 1800,
    "SZ": 2500,
    "BJ": 200,
}
PRODUCTION_MINIMUM_RATIO = {scope: 0.95 for scope in ("ALL", "SH", "SZ", "BJ")}
PRODUCTION_MINIMUM_ELIGIBLE = {scope: 0.90 for scope in ("ALL", "SH", "SZ", "BJ")}


@pytest.fixture(autouse=True)
def _compact_source_coverage_contract(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(
        probability_source_module,
        "PROBABILITY_SOURCE_MINIMUM_POPULATION",
        {"ALL": 1, "SH": 1, "SZ": 1, "BJ": 1},
    )
    monkeypatch.setattr(
        probability_source_module,
        "PROBABILITY_SOURCE_MINIMUM_COVERAGE",
        {scope: 0.0 for scope in ("ALL", "SH", "SZ", "BJ")},
    )
    monkeypatch.setattr(
        probability_source_module,
        "PROBABILITY_SOURCE_MINIMUM_ELIGIBLE_RATIO",
        {scope: 0.0 for scope in ("ALL", "SH", "SZ", "BJ")},
    )


def test_source_capture_is_content_addressed_atomic_compact_and_restart_loadable(tmp_path: Path) -> None:
    projection = _source_projection(
        ("600519.SH", "SH", "SH_MAIN"), ("300750.SZ", "SZ", "CHINEXT"),
    )
    reversed_projection = project_probability_source_capture(
        projection["run"],
        [
            _result_item("300750.SZ", "SZ", "CHINEXT"),
            _result_item("600519.SH", "SH", "SH_MAIN"),
        ],
        canonical_published=True,
    )
    first_dir, second_dir = tmp_path / "first", tmp_path / "second"

    first = _capture_current_source(first_dir, projection)
    repeated = _capture_current_source(first_dir, projection)
    second = _capture_current_source(second_dir, reversed_projection)

    assert first == repeated
    assert first["digest"] == second["digest"]
    assert Path(cast(str, first["path"])).read_bytes() == Path(cast(str, second["path"])).read_bytes()
    assert cast(dict[str, int], cast(dict[str, object], first["quality"])["market_counts"]) == {"SH": 1, "SZ": 1}
    storage = cast(dict[str, object], first["storage"])
    assert cast(int, storage["compressed_bytes"]) < cast(int, storage["uncompressed_bytes"])
    assert 0 < cast(float, storage["compression_ratio"]) < 1

    loaded = load_probability_source_snapshot(cast(str, first["path"]))
    assert loaded["schema_version"] == PROBABILITY_SOURCE_ARTIFACT_SCHEMA_VERSION
    payload = cast(dict[str, object], loaded["payload"])
    assert payload["contract_version"] == PROBABILITY_SOURCE_PAYLOAD_CONTRACT_VERSION
    assert cast(dict[str, object], payload["quality"])["record_coverage"] == 1.0
    quality = cast(dict[str, object], payload["quality"])
    assert quality["run_total_count"] == 4
    assert quality["run_success_count"] == 2
    assert quality["success_to_total_coverage"] == pytest.approx(2 / 4)
    assert quality["run_skipped_count"] == 1
    assert quality["run_eligible_count"] == 3
    strata = cast(dict[str, dict[str, object]], quality["strata_coverage"])
    assert set(strata) == {"market", "board", "industry", "liquidity", "regime"}
    assert all(value["coverage"] == 1.0 for value in strata.values())
    assert all(value["missing_count"] == 0 for value in strata.values())
    archived = cast(list[dict[str, object]], payload["records"])
    assert [item["symbol"] for item in archived] == ["300750.SZ", "600519.SH"]
    assert all("source_evidence" not in item and "score_details" not in item for item in archived)
    assert "bar_contract_61" not in canonical_probability_source_json(loaded)

    # The list/load-for-run calls create no in-memory store and therefore prove
    # that verification and selection survive a process restart boundary.
    listed = list_probability_source_snapshots(first_dir)
    assert listed == [first]
    assert load_probability_source_snapshot_for_run(first_dir, 70) == loaded
    assert load_probability_source_snapshot_for_run(first_dir, 999) is None


def test_source_load_for_run_orders_offset_timestamps_by_instant(tmp_path: Path) -> None:
    directory = tmp_path / "offset-order"
    projection = _source_projection(("600519.SH", "SH", "SH_MAIN"))
    _capture_current_source(
        directory, projection,
        captured_at="2026-08-11T17:00:00+08:00",
    )
    newer = _capture_current_source(
        directory, projection,
        captured_at="2026-08-11T10:00:00+00:00",
    )

    loaded = load_probability_source_snapshot_for_run(directory, 70)

    assert loaded is not None
    assert loaded["captured_at"] == "2026-08-11T10:00:00+00:00"
    assert cast(dict[str, object], loaded["integrity"])["integrity_digest"] == newer["digest"]


def test_source_load_and_list_reject_symlink_paths(tmp_path: Path) -> None:
    real_directory = tmp_path / "real"
    info = _capture_current_source(
        real_directory, _source_projection(("600519.SH", "SH", "SH_MAIN")),
    )
    source = Path(cast(str, info["path"]))
    link_directory = tmp_path / "links"
    link_directory.mkdir()
    archive_link = link_directory / source.name
    archive_link.symlink_to(source)
    root_link = tmp_path / "archive-root-link"
    root_link.symlink_to(real_directory, target_is_directory=True)

    with pytest.raises(ProbabilitySourceError, match="必须是普通文件"):
        load_probability_source_snapshot(archive_link)
    with pytest.raises(ProbabilitySourceError, match="路径不是目录"):
        list_probability_source_snapshots(root_link)

    empty_nested = real_directory / "empty-nested"
    empty_nested.mkdir()
    with pytest.raises(ProbabilitySourceError, match="路径不是目录"):
        list_probability_source_snapshots(root_link / "empty-nested")

    loop = tmp_path / "archive-root-loop"
    loop.symlink_to(loop, target_is_directory=True)
    with pytest.raises(ProbabilitySourceError, match="目录无法读取"):
        list_probability_source_snapshots(loop / "nested")


def test_source_capture_rejects_symlink_output_root_without_writing_target(tmp_path: Path) -> None:
    real_directory = tmp_path / "real"
    real_directory.mkdir()
    root_link = tmp_path / "archive-root-link"
    root_link.symlink_to(real_directory, target_is_directory=True)

    projection = _source_projection(("600519.SH", "SH", "SH_MAIN"))
    with pytest.raises(ProbabilitySourceError, match="输出目录必须是真实目录"):
        _capture_current_source(root_link, projection)

    assert list(real_directory.iterdir()) == []

    nested_root = root_link / "not-created"
    with pytest.raises(ProbabilitySourceError, match="输出目录必须是真实目录"):
        _capture_current_source(nested_root, projection)
    assert not (real_directory / "not-created").exists()


def test_source_load_rejects_oversize_archive(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    info = _capture_current_source(
        tmp_path, _source_projection(("600519.SH", "SH", "SH_MAIN")),
    )
    source = Path(cast(str, info["path"]))
    monkeypatch.setattr(
        probability_source_module,
        "PROBABILITY_SOURCE_MAX_COMPRESSED_BYTES",
        source.stat().st_size - 1,
    )

    with pytest.raises(ProbabilitySourceError, match="超过压缩大小上限"):
        load_probability_source_snapshot(source)


def test_source_projection_accepts_models_or_mappings_and_normalizes_runtime_local_as_of(tmp_path: Path) -> None:
    run = {
        **_run(success_count=1),
        "id": 70,
        "as_of": "2026-08-11 16:00:00",
    }
    run.pop("run_id")
    item = _result_item("600519.SH", "SH", "SH_MAIN")

    projection = project_probability_source_capture(run, [item], canonical_published=True)

    projected_run = cast(dict[str, object], projection["run"])
    assert projected_run["as_of"] == "2026-08-11T16:00:00+08:00"
    artifact = build_probability_source_snapshot(
        run=projected_run,
        records=cast(list[dict[str, object]], projection["records"]),
        captured_at="2026-08-11T16:02:00+08:00",
        projection_receipt=projection,
    )
    info = capture_source_snapshot(
        tmp_path,
        run=projected_run,
        records=cast(list[dict[str, object]], projection["records"]),
        captured_at="2026-08-11T16:02:00+08:00",
        projection_receipt=projection,
    )
    assert load_probability_source_snapshot(cast(str, info["path"])) == artifact

    with pytest.raises(ProbabilitySourceError, match="canonical_published=True"):
        project_probability_source_capture(run, [item], canonical_published=False)
    with pytest.raises(ProbabilitySourceError, match="timestamp 必须包含时区"):
        probability_source_module._normalize_run(
            {**projected_run, "as_of": "2026-08-11 16:00:00"},
            captured_at=CAPTURED_AT,
            exact_keys=False,
        )

    changed_score = deepcopy(item)
    changed_score["score_details"]["components"]["score_dimensions"]["scores"]["risk"] += 1
    with pytest.raises(ProbabilitySourceError, match="未绑定 persisted result 上下文"):
        project_probability_source_capture(run, [changed_score], canonical_published=True)

    legacy = deepcopy(item)
    legacy_evidence = legacy["score_details"]["components"]["score_dimensions"]["point_in_time_evidence"]
    legacy_evidence["contract_version"] = "market-scan-point-in-time-feature-evidence-v2"
    with pytest.raises(ProbabilitySourceError, match="未绑定 persisted result 上下文"):
        project_probability_source_capture(run, [legacy], canonical_published=True)


def test_current_source_accepts_run87_weekend_rollover_and_rejects_wrong_session() -> None:
    quote_date = "2026-08-14"
    as_of = "2026-08-15T00:38:35+08:00"
    item = _result_item(
        "600519.SH",
        "SH",
        "SH_MAIN",
        quote_date=quote_date,
        run_as_of=as_of,
        quote_timestamp="2026-08-14T16:14:15+08:00",
        quote_observed_at=as_of,
    )
    run = _run(
        success_count=1,
        quote_date=quote_date,
        as_of=as_of,
    )
    projection = project_probability_source_capture(
        run,
        [item],
        canonical_published=True,
    )

    snapshot = _build_current_source(
        projection,
        captured_at="2026-08-15T00:49:29+08:00",
    )

    payload = cast(dict[str, object], snapshot["payload"])
    projected_run = cast(dict[str, object], payload["run"])
    assert projected_run["quote_date"] == quote_date
    assert projected_run["data_date"] == quote_date
    assert projected_run["as_of"] == as_of
    assert snapshot["captured_at"] == "2026-08-15T00:49:29+08:00"

    stale_projection = project_probability_source_capture(
        {**run, "as_of": "2026-08-17T15:15:00+08:00"},
        [item],
        canonical_published=True,
    )
    with pytest.raises(ProbabilitySourceError, match="时间顺序无效"):
        _build_current_source(
            stale_projection,
            captured_at="2026-08-17T15:16:00+08:00",
        )


def test_current_source_rejects_before_close_and_capture_before_decision() -> None:
    quote_date = "2026-08-14"
    before_close = "2026-08-14T15:14:59+08:00"
    item = _result_item(
        "600519.SH",
        "SH",
        "SH_MAIN",
        quote_date=quote_date,
        run_as_of=before_close,
    )
    projection = project_probability_source_capture(
        _run(success_count=1, quote_date=quote_date, as_of=before_close),
        [item],
        canonical_published=True,
    )
    with pytest.raises(ProbabilitySourceError, match="时间顺序无效"):
        _build_current_source(
            projection,
            captured_at="2026-08-14T16:00:00+08:00",
        )

    overnight_as_of = "2026-08-15T00:38:35+08:00"
    overnight_projection = project_probability_source_capture(
        _run(success_count=1, quote_date=quote_date, as_of=overnight_as_of),
        [item],
        canonical_published=True,
    )
    with pytest.raises(ProbabilitySourceError, match="时间顺序无效"):
        _build_current_source(
            overnight_projection,
            captured_at="2026-08-15T00:38:34+08:00",
        )


@pytest.mark.parametrize("generation", ("legacy", "previous"))
def test_historical_source_time_validation_does_not_call_current_calendar(
    monkeypatch: pytest.MonkeyPatch,
    generation: str,
) -> None:
    def unexpected(*_args: object, **_kwargs: object) -> bool:
        raise AssertionError("legacy source must not consult current calendar")

    monkeypatch.setattr(
        probability_source_module,
        "current_official_source_temporal_contract_matches",
        unexpected,
    )
    historical_run = _run(success_count=1)
    if generation == "previous":
        historical_run["production_score_rule_version"] = (
            scoring_module.FULL_MARKET_LEGACY_V4_SCORE_RULE_VERSION
        )
        historical_run["production_score_spec_hash"] = (
            "30c5abb10b676fc71b5fa6c621cce809a6c2d054113fa578d77eccf28fb5955a"
        )
        coverage = cast(dict[str, object], historical_run["full_market_coverage"])
        coverage["contract_version"] = (
            probability_source_module.LEGACY_PROBABILITY_SOURCE_COVERAGE_CONTRACT_VERSION
        )
        for scope in cast(dict[str, dict[str, object]], coverage["scopes"]).values():
            scope.pop("missing_count")
            scope.pop("skipped_count")

    normalized = probability_source_module._normalize_run(  # noqa: SLF001
        historical_run,
        captured_at="2026-08-11T23:59:59+08:00",
        exact_keys=False,
        legacy=generation == "legacy",
        previous=generation == "previous",
    )

    assert normalized["as_of"] == "2026-08-11T16:00:00+08:00"

    weekend_run = deepcopy(historical_run)
    weekend_run["as_of"] = "2026-08-15T00:38:35+08:00"
    with pytest.raises(ProbabilitySourceError, match="时间顺序无效"):
        probability_source_module._normalize_run(  # noqa: SLF001
            weekend_run,
            captured_at="2026-08-15T00:49:29+08:00",
            exact_keys=False,
            legacy=generation == "legacy",
            previous=generation == "previous",
        )


@pytest.mark.parametrize(
    ("provider_timestamp", "expected_timestamp"),
    (
        ("2026-08-11 15:00:00", "2026-08-11T15:00:00+08:00"),
        ("2026-08-11T07:00:00Z", "2026-08-11T15:00:00+08:00"),
    ),
)
def test_source_projection_normalizes_verified_provider_quote_time_to_shanghai(
    provider_timestamp: str,
    expected_timestamp: str,
) -> None:
    item = _result_item(
        "600519.SH",
        "SH",
        "SH_MAIN",
        quote_timestamp=provider_timestamp,
    )

    projection = project_probability_source_capture(
        _run(success_count=1),
        [item],
        canonical_published=True,
    )
    artifact = _build_current_source(projection)
    records = cast(
        list[dict[str, object]],
        cast(dict[str, object], artifact["payload"])["records"],
    )
    instrument = cast(dict[str, object], records[0]["instrument"])
    assert instrument["quote_timestamp"] == expected_timestamp

    tampered = dict(instrument)
    tampered["quote_timestamp"] = "2026-08-11 15:00:00"
    with pytest.raises(ProbabilitySourceError, match="timestamp 必须包含时区"):
        probability_source_module._instrument(  # noqa: SLF001
            tampered,
            "600519.SH",
            cast(dict[str, object], records[0]["dimensions"]),
            cast(dict[str, object], artifact["payload"])["run"],
        )


def test_source_capture_fails_closed_on_scope_completeness_features_and_evidence() -> None:
    record = _capture_record("600519.SH", "SH", "SH_MAIN")
    with pytest.raises(ProbabilitySourceError, match="projection receipt"):
        build_probability_source_snapshot(
            run=_run(success_count=1), records=[record], captured_at=CAPTURED_AT,
        )
    with pytest.raises(TypeError, match="context verifier"):
        probability_source_module.VerifiedProbabilitySourceProjection(encoded="{}")
    projection = _source_projection(("600519.SH", "SH", "SH_MAIN"))
    with pytest.raises(ProbabilitySourceError, match="receipt 与 run/records 不一致"):
        build_probability_source_snapshot(
            run={**cast(dict[str, object], projection["run"]), "mode": "intraday"},
            records=cast(list[dict[str, object]], projection["records"]),
            captured_at=CAPTURED_AT,
            projection_receipt=projection,
        )
    changed = deepcopy(cast(list[dict[str, object]], projection["records"]))
    cast(dict[str, float], changed[0]["features"])["trend_score"] += 1
    with pytest.raises(ProbabilitySourceError, match="receipt 与 run/records 不一致"):
        build_probability_source_snapshot(
            run=cast(dict[str, object], projection["run"]), records=changed,
            captured_at=CAPTURED_AT, projection_receipt=projection,
        )


def test_source_archive_rejects_tamper_duplicate_keys_wrong_filename_and_resealed_semantics(tmp_path: Path) -> None:
    projection = _source_projection(("600519.SH", "SH", "SH_MAIN"))
    artifact = _build_current_source(projection)
    info = _capture_current_source(tmp_path / "tamper", projection)
    source = Path(cast(str, info["path"]))

    changed = json.loads(gzip.decompress(source.read_bytes()))
    changed["payload"]["records"][0]["features"]["trend_score"] += 1
    source.write_bytes(gzip.compress(json.dumps(changed, separators=(",", ":")).encode(), compresslevel=9, mtime=0))
    with pytest.raises(ProbabilitySourceError, match="feature_vector_digest"):
        load_probability_source_snapshot(source)

    duplicate_info = _capture_current_source(tmp_path / "duplicate", projection)
    duplicate = Path(cast(str, duplicate_info["path"]))
    text = gzip.decompress(duplicate.read_bytes()).decode()
    text = text.replace('{"captured_at":', '{"captured_at":"duplicate","captured_at":', 1)
    duplicate.write_bytes(gzip.compress(text.encode(), compresslevel=9, mtime=0))
    with pytest.raises(ProbabilitySourceError, match="重复 JSON key"):
        load_probability_source_snapshot(duplicate)

    valid_info = _capture_current_source(tmp_path / "rename", projection)
    valid = Path(cast(str, valid_info["path"]))
    wrong = valid.with_name(valid.name.replace(cast(str, valid_info["digest"]), "0" * 64))
    wrong.write_bytes(valid.read_bytes())
    with pytest.raises(ProbabilitySourceError, match="文件名与内容地址冲突"):
        load_probability_source_snapshot(wrong)

    resealed = deepcopy(artifact)
    payload = cast(dict[str, object], resealed["payload"])
    archived = cast(list[dict[str, object]], payload["records"])[0]
    cast(dict[str, object], archived["instrument"])["board"] = "STAR"
    cast(dict[str, object], resealed["integrity"])["integrity_digest"] = probability_source_payload_digest(payload)
    with pytest.raises(ProbabilitySourceError, match="instrument 与 dimensions 冲突"):
        verify_probability_source_snapshot(resealed)


def test_source_filename_and_quality_are_replay_bound() -> None:
    artifact = _build_current_source(_source_projection(("600519.SH", "SH", "SH_MAIN")))
    assert probability_source_snapshot_filename(70, artifact).startswith("market-scan-probability-source-run-70-")
    corrupted = deepcopy(artifact)
    payload = cast(dict[str, object], corrupted["payload"])
    cast(dict[str, object], payload["quality"])["record_count"] = 2
    cast(dict[str, object], corrupted["integrity"])["integrity_digest"] = probability_source_payload_digest(payload)
    with pytest.raises(ProbabilitySourceError, match="quality 不能由 records 重放"):
        verify_probability_source_snapshot(corrupted)


def test_legacy_v1_source_remains_readable_but_has_no_current_score_contract() -> None:
    current = _build_current_source(_source_projection(("600519.SH", "SH", "SH_MAIN")))
    legacy = deepcopy(current)
    legacy["schema_version"] = probability_source_module.LEGACY_PROBABILITY_SOURCE_ARTIFACT_SCHEMA_VERSION
    payload = cast(dict[str, object], legacy["payload"])
    payload["contract_version"] = probability_source_module.LEGACY_PROBABILITY_SOURCE_PAYLOAD_CONTRACT_VERSION
    for record in cast(list[dict[str, object]], payload["records"]):
        record["source_evidence_contract_version"] = (
            probability_source_module.MARKET_SCAN_EVIDENCE_LEGACY_V2_CONTRACT_VERSION
        )
    run = cast(dict[str, object], payload["run"])
    run.pop("production_score_rule_version")
    run.pop("production_score_spec_hash")
    run.pop("full_market_coverage")
    run.pop("skipped_count")
    score_semantics = cast(dict[str, object], payload["score_semantics"])
    score_semantics.pop("production_score_spec_hash")
    score_semantics["production_rule_version"] = (
        scoring_module.FULL_MARKET_LEGACY_V4_SCORE_RULE_VERSION
    )
    quality = cast(dict[str, object], payload["quality"])
    for name in (
        "run_total_count",
        "run_success_count",
        "success_to_total_coverage",
        "strata_coverage",
        "full_market_coverage",
        "run_skipped_count",
        "run_eligible_count",
    ):
        quality.pop(name)
    cast(dict[str, object], legacy["integrity"])["integrity_digest"] = probability_source_payload_digest(payload)

    verified = verify_probability_source_snapshot(legacy)

    assert verified["schema_version"] == probability_source_module.LEGACY_PROBABILITY_SOURCE_ARTIFACT_SCHEMA_VERSION
    assert cast(dict[str, object], verified["payload"])["contract_version"] == (
        probability_source_module.LEGACY_PROBABILITY_SOURCE_PAYLOAD_CONTRACT_VERSION
    )
    archived = cast(list[dict[str, object]], cast(dict[str, object], verified["payload"])["records"])
    assert archived[0]["source_evidence_contract_version"] == (
        probability_source_module.MARKET_SCAN_EVIDENCE_LEGACY_V2_CONTRACT_VERSION
    )


def test_frozen_v2_v4_source_archive_remains_readable_and_preloadable(
    tmp_path: Path,
) -> None:
    fixture_path = (
        Path(__file__).parent
        / "fixtures"
        / "market_scan_probability_source_v2_v4.json"
    )
    artifact = cast(dict[str, object], json.loads(fixture_path.read_text(encoding="utf-8")))
    payload = cast(dict[str, object], artifact["payload"])
    run = cast(dict[str, object], payload["run"])

    # This committed literal is cut from a real v4 source row.  It must never be
    # regenerated from the current score builder or its migration test would be
    # self-fulfilling after the writable generation changes again.
    assert artifact["schema_version"] == "market-scan-probability-source-artifact-v2"
    assert payload["contract_version"] == "market-scan-probability-source-snapshot-v2"
    assert run["production_score_rule_version"] == "full-market-score-v4"
    assert run["production_score_spec_hash"] == (
        "30c5abb10b676fc71b5fa6c621cce809a6c2d054113fa578d77eccf28fb5955a"
    )
    verified = verify_probability_source_snapshot(artifact)

    directory = tmp_path / "market_scan_probability_source"
    directory.mkdir()
    filename = probability_source_snapshot_filename(71, verified)
    archive = directory / filename
    archive.write_bytes(probability_source_module._compressed_artifact_bytes(verified))  # noqa: SLF001

    assert load_probability_source_snapshot(archive) == verified
    store = MarketScanProbabilitySourceResearchStore(directory)
    assert store.preload() == 1
    research = store.research_projection(71)
    binding = cast(dict[str, object], research["run_binding"])
    assert binding["production_score_rule_version"] == "full-market-score-v4"
    assert binding["production_score_spec_hash"] == run["production_score_spec_hash"]


def test_source_archive_mechanical_errors_are_translated_by_public_boundary(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    artifact = _build_current_source(_source_projection(("600519.SH", "SH", "SH_MAIN")))
    target = tmp_path / probability_source_snapshot_filename(70, artifact)
    publisher_errors = (
        (probability_source_module.ArtifactContentConflictError(target), "内容不同"),
        (probability_source_module.ArtifactPublishConflictError(target), "并发发布冲突"),
        (probability_source_module.ArtifactNotDirectoryError(target.parent), "输出目录必须是真实目录"),
        (probability_source_module.ArtifactNotRegularError(target), "target 不是普通文件"),
        (probability_source_module.ArtifactTooLargeError(target, max_bytes=1), "超过压缩大小上限"),
        (probability_source_module.ArtifactIOError("read failed"), "无法读取"),
        (OSError("write failed"), "写入失败"),
    )
    for error, message in publisher_errors:
        with monkeypatch.context() as scoped:
            scoped.setattr(
                probability_source_module,
                "exclusive_atomic_publish",
                lambda *_args, _error=error, **_kwargs: (_ for _ in ()).throw(_error),
            )
            with pytest.raises(ProbabilitySourceError, match=message):
                probability_source_module._write_probability_source_snapshot(target, artifact)

    preserved = ProbabilitySourceError("preserved domain error")
    with monkeypatch.context() as scoped:
        scoped.setattr(
            probability_source_module,
            "exclusive_atomic_publish",
            lambda *_args, **_kwargs: (_ for _ in ()).throw(preserved),
        )
        with pytest.raises(ProbabilitySourceError) as caught:
            probability_source_module._write_probability_source_snapshot(target, artifact)
    assert caught.value is preserved

    read_errors = (
        (probability_source_module.ArtifactNotRegularError(target), "必须是普通文件"),
        (probability_source_module.ArtifactTooLargeError(target, max_bytes=1), "超过压缩大小上限"),
        (probability_source_module.ArtifactChangedError(target, stage="open"), "打开期间"),
        (probability_source_module.ArtifactChangedError(target, stage="read"), "读取期间"),
        (probability_source_module.ArtifactIOError("read failed"), "无法读取"),
    )
    for error, message in read_errors:
        with monkeypatch.context() as scoped:
            scoped.setattr(
                probability_source_module,
                "read_regular_file",
                lambda *_args, _error=error, **_kwargs: (_ for _ in ()).throw(_error),
            )
            with pytest.raises(ProbabilitySourceError, match=message):
                probability_source_module._read_regular_file(target)

    with pytest.raises(ProbabilitySourceError, match="gzip 损坏"):
        probability_source_module._decompress(b"not-gzip", target)
    with monkeypatch.context() as scoped:
        scoped.setattr(probability_source_module, "PROBABILITY_SOURCE_MAX_UNCOMPRESSED_BYTES", 1)
        with pytest.raises(ProbabilitySourceError, match="解压内容超过"):
            probability_source_module._decompress(gzip.compress(b"12"), target)
    for encoded, message in (
        (b'{"value":NaN}', "非有限"),
        (b"{", "JSON 损坏"),
        (b"[]", "顶层必须是 JSON object"),
    ):
        with pytest.raises(ProbabilitySourceError, match=message):
            probability_source_module._decode_artifact(encoded, target)

    with monkeypatch.context() as scoped:
        scoped.setattr(probability_source_module, "PROBABILITY_SOURCE_MAX_UNCOMPRESSED_BYTES", 1)
        with pytest.raises(ProbabilitySourceError, match="未压缩内容超过"):
            probability_source_module._compressed_artifact_bytes(artifact)
    with monkeypatch.context() as scoped:
        scoped.setattr(probability_source_module, "PROBABILITY_SOURCE_MAX_COMPRESSED_BYTES", 1)
        with pytest.raises(ProbabilitySourceError, match="压缩内容超过"):
            probability_source_module._compressed_artifact_bytes(artifact)


def test_source_scalar_run_and_identity_validators_reject_ambiguous_values() -> None:
    base_run = _run(success_count=1)
    run_cases = (
        ({key: value for key, value in base_run.items() if key != "status"}, "缺少字段"),
        ({**base_run, "status": "running"}, "success/degraded"),
        ({**base_run, "canonical_published": False}, "canonical_published"),
        ({**base_run, "data_date": "2026-08-10"}, "quote_date/data_date"),
        ({**base_run, "as_of": "2026-08-12T16:00:00+08:00"}, "时间顺序"),
        ({**base_run, "total_count": 0}, "success_count 超过"),
    )
    for run, message in run_cases:
        with pytest.raises(ProbabilitySourceError, match=message):
            probability_source_module._normalize_run(
                run,
                captured_at=CAPTURED_AT,
                exact_keys=False,
            )

    invalid_calls = (
        (probability_source_module._json_value, (float("nan"), "value")),
        (probability_source_module._json_value, ({1, 2}, "value")),
        (probability_source_module._json_mapping, ({1: "value"}, "value")),
        (probability_source_module._mapping, ([], "value")),
        (probability_source_module._mapping, ({1: "value"}, "value")),
        (probability_source_module._exact_keys, ({"a": 1}, frozenset({"b"}), "value")),
        (probability_source_module._text, (" padded ", "value")),
        (probability_source_module._symbol, ("invalid",)),
        (probability_source_module._run_id, (True, "value")),
        (probability_source_module._nonnegative_integer, (-1, "value")),
        (probability_source_module._bounded_integer, (3, "value", 0, 2)),
        (probability_source_module._number, (float("inf"), "value")),
        (probability_source_module._positive_number, (0, "value")),
        (probability_source_module._nonnegative_number, (-1, "value")),
        (probability_source_module._boolean, (1, "value")),
        (probability_source_module._date_text, ("bad-date", "value")),
        (probability_source_module._date_text, ("20260811", "value")),
        (probability_source_module._optional_date, ("2026-08-12", "value")),
        (probability_source_module._projection_timestamp, ("bad-time", "value")),
        (probability_source_module._parsed_timestamp, ("bad-time",)),
        (probability_source_module._sha256, ("bad", "value")),
        (probability_source_module._object_mapping, (object(), "value")),
    )
    for function, args in invalid_calls:
        kwargs = {"maximum": QUOTE_DATE} if function is probability_source_module._optional_date else {}
        with pytest.raises(ProbabilitySourceError):
            function(*args, **kwargs)

    assert probability_source_module._optional_text(None) is None
    assert probability_source_module._optional_date(None, "value", maximum=QUOTE_DATE) is None
    assert probability_source_module._derived_board("688001.SH", "SH") == "STAR"
    assert probability_source_module._derived_board("300001.SZ", "SZ") == "CHINEXT"
    for bars, message in (
        ([], "缺少"),
        ([["short"]], "bar contract 无效"),
        ([[None, None, None, None, None, None, "hfq", None, None]], "qfq"),
    ):
        with pytest.raises(ProbabilitySourceError, match=message):
            probability_source_module._evidence_adjustment_mode(bars, "600519.SH")
    with pytest.raises(ProbabilitySourceError, match="market/board"):
        probability_source_module._validate_symbol_market_board("600519.SH", "SZ", "SZ_MAIN")
    with pytest.raises(ProbabilitySourceError, match="segment"):
        probability_source_module._validate_segment_flags("600519.SH", "new", False, False)
    with pytest.raises(ProbabilitySourceError, match="文件名不规范"):
        probability_source_module._filename_identity(Path("invalid.json"))


def test_source_snapshot_contract_dimension_and_feature_mutations_fail_closed() -> None:
    run = _run(success_count=1)
    record = _capture_record("600519.SH", "SH", "SH_MAIN")
    artifact = _build_current_source(_source_projection(("600519.SH", "SH", "SH_MAIN")))
    for path, value, message in (
        (("schema_version",), "unsupported", "schema_version"),
        (("payload", "contract_version"), "unsupported", "contract_version"),
        (("payload", "captured_at"), "2026-08-11T16:02:00+08:00", "captured_at"),
        (("integrity", "algorithm"), "md5", "integrity contract"),
        (("integrity", "integrity_digest"), "0" * 64, "payload digest"),
    ):
        mutated = deepcopy(artifact)
        _set_nested_source_value(mutated, path, value)
        if path[0] == "payload":
            payload = cast(dict[str, object], mutated["payload"])
            cast(dict[str, object], mutated["integrity"])["integrity_digest"] = (
                probability_source_payload_digest(payload)
            )
        with pytest.raises(ProbabilitySourceError, match=message):
            verify_probability_source_snapshot(mutated)
    with pytest.raises(ProbabilitySourceError, match="run_id 与 payload 冲突"):
        probability_source_snapshot_filename(71, artifact)

    normalized_run = probability_source_module._normalize_run(
        run,
        captured_at=CAPTURED_AT,
        exact_keys=True,
    )
    dimensions = deepcopy(cast(dict[str, object], record["dimensions"]))
    for name, value, message in (
        ("mode", "intraday", "run cohort"),
        ("market", "US", "dimensions.market"),
        ("liquidity", "huge", "dimensions.liquidity"),
        ("regime", "invalid", "dimensions.regime"),
        ("segment", "invalid", "dimensions.segment"),
    ):
        mutated_dimensions = {**dimensions, name: value}
        with pytest.raises(ProbabilitySourceError, match=message):
            probability_source_module._dimensions(mutated_dimensions, normalized_run)

    stored = cast(
        list[dict[str, object]],
        cast(dict[str, object], artifact["payload"])["records"],
    )[0]
    stored_features = cast(dict[str, float], stored["features"])
    stored_dimensions = cast(dict[str, object], stored["dimensions"])
    instrument = cast(dict[str, object], stored["instrument"])
    for name, delta, message in (
        ("market_sh", 1.0, "categorical features"),
        ("change_pct", 1.0, "change_pct"),
        ("log_amount", 1.0, "log_amount"),
    ):
        mutated_features = {**stored_features, name: stored_features[name] + delta}
        with pytest.raises(ProbabilitySourceError, match=message):
            probability_source_module._validate_feature_context(
                mutated_features,
                stored_dimensions,
                instrument,
                "600519.SH",
            )

    for name, value, message in (
        ("metadata_effective_date", "2026-08-10", "PIT date"),
        ("quote_timestamp", "2026-08-10T15:00:00+08:00", "quote_timestamp"),
    ):
        mutated_instrument = {**instrument, name: value}
        with pytest.raises(ProbabilitySourceError, match=message):
            probability_source_module._instrument(
                mutated_instrument,
                "600519.SH",
                stored_dimensions,
                normalized_run,
            )
    with pytest.raises(ProbabilitySourceError, match="feature_schema"):
        probability_source_module._validate_feature_schema({})
    with pytest.raises(ProbabilitySourceError, match="cohort"):
        probability_source_module._validate_cohort(
            {"mode": "intraday", "scope": FULL_MARKET_SCOPE, "rule_version": run["rule_version"]},
            normalized_run,
        )
    with pytest.raises(ProbabilitySourceError, match="score semantics"):
        probability_source_module._validate_score_semantics(
            {
                **probability_source_module._score_semantics(normalized_run),
                "probability_ranking_effect": "enabled",
            },
            run=normalized_run,
        )


def test_current_source_rejects_quote_after_run_as_of() -> None:
    item = _result_item("600519.SH", "SH", "SH_MAIN")
    quote_timestamp = "2026-08-11T16:00:01+08:00"
    item["quote_timestamp"] = quote_timestamp
    score_details = cast(dict[str, object], item["score_details"])
    components = cast(dict[str, object], score_details["components"])
    dimensions = cast(dict[str, object], components["score_dimensions"])
    evidence = cast(dict[str, object], dimensions["point_in_time_evidence"])
    evidence_payload = cast(dict[str, object], evidence["payload"])
    evidence_payload["quote_timestamp"] = quote_timestamp
    evidence["payload_digest"] = stable_probability_hash(evidence_payload)

    with pytest.raises(ProbabilitySourceError, match="point-in-time evidence 未绑定"):
        _build_current_source(
            project_probability_source_capture(
                _run(success_count=1),
                [item],
                canonical_published=True,
            )
        )


def test_current_source_rejects_unregistered_production_score_spec() -> None:
    item = _result_item("600519.SH", "SH", "SH_MAIN")
    details = cast(dict[str, object], item["score_details"])
    score_spec = deepcopy(cast(dict[str, object], details["score_spec"]))
    eligibility = cast(dict[str, object], score_spec["eligibility"])
    eligibility["minimum_data_quality_score"] = 101
    details["score_spec"] = score_spec
    details["score_spec_hash"] = stable_score_spec_hash(score_spec)

    with pytest.raises(ProbabilitySourceError, match="未注册"):
        project_probability_source_capture(
            _run(success_count=1),
            [item],
            canonical_published=True,
        )


@pytest.mark.parametrize("mutation", ("outer_raw", "outer_trend", "inner_final_raw"))
def test_current_source_rejects_score_replay_divergence(mutation: str) -> None:
    item = _result_item("600519.SH", "SH", "SH_MAIN")
    if mutation == "outer_raw":
        item["raw_score"] = cast(float, item["raw_score"]) + 0.01
    elif mutation == "outer_trend":
        item["trend_score"] = cast(int, item["trend_score"]) + 1
    else:
        details = cast(dict[str, object], item["score_details"])
        components = cast(dict[str, object], details["components"])
        final = cast(dict[str, object], components["final_score"])
        final["raw"] = cast(float, final["raw"]) + 0.01

    with pytest.raises(ProbabilitySourceError, match="重放"):
        project_probability_source_capture(
            _run(success_count=1),
            [item],
            canonical_published=True,
        )


def test_current_source_accepts_fully_replay_bound_score_row() -> None:
    projection = project_probability_source_capture(
        _run(success_count=1),
        [_result_item("600519.SH", "SH", "SH_MAIN")],
        canonical_published=True,
    )

    artifact = _build_current_source(projection)
    assert verify_probability_source_snapshot(artifact) == artifact


def test_current_source_rejects_single_stock_as_formal_full_market(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _restore_production_coverage_contract(monkeypatch)

    with pytest.raises(ProbabilitySourceError, match="population 低于正式下限"):
        _source_projection(("600519.SH", "SH", "SH_MAIN"))


def test_current_source_rejects_missing_or_low_coverage_market(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _restore_production_coverage_contract(monkeypatch)
    complete = _formal_market_progress()
    missing_bj = deepcopy(complete)
    missing_bj[2].update(
        total_count=0,
        processed_count=0,
        success_count=0,
    )
    with pytest.raises(ProbabilitySourceError, match="BJ population"):
        probability_source_module._project_full_market_coverage(  # noqa: SLF001
            missing_bj,
            total_count=4300,
            success_count=4300,
        )

    low_sh = deepcopy(complete)
    low_sh[0].update(success_count=1709, missing_count=91)
    with pytest.raises(ProbabilitySourceError, match="SH coverage"):
        probability_source_module._project_full_market_coverage(  # noqa: SLF001
            low_sh,
            total_count=4500,
            success_count=4409,
        )


def test_formal_full_market_coverage_contract_accepts_complete_population(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _restore_production_coverage_contract(monkeypatch)
    progress = _formal_market_progress()

    coverage = probability_source_module._project_full_market_coverage(  # noqa: SLF001
        progress,
        total_count=4500,
        success_count=4500,
    )

    scopes = cast(dict[str, dict[str, object]], coverage["scopes"])
    assert scopes["ALL"]["population_count"] == 4500
    assert set(scopes) == {"ALL", "SH", "SZ", "BJ"}


def test_current_full_market_coverage_counts_skipped_in_population_not_eligible() -> None:
    progress = [
        {
            "market": "SH",
            "total_count": 2312,
            "processed_count": 2312,
            "success_count": 2298,
            "missing_count": 0,
            "skipped_count": 14,
        },
        {
            "market": "SZ",
            "total_count": 2896,
            "processed_count": 2896,
            "success_count": 2879,
            "missing_count": 0,
            "skipped_count": 17,
        },
        {
            "market": "BJ",
            "total_count": 335,
            "processed_count": 335,
            "success_count": 313,
            "missing_count": 0,
            "skipped_count": 22,
        },
    ]

    coverage = probability_source_module._project_full_market_coverage(  # noqa: SLF001
        progress,
        total_count=5543,
        success_count=5490,
    )

    scopes = cast(dict[str, dict[str, object]], coverage["scopes"])
    assert scopes["ALL"]["population_count"] == 5543
    assert scopes["ALL"]["eligible_count"] == 5490
    assert scopes["ALL"]["success_count"] == 5490
    assert scopes["ALL"]["eligible_ratio"] == pytest.approx(5490 / 5543)


def test_v3_full_market_coverage_exactly_binds_new_official_skip_policy(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _restore_production_coverage_contract(monkeypatch)
    progress = [
        {
            "market": "SH", "total_count": 2312, "processed_count": 2312,
            "success_count": 2265, "missing_count": 7, "skipped_count": 40,
        },
        {
            "market": "SZ", "total_count": 2896, "processed_count": 2896,
            "success_count": 2806, "missing_count": 8, "skipped_count": 82,
        },
        {
            "market": "BJ", "total_count": 335, "processed_count": 335,
            "success_count": 310, "missing_count": 0, "skipped_count": 25,
        },
    ]

    coverage = probability_source_module._project_full_market_coverage(  # noqa: SLF001
        progress,
        total_count=5543,
        success_count=5381,
        skipped_count=147,
    )

    scopes = cast(dict[str, dict[str, object]], coverage["scopes"])
    assert scopes["ALL"] == {
        "population_count": 5543,
        "eligible_count": 5396,
        "success_count": 5381,
        "missing_count": 15,
        "skipped_count": 147,
        "eligible_ratio": 5396 / 5543,
        "success_coverage": 5381 / 5396,
        "minimum_population": 4000,
        "minimum_eligible_ratio": 0.9,
        "minimum_success_coverage": 0.95,
    }
    assert [scopes[market]["missing_count"] for market in ("SH", "SZ", "BJ")] == [7, 8, 0]
    assert [scopes[market]["skipped_count"] for market in ("SH", "SZ", "BJ")] == [40, 82, 25]

    with pytest.raises(ProbabilitySourceError, match="run.skipped_count"):
        probability_source_module.validate_current_full_market_coverage(
            coverage,
            total_count=5543,
            success_count=5381,
            skipped_count=146,
        )


def test_score_contract_registry_separates_readable_v4_from_writable_v5() -> None:
    v4_hash = "30c5abb10b676fc71b5fa6c621cce809a6c2d054113fa578d77eccf28fb5955a"
    assert stable_score_spec_hash(
        scoring_module.market_scan_score_spec_v4(min_data_quality_score=50)
    ) == v4_hash
    assert probability_source_module.is_registered_production_score_contract(
        scoring_module.FULL_MARKET_LEGACY_V4_SCORE_RULE_VERSION,
        v4_hash,
    )
    assert not probability_source_module.is_current_writable_production_score_contract(
        scoring_module.FULL_MARKET_LEGACY_V4_SCORE_RULE_VERSION,
        v4_hash,
    )
    assert probability_source_module.is_registered_production_score_contract(
        FULL_MARKET_SCORE_RULE_VERSION,
        PRODUCTION_SCORE_SPEC_HASH,
    )
    assert probability_source_module.is_current_writable_production_score_contract(
        FULL_MARKET_SCORE_RULE_VERSION,
        PRODUCTION_SCORE_SPEC_HASH,
    )

    run = _run(success_count=1)
    run["production_score_rule_version"] = (
        scoring_module.FULL_MARKET_LEGACY_V4_SCORE_RULE_VERSION
    )
    run["production_score_spec_hash"] = v4_hash
    with pytest.raises(ProbabilitySourceError, match="production score rule/spec 未注册"):
        probability_source_module._normalize_run(  # noqa: SLF001
            run,
            captured_at=CAPTURED_AT,
            exact_keys=False,
        )


def test_v5_capture_maps_continuous_trend_without_silent_defaults() -> None:
    item = _result_item("600519.SH", "SH", "SH_MAIN")
    projection = project_probability_source_capture(
        _run(success_count=1),
        [item],
        canonical_published=True,
    )
    features = cast(
        dict[str, float],
        cast(list[dict[str, object]], projection["records"])[0]["features"],
    )
    continuous = cast(
        dict[str, object],
        cast(dict[str, object], cast(dict[str, object], item["score_details"])["components"])["continuous_trend"],
    )
    assert features["rank_refinement"] == continuous["score"]
    assert features["final_rank_discount"] == 0.0

    missing = deepcopy(item)
    cast(dict[str, object], cast(dict[str, object], missing["score_details"])["components"]).pop("continuous_trend")
    with pytest.raises(ProbabilitySourceError, match="重放"):
        project_probability_source_capture(
            _run(success_count=1),
            [missing],
            canonical_published=True,
        )


def test_current_full_market_coverage_tampering_fails_closed_with_skipped() -> None:
    projection = _source_projection(
        ("600519.SH", "SH", "SH_MAIN"),
        ("300750.SZ", "SZ", "CHINEXT"),
    )
    artifact = _build_current_source(projection)
    mutated = deepcopy(artifact)
    payload = cast(dict[str, object], mutated["payload"])
    quality = cast(dict[str, object], payload["quality"])
    coverage = cast(dict[str, object], quality["full_market_coverage"])
    scopes = cast(dict[str, dict[str, object]], coverage["scopes"])
    scopes["ALL"]["eligible_count"] = cast(int, scopes["ALL"]["eligible_count"]) + 1
    cast(dict[str, object], mutated["integrity"])["integrity_digest"] = (
        probability_source_payload_digest(payload)
    )

    with pytest.raises(ProbabilitySourceError, match="ALL .*无法重放"):
        verify_probability_source_snapshot(mutated)


def _restore_production_coverage_contract(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(
        probability_source_module,
        "PROBABILITY_SOURCE_MINIMUM_POPULATION",
        PRODUCTION_MINIMUM_POPULATION,
    )
    monkeypatch.setattr(
        probability_source_module,
        "PROBABILITY_SOURCE_MINIMUM_COVERAGE",
        PRODUCTION_MINIMUM_RATIO,
    )
    monkeypatch.setattr(
        probability_source_module,
        "PROBABILITY_SOURCE_MINIMUM_ELIGIBLE_RATIO",
        PRODUCTION_MINIMUM_ELIGIBLE,
    )


def _formal_market_progress() -> list[dict[str, object]]:
    return [
        {
            "market": market,
            "total_count": count,
            "processed_count": count,
            "success_count": count,
            "missing_count": 0,
            "skipped_count": 0,
        }
        for market, count in (("SH", 1800), ("SZ", 2500), ("BJ", 200))
    ]


def _set_nested_source_value(
    value: dict[str, object],
    path: tuple[str, ...],
    replacement: object,
) -> None:
    current = value
    for key in path[:-1]:
        current = cast(dict[str, object], current[key])
    current[path[-1]] = replacement


def _run(
    *,
    success_count: int,
    market_success_counts: Mapping[str, int] | None = None,
    quote_date: str = QUOTE_DATE,
    as_of: str | None = None,
) -> dict[str, object]:
    success_by_market = dict(market_success_counts or {"SH": success_count})
    missing_count = 1
    progress = []
    for market in ("SH", "SZ", "BJ"):
        successes = success_by_market.get(market, 0)
        missing = missing_count if market == "SH" else 0
        skipped = 1 if successes == 0 and missing == 0 else 0
        total = successes + missing + skipped
        progress.append(
            {
                "market": market,
                "total_count": total,
                "processed_count": total,
                "success_count": successes,
                "missing_count": missing,
                "skipped_count": skipped,
            }
        )
    run = {
        "run_id": 70,
        "status": "degraded",
        "mode": "official",
        "scope": FULL_MARKET_SCOPE,
        "rule_version": "full-market-scan-v6:test-contract",
        "quote_date": quote_date,
        "data_date": quote_date,
        "as_of": as_of or f"{quote_date}T16:00:00+08:00",
        "total_count": sum(cast(int, item["total_count"]) for item in progress),
        "success_count": success_count,
        "skipped_count": sum(cast(int, item["skipped_count"]) for item in progress),
        "canonical_published": True,
        "production_score_rule_version": FULL_MARKET_SCORE_RULE_VERSION,
        "production_score_spec_hash": PRODUCTION_SCORE_SPEC_HASH,
        "market_progress": progress,
    }
    run["full_market_coverage"] = probability_source_module._project_full_market_coverage(  # noqa: SLF001
        progress,
        total_count=run["total_count"],
        success_count=run["success_count"],
        skipped_count=run["skipped_count"],
    )
    run.pop("market_progress")
    return run


def _capture_record(symbol: str, market: str, board: str) -> dict[str, object]:
    is_new = False
    amount = 200_000_000.0
    dimensions = {
        "mode": "official",
        "scope": FULL_MARKET_SCOPE,
        "rule_version": "full-market-scan-v6:test-contract",
        "market": market,
        "board": board,
        "industry": "白酒" if market == "SH" else "新能源",
        "liquidity": "medium",
        "regime": "neutral",
        "segment": "regular",
    }
    values = {
        "raw_score": 91.25,
        "trend_score": 92.0,
        "change_pct": 1.25,
        "data_quality_score": 100.0,
        "amount": amount,
        "turnover_rate": 0.8,
        "volume_ratio": 1.2,
        "is_st": 0.0,
        "is_new": float(is_new),
    }
    features = probability_feature_vector(
        values,
        market=market,
        board=board,
        liquidity="medium",
        regime="neutral",
        industry=cast(str, dimensions["industry"]),
        segment="regular",
    )
    return {
        "symbol": symbol,
        "features": features,
        "dimensions": dimensions,
        "source_evidence": _evidence(symbol, market, amount=amount, is_new=is_new),
    }


def _result_item(
    symbol: str,
    market: str,
    board: str,
    *,
    quote_date: str = QUOTE_DATE,
    run_as_of: str | None = None,
    quote_timestamp: str | None = None,
    quote_observed_at: str | None = None,
) -> dict[str, object]:
    resolved_as_of = run_as_of or f"{quote_date}T16:00:00+08:00"
    resolved_quote_timestamp = quote_timestamp or f"{quote_date}T15:00:00+08:00"
    evidence = _evidence(
        symbol,
        market,
        amount=200_000_000.0,
        is_new=False,
        quote_date=quote_date,
        quote_timestamp=resolved_quote_timestamp,
    )
    calculated = _calculated_score(
        symbol,
        market,
        quote_date=quote_date,
        as_of=resolved_as_of,
        quote_timestamp=resolved_quote_timestamp,
    )
    evidence_payload = cast(dict[str, object], evidence["payload"])
    evidence_payload["reported_volume_ratio"] = calculated.volume_ratio
    evidence_payload["data_quality_score"] = calculated.quality.score
    _features, raw_features = score_dimensions_module._replay_evidence_features(  # noqa: SLF001
        evidence_payload
    )
    evidence_payload["features"] = raw_features
    scores = score_dimensions_module._replay_evidence_scores(evidence_payload)  # noqa: SLF001
    assert scores is not None
    evidence_payload["derived_scores"] = scores
    evidence["payload_digest"] = stable_probability_hash(evidence_payload)
    dimensions = SimpleNamespace(
        details=lambda: {
            "scores": scores,
            "raw_features": evidence["payload"]["features"],
            "point_in_time_evidence": evidence,
        }
    )
    score_details = scoring_module._score_details(  # noqa: SLF001
        item=SimpleNamespace(symbol=symbol),
        inputs=calculated.leader_inputs,
        leader_breakdown=calculated.leader_breakdown,
        rank_refinement=calculated.rank_refinement,
        quality_score=calculated.quality.score,
        quality_penalty=calculated.quality_penalty,
        continuous_trend_adjustment=calculated.continuous_trend_adjustment,
        base_score=calculated.base_score,
        score=calculated.score,
        raw_score=calculated.raw_score,
        rounded_score=calculated.rounded_score,
        dimensions=dimensions,
        score_spec=calculated.score_spec,
        rule_version="full-market-scan-v6:test-contract",
    )
    return {
        "run_id": 70,
        "symbol": symbol,
        "code": symbol.split(".", 1)[0],
        "market": market,
        "name": "测试股票",
        "industry": "白酒" if market == "SH" else "新能源",
        "list_date": "2001-08-27",
        "metadata_source": "test-master",
        "is_st": False,
        "is_new": False,
        "status": "success",
        "score": calculated.score,
        "raw_score": calculated.raw_score,
        "trend_score": calculated.trend,
        "leader_score": calculated.leadership,
        "data_quality_score": calculated.quality.score,
        "price": 1510.0,
        "change_pct": 1.25,
        "turnover_rate": 0.8,
        "volume_ratio": calculated.volume_ratio,
        "amount": 200_000_000.0,
        "data_date": quote_date,
        "quote_timestamp": resolved_quote_timestamp,
        "quote_observed_at": quote_observed_at or f"{quote_date}T15:00:01+08:00",
        "quote_source": "test-quote",
        "kline_source": "test-kline",
        "adjustment_mode": "qfq",
        "updated_at": resolved_as_of,
        "score_details": score_details,
    }


def _calculated_score(
    symbol: str,
    market: str,
    *,
    quote_date: str = QUOTE_DATE,
    as_of: str | None = None,
    quote_timestamp: str | None = None,
) -> object:
    resolved_as_of = as_of or f"{quote_date}T16:00:00+08:00"
    resolved_quote_timestamp = quote_timestamp or f"{quote_date}T15:00:00+08:00"
    quote = Quote(
        code=symbol.split(".", 1)[0],
        name="测试股票",
        market=market,
        price=1510.0,
        prev_close=1491.36,
        open=1495.0,
        high=1520.0,
        low=1490.0,
        volume=1_000_000.0,
        amount=200_000_000.0,
        change=18.64,
        change_pct=1.25,
        turnover_rate=0.8,
        timestamp=resolved_quote_timestamp,
        source="test-quote",
    )
    rows = [
        Kline(
            date=row[0],
            open=row[1],
            close=row[2],
            high=row[3],
            low=row[4],
            volume=row[5],
            adjustment_mode=row[6],
            data_version=row[7],
            contract_version=row[8],
            as_of=row[9],
            source="test-kline",
        )
        for row in cast(
            list[list[object]],
            _evidence(
                symbol,
                market,
                amount=200_000_000.0,
                is_new=False,
                quote_date=quote_date,
                quote_timestamp=resolved_quote_timestamp,
            )["payload"]["bar_contract_61"],
        )
    ]
    return scoring_module._calculate_market_scan_score(  # noqa: SLF001
        quote,
        rows,
        as_of=datetime.fromisoformat(resolved_as_of),
        min_data_quality_score=50,
        mode="official",
    )


def _source_projection(
    *identities: tuple[str, str, str],
) -> probability_source_module.VerifiedProbabilitySourceProjection:
    items = [_result_item(*identity) for identity in identities]
    success_by_market = Counter(identity[1] for identity in identities)
    return project_probability_source_capture(
        _run(
            success_count=len(items),
            market_success_counts=success_by_market,
        ),
        items,
        canonical_published=True,
    )


def _build_current_source(
    projection: probability_source_module.VerifiedProbabilitySourceProjection,
    *,
    captured_at: str = CAPTURED_AT,
) -> dict[str, object]:
    return build_probability_source_snapshot(
        run=cast(dict[str, object], projection["run"]),
        records=cast(list[dict[str, object]], projection["records"]),
        captured_at=captured_at,
        projection_receipt=projection,
    )


def _capture_current_source(
    directory: Path,
    projection: probability_source_module.VerifiedProbabilitySourceProjection,
    *,
    captured_at: str = CAPTURED_AT,
) -> dict[str, object]:
    return capture_source_snapshot(
        directory,
        run=cast(dict[str, object], projection["run"]),
        records=cast(list[dict[str, object]], projection["records"]),
        captured_at=captured_at,
        projection_receipt=projection,
    )


def _evidence(
    symbol: str,
    market: str,
    *,
    amount: float,
    is_new: bool,
    quote_date: str = QUOTE_DATE,
    quote_timestamp: str | None = None,
) -> dict[str, object]:
    resolved_quote_timestamp = quote_timestamp or f"{quote_date}T15:00:00+08:00"
    sessions = trading_dates_between(date(2026, 1, 1), date.fromisoformat(quote_date))[-61:]
    klines = [
        Kline(
            date=session.isoformat(), open=10.0, close=10.1, high=10.2, low=9.9,
            volume=1000.0, source="test-kline", adjustment_mode="qfq",
            as_of=quote_date, data_version="test-v1", contract_version="daily-kline.v1",
        )
        for session in sessions
    ]
    rows = [
        [
            row.date, row.open, row.close, row.high, row.low, row.volume,
            row.adjustment_mode, row.data_version, row.contract_version, row.as_of,
        ]
        for row in klines
    ]
    spec = market_scan_dimension_spec()
    payload: dict[str, object] = {
        "symbol": symbol,
        "code": symbol.split(".", 1)[0],
        "market": market,
        "name": "测试股票",
        "industry": "白酒" if market == "SH" else "新能源",
        "metadata_source": "test-master",
        "quote_date": quote_date,
        "data_date": quote_date,
        "quote_timestamp": resolved_quote_timestamp,
        "quote_source": "test-quote",
        "kline_source": "test-kline",
        "adjustment_mode": "qfq",
        "quote_price": 1510.0,
        "quote_change_pct": 1.25,
        "quote_turnover_rate": 0.8,
        "quote_amount": amount,
        "reported_volume_ratio": 1.2,
        "data_quality_score": 100,
        "mode": "official",
        "volume_context": {
            "mode": "official",
            "volume_data_date": quote_date,
            "lifecycle_applied": True,
            "price_volume_alignment": "same-completed-session",
        },
        "is_st": False,
        "is_new": is_new,
        "list_date": "2001-08-27",
        "quote_fallback_used": False,
        "kline_fallback_used": False,
        "metadata_degraded": False,
        "features": {},
        "bar_contract_61": rows,
        "dimension_spec": spec,
        "dimension_spec_hash": stable_probability_hash(spec),
        "derived_scores": {},
        "session_coverage": build_market_scan_session_coverage(klines).as_dict(),
    }
    _features, raw_features = score_dimensions_module._replay_evidence_features(payload)  # noqa: SLF001
    payload["features"] = raw_features
    scores = score_dimensions_module._replay_evidence_scores(payload)  # noqa: SLF001
    assert scores is not None
    payload["derived_scores"] = scores
    return {
        "schema_version": MARKET_SCAN_EVIDENCE_SCHEMA_VERSION,
        "contract_version": MARKET_SCAN_EVIDENCE_CONTRACT_VERSION,
        "status": "verified-persisted-at-scan-time",
        "action_eligible": True,
        "eligible_for_promotion_evidence": True,
        "payload": payload,
        "payload_digest": stable_probability_hash(payload),
    }
