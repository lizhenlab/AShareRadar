"""Bounded immutable records and strict namespaces for prospective collection."""

from __future__ import annotations

import base64
import binascii
from collections.abc import Mapping
from pathlib import Path
import re
import stat
from typing import cast

from app.artifacts.io import (
    canonical_json_bytes, decode_json_bytes, exclusive_atomic_publish, path_has_only_trusted_aliases, read_regular_file, sha256_hex,
)
from app.services.market_scan_prospective_contract import MAX_INPUT_BYTES, MAX_RECORD_BYTES
from app.services.market_scan_trial_registry_contract import registry_hash, registry_identifier, registry_keys, registry_object


PLAN_KEYS = frozenset({"schema_version", "plan_id", "recorded_at", "specification", "calendar_sha256", "calendar_base64", "digest"})
RECEIPT_KEYS = frozenset({
    "schema_version", "plan_digest", "sequence", "previous_digest", "trade_date", "recorded_at", "status", "reason",
    "input_sha256", "input_base64", "available_at", "actual_run_id", "digest",
})
SEAL_KEYS = frozenset({"schema_version", "plan_digest", "head_digest", "receipt_count", "recorded_at", "statuses", "digest"})
RESULT_KEYS = frozenset({"schema_version", "plan_digest", "input_seal_digest", "recorded_at", "result_sha256", "result_base64", "digest"})
_RECEIPT_NAME = re.compile(r"receipt-([0-9]{8})\.json")
_FINAL_NAME = r"(?:plan|input-seal|result|receipt-[0-9]{8})\.json"
_STAGING_NAME = re.compile(rf"\.{_FINAL_NAME}\.[0-9a-f]{{16}}\.tmp")


def plan_directory(root: Path, plan_id: str) -> Path:
    directory = Path(root).expanduser().absolute() / registry_identifier(plan_id, "plan_id")
    if not path_has_only_trusted_aliases(directory):
        raise ValueError("prospective plan path cannot traverse symlinks or aliases")
    return directory


def namespace_files(directory: Path) -> set[str]:
    if not path_has_only_trusted_aliases(directory):
        raise ValueError("prospective namespace path changed")
    names: set[str] = set()
    for child in directory.iterdir():
        facts = child.lstat()
        if not stat.S_ISREG(facts.st_mode) or facts.st_size > MAX_RECORD_BYTES:
            raise ValueError("prospective namespace contains an unsafe entry")
        name = child.name
        if name not in {"plan.json", "input-seal.json", "result.json", ".writer.lock"} and not _RECEIPT_NAME.fullmatch(name):
            if not _STAGING_NAME.fullmatch(name):
                raise ValueError(f"unexpected prospective namespace entry: {name}")
            continue  # A killed atomic writer may leave an uncommitted, non-admissible temporary file.
        names.add(name)
    return names


def digested_record(payload: Mapping[str, object]) -> dict[str, object]:
    return {**payload, "digest": sha256_hex(canonical_json_bytes(dict(payload)))}


def publish_record(directory: Path, filename: str, record: Mapping[str, object]) -> None:
    exclusive_atomic_publish(directory / filename, canonical_json_bytes(dict(record)), max_bytes=MAX_RECORD_BYTES)


def read_record(directory: Path, filename: str, keys: frozenset[str], version: str, *, max_bytes: int = MAX_RECORD_BYTES) -> dict[str, object]:
    record = registry_object(decode_json_bytes(read_regular_file(directory / filename, max_bytes=max_bytes)), filename)
    registry_keys(record, keys, filename)
    expected = registry_hash(record["digest"], "record digest")
    observed = sha256_hex(canonical_json_bytes({key: value for key, value in record.items() if key != "digest"}))
    if record["schema_version"] != version or expected != observed:
        raise ValueError(f"{filename}: version or digest mismatch")
    return record


def receipt_names(names: set[str]) -> list[str]:
    ordered = sorted(name for name in names if _RECEIPT_NAME.fullmatch(name))
    if ordered != [f"receipt-{index:08d}.json" for index in range(1, len(ordered) + 1)]:
        raise ValueError("receipt sequence is incomplete")
    return ordered


def encode_raw(encoded: bytes) -> str:
    if len(encoded) > MAX_INPUT_BYTES:
        raise ValueError("raw input exceeds the bounded receipt size")
    return base64.b64encode(encoded).decode("ascii")


def decode_raw(value: object, digest: object) -> bytes:
    if not isinstance(value, str) or len(value) > (MAX_INPUT_BYTES + 2) // 3 * 4:
        raise ValueError("raw receipt encoding is invalid or oversized")
    try:
        encoded = base64.b64decode(value, validate=True)
    except (ValueError, binascii.Error) as exc:
        raise ValueError("raw receipt must use valid base64") from exc
    if len(encoded) > MAX_INPUT_BYTES or sha256_hex(encoded) != registry_hash(digest, "raw input digest"):
        raise ValueError("raw receipt digest mismatch")
    return encoded


def check_anchor(observed: object, expected: str | None, name: str) -> None:
    if expected is not None and registry_hash(expected, name) != observed:
        raise ValueError(f"{name} does not match the retained digest anchor")


def record_digest(record: Mapping[str, object]) -> str:
    return cast(str, record["digest"])
