"""Fail-closed file-artifact references for bounded runtime retention."""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
import gzip
from io import BytesIO
import os
from pathlib import Path
import re
import stat
from typing import Literal

from app.artifacts.io import (
    ArtifactIOError,
    canonical_json_text,
    decode_json_bytes,
    read_regular_file,
    sha256_hex,
)
from app.repositories.runtime_probability_artifact_stream import (
    RuntimeCleanupIntegrityError,
    stream_probability_run_ids as _stream_probability_run_ids,
)


_DEEP_VERIFY_MAX_BYTES = 256 * 1024 * 1024
_DECOMPRESSED_MAX_BYTES = 256 * 1024 * 1024
_SUMMARY_PATH = Path("research/market-scan-future-range-summary.json")
_PROJECT_ROOT = Path(__file__).resolve().parents[2]


@dataclass(frozen=True)
class ArtifactDirectoryRule:
    relative_path: Path
    filename_pattern: re.Pattern[str]
    allow_oversized_filename_pin: bool = False
    filename_has_run_id: bool = True
    integrity_scope: Literal["payload", "unsigned_artifact"] = "payload"


@dataclass(frozen=True)
class ArtifactDirectorySnapshot:
    rule: ArtifactDirectoryRule
    identity: tuple[int, int, int, int] | None
    files: tuple[tuple[str, int, int, int, int, int, int], ...]


@dataclass(frozen=True)
class ArtifactFileSnapshot:
    relative_path: Path
    fingerprint: tuple[str, int, int, int, int, int, int] | None


@dataclass(frozen=True)
class MarketScanArtifactProtection:
    run_ids: frozenset[int] = frozenset()
    snapshots: tuple[ArtifactDirectorySnapshot, ...] = ()
    files: tuple[ArtifactFileSnapshot, ...] = ()


_RULES = (
    ArtifactDirectoryRule(
        Path("market-scan-probability"),
        re.compile(r"market-scan-probability-run-(?P<run_id>[1-9]\d*)-(?P<digest>[0-9a-f]{64})\.json"),
        allow_oversized_filename_pin=True,
    ),
    ArtifactDirectoryRule(
        Path("research/market_scan_probability_source"),
        re.compile(r"market-scan-probability-source-run-(?P<run_id>[1-9]\d*)-(?P<digest>[0-9a-f]{64})\.json\.gz"),
    ),
    ArtifactDirectoryRule(
        Path("research/market_scan_probability_outcomes"),
        re.compile(r"market-scan-probability-outcomes-run-(?P<run_id>[1-9]\d*)-through-\d{4}-\d{2}-\d{2}-(?P<digest>[0-9a-f]{64})\.json\.gz"),
    ),
    ArtifactDirectoryRule(
        Path("research/market_scan_probability_fit"),
        re.compile(r"market-scan-probability-fit-through-run-(?P<run_id>[1-9]\d*)-(?P<digest>[0-9a-f]{64})\.json\.gz"),
    ),
    ArtifactDirectoryRule(
        Path("research/market_scan_future_range"),
        re.compile(r"market-scan-future-range-run-(?P<run_id>[1-9]\d*)-(?P<digest>[0-9a-f]{64})\.json"),
    ),
    ArtifactDirectoryRule(
        Path("research/individual_probability"),
        re.compile(r"individual-upside-probability-assessment-(?P<digest>[0-9a-f]{64})\.json"),
        filename_has_run_id=False,
        integrity_scope="unsigned_artifact",
    ),
)
_INDIVIDUAL_PROBABILITY_FALLBACK_RULE = ArtifactDirectoryRule(
    Path("docs/research/artifacts"),
    re.compile(r"individual-upside-probability-assessment-(?P<digest>[0-9a-f]{64})\.json"),
    filename_has_run_id=False,
    integrity_scope="unsigned_artifact",
)


def market_scan_artifact_protection(database_path: Path) -> MarketScanArtifactProtection:
    if str(database_path) == ":memory:":
        return MarketScanArtifactProtection()
    root = database_path.expanduser().absolute().parent
    snapshots: list[ArtifactDirectorySnapshot] = []
    run_ids: set[int] = set()
    primary_individual_has_files = False
    for rule in _RULES:
        snapshot, found = _scan_directory(root, rule)
        snapshots.append(snapshot)
        run_ids.update(found)
        if rule.relative_path == Path("research/individual_probability"):
            primary_individual_has_files = bool(snapshot.files)
    if not primary_individual_has_files and database_path.expanduser().absolute() == _PROJECT_ROOT / "data" / "ashare_radar.sqlite3":
        fallback_snapshot, fallback_ids = _scan_directory(
            _PROJECT_ROOT,
            _INDIVIDUAL_PROBABILITY_FALLBACK_RULE,
        )
        snapshots.append(fallback_snapshot)
        run_ids.update(fallback_ids)
    summary, summary_ids = _scan_summary(root)
    run_ids.update(summary_ids)
    return MarketScanArtifactProtection(
        run_ids=frozenset(run_ids),
        snapshots=tuple(snapshots),
        files=(summary,),
    )


def require_market_scan_artifacts_unchanged(
    database_path: Path,
    expected: MarketScanArtifactProtection,
) -> None:
    if market_scan_artifact_protection(database_path) != expected:
        raise RuntimeCleanupIntegrityError("研究 artifact 引用在清理事务期间发生变化")


def _scan_directory(
    root: Path,
    rule: ArtifactDirectoryRule,
) -> tuple[ArtifactDirectorySnapshot, set[int]]:
    directory = root / rule.relative_path
    try:
        facts = directory.lstat()
    except FileNotFoundError:
        return ArtifactDirectorySnapshot(rule, None, ()), set()
    except OSError as exc:
        raise RuntimeCleanupIntegrityError(f"无法枚举研究 artifact 目录：{rule.relative_path}") from exc
    if not stat.S_ISDIR(facts.st_mode):
        raise RuntimeCleanupIntegrityError(f"研究 artifact 路径不是普通目录：{rule.relative_path}")
    identity = _directory_identity(facts)
    try:
        fingerprints, run_ids = _scan_directory_entries(directory, rule)
        if _directory_identity(directory.lstat()) != identity:
            raise RuntimeCleanupIntegrityError(f"研究 artifact 目录在枚举期间发生变化：{rule.relative_path}")
    except RuntimeCleanupIntegrityError:
        raise
    except OSError as exc:
        raise RuntimeCleanupIntegrityError(f"无法完整枚举研究 artifact 目录：{rule.relative_path}") from exc
    return ArtifactDirectorySnapshot(rule, identity, fingerprints), run_ids


def _scan_directory_entries(
    directory: Path,
    rule: ArtifactDirectoryRule,
) -> tuple[tuple[tuple[str, int, int, int, int, int, int], ...], set[int]]:
    fingerprints: list[tuple[str, int, int, int, int, int, int]] = []
    run_ids: set[int] = set()
    with os.scandir(directory) as entries:
        for entry in sorted(entries, key=lambda item: item.name):
            matched = rule.filename_pattern.fullmatch(entry.name)
            if matched is None:
                raise RuntimeCleanupIntegrityError(f"研究 artifact 目录含无法归属的文件：{rule.relative_path / entry.name}")
            facts = entry.stat(follow_symlinks=False)
            if not stat.S_ISREG(facts.st_mode):
                raise RuntimeCleanupIntegrityError(f"研究 artifact 不是普通文件：{rule.relative_path / entry.name}")
            fingerprints.append(_fingerprint(entry.name, facts))
            run_ids.update(_verified_run_ids(Path(entry.path), rule, matched, facts))
    return tuple(fingerprints), run_ids


def _verified_run_ids(
    path: Path,
    rule: ArtifactDirectoryRule,
    matched: re.Match[str],
    facts: os.stat_result,
) -> set[int]:
    encoded_run_id = int(matched.group("run_id")) if rule.filename_has_run_id else None
    if rule.relative_path == Path("market-scan-probability") and facts.st_size > 64 * 1024 * 1024 and encoded_run_id is not None:
        return _stream_probability_run_ids(
            path,
            encoded_run_id,
            matched.group("digest"),
            facts.st_size,
        )
    if facts.st_size > _DEEP_VERIFY_MAX_BYTES:
        raise RuntimeCleanupIntegrityError(f"研究 artifact 超过维护校验预算：{path.name}")
    try:
        artifact = _load_artifact(path)
        payload = artifact.get("payload")
        integrity = artifact.get("integrity")
        if not isinstance(payload, Mapping) or not isinstance(integrity, Mapping):
            raise RuntimeCleanupIntegrityError(f"研究 artifact 缺少完整性合同：{path.name}")
        _require_digest(path, artifact, payload, integrity, matched.group("digest"), rule.integrity_scope)
        run_ids = _payload_run_ids(payload)
        if encoded_run_id is not None and encoded_run_id not in run_ids:
            raise RuntimeCleanupIntegrityError(f"研究 artifact 文件名与 run_id 不一致：{path.name}")
        return run_ids
    except RuntimeCleanupIntegrityError:
        raise
    except (ArtifactIOError, OSError, EOFError, gzip.BadGzipFile) as exc:
        raise RuntimeCleanupIntegrityError(f"研究 artifact 无法验证：{path.name}") from exc


def _load_artifact(path: Path) -> Mapping[object, object]:
    encoded = read_regular_file(path, max_bytes=_DEEP_VERIFY_MAX_BYTES)
    decoded: object
    if path.name.endswith(".gz"):
        with gzip.GzipFile(fileobj=BytesIO(encoded), mode="rb") as stream:
            raw = stream.read(_DECOMPRESSED_MAX_BYTES + 1)
        if len(raw) > _DECOMPRESSED_MAX_BYTES:
            raise RuntimeCleanupIntegrityError("研究 artifact 解压后超过维护校验预算")
        decoded = decode_json_bytes(raw)
    else:
        decoded = decode_json_bytes(encoded)
    if not isinstance(decoded, Mapping):
        raise RuntimeCleanupIntegrityError(f"研究 artifact 顶层不是 object：{path.name}")
    return decoded


def _require_digest(
    path: Path,
    artifact: Mapping[object, object],
    payload: Mapping[object, object],
    integrity: Mapping[object, object],
    filename_digest: str,
    scope: Literal["payload", "unsigned_artifact"],
) -> None:
    digest = integrity.get("integrity_digest")
    encoded_scope = integrity.get("scope")
    computed = _digest_candidates(artifact, payload, scope)
    if encoded_scope == "generated_at+payload":
        computed.add(sha256_hex(canonical_json_text({"generated_at": artifact.get("generated_at"), "payload": _without_generated_at(payload)})))
    if (
        integrity.get("algorithm") != "sha256"
        or (scope == "payload" and encoded_scope not in {"payload", "generated_at+payload"})
        or not isinstance(digest, str)
        or digest != filename_digest
        or digest not in computed
    ):
        raise RuntimeCleanupIntegrityError(f"研究 artifact 摘要或文件名不一致：{path.name}")


def _digest_candidates(
    artifact: Mapping[object, object],
    payload: Mapping[object, object],
    scope: Literal["payload", "unsigned_artifact"],
) -> set[str]:
    if scope == "unsigned_artifact":
        unsigned = {str(key): value for key, value in artifact.items() if str(key) != "integrity"}
        return {sha256_hex(canonical_json_text(unsigned))}
    return {
        sha256_hex(canonical_json_text(payload)),
        sha256_hex(canonical_json_text(_without_generated_at(payload))),
    }


def _without_generated_at(value: object) -> object:
    if isinstance(value, Mapping):
        return {str(key): _without_generated_at(item) for key, item in value.items() if str(key) != "generated_at"}
    if isinstance(value, list):
        return [_without_generated_at(item) for item in value]
    return value


def _payload_run_ids(payload: Mapping[object, object]) -> set[int]:
    run_ids: set[int] = set()
    pending: list[object] = [payload]
    while pending:
        current = pending.pop()
        if isinstance(current, Mapping):
            for key, value in current.items():
                name = str(key)
                if name.endswith("run_id") and _positive_int(value):
                    run_ids.add(int(value))
                elif name.endswith("run_ids") and isinstance(value, list | tuple):
                    run_ids.update(int(item) for item in value if _positive_int(item))
                pending.append(value)
        elif isinstance(current, list | tuple):
            pending.extend(current)
    return run_ids


def _scan_summary(root: Path) -> tuple[ArtifactFileSnapshot, set[int]]:
    path = root / _SUMMARY_PATH
    try:
        facts = path.lstat()
    except FileNotFoundError:
        return ArtifactFileSnapshot(_SUMMARY_PATH, None), set()
    except OSError as exc:
        raise RuntimeCleanupIntegrityError("无法读取未来区间研究 summary") from exc
    if not stat.S_ISREG(facts.st_mode):
        raise RuntimeCleanupIntegrityError("未来区间研究 summary 不是普通文件")
    try:
        decoded = decode_json_bytes(read_regular_file(path, max_bytes=_DEEP_VERIFY_MAX_BYTES))
    except ArtifactIOError as exc:
        raise RuntimeCleanupIntegrityError("未来区间研究 summary 无法验证") from exc
    if not isinstance(decoded, Mapping):
        raise RuntimeCleanupIntegrityError("未来区间研究 summary 顶层不是 object")
    run_ids = _validated_summary_run_ids(decoded)
    return ArtifactFileSnapshot(_SUMMARY_PATH, _fingerprint(path.name, facts)), run_ids


def _validated_summary_run_ids(summary: Mapping[object, object]) -> set[int]:
    if summary.get("schema_version") != "market-scan-future-range-evaluation-summary-v1":
        raise RuntimeCleanupIntegrityError("未来区间研究 summary schema_version 无效")
    artifacts = summary.get("artifacts")
    count = summary.get("artifact_count")
    if not isinstance(artifacts, list) or isinstance(count, bool) or not isinstance(count, int):
        raise RuntimeCleanupIntegrityError("未来区间研究 summary artifact 集合无效")
    if count != len(artifacts):
        raise RuntimeCleanupIntegrityError("未来区间研究 summary artifact_count 不守恒")
    run_ids: set[int] = set()
    identities: dict[int, str] = {}
    for item in artifacts:
        if not isinstance(item, Mapping):
            raise RuntimeCleanupIntegrityError("未来区间研究 summary artifact item 无效")
        run_id, digest = _summary_item_identity(item)
        previous = identities.setdefault(run_id, digest)
        if previous != digest:
            raise RuntimeCleanupIntegrityError("未来区间研究 summary 同 run 存在冲突")
        run_ids.add(run_id)
    return run_ids


def _summary_item_identity(item: Mapping[object, object]) -> tuple[int, str]:
    run_id = item.get("run_id")
    digest = item.get("integrity_digest")
    if (
        not _positive_int(run_id)
        or not isinstance(digest, str)
        or re.fullmatch(r"[0-9a-f]{64}", digest) is None
        or item.get("offline_replay_verified") is not True
    ):
        raise RuntimeCleanupIntegrityError("未来区间研究 summary artifact item 无效")
    assert isinstance(run_id, int) and not isinstance(run_id, bool)
    return run_id, digest


def _positive_int(value: object) -> bool:
    return isinstance(value, int) and not isinstance(value, bool) and value > 0


def _directory_identity(facts: os.stat_result) -> tuple[int, int, int, int]:
    return facts.st_dev, facts.st_ino, facts.st_mtime_ns, facts.st_ctime_ns


def _fingerprint(name: str, facts: os.stat_result) -> tuple[str, int, int, int, int, int, int]:
    return name, facts.st_dev, facts.st_ino, facts.st_mode, facts.st_size, facts.st_mtime_ns, facts.st_ctime_ns


__all__ = [
    "MarketScanArtifactProtection",
    "RuntimeCleanupIntegrityError",
    "market_scan_artifact_protection",
    "require_market_scan_artifacts_unchanged",
]
