from __future__ import annotations

from copy import deepcopy
import json
import os
from pathlib import Path
import subprocess
import sys
from typing import Any, cast

import pytest

from app.artifacts.io import ArtifactPublishConflictError
import app.services.market_scan_future_range as future_range_module
import app.services.market_scan_future_range_artifact as future_range_artifact_module
from app.services.market_scan_future_range import FutureRangeConfig, evaluate_market_scan_future_range
from app.services.market_scan_future_range_artifact import (
    FutureRangeArtifactError,
    build_future_range_artifact,
    canonical_future_range_artifact_json,
    future_range_artifact_filename,
    load_future_range_artifact,
    replay_future_range_artifact,
    verify_future_range_artifact,
    write_future_range_artifact,
)
from app.services.market_scan_universe import FULL_MARKET_SCOPE
from tests.test_market_scan_future_range import TARGET_DATES, _initialize, _seed_run


def test_future_range_artifact_is_atomic_immutable_and_restart_replayable(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    database, report = _report(tmp_path, monkeypatch)
    generated_at = str(report["generated_at"])
    artifact = build_future_range_artifact(report, generated_at=generated_at)
    run_id = cast(int, cast(dict[str, object], report["run"])["run_id"])
    target = tmp_path / future_range_artifact_filename(run_id, artifact)

    written = write_future_range_artifact(target, artifact, database_path=database)

    assert written == target.resolve()
    assert load_future_range_artifact(target) == artifact
    assert replay_future_range_artifact(load_future_range_artifact(target)) == report
    assert target.read_text(encoding="utf-8") == canonical_future_range_artifact_json(artifact)
    assert write_future_range_artifact(target, artifact, database_path=database) == target.resolve()

    changed_report = deepcopy(report)
    changed_report["generated_at"] = "2026-01-09T00:00:00Z"
    changed = build_future_range_artifact(changed_report, generated_at="2026-01-09T00:00:00Z")
    with pytest.raises(FutureRangeArtifactError, match="已存在且内容不同"):
        write_future_range_artifact(target, changed, database_path=database)
    with pytest.raises(FutureRangeArtifactError, match="不能覆盖 SQLite"):
        write_future_range_artifact(database, artifact, database_path=database)
    hard_link = tmp_path / "database-hard-link.json"
    os.link(database, hard_link)
    with pytest.raises(FutureRangeArtifactError, match="硬链接"):
        write_future_range_artifact(hard_link, artifact, database_path=database)


@pytest.mark.parametrize(
    ("mutation", "message"),
    [
        ("level_shift", "不能由持久化OHLC重放"),
        ("hlc3", "HLC3 proxy 计算不一致"),
        ("execution", "cost_drag 不能由 gross/net 重放"),
        ("execution_identity", "gross_return 不能由 entry/exit 重放"),
        ("path", "cumulative MFE/MAE/终值不能由固定OHLC路径重放"),
    ],
)
def test_future_range_artifact_rejects_semantic_tamper_even_when_resealed(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    mutation: str,
    message: str,
) -> None:
    _database, report = _report(tmp_path, monkeypatch)
    changed = deepcopy(report)
    record = cast(list[dict[str, Any]], changed["records"])[0]
    if mutation == "level_shift":
        record["offsets"][0]["level_shift"]["low"] += 0.1
    elif mutation == "hlc3":
        record["offsets"][0]["target_bar"]["hlc3_proxy"] += 0.1
    elif mutation == "execution":
        record["offsets"][1]["execution"]["cost_drag"] += 0.1
    elif mutation == "execution_identity":
        execution = record["offsets"][1]["execution"]
        execution["gross_return"] += 0.1
        execution["net_return"] += 0.1
        execution["net_excess_return"] += 0.1
    else:
        record["offsets"][2]["d1_open_reference"]["cumulative_path"]["mfe"] += 0.1

    with pytest.raises(FutureRangeArtifactError, match=message):
        build_future_range_artifact(changed, generated_at=str(changed["generated_at"]))


def test_future_range_artifact_rejects_digest_corruption_and_duplicate_json_keys(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _database, report = _report(tmp_path, monkeypatch)
    artifact = build_future_range_artifact(report, generated_at=str(report["generated_at"]))
    corrupted = deepcopy(artifact)
    corrupted["integrity"]["integrity_digest"] = "0" * 64  # type: ignore[index]
    with pytest.raises(FutureRangeArtifactError, match="digest 不一致"):
        verify_future_range_artifact(corrupted)

    source = tmp_path / "duplicate.json"
    encoded = canonical_future_range_artifact_json(artifact)
    source.write_text(encoded.replace('{"generated_at":', '{"generated_at":"duplicate","generated_at":', 1), encoding="utf-8")
    with pytest.raises(FutureRangeArtifactError, match="重复 JSON key"):
        load_future_range_artifact(source)


def test_future_range_artifact_rejects_malformed_wrapper_and_filename_contracts(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _database, report = _report(tmp_path, monkeypatch)
    artifact = build_future_range_artifact(report, generated_at=str(report["generated_at"]))

    for path, value, message in (
        (("schema_version",), "future-v0", "schema_version"),
        (("integrity", "algorithm"), "md5", "integrity contract"),
        (("integrity", "integrity_digest"), "bad", "小写 SHA-256"),
    ):
        changed = deepcopy(artifact)
        _set_nested(changed, path, value)
        with pytest.raises(FutureRangeArtifactError, match=message):
            verify_future_range_artifact(changed)

    extra = deepcopy(artifact)
    extra["extra"] = True
    with pytest.raises(FutureRangeArtifactError, match="字段不匹配"):
        verify_future_range_artifact(extra)
    with pytest.raises(FutureRangeArtifactError, match="run_id 必须是正整数"):
        future_range_artifact_filename(False, artifact)
    with pytest.raises(FutureRangeArtifactError, match="payload.run_id"):
        future_range_artifact_filename(999, artifact)

    non_object = tmp_path / "non-object.json"
    non_object.write_text("[]", encoding="utf-8")
    with pytest.raises(FutureRangeArtifactError, match="顶层必须是 JSON object"):
        load_future_range_artifact(non_object)
    nonfinite = tmp_path / "nonfinite.json"
    nonfinite.write_text('{"value":NaN}', encoding="utf-8")
    with pytest.raises(FutureRangeArtifactError, match="非法常量"):
        load_future_range_artifact(nonfinite)


def test_future_range_payload_validation_fails_closed_across_every_contract_layer(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _database, report = _report(tmp_path, monkeypatch)
    prediction = {"probability": 0.5, "source_artifact_digest": "a" * 64}
    cases: tuple[tuple[tuple[str | int, ...], object, str], ...] = (
        (("report_contract_version",), "range-v0", "report contract_version"),
        (("status",), "ready", "payload.status"),
        (("run", "mode"), "intraday", "仅接受 official"),
        (("run", "run_id"), False, "payload.run_id"),
        (("config", "session_offsets"), [1, 2], "session_offsets"),
        (("config", "center_proxy"), "VWAP", "不能伪称 VWAP"),
        (("source", "read_only"), False, "只读 qfq"),
        (("probability_context", "status"), "unknown", "probability_context.status"),
        (("groups",), {}, "payload.groups 必须是数组"),
        (("records", 0, "run_id"), 999, "record 身份"),
        (("records", 0, "source_evidence", "status"), "unknown", "已验证点时证据"),
        (("records", 0, "d_bar", "open"), 0.0, "价格/成交量无效"),
        (("records", 0, "d_bar", "high"), 1.0, "OHLC 关系无效"),
        (("records", 0, "d_bar", "adjustment_mode"), "hfq", "qfq/contract"),
        (("records", 0, "probability", "predictions"), {}, "predictions 必须是数组"),
        (("records", 0, "probability", "status"), "unknown", "probability.status"),
        (("records", 0, "offsets"), [], "严格覆盖"),
        (("records", 0, "offsets", 0, "fixed_session_status"), "unknown", "offset status"),
        (("records", 0, "offsets", 0, "target_session_date"), "2020-01-01", "日期或日线路径"),
        (("records", 0, "offsets", 0, "target_bar_digest"), "0" * 64, "target_bar_digest"),
        (("records", 0, "offsets", 0, "d1_open_reference", "entry_price"), 999.0, "open reference 无效"),
        (("records", 0, "offsets", 0, "d1_open_reference", "entry_date"), "2020-01-01", "entry_date"),
        (
            ("records", 0, "offsets", 0, "d1_open_reference", "cumulative_path", "daily_bar_path_unknown"),
            False,
            "日线内先后未知",
        ),
        (("records", 0, "offsets", 0, "execution", "status"), "unknown", "execution.status"),
        (("records", 0, "offsets", 0, "execution", "gross_return"), 0.1, "非 modelled"),
        (("records", 0, "offsets", 1, "execution", "net_excess_return"), 9.0, "net_excess"),
        (("records", 0, "offsets", 1, "execution", "entry_date"), "2020-01-01", "execution entry"),
        (("records", 0, "offsets", 1, "execution", "exit_date"), "2020-01-01", "execution exit"),
        (("groups", 0, "status"), "unknown", "cohort/status"),
    )
    for path, value, message in cases:
        changed = deepcopy(report)
        _set_nested(changed, path, value)
        with pytest.raises(FutureRangeArtifactError, match=message):
            build_future_range_artifact(changed, generated_at=str(changed["generated_at"]))

    generated_at_mismatch = deepcopy(report)
    with pytest.raises(FutureRangeArtifactError, match="generated_at"):
        build_future_range_artifact(generated_at_mismatch, generated_at="2020-01-01T00:00:00Z")

    duplicate = deepcopy(report)
    records = cast(list[dict[str, object]], duplicate["records"])
    records.append(deepcopy(records[0]))
    with pytest.raises(FutureRangeArtifactError, match="symbol 重复"):
        build_future_range_artifact(duplicate, generated_at=str(duplicate["generated_at"]))

    unavailable_with_outcome = deepcopy(report)
    _set_nested(unavailable_with_outcome, ("records", 0, "offsets", 0, "fixed_session_status"), "not_mature")
    with pytest.raises(FutureRangeArtifactError, match="不能携带伪造 outcome"):
        build_future_range_artifact(
            unavailable_with_outcome,
            generated_at=str(unavailable_with_outcome["generated_at"]),
        )

    not_available_with_prediction = deepcopy(report)
    _set_nested(not_available_with_prediction, ("records", 0, "probability", "predictions"), [prediction])
    with pytest.raises(FutureRangeArtifactError, match="not_available 概率"):
        build_future_range_artifact(
            not_available_with_prediction,
            generated_at=str(not_available_with_prediction["generated_at"]),
        )

    calibrated_empty = deepcopy(report)
    _set_nested(calibrated_empty, ("records", 0, "probability", "status"), "calibrated_shadow")
    with pytest.raises(FutureRangeArtifactError, match="预测不能为空"):
        build_future_range_artifact(calibrated_empty, generated_at=str(calibrated_empty["generated_at"]))

    calibrated_invalid = deepcopy(calibrated_empty)
    _set_nested(calibrated_invalid, ("records", 0, "probability", "predictions"), [{**prediction, "probability": 2.0}])
    with pytest.raises(FutureRangeArtifactError, match="概率或来源无效"):
        build_future_range_artifact(calibrated_invalid, generated_at=str(calibrated_invalid["generated_at"]))


def test_future_range_loader_rejects_symlink_and_oversize_paths(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _database, report = _report(tmp_path, monkeypatch)
    artifact = build_future_range_artifact(report, generated_at=str(report["generated_at"]))
    regular = tmp_path / "regular.json"
    regular.write_text(canonical_future_range_artifact_json(artifact), encoding="utf-8")
    symlink = tmp_path / "artifact-link.json"
    symlink.symlink_to(regular)
    with pytest.raises(FutureRangeArtifactError, match="读取失败"):
        load_future_range_artifact(symlink)

    oversize = tmp_path / "oversize.json"
    oversize.write_bytes(b"12345")
    monkeypatch.setattr(future_range_artifact_module, "FUTURE_RANGE_ARTIFACT_MAX_BYTES", 4)
    with pytest.raises(FutureRangeArtifactError, match="读取失败"):
        load_future_range_artifact(oversize)


def test_future_range_low_level_invalid_values_and_publish_failures_are_domain_mapped(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    database, report = _report(tmp_path, monkeypatch)
    artifact = build_future_range_artifact(report, generated_at=str(report["generated_at"]))
    target = tmp_path / "artifact.json"

    for failure, message in (
        (ArtifactPublishConflictError(target), "并发发布冲突"),
        (FutureRangeArtifactError("callback rejected"), "callback rejected"),
        (OSError("disk full"), "写入失败"),
    ):
        monkeypatch.setattr(
            future_range_artifact_module,
            "exclusive_atomic_publish",
            lambda *_args, _failure=failure, **_kwargs: (_ for _ in ()).throw(_failure),
        )
        with pytest.raises(FutureRangeArtifactError, match=message):
            write_future_range_artifact(target, artifact, database_path=database)

    with pytest.raises(FutureRangeArtifactError, match="有限数值"):
        future_range_artifact_module._finite_number(True, "value")
    with pytest.raises(FutureRangeArtifactError, match="NaN/Infinity"):
        future_range_artifact_module._json_value(float("nan"), "value")
    with pytest.raises(FutureRangeArtifactError, match="无效或重复键"):
        future_range_artifact_module._json_value({1: "value"}, "value")
    with pytest.raises(FutureRangeArtifactError, match="非 JSON 类型"):
        future_range_artifact_module._json_value(object(), "value")
    with pytest.raises(FutureRangeArtifactError, match="必须是 object"):
        future_range_artifact_module._required_mapping([], "value")
    with pytest.raises(FutureRangeArtifactError, match="非空字符串"):
        future_range_artifact_module._required_text("", "value")

    other = tmp_path / "other.json"
    database.write_bytes(b"database")
    other.write_bytes(b"other")
    monkeypatch.setattr(Path, "samefile", lambda *_args, **_kwargs: (_ for _ in ()).throw(OSError("identity")))
    with pytest.raises(FutureRangeArtifactError, match="无法验证"):
        future_range_artifact_module._reject_database_target(other, database)


def test_future_range_d1_execution_rejects_wrong_t_plus_one_and_missing_entry_bar(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _database, report = _report(tmp_path, monkeypatch)
    wrong_reason = deepcopy(report)
    _set_nested(wrong_reason, ("records", 0, "offsets", 0, "execution", "reason"), "wrong")
    with pytest.raises(FutureRangeArtifactError, match=r"A股T\+1"):
        build_future_range_artifact(wrong_reason, generated_at=str(wrong_reason["generated_at"]))

    missing_entry = deepcopy(report)
    first = cast(dict[str, object], cast(list[dict[str, object]], missing_entry["records"])[0]["offsets"][0])  # type: ignore[index]
    first["fixed_session_status"] = "not_mature"
    for name in (
        "target_bar", "target_bar_digest", "level_shift", "d_close_reference",
        "d1_open_reference", "interval_structure", "daily_bar_path_unknown",
    ):
        first[name] = None
    first["execution"] = {
        "status": "data_unavailable",
        "reason": "A_share_T_plus_1_no_same_session_exit",
    }
    second = cast(dict[str, object], cast(list[dict[str, object]], missing_entry["records"])[0]["offsets"][1])  # type: ignore[index]
    second["d1_open_reference"] = {"status": "unavailable", "cumulative_path": None}
    with pytest.raises(FutureRangeArtifactError, match="缺少固定交易日bar"):
        build_future_range_artifact(missing_entry, generated_at=str(missing_entry["generated_at"]))

    invalid_path = deepcopy(missing_entry)
    _set_nested(invalid_path, ("records", 0, "offsets", 1, "d1_open_reference", "status"), "available")
    with pytest.raises(FutureRangeArtifactError, match="cumulative path 必须不可用"):
        build_future_range_artifact(invalid_path, generated_at=str(invalid_path["generated_at"]))


def test_future_range_cli_is_read_only_and_persists_insufficient_or_ready_artifact(
    tmp_path: Path,
) -> None:
    database = tmp_path / "cli.sqlite3"
    _initialize(database)
    run_id = _seed_run(database, scope=FULL_MARKET_SCOPE, with_targets=True)
    output_dir = tmp_path / "artifacts"
    before = database.read_bytes()

    command = [
        sys.executable,
        "tools/evaluate_market_scan_future_range.py",
        "--database",
        str(database),
        "--output-dir",
        str(output_dir),
        "--run-id",
        str(run_id),
        "--minimum-sample-size",
        "1",
        "--minimum-session-count",
        "1",
        "--complete-run-coverage",
        "0.5",
        "--bootstrap-samples",
        "100",
    ]
    completed = subprocess.run(
        command,
        cwd=Path(__file__).resolve().parents[1],
        check=True,
        capture_output=True,
        text=True,
    )
    summary = json.loads(completed.stdout)

    assert database.read_bytes() == before
    assert summary["database_read_only"] is True
    assert summary["database_query_only"] is True
    assert summary["database_bytes_unchanged"] is True
    assert summary["artifact_count"] == 1
    assert load_future_range_artifact(summary["artifact"])["payload"]["run"]["run_id"] == run_id  # type: ignore[index]

    outside = tmp_path / "future-cli-outside"
    outside.mkdir()
    alias = tmp_path / "future-cli-alias"
    alias.symlink_to(outside, target_is_directory=True)
    output_index = command.index("--output-dir") + 1
    for hostile_output in (alias, alias / "not-created"):
        hostile_command = list(command)
        hostile_command[output_index] = str(hostile_output)
        failed = subprocess.run(
            hostile_command,
            cwd=Path(__file__).resolve().parents[1],
            check=False,
            capture_output=True,
            text=True,
        )
        assert failed.returncode != 0
    assert list(outside.iterdir()) == []
    assert not (outside / "not-created").exists()


def _report(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> tuple[Path, dict[str, object]]:
    database = tmp_path / "artifact.sqlite3"
    _initialize(database)
    run_id = _seed_run(database, scope=FULL_MARKET_SCOPE, with_targets=True)
    monkeypatch.setattr(future_range_module, "next_trade_dates", lambda _value, _count: TARGET_DATES)
    evaluation = evaluate_market_scan_future_range(
        database,
        config=FutureRangeConfig(
            minimum_sample_size=1, minimum_session_count=1,
            complete_run_coverage=0.5, bootstrap_samples=100,
        ),
        run_ids=[run_id],
        generated_at="2026-01-08T00:00:00Z",
    )
    return database, cast(list[dict[str, object]], evaluation["reports"])[0]


def _set_nested(root: object, path: tuple[str | int, ...], value: object) -> None:
    current = root
    for key in path[:-1]:
        current = current[key]  # type: ignore[index]
    current[path[-1]] = value  # type: ignore[index]
