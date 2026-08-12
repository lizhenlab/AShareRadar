"""Read-only API projection over immutable market-scan probability artifacts."""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from copy import deepcopy
from pathlib import Path
import re
import stat
from threading import RLock
from typing import cast

from app.artifacts.io import path_has_only_trusted_aliases
from app.services.market_scan_probability_artifact import (
    PROBABILITY_RESULT_CONTRACT_VERSION,
    ProbabilityArtifactError,
    load_probability_artifact,
)
from app.services.market_scan_probability_research import (
    PROBABILITY_ABSOLUTE_TARGET,
    PROBABILITY_PRIMARY_TARGET,
)


class ProbabilityFilterUnavailable(ValueError):
    """Raised when a probability threshold is requested without calibrated evidence."""


_FileFingerprint = tuple[Path, int, int, int, int, int, int]
_DirectoryIdentity = tuple[int, int, int, int]
_DirectorySnapshot = tuple[_DirectoryIdentity | None, tuple[_FileFingerprint, ...]]
_Projection = tuple[dict[str, object], dict[str, dict[str, object]]]
_RUN_ARTIFACT_PATTERN = re.compile(r"market-scan-probability-run-(\d+)-[0-9a-f]{64}\.json")


class MarketScanProbabilityStore:
    """Locate, verify and project the newest artifact for one persisted run."""

    def __init__(self, directory: Path) -> None:
        self.directory = directory.expanduser().absolute()
        self._lock = RLock()
        self._snapshot: _DirectorySnapshot | None = None
        self._file_cache: dict[Path, tuple[_FileFingerprint, dict[str, object]]] = {}
        self._artifact_cache: dict[int, dict[str, object] | None] = {}
        self._research_cache: dict[int, dict[str, object]] = {}
        self._record_cache: dict[int, dict[str, dict[str, object]]] = {}

    def research_projection(self, run_id: int) -> dict[str, object]:
        with self._lock:
            self._refresh_if_changed()
            return deepcopy(self._research_for_run(run_id))

    def run_projection(
        self,
        run_id: int,
        *,
        symbols: Sequence[str] | None = None,
    ) -> _Projection:
        with self._lock:
            self._refresh_if_changed()
            research = self._research_for_run(run_id)
            records = self._records_for_run(run_id, symbols)
            return deepcopy(research), deepcopy(records)

    def _research_for_run(self, run_id: int) -> dict[str, object]:
        cached = self._research_cache.get(run_id)
        if cached is not None:
            return cached
        artifact = self._artifact_for_run(run_id)
        research = (
            _artifact_research_projection(artifact, run_id)
            if artifact is not None
            else not_generated_probability_research(run_id)
        )
        self._research_cache[run_id] = research
        return research

    def _records_for_run(
        self,
        run_id: int,
        symbols: Sequence[str] | None,
    ) -> dict[str, dict[str, object]]:
        cached = self._record_cache.get(run_id)
        if cached is not None:
            return _selected_records(cached, symbols)
        artifact = self._artifact_for_run(run_id)
        if artifact is None:
            return {}
        records = _artifact_record_projection(artifact, run_id, symbols)
        if symbols is None:
            self._record_cache[run_id] = records
        return records

    def _artifact_for_run(self, run_id: int) -> dict[str, object] | None:
        if run_id in self._artifact_cache:
            return self._artifact_cache[run_id]
        if self._snapshot is None:
            return None
        fingerprints = _candidate_fingerprints(self._snapshot, run_id)
        artifacts = _load_candidates(fingerprints, self._file_cache)
        if _directory_snapshot(self.directory) != self._snapshot:
            raise ProbabilityArtifactError("上涨概率 artifact 目录在读取期间发生变化，请重试")
        self._file_cache.update(artifacts)
        selected = _newest_artifact_for_run(tuple(artifacts.values()), run_id)
        self._artifact_cache[run_id] = selected
        return selected

    def _refresh_if_changed(self) -> None:
        snapshot = _directory_snapshot(self.directory)
        if snapshot == self._snapshot:
            return
        current = {fingerprint[0]: fingerprint for fingerprint in snapshot[1]}
        self._file_cache = {
            path: cached for path, cached in self._file_cache.items()
            if current.get(path) == cached[0]
        }
        self._artifact_cache = {}
        self._research_cache = {}
        self._record_cache = {}
        self._snapshot = snapshot


def _directory_snapshot(directory: Path) -> _DirectorySnapshot:
    try:
        if not path_has_only_trusted_aliases(directory):
            raise ProbabilityArtifactError(f"上涨概率 artifact 路径不是普通目录：{directory}")
        directory_stat = directory.lstat()
    except ProbabilityArtifactError:
        raise
    except FileNotFoundError:
        return None, ()
    except (OSError, RuntimeError) as exc:
        raise ProbabilityArtifactError(f"上涨概率 artifact 目录无法读取：{directory}") from exc
    if not stat.S_ISDIR(directory_stat.st_mode):
        raise ProbabilityArtifactError(f"上涨概率 artifact 路径不是普通目录：{directory}")
    identity = (
        directory_stat.st_dev,
        directory_stat.st_ino,
        directory_stat.st_mtime_ns,
        directory_stat.st_ctime_ns,
    )
    try:
        paths = sorted(directory.glob("market-scan-probability-*.json"))
        fingerprints = tuple(_file_fingerprint(path) for path in paths)
    except OSError as exc:
        raise ProbabilityArtifactError(f"上涨概率 artifact 目录无法完整扫描：{directory}") from exc
    return identity, fingerprints


def _file_fingerprint(path: Path) -> _FileFingerprint:
    try:
        facts = path.lstat()
    except OSError as exc:
        raise ProbabilityArtifactError(f"上涨概率 artifact 无法读取：{path}") from exc
    if not stat.S_ISREG(facts.st_mode):
        raise ProbabilityArtifactError(f"上涨概率 artifact 不是普通文件：{path}")
    return (
        path,
        facts.st_dev,
        facts.st_ino,
        facts.st_mode,
        facts.st_size,
        facts.st_mtime_ns,
        facts.st_ctime_ns,
    )


def _load_candidates(
    fingerprints: Sequence[_FileFingerprint],
    cache: Mapping[Path, tuple[_FileFingerprint, dict[str, object]]],
) -> dict[Path, tuple[_FileFingerprint, dict[str, object]]]:
    loaded: dict[Path, tuple[_FileFingerprint, dict[str, object]]] = {}
    for fingerprint in fingerprints:
        path = fingerprint[0]
        cached = cache.get(path)
        if cached is not None and cached[0] == fingerprint:
            loaded[path] = cached
            continue
        artifact = load_probability_artifact(path)
        try:
            current = _file_fingerprint(path)
        except OSError as exc:
            raise ProbabilityArtifactError(f"上涨概率 artifact 校验后无法复查：{path}") from exc
        if current != fingerprint:
            raise ProbabilityArtifactError(f"上涨概率 artifact 在校验期间发生变化：{path}")
        loaded[path] = fingerprint, artifact
    return loaded


def _candidate_fingerprints(snapshot: _DirectorySnapshot, run_id: int) -> tuple[_FileFingerprint, ...]:
    return tuple(
        fingerprint for fingerprint in snapshot[1]
        if (encoded := _filename_run_id(fingerprint[0])) is None or encoded == run_id
    )


def _filename_run_id(path: Path) -> int | None:
    match = _RUN_ARTIFACT_PATTERN.fullmatch(path.name)
    return int(match.group(1)) if match is not None else None


def _newest_artifact_for_run(
    cached: Sequence[tuple[_FileFingerprint, dict[str, object]]],
    run_id: int,
) -> dict[str, object] | None:
    matches = [artifact for _fingerprint, artifact in cached if run_id in _artifact_run_ids(artifact)]
    if not matches:
        return None
    return max(matches, key=lambda artifact: str(artifact["generated_at"]))


def _artifact_run_ids(artifact: Mapping[str, object]) -> set[int]:
    payload = cast(Mapping[str, object], artifact["payload"])
    studies = cast(Sequence[Mapping[str, object]], payload["studies"])
    return {cast(int, study["run_id"]) for study in studies}


def _artifact_research_projection(
    artifact: Mapping[str, object],
    run_id: int,
) -> dict[str, object]:
    payload = cast(Mapping[str, object], artifact["payload"])
    studies = _run_studies(payload, run_id)
    integrity = cast(Mapping[str, object], artifact["integrity"])
    research = _research_projection(run_id, studies, artifact, integrity)
    research["record_contract_version"] = payload.get("record_contract_version") or "legacy-study-joined-v1"
    return research


def _artifact_record_projection(
    artifact: Mapping[str, object],
    run_id: int,
    symbols: Sequence[str] | None,
) -> dict[str, dict[str, object]]:
    payload = cast(Mapping[str, object], artifact["payload"])
    studies = _run_studies(payload, run_id)
    selected = frozenset(symbols) if symbols is not None else None
    records = [
        item for item in cast(Sequence[Mapping[str, object]], payload["records"])
        if item.get("run_id") == run_id
        and (selected is None or str(item.get("symbol")) in selected)
    ]
    self_contained = payload.get("record_contract_version") == PROBABILITY_RESULT_CONTRACT_VERSION
    return _record_projection(records, studies, merge_legacy_study=not self_contained)


def _run_studies(payload: Mapping[str, object], run_id: int) -> list[Mapping[str, object]]:
    return [
        item for item in cast(Sequence[Mapping[str, object]], payload["studies"])
        if item.get("run_id") == run_id
    ]


def _selected_records(
    records: Mapping[str, dict[str, object]],
    symbols: Sequence[str] | None,
) -> dict[str, dict[str, object]]:
    if symbols is None:
        return dict(records)
    selected = frozenset(symbols)
    return {symbol: values for symbol, values in records.items() if symbol in selected}


def _research_projection(
    run_id: int,
    studies: Sequence[Mapping[str, object]],
    artifact: Mapping[str, object],
    integrity: Mapping[str, object],
) -> dict[str, object]:
    horizons: dict[str, dict[str, object]] = {str(value): {} for value in (1, 5, 20)}
    for study in studies:
        horizon, target = str(study["horizon"]), str(study["target"])
        metadata = dict(cast(Mapping[str, object], study["metadata"]))
        metadata.update(
            status=study["status"],
            target=target,
            horizon=study["horizon"],
            versions=study["versions"],
            digests=study["digests"],
            limitations=study["limitations"],
        )
        horizons[horizon][target] = metadata
    return {
        "schema_version": artifact["schema_version"],
        "run_id": run_id,
        "status": "calibrated_shadow" if _has_calibrated(studies) else "insufficient_data",
        "default_horizon": 5,
        "primary_target": PROBABILITY_PRIMARY_TARGET,
        "horizons": horizons,
        "generated_at": artifact["generated_at"],
        "integrity_digest": integrity["integrity_digest"],
        "integrity_notice": integrity["notice"],
        "production_ranking_effect": "none",
        "automatic_promotion": False,
    }


def _record_projection(
    records: Sequence[Mapping[str, object]],
    studies: Sequence[Mapping[str, object]],
    *,
    merge_legacy_study: bool,
) -> dict[str, dict[str, object]]:
    study_by_key = {
        (str(study["target"]), int(cast(int, study["horizon"]))): study
        for study in studies
    }
    by_symbol: dict[str, dict[str, object]] = {}
    for record in records:
        symbol = str(record["symbol"])
        horizon = str(record["horizon"])
        target = str(record["target"])
        details = dict(cast(Mapping[str, object], record["details"]))
        study = study_by_key.get((target, int(cast(int, record["horizon"]))))
        if merge_legacy_study:
            _merge_record_study_evidence(details, study)
        interval = record.get("confidence_interval")
        details.update(
            status=record["status"],
            probability=record.get("probability"),
            confidence_interval=_interval_projection(interval),
            target=target,
            horizon=record["horizon"],
        )
        horizons = by_symbol.setdefault(symbol, {})
        targets = cast(dict[str, object], horizons.setdefault(horizon, {}))
        targets[target] = details
    return by_symbol


def _merge_record_study_evidence(
    details: dict[str, object],
    study: Mapping[str, object] | None,
) -> None:
    if study is None:
        return
    metadata = cast(Mapping[str, object], study["metadata"])
    for name in ("base_rate", "training_cutoff", "target_definition", "generated_at"):
        details.setdefault(name, metadata.get(name))
    shared = [str(value) for value in cast(Sequence[object], study["limitations"])]
    local = [str(value) for value in cast(Sequence[object], details.get("limitations") or ())]
    details["limitations"] = list(dict.fromkeys([*shared, *local, "legacy_record_requires_study_join"]))


def _interval_projection(value: object) -> dict[str, object] | None:
    if not isinstance(value, list) or len(value) != 2:
        return None
    return {"level": 0.95, "lower": value[0], "upper": value[1], "method": "date_block_bootstrap"}


def not_generated_probability_research(run_id: int) -> dict[str, object]:
    horizons = {
        str(horizon): {
            target: _not_generated_summary(horizon, target)
            for target in (PROBABILITY_PRIMARY_TARGET, PROBABILITY_ABSOLUTE_TARGET)
        }
        for horizon in (1, 5, 20)
    }
    return {
        "schema_version": "market-scan-probability-not-generated-v1",
        "run_id": run_id,
        "status": "not_generated",
        "default_horizon": 5,
        "primary_target": PROBABILITY_PRIMARY_TARGET,
        "horizons": horizons,
        "generated_at": None,
        "integrity_digest": None,
        "integrity_notice": None,
        "production_ranking_effect": "none",
        "automatic_promotion": False,
    }


def _not_generated_summary(horizon: int, target: str) -> dict[str, object]:
    return {
        "status": "not_generated",
        "probability": None,
        "horizon": horizon,
        "target": target,
        "base_rate": None,
        "training_cutoff": None,
        "limitations": ["该批次尚未生成上涨概率 Shadow artifact"],
        "automatic_promotion": False,
    }


def _has_calibrated(studies: Sequence[Mapping[str, object]]) -> bool:
    return any(item.get("status") == "calibrated_shadow" for item in studies)


__all__ = [
    "MarketScanProbabilityStore",
    "ProbabilityFilterUnavailable",
    "not_generated_probability_research",
]
