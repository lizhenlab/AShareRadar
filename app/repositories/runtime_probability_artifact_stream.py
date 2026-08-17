"""Bounded-memory validation for large probability research artifacts."""

from collections.abc import Mapping
from dataclasses import dataclass, field
import hashlib
from pathlib import Path
import re
from typing import Any

from app.artifacts.io import canonical_json_text, decode_json_bytes


class RuntimeCleanupIntegrityError(RuntimeError):
    """Cleanup could not prove that deleting a persisted audit graph is safe."""


@dataclass
class _ProbabilityStreamState:
    buffer: bytes = b""
    scan_tail: bytes = b""
    digest_tail: bytes = b""
    manifest: tuple[int, ...] | None = None
    scalar_ids: set[int] = field(default_factory=set)
    semantic_digest: Any = None
    raw_digest: Any = None
    generated_at_encoded: bytes = b""
    digest_scope: str = ""
    payload_started: bool = False


def stream_probability_run_ids(
    path: Path,
    filename_run_id: int,
    filename_digest: str,
    expected_size: int,
) -> set[int]:
    """Verify a canonical large probability artifact without materializing JSON."""

    suffix = b',"schema_version":"market-scan-probability-artifact-v1"}'
    state = _ProbabilityStreamState()
    total = 0
    try:
        with path.open("rb") as source:
            for chunk in iter(lambda: source.read(1024 * 1024), b""):
                total += len(chunk)
                state.buffer += chunk
                _start_probability_payload(state, suffix, filename_digest)
                _drain_probability_payload(state, suffix)
    except OSError as exc:
        raise RuntimeCleanupIntegrityError("超大 probability artifact 无法流式读取") from exc
    if total != expected_size:
        raise RuntimeCleanupIntegrityError("超大 probability artifact 文件大小在读取期间变化")
    return _finish_probability_payload(state, suffix, filename_run_id, filename_digest)


def _start_probability_payload(state: _ProbabilityStreamState, suffix: bytes, filename_digest: str) -> None:
    if state.payload_started:
        return
    marker = b',"payload":'
    marker_at = state.buffer.find(marker)
    if marker_at < 0:
        state.buffer = state.buffer[-len(marker) :]
        return
    header = decode_json_bytes(state.buffer[:marker_at] + b',"payload":null' + suffix)
    _require_probability_stream_header(header, filename_digest)
    assert isinstance(header, Mapping) and isinstance(header["integrity"], Mapping)
    state.digest_scope = str(header["integrity"]["scope"])
    state.generated_at_encoded = canonical_json_text(header["generated_at"]).encode()
    state.semantic_digest = hashlib.sha256()
    state.raw_digest = hashlib.sha256()
    if state.digest_scope == "generated_at+payload":
        state.semantic_digest.update(b'{"generated_at":' + state.generated_at_encoded + b',"payload":')
    state.buffer = state.buffer[marker_at + len(marker) :]
    state.payload_started = True


def _require_probability_stream_header(header: Any, filename_digest: str) -> None:
    if not isinstance(header, Mapping) or set(header) != {"generated_at", "integrity", "payload", "schema_version"}:
        raise RuntimeCleanupIntegrityError("超大 probability artifact header 无效")
    integrity = header.get("integrity")
    valid = (
        header.get("schema_version") == "market-scan-probability-artifact-v1"
        and isinstance(header.get("generated_at"), str)
        and isinstance(integrity, Mapping)
        and set(integrity) == {"algorithm", "scope", "integrity_digest", "notice"}
        and integrity.get("algorithm") == "sha256"
        and integrity.get("scope") in {"payload", "generated_at+payload"}
        and integrity.get("integrity_digest") == filename_digest
        and integrity.get("notice") == "integrity_digest_not_a_signature"
    )
    if not valid:
        raise RuntimeCleanupIntegrityError("超大 probability artifact header digest 无效")


def _drain_probability_payload(state: _ProbabilityStreamState, suffix: bytes) -> None:
    if not state.payload_started:
        return
    while len(state.buffer) > len(suffix) + 1024 * 1024:
        consumed, state.buffer = state.buffer[: 1024 * 1024], state.buffer[1024 * 1024 :]
        _consume_probability_payload(state, consumed, final=False)


def _consume_probability_payload(state: _ProbabilityStreamState, consumed: bytes, *, final: bool) -> None:
    scan = state.scan_tail + consumed
    state.manifest = _scan_probability_manifest(scan, state.manifest, state.scalar_ids)
    state.scan_tail = b"" if final else _probability_scan_tail(scan)
    filtered, state.digest_tail = _filter_probability_generated_at(
        state.digest_tail + consumed,
        state.generated_at_encoded,
        final=final,
    )
    state.raw_digest.update(consumed)
    state.semantic_digest.update(filtered)


def _probability_scan_tail(scan: bytes) -> bytes:
    marker = b'"artifact_set_run_ids":['
    marker_at = scan.rfind(marker)
    if marker_at >= 0 and b"]" not in scan[marker_at + len(marker) :]:
        tail = scan[marker_at:]
        if len(tail) > 4 * 1024 * 1024:
            raise RuntimeCleanupIntegrityError("超大 probability artifact manifest 超过流式校验预算")
        return tail
    return scan[-512:]


def _finish_probability_payload(
    state: _ProbabilityStreamState,
    suffix: bytes,
    filename_run_id: int,
    filename_digest: str,
) -> set[int]:
    if not state.payload_started or not state.buffer.endswith(suffix):
        raise RuntimeCleanupIntegrityError("超大 probability artifact canonical envelope 无效")
    _consume_probability_payload(state, state.buffer[: -len(suffix)], final=True)
    if state.digest_scope == "generated_at+payload":
        state.semantic_digest.update(b"}")
    raw_legacy = state.digest_scope == "payload" and state.raw_digest.hexdigest() == filename_digest
    if state.semantic_digest.hexdigest() != filename_digest and not raw_legacy:
        raise RuntimeCleanupIntegrityError("超大 probability artifact semantic digest 不一致")
    if state.manifest is None:
        if state.scalar_ids != {filename_run_id}:
            raise RuntimeCleanupIntegrityError("超大 probability artifact single-run manifest 不守恒")
        return {filename_run_id}
    if filename_run_id not in state.manifest or not state.scalar_ids.issubset(state.manifest):
        raise RuntimeCleanupIntegrityError("超大 probability artifact run manifest 不守恒")
    return set(state.manifest)


def _filter_probability_generated_at(value: bytes, expected: bytes, *, final: bool) -> tuple[bytes, bytes]:
    keep = 256
    if final:
        body, tail = value, b""
    elif len(value) <= keep:
        body, tail = b"", value
    else:
        cut = value.rfind(b",", 0, len(value) - keep) + 1
        body, tail = value[:cut], value[cut:]
    occurrences = body.count(b'"generated_at":')
    values = re.findall(rb'"generated_at":("[^"\\]+"),', body)
    if len(values) != occurrences or any(value != expected for value in values):
        raise RuntimeCleanupIntegrityError("超大 probability artifact generated_at 编码无效")
    return re.sub(rb'"generated_at":"[^"\\]+",', b"", body), tail


def _scan_probability_manifest(
    chunk: bytes,
    expected: tuple[int, ...] | None,
    scalar_ids: set[int],
) -> tuple[int, ...] | None:
    for matched in re.finditer(rb'"artifact_set_run_ids":\[([0-9,]+)\]', chunk):
        values = tuple(int(value) for value in matched.group(1).split(b","))
        if not values or values != tuple(sorted(set(values))) or (expected is not None and values != expected):
            raise RuntimeCleanupIntegrityError("超大 probability artifact manifest 冲突")
        expected = values
    scalar_ids.update(int(item) for item in re.findall(rb'"run_id":([1-9][0-9]*)', chunk))
    return expected


__all__ = ["RuntimeCleanupIntegrityError", "stream_probability_run_ids"]
