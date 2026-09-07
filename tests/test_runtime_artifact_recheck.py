from __future__ import annotations

import gzip
import os
from pathlib import Path

import pytest

from app.artifacts.io import canonical_json_text, sha256_hex
import app.repositories.runtime_artifact_fingerprint as fingerprints
from app.repositories.runtime_probability_artifact_stream import stream_probability_artifact_references
import app.repositories.runtime_research_artifact_retention as retention


def _artifact(root: Path, *, compressed: bool = True) -> Path:
    relative = "research/market_scan_probability_source" if compressed else "research/market_scan_future_range"
    directory = root / relative
    directory.mkdir(parents=True)
    payload = {"run_id": 37, "source_run_ids": [29, 37], "value": "original"}
    digest = sha256_hex(canonical_json_text(payload))
    artifact = {"payload": payload, "integrity": {"algorithm": "sha256", "scope": "payload", "integrity_digest": digest}}
    prefix = "market-scan-probability-source" if compressed else "market-scan-future-range"
    suffix = ".json.gz" if compressed else ".json"
    target = directory / f"{prefix}-run-37-{digest}{suffix}"
    encoded = canonical_json_text(artifact).encode()
    target.write_bytes(gzip.compress(encoded, mtime=0) if compressed else encoded)
    return target


def _summary(root: Path) -> Path:
    path = root / "research/market-scan-future-range-summary.json"
    path.parent.mkdir(exist_ok=True)
    path.write_text(canonical_json_text({
        "schema_version": "market-scan-future-range-evaluation-summary-v1", "artifact_count": 1,
        "artifacts": [{"run_id": 53, "integrity_digest": "a" * 64, "offline_replay_verified": True}],
    }))
    return path


def test_rechecks_verify_bytes_without_repeating_json_or_decompression(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    _artifact(tmp_path)
    _summary(tmp_path)
    database = tmp_path / "runtime.sqlite3"
    expected = retention.market_scan_artifact_protection(database)
    assert expected.run_ids == {29, 37, 53}

    def forbidden_decode(_encoded: bytes) -> object:
        pytest.fail("unchanged rechecks must not repeat JSON decoding")

    monkeypatch.setattr(retention, "decode_json_bytes", forbidden_decode)
    retention.require_market_scan_artifacts_unchanged(database, expected)
    retention.require_market_scan_artifacts_unchanged(database, expected)


@pytest.mark.parametrize("summary", [False, True])
def test_same_length_content_change_with_restored_mtime_is_rejected(tmp_path: Path, summary: bool) -> None:
    artifact = _artifact(tmp_path, compressed=False)
    path = _summary(tmp_path) if summary else artifact
    database = tmp_path / "runtime.sqlite3"
    expected = retention.market_scan_artifact_protection(database)
    before = path.stat()
    encoded = path.read_bytes()
    changed = encoded.replace(b'"run_id":53', b'"run_id":54') if summary else encoded.replace(b"original", b"tampered")
    assert len(changed) == len(encoded) and changed != encoded
    path.write_bytes(changed)
    os.utime(path, ns=(before.st_atime_ns, before.st_mtime_ns))
    with pytest.raises(retention.RuntimeCleanupIntegrityError):
        retention.require_market_scan_artifacts_unchanged(database, expected)


@pytest.mark.parametrize("level", ["root", "research", "directory", "file"])
def test_recheck_rejects_symlink_at_every_artifact_namespace_level(tmp_path: Path, level: str) -> None:
    root = tmp_path / "data"
    artifact = _artifact(root)
    database = root / "runtime.sqlite3"
    expected = retention.market_scan_artifact_protection(database)
    target = {"root": root, "research": root / "research", "directory": artifact.parent, "file": artifact}[level]
    moved = target.with_name(target.name + "-moved")
    target.rename(moved)
    target.symlink_to(moved, target_is_directory=level != "file")
    with pytest.raises(retention.RuntimeCleanupIntegrityError):
        retention.require_market_scan_artifacts_unchanged(database, expected)


def test_new_protection_operation_does_not_reuse_prior_semantic_validation(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    _artifact(tmp_path)
    database = tmp_path / "runtime.sqlite3"
    first = retention.market_scan_artifact_protection(database)
    calls = 0
    original = retention.decode_json_bytes

    def counted_decode(encoded: bytes) -> object:
        nonlocal calls
        calls += 1
        return original(encoded)

    monkeypatch.setattr(retention, "decode_json_bytes", counted_decode)
    second = retention.market_scan_artifact_protection(database)
    assert second == first and calls == 1


@pytest.mark.parametrize("change_during_decode", [False, True])
def test_content_digest_binds_validated_bytes_even_when_metadata_cannot_distinguish_change(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, change_during_decode: bool,
) -> None:
    path = _artifact(tmp_path, compressed=False)
    original_fingerprint = retention._fingerprint(path.name, path.stat())
    encoded = path.read_bytes()
    changed = encoded.replace(b"original", b"tampered")
    original_decode = retention.decode_json_bytes

    def decode_then_change(value: bytes) -> object:
        decoded = original_decode(value)
        path.write_bytes(changed)
        return decoded

    # Model metadata reuse/coarse timestamps; raw bytes remain independently checked.
    monkeypatch.setattr(retention, "_fingerprint", lambda _name, _facts: original_fingerprint)
    if change_during_decode:
        monkeypatch.setattr(retention, "decode_json_bytes", decode_then_change)
    database = tmp_path / "runtime.sqlite3"
    expected = retention.market_scan_artifact_protection(database)
    if not change_during_decode:
        path.write_bytes(changed)
    with pytest.raises(retention.RuntimeCleanupIntegrityError, match="事务期间发生变化"):
        retention.require_market_scan_artifacts_unchanged(database, expected)


@pytest.mark.parametrize("change", ["modify", "grow", "truncate", "replace"])
def test_recheck_rejects_file_mutation_during_raw_read_and_closes_descriptor(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, change: str,
) -> None:
    path = _artifact(tmp_path, compressed=False)
    database = tmp_path / "runtime.sqlite3"
    expected = retention.market_scan_artifact_protection(database)
    original_read = os.read
    descriptor: int | None = None

    def read_then_change(fd: int, size: int) -> bytes:
        nonlocal descriptor
        chunk = original_read(fd, size)
        if descriptor is None:
            descriptor = fd
            encoded = path.read_bytes()
            if change == "replace":
                replacement = path.with_suffix(".new")
                replacement.write_bytes(encoded)
                replacement.replace(path)
            else:
                content = {"modify": encoded.replace(b"original", b"tampered"), "grow": encoded + b" ", "truncate": b""}[change]
                path.write_bytes(content)
        return chunk

    monkeypatch.setattr(fingerprints.os, "read", read_then_change)
    with pytest.raises(retention.RuntimeCleanupIntegrityError):
        retention.require_market_scan_artifacts_unchanged(database, expected)
    assert descriptor is not None
    with pytest.raises(OSError):
        os.fstat(descriptor)


@pytest.mark.parametrize("change", ["add", "delete", "unknown", "oversized"])
def test_namespace_or_size_change_fails_before_reading_changed_content(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, change: str,
) -> None:
    path = _artifact(tmp_path)
    database = tmp_path / "runtime.sqlite3"
    expected = retention.market_scan_artifact_protection(database)
    if change == "add":
        _artifact(tmp_path, compressed=False)
    elif change == "delete":
        path.unlink()
    elif change == "unknown":
        (path.parent / "unattributed.json").write_text("{}")
    else:
        with path.open("r+b") as stream:
            stream.truncate(512 * 1024 * 1024)

    original_read = retention.artifact_content_sha256

    def forbidden_read(source: Path, *, expected_size: int) -> str:
        if change == "add" and source == path:
            return original_read(source, expected_size=expected_size)
        pytest.fail(f"changed namespace must fail before reading {expected_size} bytes")

    monkeypatch.setattr(retention, "artifact_content_sha256", forbidden_read)
    with pytest.raises(retention.RuntimeCleanupIntegrityError):
        retention.require_market_scan_artifacts_unchanged(database, expected)


def test_streamed_probability_references_include_digest_of_the_same_raw_bytes(tmp_path: Path) -> None:
    payload = {"run_id": 37}
    digest = sha256_hex(canonical_json_text(payload))
    encoded = canonical_json_text({
        "generated_at": "2026-08-11T07:09:44Z", "payload": payload,
        "schema_version": "market-scan-probability-artifact-v1",
        "integrity": {"algorithm": "sha256", "scope": "payload", "integrity_digest": digest,
                      "notice": "integrity_digest_not_a_signature"},
    }).encode()
    path = tmp_path / f"market-scan-probability-run-37-{digest}.json"
    path.write_bytes(encoded)
    assert stream_probability_artifact_references(path, 37, digest, len(encoded)) == ({37}, sha256_hex(encoded))
    alias = tmp_path / "alias.json"
    alias.symlink_to(path)
    with pytest.raises(retention.RuntimeCleanupIntegrityError, match="无法流式读取"):
        stream_probability_artifact_references(alias, 37, digest, len(encoded))
