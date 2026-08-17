from __future__ import annotations

from pathlib import Path
import tempfile
from types import SimpleNamespace

import pytest

import app.artifacts.io as artifact_io
from app.artifacts.io import (
    ArtifactCanonicalJsonError,
    ArtifactChangedError,
    ArtifactContentConflictError,
    ArtifactDuplicateKeyError,
    ArtifactIOError,
    ArtifactJsonDecodeError,
    ArtifactNonFiniteConstantError,
    ArtifactNotDirectoryError,
    ArtifactNotRegularError,
    ArtifactPublishConflictError,
    ArtifactReadError,
    ArtifactTooLargeError,
    canonical_json_bytes,
    canonical_json_text,
    content_addressed_filename,
    decode_json_bytes,
    exclusive_atomic_publish,
    read_regular_file,
    sha256_hex,
)
from app.services.market_scan_future_range_artifact import (
    canonical_future_range_artifact_json,
    future_range_payload_integrity_digest,
)
from app.services.market_scan_probability_artifact import (
    canonical_probability_artifact_json,
    probability_payload_integrity_digest,
)
from app.services.market_scan_probability_source import (
    canonical_probability_source_json,
    probability_source_payload_digest,
)


def test_existing_artifact_canonical_bytes_and_digest_are_golden() -> None:
    value = {"z": "中", "a": [1, 1.5, None, True]}
    expected = '{"a":[1,1.5,null,true],"z":"中"}'
    expected_digest = "4f597bd8d24da85312bc4e7b2a63872e5d0f99999807f976f32b7fe8f717c933"

    assert canonical_probability_artifact_json(value).encode() == expected.encode()
    assert canonical_future_range_artifact_json(value).encode() == expected.encode()
    assert canonical_probability_source_json(value).encode() == expected.encode()
    assert probability_payload_integrity_digest(value) == expected_digest
    assert future_range_payload_integrity_digest(value) == expected_digest
    assert probability_source_payload_digest(value) == expected_digest


def test_guarded_read_rejects_symlinks_and_oversize_files(tmp_path: Path) -> None:
    regular = tmp_path / "regular.json"
    regular.write_bytes(b"12345")
    symlink = tmp_path / "link.json"
    symlink.symlink_to(regular)

    assert read_regular_file(regular, max_bytes=5) == b"12345"
    with pytest.raises(ArtifactTooLargeError):
        read_regular_file(regular, max_bytes=4)
    with pytest.raises(ArtifactNotRegularError):
        read_regular_file(symlink, max_bytes=5)

    outside = tmp_path / "outside-read"
    nested = outside / "nested"
    nested.mkdir(parents=True)
    external = nested / "external.json"
    external.write_bytes(b"external")
    ancestor = tmp_path / "read-ancestor-link"
    ancestor.symlink_to(outside, target_is_directory=True)
    with pytest.raises(ArtifactNotRegularError):
        read_regular_file(ancestor / "nested" / "external.json", max_bytes=8)


def test_exclusive_publish_is_idempotent_atomic_and_tamper_closed(tmp_path: Path) -> None:
    target = tmp_path / "nested" / "artifact.json"

    assert exclusive_atomic_publish(target, b"golden", max_bytes=16) is True
    assert exclusive_atomic_publish(target, b"golden", max_bytes=16) is False
    target.write_bytes(b"tampered")
    with pytest.raises(ArtifactContentConflictError):
        exclusive_atomic_publish(target, b"golden", max_bytes=16)

    assert target.read_bytes() == b"tampered"
    assert list(target.parent.glob(f".{target.name}.*.tmp")) == []


def test_exclusive_publish_rejects_parent_symlink_without_writing_target(tmp_path: Path) -> None:
    outside = tmp_path / "outside"
    outside.mkdir()
    linked_parent = tmp_path / "linked-parent"
    linked_parent.symlink_to(outside, target_is_directory=True)

    with pytest.raises(ArtifactNotDirectoryError):
        exclusive_atomic_publish(linked_parent / "artifact.json", b"value", max_bytes=5)

    assert list(outside.iterdir()) == []

    nested_target = linked_parent / "not-created" / "artifact.json"
    with pytest.raises(ArtifactNotDirectoryError):
        exclusive_atomic_publish(nested_target, b"value", max_bytes=5)
    assert not (outside / "not-created").exists()


def test_read_and_publish_map_ancestor_symlink_loops_without_writing_outside(
    tmp_path: Path,
) -> None:
    outside = tmp_path / "outside-loop"
    outside.mkdir()
    entry = tmp_path / "loop-entry"
    backlink = outside / "loop-back"
    entry.symlink_to(backlink, target_is_directory=True)
    backlink.symlink_to(entry, target_is_directory=True)

    with pytest.raises(ArtifactReadError):
        read_regular_file(entry / "artifact.json", max_bytes=8)
    with pytest.raises(ArtifactReadError):
        exclusive_atomic_publish(entry / "nested" / "artifact.json", b"value", max_bytes=5)

    assert {path.name for path in outside.iterdir()} == {"loop-back"}
    assert not (outside / "nested").exists()


def test_read_and_publish_accept_verified_system_tmp_alias() -> None:
    with tempfile.TemporaryDirectory(prefix="ashare-artifact-io-", dir="/tmp") as raw_directory:
        directory = Path(raw_directory)
        source = directory / "source.json"
        source.write_bytes(b"source")
        assert read_regular_file(source, max_bytes=6) == b"source"

        target = directory / "published.json"
        assert exclusive_atomic_publish(target, b"value", max_bytes=5) is True
        assert target.read_bytes() == b"value"


def test_exclusive_publish_rejects_parent_retarget_before_link(tmp_path: Path) -> None:
    parent = tmp_path / "parent"
    parent.mkdir()
    moved_parent = tmp_path / "moved-parent"
    outside = tmp_path / "outside"
    outside.mkdir()

    def retarget_parent() -> None:
        parent.rename(moved_parent)
        parent.symlink_to(outside, target_is_directory=True)

    with pytest.raises(ArtifactChangedError) as changed:
        exclusive_atomic_publish(
            parent / "artifact.json",
            b"value",
            max_bytes=5,
            before_publish=retarget_parent,
        )

    assert changed.value.stage == "parent_before_publish"
    assert list(outside.iterdir()) == []
    assert not (moved_parent / "artifact.json").exists()
    assert list(moved_parent.glob(".artifact.json.*.tmp")) == []


def test_strict_json_decode_rejects_duplicate_and_nonfinite_values() -> None:
    with pytest.raises(ArtifactDuplicateKeyError) as duplicate:
        decode_json_bytes(b'{"value":1,"value":2}')
    assert duplicate.value.key == "value"

    with pytest.raises(ArtifactNonFiniteConstantError) as nonfinite:
        decode_json_bytes(b'{"value":NaN}')
    assert nonfinite.value.constant == "NaN"

    with pytest.raises(ArtifactJsonDecodeError):
        decode_json_bytes(b"\xff")
    with pytest.raises(ArtifactJsonDecodeError):
        decode_json_bytes(b"{")

    deeply_nested_bytes = b"[" * 2_000 + b"0" + b"]" * 2_000
    with pytest.raises(ArtifactJsonDecodeError):
        decode_json_bytes(deeply_nested_bytes)

    deeply_nested_value: object = None
    for _depth in range(2_000):
        deeply_nested_value = [deeply_nested_value]
    with pytest.raises(ArtifactCanonicalJsonError):
        canonical_json_text(deeply_nested_value)


def test_low_level_json_and_content_address_contracts_are_strict() -> None:
    expected = '{"value":"中"}'.encode()
    assert canonical_json_bytes({"value": "中"}) == expected
    assert sha256_hex(expected) == sha256_hex(expected.decode())
    digest = "a" * 64
    assert content_addressed_filename("artifact-run", (7,), digest, ".json.gz") == (
        f"artifact-run-7-{digest}.json.gz"
    )

    for value in ({1: "bad-key"}, {"value": float("nan")}, ("not", "json-array")):
        with pytest.raises(ArtifactCanonicalJsonError):
            canonical_json_text(value)
    for invalid in (
        ("", (7,), digest, ".json"),
        ("artifact", (7,), "A" * 64, ".json"),
        ("artifact", ("../7",), digest, ".json"),
        ("artifact", (7,), digest, "json"),
        ("artifact", (7,), digest, ".json/escape"),
    ):
        with pytest.raises(ValueError):
            content_addressed_filename(*invalid)


def test_guarded_read_maps_missing_invalid_limit_and_open_errors(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    missing = tmp_path / "missing.json"
    with pytest.raises(artifact_io.ArtifactNotFoundError):
        read_regular_file(missing, max_bytes=1)
    with pytest.raises(ValueError):
        read_regular_file(missing, max_bytes=-1)

    regular = tmp_path / "regular.json"
    regular.write_bytes(b"x")
    monkeypatch.setattr(artifact_io.os, "open", lambda *_args: (_ for _ in ()).throw(PermissionError()))
    with pytest.raises(ArtifactReadError):
        read_regular_file(regular, max_bytes=1)


def test_guarded_read_detects_identity_changes(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    source = tmp_path / "changing.json"
    source.write_bytes(b"value")
    real_fstat = artifact_io.os.fstat
    calls = 0

    def changed_after_open(descriptor: int) -> object:
        nonlocal calls
        calls += 1
        facts = real_fstat(descriptor)
        if calls == 2:
            return SimpleNamespace(
                st_dev=facts.st_dev,
                st_ino=facts.st_ino,
                st_mode=facts.st_mode,
                st_size=facts.st_size,
                st_mtime_ns=facts.st_mtime_ns + 1,
                st_ctime_ns=facts.st_ctime_ns,
            )
        return facts

    monkeypatch.setattr(artifact_io.os, "fstat", changed_after_open)
    with pytest.raises(ArtifactChangedError) as changed:
        read_regular_file(source, max_bytes=5)
    assert changed.value.stage == "read"


def test_guarded_open_detects_retarget_before_read(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    source = tmp_path / "retargeted.json"
    source.write_bytes(b"value")
    real_fstat = artifact_io.os.fstat

    def retargeted(descriptor: int) -> object:
        facts = real_fstat(descriptor)
        return SimpleNamespace(
            st_dev=facts.st_dev,
            st_ino=facts.st_ino + 1,
            st_mode=facts.st_mode,
            st_size=facts.st_size,
            st_mtime_ns=facts.st_mtime_ns,
            st_ctime_ns=facts.st_ctime_ns,
        )

    monkeypatch.setattr(artifact_io.os, "fstat", retargeted)
    with pytest.raises(ArtifactChangedError) as changed:
        read_regular_file(source, max_bytes=5)
    assert changed.value.stage == "open"


def test_publish_rejects_oversize_and_cleans_up_conflicts_and_callback_failures(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    target = tmp_path / "artifact.json"
    with pytest.raises(ArtifactTooLargeError):
        exclusive_atomic_publish(target, b"too-large", max_bytes=4)
    assert not target.exists()

    callback_target = tmp_path / "callback.json"
    with pytest.raises(RuntimeError, match="pre-publish"):
        exclusive_atomic_publish(
            callback_target,
            b"value",
            max_bytes=5,
            before_publish=lambda: (_ for _ in ()).throw(RuntimeError("pre-publish")),
        )
    assert not callback_target.exists()
    assert list(tmp_path.glob(".callback.json.*.tmp")) == []

    conflict_target = tmp_path / "conflict.json"

    def lose_publish(*_args: object, **_kwargs: object) -> None:
        conflict_target.write_bytes(b"winner")
        raise FileExistsError

    monkeypatch.setattr(artifact_io.os, "link", lose_publish)
    with pytest.raises(ArtifactPublishConflictError):
        exclusive_atomic_publish(conflict_target, b"loser", max_bytes=6)
    assert conflict_target.read_bytes() == b"winner"
    assert list(tmp_path.glob(".conflict.json.*.tmp")) == []


def test_publish_maps_stream_and_fsync_os_errors_and_cleans_temporary_files(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    write_target = tmp_path / "write-error.json"

    class FailingStream:
        def __init__(self, descriptor: int) -> None:
            self.descriptor = descriptor

        def __enter__(self) -> FailingStream:
            return self

        def __exit__(self, *_args: object) -> None:
            artifact_io.os.close(self.descriptor)

        def write(self, _encoded: bytes) -> int:
            raise OSError("write failed")

        def flush(self) -> None:
            raise AssertionError("flush must not run after write failure")

        def fileno(self) -> int:
            return self.descriptor

    with monkeypatch.context() as scoped:
        scoped.setattr(artifact_io.os, "fdopen", lambda descriptor, _mode: FailingStream(descriptor))
        with pytest.raises(ArtifactIOError):
            exclusive_atomic_publish(write_target, b"value", max_bytes=5)
    assert not write_target.exists()
    assert list(tmp_path.glob(".write-error.json.*.tmp")) == []

    fsync_target = tmp_path / "fsync-error.json"
    real_fsync = artifact_io.os.fsync

    def fail_directory_fsync(descriptor: int) -> None:
        if artifact_io.stat.S_ISDIR(artifact_io.os.fstat(descriptor).st_mode):
            raise OSError("directory fsync failed")
        real_fsync(descriptor)

    with monkeypatch.context() as scoped:
        scoped.setattr(artifact_io.os, "fsync", fail_directory_fsync)
        with pytest.raises(ArtifactIOError):
            exclusive_atomic_publish(fsync_target, b"value", max_bytes=5)
    assert not fsync_target.exists()
    assert list(tmp_path.glob(".fsync-error.json.*.tmp")) == []


def test_fallback_publisher_covers_idempotency_conflict_callback_and_rollback(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(artifact_io, "_directory_descriptor_publish_supported", lambda: False)
    target = tmp_path / "fallback.json"

    assert exclusive_atomic_publish(target, b"value", max_bytes=8) is True
    assert exclusive_atomic_publish(target, b"value", max_bytes=8) is False
    with pytest.raises(ArtifactContentConflictError):
        exclusive_atomic_publish(target, b"changed", max_bytes=8)

    callback_target = tmp_path / "fallback-callback.json"
    with pytest.raises(KeyboardInterrupt):
        exclusive_atomic_publish(
            callback_target,
            b"value",
            max_bytes=8,
            before_publish=lambda: (_ for _ in ()).throw(KeyboardInterrupt),
        )
    assert not callback_target.exists()

    rollback_target = tmp_path / "fallback-rollback.json"
    real_fsync = artifact_io.os.fsync

    def fail_directory_fsync(descriptor: int) -> None:
        if artifact_io.stat.S_ISDIR(artifact_io.os.fstat(descriptor).st_mode):
            raise OSError("directory fsync failed")
        real_fsync(descriptor)

    monkeypatch.setattr(artifact_io.os, "fsync", fail_directory_fsync)
    with pytest.raises(ArtifactReadError):
        exclusive_atomic_publish(rollback_target, b"value", max_bytes=8)
    assert not rollback_target.exists()
    assert list(tmp_path.glob(".fallback-*.tmp")) == []


def test_low_level_directory_and_dirfd_failures_map_to_artifact_errors(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    regular = tmp_path / "regular"
    regular.write_bytes(b"value")
    with pytest.raises(ArtifactNotDirectoryError):
        artifact_io._open_stable_directory(regular)
    with pytest.raises(artifact_io.ArtifactNotFoundError):
        artifact_io._open_stable_directory(tmp_path / "missing")

    directory = tmp_path / "directory"
    directory.mkdir()
    real_open = artifact_io.os.open
    with monkeypatch.context() as scoped:
        scoped.setattr(
            artifact_io.os,
            "open",
            lambda *_args, **_kwargs: (_ for _ in ()).throw(PermissionError()),
        )
        with pytest.raises(ArtifactReadError):
            artifact_io._open_stable_directory(directory)

    descriptor = real_open(directory, artifact_io.os.O_RDONLY)
    try:
        opened = artifact_io.os.fstat(descriptor)
        with monkeypatch.context() as scoped:
            scoped.setattr(
                artifact_io.Path,
                "lstat",
                lambda _path: (_ for _ in ()).throw(FileNotFoundError()),
            )
            with pytest.raises(ArtifactChangedError, match=str(directory)):
                artifact_io._validate_directory_identity(directory, opened, stage="missing")

        target = directory / "artifact.json"
        with monkeypatch.context() as scoped:
            scoped.setattr(
                artifact_io.os,
                "stat",
                lambda *_args, **_kwargs: (_ for _ in ()).throw(PermissionError()),
            )
            with pytest.raises(ArtifactReadError):
                artifact_io._existing_bytes_at(target, descriptor, max_bytes=8)

        with monkeypatch.context() as scoped:
            scoped.setattr(
                artifact_io.os,
                "open",
                lambda *_args, **_kwargs: (_ for _ in ()).throw(PermissionError()),
            )
            with pytest.raises(ArtifactReadError):
                artifact_io._write_temporary_at(target, b"value", descriptor)

        with monkeypatch.context() as scoped:
            scoped.setattr(
                artifact_io.os,
                "open",
                lambda *_args, **_kwargs: (_ for _ in ()).throw(FileExistsError()),
            )
            with pytest.raises(ArtifactPublishConflictError):
                artifact_io._write_temporary_at(target, b"value", descriptor)

        with monkeypatch.context() as scoped:
            scoped.setattr(
                artifact_io.os,
                "link",
                lambda *_args, **_kwargs: (_ for _ in ()).throw(FileExistsError()),
            )
            with pytest.raises(ArtifactPublishConflictError):
                artifact_io._link_exclusively_at("temporary", target, descriptor)
        with monkeypatch.context() as scoped:
            scoped.setattr(
                artifact_io.os,
                "link",
                lambda *_args, **_kwargs: (_ for _ in ()).throw(PermissionError()),
            )
            with pytest.raises(ArtifactReadError):
                artifact_io._link_exclusively_at("temporary", target, descriptor)

        artifact_io._unlink_at("already-missing", descriptor)
    finally:
        artifact_io.os.close(descriptor)


def test_low_level_read_overflow_alias_variants_and_fsync_cleanup(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    read_descriptor, write_descriptor = artifact_io.os.pipe()
    try:
        artifact_io.os.write(write_descriptor, b"12345")
    finally:
        artifact_io.os.close(write_descriptor)
    try:
        opened = artifact_io.os.fstat(read_descriptor)
        with pytest.raises(ArtifactTooLargeError):
            artifact_io._read_open_file(
                tmp_path / "pipe",
                read_descriptor,
                opened,
                max_bytes=4,
            )
    finally:
        artifact_io.os.close(read_descriptor)

    with monkeypatch.context() as scoped:
        scoped.setattr(artifact_io.sys, "platform", "linux")
        assert artifact_io._trusted_system_alias_path(Path("/tmp/value")) == Path("/tmp/value")
    with monkeypatch.context() as scoped:
        scoped.setitem(artifact_io._DARWIN_SYSTEM_ALIASES, "tmp", Path("/unexpected"))
        assert artifact_io._trusted_system_alias_path(Path("/tmp/value")) == Path("/tmp/value")
    with monkeypatch.context() as scoped:
        scoped.setattr(artifact_io.Path, "lstat", lambda _path: tmp_path.stat())
        assert artifact_io._trusted_system_alias_path(Path("/tmp/value")) == Path("/tmp/value")

    artifact_io._fsync_directory(tmp_path)
    temporary_target = tmp_path / "temporary-error.json"

    def fail_fdopen(descriptor: int, _mode: str) -> object:
        artifact_io.os.close(descriptor)
        raise OSError("fdopen failed")

    with monkeypatch.context() as scoped:
        scoped.setattr(artifact_io.os, "fdopen", fail_fdopen)
        with pytest.raises(OSError, match="fdopen failed"):
            artifact_io._write_temporary(temporary_target, b"value")
    assert list(tmp_path.glob(".temporary-error.json.*.tmp")) == []
