from __future__ import annotations

from datetime import datetime
import json
from pathlib import Path
import sqlite3

import pytest

from tools import track_market_scan_scores as cli


def _report() -> dict[str, object]:
    return {
        "schema_version": "market-scan-score-tracking-v1",
        "generated_at": "2026-09-19T09:00:00+08:00",
        "as_of_completed_date": "2026-09-18",
        "cohorts": [], "promotion_eligible": False,
    }


def test_cli_passes_fixed_horizons_and_writes_both_artifacts(tmp_path, monkeypatch, capsys):
    seen = {}

    def evaluate(database, **kwargs):
        seen.update(database=database, **kwargs)
        return _report()

    monkeypatch.setattr(cli, "evaluate_market_scan_score_tracking", evaluate)
    assert cli.main([
        "--database", str(tmp_path / "source.db"), "--output-directory", str(tmp_path / "reports"),
        "--as-of", "2026-09-19T09:00:00+08:00", "--run-id", "7", "--run-id", "8",
    ]) == 0
    result = json.loads(capsys.readouterr().out)
    assert seen["mode"] == "official" and seen["run_ids"] == [7, 8]
    assert seen["as_of"] == datetime.fromisoformat("2026-09-19T09:00:00+08:00")
    assert seen["config"].horizons == (1, 5, 20)
    assert json.loads(Path(result["json_path"]).read_text()) == _report()
    assert "<html" in Path(result["html_path"]).read_text()
    assert result["provider_requests"] == 0


def test_publication_is_content_addressed_and_repeats_do_not_modify_files(tmp_path):
    first = cli.publish_score_tracking_report(_report(), tmp_path)
    before = [(path.stat().st_ino, path.read_bytes()) for path in first]
    assert cli.publish_score_tracking_report(_report(), tmp_path) == first
    assert [(path.stat().st_ino, path.read_bytes()) for path in first] == before
    second = cli.publish_score_tracking_report({**_report(), "as_of_completed_date": "2026-09-21"}, tmp_path)
    assert second != first
    assert [(path.stat().st_ino, path.read_bytes()) for path in first] == before


@pytest.mark.parametrize("component", [".git", ".codex", ".agents", ".venv"])
def test_protected_output_rejected(tmp_path, component):
    with pytest.raises(ValueError, match="输出目录"):
        cli.publish_score_tracking_report(_report(), tmp_path / component / "reports")
    assert not (tmp_path / component).exists()


def test_symlink_output_rejected(tmp_path):
    target = tmp_path / "real"
    target.mkdir()
    alias = tmp_path / "alias"
    alias.symlink_to(target, target_is_directory=True)
    with pytest.raises(ValueError, match="输出目录"):
        cli.publish_score_tracking_report(_report(), alias)
    assert not list(target.iterdir())


def test_rendering_failure_does_not_publish_partial_json(tmp_path, monkeypatch):
    def fail(_report):
        raise ValueError("invalid report")

    monkeypatch.setattr(cli, "render_score_tracking_html", fail)
    with pytest.raises(ValueError):
        cli.publish_score_tracking_report(_report(), tmp_path)
    assert not list(tmp_path.iterdir())


def test_oversized_html_rejected_before_either_file_is_published(tmp_path, monkeypatch):
    monkeypatch.setattr(cli, "MAX_REPORT_BYTES", 512)
    monkeypatch.setattr(cli, "render_score_tracking_html", lambda _: "x" * 513)
    with pytest.raises(ValueError, match="大小上限"):
        cli.publish_score_tracking_report(_report(), tmp_path)
    assert not list(tmp_path.iterdir())


def test_cli_failure_does_not_claim_success_or_expose_database_details(tmp_path, monkeypatch, capsys):
    def fail(*args, **kwargs):
        raise sqlite3.OperationalError("private database detail")

    monkeypatch.setattr(cli, "evaluate_market_scan_score_tracking", fail)
    assert cli.main(["--database", "missing.db", "--output-directory", str(tmp_path)]) == 2
    captured = capsys.readouterr()
    assert captured.out == ""
    assert json.loads(captured.err) == {"status": "failed", "error_type": "OperationalError"}
    assert not list(tmp_path.iterdir())
