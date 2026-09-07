from __future__ import annotations

from pathlib import Path

import pytest

from app.db import market_scan_artifact_paths as paths


@pytest.mark.parametrize("documentation_kind", ["sample", "invalid", "symlink"])
def test_database_restore_ignores_documentation_outside_managed_data(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, documentation_kind: str,
) -> None:
    monkeypatch.setattr(paths, "__file__", str(tmp_path / "app" / "db" / "market_scan_artifact_paths.py"))
    docs = tmp_path / "docs" / "research" / "artifacts"
    docs.parent.mkdir(parents=True)
    if documentation_kind == "symlink":
        docs.symlink_to(tmp_path / "absent", target_is_directory=True)
    else:
        docs.mkdir()
        (docs / "example.json").write_text("{}" if documentation_kind == "sample" else "invalid")

    paths.require_restored_market_scan_artifact_bindings(tmp_path / "data" / "ashare_radar.sqlite3")

    assert docs.exists() or docs.is_symlink()


@pytest.mark.parametrize("managed_kind", ["sample", "invalid", "symlink"])
def test_database_restore_still_blocks_existing_managed_individual_evidence(
    tmp_path: Path, managed_kind: str,
) -> None:
    managed = tmp_path / "data" / "research" / "individual_probability"
    managed.parent.mkdir(parents=True)
    if managed_kind == "symlink":
        managed.symlink_to(tmp_path / "absent", target_is_directory=True)
    else:
        managed.mkdir()
        (managed / "evidence.json").write_text("{}" if managed_kind == "sample" else "invalid")

    with pytest.raises(paths.ManagedArtifactPathError):
        paths.require_restored_market_scan_artifact_bindings(tmp_path / "data" / "ashare_radar.sqlite3")

    assert managed.exists() or managed.is_symlink()
