"""Mechanical domain checks inside an archive, never against original source paths."""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from contextlib import closing
from dataclasses import dataclass
import gzip
import hashlib
import io
import json
import math
import os
from pathlib import Path
import re
import sqlite3
import stat
import time

from app.artifacts.io import canonical_json_bytes, decode_json_bytes, path_has_only_trusted_aliases, read_regular_file, sha256_hex
from app.services.fuyao_dumps_storage import read_dump_status
from app.services.fuyao_sync_control import FuyaoSyncControl
from app.services.market_scan_trial_registry_contract import registry_timestamp
from app.services.strategy_prospective_store import PLAN_KEYS, RECEIPT_KEYS, RECEIPT_PATTERN, SNAPSHOT_PATTERN, decode_calendar
from app.services.workspace_backup_progress import backup_checkpoint


MAX_DOMAIN_FILES = 100_000
MAX_DOMAIN_ENTRIES = 200_000
MAX_DOMAIN_BYTES = 16 * 1024 ** 3
MAX_DOMAIN_JSON_BYTES = 64 * 1024 ** 2
MAX_DOMAIN_SNAPSHOT_BYTES = 512 * 1024 ** 2
MAX_DOMAIN_SNAPSHOT_ROWS = 10_000
MAX_DOMAIN_DEPTH = 64
_SHA256 = re.compile(r"[0-9a-f]{64}")
_RANKING_SCHEMA = "market-scan-probability-ranking-publication-artifact-v1"


@dataclass
class _Budget:
    deadline: float
    files: int = 0
    entries: int = 0
    bytes: int = 0

    def add(self, *, files: int = 0, entries: int = 0, size: int = 0) -> None:
        backup_checkpoint()
        if time.monotonic() >= self.deadline:
            raise ValueError("archive domain verification deadline exceeded")
        self.files += files
        self.entries += entries
        self.bytes += size
        if self.files > MAX_DOMAIN_FILES or self.entries > MAX_DOMAIN_ENTRIES or self.bytes > MAX_DOMAIN_BYTES:
            raise ValueError("archive domain verification budget exceeded")

    def sqlite_progress(self) -> int:
        self.add()
        return 0


def _absolute(value: str) -> Path:
    path = Path(value)
    if not path.is_absolute() or ".." in path.parts or "\x00" in value:
        raise ValueError("archive domain paths must be explicit absolute paths")
    return path


def _relative(value: object) -> Path:
    if not isinstance(value, str) or not value or "\x00" in value:
        raise ValueError("invalid archive member path")
    path = Path(value)
    if path.is_absolute() or ".." in path.parts or path.as_posix() != value or value == ".":
        raise ValueError("archive member path escapes its declared root")
    return path


def _digest(value: object) -> str:
    if not isinstance(value, str) or _SHA256.fullmatch(value) is None:
        raise ValueError("archive domain digest is invalid")
    return value


def _object(value: object) -> dict[str, object]:
    if not isinstance(value, dict):
        raise ValueError("archive domain JSON must contain an object")
    return value


def _facts(path: Path) -> os.stat_result:
    if not path_has_only_trusted_aliases(path):
        raise ValueError("archive domain path contains an untrusted alias")
    value = path.stat(follow_symlinks=False)
    if not stat.S_ISREG(value.st_mode) and not stat.S_ISDIR(value.st_mode):
        raise ValueError("archive domain member is not a regular file or directory")
    return value


def _identity(value: os.stat_result) -> tuple[int, ...]:
    return value.st_dev, value.st_ino, value.st_size, value.st_mtime_ns, value.st_ctime_ns


def _read(path: Path, budget: _Budget, maximum: int = MAX_DOMAIN_JSON_BYTES) -> bytes:
    budget.add(files=1)
    value = read_regular_file(path, max_bytes=min(maximum, max(0, MAX_DOMAIN_BYTES - budget.bytes)))
    budget.add(size=len(value))
    return value


def _json(path: Path, budget: _Budget, maximum: int = MAX_DOMAIN_JSON_BYTES) -> dict[str, object]:
    return _object(decode_json_bytes(_read(path, budget, maximum)))


def _hash(path: Path, budget: _Budget) -> str:
    before = _facts(path)
    if not stat.S_ISREG(before.st_mode):
        raise ValueError("archive domain digest target is not a regular file")
    budget.add(files=1)
    descriptor = os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
    digest = hashlib.sha256()
    with os.fdopen(descriptor, "rb") as stream:
        opened = os.fstat(stream.fileno())
        if _identity(opened) != _identity(before):
            raise ValueError("archive domain file changed before hashing")
        while chunk := stream.read(1024 * 1024):
            budget.add(size=len(chunk))
            digest.update(chunk)
        after = os.fstat(stream.fileno())
    if _identity(before) != _identity(after) or _identity(after) != _identity(_facts(path)):
        raise ValueError("archive domain file changed during hashing")
    return digest.hexdigest()


def _files(root: Path, budget: _Budget) -> list[Path]:
    result: list[Path] = []
    pending = [(root, 0)]
    budget.add(entries=1)
    while pending:
        path, depth = pending.pop()
        budget.add()
        if depth > MAX_DOMAIN_DEPTH:
            raise ValueError("archive domain directory depth exceeded")
        before = _facts(path)
        if stat.S_ISREG(before.st_mode):
            result.append(path)
            continue
        with os.scandir(path) as entries:
            for entry in entries:
                budget.add(entries=1)
                pending.append((path / entry.name, depth + 1))
        if _identity(before) != _identity(_facts(path)):
            raise ValueError("archive domain directory changed during enumeration")
    return sorted(result)


def _validate_declarations(roots: Sequence[Mapping[str, str]], databases: Sequence[Mapping[str, str]]) -> None:
    if len(roots) > 64 or len(databases) > 64 or not roots and not databases:
        raise ValueError("archive domain declarations exceed their limit or are empty")
    names = [root["name"] for root in roots]
    if len(set(names)) != len(names) or any(not name for name in names):
        raise ValueError("archive domain root names must be unique")
    for item in (*roots, *databases):
        _absolute(item["source_path"])  # Lexical only: never inspect the original machine.
        _facts(_absolute(item["archive_path"]))
    if any(database["role"] not in {"primary", "auxiliary"} for database in databases):
        raise ValueError("unknown archive database role")


def validate_archive_domains(
    roots: Sequence[Mapping[str, str]], databases: Sequence[Mapping[str, str]],
    *, timeout_seconds: float = 300,
) -> dict[str, object]:
    """Validate known archive bindings; successful restoration never grants run authority."""
    if isinstance(timeout_seconds, bool) or not math.isfinite(timeout_seconds) or timeout_seconds <= 0:
        raise ValueError("archive domain timeout must be finite and positive")
    budget = _Budget(time.monotonic() + timeout_seconds)
    _validate_declarations(roots, databases)
    root_results = [_validate_root(root, budget) for root in roots]
    database_results = [_validate_database(database, roots, budget) for database in databases]
    return {"schema_version": "workspace-backup-domains-v1",
            "scope": "known_archive_domains_and_primary_ranking_references_only",
            "resume_eligible": False, "runtime_execution_verified": False, "roots": root_results,
            "databases": database_results, "external_dependencies": [
                {"root": root["name"], "kind": "shared_base_python", "status": "not_verified"}
                for root in roots if root["category"] == "runtime"],
            "budget": {"files_checked": budget.files, "entries_checked": budget.entries, "bytes_accounted": budget.bytes,
                       "max_files": MAX_DOMAIN_FILES, "max_entries": MAX_DOMAIN_ENTRIES, "max_bytes": MAX_DOMAIN_BYTES}}


def _validate_root(root: Mapping[str, str], budget: _Budget) -> dict[str, object]:
    budget.add()
    category = root["category"]
    result: dict[str, object] = {"name": root["name"], "category": category,
                                "verification": "file_integrity_only", "relocation_status": "archive_only"}
    if category == "control":
        result["relocation_status"] = "archive_requires_rebinding"
    elif category in {"prospective", "runtime", "fuyao"}:
        path = _absolute(root["archive_path"])
        files = _files(path, budget)
        validators = {"prospective": _prospective, "runtime": _runtimes, "fuyao": _fuyao}
        result.update(validators[category](root, files, budget))
    return result


def _mapped_reference(value: str, roots: Sequence[Mapping[str, str]]) -> Path:
    source = _absolute(value)
    matches = []
    for root in roots:
        original = _absolute(root["source_path"])
        if source.is_relative_to(original):
            matches.append((len(original.parts), _absolute(root["archive_path"]) / source.relative_to(original)))
    if not matches:
        raise ValueError("primary database reference is outside declared archive roots")
    longest = max(length for length, _path in matches)
    targets = {path for length, path in matches if length == longest}
    if len(targets) != 1:
        raise ValueError("primary database reference mapping is ambiguous")
    target = targets.pop()
    if not stat.S_ISREG(_facts(target).st_mode):
        raise ValueError("primary database reference is not an archived regular file")
    return target


def _validate_database(database: Mapping[str, str], roots: Sequence[Mapping[str, str]], budget: _Budget) -> dict[str, object]:
    budget.add()
    result: dict[str, object] = {"source_path": database["source_path"], "role": database["role"],
        "verification": "file_integrity_only", "known_reference_count": 0, "relocation_status": "archive_only"}
    if database["role"] != "primary":
        return result
    path = _absolute(database["archive_path"])
    before = _facts(path)
    budget.add(files=1, size=before.st_size)
    with closing(sqlite3.connect(path.as_uri() + "?mode=ro&immutable=1", uri=True)) as connection:
        connection.set_progress_handler(budget.sqlite_progress, 1000)
        connection.setlimit(sqlite3.SQLITE_LIMIT_LENGTH, MAX_DOMAIN_JSON_BYTES)
        connection.execute("PRAGMA query_only=ON")
        connection.row_factory = sqlite3.Row
        count = _ranking_references(connection, roots, budget)
    if _identity(before) != _identity(_facts(path)):
        raise ValueError("archived database changed during domain verification")
    result.update(verification="known_ranking_references", known_reference_count=count)
    if count:
        result["relocation_status"] = "archive_requires_rebinding"
    return result


def _ranking_references(connection: sqlite3.Connection, roots: Sequence[Mapping[str, str]], budget: _Budget) -> int:
    present = connection.execute("SELECT 1 FROM sqlite_schema WHERE type='table' AND name='market_scan_probability_ranking_publication'").fetchone()
    if present is None:
        return 0
    cursor = connection.execute("SELECT * FROM market_scan_probability_ranking_publication LIMIT ?", (MAX_DOMAIN_FILES + 1,))
    count = 0
    for row in cursor:
        budget.add(entries=1)
        count += 1
        if count > MAX_DOMAIN_FILES:
            raise ValueError("archive ranking reference limit exceeded")
        path = _mapped_reference(row["artifact_path"], roots)
        _ranking_artifact(path, dict(row), budget)
    return count


def _ranking_artifact(path: Path, row: Mapping[str, object], budget: _Budget) -> None:
    encoded = _read(path, budget)
    with gzip.GzipFile(fileobj=io.BytesIO(encoded)) as stream:
        raw = stream.read(MAX_DOMAIN_JSON_BYTES + 1)
    budget.add(size=len(raw))
    if len(raw) > MAX_DOMAIN_JSON_BYTES:
        raise ValueError("ranking archive exceeds decompressed byte limit")
    value = _object(decode_json_bytes(raw))
    payload, integrity = _object(value["payload"]), _object(value["integrity"])
    actual = sha256_hex(canonical_json_bytes({"generated_at": value["generated_at"], "payload": payload}))
    expected = {"algorithm": "sha256", "scope": "generated_at+payload", "notice": "content_address_only_strict_replay_required",
                "integrity_digest": _digest(row["artifact_digest"])}
    if value.keys() != {"schema_version", "generated_at", "payload", "integrity"} or value["schema_version"] != _RANKING_SCHEMA:
        raise ValueError("ranking archive envelope schema mismatch")
    if integrity != expected or actual != row["artifact_digest"]:
        raise ValueError("ranking archive digest differs from its database reference")
    _ranking_identity(value, payload, row)


def _ranking_identity(value: Mapping[str, object], payload: Mapping[str, object], row: Mapping[str, object]) -> None:
    pairs = {"run_id": "run_id", "score_rule_version": "score_rule_version", "score_spec_hash": "score_spec_hash", "base_snapshot_digest": "base_snapshot_digest",
             "current_prediction_artifact_digest": "prediction_artifact_digest", "promotion_digest": "promotion_digest"}
    if value["schema_version"] != row["schema_version"] or any(payload.get(key) != row[column] for key, column in pairs.items()):
        raise ValueError("ranking archive identity differs from its database reference")
    if value["generated_at"] != row["generated_at"] or payload.get("generated_at") != value["generated_at"]:
        raise ValueError("ranking archive time differs from its database reference")
    records = payload.get("records")
    if not isinstance(records, list) or len(records) != row["record_count"]:
        raise ValueError("ranking archive record count differs from its database reference")


def _sealed_record(path: Path, keys: frozenset[str], schema: str, budget: _Budget) -> dict[str, object]:
    value = _json(path, budget)
    if value.keys() != keys or value["schema_version"] != schema:
        raise ValueError("prospective archive record schema mismatch")
    actual = sha256_hex(canonical_json_bytes({key: item for key, item in value.items() if key != "digest"}))
    if actual != _digest(value["digest"]):
        raise ValueError("prospective archive record digest mismatch")
    return value


def _prospective(root: Mapping[str, str], files: list[Path], budget: _Budget) -> dict[str, object]:
    plans = [path for path in files if path.name == "plan.json"]
    directories = {path.parent for path in plans}
    for path in files:
        if (RECEIPT_PATTERN.fullmatch(path.name) or SNAPSHOT_PATTERN.fullmatch(path.name)) and path.parent not in directories:
            raise ValueError("prospective archive contains orphaned evidence")
    counts = [_prospective_plan(path, root, files, budget) for path in plans]
    return {"verification": "prospective_mechanical_chain" if plans else "file_integrity_only",
            "plan_count": len(plans), "receipt_count": sum(counts), "business_contract_replayed": False}


def _prospective_plan(path: Path, root: Mapping[str, str], files: list[Path], budget: _Budget) -> int:
    plan = _sealed_record(path, PLAN_KEYS, "strategy-template-prospective-plan-v1", budget)
    original = _absolute(root["source_path"]) / path.relative_to(_absolute(root["archive_path"]))
    if plan["plan_id"] != original.parent.name:
        raise ValueError("prospective archive plan directory identity mismatch")
    decode_calendar(plan["calendar_base64"], plan["calendar_sha256"])
    signals = _object(plan["specification"]).get("signal_dates")
    if not isinstance(signals, list) or any(not isinstance(day, str) for day in signals):
        raise ValueError("prospective archive lacks its frozen signal schedule")
    receipts = sorted(item for item in files if item.parent == path.parent and RECEIPT_PATTERN.fullmatch(item.name))
    if [item.name for item in receipts] != [f"receipt-{day}.json" for day in signals[:len(receipts)]]:
        raise ValueError("prospective archive receipt schedule contains gaps")
    previous = plan
    for sequence, receipt_path in enumerate(receipts, 1):
        receipt = _sealed_record(receipt_path, RECEIPT_KEYS, "strategy-template-prospective-receipt-v1", budget)
        _receipt_identity(receipt, previous, plan, signals[sequence - 1], sequence)
        payload = _object(receipt["payload"])
        if payload.get("snapshot_path") is not None:
            _snapshot(path.parent, payload, budget)
        elif receipt["status"] == "captured":
            raise ValueError("captured prospective receipt lacks its archived input")
        previous = receipt
    return len(receipts)


def _receipt_identity(receipt: Mapping[str, object], previous: Mapping[str, object], plan: Mapping[str, object],
                      day: str, sequence: int) -> None:
    if receipt["plan_digest"] != plan["digest"] or receipt["previous_digest"] != previous["digest"]:
        raise ValueError("prospective archive receipt hash chain mismatch")
    if type(receipt["sequence"]) is not int or receipt["sequence"] != sequence or receipt["trade_date"] != day:
        raise ValueError("prospective archive receipt sequence mismatch")
    if registry_timestamp(receipt["recorded_at"], "receipt time") < registry_timestamp(previous["recorded_at"], "previous time"):
        raise ValueError("prospective archive receipt clock moved backwards")
    if receipt["status"] not in {"captured", "missing", "source_incomplete", "late"}:
        raise ValueError("prospective archive receipt status is invalid")


def _snapshot(directory: Path, payload: Mapping[str, object], budget: _Budget) -> None:
    name = payload["snapshot_path"]
    if not isinstance(name, str) or SNAPSHOT_PATTERN.fullmatch(name) is None:
        raise ValueError("prospective archive snapshot path is invalid")
    expected = _digest(payload["snapshot_file_sha256"])
    encoded = _read(directory / name, budget)
    if not name.endswith(f"-{expected}.json.gz") or sha256_hex(encoded) != expected or len(encoded) != payload["snapshot_byte_size"]:
        raise ValueError("prospective archive compressed snapshot mismatch")
    digest, size, lines = hashlib.sha256(), 0, 0
    with gzip.GzipFile(fileobj=io.BytesIO(encoded)) as stream:
        while line := stream.readline(min(MAX_DOMAIN_JSON_BYTES, MAX_DOMAIN_SNAPSHOT_BYTES - size) + 1):
            size += len(line)
            budget.add(size=len(line))
            if size > MAX_DOMAIN_SNAPSHOT_BYTES or len(line) > MAX_DOMAIN_JSON_BYTES or not line.endswith(b"\n"):
                raise ValueError("prospective archive snapshot exceeds its decoded bounds")
            _object(decode_json_bytes(line))
            digest.update(line)
            lines += 1
            if lines > MAX_DOMAIN_SNAPSHOT_ROWS + 1:
                raise ValueError("prospective archive snapshot row limit exceeded")
    if digest.hexdigest() != _digest(payload["snapshot_raw_bytes_sha256"]) or size != payload["snapshot_raw_byte_size"]:
        raise ValueError("prospective archive raw snapshot mismatch")
    if type(payload["snapshot_result_count"]) is not int or lines != payload["snapshot_result_count"] + 1:
        raise ValueError("prospective archive snapshot row count mismatch")


def _runtimes(_root: Mapping[str, str], files: list[Path], budget: _Budget) -> dict[str, object]:
    manifests: list[Path] = []
    for path in sorted((item for item in files if item.name == "runtime.json"), key=lambda item: len(item.parts)):
        budget.add()
        if not any(path.is_relative_to(other.parent) for other in manifests):
            manifests.append(path)
    for path in manifests:
        _runtime(path, files, budget)
    return {"verification": "runtime_mechanical_manifest" if manifests else "file_integrity_only",
            "runtime_count": len(manifests), "runtime_execution_verified": False,
            "relocation_status": "archive_requires_external_runtime_validation"}


def _runtime(path: Path, files: list[Path], budget: _Budget) -> None:
    manifest = _json(path, budget, 16 * 1024 ** 2)
    payload = {key: item for key, item in manifest.items() if key != "digest"}
    encoded = json.dumps(payload, sort_keys=True, separators=(",", ":"), ensure_ascii=True, allow_nan=False).encode("ascii")
    if manifest.get("schema_version") != "strategy-prospective-runtime-v1" or sha256_hex(encoded) != _digest(manifest.get("digest")):
        raise ValueError("runtime archive receipt digest mismatch")
    expected = {"launch.py": _digest(manifest["launcher_sha256"])}
    for folder, key in (("code", "code_files"), ("venv", "environment_files")):
        for name, digest in _object(manifest[key]).items():
            expected[(Path(folder) / _relative(name)).as_posix()] = _digest(digest)
    actual = {item.relative_to(path.parent).as_posix(): item for item in files if item.is_relative_to(path.parent) and item != path}
    if actual.keys() != expected.keys():
        raise ValueError("runtime archive required files were omitted or unexpected files were added")
    for name, digest in expected.items():
        if _hash(actual[name], budget) != digest:
            raise ValueError("runtime archive file digest mismatch")
    _object(manifest["base_python"])  # Deliberately never inspect the external interpreter or standard library.


class _FuyaoBudgetControl(FuyaoSyncControl):
    def __init__(self, budget: _Budget) -> None:
        super().__init__()
        self.budget = budget
        self.previous = 0

    def checkpoint(self, stage: str | None = None, current: int | None = None,
                   total: int | None = None, unit: str | None = None) -> None:
        self.budget.add()
        if current is not None:
            self.budget.add(size=max(0, current - self.previous))
            self.previous = current
        super().checkpoint(stage, current, total, unit)


def _fuyao(_root: Mapping[str, str], files: list[Path], budget: _Budget) -> dict[str, object]:
    pointers = [path for path in files if path.name == "current.json"]
    for path in pointers:
        pointer = _json(path, budget, 1024)
        version = _digest(pointer.get("version"))
        directory = path.parent / "versions" / version
        required = [directory / name for name in ("manifest.json", "daily.parquet", "actions.parquet", "source-daily.parquet", "source-actions.parquet")]
        before = [_identity(_facts(item)) for item in required]
        budget.add(files=len(required), size=min(required[0].stat().st_size, 1024 ** 2))
        if read_dump_status(path.parent, verify_files=True, control=_FuyaoBudgetControl(budget)) is None:
            raise ValueError("fuyao archive current pointer disappeared")
        if before != [_identity(_facts(item)) for item in required] or pointer != _json(path, budget, 1024):
            raise ValueError("fuyao archive changed during verification")
    return {"verification": "fuyao_current_version_files" if pointers else "file_integrity_only", "current_version_count": len(pointers)}
