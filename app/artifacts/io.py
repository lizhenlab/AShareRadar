"""Fail-closed mechanical I/O for immutable, content-addressed artifacts."""

from __future__ import annotations

from collections.abc import Callable, Sequence
import hashlib
import json
import math
import os
from pathlib import Path
import re
import secrets
import stat
import sys
import tempfile


_SHA256 = re.compile(r"[0-9a-f]{64}")
_READ_CHUNK_BYTES = 1024 * 1024
_MAX_JSON_NESTING_DEPTH = 256
_DARWIN_SYSTEM_ALIASES = {
    "tmp": Path("/private/tmp"),
    "var": Path("/private/var"),
}


class ArtifactIOError(Exception):
    """Base class for a mechanical artifact boundary failure."""


class ArtifactCanonicalJsonError(ArtifactIOError):
    """The value cannot be represented as canonical finite JSON."""


class ArtifactJsonDecodeError(ArtifactIOError):
    """The encoded artifact is not valid UTF-8 JSON."""


class ArtifactDuplicateKeyError(ArtifactJsonDecodeError):
    """JSON contains a duplicate object key."""

    def __init__(self, key: str) -> None:
        self.key = key
        super().__init__(key)


class ArtifactNonFiniteConstantError(ArtifactJsonDecodeError):
    """JSON contains NaN or Infinity."""

    def __init__(self, constant: str) -> None:
        self.constant = constant
        super().__init__(constant)


class ArtifactPathError(ArtifactIOError):
    """Base class for a path-specific artifact failure."""

    def __init__(self, path: Path) -> None:
        self.path = path
        super().__init__(str(path))


class ArtifactNotFoundError(ArtifactPathError):
    """The artifact path does not exist."""


class ArtifactNotRegularError(ArtifactPathError):
    """The artifact path is not a regular file (including symlinks)."""


class ArtifactNotDirectoryError(ArtifactPathError):
    """The artifact parent is not a real directory (including symlinks)."""


class ArtifactReadError(ArtifactPathError):
    """The artifact could not be read safely."""


class ArtifactTooLargeError(ArtifactPathError):
    """The artifact exceeds its configured byte limit."""

    def __init__(self, path: Path, *, max_bytes: int) -> None:
        self.max_bytes = max_bytes
        super().__init__(path)


class ArtifactChangedError(ArtifactPathError):
    """The file identity or metadata changed during a guarded read."""

    def __init__(self, path: Path, *, stage: str) -> None:
        self.stage = stage
        super().__init__(path)


class ArtifactContentConflictError(ArtifactPathError):
    """An immutable target already exists with different bytes."""


class ArtifactPublishConflictError(ArtifactPathError):
    """Another publisher won the exclusive target-name race."""


def canonical_json_text(value: object) -> str:
    """Return UTF-8-preserving, finite JSON with stable keys and spacing."""
    try:
        _validate_json_tree(value)
        return json.dumps(
            value,
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
            allow_nan=False,
        )
    except ArtifactCanonicalJsonError:
        raise
    except (TypeError, ValueError, RecursionError) as exc:  # defensive after tree validation
        raise ArtifactCanonicalJsonError from exc


def canonical_json_bytes(value: object) -> bytes:
    """Return the canonical JSON UTF-8 byte representation."""
    return canonical_json_text(value).encode("utf-8")


def sha256_hex(value: str | bytes) -> str:
    """Return a lowercase SHA-256 digest for text or bytes."""
    encoded = value.encode("utf-8") if isinstance(value, str) else value
    return hashlib.sha256(encoded).hexdigest()


def decode_json_bytes(encoded: bytes) -> object:
    """Decode UTF-8 JSON while rejecting duplicate keys and non-finite constants."""
    try:
        text = encoded.decode("utf-8")
        _validate_json_text_nesting(text)
        return json.loads(
            text,
            object_pairs_hook=_unique_object,
            parse_constant=_reject_nonfinite_constant,
        )
    except ArtifactJsonDecodeError:
        raise
    except (UnicodeError, json.JSONDecodeError, RecursionError) as exc:
        raise ArtifactJsonDecodeError from exc


def read_regular_file(path: str | Path, *, max_bytes: int) -> bytes:
    """Read at most ``max_bytes`` from one stable, non-symlink regular file."""
    if max_bytes < 0:
        raise ValueError("max_bytes must be non-negative")
    source = Path(path).expanduser().absolute()
    _reject_path_alias(source, ArtifactNotRegularError)
    descriptor: int | None = None
    try:
        descriptor, opened = _open_regular_file(source, max_bytes=max_bytes)
        return _read_open_file(source, descriptor, opened, max_bytes=max_bytes)
    except ArtifactIOError:
        raise
    except FileNotFoundError as exc:
        raise ArtifactNotFoundError(source) from exc
    except OSError as exc:
        raise ArtifactReadError(source) from exc
    finally:
        if descriptor is not None:
            os.close(descriptor)


def exclusive_atomic_publish(
    path: str | Path,
    encoded: bytes,
    *,
    max_bytes: int,
    before_publish: Callable[[], None] | None = None,
) -> bool:
    """Publish immutable bytes by hard link; return false for an exact repeat."""
    target = Path(path).expanduser().absolute()
    try:
        if len(encoded) > max_bytes:
            raise ArtifactTooLargeError(target, max_bytes=max_bytes)
        _reject_path_alias(target.parent, ArtifactNotDirectoryError)
        target.parent.mkdir(parents=True, exist_ok=True)
        directory_descriptor, directory_facts = _open_stable_directory(target.parent)
        try:
            if _directory_descriptor_publish_supported():
                return _publish_at_directory_descriptor(
                    target,
                    encoded,
                    max_bytes=max_bytes,
                    directory_descriptor=directory_descriptor,
                    directory_facts=directory_facts,
                    before_publish=before_publish,
                )
            return _publish_with_guarded_paths(
                target,
                encoded,
                max_bytes=max_bytes,
                directory_descriptor=directory_descriptor,
                directory_facts=directory_facts,
                before_publish=before_publish,
            )
        finally:
            os.close(directory_descriptor)
    except ArtifactIOError:
        raise
    except OSError as exc:
        raise ArtifactReadError(target) from exc


def content_addressed_filename(
    prefix: str,
    identifiers: Sequence[str | int],
    digest: str,
    suffix: str,
) -> str:
    """Join validated filename components around one lowercase SHA-256 address."""
    components = [prefix, *(str(value) for value in identifiers), digest]
    filename_parts = [*components, suffix]
    if (
        not prefix
        or not suffix.startswith(".")
        or _SHA256.fullmatch(digest) is None
        or any(not value or "/" in value or "\\" in value for value in filename_parts)
    ):
        raise ValueError("invalid content-addressed filename components")
    return "-".join(components) + suffix


def path_has_only_trusted_aliases(path: Path) -> bool:
    """Accept only lexical paths or exact Darwin /tmp and /var system aliases."""
    normalized = path.absolute()
    trusted_lexical = _trusted_system_alias_path(normalized)
    return normalized.resolve(strict=False) == trusted_lexical


def _reject_path_alias(
    path: Path,
    error_type: type[ArtifactPathError],
) -> None:
    try:
        trusted = path_has_only_trusted_aliases(path)
    except (OSError, RuntimeError) as exc:
        raise ArtifactReadError(path) from exc
    if not trusted:
        raise error_type(path)


def _trusted_system_alias_path(path: Path) -> Path:
    if sys.platform != "darwin" or len(path.parts) < 2:
        return path
    alias_name = path.parts[1]
    expected = _DARWIN_SYSTEM_ALIASES.get(alias_name)
    if expected is None:
        return path
    alias = Path("/") / alias_name
    facts = alias.lstat()
    if not stat.S_ISLNK(facts.st_mode):
        return path
    link_target = Path(os.readlink(alias))
    observed = link_target if link_target.is_absolute() else alias.parent / link_target
    if observed != expected:
        return path
    return expected.joinpath(*path.parts[2:])


def _validate_json_tree(value: object) -> None:
    pending = [(value, 0)]
    while pending:
        item, depth = pending.pop()
        if isinstance(item, dict):
            if depth >= _MAX_JSON_NESTING_DEPTH or any(
                not isinstance(key, str) for key in item
            ):
                raise ArtifactCanonicalJsonError
            pending.extend((child, depth + 1) for child in item.values())
            continue
        if isinstance(item, list):
            if depth >= _MAX_JSON_NESTING_DEPTH:
                raise ArtifactCanonicalJsonError
            pending.extend((child, depth + 1) for child in item)
            continue
        if item is None or isinstance(item, str | bool | int):
            continue
        if isinstance(item, float) and math.isfinite(item):
            continue
        raise ArtifactCanonicalJsonError


def _validate_json_text_nesting(text: str) -> None:
    depth = 0
    in_string = False
    escaped = False
    for character in text:
        if in_string:
            if escaped:
                escaped = False
            elif character == "\\":
                escaped = True
            elif character == '"':
                in_string = False
            continue
        if character == '"':
            in_string = True
            continue
        if character in "[{":
            depth += 1
            if depth > _MAX_JSON_NESTING_DEPTH:
                raise ArtifactJsonDecodeError("JSON nesting exceeds the supported limit")
        elif character in "]}":
            depth -= 1


def _unique_object(pairs: list[tuple[str, object]]) -> dict[str, object]:
    output: dict[str, object] = {}
    for key, value in pairs:
        if key in output:
            raise ArtifactDuplicateKeyError(key)
        output[key] = value
    return output


def _reject_nonfinite_constant(value: str) -> object:
    raise ArtifactNonFiniteConstantError(value)


def _open_regular_file(path: Path, *, max_bytes: int) -> tuple[int, os.stat_result]:
    facts = path.lstat()
    _validate_regular_stat(path, facts, max_bytes=max_bytes)
    descriptor = os.open(path, os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0))
    try:
        opened = os.fstat(descriptor)
        _validate_regular_stat(path, opened, max_bytes=max_bytes)
        if (opened.st_dev, opened.st_ino) != (facts.st_dev, facts.st_ino):
            raise ArtifactChangedError(path, stage="open")
        return descriptor, opened
    except Exception:
        os.close(descriptor)
        raise


def _validate_regular_stat(path: Path, facts: os.stat_result, *, max_bytes: int) -> None:
    if not stat.S_ISREG(facts.st_mode):
        raise ArtifactNotRegularError(path)
    if facts.st_size > max_bytes:
        raise ArtifactTooLargeError(path, max_bytes=max_bytes)


def _read_open_file(path: Path, descriptor: int, opened: os.stat_result, *, max_bytes: int) -> bytes:
    chunks: list[bytes] = []
    remaining = max_bytes + 1
    while remaining:
        chunk = os.read(descriptor, min(_READ_CHUNK_BYTES, remaining))
        if not chunk:
            break
        chunks.append(chunk)
        remaining -= len(chunk)
    encoded = b"".join(chunks)
    finished = os.fstat(descriptor)
    if len(encoded) > max_bytes:
        raise ArtifactTooLargeError(path, max_bytes=max_bytes)
    if _stat_identity(opened) != _stat_identity(finished):
        raise ArtifactChangedError(path, stage="read")
    return encoded


def _stat_identity(facts: os.stat_result) -> tuple[int, int, int, int, int, int]:
    return (
        facts.st_dev,
        facts.st_ino,
        facts.st_mode,
        facts.st_size,
        facts.st_mtime_ns,
        facts.st_ctime_ns,
    )


def _existing_bytes(path: Path, *, max_bytes: int) -> bytes | None:
    try:
        return read_regular_file(path, max_bytes=max_bytes)
    except ArtifactNotFoundError:
        return None


def _open_stable_directory(path: Path) -> tuple[int, os.stat_result]:
    try:
        facts = path.lstat()
        _validate_directory_stat(path, facts)
        flags = os.O_RDONLY | getattr(os, "O_DIRECTORY", 0) | getattr(os, "O_NOFOLLOW", 0)
        descriptor = os.open(path, flags)
    except ArtifactIOError:
        raise
    except FileNotFoundError as exc:
        raise ArtifactNotFoundError(path) from exc
    except OSError as exc:
        raise ArtifactReadError(path) from exc
    try:
        opened = os.fstat(descriptor)
        _validate_directory_stat(path, opened)
        if (opened.st_dev, opened.st_ino) != (facts.st_dev, facts.st_ino):
            raise ArtifactChangedError(path, stage="parent_open")
        return descriptor, opened
    except Exception:
        os.close(descriptor)
        raise


def _validate_directory_stat(path: Path, facts: os.stat_result) -> None:
    if not stat.S_ISDIR(facts.st_mode):
        raise ArtifactNotDirectoryError(path)


def _validate_directory_identity(path: Path, opened: os.stat_result, *, stage: str) -> None:
    try:
        facts = path.lstat()
    except OSError as exc:
        raise ArtifactChangedError(path, stage=stage) from exc
    if not stat.S_ISDIR(facts.st_mode) or (facts.st_dev, facts.st_ino) != (opened.st_dev, opened.st_ino):
        raise ArtifactChangedError(path, stage=stage)


def _directory_descriptor_publish_supported() -> bool:
    return all(function in os.supports_dir_fd for function in (os.open, os.stat, os.link, os.unlink))


def _publish_at_directory_descriptor(
    target: Path,
    encoded: bytes,
    *,
    max_bytes: int,
    directory_descriptor: int,
    directory_facts: os.stat_result,
    before_publish: Callable[[], None] | None,
) -> bool:
    existing = _existing_bytes_at(target, directory_descriptor, max_bytes=max_bytes)
    _validate_directory_identity(target.parent, directory_facts, stage="parent_existing")
    if existing is not None:
        if existing == encoded:
            return False
        raise ArtifactContentConflictError(target)
    temporary_name = _write_temporary_at(target, encoded, directory_descriptor)
    linked = False
    try:
        if before_publish is not None:
            before_publish()
        _validate_directory_identity(target.parent, directory_facts, stage="parent_before_publish")
        _link_exclusively_at(temporary_name, target, directory_descriptor)
        linked = True
        _validate_directory_identity(target.parent, directory_facts, stage="parent_after_publish")
        os.fsync(directory_descriptor)
    except BaseException:
        if linked:
            _unlink_at(target.name, directory_descriptor)
        raise
    finally:
        _unlink_at(temporary_name, directory_descriptor)
    return True


def _existing_bytes_at(path: Path, directory_descriptor: int, *, max_bytes: int) -> bytes | None:
    try:
        facts = os.stat(path.name, dir_fd=directory_descriptor, follow_symlinks=False)
    except FileNotFoundError:
        return None
    except OSError as exc:
        raise ArtifactReadError(path) from exc
    _validate_regular_stat(path, facts, max_bytes=max_bytes)
    descriptor: int | None = None
    try:
        descriptor = os.open(
            path.name,
            os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0),
            dir_fd=directory_descriptor,
        )
        opened = os.fstat(descriptor)
        _validate_regular_stat(path, opened, max_bytes=max_bytes)
        if (opened.st_dev, opened.st_ino) != (facts.st_dev, facts.st_ino):
            raise ArtifactChangedError(path, stage="open")
        return _read_open_file(path, descriptor, opened, max_bytes=max_bytes)
    except ArtifactIOError:
        raise
    except FileNotFoundError as exc:
        raise ArtifactChangedError(path, stage="open") from exc
    except OSError as exc:
        raise ArtifactReadError(path) from exc
    finally:
        if descriptor is not None:
            os.close(descriptor)


def _write_temporary_at(target: Path, encoded: bytes, directory_descriptor: int) -> str:
    for _attempt in range(100):
        name = f".{target.name}.{secrets.token_hex(8)}.tmp"
        try:
            descriptor = os.open(
                name,
                os.O_WRONLY | os.O_CREAT | os.O_EXCL,
                0o600,
                dir_fd=directory_descriptor,
            )
        except FileExistsError:
            continue
        except OSError as exc:
            raise ArtifactReadError(target) from exc
        try:
            with os.fdopen(descriptor, "wb") as stream:
                stream.write(encoded)
                stream.flush()
                os.fsync(stream.fileno())
        except Exception:
            _unlink_at(name, directory_descriptor)
            raise
        return name
    raise ArtifactPublishConflictError(target)


def _link_exclusively_at(temporary_name: str, target: Path, directory_descriptor: int) -> None:
    try:
        os.link(
            temporary_name,
            target.name,
            src_dir_fd=directory_descriptor,
            dst_dir_fd=directory_descriptor,
            follow_symlinks=False,
        )
    except FileExistsError as exc:
        raise ArtifactPublishConflictError(target) from exc
    except OSError as exc:
        raise ArtifactReadError(target) from exc


def _unlink_at(name: str, directory_descriptor: int) -> None:
    try:
        os.unlink(name, dir_fd=directory_descriptor)
    except FileNotFoundError:
        pass


def _publish_with_guarded_paths(
    target: Path,
    encoded: bytes,
    *,
    max_bytes: int,
    directory_descriptor: int,
    directory_facts: os.stat_result,
    before_publish: Callable[[], None] | None,
) -> bool:
    existing = _existing_bytes(target, max_bytes=max_bytes)
    _validate_directory_identity(target.parent, directory_facts, stage="parent_existing")
    if existing is not None:
        if existing == encoded:
            return False
        raise ArtifactContentConflictError(target)
    temporary = _write_temporary(target, encoded)
    linked = False
    try:
        if before_publish is not None:
            before_publish()
        _validate_directory_identity(target.parent, directory_facts, stage="parent_before_publish")
        _link_exclusively(temporary, target)
        linked = True
        _validate_directory_identity(target.parent, directory_facts, stage="parent_after_publish")
        os.fsync(directory_descriptor)
    except BaseException:
        if linked:
            _validate_directory_identity(target.parent, directory_facts, stage="parent_rollback")
            target.unlink(missing_ok=True)
        raise
    finally:
        temporary.unlink(missing_ok=True)
    return True


def _write_temporary(target: Path, encoded: bytes) -> Path:
    descriptor, temporary_name = tempfile.mkstemp(
        prefix=f".{target.name}.",
        suffix=".tmp",
        dir=target.parent,
    )
    temporary = Path(temporary_name)
    try:
        with os.fdopen(descriptor, "wb") as stream:
            stream.write(encoded)
            stream.flush()
            os.fsync(stream.fileno())
    except Exception:
        temporary.unlink(missing_ok=True)
        raise
    return temporary


def _link_exclusively(temporary: Path, target: Path) -> None:
    try:
        os.link(temporary, target)
    except FileExistsError as exc:
        raise ArtifactPublishConflictError(target) from exc


def _fsync_directory(directory: Path) -> None:
    descriptor = os.open(directory, os.O_RDONLY)
    try:
        os.fsync(descriptor)
    finally:
        os.close(descriptor)
