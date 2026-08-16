"""Read-only API projection over immutable market-scan probability artifacts."""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from copy import deepcopy
from datetime import datetime, timezone
from pathlib import Path
import re
import stat
from threading import RLock
from typing import cast

from app.artifacts.io import path_has_only_trusted_aliases
from app.services.market_scan_probability_artifact import (
    LEGACY_PROBABILITY_RESULT_CONTRACT_VERSION,
    LEGACY_SCORE_BOUND_PROBABILITY_RESULT_CONTRACT_VERSION,
    PROBABILITY_ARTIFACT_DIGEST_SCOPE,
    PROBABILITY_RESULT_CONTRACT_VERSION,
    ProbabilityArtifactError,
    load_probability_artifact,
)
from app.services.market_scan_probability_research import (
    PROBABILITY_ABSOLUTE_TARGET,
    PROBABILITY_PRIMARY_TARGET,
)
from app.services.market_scan_probability import (
    ProbabilityReplayError,
    build_probability_filter_qualification,
    probability_filter_qualified,
    verify_probability_filter_authorization_artifact,
)


class ProbabilityFilterUnavailable(ValueError):
    """Raised when a probability threshold is requested without calibrated evidence."""


class ProbabilityResearchUnavailable(ValueError):
    """Raised when a persisted run is outside the official full-market contract."""


_FileFingerprint = tuple[Path, int, int, int, int, int, int]
_DirectoryIdentity = tuple[int, int, int, int]
_DirectorySnapshot = tuple[_DirectoryIdentity | None, tuple[_FileFingerprint, ...]]
_Projection = tuple[dict[str, object], dict[str, dict[str, object]]]
_RUN_ARTIFACT_PATTERN = re.compile(r"market-scan-probability-run-(\d+)-[0-9a-f]{64}\.json")
PROBABILITY_INTERACTIVE_ARTIFACT_MAX_BYTES = 64 * 1024 * 1024
PROBABILITY_OVERSIZE_AVAILABILITY = "legacy_artifact_exceeds_interactive_budget"


class MarketScanProbabilityStore:
    """Locate, verify and project the newest artifact for one persisted run."""

    def __init__(self, directory: Path) -> None:
        self.directory = directory.expanduser().absolute()
        self._lock = RLock()
        self._refresh_lock = RLock()
        self._snapshot: _DirectorySnapshot | None = None
        self._file_cache: dict[Path, tuple[_FileFingerprint, dict[str, object]]] = {}
        self._artifact_cache: dict[int, dict[str, object] | None] = {}
        self._unavailable_cache: dict[int, str] = {}
        self._research_cache: dict[int, dict[str, object]] = {}
        self._record_cache: dict[int, dict[str, dict[str, object]]] = {}

    def research_projection(self, run_id: int) -> dict[str, object]:
        self._prepare_run(run_id)
        with self._lock:
            return deepcopy(self._research_for_run(run_id))

    def run_projection(
        self,
        run_id: int,
        *,
        symbols: Sequence[str] | None = None,
    ) -> _Projection:
        self._prepare_run(run_id)
        with self._lock:
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
            else not_generated_probability_research(
                run_id,
                availability=self._unavailable_cache.get(run_id),
            )
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
        return self._artifact_cache.get(run_id)

    def _prepare_run(self, run_id: int) -> None:
        observed = _directory_snapshot(self.directory)
        with self._lock:
            if observed == self._snapshot and run_id in self._artifact_cache:
                return
        acquired = self._refresh_lock.acquire(blocking=False)
        if not acquired:
            # Another thread is verifying a complete candidate snapshot. Existing
            # verified runs remain readable from the prior atomic snapshot.
            with self._lock:
                if run_id in self._artifact_cache:
                    return
            self._refresh_lock.acquire()
        try:
            self._verify_and_publish_run(run_id)
        finally:
            self._refresh_lock.release()

    def _verify_and_publish_run(self, run_id: int) -> None:
        for _attempt in range(3):
            snapshot = _directory_snapshot(self.directory)
            with self._lock:
                if snapshot == self._snapshot and run_id in self._artifact_cache:
                    return
                cache = _matching_file_cache(snapshot, self._file_cache)
            fingerprints = _candidate_fingerprints(snapshot, run_id)
            oversized = any(
                fingerprint[4] > PROBABILITY_INTERACTIVE_ARTIFACT_MAX_BYTES
                for fingerprint in fingerprints
            )
            # Deep JSON verification deliberately runs outside the state lock.
            # Oversized historical artifacts are never partially parsed on an
            # interactive request; they remain available for offline replay.
            loaded = {} if oversized else _load_candidates(fingerprints, cache)
            if _directory_snapshot(self.directory) != snapshot:
                continue
            selected = _newest_artifact_for_run(tuple(loaded.values()), run_id)
            with self._lock:
                if snapshot != self._snapshot:
                    self._file_cache = cache
                    self._artifact_cache = {}
                    self._unavailable_cache = {}
                    self._research_cache = {}
                    self._record_cache = {}
                    self._snapshot = snapshot
                self._file_cache.update(loaded)
                self._artifact_cache[run_id] = selected
                if oversized:
                    self._unavailable_cache[run_id] = PROBABILITY_OVERSIZE_AVAILABILITY
                else:
                    self._unavailable_cache.pop(run_id, None)
                return
        raise ProbabilityArtifactError("上涨概率 artifact 目录在读取期间发生变化，请重试")


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


def _matching_file_cache(
    snapshot: _DirectorySnapshot,
    cache: Mapping[Path, tuple[_FileFingerprint, dict[str, object]]],
) -> dict[Path, tuple[_FileFingerprint, dict[str, object]]]:
    current = {fingerprint[0]: fingerprint for fingerprint in snapshot[1]}
    return {
        path: cached for path, cached in cache.items()
        if current.get(path) == cached[0]
    }


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
    sealed = [artifact for artifact in matches if _generated_at_is_content_bound(artifact)]
    if sealed:
        matches = sealed
    elif len(matches) != 1:
        raise ProbabilityArtifactError(
            f"run {run_id} 存在多个 legacy probability artifacts，不能信任未封印 generated_at 排序"
        )
    newest_at = max(_generated_at_order(artifact) for artifact in matches)
    newest = [artifact for artifact in matches if _generated_at_order(artifact) == newest_at]
    if len(newest) != 1:
        raise ProbabilityArtifactError(f"run {run_id} 存在同 generated_at 的冲突 probability artifacts")
    return newest[0]


def _generated_at_is_content_bound(artifact: Mapping[str, object]) -> bool:
    integrity = artifact.get("integrity")
    return bool(
        isinstance(integrity, Mapping)
        and integrity.get("scope") == PROBABILITY_ARTIFACT_DIGEST_SCOPE
    )


def _generated_at_order(artifact: Mapping[str, object]) -> datetime:
    try:
        parsed = datetime.fromisoformat(str(artifact["generated_at"]).replace("Z", "+00:00"))
    except ValueError as exc:
        raise ProbabilityArtifactError("上涨概率 artifact generated_at 无效") from exc
    if parsed.tzinfo is None:
        raise ProbabilityArtifactError("上涨概率 artifact generated_at 必须包含时区")
    return parsed.astimezone(timezone.utc)


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
    research["run_binding"] = _artifact_run_binding(payload, studies, run_id)
    return research


def _artifact_run_binding(
    payload: Mapping[str, object],
    studies: Sequence[Mapping[str, object]],
    run_id: int,
) -> dict[str, object]:
    record_contract = payload.get("record_contract_version") or "legacy-study-joined-v1"
    cohort = _unique_study_contract(studies, _study_binding)
    score_contract = _unique_study_contract(studies, _study_score_contract)
    source_integrity_digest = _unique_study_source_integrity_digest(studies)
    quote_dates = _artifact_run_quote_dates(payload, run_id)
    rule_version = cohort.get("rule_version") if isinstance(cohort, Mapping) else None
    verified = _binding_is_verified(
        record_contract,
        cohort,
        score_contract,
        source_integrity_digest,
        quote_dates,
        rule_version,
    )
    return {
        "schema_version": "market-scan-probability-run-binding-v1",
        "binding_status": "verified" if verified else "legacy_unbound",
        "legacy": not verified,
        "run_id": run_id,
        "mode": cohort.get("mode") if isinstance(cohort, Mapping) else None,
        "scope": cohort.get("scope") if isinstance(cohort, Mapping) else None,
        "rule_version": rule_version,
        "quote_date": next(iter(quote_dates)) if len(quote_dates) == 1 else None,
        "data_date": next(iter(quote_dates)) if len(quote_dates) == 1 else None,
        "scan_rule_hash": _run_contract_hash(rule_version),
        "production_score_rule_version": (
            score_contract.get("production_score_rule_version")
            if isinstance(score_contract, Mapping)
            else None
        ),
        "production_score_spec_hash": (
            _run_contract_hash(score_contract.get("production_score_spec_hash"))
            if isinstance(score_contract, Mapping)
            else None
        ),
        "source_integrity_digest": source_integrity_digest,
        "cohort_contract": dict(cohort) if isinstance(cohort, Mapping) else None,
        "record_contract_version": record_contract,
    }


def _unique_study_contract(
    studies: Sequence[Mapping[str, object]],
    projector,
) -> dict[str, object] | None:
    values = {_canonical_binding_value(projector(item)) for item in studies}
    return projector(studies[0]) if studies and len(values) == 1 else None


def _binding_is_verified(
    record_contract: object,
    cohort: Mapping[str, object] | None,
    score_contract: Mapping[str, object] | None,
    source_integrity_digest: str | None,
    quote_dates: set[str],
    rule_version: object,
) -> bool:
    return bool(
        record_contract == PROBABILITY_RESULT_CONTRACT_VERSION
        and isinstance(cohort, Mapping)
        and cohort.get("legacy_global") is not True
        and len(quote_dates) == 1
        and _run_contract_hash(rule_version) is not None
        and isinstance(score_contract, Mapping)
        and _run_contract_hash(score_contract.get("production_score_spec_hash")) is not None
        and bool(score_contract.get("production_score_rule_version"))
        and source_integrity_digest is not None
    )


def _study_binding(study: Mapping[str, object]) -> dict[str, object]:
    metadata = cast(Mapping[str, object], study["metadata"])
    cohort = metadata.get("cohort_contract", metadata.get("cohort"))
    return dict(cohort) if isinstance(cohort, Mapping) and cohort else {"legacy_global": True}


def _study_score_contract(study: Mapping[str, object]) -> dict[str, object]:
    metadata = cast(Mapping[str, object], study["metadata"])
    contract = metadata.get("production_score_contract")
    return dict(contract) if isinstance(contract, Mapping) and contract else {"legacy_unbound": True}


def _unique_study_source_integrity_digest(
    studies: Sequence[Mapping[str, object]],
) -> str | None:
    values = {
        cast(Mapping[str, object], study["metadata"]).get("source_integrity_digest")
        for study in studies
    }
    if len(values) != 1:
        return None
    value = next(iter(values))
    return (
        str(value)
        if isinstance(value, str) and re.fullmatch(r"[0-9a-f]{64}", value)
        else None
    )


def _artifact_run_quote_dates(payload: Mapping[str, object], run_id: int) -> set[str]:
    feature_rows = payload.get("feature_evidence")
    if isinstance(feature_rows, Sequence) and not isinstance(feature_rows, str | bytes):
        dates = {
            str(item.get("quote_date"))
            for item in feature_rows
            if isinstance(item, Mapping) and item.get("run_id") == run_id and item.get("quote_date")
        }
        if dates:
            return dates
    return {
        str(details.get("quote_date"))
        for item in cast(Sequence[Mapping[str, object]], payload["records"])
        if item.get("run_id") == run_id
        for details in (item.get("details"),)
        if isinstance(details, Mapping) and details.get("quote_date")
    }


def _canonical_binding_value(value: Mapping[str, object]) -> tuple[tuple[str, str], ...]:
    return tuple(sorted((str(key), str(item)) for key, item in value.items()))


def _run_contract_hash(value: object) -> str | None:
    suffix = str(value or "").rsplit(":", 1)[-1]
    return suffix if re.fullmatch(r"[0-9a-f]{64}", suffix) else None


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
    self_contained = payload.get("record_contract_version") in {
        LEGACY_PROBABILITY_RESULT_CONTRACT_VERSION,
        LEGACY_SCORE_BOUND_PROBABILITY_RESULT_CONTRACT_VERSION,
        PROBABILITY_RESULT_CONTRACT_VERSION,
    }
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
        authorization = metadata.get("filter_qualification")
        bound_authorization = None
        if isinstance(authorization, Mapping):
            try:
                bound_authorization = verify_probability_filter_authorization_artifact(
                    authorization, metadata,
                )
            except ProbabilityReplayError:
                bound_authorization = None
        if bound_authorization is not None:
            metadata["filter_qualification"] = bound_authorization
        metadata["filter_qualified"] = probability_filter_qualified(
            metadata,
            bound_authorization,
        )
        metadata["filter_qualification_evaluation"] = build_probability_filter_qualification(
            metadata,
            bound_authorization,
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
        interval = record.get("calibration_adjusted_probability_interval")
        if interval is None:
            interval = record.get("confidence_interval")  # legacy v2/v3 read-only adapter
        details_bias = record.get("calibration_bias_interval")
        if details_bias is None:
            details_bias = details.get("calibration_offset_ci_95")
        details.update(
            status=record["status"],
            probability=record.get("probability"),
            calibration_bias_interval=_bias_interval_projection(details_bias),
            calibration_adjusted_probability_interval=_interval_projection(interval),
            target=target,
            horizon=record["horizon"],
            holding_period_sessions=record["horizon"],
            target_session_offset=int(cast(int, record["horizon"])) + 1,
        )
        details.pop("confidence_interval", None)
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
    return {
        "level": 0.95,
        "lower": value[0],
        "upper": value[1],
        "method": "date_block_bootstrap_calibration_offset",
        "semantics": "calibration_adjusted_probability_interval_not_individual_outcome_interval",
    }


def _bias_interval_projection(value: object) -> dict[str, object] | None:
    if not isinstance(value, list) or len(value) != 2:
        return None
    return {
        "level": 0.95,
        "lower": value[0],
        "upper": value[1],
        "method": "date_block_bootstrap_signed_calibration_bias",
        "semantics": "signed_observed_rate_minus_probability_bias",
    }


def not_generated_probability_research(
    run_id: int,
    *,
    availability: str | None = None,
) -> dict[str, object]:
    horizons = {
        str(horizon): {
            target: _not_generated_summary(horizon, target)
            for target in (PROBABILITY_PRIMARY_TARGET, PROBABILITY_ABSOLUTE_TARGET)
        }
        for horizon in (1, 5, 20)
    }
    research: dict[str, object] = {
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
    if availability is not None:
        research["availability"] = availability
        research["limitations"] = [availability]
    return research


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
        "filter_qualified": False,
        "filter_qualification_evaluation": None,
    }


def _has_calibrated(studies: Sequence[Mapping[str, object]]) -> bool:
    return any(item.get("status") == "calibrated_shadow" for item in studies)


__all__ = [
    "MarketScanProbabilityStore",
    "ProbabilityFilterUnavailable",
    "ProbabilityResearchUnavailable",
    "not_generated_probability_research",
]
