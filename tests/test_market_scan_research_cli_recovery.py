from __future__ import annotations

import json
from pathlib import Path

import pytest

from app.services.market_scan_trial_registry import load_trial_registry
from tests.test_run_market_scan_research_cli import _args, _mutate_bundle, bundle as bundle_fixture
from tools import run_market_scan_research as cli


bundle = bundle_fixture


def _registry_bytes(bundle: Path) -> dict[str, bytes]:
    directory = bundle.parent / "registries" / "synthetic-retrospective"
    return {path.name: path.read_bytes() for path in directory.iterdir()}


def _failed_publish(*args, **kwargs) -> None:
    raise OSError("simulated output device failure")


def test_registration_receipt_can_be_recovered_without_input_after_output_failure(bundle, monkeypatch) -> None:
    with monkeypatch.context() as patch:
        patch.setattr(cli, "exclusive_atomic_publish", _failed_publish)
        assert cli.main(_args("register", bundle, output="lost-registration.json")) == 1
    state = load_trial_registry(bundle.parent / "registries", "synthetic-retrospective")
    original = _registry_bytes(bundle)
    bundle.unlink()
    assert cli.main([
        "export-registration", "--registry-root", str(bundle.parent / "registries"),
        "--registration-id", "synthetic-retrospective", "--output", str(bundle.parent / "recovered.json"),
    ]) == 0
    recovered = json.loads((bundle.parent / "recovered.json").read_text())
    assert recovered["registration"] == state.registration
    assert recovered["verification"]["local_integrity"] == "verified"
    assert _registry_bytes(bundle) == original


def test_sealed_run_report_can_be_replayed_after_output_failure_without_new_attempt(bundle, monkeypatch) -> None:
    assert cli.main(_args("register", bundle, output="registration.json")) == 0
    with monkeypatch.context() as patch:
        patch.setattr(cli, "exclusive_atomic_publish", _failed_publish)
        assert cli.main(_args("run", bundle, output="lost-report.json")) == 1
    original = _registry_bytes(bundle)
    assert load_trial_registry(bundle.parent / "registries", "synthetic-retrospective").seal is not None
    assert cli.main(_args("replay", bundle, output="recovered-report.json")) == 0
    assert cli.main([*_args("verify", bundle), "--report", str(bundle.parent / "recovered-report.json")]) == 0
    assert _registry_bytes(bundle) == original
    assert not (bundle.parent / "lost-report.json").exists()


def test_report_recovery_rejects_changed_input_and_unsealed_registry(bundle, capsys) -> None:
    assert cli.main(_args("register", bundle, output="registration.json")) == 0
    assert cli.main(_args("replay", bundle, output="early-report.json")) == 1
    assert "sealed registry" in capsys.readouterr().err
    assert cli.main(_args("run", bundle, output="report.json")) == 0
    original = _registry_bytes(bundle)
    _mutate_bundle(bundle, lambda payload: payload["synthetic_rows"].pop())
    assert cli.main(_args("replay", bundle, output="changed-report.json")) == 1
    assert "input manifest mismatch" in capsys.readouterr().err
    assert _registry_bytes(bundle) == original
    assert not (bundle.parent / "changed-report.json").exists()


@pytest.mark.parametrize("command", ["register", "run"])
def test_output_failure_identifies_safe_recovery_command(bundle, monkeypatch, capsys, command) -> None:
    if command == "run":
        assert cli.main(_args("register", bundle, output="registration.json")) == 0
    monkeypatch.setattr(cli, "exclusive_atomic_publish", _failed_publish)
    assert cli.main(_args(command, bundle, output="missing.json")) == 1
    assert ("export-registration" if command == "register" else "replay") in capsys.readouterr().err
