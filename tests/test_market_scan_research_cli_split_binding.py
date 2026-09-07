from __future__ import annotations

from pathlib import Path

import pytest

from app.services import market_scan_research_experiment as experiment
from app.services.market_scan_trial_registry import load_trial_registry, start_trial
from tests.test_run_market_scan_research_cli import _args, _mutate_bundle, bundle as bundle_fixture
from tools import run_market_scan_research as cli


bundle = bundle_fixture


def _registry_bytes(bundle: Path) -> dict[str, bytes]:
    directory = bundle.parent / "registries" / "synthetic-retrospective"
    return {path.name: path.read_bytes() for path in directory.iterdir()}


@pytest.mark.parametrize("command", ["run", "resume", "replay", "verify"])
@pytest.mark.parametrize("change", ["cutoff", "trading_dates", "train_signal_dates", "test_signal_dates"])
def test_cli_rejects_changed_split_before_execution_or_recovery(bundle, monkeypatch, capsys, command, change) -> None:
    monkeypatch.setattr(experiment, "research_implementation_digest", lambda: "a" * 64)
    assert cli.main(_args("register", bundle, output="registration.json")) == 0
    if command in {"replay", "verify"}:
        assert cli.main(_args("run", bundle, output="report.json")) == 0
    if command == "resume":
        start_trial(bundle.parent / "registries", "synthetic-retrospective", "production_v5")
    before = _registry_bytes(bundle)

    def mutate(payload):
        if change == "cutoff":
            payload["exploration_cutoff"] = "2099-01-01"
        elif change == "test_signal_dates":
            payload["calendar"][change] = ["2026-07-20"]
        else:
            payload["calendar"][change].pop()

    _mutate_bundle(bundle, mutate)
    args = _args("run" if command == "resume" else command, bundle, output="changed-report.json")
    if command == "resume":
        args.append("--resume")
    if command == "verify":
        args.extend(["--report", str(bundle.parent / "report.json")])
    assert cli.main(args) == 1
    assert "calendar or exploration cutoff differs from frozen registration" in capsys.readouterr().err
    assert _registry_bytes(bundle) == before
    assert not (bundle.parent / "changed-report.json").exists()
    state = load_trial_registry(bundle.parent / "registries", "synthetic-retrospective")
    if command == "run":
        assert state.events == ()
    if command == "resume":
        assert state.statuses == {"production_v5": "running"}
