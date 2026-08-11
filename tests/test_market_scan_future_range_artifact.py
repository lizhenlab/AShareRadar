from __future__ import annotations

from copy import deepcopy
import json
import os
from pathlib import Path
import subprocess
import sys
from typing import Any, cast

import pytest

import app.services.market_scan_future_range as future_range_module
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


def test_future_range_cli_is_read_only_and_persists_insufficient_or_ready_artifact(
    tmp_path: Path,
) -> None:
    database = tmp_path / "cli.sqlite3"
    _initialize(database)
    run_id = _seed_run(database, scope=FULL_MARKET_SCOPE, with_targets=True)
    output_dir = tmp_path / "artifacts"
    before = database.read_bytes()

    completed = subprocess.run(
        [
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
        ],
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
