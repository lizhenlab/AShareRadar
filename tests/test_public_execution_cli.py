import json

import pytest

from tools import collect_public_execution_facts as cli


def test_collect_only_selected_anonymous_provider(tmp_path, monkeypatch, capsys):
    calls = []
    monkeypatch.setattr(cli, "collect_public_execution_day", lambda root, day, **kw: calls.append((root, day, kw)) or {"status": "ok"})
    assert cli.main(["--root", str(tmp_path), "collect", "--date", "2026-09-18", "--provider", "baostock"]) == 0
    assert calls == [(tmp_path, "2026-09-18", {"providers": ["baostock"]})]
    assert json.loads(capsys.readouterr().out)["status"] == "ok"


def test_offline_compare_keeps_requested_missing_and_unsupported(tmp_path, monkeypatch, capsys):
    monkeypatch.setattr(cli, "collect_public_execution_day", lambda *_args, **_kw: pytest.fail("offline read started collection"))
    assert cli.main(["--root", str(tmp_path), "compare", "--date", "2026-09-18", "--symbol", "600519.SH", "--symbol", "920001.BJ"]) == 0
    result = json.loads(capsys.readouterr().out)
    assert result["required_pair_count"] == 2
    assert {row["status"] for row in result["rows"]} == {"missing", "unsupported"}
    assert result["official_execution_admitted"] is False
    assert not list(tmp_path.iterdir())


def test_inspect_is_offline_and_bounded_output(tmp_path, capsys):
    assert cli.main(["--root", str(tmp_path), "inspect", "--date", "2026-09-18"]) == 0
    result = json.loads(capsys.readouterr().out)
    assert len(result["sources"]) == 3
    assert all(row["status"] == "missing" and row["row_count"] == 0 for row in result["sources"])


def test_cli_safe_error_and_no_backdating(monkeypatch, capsys):
    def fail(*_args, **_kwargs):
        raise ValueError("do not expose provider private text")
    monkeypatch.setattr(cli, "collect_public_execution_day", fail)
    assert cli.main(["collect"]) == 2
    out = capsys.readouterr()
    assert json.loads(out.err) == {"status": "failed", "error_type": "ValueError"}
    assert out.out == ""
    with pytest.raises(SystemExit):
        cli.main(["collect", "--as-of", "2026-01-01"])


def test_collection_validation_failure_has_nonzero_exit(monkeypatch, capsys):
    monkeypatch.setattr(cli, "collect_public_execution_day", lambda *_a, **_kw: {"sources": [{"status": "invalid_response"}]})
    assert cli.main(["collect", "--date", "2026-09-18"]) == 2
    assert json.loads(capsys.readouterr().out)["sources"][0]["status"] == "invalid_response"
