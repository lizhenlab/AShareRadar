import argparse
import json
from pathlib import Path

import pytest

from tools import collect_strategy_prospective as cli
from tests import test_strategy_prospective_collection as collection_fixtures

study = collection_fixtures.study


def test_cli_captures_and_reads_the_same_frozen_study(study, capsys):
    database, root, _clock = study
    common = ["--root", str(root), "--plan-id", "study"]
    assert cli.main([*common, "capture", "--database", str(database[0])]) == 0
    captured = json.loads(capsys.readouterr().out)
    assert captured["receipts_added"][0]["status"] == "captured"
    assert cli.main([*common, "status"]) == 0
    state = json.loads(capsys.readouterr().out)
    assert state["receipt_count"] == 1 and state["adoptable_template_id"] is None
    assert cli.main([*common, "evidence", "--history-root", str(root / "no-history"), "--public-root", str(root / "public")]) == 0
    output = json.loads(capsys.readouterr().out)
    assert output["official_execution_admitted"] is False and Path(output["report_path"]).is_file()
    report = json.loads(Path(output["report_path"]).read_text())
    public = report["public_execution_facts"]
    assert public["required_pair_count"] == report["requirements"]["pair_count"]
    assert public["official_execution_admitted"] is False
    assert cli.main([*common, "evaluate"]) == 0
    output = json.loads(capsys.readouterr().out)
    assert output["captured_session_count"] == 1 and output["adoptable_template_id"] is None


def test_cli_init_cannot_retroactively_register_today(study, capsys):
    _database, root, _clock = study
    assert cli.main(["--root", str(root), "--plan-id", "late", "init", "--start-date", "2026-07-17",
                     "--end-date", "2026-08-20"]) == 2
    output = capsys.readouterr()
    assert output.out == "" and json.loads(output.err)["error_type"] == "ValueError"
    assert not (root / "late" / "plan.json").exists()


def test_cli_init_writes_future_plan_and_retries_idempotently(study, capsys):
    _database, root, _clock = study
    args = ["--root", str(root), "--plan-id", "next", "init", "--start-date", "2026-07-20", "--end-date", "2026-08-20"]
    assert cli.main(args) == 0
    first = json.loads(capsys.readouterr().out)
    assert cli.main(args) == 0
    second = json.loads(capsys.readouterr().out)
    assert first["plan_digest"] == second["plan_digest"] and second["receipt_count"] == 0


@pytest.mark.parametrize("flag,value", [
    ("--official-registry", "registry.json"), ("--official-registry-digest", "a" * 64),
    ("--official-raw-root", "raw"), ("--official-session-directory", "sessions"),
])
def test_partial_official_source_flags_fail_before_evaluation(tmp_path, monkeypatch, capsys, flag, value):
    monkeypatch.setattr(cli, "evaluate_prospective_execution", lambda *_args, **_kwargs: pytest.fail("partial source was accepted"))
    assert cli.main(["--root", str(tmp_path), "--plan-id", "test", "evaluate", flag, value]) == 2
    assert json.loads(capsys.readouterr().err)["status"] == "failed"
    assert list(tmp_path.iterdir()) == []


def test_official_store_must_contain_strictly_verified_sessions(monkeypatch):
    class EmptyStore:
        def __init__(self, **_kwargs):
            pass
        def sessions(self):
            return ()
    monkeypatch.setattr(cli, "MarketScanOfficialExecutionStore", EmptyStore)
    args = argparse.Namespace(official_registry=Path("registry"), official_registry_digest="a" * 64,
                              official_raw_root=Path("raw"), official_session_directory=Path("sessions"))
    with pytest.raises(ValueError, match="no verified"):
        cli._official_sessions(args)


def test_cli_does_not_expose_error_payloads(tmp_path, monkeypatch, capsys):
    def fail(*_args):
        raise ValueError("sensitive-provider-response-must-not-appear")
    monkeypatch.setattr(cli, "strategy_prospective_status", fail)
    assert cli.main(["--root", str(tmp_path), "--plan-id", "test", "status"]) == 2
    output = capsys.readouterr()
    assert "sensitive" not in output.err and output.out == ""


def test_no_cli_clock_override_can_create_backdated_receipts(tmp_path):
    with pytest.raises(SystemExit):
        cli.main(["--root", str(tmp_path), "--plan-id", "test", "capture", "--as-of", "2020-01-01"])
