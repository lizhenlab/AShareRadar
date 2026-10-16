"""Bounded, descriptor-relative I/O for explicitly selected workspace backups.

A destination is incomplete until its caller publishes the final manifest. File
and directory names are never replaced. Limits are cumulative per budget; walk
entries and streamed files have separate counters, both capped by max_files.
"""

from __future__ import annotations

from collections.abc import Callable, Iterator, Sequence
from contextlib import contextmanager
from dataclasses import dataclass, field
import hashlib
import math
import os
from pathlib import Path
import stat
import sys
from typing import TypedDict

from app.artifacts.io import path_has_only_trusted_aliases
from app.services.workspace_backup_progress import backup_checkpoint
from app.utils.clock import monotonic_now


_CHUNK_BYTES = 1024 * 1024
_DIRECTORY_FLAGS = os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW | os.O_CLOEXEC
_READ_FLAGS = os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK | os.O_CLOEXEC


class BackupIOError(ValueError):
    """A path, resource budget or stable-file requirement was not satisfied."""


class FileFacts(TypedDict):
    size_bytes: int
    sha256: str
    mode: int


@dataclass
class BackupBudget:
    max_files: int = 100_000
    max_bytes: int = 1024**4
    max_file_bytes: int = 256 * 1024**3
    max_depth: int = 64
    max_seconds: float = 3600
    files: int = field(default=0, init=False)
    bytes: int = field(default=0, init=False)
    entries: int = field(default=0, init=False)
    _started: float = field(default_factory=monotonic_now, init=False, repr=False)

    def __post_init__(self) -> None:
        limits = (self.max_files, self.max_bytes, self.max_file_bytes, self.max_depth)
        if any(isinstance(value, bool) or not isinstance(value, int) or value < 0 for value in limits):
            raise BackupIOError("backup count, byte and depth budgets must be non-negative integers")
        if self.max_depth > 256:
            raise BackupIOError("backup max_depth must not exceed 256")
        if isinstance(self.max_seconds, bool) or not isinstance(self.max_seconds, (int, float)) or not math.isfinite(self.max_seconds) or self.max_seconds <= 0:
            raise BackupIOError("backup max_seconds must be finite and positive")

    def check(self, *, depth: int = 0) -> None:
        backup_checkpoint()
        if depth > self.max_depth:
            raise BackupIOError("backup max_depth exhausted")
        if monotonic_now() - self._started >= self.max_seconds:
            raise BackupIOError("backup max_seconds exhausted")

    def admit(self, kind: str) -> None:
        self.check()
        count = self.entries if kind == "entries" else self.files
        if count >= self.max_files:
            raise BackupIOError(f"backup max_files exhausted ({kind})")
        if kind == "entries":
            self.entries += 1
        else:
            self.files += 1

    def admit_bytes(self, size: int, file_size: int) -> None:
        self.check()
        if file_size > self.max_file_bytes:
            raise BackupIOError("backup max_file_bytes exhausted")
        if self.bytes + size > self.max_bytes:
            raise BackupIOError("backup max_bytes exhausted")
        self.bytes += size


def safe_absolute_path(path: str | Path) -> Path:
    """Reject all path aliases except the shared, verified Darwin system aliases."""
    raw = os.fspath(path)
    value = Path(raw)
    if not value.is_absolute() or ".." in value.parts or "\x00" in raw:
        raise BackupIOError("backup paths must be absolute and contain no parent traversal")
    try:
        if not path_has_only_trusted_aliases(value):
            raise BackupIOError("backup path contains a symlink or unsupported alias")
        if sys.platform == "darwin" and len(value.parts) > 1 and value.parts[1] in {"tmp", "var"}:
            alias = Path("/") / value.parts[1]
            if alias.is_symlink():
                return Path("/private").joinpath(*value.parts[1:])
        return value
    except (OSError, RuntimeError) as exc:
        raise BackupIOError("backup path cannot be safely inspected") from exc


@contextmanager
def _parent_descriptor(path: Path) -> Iterator[int]:
    descriptor = os.open("/", _DIRECTORY_FLAGS)
    try:
        for part in path.parent.parts[1:]:
            child = os.open(part, _DIRECTORY_FLAGS, dir_fd=descriptor)
            os.close(descriptor)
            descriptor = child
        yield descriptor
    finally:
        os.close(descriptor)


def _identity(facts: os.stat_result) -> tuple[int, int]:
    return facts.st_dev, facts.st_ino


def _fingerprint(facts: os.stat_result) -> tuple[int, ...]:
    return (*_identity(facts), facts.st_mode, facts.st_size, facts.st_mtime_ns, facts.st_ctime_ns)


def _require_same(actual: os.stat_result, expected: os.stat_result, *, metadata: bool = True) -> None:
    observed = _fingerprint(actual) if metadata else _identity(actual)
    original = _fingerprint(expected) if metadata else _identity(expected)
    if observed != original:
        raise BackupIOError("backup path identity or metadata changed during the operation")


def _verify_path(path: Path, expected: os.stat_result, *, metadata: bool = True) -> None:
    with _parent_descriptor(path) as parent:
        _require_same(os.stat(path.name or ".", dir_fd=parent, follow_symlinks=False), expected, metadata=metadata)


@contextmanager
def _regular_source(path: Path, budget: BackupBudget) -> Iterator[tuple[int, os.stat_result]]:
    with _parent_descriptor(path) as parent:
        before = os.stat(path.name, dir_fd=parent, follow_symlinks=False)
        if not stat.S_ISREG(before.st_mode):
            raise BackupIOError("backup source is not a regular file")
        budget.admit("files")
        budget.admit_bytes(0, before.st_size)
        if before.st_size > budget.max_bytes - budget.bytes:
            raise BackupIOError("backup max_bytes exhausted")
        descriptor = os.open(path.name, _READ_FLAGS, dir_fd=parent)
        try:
            _require_same(os.fstat(descriptor), before)
            yield descriptor, before
            _require_same(os.fstat(descriptor), before)
            _require_same(os.stat(path.name, dir_fd=parent, follow_symlinks=False), before)
            _verify_path(path, before)
        finally:
            os.close(descriptor)


def _stream(source: int, destination: int | None, budget: BackupBudget, before: os.stat_result) -> FileFacts:
    digest = hashlib.sha256()
    used = 0
    backup_checkpoint(current=used, total=before.st_size, unit="bytes")
    while True:
        budget.check()
        allowance = min(_CHUNK_BYTES, budget.max_file_bytes - used + 1, budget.max_bytes - budget.bytes + 1)
        chunk = os.read(source, allowance)
        if not chunk:
            break
        budget.admit_bytes(len(chunk), used + len(chunk))
        if destination is not None:
            _write_all(destination, chunk)
        digest.update(chunk)
        used += len(chunk)
        backup_checkpoint(current=used, total=before.st_size, unit="bytes")
    if used != before.st_size:
        raise BackupIOError("backup source size changed during reading")
    return {"size_bytes": used, "sha256": digest.hexdigest(), "mode": stat.S_IMODE(before.st_mode) & 0o777}


def _write_all(descriptor: int, chunk: bytes) -> None:
    remaining = memoryview(chunk)
    while remaining:
        written = os.write(descriptor, remaining)
        if written <= 0:
            raise BackupIOError("backup destination write made no progress")
        remaining = remaining[written:]


def _unlink_owned(parent: int, name: str, expected: os.stat_result) -> None:
    try:
        current = os.stat(name, dir_fd=parent, follow_symlinks=False)
    except FileNotFoundError:
        return
    if _identity(current) == _identity(expected):
        os.unlink(name, dir_fd=parent)


@contextmanager
def _regular_destination(path: Path) -> Iterator[int]:
    with _parent_descriptor(path) as parent:
        flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW | os.O_CLOEXEC
        descriptor = os.open(path.name, flags, 0o600, dir_fd=parent)
        before = None
        try:
            before = os.fstat(descriptor)
            yield descriptor
            _verify_path(path, before, metadata=False)
            os.fsync(parent)
        except BaseException as failure:
            try:
                if before is not None:
                    _unlink_owned(parent, path.name, before)
                else:
                    failure.add_note("backup file retained because its created identity could not be inspected")
            except BaseException as cleanup:
                failure.add_note(f"backup file cleanup failed: {type(cleanup).__name__}")
                raise failure from cleanup
            raise
        finally:
            os.close(descriptor)


def copy_regular(source: str | Path, destination: str | Path, budget: BackupBudget) -> FileFacts:
    """Stream one stable source into a new file, preserving only ordinary mode bits."""
    source_path, target = safe_absolute_path(source), safe_absolute_path(destination)
    try:
        with _regular_destination(target) as output:
            with _regular_source(source_path, budget) as (input_descriptor, before):
                result = _stream(input_descriptor, output, budget, before)
                os.fchmod(output, result["mode"])
                os.fsync(output)
            return result
    except OSError as exc:
        raise BackupIOError(f"backup file copy failed: {type(exc).__name__}") from exc


def hash_regular(path: str | Path, budget: BackupBudget) -> FileFacts:
    """Hash one bounded stable regular file, including its final path identity."""
    source = safe_absolute_path(path)
    try:
        with _regular_source(source, budget) as (descriptor, before):
            return _stream(descriptor, None, budget, before)
    except OSError as exc:
        raise BackupIOError(f"backup file verification failed: {type(exc).__name__}") from exc


def _read_prefix_at(parent: int, name: str, max_bytes: int) -> tuple[bytes, os.stat_result]:
    before = os.stat(name, dir_fd=parent, follow_symlinks=False)
    if not stat.S_ISREG(before.st_mode):
        raise BackupIOError("backup prefix source is not a regular file")
    descriptor = os.open(name, _READ_FLAGS, dir_fd=parent)
    try:
        _require_same(os.fstat(descriptor), before)
        remaining, chunks = max_bytes, []
        while remaining:
            chunk = os.read(descriptor, min(_CHUNK_BYTES, remaining))
            if not chunk:
                break
            chunks.append(chunk)
            remaining -= len(chunk)
        _require_same(os.fstat(descriptor), before)
        _require_same(os.stat(name, dir_fd=parent, follow_symlinks=False), before)
        return b"".join(chunks), before
    finally:
        os.close(descriptor)


def read_regular_prefix(path: str | Path, max_bytes: int = 16) -> bytes:
    """Read at most the requested prefix from a stable non-symlink regular file."""
    if isinstance(max_bytes, bool) or not isinstance(max_bytes, int) or max_bytes < 0:
        raise BackupIOError("backup prefix size must be a non-negative integer")
    source = safe_absolute_path(path)
    try:
        with _parent_descriptor(source) as parent:
            encoded, before = _read_prefix_at(parent, source.name, max_bytes)
        _verify_path(source, before)
        return encoded
    except OSError as exc:
        raise BackupIOError(f"backup prefix read failed: {type(exc).__name__}") from exc


def _clear_directory(descriptor: int) -> None:
    with os.scandir(descriptor) as entries:
        for entry in entries:
            before = os.stat(entry.name, dir_fd=descriptor, follow_symlinks=False)
            if stat.S_ISDIR(before.st_mode):
                child = os.open(entry.name, _DIRECTORY_FLAGS, dir_fd=descriptor)
                try:
                    _require_same(os.fstat(child), before)
                    _clear_directory(child)
                    _require_same(os.stat(entry.name, dir_fd=descriptor, follow_symlinks=False), before, metadata=False)
                    os.rmdir(entry.name, dir_fd=descriptor)
                finally:
                    os.close(child)
            else:
                _unlink_owned(descriptor, entry.name, before)


def _remove_owned_directory(parent: int, name: str, descriptor: int | None, before: os.stat_result) -> None:
    try:
        current = os.stat(name, dir_fd=parent, follow_symlinks=False)
    except FileNotFoundError:
        return
    if _identity(current) != _identity(before):
        return
    if descriptor is not None:
        _require_same(os.fstat(descriptor), before, metadata=False)
        _clear_directory(descriptor)
    current = os.stat(name, dir_fd=parent, follow_symlinks=False)
    if _identity(current) == _identity(before):
        os.rmdir(name, dir_fd=parent)
        os.fsync(parent)


@contextmanager
def _owned_at(parent: int, target: Path) -> Iterator[Path]:
    os.mkdir(target.name, 0o700, dir_fd=parent)
    before = None
    descriptor = None
    try:
        before = os.stat(target.name, dir_fd=parent, follow_symlinks=False)
        descriptor = os.open(target.name, _DIRECTORY_FLAGS, dir_fd=parent)
        _require_same(os.fstat(descriptor), before)
        yield target
        _verify_path(target, before, metadata=False)
        os.fsync(descriptor)
        os.fsync(parent)
    except BaseException as failure:
        try:
            if before is not None:
                _remove_owned_directory(parent, target.name, descriptor, before)
            else:
                failure.add_note("backup directory retained because its created identity could not be inspected")
        except BaseException as cleanup:
            failure.add_note(f"backup directory cleanup failed: {type(cleanup).__name__}")
            raise failure from cleanup
        raise
    finally:
        if descriptor is not None:
            os.close(descriptor)


@contextmanager
def owned_destination(destination: str | Path) -> Iterator[Path]:
    """Exclusively create a private destination; clean only its owned identity on failure."""
    target = safe_absolute_path(destination)
    if target == Path("/"):
        raise BackupIOError("backup destination cannot be the filesystem root")
    try:
        with _parent_descriptor(target) as parent, _owned_at(parent, target) as owned:
            yield owned
    except OSError as exc:
        raise BackupIOError(f"backup destination failed: {type(exc).__name__}") from exc


def _relative_directory_name(name: str) -> Path:
    if not isinstance(name, str):
        raise BackupIOError("backup directory names must be relative strings")
    value = Path(name)
    if (not name or name == "." or value.is_absolute() or value.as_posix() != name or ".." in value.parts
            or "\\" in name or any(ord(character) < 32 for character in name) or len(value.parts) > 256):
        raise BackupIOError("backup directory name is not a canonical bounded relative path")
    return value


@contextmanager
def _relative_parent_descriptor(root: int, relative: Path) -> Iterator[int]:
    descriptor = os.dup(root)
    try:
        for component in relative.parent.parts:
            child = os.open(component, _DIRECTORY_FLAGS, dir_fd=descriptor)
            os.close(descriptor)
            descriptor = child
        yield descriptor
    finally:
        os.close(descriptor)


def _create_relative_at(root: int, root_path: Path, relative: Path) -> None:
    with _relative_parent_descriptor(root, relative) as parent:
        before = os.fstat(parent)
        _verify_path(root_path / relative.parent, before, metadata=False)
        with _owned_at(parent, root_path / relative):
            _verify_path(root_path / relative.parent, before, metadata=False)
        _require_same(os.fstat(parent), before, metadata=False)


def create_relative_directories(root: str | Path, names: Sequence[str]) -> None:
    """Create new declared directories below one held root; existing leaves fail closed."""
    if len(names) > 100_000:
        raise BackupIOError("backup directory count exceeds its limit")
    relative = [_relative_directory_name(name) for name in names]
    if len(set(relative)) != len(relative):
        raise BackupIOError("backup directory names contain duplicates")
    selected = safe_absolute_path(root)
    try:
        with _parent_descriptor(selected) as parent:
            before = os.stat(selected.name or ".", dir_fd=parent, follow_symlinks=False)
            descriptor = os.open(selected.name or ".", _DIRECTORY_FLAGS, dir_fd=parent)
            try:
                _require_same(os.fstat(descriptor), before)
                for name in sorted(relative, key=lambda value: (len(value.parts), value.as_posix())):
                    _verify_path(selected, before, metadata=False)
                    _create_relative_at(descriptor, selected, name)
                _verify_path(selected, before, metadata=False)
            finally:
                os.close(descriptor)
    except OSError as exc:
        raise BackupIOError(f"backup directory creation failed: {type(exc).__name__}") from exc


def _walk_entry(parent: int, name: str, root: Path, relative: Path, budget: BackupBudget,
                excluded: Callable[[Path, bool], bool] | None, depth: int) -> Iterator[tuple[Path, Path, bool]]:
    budget.check(depth=depth)
    budget.admit("entries")
    before = os.stat(name, dir_fd=parent, follow_symlinks=False)
    is_dir = stat.S_ISDIR(before.st_mode)
    if not is_dir and not stat.S_ISREG(before.st_mode):
        raise BackupIOError("backup tree contains a symlink or special file")
    path = root / relative
    if excluded is not None and excluded(path, is_dir):
        return
    if relative != Path("."):
        yield relative, path, is_dir
    if is_dir:
        yield from _walk_directory(parent, name, root, relative, budget, excluded, depth, before)
    _require_same(os.stat(name, dir_fd=parent, follow_symlinks=False), before)


def _walk_directory(parent: int, name: str, root: Path, relative: Path, budget: BackupBudget,
                    excluded: Callable[[Path, bool], bool] | None, depth: int, before: os.stat_result) -> Iterator[tuple[Path, Path, bool]]:
    descriptor = os.open(name, _DIRECTORY_FLAGS, dir_fd=parent)
    try:
        _require_same(os.fstat(descriptor), before)
        with os.scandir(descriptor) as entries:
            for entry in entries:
                yield from _walk_entry(descriptor, entry.name, root, relative / entry.name, budget, excluded, depth + 1)
        _require_same(os.fstat(descriptor), before)
    finally:
        os.close(descriptor)


def walk_regular(root: str | Path, budget: BackupBudget, *, excluded: Callable[[Path, bool], bool] | None = None) -> Iterator[tuple[Path, Path, bool]]:
    """Yield directories and files without following links; consume fully for final checks."""
    source = safe_absolute_path(root)
    try:
        with _parent_descriptor(source) as parent:
            before = os.stat(source.name or ".", dir_fd=parent, follow_symlinks=False)
            is_dir = stat.S_ISDIR(before.st_mode)
            base, relative = (source, Path(".")) if is_dir else (source.parent, Path(source.name))
            yield from _walk_entry(parent, source.name or ".", base, relative, budget, excluded, 0)
            _verify_path(source, before)
            budget.check()
    except OSError as exc:
        raise BackupIOError(f"backup tree inspection failed: {type(exc).__name__}") from exc
