from __future__ import annotations

from datetime import datetime
import json
from pathlib import Path
import sqlite3

import pytest

from tools import track_strategy_templates as cli


def _report():
    return {"schema_version": "strategy-template-tracking-v1", "cohorts": [], "status": "insufficient_data",
            "source_session_count": 0, "horizon_sessions": 10, "notional_cash_cny": 1_000_000}


def test_cli_preserves_fixed_defaults_and_content_addressed_artifacts(tmp_path, monkeypatch, capsys):
    seen = {}
    def evaluate(database, **kwargs):
        seen.update(database=database, **kwargs)
        return _report()
    monkeypatch.setattr(cli, "evaluate_strategy_template_tracking", evaluate)
    assert cli.main(["--database", str(tmp_path / "local.sqlite3"), "--output-directory", str(tmp_path / "report")]) == 0
    output = json.loads(capsys.readouterr().out)
    assert seen == {"database": tmp_path / "local.sqlite3", "as_of": None, "horizon": 10,
                    "notional_cash_cny": 1_000_000, "run_ids": None, "official_sessions": (), "non_overlapping_signals": False}
    assert json.loads(Path(output["json_path"]).read_text()) == _report()
    assert "策略模板对照" in Path(output["html_path"]).read_text()
    assert output["provider_requests"] == 0 and output["production_ranking_effect"] == "none"


def test_cli_passes_custom_horizon_cash_cutoff_and_repeated_run_ids(tmp_path, monkeypatch, capsys):
    seen = {}
    def evaluate(database, **kwargs):
        seen.update(kwargs)
        return _report()
    monkeypatch.setattr(cli, "evaluate_strategy_template_tracking", evaluate)
    assert cli.main(["--database", "db.sqlite3", "--output-directory", str(tmp_path), "--horizon", "5",
                     "--notional-cash-cny", "2000000", "--as-of", "2026-09-19T16:00:00+08:00",
                     "--run-id", "7", "--run-id", "8"]) == 0
    assert seen == {"as_of": datetime.fromisoformat("2026-09-19T16:00:00+08:00"), "horizon": 5,
                    "notional_cash_cny": 2_000_000, "run_ids": [7, 8], "official_sessions": (), "non_overlapping_signals": False}
    assert json.loads(capsys.readouterr().out)["evidence_status"] == "insufficient_data"


def test_cli_passes_fixed_non_overlapping_schedule(tmp_path, monkeypatch, capsys):
    seen = {}
    def evaluate(database, **kwargs):
        seen.update(kwargs)
        return {**_report(), "strategy_selection": {
            "status": "insufficient_data", "adoptable_template_id": None,
        }}
    monkeypatch.setattr(cli, "evaluate_strategy_template_tracking", evaluate)
    assert cli.main(["--database", "db.sqlite3", "--output-directory", str(tmp_path),
                     "--non-overlapping-signals"]) == 0
    assert seen["non_overlapping_signals"] is True and seen["run_ids"] is None
    output = json.loads(capsys.readouterr().out)
    assert output["selection_status"] == "insufficient_data"
    assert output["adoptable_template_id"] is None


def test_publication_is_immutable_and_repeated_publication_reuses_files(tmp_path):
    first = cli.publish_strategy_template_tracking_report(_report(), tmp_path)
    before = [(item.stat().st_ino, item.read_bytes()) for item in first]
    assert cli.publish_strategy_template_tracking_report(_report(), tmp_path) == first
    assert [(item.stat().st_ino, item.read_bytes()) for item in first] == before
    other = cli.publish_strategy_template_tracking_report({**_report(), "horizon_sessions": 5}, tmp_path)
    assert other != first and first[0].stem.startswith("strategy-template-tracking-")


@pytest.mark.parametrize("component", [".git", ".codex", ".agents", ".venv"])
def test_protected_output_directory_is_rejected_before_creation(tmp_path, component):
    with pytest.raises(ValueError, match="输出目录"):
        cli.publish_strategy_template_tracking_report(_report(), tmp_path / component / "output")
    assert not (tmp_path / component).exists()


def test_symlink_output_is_rejected(tmp_path):
    target = tmp_path / "real"
    target.mkdir()
    alias = tmp_path / "alias"
    alias.symlink_to(target, target_is_directory=True)
    with pytest.raises(ValueError, match="输出目录"):
        cli.publish_strategy_template_tracking_report(_report(), alias)
    assert list(target.iterdir()) == []


@pytest.mark.parametrize("bad_html", [True, False])
def test_both_artifacts_are_size_checked_before_publication(tmp_path, monkeypatch, bad_html):
    monkeypatch.setattr(cli, "MAX_REPORT_BYTES", 512)
    monkeypatch.setattr(cli, "render_strategy_template_tracking_html", lambda _: "x" * (513 if bad_html else 20))
    report = _report() if bad_html else {**_report(), "oversized": "x" * 513}
    with pytest.raises(ValueError, match="大小上限"):
        cli.publish_strategy_template_tracking_report(report, tmp_path)
    assert list(tmp_path.iterdir()) == []


def test_render_failure_does_not_leave_partial_json(tmp_path, monkeypatch):
    def fail(_report):
        raise ValueError("invalid render")
    monkeypatch.setattr(cli, "render_strategy_template_tracking_html", fail)
    with pytest.raises(ValueError):
        cli.publish_strategy_template_tracking_report(_report(), tmp_path)
    assert list(tmp_path.iterdir()) == []


@pytest.mark.parametrize("error", [sqlite3.OperationalError, ValueError, OSError])
def test_cli_errors_are_sanitized_and_never_claim_success(tmp_path, monkeypatch, capsys, error):
    def fail(*args, **kwargs):
        raise error("PRIVATE database detail and credential")
    monkeypatch.setattr(cli, "evaluate_strategy_template_tracking", fail)
    assert cli.main(["--database", "missing.sqlite3", "--output-directory", str(tmp_path)]) == 2
    output = capsys.readouterr()
    assert output.out == ""
    assert json.loads(output.err) == {"status": "failed", "error_type": error.__name__}
    assert "PRIVATE" not in output.err and list(tmp_path.iterdir()) == []


def test_cli_rejects_unsupported_horizon_without_reading_database(tmp_path, monkeypatch):
    monkeypatch.setattr(cli, "evaluate_strategy_template_tracking", lambda *args, **kwargs: pytest.fail("database read"))
    with pytest.raises(SystemExit) as exc:
        cli.main(["--database", "db", "--output-directory", str(tmp_path), "--horizon", "3"])
    assert exc.value.code == 2


@pytest.mark.parametrize('flag', ['--official-registry', '--official-registry-digest', '--official-raw-root', '--official-session-directory'])
def test_cli_rejects_partial_official_evidence_without_evaluating_database(tmp_path, monkeypatch, capsys, flag):
    monkeypatch.setattr(cli, 'evaluate_strategy_template_tracking', lambda *_args, **_kwargs: pytest.fail('should not evaluate'))
    assert cli.main(['--database', 'unused', '--output-directory', str(tmp_path / 'report'), flag, 'incomplete']) == 2
    assert json.loads(capsys.readouterr().err) == {'status': 'failed', 'error_type': 'ValueError'}
    assert not (tmp_path / 'report').exists()


def _official_cli_args(tmp_path):
    from tests.test_market_scan_official_execution import _artifacts
    registry_path, artifact_path, raw_root, registry = _artifacts(tmp_path)
    sessions = tmp_path / 'sessions'
    store = cli.MarketScanOfficialExecutionStore(registry_path=registry_path, registry_digest=registry['registry_digest'],
                                               raw_file_root=raw_root, session_directory=sessions)
    store.ingest(artifact_path)
    return ['--official-registry', str(registry_path), '--official-registry-digest', registry['registry_digest'],
            '--official-raw-root', str(raw_root), '--official-session-directory', str(sessions)]


def test_cli_passes_only_strictly_verified_official_sessions(tmp_path, monkeypatch, capsys):
    flags = _official_cli_args(tmp_path)
    seen = {}
    def evaluate(_database, **kwargs):
        seen.update(kwargs)
        return {**_report(), 'strategy_selection': {'status': 'insufficient_data', 'adoptable_template_id': None}}
    monkeypatch.setattr(cli, 'evaluate_strategy_template_tracking', evaluate)
    assert cli.main(['--database', 'unused', '--output-directory', str(tmp_path / 'report'), *flags]) == 0
    sessions = seen['official_sessions']
    assert len(sessions) == 1 and isinstance(sessions[0], cli.VerifiedOfficialExecutionSession)
    assert sessions[0].session_date == '2026-08-25'
    output = json.loads(capsys.readouterr().out)
    assert output['adoptable_template_id'] is None and output['selection_status'] == 'insufficient_data'


def test_cli_rejects_tampered_raw_official_delivery(tmp_path, monkeypatch, capsys):
    flags = _official_cli_args(tmp_path)
    (tmp_path / 'raw/2026/08/25/sh.raw').write_bytes(b'changed after intake')
    monkeypatch.setattr(cli, 'evaluate_strategy_template_tracking', lambda *_args, **_kwargs: pytest.fail('should not evaluate'))
    assert cli.main(['--database', 'unused', '--output-directory', str(tmp_path / 'report'), *flags]) == 2
    output = capsys.readouterr()
    assert output.out == ''
    assert 'raw' not in output.err and not (tmp_path / 'report').exists()


def test_explicit_empty_official_store_is_not_a_silent_fallback(tmp_path, monkeypatch, capsys):
    flags = _official_cli_args(tmp_path)
    for path in (tmp_path / 'sessions').glob('*.json'):
        path.unlink()
    monkeypatch.setattr(cli, 'evaluate_strategy_template_tracking', lambda *_args, **_kwargs: pytest.fail('should not evaluate'))
    assert cli.main(['--database', 'unused', '--output-directory', str(tmp_path / 'report'), *flags]) == 2
    assert json.loads(capsys.readouterr().err)['error_type'] == 'ValueError'
