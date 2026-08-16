"""Fail-closed projection service for individual short-horizon probabilities."""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from copy import deepcopy
from pathlib import Path
import stat
from threading import RLock
from typing import cast

from app.artifacts.io import ArtifactIOError, read_regular_file, sha256_hex
from app.config import PROJECT_ROOT
from app.models.individual_probability import (
    IndividualProbabilityCounts,
    IndividualProbabilityEvidence,
    IndividualProbabilityMetrics,
    IndividualProbabilityStatus,
    IndividualProbabilityTargetContract,
    IndividualUpsideHorizon,
    IndividualUpsideProbabilityReport,
)
from app.services.individual_probability_artifact import (
    INDIVIDUAL_PROBABILITY_ARTIFACT_MAX_BYTES,
    INDIVIDUAL_PROBABILITY_ASSESSMENT_PREFIX,
    INDIVIDUAL_PROBABILITY_ASSESSMENT_SCHEMA_VERSION,
    IndividualProbabilityArtifactError,
    individual_probability_target_contract,
    load_individual_probability_assessment,
    required_official_pit_sessions,
)
from app.utils.audit_time import audit_now_text, parse_audit_time
from app.utils.symbols import standard_symbol


_AssessmentFileFingerprint = tuple[str, int, int, int, int, int, int, str]
_AssessmentSnapshot = tuple[_AssessmentFileFingerprint, ...]
_StoreFingerprint = tuple[str, _AssessmentSnapshot]
_MAX_SNAPSHOT_RETRIES = 3
_LEGACY_SOURCE_LIMITATION = "legacy_official_pit_sources_audit_only_not_current_evidence"
_COMPACT_METRICS_LIMITATION = "compact_horizon_metrics_not_independently_replayable"
_RUNTIME_SOURCE_REPLAY_LIMITATION = "official_pit_source_artifacts_not_runtime_replayed"


class IndividualProbabilityStore:
    """Load the newest immutable assessment; malformed state degrades closed."""

    def __init__(
        self,
        directory: str | Path,
        *,
        fallback_directory: str | Path | None = None,
    ) -> None:
        self.directory = Path(directory).expanduser().absolute()
        self.fallback_directory = Path(fallback_directory).expanduser().absolute() if fallback_directory is not None else None
        self._lock = RLock()
        self._fingerprint: _StoreFingerprint | None = None
        self._assessment: dict[str, object] | None = None

    def latest(self) -> dict[str, object] | None:
        with self._lock:
            for _attempt in range(_MAX_SNAPSHOT_RETRIES):
                directory, scope, snapshot = self._effective_snapshot()
                fingerprint = scope, snapshot
                if fingerprint == self._fingerprint:
                    return deepcopy(self._assessment) if self._assessment is not None else None
                assessment = self._load_latest(directory, snapshot)
                observed_directory, observed_scope, observed_snapshot = self._effective_snapshot()
                if (
                    observed_directory,
                    observed_scope,
                    observed_snapshot,
                ) != (directory, scope, snapshot):
                    continue
                self._assessment = assessment
                self._fingerprint = fingerprint
                return deepcopy(assessment) if assessment is not None else None
            raise IndividualProbabilityArtifactError("个股上涨概率 assessment 目录在读取期间持续变化")

    def _effective_snapshot(
        self,
    ) -> tuple[Path, str, _AssessmentSnapshot]:
        primary = self._directory_fingerprint(self.directory)
        if primary:
            return self.directory, "primary", primary
        if self.fallback_directory is None:
            return self.directory, "primary", ()
        fallback = self._directory_fingerprint(self.fallback_directory)
        return self.fallback_directory, "fallback", fallback

    def _directory_fingerprint(
        self,
        directory: Path,
    ) -> _AssessmentSnapshot:
        if directory.is_symlink():
            raise IndividualProbabilityArtifactError("个股上涨概率 assessment 目录不能包含符号链接")
        if not directory.exists():
            return ()
        _require_real_directory(directory)
        values: list[_AssessmentFileFingerprint] = []
        for path in directory.glob(f"{INDIVIDUAL_PROBABILITY_ASSESSMENT_PREFIX}-*.json"):
            facts = path.lstat()
            if not stat.S_ISREG(facts.st_mode):
                raise IndividualProbabilityArtifactError("个股上涨概率 assessment 目录包含非普通候选文件")
            try:
                encoded = read_regular_file(
                    path,
                    max_bytes=INDIVIDUAL_PROBABILITY_ARTIFACT_MAX_BYTES,
                )
            except ArtifactIOError as exc:
                raise IndividualProbabilityArtifactError("个股上涨概率 assessment 候选文件无法安全指纹化") from exc
            current = path.lstat()
            if not stat.S_ISREG(current.st_mode):
                raise IndividualProbabilityArtifactError("个股上涨概率 assessment 目录包含非普通候选文件")
            values.append(
                (
                    path.name,
                    current.st_dev,
                    current.st_ino,
                    current.st_mode,
                    current.st_size,
                    current.st_mtime_ns,
                    current.st_ctime_ns,
                    sha256_hex(encoded),
                )
            )
        return tuple(sorted(values))

    def _load_latest(
        self,
        directory: Path,
        snapshot: Sequence[_AssessmentFileFingerprint],
    ) -> dict[str, object] | None:
        if not snapshot:
            return None
        loaded: list[tuple[float, str, dict[str, object]]] = []
        for name, _dev, _ino, _mode, _size, _mtime, _ctime, _content_digest in snapshot:
            try:
                artifact = load_individual_probability_assessment(directory / name)
            except IndividualProbabilityArtifactError as exc:
                raise IndividualProbabilityArtifactError("个股上涨概率 assessment 集合包含损坏证据，拒绝回退旧版本") from exc
            digest = str(cast(Mapping[str, object], artifact["integrity"])["integrity_digest"])
            expected_name = f"{INDIVIDUAL_PROBABILITY_ASSESSMENT_PREFIX}-{digest}.json"
            if name != expected_name:
                raise IndividualProbabilityArtifactError("个股上涨概率 assessment 文件名与内容地址不一致")
            try:
                epoch = parse_audit_time(str(artifact["generated_at"])).timestamp()
            except ValueError as exc:
                raise IndividualProbabilityArtifactError("个股上涨概率 assessment generated_at 无效") from exc
            loaded.append((epoch, digest, artifact))
        newest_epoch = max(item[0] for item in loaded)
        newest = [item for item in loaded if item[0] == newest_epoch]
        if len(newest) != 1:
            raise IndividualProbabilityArtifactError("个股上涨概率 assessment 存在同 generated_at 的冲突证据")
        return newest[0][2]


def individual_probability_store_for_cache_path(cache_path: str | Path) -> IndividualProbabilityStore:
    cache = Path(cache_path).expanduser().absolute()
    root = cache.parent
    fallback = PROJECT_ROOT / "docs" / "research" / "artifacts" if cache == PROJECT_ROOT / "data" / "ashare_radar.sqlite3" else None
    return IndividualProbabilityStore(
        root / "research" / "individual_probability",
        fallback_directory=fallback,
    )


def project_individual_upside_probability(
    symbol: str,
    assessment: Mapping[str, object] | None,
    *,
    generated_at: str | None = None,
) -> IndividualUpsideProbabilityReport:
    """Project typed evidence; no artifact/gate means a null probability."""
    normalized = standard_symbol(symbol)
    timestamp = generated_at or audit_now_text()
    contract = IndividualProbabilityTargetContract.model_validate(individual_probability_target_contract())
    if assessment is None:
        return _not_generated_report(normalized, None, timestamp, contract)
    try:
        artifact = load_or_validate_assessment(assessment)
        return _assessment_report(normalized, timestamp, contract, artifact)
    except (IndividualProbabilityArtifactError, KeyError, TypeError, ValueError):
        return _not_generated_report(
            normalized,
            None,
            timestamp,
            contract,
            limitation="assessment_invalid_or_unreadable",
        )


def load_or_validate_assessment(value: Mapping[str, object]) -> dict[str, object]:
    from app.services.individual_probability_artifact import (  # local to keep service import light
        verify_individual_probability_assessment,
    )

    return verify_individual_probability_assessment(value)


def _assessment_report(
    symbol: str,
    _request_generated_at: str,
    contract: IndividualProbabilityTargetContract,
    assessment: Mapping[str, object],
) -> IndividualUpsideProbabilityReport:
    assessment_generated_at = assessment.get("generated_at")
    if not isinstance(assessment_generated_at, str) or not assessment_generated_at.strip():
        raise IndividualProbabilityArtifactError("assessment generated_at 无效")
    payload = _mapping(assessment["payload"])
    source = _mapping(payload["source"])
    official = _mapping(payload["official_pit"])
    raw_horizons = _mapping(payload["horizons"])
    digest = str(_mapping(assessment["integrity"])["integrity_digest"])
    source_contract_bound = assessment.get("schema_version") == INDIVIDUAL_PROBABILITY_ASSESSMENT_SCHEMA_VERSION
    # A compact assessment only carries source identities. Until the runtime
    # store locates and replays every content-addressed source artifact, those
    # self-described identities are audit metadata, never current evidence.
    runtime_source_replayed = False
    all_official = False
    horizons = [_horizon_projection(_mapping(raw_horizons[str(holding)]), all_official=all_official) for holding in (1, 2, 3)]
    any_qualified = any(item.status == "calibrated_shadow" for item in horizons)
    return IndividualUpsideProbabilityReport(
        symbol=symbol,
        signal_date=_official_signal_date(official, runtime_source_replayed),
        generated_at=assessment_generated_at,
        status="calibrated_shadow" if any_qualified else "insufficient_data",
        target_contract=contract,
        horizons=horizons,
        evidence=_report_evidence(
            digest,
            source,
            official,
            source_contract_bound=runtime_source_replayed,
            selection_qualified=any_qualified,
        ),
        limitations=_report_limitations(
            payload,
            official,
            source_contract_bound=source_contract_bound,
            runtime_source_replayed=runtime_source_replayed,
        ),
    )


def _official_signal_date(
    official: Mapping[str, object],
    source_contract_bound: bool,
) -> str | None:
    dates = official.get("session_dates")
    return str(dates[-1]) if source_contract_bound and isinstance(dates, list) and dates else None


def _report_evidence(
    digest: str,
    source: Mapping[str, object],
    official: Mapping[str, object],
    *,
    source_contract_bound: bool,
    selection_qualified: bool,
) -> IndividualProbabilityEvidence:
    return IndividualProbabilityEvidence(
        assessment_digest=digest,
        history_manifest_digest=cast(str, source.get("history_manifest_digest")),
        history_database_sha256=cast(str, source.get("history_database_sha256")),
        official_pit_session_count=(int(cast(int, official.get("session_count"))) if source_contract_bound else 0),
        required_official_pit_session_count=int(cast(int, official.get("required_session_count"))),
        historical_replay_session_count=int(cast(int, source.get("historical_replay_session_count"))),
        historical_replay_official=source.get("historical_replay_official") is True,
        selection_qualified=selection_qualified,
    )


def _report_limitations(
    payload: Mapping[str, object],
    official: Mapping[str, object],
    *,
    source_contract_bound: bool,
    runtime_source_replayed: bool,
) -> list[str]:
    raw = payload.get("limitations")
    limitations = [str(value) for value in raw] if isinstance(raw, list) else []
    limitations.append(_COMPACT_METRICS_LIMITATION)
    if not source_contract_bound and int(cast(int, official.get("session_count"))) > 0:
        limitations.append(_LEGACY_SOURCE_LIMITATION)
    if source_contract_bound and not runtime_source_replayed and int(cast(int, official.get("session_count"))) > 0:
        limitations.append(_RUNTIME_SOURCE_REPLAY_LIMITATION)
    return list(dict.fromkeys(limitations))


def _horizon_projection(
    evidence: Mapping[str, object],
    *,
    all_official: bool,
) -> IndividualUpsideHorizon:
    raw_counts = _mapping(evidence["counts"])
    raw_metrics = evidence.get("calibration_metrics")
    selection = evidence.get("selection_qualified") is True
    qualified = all_official and selection
    raw_reasons = evidence.get("gate_reasons")
    reasons = [str(value) for value in raw_reasons] if isinstance(raw_reasons, list) else []
    if not all_official:
        reasons.append("official_pit_and_replay_gate_not_satisfied")
    status: IndividualProbabilityStatus = "calibrated_shadow" if qualified else "insufficient_data"
    # The compact assessment intentionally contains no current-stock model
    # coefficients. It therefore cannot produce a point estimate yet, even if
    # future evidence gates pass. Keep this extra guard explicit.
    if qualified:
        status = "insufficient_data"
        reasons.append("current_stock_replayable_predictor_not_persisted")
    return IndividualUpsideHorizon(
        display_day=cast(int, evidence["display_day"]),
        holding_sessions=cast(int, evidence["holding_sessions"]),
        status=status,
        probability=None,
        confidence_interval=None,
        base_rate=cast(float | None, evidence.get("base_rate")),
        counts=IndividualProbabilityCounts.model_validate(raw_counts),
        calibration_metrics=(IndividualProbabilityMetrics.model_validate(_project_metrics(raw_metrics)) if isinstance(raw_metrics, Mapping) else None),
        training_cutoff=cast(str | None, evidence.get("training_cutoff")),
        model_version=cast(str | None, evidence.get("model_version")),
        feature_version=str(evidence["feature_version"]),
        evidence_digest=cast(str | None, evidence.get("evidence_digest")),
        gate_reasons=list(dict.fromkeys(reasons)),
    )


def _not_generated_report(
    symbol: str,
    signal_date: str | None,
    generated_at: str,
    contract: IndividualProbabilityTargetContract,
    *,
    limitation: str = "assessment_not_generated",
) -> IndividualUpsideProbabilityReport:
    empty = IndividualProbabilityCounts()
    horizons = [
        IndividualUpsideHorizon(
            display_day=holding + 1,
            holding_sessions=holding,
            status="not_generated",
            counts=empty,
            feature_version=contract.feature_version,
            gate_reasons=[limitation],
        )
        for holding in (1, 2, 3)
    ]
    return IndividualUpsideProbabilityReport(
        symbol=symbol,
        signal_date=signal_date,
        generated_at=generated_at,
        status="not_generated",
        target_contract=contract,
        horizons=horizons,
        evidence=IndividualProbabilityEvidence(
            required_official_pit_session_count=required_official_pit_sessions(),
        ),
        limitations=[limitation, "shadow_research_only_no_production_effect"],
    )


def _project_metrics(value: Mapping[str, object]) -> dict[str, object]:
    projected = dict(value)
    raw_interval = projected.get("actual_positive_rate_ci_95")
    if isinstance(raw_interval, list | tuple) and len(raw_interval) == 2:
        projected["actual_positive_rate_ci_95"] = {
            "lower": raw_interval[0],
            "upper": raw_interval[1],
            "level": 0.95,
        }
    return projected


def _mapping(value: object) -> dict[str, object]:
    if not isinstance(value, Mapping):
        raise ValueError("expected mapping")
    return dict(value)


def _require_real_directory(path: Path) -> None:
    current = path.absolute()
    while True:
        try:
            facts = current.lstat()
        except OSError as exc:
            raise IndividualProbabilityArtifactError("个股上涨概率 assessment 目录不可访问") from exc
        if stat.S_ISLNK(facts.st_mode):
            raise IndividualProbabilityArtifactError("个股上涨概率 assessment 目录不能包含符号链接")
        if current == current.parent:
            break
        current = current.parent
    if not stat.S_ISDIR(path.lstat().st_mode):
        raise IndividualProbabilityArtifactError("个股上涨概率 assessment 路径必须是目录")


__all__ = [
    "IndividualProbabilityStore",
    "individual_probability_store_for_cache_path",
    "project_individual_upside_probability",
]
