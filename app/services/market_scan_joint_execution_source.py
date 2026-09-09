"""All-decisions signal corpus bound to official session evidence.

The ordinary probability source contains every successful score row.  That is
appropriate for the current audit-only research, but it is not the fixed
all-decisions population required by the joint execution estimand.  This module
adds every published success/missing/skipped decision, uses a preregistered
within-signal median imputation for unavailable score features, and binds the
whole set to a raw-file-verified official signal session.

The serialized artifact is content addressed but is not itself an authority
token.  Downstream code must replay it against the original source artifact,
complete published execution-session projection, and opaque verified official
session to obtain :class:`VerifiedJointExecutionSourceCorpus`.
"""

from __future__ import annotations

from collections import Counter
from collections.abc import Mapping, Sequence
from copy import deepcopy
from dataclasses import dataclass
from datetime import datetime
import json
from math import isclose, isfinite
from statistics import median
from typing import Literal, Self, cast, overload

from pydantic import BaseModel, ConfigDict, Field, model_validator

from app.artifacts.io import canonical_json_bytes, sha256_hex
from app.services.market_scan_execution_session import (
    MarketScanExecutionSessionEvidence,
)
from app.services.market_scan_official_execution import (
    BoundOfficialExecutionDecisionSession,
    VerifiedOfficialExecutionSession,
    bind_official_execution_session_to_decisions,
)
from app.services.market_scan_probability_source import (
    PROBABILITY_FEATURE_VERSION,
    PROBABILITY_SOURCE_ARTIFACT_SCHEMA_VERSION,
    verify_probability_source_snapshot,
)


JOINT_EXECUTION_SOURCE_SCHEMA_VERSION = "market-scan-joint-execution-source-artifact-v1"
JOINT_EXECUTION_SOURCE_CONTRACT_VERSION = "fixed-published-all-decisions-signal-corpus-v1"
JOINT_EXECUTION_SOURCE_FEATURE_VERSION = (
    "full-market-point-in-time-features-v5-target-semideviation-all-decisions"
)
JOINT_EXECUTION_SOURCE_IMPUTATION_POLICY = "signal_success_cohort_median_plus_status_flags_v1"
JOINT_EXECUTION_SOURCE_STATUS_FEATURES = (
    "source_score_available",
    "source_status_missing",
    "source_status_skipped",
)
_VERIFIED_SOURCE_SEAL = object()


@dataclass(frozen=True)
class _SourceBuildContext:
    session: MarketScanExecutionSessionEvidence
    source_payload: Mapping[str, object]
    source_run: Mapping[str, object]
    source_integrity: Mapping[str, object]
    snapshot_digest: str
    signal_session: str


@dataclass(frozen=True)
class _SourceBuildPopulation:
    records: list[dict[str, object]]
    feature_schema: dict[str, object]
    bound_official: BoundOfficialExecutionDecisionSession


class JointExecutionSourceError(ValueError):
    """Raised when the fixed all-decisions signal corpus cannot replay."""


class _StrictModel(BaseModel):
    model_config = ConfigDict(
        extra="forbid",
        strict=True,
        str_strip_whitespace=True,
        allow_inf_nan=False,
    )


class JointExecutionSourceRunBinding(_StrictModel):
    run_id: int = Field(gt=0)
    mode: Literal["official"] = "official"
    scope: str = Field(min_length=1)
    rule_version: str = Field(min_length=1)
    signal_session: str
    decision_frozen_at: str
    total_decision_count: int = Field(gt=0)
    success_count: int = Field(ge=0)
    missing_count: int = Field(ge=0)
    skipped_count: int = Field(ge=0)
    published_snapshot_digest: str = Field(pattern=r"^[0-9a-f]{64}$")
    probability_source_digest: str = Field(pattern=r"^[0-9a-f]{64}$")
    execution_session_digest: str = Field(pattern=r"^[0-9a-f]{64}$")
    official_signal_session_digest: str = Field(pattern=r"^[0-9a-f]{64}$")
    official_signal_raw_file_set_digest: str = Field(pattern=r"^[0-9a-f]{64}$")

    @model_validator(mode="after")
    def validate_binding(self) -> Self:
        _date(self.signal_session, "run.signal_session")
        frozen = _timestamp(self.decision_frozen_at, "run.decision_frozen_at")
        if frozen.date().isoformat() != self.signal_session:
            raise ValueError("joint source decision freeze must remain on signal session")
        if self.success_count + self.missing_count + self.skipped_count != self.total_decision_count:
            raise ValueError("joint source published result counts do not conserve")
        return self


class JointExecutionSourceFeatureSchema(_StrictModel):
    version: Literal[
        "full-market-point-in-time-features-v5-target-semideviation-all-decisions"
    ] = "full-market-point-in-time-features-v5-target-semideviation-all-decisions"
    base_version: Literal["full-market-point-in-time-features-v4-target-semideviation"] = (
        "full-market-point-in-time-features-v4-target-semideviation"
    )
    imputation_policy: Literal[
        "signal_success_cohort_median_plus_status_flags_v1"
    ] = "signal_success_cohort_median_plus_status_flags_v1"
    base_names: list[str] = Field(min_length=1)
    names: list[str] = Field(min_length=4)
    imputation_values: dict[str, float]
    imputation_digest: str = Field(pattern=r"^[0-9a-f]{64}$")
    schema_digest: str = Field(pattern=r"^[0-9a-f]{64}$")

    @model_validator(mode="after")
    def validate_schema(self) -> Self:
        if self.base_version != PROBABILITY_FEATURE_VERSION:
            raise ValueError("joint source base feature version drifted")
        if self.base_names != sorted(set(self.base_names)):
            raise ValueError("joint source base feature names must be canonical")
        expected_names = sorted([*self.base_names, *JOINT_EXECUTION_SOURCE_STATUS_FEATURES])
        if self.names != expected_names or set(self.imputation_values) != set(self.base_names):
            raise ValueError("joint source feature/imputation schema mismatch")
        if self.imputation_digest != _digest(self.imputation_values):
            raise ValueError("joint source imputation digest mismatch")
        if self.schema_digest != _digest(
            {
                "version": self.version,
                "base_version": self.base_version,
                "imputation_policy": self.imputation_policy,
                "base_names": self.base_names,
                "names": self.names,
                "imputation_digest": self.imputation_digest,
            }
        ):
            raise ValueError("joint source feature schema digest mismatch")
        return self


class JointExecutionSourceRecord(_StrictModel):
    decision_id: str = Field(min_length=1)
    symbol: str = Field(pattern=r"^\d{6}\.(SH|SZ|BJ)$")
    result_status: Literal["success", "missing", "skipped"]
    feature_availability: Literal["observed_success", "median_imputed_missing", "median_imputed_skipped"]
    features: dict[str, float] = Field(min_length=4)
    feature_vector_digest: str = Field(pattern=r"^[0-9a-f]{64}$")
    probability_source_record_digest: str | None = Field(
        default=None, pattern=r"^[0-9a-f]{64}$"
    )
    execution_session_row_digest: str = Field(pattern=r"^[0-9a-f]{64}$")
    official_signal_row_digest: str = Field(pattern=r"^[0-9a-f]{64}$")
    record_digest: str = Field(pattern=r"^[0-9a-f]{64}$")

    @model_validator(mode="after")
    def validate_record(self) -> Self:
        if not self.decision_id.endswith(f":{self.symbol}"):
            raise ValueError("joint source decision identity does not bind symbol")
        expected_availability = {
            "success": "observed_success",
            "missing": "median_imputed_missing",
            "skipped": "median_imputed_skipped",
        }[self.result_status]
        if self.feature_availability != expected_availability:
            raise ValueError("joint source feature availability conflicts with status")
        if (self.result_status == "success") is not (
            self.probability_source_record_digest is not None
        ):
            raise ValueError("joint source success record binding is inconsistent")
        if self.feature_vector_digest != _digest(self.features):
            raise ValueError("joint source feature vector digest mismatch")
        if self.record_digest != joint_execution_source_content_digest(self, "record_digest"):
            raise ValueError("joint source record digest mismatch")
        return self


class JointExecutionSourceQuality(_StrictModel):
    expected_decision_count: int = Field(gt=0)
    record_count: int = Field(gt=0)
    success_record_count: int = Field(ge=0)
    missing_record_count: int = Field(ge=0)
    skipped_record_count: int = Field(ge=0)
    official_signal_row_count: int = Field(ge=0)
    decision_coverage: float = Field(ge=0, le=1)
    official_signal_coverage: float = Field(ge=0, le=1)
    all_decisions_included: bool
    no_post_outcome_feature_selection: Literal[True] = True
    historical_replay: Literal[False] = False
    formal_forward_pit_signal_eligible: bool


class JointExecutionSourceArtifact(_StrictModel):
    schema_version: Literal["market-scan-joint-execution-source-artifact-v1"] = (
        "market-scan-joint-execution-source-artifact-v1"
    )
    contract_version: Literal["fixed-published-all-decisions-signal-corpus-v1"] = (
        "fixed-published-all-decisions-signal-corpus-v1"
    )
    generated_at: str
    run: JointExecutionSourceRunBinding
    feature_schema: JointExecutionSourceFeatureSchema
    records: list[JointExecutionSourceRecord] = Field(min_length=1)
    quality: JointExecutionSourceQuality
    decision_identity_digest: str = Field(pattern=r"^[0-9a-f]{64}$")
    decision_membership_digest: str = Field(pattern=r"^[0-9a-f]{64}$")
    record_set_digest: str = Field(pattern=r"^[0-9a-f]{64}$")
    artifact_digest: str = Field(pattern=r"^[0-9a-f]{64}$")

    @model_validator(mode="after")
    def validate_artifact(self) -> Self:
        generated = _timestamp(self.generated_at, "generated_at")
        frozen = _timestamp(self.run.decision_frozen_at, "run.decision_frozen_at")
        if generated < frozen:
            raise ValueError("joint source artifact predates the frozen decision set")
        symbols = [item.symbol for item in self.records]
        identities = [item.decision_id for item in self.records]
        if symbols != sorted(symbols) or len(set(symbols)) != len(symbols):
            raise ValueError("joint source records must use unique canonical symbols")
        _validate_quality(self)
        if self.decision_identity_digest != _digest(identities):
            raise ValueError("joint source decision identity digest mismatch")
        if self.decision_membership_digest != _digest(symbols):
            raise ValueError("joint source membership digest mismatch")
        if self.record_set_digest != _digest([item.record_digest for item in self.records]):
            raise ValueError("joint source record-set digest mismatch")
        if self.artifact_digest != joint_execution_source_content_digest(self, "artifact_digest"):
            raise ValueError("joint source artifact digest mismatch")
        return self


class VerifiedJointExecutionSourceCorpus(Sequence[Mapping[str, object]]):
    """Opaque all-decisions corpus returned only after full authority replay."""

    __slots__ = (
        "_encoded_records",
        "artifact_digest",
        "run_id",
        "signal_session",
        "source_snapshot_digest",
        "decision_identity_digest",
        "decision_membership_digest",
        "decision_frozen_at",
        "feature_schema_digest",
        "_encoded_feature_schema",
    )

    def __init__(
        self,
        encoded_records: str,
        *,
        artifact_digest: str,
        run_id: int,
        signal_session: str,
        source_snapshot_digest: str,
        decision_identity_digest: str,
        decision_membership_digest: str,
        decision_frozen_at: str,
        feature_schema_digest: str,
        encoded_feature_schema: str = "{}",
        _seal: object | None = None,
    ) -> None:
        if _seal is not _VERIFIED_SOURCE_SEAL:
            raise TypeError("joint execution source token can only be created by strict replay")
        self._encoded_records = encoded_records
        self.artifact_digest = artifact_digest
        self.run_id = run_id
        self.signal_session = signal_session
        self.source_snapshot_digest = source_snapshot_digest
        self.decision_identity_digest = decision_identity_digest
        self.decision_membership_digest = decision_membership_digest
        self.decision_frozen_at = decision_frozen_at
        self.feature_schema_digest = feature_schema_digest
        self._encoded_feature_schema = encoded_feature_schema

    @property
    def records(self) -> list[dict[str, object]]:
        value = json.loads(self._encoded_records)
        if not isinstance(value, list):  # pragma: no cover - sealed invariant
            raise TypeError("verified joint source records are invalid")
        return [cast(dict[str, object], item) for item in value]

    @property
    def feature_schema(self) -> dict[str, object]:
        value = json.loads(self._encoded_feature_schema)
        if not isinstance(value, dict):  # pragma: no cover - sealed invariant
            raise TypeError("verified joint source feature schema is invalid")
        return cast(dict[str, object], value)

    @overload
    def __getitem__(self, index: int) -> Mapping[str, object]: ...

    @overload
    def __getitem__(self, index: slice) -> Sequence[Mapping[str, object]]: ...

    def __getitem__(
        self, index: int | slice
    ) -> Mapping[str, object] | Sequence[Mapping[str, object]]:
        return self.records[index]

    def __len__(self) -> int:
        return len(self.records)


def joint_execution_source_content_digest(
    value: BaseModel | Mapping[str, object], digest_field: str
) -> str:
    if isinstance(value, BaseModel):
        payload = value.model_dump(mode="json")
    elif isinstance(value, Mapping):
        payload = deepcopy(dict(value))
    else:
        raise TypeError("joint execution source evidence must be a model or mapping")
    payload.pop(digest_field, None)
    return _digest(payload)


def build_joint_execution_source_artifact(
    probability_source: Mapping[str, object],
    execution_session: Mapping[str, object],
    official_signal_session: VerifiedOfficialExecutionSession,
    *,
    generated_at: str,
) -> dict[str, object]:
    """Build one complete forward signal corpus from trusted inputs."""

    context = _source_build_context(
        probability_source,
        execution_session,
        official_signal_session,
    )
    population = _source_build_population(context, official_signal_session)
    run = _source_run_payload(context, official_signal_session)
    payload: dict[str, object] = {
        "schema_version": JOINT_EXECUTION_SOURCE_SCHEMA_VERSION,
        "contract_version": JOINT_EXECUTION_SOURCE_CONTRACT_VERSION,
        "generated_at": generated_at,
        "run": run,
        "feature_schema": population.feature_schema,
        "records": population.records,
        "quality": _quality(population.records, run, population.bound_official),
        "decision_identity_digest": _digest([item["decision_id"] for item in population.records]),
        "decision_membership_digest": _digest([item["symbol"] for item in population.records]),
        "record_set_digest": _digest([item["record_digest"] for item in population.records]),
    }
    payload["artifact_digest"] = joint_execution_source_content_digest(payload, "artifact_digest")
    return JointExecutionSourceArtifact.model_validate(payload).model_dump(mode="json")


def _source_build_context(
    probability_source: Mapping[str, object],
    execution_session: Mapping[str, object],
    official_signal_session: VerifiedOfficialExecutionSession,
) -> _SourceBuildContext:
    source, session = _verified_inputs(probability_source, execution_session)
    source_payload = _mapping(source["payload"], "source.payload")
    source_run = _mapping(source_payload["run"], "source.run")
    source_integrity = _mapping(source["integrity"], "source.integrity")
    snapshot_digest = str(session.source_snapshot_digest or "")
    signal_session = str(source_run["quote_date"])
    if official_signal_session.session_date != signal_session:
        raise JointExecutionSourceError("official signal session date does not bind source")
    return _SourceBuildContext(
        session=session,
        source_payload=source_payload,
        source_run=source_run,
        source_integrity=source_integrity,
        snapshot_digest=snapshot_digest,
        signal_session=signal_session,
    )


def _source_build_population(
    context: _SourceBuildContext,
    official_signal_session: VerifiedOfficialExecutionSession,
) -> _SourceBuildPopulation:
    session = context.session
    execution_rows = [item.model_dump(mode="json") for item in session.rows]
    symbols = [str(item["symbol"]) for item in execution_rows]
    bound_official = bind_official_execution_session_to_decisions(
        official_signal_session,
        expected_symbols=symbols,
        source_snapshot_digest=context.snapshot_digest,
    )
    source_records = {
        str(item["symbol"]): item
        for item in _mapping_sequence(context.source_payload["records"], "source.records")
    }
    feature_schema = _joint_feature_schema(context.source_payload, source_records)
    official_rows = bound_official.row_by_symbol()
    records = [
        _joint_record(
            row,
            source_records.get(str(row["symbol"])),
            official_rows[str(row["symbol"])],
            run_id=int(cast(int, context.source_run["run_id"])),
            feature_schema=feature_schema,
        )
        for row in execution_rows
    ]
    records.sort(key=lambda item: str(item["symbol"]))
    return _SourceBuildPopulation(
        records=records,
        feature_schema=feature_schema,
        bound_official=bound_official,
    )


def _source_run_payload(
    context: _SourceBuildContext,
    official_signal_session: VerifiedOfficialExecutionSession,
) -> dict[str, object]:
    session = context.session
    return {
        "run_id": int(cast(int, context.source_run["run_id"])),
        "mode": "official",
        "scope": str(context.source_run["scope"]),
        "rule_version": str(context.source_run["rule_version"]),
        "signal_session": context.signal_session,
        "decision_frozen_at": str(context.source_run["as_of"]),
        "total_decision_count": session.expected_result_count,
        "success_count": session.expected_success_count,
        "missing_count": session.expected_missing_count,
        "skipped_count": session.expected_skipped_count,
        "published_snapshot_digest": context.snapshot_digest,
        "probability_source_digest": str(context.source_integrity["integrity_digest"]),
        "execution_session_digest": session.evidence_digest,
        "official_signal_session_digest": official_signal_session.artifact_digest,
        "official_signal_raw_file_set_digest": official_signal_session.raw_file_set_digest,
    }


def verify_joint_execution_source_artifact(value: Mapping[str, object]) -> dict[str, object]:
    try:
        return JointExecutionSourceArtifact.model_validate(dict(value)).model_dump(mode="json")
    except (TypeError, ValueError) as exc:
        raise JointExecutionSourceError("joint execution source artifact failed verification") from exc


def replay_and_verify_joint_execution_source_artifact(
    artifact: Mapping[str, object],
    probability_source: Mapping[str, object],
    execution_session: Mapping[str, object],
    official_signal_session: VerifiedOfficialExecutionSession,
) -> VerifiedJointExecutionSourceCorpus:
    """Replay every input and return the only formal source authority token."""

    verified = verify_joint_execution_source_artifact(artifact)
    rebuilt = build_joint_execution_source_artifact(
        probability_source,
        execution_session,
        official_signal_session,
        generated_at=str(verified["generated_at"]),
    )
    if rebuilt != verified:
        raise JointExecutionSourceError("joint execution source artifact does not replay")
    run = cast(dict[str, object], verified["run"])
    records = cast(list[dict[str, object]], verified["records"])
    feature_schema = cast(dict[str, object], verified["feature_schema"])
    encoded = canonical_json_bytes(records).decode("utf-8")
    return VerifiedJointExecutionSourceCorpus(
        encoded,
        artifact_digest=str(verified["artifact_digest"]),
        run_id=int(cast(int, run["run_id"])),
        signal_session=str(run["signal_session"]),
        source_snapshot_digest=str(run["published_snapshot_digest"]),
        decision_identity_digest=str(verified["decision_identity_digest"]),
        decision_membership_digest=str(verified["decision_membership_digest"]),
        decision_frozen_at=str(run["decision_frozen_at"]),
        feature_schema_digest=str(feature_schema["schema_digest"]),
        encoded_feature_schema=canonical_json_bytes(feature_schema).decode("utf-8"),
        _seal=_VERIFIED_SOURCE_SEAL,
    )


def _verified_inputs(
    probability_source: Mapping[str, object], execution_session: Mapping[str, object]
) -> tuple[dict[str, object], MarketScanExecutionSessionEvidence]:
    source = verify_probability_source_snapshot(probability_source)
    if source.get("schema_version") != PROBABILITY_SOURCE_ARTIFACT_SCHEMA_VERSION:
        raise JointExecutionSourceError("joint source requires current probability source v3")
    try:
        session = MarketScanExecutionSessionEvidence.model_validate(dict(execution_session))
    except (TypeError, ValueError) as exc:
        raise JointExecutionSourceError("joint source execution session is invalid") from exc
    payload = _mapping(source["payload"], "source.payload")
    run = _mapping(payload["run"], "source.run")
    total_count = int(cast(int, run["total_count"]))
    success_count = int(cast(int, run["success_count"]))
    skipped_count = int(cast(int, run["skipped_count"]))
    if (
        run.get("mode") != "official"
        or run.get("canonical_published") is not True
        or int(cast(int, run["run_id"])) != session.run_id
        or str(run["quote_date"]) != session.quote_date
        or str(run["as_of"]) != session.run_as_of
        or total_count != session.expected_result_count
        or success_count != session.expected_success_count
        or skipped_count != session.expected_skipped_count
        or total_count - success_count - skipped_count != session.expected_missing_count
        or session.mode != "official"
        or not session.complete_result_set
        or session.source_snapshot_binding != "verified_digest"
        or not session.source_snapshot_digest
    ):
        raise JointExecutionSourceError("joint source inputs do not share a formal run boundary")
    return source, session


def _joint_feature_schema(
    source_payload: Mapping[str, object],
    source_records: Mapping[str, Mapping[str, object]],
) -> dict[str, object]:
    raw_schema = _mapping(source_payload["feature_schema"], "source.feature_schema")
    if raw_schema.get("version") != PROBABILITY_FEATURE_VERSION:
        raise JointExecutionSourceError("joint source base feature version is unsupported")
    base_names = [str(item) for item in cast(list[object], raw_schema["names"])]
    if base_names != sorted(set(base_names)) or not base_names or not source_records:
        raise JointExecutionSourceError("joint source base feature schema is invalid")
    imputation = {
        name: float(
            median(
                _finite(cast(Mapping[str, object], record["features"])[name], name)
                for record in source_records.values()
            )
        )
        for name in base_names
    }
    names = sorted([*base_names, *JOINT_EXECUTION_SOURCE_STATUS_FEATURES])
    imputation_digest = _digest(imputation)
    schema = {
        "version": JOINT_EXECUTION_SOURCE_FEATURE_VERSION,
        "base_version": PROBABILITY_FEATURE_VERSION,
        "imputation_policy": JOINT_EXECUTION_SOURCE_IMPUTATION_POLICY,
        "base_names": base_names,
        "names": names,
        "imputation_values": imputation,
        "imputation_digest": imputation_digest,
    }
    schema["schema_digest"] = _digest(
        {
            "version": schema["version"],
            "base_version": schema["base_version"],
            "imputation_policy": schema["imputation_policy"],
            "base_names": base_names,
            "names": names,
            "imputation_digest": imputation_digest,
        }
    )
    return JointExecutionSourceFeatureSchema.model_validate(schema).model_dump(mode="json")


def _joint_record(
    execution_row: Mapping[str, object],
    source_record: Mapping[str, object] | None,
    official_row: Mapping[str, object],
    *,
    run_id: int,
    feature_schema: Mapping[str, object],
) -> dict[str, object]:
    symbol = str(execution_row["symbol"])
    status = str(execution_row["result_status"])
    if status not in {"success", "missing", "skipped"}:
        raise JointExecutionSourceError(f"joint source result status is unsupported: {status}")
    if (status == "success") is not (source_record is not None):
        raise JointExecutionSourceError(
            f"joint source success/source-record identity mismatch: {symbol}"
        )
    base_names = [str(item) for item in cast(list[object], feature_schema["base_names"])]
    imputation = cast(Mapping[str, object], feature_schema["imputation_values"])
    source_features = (
        cast(Mapping[str, object], source_record["features"])
        if source_record is not None
        else imputation
    )
    flags = {
        "source_score_available": 1.0 if status == "success" else 0.0,
        "source_status_missing": 1.0 if status == "missing" else 0.0,
        "source_status_skipped": 1.0 if status == "skipped" else 0.0,
    }
    features = {name: _finite(source_features[name], name) for name in base_names} | flags
    record: dict[str, object] = {
        "decision_id": f"{run_id}:{symbol}",
        "symbol": symbol,
        "result_status": status,
        "feature_availability": (
            "observed_success" if status == "success" else f"median_imputed_{status}"
        ),
        "features": {name: features[name] for name in sorted(features)},
        "feature_vector_digest": _digest({name: features[name] for name in sorted(features)}),
        "probability_source_record_digest": (
            _digest(source_record)
            if source_record is not None
            else None
        ),
        "execution_session_row_digest": str(execution_row["row_digest"]),
        "official_signal_row_digest": str(official_row["row_digest"]),
    }
    record["record_digest"] = joint_execution_source_content_digest(record, "record_digest")
    return JointExecutionSourceRecord.model_validate(record).model_dump(mode="json")


def _quality(
    records: Sequence[Mapping[str, object]],
    run: Mapping[str, object],
    official: BoundOfficialExecutionDecisionSession,
) -> dict[str, object]:
    statuses = Counter(str(item["result_status"]) for item in records)
    expected = int(cast(int, run["total_decision_count"]))
    quality = {
        "expected_decision_count": expected,
        "record_count": len(records),
        "success_record_count": statuses["success"],
        "missing_record_count": statuses["missing"],
        "skipped_record_count": statuses["skipped"],
        "official_signal_row_count": len(official),
        "decision_coverage": len(records) / expected,
        "official_signal_coverage": len(official) / expected,
        "all_decisions_included": len(records) == expected,
        "no_post_outcome_feature_selection": True,
        "historical_replay": False,
        "formal_forward_pit_signal_eligible": len(records) == expected == len(official),
    }
    return JointExecutionSourceQuality.model_validate(quality).model_dump(mode="json")


def _validate_quality(artifact: JointExecutionSourceArtifact) -> None:
    quality = artifact.quality
    statuses = Counter(item.result_status for item in artifact.records)
    expected = artifact.run.total_decision_count
    exact = (
        quality.expected_decision_count == expected
        and quality.record_count == len(artifact.records) == expected
        and quality.success_record_count == statuses["success"] == artifact.run.success_count
        and quality.missing_record_count == statuses["missing"] == artifact.run.missing_count
        and quality.skipped_record_count == statuses["skipped"] == artifact.run.skipped_count
        and quality.official_signal_row_count == expected
        and isclose(quality.decision_coverage, 1.0, rel_tol=0, abs_tol=1e-12)
        and isclose(quality.official_signal_coverage, 1.0, rel_tol=0, abs_tol=1e-12)
    )
    if (
        not exact
        or quality.all_decisions_included is not True
        or quality.formal_forward_pit_signal_eligible is not True
    ):
        raise ValueError("joint source quality cannot be replayed")
    names = set(artifact.feature_schema.names)
    imputation = artifact.feature_schema.imputation_values
    for record in artifact.records:
        if set(record.features) != names:
            raise ValueError("joint source record feature schema mismatch")
        if record.result_status != "success" and any(
            not isclose(record.features[name], imputation[name], rel_tol=0, abs_tol=0)
            for name in artifact.feature_schema.base_names
        ):
            raise ValueError("joint source imputed feature values cannot be replayed")


def _mapping(value: object, label: str) -> Mapping[str, object]:
    if not isinstance(value, Mapping):
        raise JointExecutionSourceError(f"{label} must be an object")
    return cast(Mapping[str, object], value)


def _mapping_sequence(value: object, label: str) -> list[Mapping[str, object]]:
    if not isinstance(value, list):
        raise JointExecutionSourceError(f"{label} must be an array")
    return [_mapping(item, f"{label}[]") for item in value]


def _date(value: str, label: str) -> None:
    try:
        parsed = datetime.strptime(value, "%Y-%m-%d")
    except (TypeError, ValueError) as exc:
        raise ValueError(f"{label} must be an ISO date") from exc
    if parsed.strftime("%Y-%m-%d") != value:
        raise ValueError(f"{label} must be a canonical ISO date")


def _timestamp(value: str, label: str) -> datetime:
    try:
        parsed = datetime.fromisoformat(value)
    except (TypeError, ValueError) as exc:
        raise ValueError(f"{label} must be an ISO timestamp") from exc
    if parsed.tzinfo is None or parsed.utcoffset() is None:
        raise ValueError(f"{label} must include a timezone offset")
    return parsed


def _digest(value: object) -> str:
    return sha256_hex(canonical_json_bytes(value))


def _finite(value: object, label: str) -> float:
    if isinstance(value, bool) or not isinstance(value, int | float):
        raise JointExecutionSourceError(f"joint source feature {label} is not numeric")
    normalized = float(value)
    if not isfinite(normalized):
        raise JointExecutionSourceError(f"joint source feature {label} is not finite")
    return normalized


__all__ = [
    "JOINT_EXECUTION_SOURCE_CONTRACT_VERSION",
    "JOINT_EXECUTION_SOURCE_FEATURE_VERSION",
    "JOINT_EXECUTION_SOURCE_IMPUTATION_POLICY",
    "JOINT_EXECUTION_SOURCE_SCHEMA_VERSION",
    "JOINT_EXECUTION_SOURCE_STATUS_FEATURES",
    "JointExecutionSourceArtifact",
    "JointExecutionSourceError",
    "VerifiedJointExecutionSourceCorpus",
    "build_joint_execution_source_artifact",
    "joint_execution_source_content_digest",
    "replay_and_verify_joint_execution_source_artifact",
    "verify_joint_execution_source_artifact",
]
