from __future__ import annotations

from pathlib import Path
from types import TracebackType
from typing import Any

import pytest

from app.services import research_artifact_catalog as catalog


def test_catalog_handles_memory_missing_and_non_directory_roots_without_writes(tmp_path: Path) -> None:
    assert catalog.research_artifact_storage(Path(":memory:")).root_path is None

    missing_root = tmp_path / "missing" / "cache.sqlite3"
    missing = catalog.research_artifact_storage(missing_root)
    assert missing.scan_status == "empty"
    assert missing.regular_file_count == 0
    assert not missing_root.parent.exists()

    root_file = tmp_path / "root-file"
    root_file.write_text("not a directory", encoding="utf-8")
    blocked = catalog.research_artifact_storage(root_file / "cache.sqlite3")
    assert blocked.scan_status == "ok_with_ignored_entries"
    assert blocked.ignored_non_regular_count == len(catalog.KNOWN_RESEARCH_ARTIFACT_DIRECTORIES)
    assert {item.scan_status for item in blocked.categories} == {"not_directory"}


def test_catalog_counts_manual_and_protected_files_but_never_offers_automatic_deletion(
    tmp_path: Path,
) -> None:
    probability = tmp_path / "market-scan-probability"
    probability.mkdir()
    (probability / "projection.json").write_bytes(b"projection")
    (probability / "nested").mkdir()
    (probability / "ignored-link").symlink_to(probability / "projection.json")
    history = tmp_path / "research" / "market_scan_probability_history"
    history.mkdir(parents=True)
    (history / "history.sqlite3").write_bytes(b"history")
    outcomes = tmp_path / "research" / "market_scan_probability_outcomes"
    outcomes.mkdir(parents=True)
    (outcomes / "outcomes.json.gz").write_bytes(b"outcomes")
    fit = tmp_path / "research" / "market_scan_probability_fit"
    fit.mkdir(parents=True)
    (fit / "fit.json.gz").write_bytes(b"fit")
    (tmp_path / "research" / "ordinary-report.json").write_bytes(b"report")

    result = catalog.research_artifact_storage(tmp_path / "cache.sqlite3")

    assert result.scan_status == "ok_with_ignored_entries"
    assert result.regular_file_count == 5
    assert result.ignored_symlink_count == 1
    assert result.ignored_non_regular_count == 1
    preview = result.retention_preview
    assert preview.automatic_deletion_allowed is False
    assert preview.manual_review_file_count == 2
    assert preview.protected_evidence_file_count == 3
    assert "唯一的时点" in " ".join(preview.blocked_reasons)
    assert "符号链接" in " ".join(preview.blocked_reasons)
    assert preview.summary.startswith("共识别 5 个")


def test_catalog_reports_child_non_directory_and_root_metadata_failure(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    research = tmp_path / "research"
    research.write_text("not a directory", encoding="utf-8")

    result = catalog.research_artifact_storage(tmp_path / "cache.sqlite3")
    nested = [item for item in result.categories if len(Path(item.relative_path).parts) > 1]
    assert nested
    assert {item.scan_status for item in nested} == {"not_directory"}

    original_lstat = Path.lstat

    def fail_root_lstat(path: Path) -> Any:
        if path == tmp_path:
            raise OSError("metadata unavailable")
        return original_lstat(path)

    monkeypatch.setattr(Path, "lstat", fail_root_lstat)
    unreadable = catalog.research_artifact_storage(tmp_path / "cache.sqlite3")
    assert unreadable.scan_status == "partial"
    assert unreadable.scan_error_count == len(catalog.KNOWN_RESEARCH_ARTIFACT_DIRECTORIES)
    assert {item.scan_status for item in unreadable.categories} == {"unreadable"}


def test_catalog_translates_root_and_child_open_failures_to_partial_diagnostics(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    original_open = catalog.os.open

    def fail_root_open(path: str | bytes | Path, flags: int, *args: object, **kwargs: object) -> int:
        if isinstance(path, Path):
            raise OSError("root open failed")
        return original_open(path, flags, *args, **kwargs)

    monkeypatch.setattr(catalog.os, "open", fail_root_open)
    root_failure = catalog.research_artifact_storage(tmp_path / "cache.sqlite3")
    assert root_failure.scan_status == "partial"
    assert root_failure.scan_error_count == len(catalog.KNOWN_RESEARCH_ARTIFACT_DIRECTORIES)

    monkeypatch.setattr(catalog.os, "open", original_open)
    (tmp_path / "market-scan-probability").mkdir()

    def fail_child_open(path: str | bytes | Path, flags: int, *args: object, **kwargs: object) -> int:
        if path == "market-scan-probability":
            raise OSError("child open failed")
        return original_open(path, flags, *args, **kwargs)

    monkeypatch.setattr(catalog.os, "open", fail_child_open)
    child_failure = catalog.research_artifact_storage(tmp_path / "cache.sqlite3")
    probability = next(item for item in child_failure.categories if item.category == "probability_projection")
    assert probability.scan_status == "unreadable"
    assert probability.scan_error_count == 1


def test_catalog_translates_child_stat_failure_to_partial_diagnostics(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    (tmp_path / "market-scan-probability").mkdir()
    original_stat = catalog.os.stat

    def fail_child_stat(path: str | bytes | Path, *args: object, **kwargs: object) -> Any:
        if path == "market-scan-probability":
            raise OSError("child stat failed")
        return original_stat(path, *args, **kwargs)

    monkeypatch.setattr(catalog.os, "stat", fail_child_stat)
    result = catalog.research_artifact_storage(tmp_path / "cache.sqlite3")

    probability = next(item for item in result.categories if item.category == "probability_projection")
    assert result.scan_status == "partial"
    assert probability.scan_status == "unreadable"
    assert probability.scan_error_count == 1


class _BrokenEntry:
    name = "unreadable.json"

    def stat(self, *, follow_symlinks: bool) -> Any:
        assert follow_symlinks is False
        raise OSError("entry disappeared")


class _Entries:
    def __enter__(self) -> list[_BrokenEntry]:
        return [_BrokenEntry()]

    def __exit__(
        self,
        exc_type: type[BaseException] | None,
        exc: BaseException | None,
        traceback: TracebackType | None,
    ) -> None:
        del exc_type, exc, traceback


def test_catalog_keeps_scanning_contract_when_entry_or_directory_listing_fails(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    (tmp_path / "market-scan-probability").mkdir()
    monkeypatch.setattr(catalog.os, "scandir", lambda _descriptor: _Entries())

    entry_failure = catalog.research_artifact_storage(tmp_path / "cache.sqlite3")
    probability = next(item for item in entry_failure.categories if item.category == "probability_projection")
    assert probability.scan_status == "partial"
    assert probability.scan_error_count == 1

    def fail_scandir(_descriptor: int) -> Any:
        raise OSError("directory listing failed")

    monkeypatch.setattr(catalog.os, "scandir", fail_scandir)
    directory_failure = catalog.research_artifact_storage(tmp_path / "cache.sqlite3")
    probability = next(item for item in directory_failure.categories if item.category == "probability_projection")
    assert probability.scan_status == "partial"
    assert probability.scan_error_count == 1
