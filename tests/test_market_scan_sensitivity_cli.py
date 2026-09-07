from copy import deepcopy
from dataclasses import replace
import json
from pathlib import Path
import subprocess
import sys

import pytest

from app.artifacts.io import canonical_json_bytes, sha256_hex
from tests import test_run_market_scan_research_cli as research_fixtures
from tests.test_market_scan_research_sensitivity import plan
from tools import run_market_scan_sensitivity as cli


bundle = research_fixtures.bundle


def inputs(bundle, tmp_path):
    value = json.loads(bundle.read_text())
    specification = plan(signal_dates=value["calendar"]["test_signal_dates"])
    path = tmp_path / "scenario-plan.json"
    path.write_bytes(canonical_json_bytes(specification))
    return ["--bundle", str(bundle), "--plan", str(path), "--plan-digest", sha256_hex(canonical_json_bytes(specification)),
            "--output", str(tmp_path / "scenario-report.json")]


def test_cli_pinned_grid_runs_reproducibly_without_registering_or_mutating_inputs(bundle, tmp_path):
    args = inputs(bundle, tmp_path)
    source = bundle.read_bytes()
    assert cli.main(args) == 0
    result = json.loads(Path(args[-1]).read_text())
    assert result["declared_cell_count"] == 8
    assert result["source_calendar"]["test_signal_dates"] == result["plan"]["signal_dates"]
    assert all(cell["account"]["provenance_status"] == "synthetic" for cell in result["cells"])
    assert bundle.read_bytes() == source and not (tmp_path / "registries").exists()
    alternate = [*args[:-1], str(tmp_path / "replay.json")]
    assert cli.main(alternate) == 0
    assert Path(args[-1]).read_bytes() == Path(alternate[-1]).read_bytes()
    assert cli.main(args) == 1


def test_wrong_pin_rejects_before_forward_bundle_is_read(bundle, tmp_path, monkeypatch):
    args = inputs(bundle, tmp_path)
    args[args.index("--plan-digest") + 1] = "f" * 64
    monkeypatch.setattr(cli, "load_research_bundle", lambda *a, **kw: pytest.fail("should not read future outcomes"))
    assert cli.main(args) == 1 and not Path(args[-1]).exists()


@pytest.mark.parametrize("change", ["signals", "cutoff", "calendar-membership"])
def test_bundle_metadata_cannot_change_the_evaluated_window_silently(bundle, tmp_path, monkeypatch, change):
    args = inputs(bundle, tmp_path)
    loaded = cli.load_research_bundle(bundle)
    calendar = deepcopy(loaded.calendar)
    if change == "signals":
        calendar["test_signal_dates"] = []
    if change == "calendar-membership":
        calendar["trading_dates"] = calendar["trading_dates"][:2]
    loaded = replace(loaded, calendar=calendar, exploration_cutoff="2099-01-01" if change == "cutoff" else loaded.exploration_cutoff)
    monkeypatch.setattr(cli, "load_research_bundle", lambda *a, **kw: loaded)
    assert cli.main(args) == 1 and not Path(args[-1]).exists()


@pytest.mark.parametrize("encoded", [b'{"a":1,"a":2}', b'{"x":NaN}', b'[]'])
def test_invalid_plan_json_never_writes_an_artifact(tmp_path, encoded):
    plan_path = tmp_path / "plan.json"
    plan_path.write_bytes(encoded)
    output = tmp_path / "out.json"
    assert cli.main(["--plan", str(plan_path), "--plan-digest", "a" * 64, "--bundle", "absent.json", "--output", str(output)]) == 1
    assert not output.exists()


def test_failed_grid_is_saved_and_returns_distinct_nonzero_status(bundle, tmp_path, monkeypatch):
    args = inputs(bundle, tmp_path)
    monkeypatch.setattr(cli, "run_research_sensitivity", lambda *a, **kw: {"status": "incomplete", "digest": "a" * 64})
    assert cli.main(args) == 2
    assert json.loads(Path(args[-1]).read_text())["status"] == "incomplete"


@pytest.mark.parametrize("missing", ["valuation", "all"])
def test_actual_missing_evidence_is_retained_and_returns_incomplete(bundle, tmp_path, missing):
    args = inputs(bundle, tmp_path)
    value = json.loads(bundle.read_text())
    sessions = value["calendar"]["trading_dates"]
    exit_day = sessions[sessions.index(value["calendar"]["test_signal_dates"][0]) + 2]
    value["synthetic_rows"] = [] if missing == "all" else [item for item in value["synthetic_rows"] if item["session_date"] != exit_day]
    bundle.write_bytes(canonical_json_bytes(value))
    assert cli.main(args) == 2
    result = json.loads(Path(args[-1]).read_text())
    assert result["status"] == "incomplete" and result["comparable_cell_count"] == 0
    assert result["replayed_cell_count"] == result["declared_cell_count"] == 8


def test_output_symlink_is_not_followed(bundle, tmp_path):
    args = inputs(bundle, tmp_path)
    target = tmp_path / "never-written.json"
    Path(args[-1]).symlink_to(target)
    assert cli.main(args) == 1 and not target.exists()


def test_actual_cli_entrypoint_runs(bundle, tmp_path):
    args = inputs(bundle, tmp_path)
    result = subprocess.run([sys.executable, "tools/run_market_scan_sensitivity.py", *args], cwd=Path(__file__).resolve().parents[1],
                            capture_output=True, text=True, check=False)
    assert result.returncode == 0 and "Traceback" not in result.stderr
