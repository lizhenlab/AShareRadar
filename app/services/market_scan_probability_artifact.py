"""Immutable JSON artifacts for full-market Shadow probability research.

The SHA-256 value in this format is an integrity digest, not a digital
signature.  This module never opens or writes the SQLite database.
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from datetime import date, datetime, timedelta, timezone
import math
from pathlib import Path
import re
from typing import cast

from app.artifacts.io import (
    ArtifactCanonicalJsonError,
    ArtifactContentConflictError,
    ArtifactDuplicateKeyError,
    ArtifactIOError,
    ArtifactNonFiniteConstantError,
    canonical_json_text,
    decode_json_bytes,
    read_regular_file,
    sha256_hex,
)
from app.db.market_scan_artifact_lease import (
    MarketScanArtifactLeaseError,
    publish_market_scan_artifact,
    require_project_managed_artifact_database,
    verified_market_scan_artifact_publication,
)
from app.services.market_scan_probability import (
    ProbabilityConfig,
    ProbabilitySample,
    ProbabilityTarget,
    fit_shadow_probability,
)
from app.utils.clock import utc_now


PROBABILITY_ARTIFACT_SCHEMA_VERSION = "market-scan-probability-artifact-v1"
PROBABILITY_ARTIFACT_DIGEST_ALGORITHM = "sha256"
PROBABILITY_ARTIFACT_DIGEST_SCOPE = "generated_at+payload"
PROBABILITY_ARTIFACT_INTEGRITY_NOTICE = "integrity_digest_not_a_signature"
LEGACY_PROBABILITY_RESULT_CONTRACT_VERSION = "market-scan-probability-result-v2-self-contained"
LEGACY_SCORE_BOUND_PROBABILITY_RESULT_CONTRACT_VERSION = "market-scan-probability-result-v3-score-bound"
PROBABILITY_RESULT_CONTRACT_VERSION = "market-scan-probability-result-v4-explicit-intervals"
PROBABILITY_ARTIFACT_SET_REPLAY_SCHEMA_VERSION = "market-scan-probability-artifact-set-replay-v1"
PROBABILITY_ARTIFACT_HORIZONS = frozenset({1, 5, 20})
PROBABILITY_ARTIFACT_TARGETS = frozenset({"net_excess_positive", "absolute_net_positive", "net_return_positive"})
PROBABILITY_ARTIFACT_STATUSES = frozenset({"calibrated_shadow", "insufficient_data"})
# Existing full-market v1 artifacts are about 192 MiB. Keep a bounded 256 MiB
# compatibility envelope while still rejecting unexpectedly large local files.
PROBABILITY_ARTIFACT_MAX_BYTES = 256 * 1024 * 1024
PROBABILITY_MANAGED_DIRECTORY = Path("market-scan-probability")

_SELF_CONTAINED_RESULT_CONTRACTS = frozenset(
    {
        LEGACY_PROBABILITY_RESULT_CONTRACT_VERSION,
        LEGACY_SCORE_BOUND_PROBABILITY_RESULT_CONTRACT_VERSION,
        PROBABILITY_RESULT_CONTRACT_VERSION,
    }
)
_READ_ONLY_RESULT_CONTRACTS = frozenset(
    {
        LEGACY_PROBABILITY_RESULT_CONTRACT_VERSION,
        LEGACY_SCORE_BOUND_PROBABILITY_RESULT_CONTRACT_VERSION,
    }
)

_TOP_LEVEL_KEYS = frozenset({"schema_version", "generated_at", "payload", "integrity"})
_LEGACY_PAYLOAD_KEYS = frozenset({"studies", "records"})
_PAYLOAD_KEYS = frozenset({"record_contract_version", "feature_evidence", "studies", "records"})
_STUDY_KEYS = frozenset({"run_id", "target", "horizon", "status", "versions", "digests", "limitations", "metadata"})
_LEGACY_RECORD_KEYS = frozenset({"run_id", "symbol", "target", "horizon", "status", "probability", "confidence_interval", "details"})
_CURRENT_RECORD_KEYS = frozenset(
    {
        "run_id",
        "symbol",
        "target",
        "horizon",
        "status",
        "probability",
        "calibration_bias_interval",
        "calibration_adjusted_probability_interval",
        "details",
    }
)
_INTEGRITY_KEYS = frozenset({"algorithm", "scope", "integrity_digest", "notice"})
_REQUIRED_VERSION_KEYS = frozenset({"model", "calibrator", "feature", "label", "cost_model"})
_REQUIRED_DIGEST_KEYS = frozenset({"input", "model", "calibrator"})
_CURRENT_REQUIRED_RESULT_DETAIL_KEYS = frozenset(
    {
        "record_contract_version",
        "sample_id",
        "quote_date",
        "feature_evidence_key",
        "dimensions",
        "feature_vector_digest",
        "source_evidence_digest",
        "mature_horizon",
        "executable",
        "model_target",
        "fold_id",
        "observed_label",
        "label_status",
        "label_reason",
        "net_return",
        "market_benchmark_net_return",
        "net_excess_return",
        "entry_date",
        "exit_date",
        "raw_probability",
        "empirical_bayes_probability",
        "calibration_adjusted_probability_interval_definition",
        "versions",
        "digests",
        "base_rate",
        "training_cutoff",
        "target_definition",
        "counts",
        "contract",
        "calibration_summary",
        "calibration_offset_ci_95",
        "limitations",
        "generated_at",
        "automatic_promotion",
    }
)
_LEGACY_REQUIRED_RESULT_DETAIL_KEYS = frozenset(
    (_CURRENT_REQUIRED_RESULT_DETAIL_KEYS - {"calibration_adjusted_probability_interval_definition"}) | {"confidence_interval_definition"}
)
_SHA256_PATTERN = re.compile(r"[0-9a-f]{64}")
_REPLAY_STUDY_FIELDS = (
    "schema_version",
    "status",
    "fit_status",
    "selection_qualified",
    "selection_qualification",
    "probability",
    "horizon",
    "target_definition",
    "base_rate",
    "actual_positive_rate_interval",
    "model_version",
    "feature_version",
    "label_version",
    "cost_model_version",
    "label_contract_digest",
    "label_contract_binding",
    "generated_at",
    "input_digest",
    "contract",
    "limitations",
    "split",
    "counts",
    "training_cutoff",
    "model",
    "calibrator",
    "isotonic_calibrator",
    "empirical_bayes_baseline",
    "calibration_metrics",
    "calibration_candidates",
    "folds",
    "model_digest",
    "calibrator_digest",
    "isotonic_calibrator_digest",
    "baseline_digest",
    "evidence_digest",
)


class ProbabilityArtifactError(ValueError):
    """Raised when a probability artifact cannot be safely built or loaded."""


def canonical_probability_artifact_json(value: object) -> str:
    """Render finite JSON with stable key ordering and no insignificant spaces."""
    normalized = _json_value(value, "JSON")
    try:
        return canonical_json_text(normalized)
    except ArtifactCanonicalJsonError as exc:  # defensive: _json_value already rejects these cases
        raise ProbabilityArtifactError("上涨概率 artifact 不是有限 JSON") from exc


def _canonical_validated_json(value: object) -> str:
    try:
        return canonical_json_text(value)
    except ArtifactCanonicalJsonError as exc:
        raise ProbabilityArtifactError("上涨概率 artifact 不是有限 JSON") from exc


def probability_payload_integrity_digest(payload: Mapping[str, object]) -> str:
    """Return a component/payload SHA-256 digest.

    Top-level artifact integrity additionally binds ``generated_at`` via
    :func:`_artifact_integrity_digest`; this helper remains for component hashes.
    """
    canonical = canonical_probability_artifact_json(_digest_payload(payload))
    return sha256_hex(canonical)


def probability_artifact_integrity_digest(
    generated_at: str,
    payload: Mapping[str, object],
) -> str:
    """Return the current top-level content address including generation time."""

    _validated_artifact_timestamp(generated_at, "generated_at")
    return _artifact_integrity_digest(generated_at, payload)


def _validated_payload_integrity_digest(payload: Mapping[str, object]) -> str:
    return sha256_hex(_canonical_validated_json(_digest_payload(payload)))


def _artifact_integrity_digest(generated_at: str, payload: Mapping[str, object]) -> str:
    identity = {"generated_at": generated_at, "payload": _digest_payload(payload)}
    return sha256_hex(_canonical_validated_json(identity))


def _digest_payload(payload: Mapping[str, object]) -> object:
    if payload.get("record_contract_version") in _SELF_CONTAINED_RESULT_CONTRACTS:
        return _integrity_payload(payload)
    return payload


def _integrity_payload(value: object) -> object:
    if isinstance(value, Mapping):
        return {str(key): _integrity_payload(item) for key, item in value.items() if key != "generated_at"}
    if isinstance(value, list):
        return [_integrity_payload(item) for item in value]
    return value


def build_probability_artifact(
    payload: Mapping[str, object],
    *,
    generated_at: str,
) -> dict[str, object]:
    """Build and verify one schema-v1 artifact from a complete research payload."""
    normalized_payload = _validate_payload(payload, allow_legacy=False)
    _validated_artifact_timestamp(generated_at, "generated_at")
    canonical_generated_at = generated_at
    artifact: dict[str, object] = {
        "schema_version": PROBABILITY_ARTIFACT_SCHEMA_VERSION,
        "generated_at": canonical_generated_at,
        "payload": normalized_payload,
        "integrity": {
            "algorithm": PROBABILITY_ARTIFACT_DIGEST_ALGORITHM,
            "scope": PROBABILITY_ARTIFACT_DIGEST_SCOPE,
            "integrity_digest": _artifact_integrity_digest(
                canonical_generated_at,
                normalized_payload,
            ),
            "notice": PROBABILITY_ARTIFACT_INTEGRITY_NOTICE,
        },
    }
    return _verify_normalized_artifact(artifact, allow_legacy=False)


def verify_probability_artifact(artifact: Mapping[str, object]) -> dict[str, object]:
    """Fail closed unless structure, payload integrity, and record contracts agree."""
    _validate_json_tree(artifact, "artifact")
    return _verify_normalized_artifact(dict(artifact), allow_legacy=False)


def replay_probability_artifact(artifact: Mapping[str, object]) -> dict[str, float | None]:
    """Verify and deterministically replay every self-contained persisted result."""
    _validate_json_tree(artifact, "artifact")
    verified = _verify_normalized_artifact(dict(artifact), allow_legacy=True)
    payload = cast(Mapping[str, object], verified["payload"])
    if payload.get("record_contract_version") not in _SELF_CONTAINED_RESULT_CONTRACTS:
        raise ProbabilityArtifactError("上涨概率回放仅支持 self-contained records")
    studies = {_study_key(item): item for item in cast(Sequence[Mapping[str, object]], payload["studies"])}
    features = _feature_evidence_by_key(payload["feature_evidence"])
    return {
        str(details["sample_id"]): _replayed_record_probability(
            record,
            studies[_study_key(record)],
            features[str(details["feature_evidence_key"])],
        )
        for record in cast(Sequence[Mapping[str, object]], payload["records"])
        for details in (cast(Mapping[str, object], record["details"]),)
    }


def replay_probability_artifact_set(
    sources: Sequence[str | Path | Mapping[str, object]],
) -> dict[str, object]:
    """Refit every study from a complete immutable per-run artifact set."""
    artifacts = _verified_artifact_set(sources)
    generated_at, run_ids, by_run = _artifact_set_identity(artifacts)
    study_keys = _validate_artifact_set_layout(by_run, run_ids)
    study_groups = _artifact_set_study_groups(by_run, run_ids, study_keys)
    studies: list[dict[str, object]] = []
    for cohort, cohort_run_ids, target, horizon, metadata in study_groups:
        samples = _artifact_set_samples(by_run, cohort_run_ids, target, horizon)
        config = _probability_config_from_metadata(metadata, target, horizon)
        rebuilt = fit_shadow_probability(samples, config=config, generated_at=generated_at)
        _validate_rebuilt_study(metadata, rebuilt, target, horizon)
        studies.append(
            {
                "cohort_contract": cohort,
                "run_ids": list(cohort_run_ids),
                "target": target,
                "model_target": config.target,
                "horizon": horizon,
                "status": rebuilt["status"],
                "sample_count": len(samples),
                "input_digest": rebuilt["input_digest"],
                "evidence_digest": rebuilt["evidence_digest"],
            }
        )
    return {
        "schema_version": PROBABILITY_ARTIFACT_SET_REPLAY_SCHEMA_VERSION,
        "generated_at": generated_at,
        "run_ids": list(run_ids),
        "study_count": len(studies),
        "studies": studies,
    }


def _verified_artifact_set(
    sources: Sequence[str | Path | Mapping[str, object]],
) -> list[dict[str, object]]:
    if not sources:
        raise ProbabilityArtifactError("上涨概率 artifact set 不能为空")
    artifacts: list[dict[str, object]] = []
    for source in sources:
        if isinstance(source, Mapping):
            _validate_json_tree(source, "artifact")
            artifact = _verify_normalized_artifact(dict(source), allow_legacy=True)
        elif isinstance(source, str | Path):
            artifact = load_probability_artifact(source)
        else:
            raise ProbabilityArtifactError("上涨概率 artifact set source 类型无效")
        payload = _required_mapping(artifact["payload"], "artifact.payload")
        if payload.get("record_contract_version") not in _SELF_CONTAINED_RESULT_CONTRACTS:
            raise ProbabilityArtifactError("上涨概率 artifact set 仅支持 self-contained records")
        artifacts.append(artifact)
    return artifacts


def _artifact_set_identity(
    artifacts: Sequence[Mapping[str, object]],
) -> tuple[str, tuple[int, ...], dict[int, Mapping[str, object]]]:
    generated_values = {_required_text(item["generated_at"], "artifact.generated_at") for item in artifacts}
    if len(generated_values) != 1:
        raise ProbabilityArtifactError("上涨概率 artifact set generated_at 不一致")
    by_run: dict[int, Mapping[str, object]] = {}
    manifests: list[tuple[int, ...]] = []
    for artifact in artifacts:
        payload = _required_mapping(artifact["payload"], "artifact.payload")
        run_id = _single_artifact_run_id(payload)
        if run_id in by_run:
            raise ProbabilityArtifactError(f"上涨概率 artifact set 存在重复 run：{run_id}")
        by_run[run_id] = artifact
        manifests.append(_artifact_run_manifest(payload))
    run_ids = manifests[0]
    if any(value != run_ids for value in manifests[1:]):
        raise ProbabilityArtifactError("上涨概率 artifact set run manifest 冲突")
    missing = sorted(set(run_ids) - by_run.keys())
    unexpected = sorted(by_run.keys() - set(run_ids))
    if missing or unexpected:
        raise ProbabilityArtifactError(f"上涨概率 artifact set run 不完整；missing={missing} extra={unexpected}")
    return next(iter(generated_values)), run_ids, by_run


def _single_artifact_run_id(payload: Mapping[str, object]) -> int:
    collections = (
        cast(Sequence[Mapping[str, object]], payload["feature_evidence"]),
        cast(Sequence[Mapping[str, object]], payload["studies"]),
        cast(Sequence[Mapping[str, object]], payload["records"]),
    )
    run_sets = [{cast(int, item["run_id"]) for item in values} for values in collections]
    if any(len(values) != 1 for values in run_sets) or not all(values == run_sets[0] for values in run_sets):
        raise ProbabilityArtifactError("上涨概率 per-run artifact 必须且只能包含同一个 run")
    return next(iter(run_sets[0]))


def _artifact_run_manifest(payload: Mapping[str, object]) -> tuple[int, ...]:
    manifests = {
        _required_run_id_list(_required_mapping(study["metadata"], "study.metadata").get("artifact_set_run_ids"))
        for study in cast(Sequence[Mapping[str, object]], payload["studies"])
    }
    if len(manifests) != 1:
        raise ProbabilityArtifactError("上涨概率 artifact 内部 run manifest 缺失或冲突")
    return next(iter(manifests))


def _required_run_id_list(value: object) -> tuple[int, ...]:
    run_ids = tuple(_positive_integer(item, "artifact_set_run_ids[]") for item in _required_list(value, "artifact_set_run_ids"))
    if not run_ids or len(run_ids) != len(set(run_ids)):
        raise ProbabilityArtifactError("上涨概率 artifact_set_run_ids 不能为空或重复")
    return run_ids


def _validate_artifact_set_layout(
    by_run: Mapping[int, Mapping[str, object]],
    run_ids: Sequence[int],
) -> tuple[tuple[str, int], ...]:
    expected_keys: set[tuple[str, int]] | None = None
    for run_id in run_ids:
        payload = _required_mapping(by_run[run_id]["payload"], "artifact.payload")
        studies = cast(Sequence[Mapping[str, object]], payload["studies"])
        keys = {(str(item["target"]), cast(int, item["horizon"])) for item in studies}
        if not keys or (expected_keys is not None and keys != expected_keys):
            raise ProbabilityArtifactError("上涨概率 artifact set study 集合不完整或不一致")
        expected_keys = keys
        _validate_run_record_matrix(payload, run_id, keys)
    return tuple(sorted(expected_keys or (), key=lambda item: (item[1], item[0])))


def _validate_run_record_matrix(
    payload: Mapping[str, object],
    run_id: int,
    study_keys: set[tuple[str, int]],
) -> None:
    feature_rows = cast(Sequence[Mapping[str, object]], payload["feature_evidence"])
    records = cast(Sequence[Mapping[str, object]], payload["records"])
    symbols = {str(item["symbol"]) for item in feature_rows}
    actual = {(str(item["symbol"]), str(item["target"]), cast(int, item["horizon"])) for item in records}
    expected = {(symbol, target, horizon) for symbol in symbols for target, horizon in study_keys}
    if not symbols or actual != expected:
        raise ProbabilityArtifactError(f"上涨概率 run {run_id} 缺少 feature 或 result record")
    for study in cast(Sequence[Mapping[str, object]], payload["studies"]):
        _validate_run_study_records(study, records, run_id)


def _validate_run_study_records(
    study: Mapping[str, object],
    records: Sequence[Mapping[str, object]],
    run_id: int,
) -> None:
    key = str(study["target"]), cast(int, study["horizon"])
    selected = [item for item in records if (str(item["target"]), cast(int, item["horizon"])) == key]
    calibrated = sum(item["status"] == "calibrated_shadow" for item in selected)
    metadata = _required_mapping(study["metadata"], "study.metadata")
    if metadata.get("run_record_count") != len(selected) or metadata.get("run_calibrated_record_count") != calibrated:
        raise ProbabilityArtifactError(f"上涨概率 run {run_id} study record counts 不一致")
    expected_status = "calibrated_shadow" if calibrated else "insufficient_data"
    if study["status"] != expected_status:
        raise ProbabilityArtifactError(f"上涨概率 run {run_id} study status 与 records 不一致")


def _artifact_set_study_groups(
    by_run: Mapping[int, Mapping[str, object]],
    run_ids: Sequence[int],
    study_keys: Sequence[tuple[str, int]],
) -> list[tuple[dict[str, object], tuple[int, ...], str, int, Mapping[str, object]]]:
    output: list[tuple[dict[str, object], tuple[int, ...], str, int, Mapping[str, object]]] = []
    for cohort, _score_contract, cohort_run_ids in _artifact_set_cohorts(
        by_run,
        run_ids,
        study_keys,
    ):
        for target, horizon in study_keys:
            key = target, horizon
            studies = [_artifact_study_for_key(by_run[run_id], key) for run_id in cohort_run_ids]
            metadata = _consistent_study_metadata(studies, key)
            output.append((cohort, cohort_run_ids, target, horizon, metadata))
    return output


def _artifact_set_cohorts(
    by_run: Mapping[int, Mapping[str, object]],
    run_ids: Sequence[int],
    study_keys: Sequence[tuple[str, int]],
) -> list[tuple[dict[str, object], dict[str, object], tuple[int, ...]]]:
    grouped: dict[str, tuple[dict[str, object], dict[str, object], list[int]]] = {}
    for run_id in run_ids:
        cohort = _artifact_run_cohort(by_run[run_id], study_keys)
        score_contract = _artifact_run_score_contract(by_run[run_id], study_keys)
        token = _canonical_validated_json({"cohort_contract": cohort, "production_score_contract": score_contract})
        if token not in grouped:
            grouped[token] = cohort, score_contract, []
        grouped[token][2].append(run_id)
    output: list[tuple[dict[str, object], dict[str, object], tuple[int, ...]]] = []
    for cohort, score_contract, values in grouped.values():
        ordered = tuple(sorted(values, key=lambda run_id: _artifact_run_order(by_run[run_id], run_id)))
        _validate_artifact_cohort_identity(
            by_run,
            cohort,
            score_contract,
            ordered,
            study_keys,
        )
        output.append((cohort, score_contract, ordered))
    return output


def _artifact_run_order(artifact: Mapping[str, object], run_id: int) -> tuple[str, int]:
    payload = _required_mapping(artifact["payload"], "artifact.payload")
    dates = {_required_text(item["quote_date"], "feature.quote_date") for item in cast(Sequence[Mapping[str, object]], payload["feature_evidence"])}
    if len(dates) != 1:
        raise ProbabilityArtifactError(f"上涨概率 run {run_id} quote_date 不唯一")
    return next(iter(dates)), run_id


def _validate_artifact_cohort_identity(
    by_run: Mapping[int, Mapping[str, object]],
    cohort: Mapping[str, object],
    score_contract: Mapping[str, object],
    run_ids: Sequence[int],
    study_keys: Sequence[tuple[str, int]],
) -> None:
    if cohort == {"legacy_global": True}:
        return
    sessions = sorted({_artifact_run_order(by_run[run_id], run_id)[0] for run_id in run_ids})
    evidence_digests = {
        f"{horizon}/{target}": _required_mapping(
            _artifact_study_for_key(by_run[run_ids[0]], (target, horizon))["metadata"],
            "study.metadata",
        ).get("evidence_digest")
        for target, horizon in study_keys
    }
    identity = {
        "cohort_contract": dict(cohort),
        "run_ids": sorted(run_ids),
        "session_dates": sessions,
        "horizon_evidence_digests": evidence_digests,
    }
    if score_contract and all(score_contract.values()):
        identity["production_score_contract"] = dict(score_contract)
    expected = _stable_json_digest(identity)
    for run_id in run_ids:
        for key in study_keys:
            study = _artifact_study_for_key(by_run[run_id], key)
            metadata = _required_mapping(study["metadata"], "study.metadata")
            if metadata.get("cohort_digest") != expected:
                raise ProbabilityArtifactError("上涨概率 artifact set cohort_digest 无法重建")


def _artifact_run_cohort(
    artifact: Mapping[str, object],
    study_keys: Sequence[tuple[str, int]],
) -> dict[str, object]:
    cohorts = {_canonical_validated_json(cohort): cohort for key in study_keys for cohort in (_study_cohort_contract(_artifact_study_for_key(artifact, key)),)}
    if len(cohorts) != 1:
        raise ProbabilityArtifactError("上涨概率同一 run 的 study cohort contract 不一致")
    return next(iter(cohorts.values()))


def _artifact_run_score_contract(
    artifact: Mapping[str, object],
    study_keys: Sequence[tuple[str, int]],
) -> dict[str, object]:
    contracts = {
        _canonical_validated_json(contract): contract
        for key in study_keys
        for contract in (_study_production_score_contract(_artifact_study_for_key(artifact, key)),)
    }
    if len(contracts) != 1:
        raise ProbabilityArtifactError("上涨概率同一 run 的生产评分合同不一致")
    return next(iter(contracts.values()))


def _study_production_score_contract(study: Mapping[str, object]) -> dict[str, object]:
    metadata = _required_mapping(study["metadata"], "study.metadata")
    value = metadata.get("production_score_contract")
    if value is None:
        return {
            "production_score_rule_version": None,
            "production_score_spec_hash": None,
        }
    contract = _required_mapping(value, "study.metadata.production_score_contract")
    return dict(contract)


def _study_cohort_contract(study: Mapping[str, object]) -> dict[str, object]:
    metadata = _required_mapping(study["metadata"], "study.metadata")
    cohort = metadata.get("cohort")
    contract = metadata.get("cohort_contract")
    if cohort is not None and contract is not None and cohort != contract:
        raise ProbabilityArtifactError("上涨概率 study cohort/cohort_contract 冲突")
    selected = contract if contract is not None else cohort
    if selected is None:
        return {"legacy_global": True}
    value = _required_mapping(selected, "study.metadata.cohort_contract")
    if not value:
        raise ProbabilityArtifactError("上涨概率 study cohort contract 不能为空")
    return dict(value)


def _consistent_study_metadata(
    studies: Sequence[Mapping[str, object]],
    key: tuple[str, int],
) -> Mapping[str, object]:
    reference = studies[0]
    metadata = _shared_study_metadata(reference)
    _validate_study_digest_fields(reference)
    for study in studies[1:]:
        _validate_study_digest_fields(study)
        if study["versions"] != reference["versions"] or study["digests"] != reference["digests"] or _shared_study_metadata(study) != metadata:
            raise ProbabilityArtifactError(f"上涨概率 cohort 内 study evidence 冲突：{key}")
    return metadata


def _artifact_study_for_key(
    artifact: Mapping[str, object],
    key: tuple[str, int],
) -> Mapping[str, object]:
    payload = _required_mapping(artifact["payload"], "artifact.payload")
    studies = cast(Sequence[Mapping[str, object]], payload["studies"])
    return next(item for item in studies if (str(item["target"]), cast(int, item["horizon"])) == key)


def _shared_study_metadata(study: Mapping[str, object]) -> dict[str, object]:
    metadata = dict(_required_mapping(study["metadata"], "study.metadata"))
    metadata.pop("run_record_count", None)
    metadata.pop("run_calibrated_record_count", None)
    return metadata


def _validate_study_digest_fields(study: Mapping[str, object]) -> None:
    metadata = _required_mapping(study["metadata"], "study.metadata")
    digests = _required_mapping(study["digests"], "study.digests")
    pairs = (
        ("input_digest", "input"),
        ("label_contract_digest", "label_contract"),
        ("model_digest", "model"),
        ("calibrator_digest", "calibrator"),
        ("isotonic_calibrator_digest", "isotonic_calibrator"),
        ("baseline_digest", "baseline"),
        ("evidence_digest", "evidence"),
    )
    if any(metadata.get(metadata_name) != digests.get(digest_name) for metadata_name, digest_name in pairs):
        raise ProbabilityArtifactError("上涨概率 study metadata 与 digest registry 不一致")


def _artifact_set_samples(
    by_run: Mapping[int, Mapping[str, object]],
    run_ids: Sequence[int],
    target: str,
    horizon: int,
) -> list[ProbabilitySample]:
    samples: list[ProbabilitySample] = []
    for run_id in run_ids:
        payload = _required_mapping(by_run[run_id]["payload"], "artifact.payload")
        features = _feature_evidence_by_key(payload["feature_evidence"])
        records = cast(Sequence[Mapping[str, object]], payload["records"])
        for record in records:
            if record["target"] != target or record["horizon"] != horizon:
                continue
            details = _required_mapping(record["details"], "record.details")
            if details["mature_horizon"] is not True:
                continue
            feature_row = features[str(details["feature_evidence_key"])]
            raw_features = _required_mapping(feature_row["features"], "feature_evidence.features")
            samples.append(_probability_sample_from_record(details, raw_features))
    return samples


def _probability_sample_from_record(
    details: Mapping[str, object],
    features: Mapping[str, object],
) -> ProbabilitySample:
    target = details["model_target"]
    return ProbabilitySample(
        sample_id=_required_text(details["sample_id"], "record.details.sample_id"),
        session_date=_required_text(details["quote_date"], "record.details.quote_date"),
        features={name: _required_finite_number(value, f"features.{name}") for name, value in features.items()},
        target=cast(int | None, target),
        executable=cast(bool, details["executable"]),
        net_return=_optional_finite_number(details["net_return"], "record.details.net_return"),
        net_excess_return=_optional_finite_number(details["net_excess_return"], "record.details.net_excess_return"),
    )


def _optional_finite_number(value: object, path: str) -> float | None:
    return None if value is None else _required_finite_number(value, path)


def _probability_config_from_metadata(
    metadata: Mapping[str, object],
    public_target: str,
    horizon: int,
) -> ProbabilityConfig:
    contract = _required_mapping(metadata.get("contract"), "study.metadata.contract")
    label = _required_mapping(contract.get("label"), "contract.label")
    cost = _required_mapping(contract.get("cost"), "contract.cost")
    model = _required_mapping(contract.get("model"), "contract.model")
    baseline = _required_mapping(contract.get("baseline"), "contract.baseline")
    split = _required_mapping(contract.get("split"), "contract.split")
    evaluation = _required_mapping(contract.get("evaluation"), "contract.evaluation")
    raw_bound_label = cost.get("label_contract")
    bound_label = dict(raw_bound_label) if isinstance(raw_bound_label, Mapping) else None
    label_contract = None if bound_label is None or set(bound_label) == {"label_version", "cost_model_version"} else bound_label
    model_target = _required_text(label.get("target"), "contract.label.target")
    expected_target = "net_excess_positive" if public_target == "net_excess_positive" else "net_return_positive"
    if model_target != expected_target or metadata.get("horizon") != horizon:
        raise ProbabilityArtifactError("上涨概率 public target/horizon 与持久化 contract 不一致")
    try:
        return ProbabilityConfig(
            horizon=horizon,
            target=cast(ProbabilityTarget, model_target),
            cost_model_version=_required_text(cost.get("version"), "contract.cost.version"),
            label_contract=label_contract,
            minimum_train_sessions=_positive_integer(split.get("minimum_train_sessions"), "minimum_train_sessions"),
            minimum_calibration_sessions=_positive_integer(split.get("minimum_calibration_sessions"), "minimum_calibration_sessions"),
            minimum_test_sessions=_positive_integer(split.get("minimum_test_sessions"), "minimum_test_sessions"),
            minimum_label_coverage=_required_finite_number(evaluation.get("minimum_label_coverage"), "minimum_label_coverage"),
            minimum_bin_sessions=_positive_integer(evaluation.get("minimum_bin_sessions"), "minimum_bin_sessions"),
            minimum_selection_folds=_positive_integer(
                evaluation.get("minimum_selection_folds", 2),
                "minimum_selection_folds",
            ),
            gap_sessions=_positive_integer(split.get("gap_sessions"), "gap_sessions"),
            calibration_bin_count=_positive_integer(evaluation.get("calibration_bin_count"), "calibration_bin_count"),
            minimum_isotonic_calibration_sessions=_positive_integer(
                evaluation.get("minimum_isotonic_calibration_sessions"), "minimum_isotonic_calibration_sessions"
            ),
            empirical_bayes_bin_count=_positive_integer(baseline.get("bin_count"), "empirical_bayes_bin_count"),
            empirical_bayes_prior_strength=_required_finite_number(baseline.get("prior_strength"), "empirical_bayes_prior_strength"),
            l2_strength=_required_finite_number(model.get("l2_strength"), "l2_strength"),
            bootstrap_samples=_positive_integer(evaluation.get("bootstrap_samples"), "bootstrap_samples"),
            maximum_iterations=_positive_integer(model.get("maximum_iterations"), "maximum_iterations"),
            convergence_tolerance=_required_finite_number(model.get("convergence_tolerance"), "convergence_tolerance"),
        )
    except ValueError as exc:
        raise ProbabilityArtifactError("上涨概率 artifact set config 无效") from exc


def _validate_rebuilt_study(
    metadata: Mapping[str, object],
    rebuilt: Mapping[str, object],
    target: str,
    horizon: int,
) -> None:
    for name in _REPLAY_STUDY_FIELDS:
        if name not in metadata or metadata[name] != rebuilt.get(name):
            raise ProbabilityArtifactError(f"上涨概率 artifact set {target}/{horizon} {name} 无法从完整输入确定性重放")


def _verify_normalized_artifact(
    normalized: dict[str, object],
    *,
    allow_legacy: bool,
) -> dict[str, object]:
    _require_exact_keys(normalized, _TOP_LEVEL_KEYS, "artifact")
    if normalized["schema_version"] != PROBABILITY_ARTIFACT_SCHEMA_VERSION:
        raise ProbabilityArtifactError("上涨概率 artifact schema_version 不受支持")
    generated_at = _validated_artifact_timestamp(normalized["generated_at"], "generated_at")
    payload = _required_mapping(normalized["payload"], "payload")
    integrity = _validate_integrity(normalized["integrity"], allow_legacy=allow_legacy)
    actual_digest = (
        _artifact_integrity_digest(generated_at, payload)
        if integrity["scope"] == PROBABILITY_ARTIFACT_DIGEST_SCOPE
        else _validated_payload_integrity_digest(payload)
    )
    if integrity["integrity_digest"] != actual_digest:
        raise ProbabilityArtifactError("上涨概率 artifact integrity digest 不一致")
    normalized["payload"] = _validate_normalized_payload(payload, allow_legacy=allow_legacy)
    _validate_result_generated_at(
        cast(Mapping[str, object], normalized["payload"]),
        cast(str, normalized["generated_at"]),
    )
    if integrity["scope"] == PROBABILITY_ARTIFACT_DIGEST_SCOPE:
        _validate_result_maturity_time(cast(Mapping[str, object], normalized["payload"]), generated_at)
    normalized["integrity"] = integrity
    return normalized


def write_probability_artifact(
    path: str | Path,
    artifact: Mapping[str, object],
    *,
    database_path: str | Path,
) -> Path:
    """Atomically write a verified artifact without ever replacing the database."""
    target = Path(path).expanduser().absolute()
    database = Path(database_path).expanduser().resolve()
    _reject_database_target(target, database)
    try:
        require_project_managed_artifact_database(target, database, PROBABILITY_MANAGED_DIRECTORY)
    except MarketScanArtifactLeaseError as exc:
        raise ProbabilityArtifactError(str(exc)) from exc
    verified = verify_probability_artifact(artifact)
    encoded = _canonical_validated_json(verified).encode("utf-8")
    payload = cast(Mapping[str, object], verified["payload"])
    try:
        with verified_market_scan_artifact_publication(
            database,
            target,
            _publication_run_manifest(payload),
            managed_directory=PROBABILITY_MANAGED_DIRECTORY,
        ):
            publish_market_scan_artifact(
                target,
                encoded,
                max_bytes=PROBABILITY_ARTIFACT_MAX_BYTES,
                before_publish=lambda: _reject_database_target(target, database),
            )
    except ArtifactContentConflictError as exc:
        raise ProbabilityArtifactError(f"上涨概率 artifact 已存在且内容不同，拒绝覆盖不可变证据：{target}") from exc
    except ArtifactIOError as exc:
        raise ProbabilityArtifactError(f"上涨概率 artifact 写入失败：{target}") from exc
    except ProbabilityArtifactError:
        raise
    except MarketScanArtifactLeaseError as exc:
        raise ProbabilityArtifactError("上涨概率 artifact 来源批次已失效") from exc
    except OSError as exc:
        raise ProbabilityArtifactError(f"上涨概率 artifact 写入失败：{target}") from exc
    return target


def probability_artifact_run_manifest(
    artifact: Mapping[str, object],
) -> tuple[int, ...]:
    """Return every run semantically referenced by one verified artifact."""

    verified = verify_probability_artifact(artifact)
    return _publication_run_manifest(cast(Mapping[str, object], verified["payload"]))


def _publication_run_manifest(payload: Mapping[str, object]) -> tuple[int, ...]:
    studies = cast(Sequence[Mapping[str, object]], payload["studies"])
    encoded = [_required_mapping(study["metadata"], "study.metadata").get("artifact_set_run_ids") for study in studies]
    if all(value is None for value in encoded):
        return (_single_artifact_run_id(payload),)
    return _artifact_run_manifest(payload)


def load_probability_artifact(path: str | Path) -> dict[str, object]:
    """Load and strictly verify an artifact; malformed input never degrades open."""
    source = Path(path).expanduser().absolute()
    try:
        decoded = decode_json_bytes(read_regular_file(source, max_bytes=PROBABILITY_ARTIFACT_MAX_BYTES))
    except ArtifactDuplicateKeyError as exc:
        raise ProbabilityArtifactError(f"上涨概率 artifact JSON 包含重复 key：{exc.key}") from exc
    except ArtifactNonFiniteConstantError as exc:
        raise ProbabilityArtifactError(f"上涨概率 artifact JSON 包含非有限常量：{exc.constant}") from exc
    except ArtifactIOError as exc:
        raise ProbabilityArtifactError(f"上涨概率 artifact 读取失败：{source}") from exc
    if not isinstance(decoded, Mapping):
        raise ProbabilityArtifactError("上涨概率 artifact 顶层必须是 JSON object")
    _validate_json_tree(decoded, "artifact")
    return _verify_normalized_artifact(cast(dict[str, object], decoded), allow_legacy=True)


def _validate_payload(payload: Mapping[str, object], *, allow_legacy: bool) -> dict[str, object]:
    normalized = cast(dict[str, object], _json_value(payload, "payload"))
    return _validate_normalized_payload(normalized, allow_legacy=allow_legacy)


def _validate_normalized_payload(
    normalized: dict[str, object],
    *,
    allow_legacy: bool,
) -> dict[str, object]:
    contract = normalized.get("record_contract_version")
    if contract is None and allow_legacy:
        _require_exact_keys(normalized, _LEGACY_PAYLOAD_KEYS, "payload")
    else:
        _require_exact_keys(normalized, _PAYLOAD_KEYS, "payload")
        if contract not in _SELF_CONTAINED_RESULT_CONTRACTS:
            raise ProbabilityArtifactError("上涨概率 result record contract 不受支持")
        if contract in _READ_ONLY_RESULT_CONTRACTS and not allow_legacy:
            raise ProbabilityArtifactError("上涨概率 result record contract 仅允许只读兼容回放")
    studies, study_statuses = _validated_studies(normalized["studies"])
    records = _validated_records(normalized["records"], contract=cast(str | None, contract))
    _validate_record_studies(records, study_statuses)
    if contract in _SELF_CONTAINED_RESULT_CONTRACTS:
        feature_evidence = _validated_feature_evidence(normalized["feature_evidence"])
        _validate_self_contained_results(
            records,
            studies,
            feature_evidence,
            record_contract_version=cast(str, contract),
        )
        normalized["feature_evidence"] = feature_evidence
    normalized["studies"] = studies
    normalized["records"] = records
    return normalized


def _validated_studies(value: object) -> tuple[list[dict[str, object]], dict[tuple[int, str, int], str]]:
    rows = _required_list(value, "payload.studies")
    studies: list[dict[str, object]] = []
    statuses: dict[tuple[int, str, int], str] = {}
    for index, raw in enumerate(rows):
        study = _validate_study(raw, f"payload.studies[{index}]")
        key = _study_key(study)
        if key in statuses:
            raise ProbabilityArtifactError(f"上涨概率 artifact 存在重复 study：{key}")
        statuses[key] = cast(str, study["status"])
        studies.append(study)
    return studies, statuses


def _validated_records(
    value: object,
    *,
    contract: str | None,
) -> list[dict[str, object]]:
    rows = _required_list(value, "payload.records")
    records: list[dict[str, object]] = []
    identities: set[tuple[int, str, int, str]] = set()
    for index, raw in enumerate(rows):
        record = _validate_record(raw, f"payload.records[{index}]", contract=contract)
        key = (*_study_key(record), cast(str, record["symbol"]))
        if key in identities:
            raise ProbabilityArtifactError(f"上涨概率 artifact 存在重复 record：{key}")
        identities.add(key)
        records.append(record)
    return records


def _validate_study(value: object, path: str) -> dict[str, object]:
    study = _required_mapping(value, path)
    _require_exact_keys(study, _STUDY_KEYS, path)
    _validate_identity(study, path)
    status = _required_status(study["status"], f"{path}.status")
    study["status"] = status
    study["versions"] = _validate_versions(study["versions"], f"{path}.versions")
    study["digests"] = _validate_digests(study["digests"], status, f"{path}.digests")
    study["limitations"] = _validate_limitations(study["limitations"], f"{path}.limitations")
    study["metadata"] = _required_mapping(study["metadata"], f"{path}.metadata")
    return study


def _validate_record(
    value: object,
    path: str,
    *,
    contract: str | None,
) -> dict[str, object]:
    record = _required_mapping(value, path)
    current = contract == PROBABILITY_RESULT_CONTRACT_VERSION
    _require_exact_keys(record, _CURRENT_RECORD_KEYS if current else _LEGACY_RECORD_KEYS, path)
    _validate_identity(record, path)
    record["symbol"] = _required_text(record["symbol"], f"{path}.symbol")
    status = _required_status(record["status"], f"{path}.status")
    probability = record["probability"]
    bias = record["calibration_bias_interval"] if current else None
    adjusted = record["calibration_adjusted_probability_interval"] if current else record["confidence_interval"]
    _validate_probability_state(status, probability, bias, adjusted, path, current=current)
    record["status"] = status
    record["details"] = _required_mapping(record["details"], f"{path}.details")
    return record


def _validate_identity(value: dict[str, object], path: str) -> None:
    value["run_id"] = _positive_integer(value["run_id"], f"{path}.run_id")
    target = _required_text(value["target"], f"{path}.target")
    if target not in PROBABILITY_ARTIFACT_TARGETS:
        raise ProbabilityArtifactError(f"{path}.target 不受支持")
    horizon = _positive_integer(value["horizon"], f"{path}.horizon")
    if horizon not in PROBABILITY_ARTIFACT_HORIZONS:
        raise ProbabilityArtifactError(f"{path}.horizon 仅支持 1、5、20")
    value["target"] = target
    value["horizon"] = horizon


def _validate_versions(value: object, path: str) -> dict[str, object]:
    versions = _required_mapping(value, path)
    missing = sorted(_REQUIRED_VERSION_KEYS - versions.keys())
    if missing:
        raise ProbabilityArtifactError(f"{path} 缺少版本：{', '.join(missing)}")
    for name, version in versions.items():
        _required_text(name, f"{path} key")
        versions[name] = _required_text(version, f"{path}.{name}")
    return versions


def _validate_digests(value: object, status: str, path: str) -> dict[str, object]:
    digests = _required_mapping(value, path)
    missing = sorted(_REQUIRED_DIGEST_KEYS - digests.keys())
    if missing:
        raise ProbabilityArtifactError(f"{path} 缺少 digest：{', '.join(missing)}")
    for name, digest in digests.items():
        _required_text(name, f"{path} key")
        if digest is not None:
            digests[name] = _required_sha256(digest, f"{path}.{name}")
    _required_sha256(digests["input"], f"{path}.input")
    if status == "calibrated_shadow" and any(digests[name] is None for name in ("model", "calibrator")):
        raise ProbabilityArtifactError(f"{path} calibrated_shadow 缺少模型或校准器 digest")
    return digests


def _validate_limitations(value: object, path: str) -> list[object]:
    limitations = _required_list(value, path)
    normalized = [_required_text(item, f"{path}[]") for item in limitations]
    if not normalized:
        raise ProbabilityArtifactError(f"{path} 至少需要一项局限说明")
    if len(normalized) != len(set(normalized)):
        raise ProbabilityArtifactError(f"{path} 不能包含重复项")
    return cast(list[object], normalized)


def _validate_probability_state(
    status: str,
    probability: object,
    bias: object,
    adjusted: object,
    path: str,
    *,
    current: bool,
) -> None:
    if status == "insufficient_data":
        if probability is not None or bias is not None or adjusted is not None:
            raise ProbabilityArtifactError(f"{path} 数据不足时 probability 与校准区间必须为 null")
        return
    _required_probability(probability, f"{path}.probability")
    if current:
        bias_bounds = _required_list(bias, f"{path}.calibration_bias_interval")
        if len(bias_bounds) != 2:
            raise ProbabilityArtifactError(f"{path}.calibration_bias_interval 必须为 [lower, upper]")
        lower_bias = _required_finite_number(bias_bounds[0], f"{path}.calibration_bias_interval[0]")
        upper_bias = _required_finite_number(bias_bounds[1], f"{path}.calibration_bias_interval[1]")
        if not -1 <= lower_bias <= upper_bias <= 1:
            raise ProbabilityArtifactError(f"{path}.calibration_bias_interval 必须为有序 [-1,1] 区间")
    bounds = _required_list(adjusted, f"{path}.calibration_adjusted_probability_interval")
    if len(bounds) != 2:
        raise ProbabilityArtifactError(f"{path}.calibration_adjusted_probability_interval 必须为 [lower, upper]")
    lower = _required_probability(bounds[0], f"{path}.calibration_adjusted_probability_interval[0]")
    upper = _required_probability(bounds[1], f"{path}.calibration_adjusted_probability_interval[1]")
    if lower > upper:
        raise ProbabilityArtifactError(f"{path}.calibration_adjusted_probability_interval 上下界颠倒")
    # This is a calibration-adjusted probability interval p + signed bias CI.
    # A statistically significant positive/negative bias interval legitimately
    # does not cover the unadjusted point probability; replay below verifies the
    # exact relationship to calibration_offset_ci_95.


def _validate_record_studies(
    records: Sequence[Mapping[str, object]],
    statuses: Mapping[tuple[int, str, int], str],
) -> None:
    for record in records:
        key = _study_key(record)
        if key not in statuses:
            raise ProbabilityArtifactError(f"上涨概率 record 没有对应 study：{key}")
        if statuses[key] == "insufficient_data" and record["status"] != "insufficient_data":
            raise ProbabilityArtifactError(f"上涨概率 record 与 study 状态不一致：{key}")


def _validate_self_contained_results(
    records: Sequence[Mapping[str, object]],
    studies: Sequence[Mapping[str, object]],
    feature_evidence: Sequence[Mapping[str, object]],
    *,
    record_contract_version: str,
) -> None:
    study_by_key = {_study_key(study): study for study in studies}
    features_by_key = _feature_evidence_by_key(feature_evidence)
    for study in studies:
        _validate_study_replay_evidence(study)
    for record in records:
        study = study_by_key[_study_key(record)]
        details = cast(Mapping[str, object], record["details"])
        feature_row = features_by_key.get(str(details.get("feature_evidence_key") or ""))
        if feature_row is None:
            raise ProbabilityArtifactError("上涨概率 record 引用的 feature evidence 不存在")
        _validate_result_details(
            record,
            study,
            feature_row,
            record_contract_version=record_contract_version,
        )
        _replayed_record_probability(record, study, feature_row)


def _validated_feature_evidence(value: object) -> list[dict[str, object]]:
    rows = _required_list(value, "payload.feature_evidence")
    output: list[dict[str, object]] = []
    identities: set[str] = set()
    required = frozenset(
        {
            "run_id",
            "symbol",
            "quote_date",
            "features",
            "feature_names",
            "feature_vector_digest",
            "dimensions",
            "source_evidence_digest",
        }
    )
    for raw in rows:
        item = _required_mapping(raw, "payload.feature_evidence[]")
        _require_exact_keys(item, required, "payload.feature_evidence[]")
        key = f'{_positive_integer(item["run_id"], "feature.run_id")}:{_required_text(item["symbol"], "feature.symbol")}'
        if key in identities:
            raise ProbabilityArtifactError(f"上涨概率 feature evidence 重复：{key}")
        identities.add(key)
        _validate_feature_row(item)
        output.append(item)
    return output


def _validate_feature_row(item: Mapping[str, object]) -> None:
    _required_text(item["quote_date"], "feature.quote_date")
    features = _required_mapping(item["features"], "feature.features")
    names = _required_list(item["feature_names"], "feature.feature_names")
    if not features or names != sorted(features):
        raise ProbabilityArtifactError("上涨概率 feature evidence names 与 values 不一致")
    for name, value in features.items():
        _required_text(name, "feature name")
        _required_finite_number(value, f"feature.{name}")
    if item["feature_vector_digest"] != _stable_json_digest(features):
        raise ProbabilityArtifactError("上涨概率 feature evidence digest 不一致")
    _required_mapping(item["dimensions"], "feature.dimensions")
    if item["source_evidence_digest"] is not None:
        _required_sha256(item["source_evidence_digest"], "feature.source_evidence_digest")


def _feature_evidence_by_key(value: object) -> dict[str, Mapping[str, object]]:
    rows = cast(Sequence[Mapping[str, object]], value)
    return {f'{item["run_id"]}:{item["symbol"]}': item for item in rows}


def _validate_study_replay_evidence(study: Mapping[str, object]) -> None:
    metadata = _required_mapping(study["metadata"], "study.metadata")
    if metadata.get("filter_qualified") is True:
        raise ProbabilityArtifactError("上涨概率 core artifact 不能在缺少独立绑定 authorization 时声明 filter_qualified")
    digests = _required_mapping(study["digests"], "study.digests")
    versions = _required_mapping(study["versions"], "study.versions")
    if metadata.get("input_digest") != digests["input"]:
        raise ProbabilityArtifactError("上涨概率 study input digest 与持久化 evidence 不一致")
    if "evidence" in digests and metadata.get("evidence_digest") != digests["evidence"]:
        raise ProbabilityArtifactError("上涨概率 study evidence digest 不一致")
    _validate_label_contract_identity(metadata, digests)
    pairs = (
        ("model", "model", "model"),
        ("calibrator", "calibrator", "calibrator"),
        ("isotonic_calibrator", "isotonic_calibrator", None),
        ("empirical_bayes_baseline", "baseline", None),
    )
    for payload_name, digest_name, version_name in pairs:
        _validate_study_component(metadata, digests, versions, payload_name, digest_name, version_name)
    _validate_study_folds(metadata)


def _validate_label_contract_identity(
    metadata: Mapping[str, object],
    digests: Mapping[str, object],
) -> None:
    digest = metadata.get("label_contract_digest")
    registry_digest = digests.get("label_contract")
    if digest is None and registry_digest is None:
        return  # Read-only compatibility for artifacts created before this binding.
    expected = _required_sha256(digest, "study.metadata.label_contract_digest")
    if registry_digest != expected:
        raise ProbabilityArtifactError("上涨概率完整 label contract digest registry 不一致")
    contract = _required_mapping(metadata.get("contract"), "study.metadata.contract")
    cost = _required_mapping(contract.get("cost"), "study.metadata.contract.cost")
    label_contract = _required_mapping(cost.get("label_contract"), "contract.cost.label_contract")
    if _stable_json_digest(label_contract) != expected or cost.get("label_contract_digest") != expected:
        raise ProbabilityArtifactError("上涨概率完整 label contract 未绑定到模型契约")


def _validate_study_component(
    metadata: Mapping[str, object],
    digests: Mapping[str, object],
    versions: Mapping[str, object],
    payload_name: str,
    digest_name: str,
    version_name: str | None,
) -> None:
    payload = metadata.get(payload_name)
    digest = digests.get(digest_name)
    if payload is None and digest is None:
        return
    if payload is None or digest is None:
        raise ProbabilityArtifactError(f"上涨概率 study {payload_name}/{digest_name} 持久化不完整")
    component = _required_mapping(payload, f"study.metadata.{payload_name}")
    if _stable_json_digest(component) != digest:
        raise ProbabilityArtifactError(f"上涨概率 study {digest_name} digest 无法从持久化组件重放")
    if version_name is not None and component.get("version") != versions.get(version_name):
        raise ProbabilityArtifactError(f"上涨概率 study {payload_name} version 不一致")


def _validate_study_folds(metadata: Mapping[str, object]) -> None:
    folds = _required_list(metadata.get("folds"), "study.metadata.folds")
    for index, raw in enumerate(folds, start=1):
        fold = _required_mapping(raw, "study.metadata.folds[]")
        if _positive_integer(fold.get("fold_id"), "fold.fold_id") != index:
            raise ProbabilityArtifactError("上涨概率 fold_id 必须从 1 连续递增")
        split = _required_mapping(fold.get("split"), "fold.split")
        test_dates = _required_list(split.get("test_dates"), "fold.split.test_dates")
        if not test_dates or fold.get("test_session_count") != len(test_dates):
            raise ProbabilityArtifactError("上涨概率 fold test window 不完整")
        if _positive_integer(fold.get("prediction_count"), "fold.prediction_count") < len(test_dates):
            raise ProbabilityArtifactError("上涨概率 fold prediction_count 少于 test sessions")
        _validate_fold_components(fold)
    if folds:
        _validate_final_fold_matches_study(metadata, _required_mapping(folds[-1], "study.metadata.folds[-1]"))
    elif metadata.get("model") is not None:
        raise ProbabilityArtifactError("上涨概率已拟合 study 缺少 folds")


def _validate_fold_components(fold: Mapping[str, object]) -> None:
    pairs = (
        ("model", "model_digest"),
        ("calibrator", "calibrator_digest"),
        ("isotonic_calibrator", "isotonic_calibrator_digest"),
        ("empirical_bayes_baseline", "baseline_digest"),
    )
    for payload_name, digest_name in pairs:
        payload, digest = fold.get(payload_name), fold.get(digest_name)
        if payload is None and digest is None:
            continue
        component = _required_mapping(payload, f"fold.{payload_name}")
        if _stable_json_digest(component) != _required_sha256(digest, f"fold.{digest_name}"):
            raise ProbabilityArtifactError(f"上涨概率 fold {digest_name} 无法重放")


def _validate_final_fold_matches_study(
    metadata: Mapping[str, object],
    fold: Mapping[str, object],
) -> None:
    pairs = (
        ("split", "split"),
        ("training_cutoff", "training_cutoff"),
        ("base_rate", "base_rate"),
        ("model", "model"),
        ("calibrator", "calibrator"),
        ("isotonic_calibrator", "isotonic_calibrator"),
        ("empirical_bayes_baseline", "empirical_bayes_baseline"),
        ("model_digest", "model_digest"),
        ("calibrator_digest", "calibrator_digest"),
        ("isotonic_calibrator_digest", "isotonic_calibrator_digest"),
        ("baseline_digest", "baseline_digest"),
    )
    if any(metadata.get(study_name) != fold.get(fold_name) for study_name, fold_name in pairs):
        raise ProbabilityArtifactError("上涨概率最后完整 fold 与 study 顶层模型不一致")


def _validate_result_details(
    record: Mapping[str, object],
    study: Mapping[str, object],
    feature_row: Mapping[str, object],
    *,
    record_contract_version: str,
) -> None:
    details = _required_mapping(record["details"], "record.details")
    current = record_contract_version == PROBABILITY_RESULT_CONTRACT_VERSION
    required = _CURRENT_REQUIRED_RESULT_DETAIL_KEYS if current else _LEGACY_REQUIRED_RESULT_DETAIL_KEYS
    missing = sorted(required - details.keys())
    if missing:
        raise ProbabilityArtifactError(f"上涨概率 self-contained record 缺少字段：{missing}")
    forbidden_interval_name = "confidence_interval_definition" if current else "calibration_adjusted_probability_interval_definition"
    if forbidden_interval_name in details:
        raise ProbabilityArtifactError("上涨概率 record 区间语义字段与 contract 版本冲突")
    if details["record_contract_version"] != record_contract_version:
        raise ProbabilityArtifactError("上涨概率 record_contract_version 不一致")
    expected_sample_id = f'{record["run_id"]}:{record["symbol"]}:{record["horizon"]}:{record["target"]}'
    if details["sample_id"] != expected_sample_id:
        raise ProbabilityArtifactError("上涨概率 record sample_id 与结果身份不一致")
    if details["versions"] != study["versions"]:
        raise ProbabilityArtifactError("上涨概率 record 未持久化与 study 一致的版本")
    _validate_result_fold(record, study)
    if details["digests"] != _expected_result_digests(study, details["fold_id"]):
        raise ProbabilityArtifactError("上涨概率 record 未持久化实际预测 fold 的 digest")
    _validate_result_features(record, details, feature_row)
    _validate_result_execution(details)
    _validate_result_study_fields(details, study)


def _validate_result_features(
    record: Mapping[str, object],
    details: Mapping[str, object],
    feature_row: Mapping[str, object],
) -> None:
    expected_key = f'{record["run_id"]}:{record["symbol"]}'
    if details["feature_evidence_key"] != expected_key:
        raise ProbabilityArtifactError("上涨概率 record feature_evidence_key 与身份不一致")
    for name in ("quote_date", "feature_vector_digest", "dimensions", "source_evidence_digest"):
        if details[name] != feature_row[name]:
            raise ProbabilityArtifactError(f"上涨概率 record {name} 与 feature evidence 不一致")


def _validate_result_execution(details: Mapping[str, object]) -> None:
    mature, executable = details["mature_horizon"], details["executable"]
    if not isinstance(mature, bool) or not isinstance(executable, bool):
        raise ProbabilityArtifactError("上涨概率 record mature_horizon/executable 必须是布尔值")
    model_target = details["model_target"]
    if model_target is not None and (isinstance(model_target, bool) or model_target not in (0, 1)):
        raise ProbabilityArtifactError("上涨概率 record model_target 必须是 0、1 或 null")
    if executable and (not mature or model_target is None or details["label_status"] != "modelled" or details["source_evidence_digest"] is None):
        raise ProbabilityArtifactError("上涨概率 executable record 缺少成熟标签或时点证据")
    if not executable and model_target is not None:
        raise ProbabilityArtifactError("上涨概率不可执行 record 的 model_target 必须为 null")
    for name in ("net_return", "market_benchmark_net_return", "net_excess_return"):
        if details[name] is not None:
            _required_finite_number(details[name], f"record.details.{name}")


def _validate_result_fold(record: Mapping[str, object], study: Mapping[str, object]) -> None:
    details = _required_mapping(record["details"], "record.details")
    fold_id = details["fold_id"]
    if fold_id is not None:
        _positive_integer(fold_id, "record.details.fold_id")
    if record["status"] != "calibrated_shadow":
        if fold_id is not None:
            raise ProbabilityArtifactError("上涨概率 null/insufficient record 的 fold_id 必须为 null")
        return
    metadata = _required_mapping(study["metadata"], "study.metadata")
    folds = cast(Sequence[Mapping[str, object]], metadata["folds"])
    quote_date = str(details["quote_date"])
    matching = [fold for fold in folds if quote_date in _fold_test_dates(fold)]
    if matching:
        if len(matching) != 1 or fold_id != matching[0]["fold_id"]:
            raise ProbabilityArtifactError("上涨概率 OOS record fold_id 与 test window 不一致")
        return
    last_test_date = _fold_test_dates(folds[-1])[-1] if folds else None
    if fold_id is not None or last_test_date is None or quote_date <= last_test_date:
        raise ProbabilityArtifactError("上涨概率非 OOS record 不能伪装成 calibrated forecast")


def _fold_test_dates(fold: Mapping[str, object]) -> list[str]:
    split = _required_mapping(fold["split"], "fold.split")
    return [_required_text(value, "fold.split.test_dates[]") for value in _required_list(split["test_dates"], "fold.split.test_dates")]


def _expected_result_digests(study: Mapping[str, object], fold_id: object) -> dict[str, object]:
    digests = dict(_required_mapping(study["digests"], "study.digests"))
    if fold_id is None:
        return digests
    metadata = _required_mapping(study["metadata"], "study.metadata")
    folds = cast(Sequence[Mapping[str, object]], metadata["folds"])
    fold = next((item for item in folds if item["fold_id"] == fold_id), None)
    if fold is None:
        raise ProbabilityArtifactError("上涨概率 record fold_id 没有对应 fold evidence")
    for digest_name in ("model", "calibrator", "isotonic_calibrator", "baseline"):
        digests[digest_name] = fold.get(f"{digest_name}_digest")
    return digests


def _validate_result_study_fields(details: Mapping[str, object], study: Mapping[str, object]) -> None:
    metadata = _required_mapping(study["metadata"], "study.metadata")
    for name in ("base_rate", "training_cutoff", "target_definition", "counts", "contract", "generated_at"):
        if details[name] != metadata.get(name):
            raise ProbabilityArtifactError(f"上涨概率 record {name} 未按结果持久化")
    if details["calibration_summary"] != _study_calibration_summary(metadata):
        raise ProbabilityArtifactError("上涨概率 record calibration_summary 与 study 不一致")
    shared = set(_required_list(study["limitations"], "study.limitations"))
    local = set(_required_list(details["limitations"], "record.details.limitations"))
    if not shared <= local:
        raise ProbabilityArtifactError("上涨概率 record 未完整持久化 study limitations")
    if details["automatic_promotion"] is not False:
        raise ProbabilityArtifactError("上涨概率 record automatic_promotion 必须为 false")


def _study_calibration_summary(metadata: Mapping[str, object]) -> dict[str, object]:
    metrics = metadata.get("calibration_metrics")
    calibrated = metrics.get("calibrated") if isinstance(metrics, Mapping) else None
    values: Mapping[str, object] = calibrated if isinstance(calibrated, Mapping) else {}
    names = (
        "observation_count",
        "independent_session_count",
        "brier_score",
        "brier_skill_score",
        "reference_base_rate_mean",
        "reference_brier_score",
        "reference_definition",
        "log_loss",
        "ece",
        "auc",
        "bin_monotonic",
        "highest_bin_above_base_rate",
        "brier_score_ci_95",
        "actual_positive_rate_ci_95",
        "calibration_offset_ci_95",
        "bootstrap_samples",
    )
    bins = values.get("calibration_bins")
    rows = [item for item in bins if isinstance(item, Mapping)] if isinstance(bins, list) else []
    sessions = [int(item.get("independent_session_count") or 0) for item in rows]
    return {
        **{name: values.get(name) for name in names},
        "calibration_bin_count": len(rows) if calibrated is not None else None,
        "minimum_bin_independent_session_count": min(sessions) if sessions else None,
    }


def _replayed_record_probability(
    record: Mapping[str, object],
    study: Mapping[str, object],
    feature_row: Mapping[str, object],
) -> float | None:
    if record["status"] != "calibrated_shadow":
        return None
    details = _required_mapping(record["details"], "record.details")
    metadata = _required_mapping(study["metadata"], "study.metadata")
    predictor = _record_predictor(metadata, details["fold_id"])
    model = _required_mapping(predictor.get("model"), "record predictor.model")
    calibrator = _required_mapping(predictor.get("calibrator"), "record predictor.calibrator")
    features = _required_mapping(feature_row["features"], "feature_evidence.features")
    raw = _replay_raw_probability(model, features)
    probability = _replay_calibrated_probability(calibrator, raw)
    _require_close(details["raw_probability"], raw, "record raw_probability")
    _require_close(record["probability"], probability, "record probability")
    baseline = _required_mapping(predictor.get("empirical_bayes_baseline"), "record predictor.empirical_bayes_baseline")
    _require_close(
        details["empirical_bayes_probability"],
        _replay_baseline_probability(baseline, raw),
        "record empirical_bayes_probability",
    )
    _validate_replayed_interval(record, details, probability)
    return probability


def _record_predictor(metadata: Mapping[str, object], fold_id: object) -> Mapping[str, object]:
    if fold_id is None:
        return metadata
    folds = cast(Sequence[Mapping[str, object]], metadata["folds"])
    fold = next((item for item in folds if item["fold_id"] == fold_id), None)
    if fold is None:
        raise ProbabilityArtifactError("上涨概率 calibrated record 缺少 fold predictor")
    return fold


def _replay_raw_probability(model: Mapping[str, object], features: Mapping[str, object]) -> float:
    names = _required_list(model.get("feature_names"), "model.feature_names")
    means = _finite_number_list(model.get("means"), "model.means")
    scales = _finite_number_list(model.get("scales"), "model.scales")
    coefficients = _finite_number_list(model.get("coefficients"), "model.coefficients")
    if names != sorted(features) or not (len(names) == len(means) == len(scales) == len(coefficients)):
        raise ProbabilityArtifactError("上涨概率 model 与 record features 维度不一致")
    linear = _required_finite_number(model.get("intercept"), "model.intercept")
    for name, mean, scale, coefficient in zip(names, means, scales, coefficients, strict=True):
        if scale <= 0:
            raise ProbabilityArtifactError("上涨概率 model scale 必须为正数")
        value = _required_finite_number(features[str(name)], f"features.{name}")
        linear += coefficient * (value - mean) / scale
    return _sigmoid(linear)


def _replay_calibrated_probability(calibrator: Mapping[str, object], raw: float) -> float:
    intercept = _required_finite_number(calibrator.get("intercept"), "calibrator.intercept")
    slope = _required_finite_number(calibrator.get("slope"), "calibrator.slope")
    clipped = min(1.0 - 1e-12, max(1e-12, raw))
    return _sigmoid(intercept + slope * math.log(clipped / (1.0 - clipped)))


def _replay_baseline_probability(baseline: Mapping[str, object], raw: float) -> float:
    boundaries = _finite_number_list(baseline.get("boundaries"), "baseline.boundaries")
    probabilities = _finite_number_list(baseline.get("probabilities"), "baseline.probabilities")
    if len(probabilities) != len(boundaries) + 1 or boundaries != sorted(boundaries):
        raise ProbabilityArtifactError("上涨概率 empirical_bayes_baseline 维度无效")
    index = sum(raw >= boundary for boundary in boundaries)
    return _required_probability(probabilities[index], "baseline probability")


def _validate_replayed_interval(
    record: Mapping[str, object],
    details: Mapping[str, object],
    probability: float,
) -> None:
    offsets = _finite_number_list(details["calibration_offset_ci_95"], "calibration_offset_ci_95")
    current = "calibration_adjusted_probability_interval" in record
    interval = _required_list(
        record["calibration_adjusted_probability_interval" if current else "confidence_interval"],
        "record.calibration_adjusted_probability_interval",
    )
    if len(offsets) != 2 or len(interval) != 2:
        raise ProbabilityArtifactError("上涨概率回放校准区间必须具有两个边界")
    if current and record.get("calibration_bias_interval") != offsets:
        raise ProbabilityArtifactError("上涨概率 signed calibration bias 无法从 record 重放")
    expected = [max(0.0, min(1.0, probability + value)) for value in offsets]
    for persisted, replayed in zip(interval, expected, strict=True):
        _require_close(persisted, replayed, "record calibration adjusted interval")


def _validate_result_generated_at(payload: Mapping[str, object], generated_at: str) -> None:
    if payload.get("record_contract_version") not in _SELF_CONTAINED_RESULT_CONTRACTS:
        return
    records = cast(Sequence[Mapping[str, object]], payload["records"])
    if any(cast(Mapping[str, object], record["details"])["generated_at"] != generated_at for record in records):
        raise ProbabilityArtifactError("上涨概率 record generated_at 与 artifact 不一致")


def _validated_artifact_timestamp(value: object, path: str) -> str:
    text = _required_text(value, path)
    try:
        parsed = datetime.fromisoformat(text.replace("Z", "+00:00"))
    except ValueError as exc:
        raise ProbabilityArtifactError(f"{path} 无效") from exc
    if parsed.tzinfo is None or parsed.utcoffset() is None:
        raise ProbabilityArtifactError(f"{path} 必须包含时区")
    if parsed.astimezone(timezone.utc) > utc_now() + timedelta(minutes=5):
        raise ProbabilityArtifactError(f"{path} 不能晚于当前时间")
    return text


def _validate_result_maturity_time(payload: Mapping[str, object], generated_at: str) -> None:
    if payload.get("record_contract_version") not in _SELF_CONTAINED_RESULT_CONTRACTS:
        return
    generated = datetime.fromisoformat(generated_at.replace("Z", "+00:00"))
    market_date = generated.astimezone(timezone(timedelta(hours=8))).date()
    records = cast(Sequence[Mapping[str, object]], payload["records"])
    for record in records:
        details = cast(Mapping[str, object], record["details"])
        maturity_dates = [_optional_artifact_date(details.get(name), f"record.details.{name}") for name in ("quote_date", "entry_date", "exit_date")]
        latest = max((value for value in maturity_dates if value is not None), default=None)
        if latest is not None and latest > market_date:
            raise ProbabilityArtifactError("上涨概率 artifact 生成时间早于绑定行情或标签成熟日期")


def _optional_artifact_date(value: object, path: str) -> date | None:
    if value is None:
        return None
    text = _required_text(value, path)
    try:
        parsed = date.fromisoformat(text)
    except ValueError as exc:
        raise ProbabilityArtifactError(f"{path} 日期无效") from exc
    if parsed.isoformat() != text:
        raise ProbabilityArtifactError(f"{path} 日期无效")
    return parsed


def _validate_integrity(value: object, *, allow_legacy: bool) -> dict[str, object]:
    integrity = _required_mapping(value, "integrity")
    _require_exact_keys(integrity, _INTEGRITY_KEYS, "integrity")
    expected = {
        "algorithm": PROBABILITY_ARTIFACT_DIGEST_ALGORITHM,
        "notice": PROBABILITY_ARTIFACT_INTEGRITY_NOTICE,
    }
    if any(integrity[name] != expected_value for name, expected_value in expected.items()):
        raise ProbabilityArtifactError("上涨概率 artifact integrity contract 不受支持")
    scope = integrity["scope"]
    if scope != PROBABILITY_ARTIFACT_DIGEST_SCOPE and not (allow_legacy and scope == "payload"):
        raise ProbabilityArtifactError("上涨概率 artifact integrity scope 不受支持")
    integrity["integrity_digest"] = _required_sha256(integrity["integrity_digest"], "integrity.integrity_digest")
    return integrity


def _reject_database_target(target: Path, database: Path) -> None:
    if target == database:
        raise ProbabilityArtifactError("上涨概率 artifact 输出路径不能是 SQLite 数据库")
    if not target.exists() or not database.exists():
        return
    try:
        same_file = target.samefile(database)
    except OSError as exc:
        raise ProbabilityArtifactError("无法安全确认 artifact 与 SQLite 数据库路径不同") from exc
    if same_file:
        raise ProbabilityArtifactError("上涨概率 artifact 输出路径不能指向 SQLite 数据库")


def _study_key(value: Mapping[str, object]) -> tuple[int, str, int]:
    return cast(int, value["run_id"]), cast(str, value["target"]), cast(int, value["horizon"])


def _required_mapping(value: object, path: str) -> dict[str, object]:
    if not isinstance(value, dict):
        raise ProbabilityArtifactError(f"{path} 必须是 JSON object")
    if any(not isinstance(key, str) for key in value):
        raise ProbabilityArtifactError(f"{path} 的 key 必须是字符串")
    return value


def _required_list(value: object, path: str) -> list[object]:
    if not isinstance(value, list):
        raise ProbabilityArtifactError(f"{path} 必须是 JSON array")
    return value


def _required_text(value: object, path: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise ProbabilityArtifactError(f"{path} 必须是非空字符串")
    if value != value.strip():
        raise ProbabilityArtifactError(f"{path} 不能包含首尾空白")
    return value


def _positive_integer(value: object, path: str) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or value <= 0:
        raise ProbabilityArtifactError(f"{path} 必须是正整数")
    return value


def _required_probability(value: object, path: str) -> float:
    if isinstance(value, bool) or not isinstance(value, int | float):
        raise ProbabilityArtifactError(f"{path} 必须是有限概率")
    probability = float(value)
    if not math.isfinite(probability) or not 0.0 <= probability <= 1.0:
        raise ProbabilityArtifactError(f"{path} 必须在 [0, 1] 范围内")
    return probability


def _required_finite_number(value: object, path: str) -> float:
    if isinstance(value, bool) or not isinstance(value, int | float) or not math.isfinite(float(value)):
        raise ProbabilityArtifactError(f"{path} 必须是有限数值")
    return float(value)


def _finite_number_list(value: object, path: str) -> list[float]:
    values = _required_list(value, path)
    return [_required_finite_number(item, f"{path}[]") for item in values]


def _stable_json_digest(value: object) -> str:
    return sha256_hex(_canonical_validated_json(value))


def _sigmoid(value: float) -> float:
    bounded = max(-35.0, min(35.0, value))
    return 1.0 / (1.0 + math.exp(-bounded))


def _require_close(value: object, expected: float, path: str) -> None:
    actual = _required_finite_number(value, path)
    if not math.isclose(actual, expected, rel_tol=0.0, abs_tol=1e-12):
        raise ProbabilityArtifactError(f"上涨概率 {path} 无法确定性重放")


def _required_status(value: object, path: str) -> str:
    status = _required_text(value, path)
    if status not in PROBABILITY_ARTIFACT_STATUSES:
        raise ProbabilityArtifactError(f"{path} 不受支持")
    return status


def _required_sha256(value: object, path: str) -> str:
    if not isinstance(value, str) or _SHA256_PATTERN.fullmatch(value) is None:
        raise ProbabilityArtifactError(f"{path} 必须是小写 SHA-256 digest")
    return value


def _require_exact_keys(value: Mapping[str, object], expected: frozenset[str], path: str) -> None:
    missing = sorted(expected - value.keys())
    extra = sorted(value.keys() - expected)
    if missing or extra:
        raise ProbabilityArtifactError(f"{path} schema 不匹配；missing={missing} extra={extra}")


def _validate_json_tree(value: object, path: str) -> None:
    if isinstance(value, dict):
        if any(not isinstance(key, str) for key in value):
            raise ProbabilityArtifactError(f"{path} 的 key 必须是字符串")
        for key, item in value.items():
            _validate_json_tree(item, f"{path}.{key}")
        return
    if isinstance(value, list):
        for item in value:
            _validate_json_tree(item, f"{path}[]")
        return
    if value is None or isinstance(value, str | bool | int):
        return
    if isinstance(value, float) and math.isfinite(value):
        return
    raise ProbabilityArtifactError(f"{path} 包含非 JSON 或非有限值")


def _json_value(value: object, path: str) -> object:
    if value is None or isinstance(value, str | bool | int):
        return value
    if isinstance(value, float):
        if not math.isfinite(value):
            raise ProbabilityArtifactError(f"{path} 不能包含 NaN 或 Infinity")
        return value
    if isinstance(value, list):
        return [_json_value(item, f"{path}[]") for item in value]
    if isinstance(value, Mapping):
        if any(not isinstance(key, str) for key in value):
            raise ProbabilityArtifactError(f"{path} 的 key 必须是字符串")
        return {cast(str, key): _json_value(item, f"{path}.{key}") for key, item in value.items()}
    raise ProbabilityArtifactError(f"{path} 包含非 JSON 类型：{type(value).__name__}")
