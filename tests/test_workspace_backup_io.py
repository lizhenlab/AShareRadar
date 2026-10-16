from __future__ import annotations

import errno
import hashlib
import os
from pathlib import Path
import stat

import pytest

from app.services import workspace_backup_io as io
from app.services.workspace_backup_io import (
    BackupBudget, BackupIOError, copy_regular, create_relative_directories, hash_regular, owned_destination,
    read_regular_prefix, safe_absolute_path, walk_regular,
)


def test_copy_hash_and_walk_share_a_cumulative_budget(tmp_path: Path) -> None:
    root = tmp_path / "source"
    (root / "nested").mkdir(parents=True)
    source = root / "nested" / "script"
    source.write_bytes(b"hello")
    source.chmod(0o751)
    budget = BackupBudget(max_files=3, max_bytes=10, max_file_bytes=5)
    entries = list(walk_regular(root, budget))
    assert entries == [(Path("nested"), root / "nested", True), (Path("nested/script"), source, False)]
    target = tmp_path / "copy"
    facts = copy_regular(source, target, budget)
    assert facts == {"size_bytes": 5, "sha256": hashlib.sha256(b"hello").hexdigest(), "mode": 0o751}
    assert hash_regular(target, budget) == facts
    assert target.read_bytes() == b"hello"
    assert stat.S_IMODE(target.stat().st_mode) == 0o751
    assert (budget.entries, budget.files, budget.bytes) == (3, 2, 10)


@pytest.mark.parametrize("value", ["relative", "../relative", "/tmp/../escape", "/tmp/\x00bad"])
def test_paths_must_be_absolute_and_without_parent_traversal(value: str) -> None:
    with pytest.raises(BackupIOError):
        safe_absolute_path(value)


@pytest.mark.parametrize("limit,value", [("max_files", -1), ("max_bytes", True), ("max_file_bytes", 1.5), ("max_depth", 257),
                                         ("max_seconds", float("inf")), ("max_seconds", 0)])
def test_invalid_budgets_are_rejected(limit: str, value: object) -> None:
    with pytest.raises(BackupIOError):
        BackupBudget(**{limit: value})


def test_darwin_trusted_tmp_alias_is_accepted() -> None:
    assert safe_absolute_path(Path("/tmp")) == Path("/tmp").resolve()


@pytest.mark.parametrize("kind", ["leaf", "ancestor", "loop", "fifo"])
def test_sources_reject_symlinks_and_special_files(tmp_path: Path, kind: str) -> None:
    outside = tmp_path / "outside"
    outside.mkdir()
    source = outside / "data"
    source.write_bytes(b"outside")
    candidate = tmp_path / "candidate"
    if kind == "leaf":
        candidate.symlink_to(source)
    elif kind == "ancestor":
        candidate.symlink_to(outside, target_is_directory=True)
        candidate /= "data"
    elif kind == "loop":
        candidate.symlink_to(candidate)
    else:
        os.mkfifo(candidate)
    with pytest.raises(BackupIOError):
        hash_regular(candidate, BackupBudget())
    assert source.read_bytes() == b"outside"


@pytest.mark.parametrize("existing", ["file", "directory", "symlink"])
def test_owned_destination_never_overwrites_existing_target(tmp_path: Path, existing: str) -> None:
    target = tmp_path / "target"
    if existing == "file":
        target.write_bytes(b"winner")
    elif existing == "directory":
        target.mkdir()
    else:
        target.symlink_to(tmp_path / "missing")
    before = target.lstat()
    with pytest.raises(BackupIOError), owned_destination(target):
        pytest.fail("existing target admitted")
    assert target.lstat().st_ino == before.st_ino


def test_copy_never_overwrites_existing_file(tmp_path: Path) -> None:
    source, target = tmp_path / "source", tmp_path / "target"
    source.write_bytes(b"source")
    target.write_bytes(b"winner")
    with pytest.raises(BackupIOError):
        copy_regular(source, target, BackupBudget())
    assert target.read_bytes() == b"winner"


@pytest.mark.parametrize("failure", [KeyboardInterrupt, SystemExit, RuntimeError])
def test_owned_destination_cleans_owned_contents_on_failure(tmp_path: Path, failure: type[BaseException]) -> None:
    target, outside = tmp_path / "target", tmp_path / "outside"
    outside.write_bytes(b"keep")
    with pytest.raises(failure), owned_destination(target) as owned:
        (owned / "nested").mkdir()
        (owned / "nested" / "data").write_bytes(b"partial")
        (owned / "link").symlink_to(outside)
        raise failure("cancel")
    assert not target.exists()
    assert outside.read_bytes() == b"keep"


def test_owned_destination_commits_only_after_body_returns(tmp_path: Path) -> None:
    target = tmp_path / "target"
    with owned_destination(target) as owned:
        assert owned.exists()
        assert stat.S_IMODE(owned.stat().st_mode) == 0o700
        assert not (owned / "manifest.json").exists()
        (owned / "manifest.json").write_text("complete")
    assert (target / "manifest.json").read_text() == "complete"


def test_ancestor_rename_cleans_original_owned_directory(tmp_path: Path) -> None:
    parent, moved = tmp_path / "parent", tmp_path / "moved"
    parent.mkdir()
    with pytest.raises(BackupIOError), owned_destination(parent / "target") as owned:
        (owned / "data").write_bytes(b"owned")
        parent.rename(moved)
        parent.mkdir()
        (parent / "target").mkdir()
        (parent / "target" / "winner").write_bytes(b"keep")
    assert not (moved / "target").exists()
    assert (parent / "target" / "winner").read_bytes() == b"keep"


def test_replaced_destination_is_never_deleted_on_cancel(tmp_path: Path) -> None:
    target, moved = tmp_path / "target", tmp_path / "moved"
    with pytest.raises(KeyboardInterrupt), owned_destination(target):
        target.rename(moved)
        target.mkdir()
        (target / "winner").write_bytes(b"keep")
        raise KeyboardInterrupt
    assert (target / "winner").read_bytes() == b"keep"
    assert moved.is_dir()


@pytest.mark.parametrize("kind", ["cancel_write", "enospc", "zero_write", "cancel_fsync"])
def test_partial_copy_is_cleaned_on_write_and_sync_failures(tmp_path: Path, monkeypatch: pytest.MonkeyPatch, kind: str) -> None:
    source, target = tmp_path / "source", tmp_path / "target"
    source.write_bytes(b"payload")

    def fail(*_args: object) -> int:
        if kind in {"cancel_write", "cancel_fsync"}:
            raise KeyboardInterrupt
        if kind == "zero_write":
            return 0
        raise OSError(errno.ENOSPC, "full")

    monkeypatch.setattr(io.os, "fsync" if kind == "cancel_fsync" else "write", fail)
    expected = KeyboardInterrupt if kind.startswith("cancel") else BackupIOError
    with pytest.raises(expected):
        copy_regular(source, target, BackupBudget())
    assert not target.exists()


def test_copy_handles_short_writes(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    source, target = tmp_path / "source", tmp_path / "target"
    source.write_bytes(b"payload")
    original = io.os.write
    monkeypatch.setattr(io.os, "write", lambda descriptor, chunk: original(descriptor, chunk[:2]))
    copy_regular(source, target, BackupBudget())
    assert target.read_bytes() == b"payload"


@pytest.mark.parametrize("limit,maximum,error", [("max_bytes", 4, "max_bytes"), ("max_file_bytes", 4, "max_file_bytes"),
                                                ("max_files", 0, "max_files")])
def test_copy_respects_admission_budgets(tmp_path: Path, limit: str, maximum: int, error: str) -> None:
    source, target = tmp_path / "source", tmp_path / "target"
    source.write_bytes(b"hello")
    with pytest.raises(BackupIOError, match=error):
        copy_regular(source, target, BackupBudget(**{limit: maximum}))
    assert not target.exists()


def test_total_bytes_accumulate_across_files(tmp_path: Path) -> None:
    source = tmp_path / "source"
    source.write_bytes(b"123")
    budget = BackupBudget(max_bytes=5)
    hash_regular(source, budget)
    with pytest.raises(BackupIOError, match="max_bytes"):
        hash_regular(source, budget)
    assert budget.bytes == 3


@pytest.mark.parametrize("change", ["grow", "truncate", "replace", "ancestor", "same_size"])
def test_stream_rejects_source_changes(tmp_path: Path, monkeypatch: pytest.MonkeyPatch, change: str) -> None:
    parent = tmp_path / "parent"
    parent.mkdir()
    source, target = parent / "source", tmp_path / "target"
    source.write_bytes(b"payload")
    original = io.os.read
    changed = False

    def mutate(descriptor: int, count: int) -> bytes:
        nonlocal changed
        chunk = original(descriptor, count)
        if not changed:
            changed = True
            if change == "grow":
                with source.open("ab") as stream:
                    stream.write(b"longer")
            elif change == "truncate":
                source.write_bytes(b"")
            elif change == "replace":
                source.rename(parent / "old")
                source.write_bytes(b"payload")
            elif change == "ancestor":
                parent.rename(tmp_path / "moved")
                parent.mkdir()
                source.write_bytes(b"payload")
            else:
                source.write_bytes(b"changed")
        return chunk

    monkeypatch.setattr(io.os, "read", mutate)
    with pytest.raises(BackupIOError):
        copy_regular(source, target, BackupBudget())
    assert not target.exists()


def test_leaf_replaced_by_fifo_before_open_does_not_block(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    source = tmp_path / "source"
    source.write_bytes(b"payload")
    original = io.os.open

    def replace(name: str | Path, flags: int, *args: object, **kwargs: object) -> int:
        if name == "source" and flags == io._READ_FLAGS:
            source.unlink()
            os.mkfifo(source)
        return original(name, flags, *args, **kwargs)

    monkeypatch.setattr(io.os, "open", replace)
    with pytest.raises(BackupIOError, match="changed"):
        hash_regular(source, BackupBudget())


def test_walk_exclusions_still_consume_entry_budget(tmp_path: Path) -> None:
    for number in range(4):
        (tmp_path / f"skip{number}").write_bytes(b"x")
    with pytest.raises(BackupIOError, match="max_files"):
        list(walk_regular(tmp_path, BackupBudget(max_files=3), excluded=lambda path, _directory: path.name.startswith("skip")))


def test_walk_rejects_nested_links_even_when_excluded(tmp_path: Path) -> None:
    (tmp_path / "link").symlink_to(tmp_path / "missing")
    with pytest.raises(BackupIOError, match="symlink"):
        list(walk_regular(tmp_path, BackupBudget(), excluded=lambda path, _directory: path.name == "link"))


def test_walk_depth_and_time_are_bounded(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    (tmp_path / "a" / "b").mkdir(parents=True)
    with pytest.raises(BackupIOError, match="max_depth"):
        list(walk_regular(tmp_path, BackupBudget(max_depth=1)))
    budget = BackupBudget(max_seconds=1)
    monkeypatch.setattr(io, "monotonic_now", lambda: budget._started + 2)
    with pytest.raises(BackupIOError, match="max_seconds"):
        list(walk_regular(tmp_path, budget))


def test_walk_rechecks_the_root_after_yields(tmp_path: Path) -> None:
    root, moved = tmp_path / "root", tmp_path / "moved"
    root.mkdir()
    (root / "file").write_bytes(b"x")
    iterator = walk_regular(root, BackupBudget())
    assert next(iterator)[0] == Path("file")
    root.rename(moved)
    root.mkdir()
    with pytest.raises(BackupIOError, match="changed"):
        list(iterator)


def test_root_regular_file_is_yielded_by_name(tmp_path: Path) -> None:
    source = tmp_path / "source"
    source.write_bytes(b"x")
    assert list(walk_regular(source, BackupBudget())) == [(Path("source"), source, False)]


def test_failed_open_of_new_directory_cleans_its_empty_identity(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    target = tmp_path / "target"
    original = io.os.open

    def fail_open(name: str | Path, flags: int, *args: object, **kwargs: object) -> int:
        if name == "target" and flags == io._DIRECTORY_FLAGS:
            raise PermissionError("cannot open created directory")
        return original(name, flags, *args, **kwargs)

    monkeypatch.setattr(io.os, "open", fail_open)
    with pytest.raises(BackupIOError), owned_destination(target):
        pytest.fail("failed directory open admitted")
    assert not target.exists()


def test_cleanup_uses_held_directory_when_target_is_replaced_mid_cleanup(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    target, moved = tmp_path / "target", tmp_path / "moved"
    original = io._clear_directory

    def replace(descriptor: int) -> None:
        target.rename(moved)
        target.mkdir()
        (target / "winner").write_bytes(b"keep")
        original(descriptor)

    monkeypatch.setattr(io, "_clear_directory", replace)
    with pytest.raises(KeyboardInterrupt), owned_destination(target):
        (target / "partial").write_bytes(b"owned")
        raise KeyboardInterrupt
    assert (target / "winner").read_bytes() == b"keep"
    assert list(moved.iterdir()) == []


def test_copy_cleanup_preserves_a_concurrent_replacement(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    source, target = tmp_path / "source", tmp_path / "target"
    source.write_bytes(b"payload")

    def replace(_descriptor: int, _chunk: object) -> int:
        target.rename(tmp_path / "partial")
        target.write_bytes(b"winner")
        raise KeyboardInterrupt

    monkeypatch.setattr(io.os, "write", replace)
    with pytest.raises(KeyboardInterrupt):
        copy_regular(source, target, BackupBudget())
    assert target.read_bytes() == b"winner"


def test_no_overwrite_is_enforced_at_create_time(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    source, target = tmp_path / "source", tmp_path / "target"
    source.write_bytes(b"payload")
    original = io.os.open

    def race(name: str | Path, flags: int, *args: object, **kwargs: object) -> int:
        if name == "target" and flags & os.O_CREAT:
            target.write_bytes(b"winner")
        return original(name, flags, *args, **kwargs)

    monkeypatch.setattr(io.os, "open", race)
    with pytest.raises(BackupIOError):
        copy_regular(source, target, BackupBudget())
    assert target.read_bytes() == b"winner"


def test_ancestor_swapped_to_symlink_after_admission_is_not_followed(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    parent, outside = tmp_path / "parent", tmp_path / "outside"
    parent.mkdir()
    outside.mkdir()
    (parent / "source").write_bytes(b"owned")
    (outside / "source").write_bytes(b"outside")
    original = io.os.open
    replaced = False

    def race(name: str | Path, flags: int, *args: object, **kwargs: object) -> int:
        nonlocal replaced
        if name == "parent" and not replaced:
            replaced = True
            parent.rename(tmp_path / "moved")
            parent.symlink_to(outside, target_is_directory=True)
        return original(name, flags, *args, **kwargs)

    monkeypatch.setattr(io.os, "open", race)
    with pytest.raises(BackupIOError):
        hash_regular(parent / "source", BackupBudget())
    assert (outside / "source").read_bytes() == b"outside"


def test_cleanup_cancellation_preserves_original_failure(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    target = tmp_path / "target"
    failure = RuntimeError("body failed")

    def cancel(_descriptor: int) -> None:
        raise KeyboardInterrupt("cleanup cancelled")

    monkeypatch.setattr(io.os, "fsync", cancel)
    with pytest.raises(RuntimeError) as caught, owned_destination(target):
        raise failure
    assert caught.value is failure
    assert isinstance(failure.__cause__, KeyboardInterrupt)
    assert not target.exists()


def test_cancelled_manifest_publish_is_cleaned_by_owned_directory(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    from app.artifacts.io import exclusive_atomic_publish

    target = tmp_path / "target"

    def cancel(_descriptor: int) -> None:
        raise KeyboardInterrupt("fsync cancelled")

    with pytest.raises(KeyboardInterrupt), owned_destination(target):
        monkeypatch.setattr(io.os, "fsync", cancel)
        exclusive_atomic_publish(target / "manifest.json", b"{}", max_bytes=2)
    assert not target.exists()


@pytest.mark.parametrize("limit", ["max_bytes", "max_file_bytes"])
def test_stream_growth_cannot_escape_byte_limits(tmp_path: Path, monkeypatch: pytest.MonkeyPatch, limit: str) -> None:
    source, target = tmp_path / "source", tmp_path / "target"
    source.write_bytes(b"123")
    original = io.os.read
    grown = False

    def grow(descriptor: int, size: int) -> bytes:
        nonlocal grown
        chunk = original(descriptor, size)
        if not grown:
            grown = True
            with source.open("ab") as stream:
                stream.write(b"4567")
        return chunk

    monkeypatch.setattr(io.os, "read", grow)
    budget = BackupBudget(**{limit: 4})
    with pytest.raises(BackupIOError, match=limit):
        copy_regular(source, target, budget)
    assert not target.exists()
    assert budget.bytes <= 4


def test_deadline_is_checked_while_streaming(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    source, target = tmp_path / "source", tmp_path / "target"
    source.write_bytes(b"payload")
    original = io.os.read
    budget = BackupBudget(max_seconds=1)

    def delayed(descriptor: int, count: int) -> bytes:
        chunk = original(descriptor, count)
        monkeypatch.setattr(io, "monotonic_now", lambda: budget._started + 2)
        return chunk

    monkeypatch.setattr(io.os, "read", delayed)
    with pytest.raises(BackupIOError, match="max_seconds"):
        copy_regular(source, target, budget)
    assert not target.exists()


def test_relative_directories_create_declared_parents_in_depth_order(tmp_path: Path) -> None:
    create_relative_directories(tmp_path, ["payload/data/empty", "payload", "payload/data"])
    assert (tmp_path / "payload/data/empty").is_dir()
    assert stat.S_IMODE((tmp_path / "payload/data").stat().st_mode) == 0o700
    with pytest.raises(BackupIOError):
        create_relative_directories(tmp_path, ["payload/data"])
    assert (tmp_path / "payload/data/empty").is_dir()


@pytest.mark.parametrize("name", ["/absolute", "..", "../escape", "payload/../escape", "payload//child", "payload/./child",
                                  "payload/child/", "", ".", "payload\\child", "payload/\x00child"])
def test_relative_directory_names_cannot_escape_the_root(tmp_path: Path, name: str) -> None:
    with pytest.raises(BackupIOError):
        create_relative_directories(tmp_path, [name])
    assert list(tmp_path.iterdir()) == []


def test_relative_directory_duplicates_fail_before_writing(tmp_path: Path) -> None:
    with pytest.raises(BackupIOError, match="duplicates"):
        create_relative_directories(tmp_path, ["payload", "payload"])
    assert list(tmp_path.iterdir()) == []


def test_relative_directory_creation_does_not_follow_replaced_payload(tmp_path: Path) -> None:
    root, outside = tmp_path / "root", tmp_path / "outside"
    root.mkdir()
    outside.mkdir()
    create_relative_directories(root, ["payload"])
    (root / "payload").rename(root / "moved")
    (root / "payload").symlink_to(outside, target_is_directory=True)
    with pytest.raises(BackupIOError):
        create_relative_directories(root, ["payload/escaped"])
    assert list(outside.iterdir()) == []


def test_relative_directory_parent_retarget_after_open_never_writes_outside(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    root, outside = tmp_path / "root", tmp_path / "outside"
    (root / "payload").mkdir(parents=True)
    outside.mkdir()
    original = io.os.mkdir

    def retarget(name: str | Path, *args: object, **kwargs: object) -> None:
        if name == "child":
            (root / "payload").rename(root / "moved")
            (root / "payload").symlink_to(outside, target_is_directory=True)
        original(name, *args, **kwargs)

    monkeypatch.setattr(io.os, "mkdir", retarget)
    with pytest.raises(BackupIOError):
        create_relative_directories(root, ["payload/child"])
    assert list(outside.iterdir()) == []
    assert not (root / "moved/child").exists()


def test_relative_directory_creation_rejects_a_replaced_root(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    root = tmp_path / "root"
    root.mkdir()
    original = io._create_relative_at

    def replace(descriptor: int, root_path: Path, relative: Path) -> None:
        root.rename(tmp_path / "moved")
        root.mkdir()
        original(descriptor, root_path, relative)

    monkeypatch.setattr(io, "_create_relative_at", replace)
    with pytest.raises(BackupIOError):
        create_relative_directories(root, ["payload"])
    assert list(root.iterdir()) == []
    assert list((tmp_path / "moved").iterdir()) == []


@pytest.mark.parametrize("size,expected", [(0, b""), (3, b"abc"), (16, b"abcdefghijklmnop"), (100, b"abcdefghijklmnopqrstuvwxyz")])
def test_regular_prefix_is_bounded_and_handles_short_sources(tmp_path: Path, monkeypatch: pytest.MonkeyPatch, size: int, expected: bytes) -> None:
    source = tmp_path / "source"
    source.write_bytes(b"abcdefghijklmnopqrstuvwxyz")
    original = io.os.read
    total = 0

    def track(descriptor: int, count: int) -> bytes:
        nonlocal total
        assert count <= size - total
        result = original(descriptor, count)
        total += len(result)
        return result

    monkeypatch.setattr(io.os, "read", track)
    assert read_regular_prefix(source, size) == expected
    assert total == len(expected)


@pytest.mark.parametrize("kind", ["symlink", "ancestor", "fifo", "directory"])
def test_regular_prefix_rejects_aliases_and_special_files(tmp_path: Path, kind: str) -> None:
    outside = tmp_path / "outside"
    outside.mkdir()
    (outside / "source").write_bytes(b"outside")
    source = tmp_path / "source"
    if kind == "symlink":
        source.symlink_to(outside / "source")
    elif kind == "ancestor":
        source.symlink_to(outside, target_is_directory=True)
        source /= "source"
    elif kind == "fifo":
        os.mkfifo(source)
    else:
        source.mkdir()
    with pytest.raises(BackupIOError):
        read_regular_prefix(source)


def test_regular_prefix_rejects_fifo_swapped_before_open(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    source = tmp_path / "source"
    source.write_bytes(b"payload")
    original = io.os.open

    def replace(name: str | Path, flags: int, *args: object, **kwargs: object) -> int:
        if name == "source" and flags == io._READ_FLAGS:
            source.unlink()
            os.mkfifo(source)
        return original(name, flags, *args, **kwargs)

    monkeypatch.setattr(io.os, "open", replace)
    with pytest.raises(BackupIOError, match="changed"):
        read_regular_prefix(source)


@pytest.mark.parametrize("change", ["leaf", "ancestor", "rewrite"])
def test_regular_prefix_rechecks_metadata_and_current_namespace(tmp_path: Path, monkeypatch: pytest.MonkeyPatch, change: str) -> None:
    root = tmp_path / "root"
    root.mkdir()
    source = root / "source"
    source.write_bytes(b"payload")
    original = io.os.read

    def mutate(descriptor: int, count: int) -> bytes:
        value = original(descriptor, count)
        if change == "leaf":
            source.rename(root / "old")
            source.write_bytes(b"payload")
        elif change == "ancestor":
            root.rename(tmp_path / "old")
            root.mkdir()
            source.write_bytes(b"payload")
        else:
            source.write_bytes(b"changed")
        return value

    monkeypatch.setattr(io.os, "read", mutate)
    with pytest.raises(BackupIOError, match="changed"):
        read_regular_prefix(source, 3)
