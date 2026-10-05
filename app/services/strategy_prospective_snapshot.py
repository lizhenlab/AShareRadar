"""Self-contained, bounded archives of the original signal input, without labels."""

from __future__ import annotations

from collections.abc import Callable, Mapping
import gzip
import hashlib
import io
from pathlib import Path
import sqlite3
import zlib
from typing import Protocol

from app.artifacts.io import canonical_json_bytes, decode_json_bytes, exclusive_atomic_publish, read_regular_file, sha256_hex
from app.services.market_scan_trial_registry_contract import registry_hash, registry_keys, registry_object
from app.services.strategy_prospective_store import SNAPSHOT_PATTERN


MAX_SNAPSHOT_BYTES = 64 * 1024 * 1024
MAX_SNAPSHOT_RAW_BYTES = 512 * 1024 * 1024
MAX_SNAPSHOT_ROWS = 10_000


class _DigestWriter(Protocol):
    def update(self, data: bytes) -> None: ...


def archive_strategy_scan(conn: sqlite3.Connection, row: sqlite3.Row, directory: Path) -> dict[str, object]:
    """Retain every result including missing/skipped rows in the caller's read transaction."""
    day = str(row["data_date"])
    contract = conn.execute("SELECT * FROM market_scan_rule_contract WHERE rule_version = ?", (row["rule_version"],)).fetchone()
    buffer = io.BytesIO()
    digest = hashlib.sha256()
    raw_bytes, count = 0, 0
    header = {"schema_version": "strategy-prospective-source-jsonl-v1", "kind": "header",
              "run": dict(row), "rule_contract": dict(contract) if contract is not None else None,
              "forward_prices_read": False}
    with gzip.GzipFile(fileobj=buffer, mode="wb", mtime=0) as writer:
        raw_bytes += _write_record(writer, header, digest, raw_bytes)
        cursor = conn.execute("SELECT * FROM market_scan_result WHERE run_id = ? ORDER BY rank, symbol", (row["id"],))
        for result in cursor:
            count += 1
            if count > MAX_SNAPSHOT_ROWS:
                raise ValueError("prospective snapshot has too many source rows")
            raw_bytes += _write_record(writer, {"kind": "result", "row": dict(result)}, digest, raw_bytes)
            if buffer.tell() > MAX_SNAPSHOT_BYTES:
                raise ValueError("prospective compressed snapshot exceeds its limit")
    encoded = buffer.getvalue()
    compressed_digest = sha256_hex(encoded)
    name = f"snapshot-{day}-{compressed_digest}.json.gz"
    exclusive_atomic_publish(directory / name, encoded, max_bytes=MAX_SNAPSHOT_BYTES)
    return {"snapshot_path": name, "snapshot_file_sha256": compressed_digest,
            "snapshot_raw_bytes_sha256": digest.hexdigest(), "snapshot_raw_byte_size": raw_bytes,
            "snapshot_byte_size": len(encoded), "snapshot_result_count": count}


def _write_record(writer: gzip.GzipFile, record: Mapping[str, object], digest: _DigestWriter, used: int) -> int:
    encoded = canonical_json_bytes(dict(record)) + b"\n"
    if used + len(encoded) > MAX_SNAPSHOT_RAW_BYTES:
        raise ValueError("prospective uncompressed snapshot exceeds its limit")
    writer.write(encoded)
    digest.update(encoded)
    return len(encoded)


def verify_strategy_scan_archive(directory: Path, payload: Mapping[str, object]) -> None:
    """Validate all original bytes, the source header, and every retained result row."""
    visit_strategy_scan_archive(directory, payload, on_header=lambda _header: None, on_result=lambda _row: None)


def visit_strategy_scan_archive(
    directory: Path, payload: Mapping[str, object], *,
    on_header: Callable[[dict[str, object]], None], on_result: Callable[[dict[str, object]], None],
) -> None:
    """Stream bounded source records; callers must discard their work on any failure."""
    encoded = _archive_bytes(directory, payload)
    try:
        with gzip.GzipFile(fileobj=io.BytesIO(encoded)) as reader:
            _visit_records(reader, payload, on_header, on_result)
    except (EOFError, gzip.BadGzipFile, zlib.error) as exc:
        raise ValueError("prospective source gzip is invalid") from exc


def _archive_bytes(directory: Path, payload: Mapping[str, object]) -> bytes:
    name = payload.get("snapshot_path")
    if not isinstance(name, str) or SNAPSHOT_PATTERN.fullmatch(name) is None:
        raise ValueError("invalid prospective source archive path")
    expected = registry_hash(payload.get("snapshot_file_sha256"), "snapshot file digest")
    registry_hash(payload.get("snapshot_raw_bytes_sha256"), "snapshot raw digest")
    for key, maximum in (("snapshot_byte_size", MAX_SNAPSHOT_BYTES), ("snapshot_raw_byte_size", MAX_SNAPSHOT_RAW_BYTES),
                         ("snapshot_result_count", MAX_SNAPSHOT_ROWS)):
        value = payload.get(key)
        if type(value) is not int or not 0 <= value <= maximum:
            raise ValueError("invalid prospective source archive bounds")
    if not name.endswith(f"-{expected}.json.gz"):
        raise ValueError("prospective source archive name digest mismatch")
    encoded = read_regular_file(directory / name, max_bytes=MAX_SNAPSHOT_BYTES)
    if sha256_hex(encoded) != expected or len(encoded) != payload["snapshot_byte_size"]:
        raise ValueError("prospective source archive digest mismatch")
    return encoded


def _visit_records(
    reader: gzip.GzipFile, payload: Mapping[str, object], on_header: Callable[[dict[str, object]], None],
    on_result: Callable[[dict[str, object]], None],
) -> None:
    digest, length, count = hashlib.sha256(), 0, 0
    seen: set[str] = set()
    header = None
    while line := reader.readline(MAX_SNAPSHOT_RAW_BYTES - length + 1):
        length += len(line)
        if length > MAX_SNAPSHOT_RAW_BYTES or not line.endswith(b"\n"):
            raise ValueError("prospective source archive decompression exceeds limit or record is incomplete")
        digest.update(line)
        record = registry_object(decode_json_bytes(line), "source record")
        if header is None:
            header = _source_header(record, payload)
            on_header(header)
        else:
            count += 1
            if count > MAX_SNAPSHOT_ROWS:
                raise ValueError("prospective snapshot has too many source rows")
            on_result(_source_result(record, payload, seen))
    if header is None or count != payload["snapshot_result_count"]:
        raise ValueError("prospective source archive row count mismatch")
    if digest.hexdigest() != payload["snapshot_raw_bytes_sha256"] or length != payload["snapshot_raw_byte_size"]:
        raise ValueError("prospective source raw bytes mismatch")


def _source_header(record: dict[str, object], payload: Mapping[str, object]) -> dict[str, object]:
    registry_keys(record, frozenset({"schema_version", "kind", "run", "rule_contract", "forward_prices_read"}), "source header")
    if (record["schema_version"], record["kind"]) != ("strategy-prospective-source-jsonl-v1", "header") or record["forward_prices_read"] is not False:
        raise ValueError("prospective source header contract mismatch")
    run = registry_object(record["run"], "source run")
    bindings = {"id": "run_id", "data_date": "data_date", "rule_version": "rule_version", "snapshot_digest": "source_snapshot_digest",
                "declared_score_spec_hash": "score_spec_hash"}
    if any(key not in run or target not in payload or run[key] != payload[target] for key, target in bindings.items()):
        raise ValueError("prospective source header identity mismatch")
    counts = registry_object(payload.get("source_result_counts"), "source result counts")
    if set(counts) != {"total_count", "success_count", "missing_count", "skipped_count"} or any(run.get(key) != value for key, value in counts.items()):
        raise ValueError("prospective source header counts mismatch")
    if not str(payload["snapshot_path"]).startswith(f"snapshot-{run.get('data_date')}-"):
        raise ValueError("prospective source header date mismatch")
    return record


def _source_result(record: dict[str, object], payload: Mapping[str, object], seen: set[str]) -> dict[str, object]:
    registry_keys(record, frozenset({"kind", "row"}), "source result")
    row = registry_object(record["row"], "source result row")
    symbol = row.get("symbol")
    if record["kind"] != "result" or row.get("run_id") != payload["run_id"]:
        raise ValueError("prospective source result run mismatch")
    if not isinstance(symbol, str) or not symbol or symbol in seen:
        raise ValueError("prospective source result symbol is invalid or duplicate")
    seen.add(symbol)
    return row
