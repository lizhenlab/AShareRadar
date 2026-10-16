"""Storage previews observe bounded metadata without following links or writing."""

from __future__ import annotations

import os
from contextlib import contextmanager
from pathlib import Path
import sqlite3
import sys
from types import SimpleNamespace
from typing import Any

import pytest
from pydantic import ValidationError

from app.models.workspace_storage import WorkspaceStorageBudget, WorkspaceStorageRoot
from app.services import workspace_storage as storage


def _root(path: Path, name: str = "data", category: str = "research") -> WorkspaceStorageRoot:
    return WorkspaceStorageRoot.model_validate({"name": name, "category": category, "path": path})


def test_hardlinks_and_overlapping_roots_have_separate_logical_and_unique_sizes(tmp_path: Path) -> None:
    original = tmp_path / "original.bin"
    original.write_bytes(b"example")
    os.link(original, tmp_path / "linked.bin")

    report = storage.inspect_workspace_storage([_root(tmp_path), _root(original, "overlap")])

    assert report.status == "complete"
    assert report.totals.logical_bytes == 21
    assert report.totals.unique_bytes == 7
    assert report.totals.file_count == 3
    assert report.totals.unique_file_count == 1
    assert report.totals.duplicate_hardlink_count == 1
    assert report.totals.overlap_count == 1
    assert report.totals.allocated_bytes == original.stat().st_blocks * 512
    assert [root.counts.unique_bytes for root in report.roots] == [7, 7]
    assert [root.counts.file_count for root in report.roots] == [2, 1]


def test_cross_root_hardlink_is_distinct_from_overlapping_path(tmp_path: Path) -> None:
    first = tmp_path / "first"
    second = tmp_path / "second"
    first.write_bytes(b"1234")
    os.link(first, second)

    report = storage.inspect_workspace_storage([_root(first), _root(second, "second")])

    assert report.totals.unique_bytes == 4 and report.totals.logical_bytes == 8
    assert report.totals.duplicate_hardlink_count == 1 and report.totals.overlap_count == 0


def test_sparse_file_uses_allocated_blocks_without_reading_content(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    path = tmp_path / "sparse.sqlite3"
    with path.open("wb") as stream:
        stream.truncate(16 * 1024 * 1024)
    facts = path.stat()
    original_open = os.open

    def directories_only(name: Any, flags: int, *args: Any, **kwargs: Any) -> int:
        assert flags & os.O_DIRECTORY, "inspection opened ordinary file contents"
        return original_open(name, flags, *args, **kwargs)

    def no_database(*_args: Any, **_kwargs: Any) -> None:
        pytest.fail("storage inspection opened a database connection")

    with monkeypatch.context() as scoped:
        scoped.setattr(storage.os, "open", directories_only)
        scoped.setattr(sqlite3, "connect", no_database)
        report = storage.inspect_workspace_storage([_root(path, category="database")])

    assert report.status == "complete"
    assert report.totals.logical_bytes == facts.st_size
    assert report.totals.allocated_bytes == facts.st_blocks * 512
    assert report.totals.allocated_bytes < report.totals.logical_bytes
    assert path.stat().st_size == facts.st_size


def test_links_and_unauthorized_ancestor_are_never_followed(tmp_path: Path) -> None:
    outside = tmp_path / "outside"
    outside.mkdir()
    (outside / "large").write_bytes(b"x" * 100)
    scoped = tmp_path / "scope"
    scoped.mkdir()
    (scoped / "external").symlink_to(outside, target_is_directory=True)
    (scoped / "cycle").symlink_to(scoped, target_is_directory=True)
    (scoped / "broken").symlink_to(tmp_path / "missing")

    report = storage.inspect_workspace_storage([
        _root(scoped), _root(scoped / "external" / "large", "ancestor"), _root(scoped / "external", "root-link"),
    ])

    assert report.status == "partial"
    assert report.totals.logical_bytes == 0
    assert report.totals.symlink_count == 5
    assert all(root.status == "partial" and "symlink" in root.incomplete_reasons for root in report.roots)
    assert (outside / "large").read_bytes() == b"x" * 100


def test_default_exclusions_and_explicit_backup_root(tmp_path: Path) -> None:
    for name in (".env", ".env.local", "fuyao-api-key", "credentials.json", "private.pem", "run.log", "run.log.1", "state.lock"):
        (tmp_path / name).write_bytes(b"private")
    for name in ("logs", "locks", "backups", ".venv"):
        folder = tmp_path / name
        folder.mkdir()
        (folder / "payload").write_bytes(b"excluded")
    (tmp_path / "evidence.json").write_bytes(b"{}")

    report = storage.inspect_workspace_storage([_root(tmp_path)])

    assert report.status == "complete"
    assert report.totals.logical_bytes == 2 and report.totals.excluded_count == 12
    assert {issue.reason for issue in report.roots[0].issues} == {
        "secret_configuration", "log", "lock", "nested_backup", "production_environment",
    }
    backups = tmp_path / "backups"
    (backups / "backups").mkdir()
    (backups / "backups" / "recursive").write_bytes(b"nested")
    included = storage.inspect_workspace_storage([_root(backups, category="backup")])
    assert included.totals.logical_bytes == len(b"excluded")
    assert included.totals.excluded_count == 1


@pytest.mark.parametrize("ancestor,reason", [
    ("secrets", "secret_configuration"), (".aws", "secret_configuration"),
    ("credentials.json", "secret_configuration"), ("logs", "log"),
    ("service.log.1", "log"), ("locks", "lock"), ("backups", "nested_backup"),
])
@pytest.mark.parametrize("category", ["research", "runtime", "backup"])
def test_explicit_roots_cannot_bypass_ancestor_exclusions(
    tmp_path: Path, ancestor: str, reason: str, category: str,
) -> None:
    folder = tmp_path / ancestor
    folder.mkdir()
    payload = folder / "payload.bin"
    payload.write_bytes(b"excluded")

    report = storage.inspect_workspace_storage([_root(payload, category=category)])

    assert report.status == "partial" and report.totals.logical_bytes == 0
    assert report.totals.excluded_count == 1 and report.files_inspected == 0
    assert report.roots[0].incomplete_reasons == [reason]


def test_symlink_loop_root_is_disclosed_without_resolving_target(tmp_path: Path) -> None:
    link = tmp_path / "loop"
    link.symlink_to(link)

    report = storage.inspect_workspace_storage([_root(link, category="runtime")])

    assert report.status == "partial" and report.totals.logical_bytes == 0
    assert report.totals.symlink_count == 1
    assert report.roots[0].incomplete_reasons == ["runtime_symlink_dependency"]


def test_runtime_environment_includes_files_but_discloses_link_dependency(tmp_path: Path) -> None:
    environment = tmp_path / ".venv"
    environment.mkdir()
    (environment / "package.py").write_bytes(b"source")
    (environment / "python").symlink_to(Path(sys.executable))

    ordinary = storage.inspect_workspace_storage([_root(environment)])
    runtime = storage.inspect_workspace_storage([_root(environment, category="runtime")])

    assert ordinary.status == "partial" and ordinary.totals.excluded_count == 1
    assert ordinary.totals.logical_bytes == 0
    assert runtime.status == "partial" and runtime.totals.logical_bytes == len(b"source")
    assert runtime.totals.symlink_count == 1
    assert "runtime_symlink_dependency" in runtime.roots[0].incomplete_reasons


def test_only_named_tool_caches_are_excluded_and_runtime_keeps_them(tmp_path: Path) -> None:
    for name in ("__pycache__", ".pytest_cache", ".mypy_cache", ".ruff_cache", "cache"):
        directory = tmp_path / name
        directory.mkdir()
        (directory / "payload").write_bytes(b"x")

    ordinary = storage.inspect_workspace_storage([_root(tmp_path)])
    runtime = storage.inspect_workspace_storage([_root(tmp_path, category="runtime")])

    assert ordinary.totals.logical_bytes == 1 and ordinary.totals.excluded_count == 4
    assert runtime.totals.logical_bytes == 5 and runtime.totals.excluded_count == 0


def test_observation_window_has_separate_utc_start_and_finish(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    times = iter(["2026-10-15T01:00:00.000000Z", "2026-10-15T01:00:02.000000Z"])
    monkeypatch.setattr(storage, "audit_now_text", lambda: next(times))

    report = storage.inspect_workspace_storage([_root(tmp_path)])

    assert report.started_at == "2026-10-15T01:00:00.000000Z"
    assert report.finished_at == "2026-10-15T01:00:02.000000Z"


def test_empty_directory_and_missing_root_remain_distinct(tmp_path: Path) -> None:
    empty = tmp_path / "empty"
    empty.mkdir()

    report = storage.inspect_workspace_storage([_root(empty), _root(tmp_path / "missing", "missing")])

    assert report.roots[0].status == "complete" and report.roots[0].counts.directory_count == 1
    assert report.roots[1].status == "missing" and report.roots[1].incomplete_reasons == ["missing"]
    assert report.totals.file_count == report.totals.logical_bytes == 0
    assert report.status == "partial"


@pytest.mark.parametrize("budget,reason", [
    ({"max_entries": 2}, "max_entries"), ({"max_files": 1}, "max_files"),
])
def test_excluded_entries_still_exhaust_budget(tmp_path: Path, budget: dict[str, int], reason: str) -> None:
    for index in range(10):
        (tmp_path / f".env.{index}").write_bytes(b"secret")

    report = storage.inspect_workspace_storage([_root(tmp_path)], WorkspaceStorageBudget(**budget))

    assert report.status == "partial" and report.budget_exhausted == [reason]
    assert report.totals.logical_bytes == 0 and report.totals.excluded_count == 1
    assert reason in report.roots[0].incomplete_reasons


@pytest.mark.parametrize("budget,reason", [
    ({"max_directories": 1}, "max_directories"), ({"max_depth": 0}, "max_depth"),
])
def test_directory_and_depth_budgets_are_reported(tmp_path: Path, budget: dict[str, int], reason: str) -> None:
    child = tmp_path / "child"
    child.mkdir()
    (child / "unseen").write_bytes(b"unseen")

    report = storage.inspect_workspace_storage([_root(tmp_path)], WorkspaceStorageBudget(**budget))

    assert report.status == "partial" and report.budget_exhausted == [reason]
    assert report.totals.file_count == 0
    assert reason in report.roots[0].incomplete_reasons


def test_time_budget_marks_all_uninspected_roots(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    ticks = iter([0.0, 2.0, 2.0, 2.0])
    monkeypatch.setattr(storage, "monotonic_now", lambda: next(ticks, 2.0))

    report = storage.inspect_workspace_storage(
        [_root(tmp_path), _root(tmp_path / "second", "second")], WorkspaceStorageBudget(max_seconds=1),
    )

    assert report.budget_exhausted == ["max_seconds"]
    assert all(root.status == "partial" and root.incomplete_reasons == ["max_seconds"] for root in report.roots)
    assert report.entries_inspected == 0


def test_permission_failure_is_partial_and_does_not_leak_os_error(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    def denied(_descriptor: int) -> Any:
        raise PermissionError("DO_NOT_EXPOSE_PRIVATE_DETAIL")

    monkeypatch.setattr(storage.os, "scandir", denied)
    report = storage.inspect_workspace_storage([_root(tmp_path)])

    assert report.status == "partial" and report.totals.error_count == 1
    assert report.roots[0].incomplete_reasons == ["permission_denied"]
    assert "DO_NOT_EXPOSE_PRIVATE_DETAIL" not in report.model_dump_json()


def test_deleted_entry_is_reported_as_race(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    disappearing = tmp_path / "disappearing"
    disappearing.write_bytes(b"old")
    original_stat = os.stat

    def stat_after_delete(path: Any, *args: Any, **kwargs: Any) -> os.stat_result:
        if path == "disappearing" and kwargs.get("dir_fd") is not None:
            disappearing.unlink(missing_ok=True)
        return original_stat(path, *args, **kwargs)

    monkeypatch.setattr(storage.os, "stat", stat_after_delete)
    report = storage.inspect_workspace_storage([_root(tmp_path)])

    assert report.status == "partial" and report.totals.logical_bytes == 0
    assert "vanished" in report.roots[0].incomplete_reasons


def test_directory_changed_after_enumeration_is_partial(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    original_walk = storage._walk_directory

    def changed(*args: Any) -> None:
        original_walk(*args)
        (tmp_path / "late").write_bytes(b"after scan")

    monkeypatch.setattr(storage, "_walk_directory", changed)
    report = storage.inspect_workspace_storage([_root(tmp_path)])

    assert report.status == "partial" and "changed" in report.roots[0].incomplete_reasons
    assert report.totals.logical_bytes == 0


def test_file_changed_between_stat_observations_is_not_counted(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    path = tmp_path / "changing"
    path.write_bytes(b"old")
    original_stat = os.stat
    calls = 0

    def changing_stat(name: Any, *args: Any, **kwargs: Any) -> os.stat_result:
        nonlocal calls
        if name == "changing" and kwargs.get("dir_fd") is not None:
            calls += 1
            if calls == 2:
                path.write_bytes(b"new content")
        return original_stat(name, *args, **kwargs)

    monkeypatch.setattr(storage.os, "stat", changing_stat)
    report = storage.inspect_workspace_storage([_root(tmp_path)])

    assert report.status == "partial" and report.totals.logical_bytes == 0
    assert "changed" in report.roots[0].incomplete_reasons


def test_changed_hardlink_metadata_is_not_silently_deduplicated(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    original = tmp_path / "first"
    original.write_bytes(b"old")
    os.link(original, tmp_path / "second")
    original_inspect = storage._inspect_file
    visits = 0

    def mutate_after_first(*args: Any) -> None:
        nonlocal visits
        original_inspect(*args)
        visits += 1
        if visits == 1:
            original.write_bytes(b"new bytes")

    monkeypatch.setattr(storage, "_inspect_file", mutate_after_first)
    report = storage.inspect_workspace_storage([_root(tmp_path)])

    assert report.status == "partial" and report.totals.logical_bytes == 3
    assert report.totals.unique_bytes == 3 and report.totals.file_count == 1
    assert report.roots[0].incomplete_reasons == ["inode_changed"]


def test_inode_changes_between_individual_roots_make_report_partial(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    original, linked = tmp_path / "first", tmp_path / "second"
    original.write_bytes(b"old")
    os.link(original, linked)
    original_inspect = storage._inspect_root

    def mutate_between_roots(root: Any, run: Any) -> Any:
        result = original_inspect(root, run)
        if root.name == "first":
            original.write_bytes(b"new bytes")
        return result

    monkeypatch.setattr(storage, "_inspect_root", mutate_between_roots)
    report = storage.inspect_workspace_storage([_root(original, "first"), _root(linked, "second")])

    assert report.status == "partial" and report.roots[0].status == "complete"
    assert report.roots[1].incomplete_reasons == ["inode_changed"]
    assert report.totals.logical_bytes == report.totals.unique_bytes == 3


def test_replaced_file_between_overlapping_roots_is_partial(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    path = tmp_path / "target"
    path.write_bytes(b"old")
    original_inspect = storage._inspect_root

    def replace_between_roots(root: Any, run: Any) -> Any:
        result = original_inspect(root, run)
        if root.name == "first":
            path.rename(tmp_path / "old-target")
            path.write_bytes(b"replacement content")
        return result

    monkeypatch.setattr(storage, "_inspect_root", replace_between_roots)
    report = storage.inspect_workspace_storage([_root(path, "first"), _root(path, "second")])

    assert report.status == "partial" and report.roots[0].status == "complete"
    assert report.roots[1].incomplete_reasons == ["path_changed"]
    assert report.totals.logical_bytes == report.totals.unique_bytes == 3


@pytest.mark.parametrize("root_kind", ["file", "directory"])
@pytest.mark.parametrize("replacement", ["directory", "symlink", "missing"])
def test_root_path_is_rechecked_after_ancestor_replacement(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, root_kind: str, replacement: str,
) -> None:
    ancestor, outside = tmp_path / "ancestor", tmp_path / "outside"
    for parent, payload in ((ancestor, b"old"), (outside, b"replacement content")):
        parent.mkdir()
        target = parent / "target"
        if root_kind == "directory":
            target.mkdir()
            target = target / "item"
        target.write_bytes(payload)
    original_visit = storage._visit

    def replace_before_visit(*args: Any, **kwargs: Any) -> Any:
        if kwargs.get("is_root"):
            ancestor.rename(tmp_path / "moved")
            if replacement == "directory":
                outside.rename(ancestor)
            elif replacement == "symlink":
                ancestor.symlink_to(outside, target_is_directory=True)
        return original_visit(*args, **kwargs)

    monkeypatch.setattr(storage, "_visit", replace_before_visit)
    report = storage.inspect_workspace_storage([_root(ancestor / "target")])

    assert report.status == "partial" and report.totals.logical_bytes == 3
    reason = "symlink" if replacement == "symlink" else "root_path_changed"
    assert reason in report.roots[0].incomplete_reasons


def test_ancestor_swapped_to_link_after_admission_is_not_followed(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    ancestor = tmp_path / "ancestor"
    ancestor.mkdir()
    (ancestor / "payload").write_bytes(b"original")
    outside = tmp_path / "outside"
    outside.mkdir()
    (outside / "payload").write_bytes(b"untrusted")
    original_admission = storage.admitted_lexical_path

    def swap_after_admission(path: Path) -> Path | None:
        admitted = original_admission(path)
        ancestor.rename(tmp_path / "moved")
        ancestor.symlink_to(outside, target_is_directory=True)
        return admitted

    monkeypatch.setattr(storage, "admitted_lexical_path", swap_after_admission)
    report = storage.inspect_workspace_storage([_root(ancestor / "payload")])

    assert report.status == "partial" and report.totals.logical_bytes == 0
    assert report.totals.error_count == 1


def test_unknown_allocation_does_not_mark_later_known_file_unavailable(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    for name in ("a-unknown", "b-known"):
        (tmp_path / name).write_bytes(b"data")
    original_stat, original_scandir = os.stat, os.scandir

    def partly_known_stat(name: Any, *args: Any, **kwargs: Any) -> Any:
        facts = original_stat(name, *args, **kwargs)
        if name != "a-unknown":
            return facts
        fields = ("st_dev", "st_ino", "st_mode", "st_size", "st_mtime_ns", "st_ctime_ns")
        return SimpleNamespace(**{field: getattr(facts, field) for field in fields})

    @contextmanager
    def ordered_entries(descriptor: int):
        with original_scandir(descriptor) as entries:
            yield iter(sorted(entries, key=lambda item: item.name))

    monkeypatch.setattr(storage.os, "stat", partly_known_stat)
    monkeypatch.setattr(storage.os, "scandir", ordered_entries)
    report = storage.inspect_workspace_storage([_root(tmp_path)])

    assert report.totals.logical_bytes == 8 and not report.totals.allocated_bytes_complete
    issues = [issue for issue in report.roots[0].issues if issue.reason == "allocation_unavailable"]
    assert len(issues) == 1 and issues[0].path.endswith("a-unknown")


def test_issue_samples_are_bounded_without_hiding_missing_coverage(tmp_path: Path) -> None:
    for index in range(3):
        (tmp_path / f"link-{index}").symlink_to(tmp_path / "absent")

    report = storage.inspect_workspace_storage([_root(tmp_path)], WorkspaceStorageBudget(max_issues=1))

    assert report.totals.symlink_count == 3 and report.status == "partial"
    assert len(report.roots[0].issues) == 1 and report.roots[0].issues_truncated == 2
    assert report.roots[0].incomplete_reasons == ["symlink"]


def test_fifo_is_not_opened(tmp_path: Path) -> None:
    os.mkfifo(tmp_path / "pipe")
    report = storage.inspect_workspace_storage([_root(tmp_path)])
    assert report.status == "partial" and report.totals.file_count == 0
    assert report.roots[0].incomplete_reasons == ["unsupported_type"]


@pytest.mark.skipif(sys.platform != "darwin", reason="Darwin trusted system aliases")
def test_mac_system_alias_and_canonical_path_are_deduplicated(tmp_path: Path) -> None:
    path = tmp_path / "payload"
    path.write_bytes(b"alias")
    canonical = path.resolve()
    alias = Path(str(canonical).removeprefix("/private"))
    if not str(alias).startswith(("/var/", "/tmp/")):
        pytest.skip("temporary directory does not use a supported system alias")

    report = storage.inspect_workspace_storage([_root(alias), _root(canonical, "canonical")])

    assert report.status == "complete"
    assert report.totals.unique_bytes == 5 and report.totals.overlap_count == 1


@pytest.mark.parametrize("payload", [
    {"name": "x", "category": "research", "path": "relative"},
    {"name": "x", "category": "research", "path": "/tmp/../secret"},
    {"name": "", "category": "research", "path": "/tmp"},
    {"name": "x", "category": "unknown", "path": "/tmp"},
    {"name": "x", "category": "research", "path": "/tmp", "unexpected": True},
])
def test_scope_rejects_ambiguous_inputs(payload: dict[str, Any]) -> None:
    with pytest.raises(ValidationError):
        WorkspaceStorageRoot.model_validate(payload)


def test_empty_and_duplicate_named_scopes_are_rejected(tmp_path: Path) -> None:
    with pytest.raises(ValueError, match="explicit storage roots"):
        storage.inspect_workspace_storage([])
    with pytest.raises(ValueError, match="unique"):
        storage.inspect_workspace_storage([_root(tmp_path), _root(tmp_path / "child")])
