from __future__ import annotations

from copy import deepcopy
from datetime import date, datetime
import json
from pathlib import Path
import subprocess
import sys

import pytest

from app.artifacts.io import canonical_json_bytes, sha256_hex
from app.models.market_scan import MarketScanMarketProgress
from app.services.market_scan_official_execution_store import official_execution_session_filename
from app.services.market_scan_trial_registry import load_trial_registry, start_trial
from app.services.market_scan_trial_registry_contract import trial_registry_digest
from app.services.trading_calendar import trading_dates_between
from tests.test_market_scan_raw_score_replay import _snapshot
from tests import test_market_scan_raw_score_replay as raw_replay_fixtures
from tests.test_market_scan_research_portfolio import _official_sessions, row
from tools import run_market_scan_research as cli


@pytest.fixture
def bundle(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    item, run = _snapshot(monkeypatch, mutation="none", mode="official")
    item = item.model_copy(update={"rank": 1})
    run = run.model_copy(update={
        "status": "success", "finished_at": run.as_of, "snapshot_digest": "d" * 64,
        "snapshot_seal_origin": "publication", "snapshot_sealed_at": run.as_of,
        "market_progress": [
            MarketScanMarketProgress(market=market, total_count=int(market == item.market), processed_count=int(market == item.market),
                                     success_count=int(market == item.market), coverage_pct=100 if market == item.market else 0)
            for market in ("SH", "SZ", "BJ")
        ],
    })
    days = [day.isoformat() for day in trading_dates_between(date(2026, 5, 6), date(2026, 8, 31))]
    payload = {
        "schema_version": cli.RESEARCH_INPUT_BUNDLE_VERSION,
        "snapshots": [{"run": run.model_dump(mode="json"), "items": [item.model_dump(mode="json")]}],
        "calendar": {"trading_dates": days, "train_signal_dates": days[:3],
                     "calibration_signal_dates": days[12:15], "test_signal_dates": [run.data_date]},
        "exploration_cutoff": days[21], "execution_mode": "synthetic",
        "synthetic_rows": [row(day, symbol=item.symbol).model_dump(mode="json") for day in days],
    }
    path = tmp_path / "bundle.json"
    path.write_bytes(canonical_json_bytes(payload))
    return path


def _args(command: str, bundle: Path, *, output: str | None = None) -> list[str]:
    args = [command, "--bundle", str(bundle), "--registry-root", str(bundle.parent / "registries"),
            "--registration-id", "synthetic-retrospective"]
    if output is not None:
        args.extend(["--output", str(bundle.parent / output)])
    return args


def _mutate_bundle(path: Path, mutate) -> None:
    payload = json.loads(path.read_text())
    mutate(payload)
    path.write_bytes(canonical_json_bytes(payload))


def _register_and_run(bundle: Path) -> Path:
    assert cli.main(_args("register", bundle, output="registration.json")) == 0
    assert cli.main(_args("run", bundle, output="report.json")) == 0
    return bundle.parent / "report.json"


def test_cli_register_run_and_independent_verify_real_frozen_scores(bundle, capsys) -> None:
    saved_path = _register_and_run(bundle)
    saved = json.loads(saved_path.read_text())
    assert saved["promotion_eligible"] is False
    assert saved["registry"]["registration_kind"] == "retrospective"
    assert saved["registry"]["declared_family_complete"] is True
    assert len(saved["results"]) == 4
    assert all(value["candidate"]["provenance_status"] == "synthetic" for value in saved["results"].values())
    assert cli.main([*_args("verify", bundle), "--report", str(saved_path)]) == 0
    verification = json.loads(capsys.readouterr().out)
    assert verification["independent_replay"] == "matched"
    assert verification["report_digest"] == saved["digest"]
    assert verification["promotion_eligible"] is False


def test_cli_actual_subprocess_entry_point_can_verify_saved_report(bundle) -> None:
    saved = _register_and_run(bundle)
    result = subprocess.run([sys.executable, "tools/run_market_scan_research.py",
                             *_args("verify", bundle), "--report", str(saved)],
                            cwd=Path(__file__).resolve().parents[1], check=False, capture_output=True, text=True)
    assert result.returncode == 0, result.stderr
    assert json.loads(result.stdout)["independent_replay"] == "matched"


@pytest.mark.parametrize("mutation", [
    lambda payload: payload.pop("execution_mode"),
    lambda payload: payload.update(execution_mode="auto"),
    lambda payload: payload.update(execution_mode=[]),
    lambda payload: payload.update(official_store={}),
    lambda payload: payload.update(verified=True),
    lambda payload: payload.update(schema_version="unknown"),
    lambda payload: payload.update(snapshots={}),
    lambda payload: payload.update(synthetic_rows={}),
    lambda payload: payload.update(exploration_cutoff=123),
    lambda payload: payload["calendar"]["trading_dates"].pop(3),
    lambda payload: payload["calendar"].update(trading_dates=["2026-07-17"]),
])
def test_cli_rejects_ambiguous_or_incomplete_bundle_before_registration(bundle, mutation) -> None:
    _mutate_bundle(bundle, mutation)
    assert cli.main(_args("register", bundle, output="registration.json")) == 1
    assert not (bundle.parent / "registration.json").exists()
    assert not (bundle.parent / "registries" / "synthetic-retrospective" / "registration.json").exists()


def test_synthetic_bundle_cannot_assert_official_provenance_by_passing_pin(bundle) -> None:
    assert cli.main([*_args("register", bundle, output="registration.json"),
                     "--official-registry-digest", "a" * 64]) == 1


def test_cli_does_not_touch_registry_if_output_already_exists(bundle) -> None:
    output = bundle.parent / "registration.json"
    output.write_text("existing")
    assert cli.main(_args("register", bundle, output=output.name)) == 1
    assert output.read_text() == "existing"
    assert not (bundle.parent / "registries").exists()


@pytest.mark.parametrize("output", ["registries/synthetic-retrospective/extra.json",
                                    "registries/synthetic-retrospective/../synthetic-retrospective/extra.json"])
def test_cli_output_cannot_pollute_append_only_registry_namespace(bundle, output) -> None:
    assert cli.main(_args("register", bundle, output=output)) == 1
    assert not (bundle.parent / "registries").exists()


def test_independent_verify_rejects_redigested_false_report(bundle, capsys) -> None:
    report = _register_and_run(bundle)
    payload = json.loads(report.read_text())
    payload["promotion_eligible"] = True
    payload["digest"] = trial_registry_digest({key: value for key, value in payload.items() if key != "digest"})
    report.write_bytes(canonical_json_bytes(payload))
    assert cli.main([*_args("verify", bundle), "--report", str(report)]) == 1
    assert "does not match" in capsys.readouterr().err


def test_independent_verify_rejects_unhashed_report_corruption(bundle, capsys) -> None:
    report = _register_and_run(bundle)
    payload = json.loads(report.read_text())
    payload["conclusion"] = "forged conclusion"
    report.write_bytes(canonical_json_bytes(payload))
    assert cli.main([*_args("verify", bundle), "--report", str(report)]) == 1
    assert "saved report digest mismatch" in capsys.readouterr().err


def test_changed_input_cannot_run_against_existing_registration(bundle) -> None:
    assert cli.main(_args("register", bundle, output="registration.json")) == 0
    _mutate_bundle(bundle, lambda payload: payload["synthetic_rows"].pop())
    assert cli.main(_args("run", bundle, output="report.json")) == 1
    assert load_trial_registry(bundle.parent / "registries", "synthetic-retrospective").events == ()


def test_official_mode_requires_explicit_out_of_band_pin_and_strict_store(bundle, tmp_path) -> None:
    official_root = tmp_path / "official"
    official_root.mkdir()
    tokens = _official_sessions(official_root)
    sessions = official_root / "sessions"
    sessions.mkdir()
    for token in tokens:
        (sessions / official_execution_session_filename(token)).write_bytes(
            canonical_json_bytes(token.artifact),
        )
    config = {"registry_path": "official/registry.json", "raw_file_root": "official", "session_directory": "official/sessions"}
    _mutate_bundle(bundle, lambda payload: (payload.pop("synthetic_rows"),
                                           payload.update(execution_mode="official", official_store=config)))
    with pytest.raises(ValueError, match="official-registry-digest"):
        cli.load_research_bundle(bundle)
    digest = json.loads((official_root / "registry.json").read_text())["registry_digest"]
    loaded = cli.load_research_bundle(bundle, official_registry_digest=digest)
    assert len(loaded.official_sessions) == len(tokens)
    assert loaded.synthetic_rows == ()
    with pytest.raises(ValueError):
        cli.load_research_bundle(bundle, official_registry_digest="b" * 64)


def test_cli_failed_trial_exit_is_nonzero_and_output_is_preserved(bundle, monkeypatch) -> None:
    assert cli.main(_args("register", bundle, output="registration.json")) == 0
    failure = {"results": {"baseline": {"status": "failed", "reason": "retained"}}, "promotion_eligible": False}
    monkeypatch.setattr(cli, "run_registered_research", lambda *args, **kwargs: deepcopy(failure))
    assert cli.main(_args("run", bundle, output="failed.json")) == 1
    assert json.loads((bundle.parent / "failed.json").read_text()) == failure


def test_cli_cancelled_execution_returns_130(bundle, monkeypatch) -> None:
    assert cli.main(_args("register", bundle, output="registration.json")) == 0
    def cancelled(*args, **kwargs):
        raise KeyboardInterrupt
    monkeypatch.setattr(cli, "run_registered_research", cancelled)
    assert cli.main(_args("run", bundle, output="cancelled.json")) == 130
    assert not (bundle.parent / "cancelled.json").exists()


def test_cli_resume_preserves_interrupted_attempt_and_runs_remaining_family(bundle) -> None:
    assert cli.main(_args("register", bundle, output="registration.json")) == 0
    root = bundle.parent / "registries"
    start_trial(root, "synthetic-retrospective", "production_v5")
    assert cli.main([*_args("run", bundle, output="resumed-report.json"), "--resume"]) == 1
    state = load_trial_registry(root, "synthetic-retrospective")
    assert state.seal is not None
    assert state.statuses["production_v5"] == "cancelled"
    assert sum(status == "succeeded" for status in state.statuses.values()) == 3
    assert len([event for event in state.events if event["trial_id"] == "production_v5"]) == 2


def _external_snapshot_bundle(bundle: Path) -> tuple[Path, dict]:
    payload = json.loads(bundle.read_text())
    snapshot = payload.pop("snapshots")[0]
    path = bundle.parent / "snapshot-1.json"
    path.write_bytes(canonical_json_bytes(snapshot))
    payload["snapshot_files"] = [{"path": path.name, "sha256": sha256_hex(path.read_bytes())}]
    bundle.write_bytes(canonical_json_bytes(payload))
    return path, snapshot


def _second_snapshot(monkeypatch, first: dict) -> dict:
    original_quote = raw_replay_fixtures._quote
    with monkeypatch.context() as patch:
        patch.setattr(raw_replay_fixtures, "DATA_DATE", date(2026, 7, 20))
        patch.setattr(raw_replay_fixtures, "AS_OF", datetime(2026, 7, 20, 16, 30))
        patch.setattr(raw_replay_fixtures, "_quote", lambda: original_quote(timestamp="2026-07-20 15:00:00"))
        item, run = raw_replay_fixtures._snapshot(patch, mutation="none", mode="official")
    run_data = run.model_dump(mode="json")
    run_data.update(id=2, status="success", finished_at=run.as_of, snapshot_digest="e" * 64,
                    snapshot_seal_origin="publication", snapshot_sealed_at=run.as_of,
                    market_progress=first["run"]["market_progress"])
    return {"run": run_data, "items": [item.model_copy(update={"run_id": 2, "rank": 1}).model_dump(mode="json")]}


def test_external_snapshot_files_stream_and_normalize_two_batch_order(bundle, monkeypatch) -> None:
    first_path, first = _external_snapshot_bundle(bundle)
    second_path = bundle.parent / "snapshot-2.json"
    second_path.write_bytes(canonical_json_bytes(_second_snapshot(monkeypatch, first)))
    reference = {"path": second_path.name, "sha256": sha256_hex(second_path.read_bytes())}
    _mutate_bundle(bundle, lambda payload: (payload["snapshot_files"].append(reference),
                                           payload["calendar"]["test_signal_dates"].append("2026-07-20")))
    original_prepare = cli.prepare_research_dataset
    observed: list[str] = []
    def streamed(snapshots):
        assert iter(snapshots) is snapshots  # The boundary receives a generator, not all raw PIT.
        observed.append("generator")
        return original_prepare(snapshots)
    monkeypatch.setattr(cli, "prepare_research_dataset", streamed)
    ordered = cli.load_research_bundle(bundle)
    _mutate_bundle(bundle, lambda payload: payload["snapshot_files"].reverse())
    reversed_bundle = cli.load_research_bundle(bundle)
    assert ordered.dataset == reversed_bundle.dataset
    assert [batch.run_id for batch in ordered.dataset.batches] == [1, 2]
    assert observed == ["generator", "generator"]
    assert first_path.is_file()


@pytest.mark.parametrize("mutation", ["missing", "hash", "inline", "reference_extra"])
def test_external_snapshot_missing_hash_conflict_and_mixed_inputs_fail_closed(bundle, mutation) -> None:
    path, snapshot = _external_snapshot_bundle(bundle)
    if mutation == "missing":
        path.unlink()
    elif mutation == "hash":
        path.write_text("{}")
    elif mutation == "inline":
        _mutate_bundle(bundle, lambda payload: payload.update(snapshots=[snapshot]))
    else:
        _mutate_bundle(bundle, lambda payload: payload["snapshot_files"][0].update(verified=True))
    assert cli.main(_args("register", bundle, output="registration.json")) == 1
    assert not (bundle.parent / "registries").exists()


def test_external_snapshot_file_bundle_runs_and_replays(bundle) -> None:
    _external_snapshot_bundle(bundle)
    report = _register_and_run(bundle)
    assert cli.main([*_args("verify", bundle), "--report", str(report)]) == 0
