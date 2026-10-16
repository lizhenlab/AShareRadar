from __future__ import annotations

import hashlib
import json
import os
from pathlib import Path
import sqlite3
import subprocess
import sys

import pytest

from tools import workspace_storage as cli


def _scope(tmp_path: Path, roots: list[dict[str, str]]) -> Path:
    path = tmp_path / "scope.json"
    path.write_text(json.dumps({"schema_version": "workspace-storage-scope-v1", "roots": roots}))
    return path


def _root(path: Path, name: str = "primary", category: str = "database") -> dict[str, str]:
    return {"name": name, "category": category, "path": str(path)}


def _digest_files(root: Path) -> dict[str, str]:
    return {str(p.relative_to(root)): hashlib.sha256(p.read_bytes()).hexdigest()
            for p in root.rglob("*") if p.is_file() and not p.is_symlink()}


@pytest.mark.parametrize("operation", ["inventory", "preview"])
def test_real_cli_inspects_sqlite_and_side_data_without_mutation(tmp_path: Path, operation: str) -> None:
    database = tmp_path / "runtime.sqlite3"
    with sqlite3.connect(database) as conn:
        conn.execute("CREATE TABLE records(value TEXT)")
        conn.execute("INSERT INTO records VALUES ('retained')")
    side = tmp_path / "runtime.fuyao"
    side.mkdir()
    (side / "history.parquet").write_bytes(b"retained history fixture")
    scope = _scope(tmp_path, [_root(database), _root(side, "side", "fuyao")])
    before = _digest_files(tmp_path)

    result = subprocess.run(
        [sys.executable, str(cli.ROOT / "tools/workspace_storage.py"), operation, "--scope", str(scope)],
        capture_output=True, text=True, timeout=15,
        env={**os.environ, "PYTHONDONTWRITEBYTECODE": "1", "PYTHONNOUSERSITE": "1"},
    )

    assert result.returncode == 0, result.stderr
    payload = json.loads(result.stdout)
    assert payload["read_only"] is True
    inventory = payload["inventory"] if operation == "preview" else payload
    assert inventory["status"] == "complete"
    assert inventory["totals"]["file_count"] == 2
    assert inventory["totals"]["logical_bytes"] == database.stat().st_size + (side / "history.parquet").stat().st_size
    assert _digest_files(tmp_path) == before
    if operation == "preview":
        assert payload["backup_created"] is False
        assert payload["restore_verified"] is False
        assert payload["coverage_basis"] == "explicit_roots_only_not_discovered_database_references"
        assert payload["consistency"] == "not_established_live_metadata_observation"
        assert len(payload["pending_backup_requirements"]) == 4


@pytest.mark.parametrize("operation", ["inventory", "preview"])
def test_cli_partial_report_has_distinct_exit_status(tmp_path: Path, capsys, operation: str) -> None:
    scope = _scope(tmp_path, [_root(tmp_path / "missing.sqlite3")])
    assert cli.main([operation, "--scope", str(scope)]) == 3
    payload = json.loads(capsys.readouterr().out)
    assert payload["status"] == "partial"


def test_cli_explicit_budget_is_enforced(tmp_path: Path, capsys) -> None:
    data = tmp_path / "data"
    data.mkdir()
    for number in range(5):
        (data / f"{number}.bin").write_bytes(b"1234")
    scope = _scope(tmp_path, [_root(data, category="research")])
    assert cli.main(["inventory", "--scope", str(scope), "--max-files", "1"]) == 3
    payload = json.loads(capsys.readouterr().out)
    assert payload["status"] == "partial"
    assert payload["totals"]["file_count"] <= 1
    assert payload["budget_exhausted"]


@pytest.mark.parametrize("change", ["unknown_field", "empty", "relative", "duplicate_names", "invalid_category"])
def test_cli_rejects_ambiguous_scope(tmp_path: Path, capsys, change: str) -> None:
    path = tmp_path / "data.bin"
    path.write_bytes(b"data")
    value = {"schema_version": "workspace-storage-scope-v1", "roots": [_root(path)]}
    if change == "unknown_field":
        value["untrusted"] = "PRIVATE-MARKER"
    elif change == "empty":
        value["roots"] = []
    elif change == "relative":
        value["roots"][0]["path"] = "relative/path"
    elif change == "duplicate_names":
        value["roots"].append(_root(path))
    else:
        value["roots"][0]["category"] = "arbitrary"
    scope = tmp_path / "scope.json"
    scope.write_text(json.dumps(value))
    assert cli.main(["inventory", "--scope", str(scope)]) == 2
    streams = capsys.readouterr()
    assert not streams.out
    assert "PRIVATE-MARKER" not in streams.err
    assert json.loads(streams.err)["status"] == "failed"


@pytest.mark.parametrize("body", ['{"schema_version":1,"schema_version":2}', '{"roots":NaN}', 'not json', '"' + 'a' * 65536 + '"'])
def test_cli_scope_read_is_strict_and_bounded(tmp_path: Path, capsys, body: str) -> None:
    path = tmp_path / "scope.json"
    path.write_text(body)
    assert cli.main(["inventory", "--scope", str(path)]) == 2
    assert json.loads(capsys.readouterr().err)["status"] == "failed"


def test_cli_refuses_symlink_scope(tmp_path: Path, capsys) -> None:
    source = _scope(tmp_path, [_root(tmp_path / "database")])
    link = tmp_path / "alias.json"
    link.symlink_to(source)
    assert cli.main(["inventory", "--scope", str(link)]) == 2
    assert json.loads(capsys.readouterr().err)["status"] == "failed"


@pytest.mark.parametrize("arguments", [["--max-seconds", "nan"], ["--max-depth", "-1"], ["--max-files", "0"]])
def test_cli_rejects_invalid_budget(tmp_path: Path, capsys, arguments: list[str]) -> None:
    scope = _scope(tmp_path, [_root(tmp_path)])
    assert cli.main(["inventory", "--scope", str(scope), *arguments]) == 2
    assert json.loads(capsys.readouterr().err)["status"] == "failed"


def test_cli_does_not_expose_internal_error_details(tmp_path: Path, capsys, monkeypatch) -> None:
    scope = _scope(tmp_path, [_root(tmp_path)])

    def unavailable(*_args, **_kwargs):
        raise OSError("PRIVATE-MARKER credential-like error payload")

    monkeypatch.setattr(cli, "inspect_workspace_storage", unavailable)
    assert cli.main(["preview", "--scope", str(scope)]) == 2
    output = capsys.readouterr()
    assert not output.out
    assert "PRIVATE-MARKER" not in output.err
    assert json.loads(output.err)["error_type"] == "OSError"
