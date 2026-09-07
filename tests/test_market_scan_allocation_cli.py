from copy import deepcopy
import json
from pathlib import Path
import subprocess
import sys

import pytest

from app.artifacts.io import canonical_json_bytes
from app.services.market_scan_allocation_contracts import allocation_digest, allocation_source_digests
from tests.test_market_scan_allocation import inputs, policy
from tools import plan_market_scan_allocation as cli


def arguments(tmp_path, value=None):
    value = inputs() if value is None else value
    settings = policy()
    source, config, output = (tmp_path / name for name in ("sources.json", "policy.json", "plan.json"))
    source.write_bytes(canonical_json_bytes(value))
    config.write_bytes(canonical_json_bytes(settings))
    pins = allocation_source_digests(value)
    return ["--input", str(source), "--policy", str(config), "--output", str(output), "--account-digest", pins["account"],
            "--candidates-digest", pins["candidates"], "--market-digest", pins["market"], "--policy-digest", allocation_digest(settings)]


def test_actual_cli_emits_an_immutable_plan_and_leaves_inputs_intact(tmp_path):
    args = arguments(tmp_path)
    before = {name: (tmp_path / name).read_bytes() for name in ("sources.json", "policy.json")}
    process = subprocess.run([sys.executable, "tools/plan_market_scan_allocation.py", *args], capture_output=True, text=True,
                             cwd=Path(__file__).resolve().parents[1], timeout=10)
    assert process.returncode == 0, process.stderr
    result = json.loads((tmp_path / "plan.json").read_bytes())
    assert process.stdout.strip() == result["result_digest"]
    assert result["proposed_orders"][0]["quantity"] == 500
    assert result["orders_submitted"] is False
    saved = (tmp_path / "plan.json").read_bytes()
    assert cli.main(args) == 1 and (tmp_path / "plan.json").read_bytes() == saved
    assert all((tmp_path / name).read_bytes() == value for name, value in before.items())


def test_blocked_account_preserves_inspectable_plan_with_nonzero_exit(tmp_path):
    args = arguments(tmp_path, inputs(cash=6000.0))
    assert cli.main(args) == 2
    result = json.loads((tmp_path / "plan.json").read_bytes())
    assert result["status"] == "blocked" and result["proposed_orders"] == []


@pytest.mark.parametrize("name", ["account", "candidates", "market", "policy"])
def test_cli_requires_every_independently_retained_digest(tmp_path, name):
    args = arguments(tmp_path)
    args[args.index("--" + name + "-digest") + 1] = "f" * 64
    assert cli.main(args) == 1
    assert not (tmp_path / "plan.json").exists()


@pytest.mark.parametrize("encoded", [b'{"x":1,"x":2}', b'{"value":NaN}', b'[]', b'{'])
def test_cli_rejects_injected_or_invalid_json_without_output(tmp_path, encoded):
    args = arguments(tmp_path)
    (tmp_path / "sources.json").write_bytes(encoded)
    assert cli.main(args) == 1 and not (tmp_path / "plan.json").exists()


@pytest.mark.parametrize("which", ["input", "output", "output_parent"])
def test_cli_does_not_follow_input_or_output_aliases(tmp_path, which):
    args = arguments(tmp_path)
    alias = tmp_path / "alias"
    if which == "input":
        alias.symlink_to(tmp_path / "sources.json")
        args[args.index("--input") + 1] = str(alias)
    elif which == "output":
        (tmp_path / "plan.json").symlink_to(tmp_path / "absent.json")
    else:
        destination = tmp_path / "destination"
        destination.mkdir()
        alias.symlink_to(destination, target_is_directory=True)
        args[args.index("--output") + 1] = str(alias / "plan.json")
    assert cli.main(args) == 1
    assert not (tmp_path / "absent.json").exists()
    assert not (tmp_path / "destination" / "plan.json").exists()


def test_output_race_cannot_claim_a_new_publication(tmp_path, monkeypatch):
    args = arguments(tmp_path)
    monkeypatch.setattr(cli, "exclusive_atomic_publish", lambda *_args, **_kwargs: False)
    assert cli.main(deepcopy(args)) == 1


@pytest.mark.parametrize("timestamp", ["0001-01-01T00:00:00+23:59", "9999-12-31T23:59:59-23:59"])
@pytest.mark.parametrize("location", ["decision", "source", "row"])
def test_cli_timestamp_bounds_are_rejected_without_tracebacks(tmp_path, timestamp, location):
    value = inputs()
    if location == "decision":
        value["decision_at"] = timestamp
    elif location == "source":
        value["account"]["observed_as_of"] = timestamp
    else:
        value["market"]["rows"][1]["valuation_observed_at"] = timestamp
    args = arguments(tmp_path, value)
    process = subprocess.run([sys.executable, "tools/plan_market_scan_allocation.py", *args], capture_output=True, text=True,
                             cwd=Path(__file__).resolve().parents[1], timeout=10)
    assert process.returncode == 1
    assert "Traceback" not in process.stderr
    assert "ValueError" in process.stderr
    assert not (tmp_path / "plan.json").exists()
