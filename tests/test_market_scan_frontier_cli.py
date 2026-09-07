from copy import deepcopy
import json
from pathlib import Path
import subprocess
import sys

import pytest

from app.artifacts.io import canonical_json_bytes
from app.services.market_scan_research_portfolio import research_portfolio_payload
from tests.test_market_scan_probability_coherence import event, payload
from tests.test_market_scan_research_holdings import sidecar
from tests.test_market_scan_research_portfolio import replay
from tests import test_run_market_scan_research_cli as research_fixtures
from tools import audit_market_scan_frontier as cli


bundle = research_fixtures.bundle


def test_execution_cli_uses_existing_bundle_admission_without_new_registration(bundle, tmp_path):
    output = tmp_path / "audit.json"
    assert cli.main(["execution", "--bundle", str(bundle), "--top-n", "1", "--output", str(output)]) == 0
    result = json.loads(output.read_text())
    assert result["source_provenance"] == "synthetic"
    assert result["records"][0]["signal_label_starts_before_available"] is True
    assert result["candidate_account"]["result_digest"] == result["production_account_digest"]
    assert not (tmp_path / "registries").exists()
    before = output.read_bytes()
    assert cli.main(["execution", "--bundle", str(bundle), "--output", str(output)]) == 1
    assert output.read_bytes() == before


def test_probability_violation_returns_nonzero_and_retains_inspectable_report(tmp_path):
    source, output = tmp_path / "input.json", tmp_path / "report.json"
    source.write_bytes(canonical_json_bytes(payload(event("five", "gt", .05, .2), event("ten", "gt", .1, .8))))
    before = source.read_bytes()
    process = subprocess.run([sys.executable, "tools/audit_market_scan_frontier.py", "coherence", "--input", str(source),
                              "--output", str(output)], cwd=Path(__file__).resolve().parents[1], capture_output=True, text=True, check=False)
    assert process.returncode == 1 and json.loads(output.read_text())["status"] == "incoherent"
    assert source.read_bytes() == before


def test_holdings_cli_uses_explicit_independent_digest_pins(tmp_path):
    account = replay()
    classifications = sidecar(account)
    portfolio, evidence, output = (tmp_path / name for name in ("portfolio.json", "classifications.json", "holdings.json"))
    portfolio.write_bytes(canonical_json_bytes(research_portfolio_payload(account)))
    evidence.write_bytes(canonical_json_bytes(classifications))
    args = ["holdings", "--portfolio", str(portfolio), "--portfolio-digest", account.result_digest,
            "--classifications", str(evidence), "--classification-digest", classifications["classification_digest"], "--output", str(output)]
    assert cli.main(args) == 0
    report = json.loads(output.read_text())
    assert report["days"][1]["positions"][0]["nav_weight"] > 0
    assert report["provenance"]["official_pit_verified"] is False
    changed = deepcopy(args)
    changed[changed.index("--portfolio-digest") + 1] = "e" * 64
    changed[-1] = str(tmp_path / "bad.json")
    assert cli.main(changed) == 1 and not Path(changed[-1]).exists()


@pytest.mark.parametrize("encoded", [b'{"groups": [], "groups": []}', b'{"x": NaN}', b'[]', b'{'])
def test_invalid_json_never_produces_report(tmp_path, encoded):
    source, output = tmp_path / "bad.json", tmp_path / "out.json"
    source.write_bytes(encoded)
    assert cli.main(["coherence", "--input", str(source), "--output", str(output)]) == 1
    assert not output.exists()


def test_input_or_output_alias_not_followed(tmp_path):
    real, alias, output = (tmp_path / name for name in ("real.json", "alias.json", "report.json"))
    real.write_bytes(canonical_json_bytes(payload(event("a", "gt", .05, .5))))
    alias.symlink_to(real)
    assert cli.main(["coherence", "--input", str(alias), "--output", str(output)]) == 1
    output.symlink_to(tmp_path / "absent.json")
    assert cli.main(["coherence", "--input", str(real), "--output", str(output)]) == 1
    assert not (tmp_path / "absent.json").exists()


@pytest.mark.parametrize("stamp", ["0001-01-01T00:00:00+14:00", "9999-12-31T23:59:59-12:00"])
def test_extreme_timezone_returns_a_clean_cli_failure(tmp_path, stamp):
    value = payload(event("a", "gt", 0., .5))
    value["groups"][0]["context"]["decision_at"] = stamp
    source, output = tmp_path / "overflow.json", tmp_path / "report.json"
    source.write_bytes(canonical_json_bytes(value))
    result = subprocess.run([sys.executable, "tools/audit_market_scan_frontier.py", "coherence", "--input", str(source),
                             "--output", str(output)], cwd=Path(__file__).resolve().parents[1], capture_output=True, text=True, check=False)
    assert result.returncode == 1 and "Traceback" not in result.stderr
    assert not output.exists()
