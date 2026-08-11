"""Read-only API projection over immutable future-range research artifacts."""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from copy import deepcopy
from pathlib import Path
import re
import stat
from threading import RLock
from typing import Final, Literal, cast

from app.services.market_scan_future_range_artifact import (
    FUTURE_RANGE_ARTIFACT_SCHEMA_VERSION,
    FutureRangeArtifactError,
    future_range_artifact_filename,
    load_future_range_artifact,
)


FUTURE_RANGE_API_SCHEMA_VERSION: Final = "market-scan-future-range-api-v1"


class FutureRangeResearchUnavailable(ValueError):
    """Raised when a run is not an eligible published official snapshot."""


_FileFingerprint = tuple[Path, int, int, int, int, int, int]
_DirectoryIdentity = tuple[int, int, int, int]
_DirectorySnapshot = tuple[_DirectoryIdentity | None, tuple[_FileFingerprint, ...]]
_RUN_ARTIFACT_PATTERN = re.compile(
    r"market-scan-future-range-run-(\d+)-([0-9a-f]{64})\.json"
)


class MarketScanFutureRangeStore:
    """Locate, verify, and project one run-bound future-range artifact.

    The store only reads immutable JSON files. Missing and legacy filenames are
    normalized to ``not_generated``; a current-schema candidate that cannot be
    verified fails closed instead of exposing partial research.
    """

    def __init__(self, directory: Path) -> None:
        self.directory = directory.expanduser().resolve()
        self._lock = RLock()
        self._snapshot: _DirectorySnapshot | None = None
        self._file_cache: dict[Path, tuple[_FileFingerprint, dict[str, object]]] = {}
        self._artifact_cache: dict[int, dict[str, object] | None] = {}
        self._projection_cache: dict[int, dict[str, object]] = {}

    def research_projection(
        self,
        run_id: int,
        *,
        page: int = 1,
        page_size: int = 100,
        session_offset: Literal[1, 2, 3] | None = None,
        symbol: str | None = None,
        include_research: bool = True,
    ) -> dict[str, object]:
        if isinstance(run_id, bool) or run_id <= 0:
            raise ValueError("run_id 必须是正整数")
        if isinstance(page, bool) or page <= 0:
            raise ValueError("page 必须是正整数")
        if isinstance(page_size, bool) or not 1 <= page_size <= 200:
            raise ValueError("page_size 必须在 1 到 200 之间")
        normalized_symbol = _normalized_symbol(symbol)
        with self._lock:
            self._refresh_if_changed()
            cached = self._projection_cache.get(run_id)
            if cached is None:
                artifact = self._artifact_for_run(run_id)
                cached = (
                    _artifact_projection(artifact, run_id)
                    if artifact is not None
                    else not_generated_future_range_research(run_id)
                )
                self._projection_cache[run_id] = cached
            return _paged_projection(
                cached,
                page=page,
                page_size=page_size,
                session_offset=session_offset,
                symbol=normalized_symbol,
                include_research=include_research,
            )

    def export_projection(self, run_id: int) -> dict[str, object]:
        """Return all persisted records for server-side XLSX rendering only."""
        with self._lock:
            self._refresh_if_changed()
            artifact = self._artifact_for_run(run_id)
            if artifact is None:
                return not_generated_future_range_research(run_id)
            return deepcopy(_artifact_projection(artifact, run_id))

    def _artifact_for_run(self, run_id: int) -> dict[str, object] | None:
        if run_id in self._artifact_cache:
            return self._artifact_cache[run_id]
        if self._snapshot is None:
            return None
        fingerprints = _candidate_fingerprints(self._snapshot, run_id)
        loaded = _load_candidates(fingerprints, self._file_cache, run_id)
        if _directory_snapshot(self.directory) != self._snapshot:
            raise FutureRangeArtifactError("未来区间 artifact 目录在读取期间发生变化，请重试")
        self._file_cache.update(loaded)
        selected = _newest_artifact(tuple(loaded.values()))
        self._artifact_cache[run_id] = selected
        return selected

    def _refresh_if_changed(self) -> None:
        snapshot = _directory_snapshot(self.directory)
        if snapshot == self._snapshot:
            return
        current = {fingerprint[0]: fingerprint for fingerprint in snapshot[1]}
        self._file_cache = {
            path: cached
            for path, cached in self._file_cache.items()
            if current.get(path) == cached[0]
        }
        self._artifact_cache = {}
        self._projection_cache = {}
        self._snapshot = snapshot


def not_generated_future_range_research(run_id: int) -> dict[str, object]:
    """Return the explicit null contract for an old or ungenerated run."""
    return {
        "schema_version": FUTURE_RANGE_API_SCHEMA_VERSION,
        "generation_status": "not_generated",
        "artifact": None,
        "research": None,
        "record_page": {
            "page": 1,
            "page_size": 100,
            "total": 0,
            "page_count": 0,
            "session_offset": None,
            "symbol": None,
            "items": [],
        },
    }


def _artifact_projection(
    artifact: Mapping[str, object],
    run_id: int,
) -> dict[str, object]:
    payload = cast(Mapping[str, object], artifact["payload"])
    run = cast(Mapping[str, object], payload["run"])
    if run.get("run_id") != run_id:
        raise FutureRangeArtifactError("未来区间 artifact 与请求的 run_id 不一致")
    status = payload.get("status")
    if status not in {"ok", "insufficient_data"}:
        raise FutureRangeArtifactError("未来区间 artifact 研究状态无效")
    integrity = cast(Mapping[str, object], artifact["integrity"])
    records = payload.get("records")
    if not isinstance(records, list):
        raise FutureRangeArtifactError("未来区间 artifact records 无效")
    _validate_run_records(records, run_id)
    research = dict(payload)
    research.pop("records", None)
    research["record_count"] = len(records)
    return {
        "schema_version": FUTURE_RANGE_API_SCHEMA_VERSION,
        "generation_status": "ready" if status == "ok" else "insufficient_data",
        "artifact": {
            "schema_version": artifact["schema_version"],
            "generated_at": artifact["generated_at"],
            "integrity_digest": integrity["integrity_digest"],
        },
        "research": research,
        "record_page": {
            "page": 1,
            "page_size": max(1, len(records)),
            "total": len(records),
            "page_count": 1 if records else 0,
            "session_offset": None,
            "symbol": None,
            "items": records,
        },
    }


def _paged_projection(
    projection: Mapping[str, object],
    *,
    page: int,
    page_size: int,
    session_offset: Literal[1, 2, 3] | None,
    symbol: str | None,
    include_research: bool,
) -> dict[str, object]:
    output = {
        key: None if key == "research" and not include_research else deepcopy(value)
        for key, value in projection.items()
        if key != "record_page"
    }
    record_page = projection.get("record_page")
    if not isinstance(record_page, dict):
        raise FutureRangeArtifactError("未来区间 API record_page contract 无效")
    records = record_page.get("items")
    if not isinstance(records, list):
        raise FutureRangeArtifactError("未来区间 API records contract 无效")
    if symbol is None:
        selected = cast(list[dict[str, object]], records)
    else:
        selected = [
            record
            for record in records
            if isinstance(record, dict) and _record_matches_symbol(record, symbol)
        ]
    total = len(selected)
    start = (page - 1) * page_size
    items = [
        _record_offset_projection(record, session_offset)
        for record in selected[start : start + page_size]
    ]
    output["record_page"] = {
        "page": page,
        "page_size": page_size,
        "total": total,
        "page_count": (total + page_size - 1) // page_size,
        "session_offset": session_offset,
        "symbol": symbol,
        "items": items,
    }
    return output


def _record_offset_projection(
    record: dict[str, object],
    session_offset: Literal[1, 2, 3] | None,
) -> dict[str, object]:
    output = deepcopy(record)
    offsets = output.get("offsets")
    if not isinstance(offsets, list):
        raise FutureRangeArtifactError("未来区间 record.offsets contract 无效")
    if session_offset is not None:
        output["offsets"] = [
            item
            for item in offsets
            if isinstance(item, dict) and item.get("session_offset") == session_offset
        ]
    return output


def _validate_run_records(records: list[object], run_id: int) -> None:
    for record in records:
        if not isinstance(record, dict) or record.get("run_id") != run_id:
            raise FutureRangeArtifactError("未来区间 artifact record 与 run_id 不一致")
        offsets = record.get("offsets")
        if not isinstance(offsets, list) or any(not isinstance(item, dict) for item in offsets):
            raise FutureRangeArtifactError("未来区间 artifact record.offsets 无效")
        values = [item.get("session_offset") for item in offsets]
        if any(isinstance(value, bool) or not isinstance(value, int) or value not in {1, 2, 3} for value in values):
            raise FutureRangeArtifactError("未来区间 artifact session_offset 无效或重复")
        if len(values) != len(set(values)):
            raise FutureRangeArtifactError("未来区间 artifact session_offset 无效或重复")


def _normalized_symbol(value: str | None) -> str | None:
    normalized = "".join((value or "").split()).upper()
    if not normalized:
        return None
    if len(normalized) > 20 or any(ord(character) < 33 for character in normalized):
        raise ValueError("symbol 格式无效")
    return normalized


def _record_matches_symbol(record: Mapping[str, object], symbol: str) -> bool:
    current = str(record.get("symbol") or "").strip().upper()
    if current == symbol:
        return True
    compact_current = current.replace(".", "")
    compact_symbol = symbol.replace(".", "")
    return compact_current == compact_symbol


def _directory_snapshot(directory: Path) -> _DirectorySnapshot:
    try:
        directory_stat = directory.stat()
    except FileNotFoundError:
        return None, ()
    except OSError as exc:
        raise FutureRangeArtifactError(f"未来区间 artifact 目录无法读取：{directory}") from exc
    if not stat.S_ISDIR(directory_stat.st_mode):
        return None, ()
    identity = (
        directory_stat.st_dev,
        directory_stat.st_ino,
        directory_stat.st_mtime_ns,
        directory_stat.st_ctime_ns,
    )
    try:
        paths = sorted(directory.glob("market-scan-future-range-run-*.json"))
        fingerprints = tuple(
            _file_fingerprint(path)
            for path in paths
            if _RUN_ARTIFACT_PATTERN.fullmatch(path.name) is not None
        )
    except OSError as exc:
        raise FutureRangeArtifactError(
            f"未来区间 artifact 目录无法完整扫描：{directory}"
        ) from exc
    return identity, fingerprints


def _file_fingerprint(path: Path) -> _FileFingerprint:
    try:
        facts = path.lstat()
    except OSError as exc:
        raise FutureRangeArtifactError(f"未来区间 artifact 无法读取：{path}") from exc
    if not stat.S_ISREG(facts.st_mode):
        raise FutureRangeArtifactError(f"未来区间 artifact 不是普通文件：{path}")
    return (
        path,
        facts.st_dev,
        facts.st_ino,
        facts.st_mode,
        facts.st_size,
        facts.st_mtime_ns,
        facts.st_ctime_ns,
    )


def _candidate_fingerprints(
    snapshot: _DirectorySnapshot,
    run_id: int,
) -> tuple[_FileFingerprint, ...]:
    return tuple(
        fingerprint
        for fingerprint in snapshot[1]
        if _filename_run_id(fingerprint[0]) == run_id
    )


def _filename_run_id(path: Path) -> int | None:
    match = _RUN_ARTIFACT_PATTERN.fullmatch(path.name)
    return int(match.group(1)) if match is not None else None


def _load_candidates(
    fingerprints: Sequence[_FileFingerprint],
    cache: Mapping[Path, tuple[_FileFingerprint, dict[str, object]]],
    run_id: int,
) -> dict[Path, tuple[_FileFingerprint, dict[str, object]]]:
    loaded: dict[Path, tuple[_FileFingerprint, dict[str, object]]] = {}
    for fingerprint in fingerprints:
        path = fingerprint[0]
        cached = cache.get(path)
        if cached is not None and cached[0] == fingerprint:
            loaded[path] = cached
            continue
        artifact = load_future_range_artifact(path)
        if artifact.get("schema_version") != FUTURE_RANGE_ARTIFACT_SCHEMA_VERSION:
            raise FutureRangeArtifactError("未来区间 artifact schema_version 不受支持")
        if future_range_artifact_filename(run_id, artifact) != path.name:
            raise FutureRangeArtifactError("未来区间 artifact 文件名与内容摘要不一致")
        current = _file_fingerprint(path)
        if current != fingerprint:
            raise FutureRangeArtifactError("未来区间 artifact 在校验期间发生变化")
        loaded[path] = fingerprint, artifact
    return loaded


def _newest_artifact(
    cached: Sequence[tuple[_FileFingerprint, dict[str, object]]],
) -> dict[str, object] | None:
    if not cached:
        return None
    return max(
        cached,
        key=lambda item: (str(item[1]["generated_at"]), item[0][0].name),
    )[1]


__all__ = [
    "FUTURE_RANGE_API_SCHEMA_VERSION",
    "FutureRangeResearchUnavailable",
    "MarketScanFutureRangeStore",
    "not_generated_future_range_research",
]
