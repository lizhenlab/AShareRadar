from __future__ import annotations

from copy import deepcopy
import gzip
import json
from pathlib import Path
from typing import cast

import pytest

import app.services.market_scan_probability_source as probability_source_module
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
from app.services.market_scan_score_dimensions import (
    MARKET_SCAN_EVIDENCE_CONTRACT_VERSION,
    MARKET_SCAN_EVIDENCE_SCHEMA_VERSION,
)
from app.services.market_scan_universe import FULL_MARKET_SCOPE


CAPTURED_AT = "2026-08-11T16:01:00+08:00"
QUOTE_DATE = "2026-08-11"


def test_source_capture_is_content_addressed_atomic_compact_and_restart_loadable(tmp_path: Path) -> None:
    run = _run(success_count=2)
    records = [_capture_record("600519.SH", "SH", "SH_MAIN"), _capture_record("300750.SZ", "SZ", "CHINEXT")]
    first_dir, second_dir = tmp_path / "first", tmp_path / "second"

    first = capture_source_snapshot(first_dir, run=run, records=records, captured_at=CAPTURED_AT)
    repeated = capture_source_snapshot(first_dir, run=run, records=records, captured_at=CAPTURED_AT)
    second = capture_source_snapshot(second_dir, run=run, records=list(reversed(records)), captured_at=CAPTURED_AT)

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
    run = _run(success_count=1)
    records = [_capture_record("600519.SH", "SH", "SH_MAIN")]
    capture_source_snapshot(
        directory,
        run=run,
        records=records,
        captured_at="2026-08-11T17:00:00+08:00",
    )
    newer = capture_source_snapshot(
        directory,
        run=run,
        records=records,
        captured_at="2026-08-11T10:00:00+00:00",
    )

    loaded = load_probability_source_snapshot_for_run(directory, 70)

    assert loaded is not None
    assert loaded["captured_at"] == "2026-08-11T10:00:00+00:00"
    assert cast(dict[str, object], loaded["integrity"])["integrity_digest"] == newer["digest"]


def test_source_load_and_list_reject_symlink_paths(tmp_path: Path) -> None:
    real_directory = tmp_path / "real"
    info = capture_source_snapshot(
        real_directory,
        run=_run(success_count=1),
        records=[_capture_record("600519.SH", "SH", "SH_MAIN")],
        captured_at=CAPTURED_AT,
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

    with pytest.raises(ProbabilitySourceError, match="输出目录必须是真实目录"):
        capture_source_snapshot(
            root_link,
            run=_run(success_count=1),
            records=[_capture_record("600519.SH", "SH", "SH_MAIN")],
            captured_at=CAPTURED_AT,
        )

    assert list(real_directory.iterdir()) == []

    nested_root = root_link / "not-created"
    with pytest.raises(ProbabilitySourceError, match="输出目录必须是真实目录"):
        capture_source_snapshot(
            nested_root,
            run=_run(success_count=1),
            records=[_capture_record("600519.SH", "SH", "SH_MAIN")],
            captured_at=CAPTURED_AT,
        )
    assert not (real_directory / "not-created").exists()


def test_source_load_rejects_oversize_archive(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    info = capture_source_snapshot(
        tmp_path,
        run=_run(success_count=1),
        records=[_capture_record("600519.SH", "SH", "SH_MAIN")],
        captured_at=CAPTURED_AT,
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
    base = _capture_record("600519.SH", "SH", "SH_MAIN")
    evidence = base["source_evidence"]
    item = {
        "run_id": 70,
        "symbol": "600519.SH",
        "market": "SH",
        "industry": "白酒",
        "list_date": "2001-08-27",
        "is_st": False,
        "is_new": False,
        "status": "success",
        "score": 91,
        "raw_score": 91.25,
        "trend_score": 92,
        "data_quality_score": 100,
        "price": 1510.0,
        "change_pct": 1.25,
        "turnover_rate": 0.8,
        "volume_ratio": 1.2,
        "amount": 200_000_000.0,
        "score_details": {
            "components": {
                "leader_score": {"base": 91.0, "trend_delta": 0.25, "unclamped": 91.25, "score": 91.0},
                "final_score": {
                    "quality_penalty": 0.0,
                    "base": 91.0,
                    "rank_discount": 0.0,
                    "raw": 91.25,
                    "rounded": 91.0,
                    "score": 91.0,
                },
                "rank_refinement": {"score": 0.7, "normalized_inputs": {}},
                "score_dimensions": {
                    "scores": {"alpha_1d": 60, "alpha_5d": 65, "alpha_20d": 70, "confidence": 90, "risk": 30, "tradability": 80},
                    "raw_features": {},
                    "point_in_time_evidence": evidence,
                },
            }
        },
    }

    projection = project_probability_source_capture(run, [item], canonical_published=True)

    projected_run = cast(dict[str, object], projection["run"])
    assert projected_run["as_of"] == "2026-08-11T16:00:00+08:00"
    artifact = build_probability_source_snapshot(
        run=projected_run,
        records=cast(list[dict[str, object]], projection["records"]),
        captured_at="2026-08-11T16:02:00+08:00",
    )
    info = capture_source_snapshot(
        tmp_path,
        run=projected_run,
        records=cast(list[dict[str, object]], projection["records"]),
        captured_at="2026-08-11T16:02:00+08:00",
    )
    assert load_probability_source_snapshot(cast(str, info["path"])) == artifact

    with pytest.raises(ProbabilitySourceError, match="canonical_published=True"):
        project_probability_source_capture(run, [item], canonical_published=False)
    with pytest.raises(ProbabilitySourceError, match="timestamp 必须包含时区"):
        build_probability_source_snapshot(run={**projected_run, "as_of": "2026-08-11 16:00:00"}, records=[], captured_at=CAPTURED_AT)


def test_source_capture_fails_closed_on_scope_completeness_features_and_evidence() -> None:
    record = _capture_record("600519.SH", "SH", "SH_MAIN")
    with pytest.raises(ProbabilitySourceError, match="official 全市场"):
        build_probability_source_snapshot(
            run={**_run(success_count=1), "mode": "intraday"}, records=[record], captured_at=CAPTURED_AT,
        )
    with pytest.raises(ProbabilitySourceError, match="记录不完整"):
        build_probability_source_snapshot(run=_run(success_count=2), records=[record], captured_at=CAPTURED_AT)
    with pytest.raises(ProbabilitySourceError, match="重复 symbol"):
        build_probability_source_snapshot(run=_run(success_count=2), records=[record, record], captured_at=CAPTURED_AT)

    missing_feature = deepcopy(record)
    cast(dict[str, float], missing_feature["features"]).pop("trend_score")
    with pytest.raises(ProbabilitySourceError, match="注册 feature schema"):
        build_probability_source_snapshot(run=_run(success_count=1), records=[missing_feature], captured_at=CAPTURED_AT)

    corrupt_evidence = deepcopy(record)
    evidence = cast(dict[str, object], corrupt_evidence["source_evidence"])
    cast(dict[str, object], evidence["payload"])["quote_price"] = 1.0
    with pytest.raises(ProbabilitySourceError, match="evidence 未通过验证"):
        build_probability_source_snapshot(run=_run(success_count=1), records=[corrupt_evidence], captured_at=CAPTURED_AT)


def test_source_archive_rejects_tamper_duplicate_keys_wrong_filename_and_resealed_semantics(tmp_path: Path) -> None:
    run = _run(success_count=1)
    record = _capture_record("600519.SH", "SH", "SH_MAIN")
    artifact = build_probability_source_snapshot(run=run, records=[record], captured_at=CAPTURED_AT)
    info = capture_source_snapshot(tmp_path / "tamper", run=run, records=[record], captured_at=CAPTURED_AT)
    source = Path(cast(str, info["path"]))

    changed = json.loads(gzip.decompress(source.read_bytes()))
    changed["payload"]["records"][0]["features"]["trend_score"] += 1
    source.write_bytes(gzip.compress(json.dumps(changed, separators=(",", ":")).encode(), compresslevel=9, mtime=0))
    with pytest.raises(ProbabilitySourceError, match="feature_vector_digest"):
        load_probability_source_snapshot(source)

    duplicate_info = capture_source_snapshot(tmp_path / "duplicate", run=run, records=[record], captured_at=CAPTURED_AT)
    duplicate = Path(cast(str, duplicate_info["path"]))
    text = gzip.decompress(duplicate.read_bytes()).decode()
    text = text.replace('{"captured_at":', '{"captured_at":"duplicate","captured_at":', 1)
    duplicate.write_bytes(gzip.compress(text.encode(), compresslevel=9, mtime=0))
    with pytest.raises(ProbabilitySourceError, match="重复 JSON key"):
        load_probability_source_snapshot(duplicate)

    valid_info = capture_source_snapshot(tmp_path / "rename", run=run, records=[record], captured_at=CAPTURED_AT)
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
    artifact = build_probability_source_snapshot(
        run=_run(success_count=1), records=[_capture_record("600519.SH", "SH", "SH_MAIN")], captured_at=CAPTURED_AT,
    )
    assert probability_source_snapshot_filename(70, artifact).startswith("market-scan-probability-source-run-70-")
    corrupted = deepcopy(artifact)
    payload = cast(dict[str, object], corrupted["payload"])
    cast(dict[str, object], payload["quality"])["record_count"] = 2
    cast(dict[str, object], corrupted["integrity"])["integrity_digest"] = probability_source_payload_digest(payload)
    with pytest.raises(ProbabilitySourceError, match="quality 不能由 records 重放"):
        verify_probability_source_snapshot(corrupted)


def test_source_archive_mechanical_errors_are_translated_by_public_boundary(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    artifact = build_probability_source_snapshot(
        run=_run(success_count=1),
        records=[_capture_record("600519.SH", "SH", "SH_MAIN")],
        captured_at=CAPTURED_AT,
    )
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
    artifact = build_probability_source_snapshot(
        run=run,
        records=[record],
        captured_at=CAPTURED_AT,
    )
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

    stored = probability_source_module._capture_record(record, normalized_run)
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
            {**probability_source_module._score_semantics(), "probability_ranking_effect": "enabled"}
        )


def _set_nested_source_value(
    value: dict[str, object],
    path: tuple[str, ...],
    replacement: object,
) -> None:
    current = value
    for key in path[:-1]:
        current = cast(dict[str, object], current[key])
    current[path[-1]] = replacement


def _run(*, success_count: int) -> dict[str, object]:
    return {
        "run_id": 70,
        "status": "degraded",
        "mode": "official",
        "scope": FULL_MARKET_SCOPE,
        "rule_version": "full-market-scan-v6:test-contract",
        "quote_date": QUOTE_DATE,
        "data_date": QUOTE_DATE,
        "as_of": "2026-08-11T16:00:00+08:00",
        "total_count": success_count + 1,
        "success_count": success_count,
        "canonical_published": True,
    }


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


def _evidence(symbol: str, market: str, *, amount: float, is_new: bool) -> dict[str, object]:
    rows = [
        [f"2026-05-{index:02d}", 10.0, 10.1, 10.2, 9.9, 1000.0, "qfq", "test-v1", "daily-kline.v1"]
        for index in range(1, 29)
    ]
    rows.extend(
        [f"2026-06-{index:02d}", 10.0, 10.1, 10.2, 9.9, 1000.0, "qfq", "test-v1", "daily-kline.v1"]
        for index in range(1, 31)
    )
    rows.extend(
        [f"2026-07-{index:02d}", 10.0, 10.1, 10.2, 9.9, 1000.0, "qfq", "test-v1", "daily-kline.v1"]
        for index in range(1, 4)
    )
    payload: dict[str, object] = {
        "symbol": symbol,
        "market": market,
        "industry": "白酒" if market == "SH" else "新能源",
        "metadata_source": "test-master",
        "quote_date": QUOTE_DATE,
        "data_date": QUOTE_DATE,
        "quote_timestamp": "2026-08-11T15:00:00+08:00",
        "quote_price": 1510.0,
        "quote_change_pct": 1.25,
        "quote_turnover_rate": 0.8,
        "quote_amount": amount,
        "reported_volume_ratio": 1.2,
        "data_quality_score": 100,
        "mode": "official",
        "volume_context": {
            "mode": "official",
            "volume_data_date": QUOTE_DATE,
            "lifecycle_applied": True,
            "price_volume_alignment": "same-completed-session",
        },
        "is_st": False,
        "is_new": is_new,
        "list_date": "2001-08-27",
        "quote_fallback_used": False,
        "kline_fallback_used": False,
        "metadata_degraded": False,
        "features": {"return_1d_pct": 1.25},
        "bar_contract_61": rows,
    }
    return {
        "schema_version": MARKET_SCAN_EVIDENCE_SCHEMA_VERSION,
        "contract_version": MARKET_SCAN_EVIDENCE_CONTRACT_VERSION,
        "status": "verified-persisted-at-scan-time",
        "eligible_for_promotion_evidence": True,
        "payload": payload,
        "payload_digest": stable_probability_hash(payload),
    }
