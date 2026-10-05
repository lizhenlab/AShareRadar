"""Bounded immutable storage for fixed-template forward collection."""

from __future__ import annotations

import base64
import binascii
from collections.abc import Mapping
from pathlib import Path
import re
import stat

from app.artifacts.io import (
    canonical_json_bytes, decode_json_bytes, exclusive_atomic_publish,
    path_has_only_trusted_aliases, read_regular_file, sha256_hex,
)
from app.services.market_scan_trial_registry_contract import (
    registry_hash, registry_identifier, registry_keys, registry_object,
)


MAX_PLAN_BYTES = 4 * 1024 * 1024
MAX_RECEIPT_BYTES = 48 * 1024 * 1024
PLAN_KEYS = frozenset({"schema_version", "plan_id", "recorded_at", "specification", "calendar_sha256", "calendar_base64", "digest"})
RECEIPT_KEYS = frozenset({"schema_version", "plan_digest", "sequence", "previous_digest", "trade_date", "recorded_at", "status", "payload", "digest"})
RECEIPT_PATTERN = re.compile(r"receipt-(\d{4}-\d{2}-\d{2})\.json")
SNAPSHOT_PATTERN = re.compile(r"snapshot-\d{4}-\d{2}-\d{2}-[0-9a-f]{64}\.json\.gz")
_STAGING_PATTERN = re.compile(r"\.(?:plan\.json|receipt-\d{4}-\d{2}-\d{2}\.json|snapshot-\d{4}-\d{2}-\d{2}-[0-9a-f]{64}\.json\.gz)\.[0-9a-f]{16}\.tmp")


def prospective_directory(root: Path, plan_id: str) -> Path:
    directory = Path(root).expanduser().absolute() / registry_identifier(plan_id, "plan_id")
    if {".git", ".codex", ".agents", ".venv"}.intersection(directory.parts) or not path_has_only_trusted_aliases(directory):
        raise ValueError("strategy prospective path is unsafe")
    return directory


def prospective_namespace(directory: Path) -> set[str]:
    if not path_has_only_trusted_aliases(directory):
        raise ValueError("strategy prospective namespace path changed")
    names: set[str] = set()
    for child in directory.iterdir():
        facts = child.lstat()
        if child.name == "research" and stat.S_ISDIR(facts.st_mode):
            continue
        if not stat.S_ISREG(facts.st_mode) or facts.st_size > 128 * 1024 * 1024:
            raise ValueError("strategy prospective namespace contains an unsafe entry")
        name = child.name
        if name == ".writer.lock" and facts.st_size != 0:
            raise ValueError("strategy prospective lock must be empty")
        if name in {"plan.json", ".writer.lock"} or RECEIPT_PATTERN.fullmatch(name):
            names.add(name)
        elif not SNAPSHOT_PATTERN.fullmatch(name) and not _STAGING_PATTERN.fullmatch(name):
            raise ValueError("strategy prospective namespace contains an unexpected entry")
    return names


def prospective_record(payload: Mapping[str, object]) -> dict[str, object]:
    return {**payload, "digest": sha256_hex(canonical_json_bytes(dict(payload)))}


def publish_prospective_record(directory: Path, filename: str, record: Mapping[str, object]) -> None:
    maximum = MAX_PLAN_BYTES if filename == "plan.json" else MAX_RECEIPT_BYTES
    exclusive_atomic_publish(directory / filename, canonical_json_bytes(dict(record)), max_bytes=maximum)


def read_prospective_record(directory: Path, filename: str, keys: frozenset[str], schema: str) -> dict[str, object]:
    maximum = MAX_PLAN_BYTES if filename == "plan.json" else MAX_RECEIPT_BYTES
    record = registry_object(decode_json_bytes(read_regular_file(directory / filename, max_bytes=maximum)), filename)
    registry_keys(record, keys, filename)
    expected = registry_hash(record["digest"], "record digest")
    actual = sha256_hex(canonical_json_bytes({key: value for key, value in record.items() if key != "digest"}))
    if record["schema_version"] != schema or actual != expected:
        raise ValueError("strategy prospective record identity or digest mismatch")
    return record


def encode_calendar(encoded: bytes) -> str:
    if len(encoded) > MAX_PLAN_BYTES:
        raise ValueError("strategy prospective calendar is oversized")
    return base64.b64encode(encoded).decode("ascii")


def decode_calendar(value: object, digest: object) -> bytes:
    if not isinstance(value, str) or len(value) > (MAX_PLAN_BYTES + 2) // 3 * 4:
        raise ValueError("strategy prospective calendar encoding is invalid")
    try:
        encoded = base64.b64decode(value, validate=True)
    except (ValueError, binascii.Error) as exc:
        raise ValueError("strategy prospective calendar encoding is invalid") from exc
    if len(encoded) > MAX_PLAN_BYTES or sha256_hex(encoded) != registry_hash(digest, "calendar digest"):
        raise ValueError("strategy prospective calendar digest mismatch")
    return encoded
