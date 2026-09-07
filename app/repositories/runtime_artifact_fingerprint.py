"""Bounded raw-byte fingerprints for one guarded retention operation."""

from __future__ import annotations

from collections.abc import Iterator
import hashlib
import os
from pathlib import Path
import stat

from app.artifacts.io import (
    ArtifactChangedError,
    ArtifactIOError,
    ArtifactNotRegularError,
    ArtifactReadError,
    path_has_only_trusted_aliases,
)


def require_trusted_artifact_path(path: Path) -> None:
    try:
        trusted = path_has_only_trusted_aliases(path)
    except (OSError, RuntimeError) as exc:
        raise ArtifactReadError(path) from exc
    if not trusted:
        raise ArtifactNotRegularError(path)


def iter_regular_artifact_chunks(path: Path, *, expected_size: int) -> Iterator[bytes]:
    """Read one exact file identity, rejecting aliases and mutations before returning."""
    descriptor: int | None = None
    try:
        require_trusted_artifact_path(path)
        before = path.lstat()
        if not stat.S_ISREG(before.st_mode) or before.st_size != expected_size:
            raise ArtifactNotRegularError(path)
        descriptor = os.open(path, os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0))
        opened = os.fstat(descriptor)
        if _identity(opened) != _identity(before):
            raise ArtifactChangedError(path, stage="fingerprint_open")
        total = 0
        while chunk := os.read(descriptor, min(1024 * 1024, expected_size + 1 - total)):
            total += len(chunk)
            if total > expected_size:
                raise ArtifactChangedError(path, stage="fingerprint_size")
            yield chunk
        require_trusted_artifact_path(path)
        if total != expected_size or _identity(os.fstat(descriptor)) != _identity(opened):
            raise ArtifactChangedError(path, stage="fingerprint_read")
        if _identity(path.lstat()) != _identity(opened):
            raise ArtifactChangedError(path, stage="fingerprint_path")
    except ArtifactIOError:
        raise
    except OSError as exc:
        raise ArtifactReadError(path) from exc
    finally:
        if descriptor is not None:
            os.close(descriptor)


def artifact_content_sha256(path: Path, *, expected_size: int) -> str:
    digest = hashlib.sha256()
    for chunk in iter_regular_artifact_chunks(path, expected_size=expected_size):
        digest.update(chunk)
    return digest.hexdigest()


def _identity(facts: os.stat_result) -> tuple[int, int, int, int, int, int]:
    return facts.st_dev, facts.st_ino, facts.st_mode, facts.st_size, facts.st_mtime_ns, facts.st_ctime_ns
