"""Preregistered three-component OOS estimator for joint execution probability.

The conditional components are fitted on their mathematically appropriate
sub-populations, but every fitted component is applied to every held-out source
decision.  The published OOS action probability is always the product

``P(entry fill) * P(exit executable | entry) * P(net excess positive | both)``.

This module accepts only opaque source/outcome tokens created by their strict
raw-evidence replay boundaries.  Its first formal candidate is H5 and was
locally preregistered before the earliest H5 label of the currently accumulating
forward corpus.  A Git/content-addressed publication is still required before
the candidate may count as externally timestamped promotion evidence.
"""

from __future__ import annotations

from collections.abc import Iterator, Mapping, Sequence
from copy import deepcopy
from dataclasses import dataclass
from datetime import datetime, timedelta
import json
from math import isfinite
from typing import Literal, cast, overload
from zoneinfo import ZoneInfo

import app.services.market_scan_probability as _probability
from app.artifacts.io import canonical_json_bytes, sha256_hex
from app.services.market_scan_joint_execution_outcomes import (
    JOINT_EXECUTION_LABEL_VERSION,
    JOINT_EXECUTION_PRIMARY_PROFILE,
    JOINT_EXECUTION_TARGET,
    VerifiedJointExecutionOutcomeCorpus,
)
from app.services.market_scan_joint_execution_source import (
    VerifiedJointExecutionSourceCorpus,
)
from app.services.joint_execution_probability_v3 import (
    VerifiedJointExecutionProbabilityCorpusV3,
    build_joint_execution_probability_corpus_v3,
    joint_execution_v3_decision_identity_digest,
)


JOINT_EXECUTION_PROBABILITY_SCHEMA_VERSION = "market-scan-joint-execution-probability-v1"
JOINT_EXECUTION_MODEL_VERSION = "joint-execution-three-component-logit-l2-v1"
JOINT_EXECUTION_CALIBRATOR_VERSION = "joint-execution-independent-platt-v1"
JOINT_EXECUTION_SPLIT_VERSION = "joint-grouped-date-horizon-purged-oos-v1"
JOINT_EXECUTION_CANDIDATE_ID = "h5-joint-execution-logit-l2-candidate-v1"
JOINT_EXECUTION_PREREGISTERED_AT = "2026-08-22T21:06:32+08:00"
JOINT_EXECUTION_HORIZON = 5
JOINT_EXECUTION_TARGET_SESSION_OFFSET = JOINT_EXECUTION_HORIZON + 1
JOINT_EXECUTION_MINIMUM_TRAIN_SESSIONS = 120
JOINT_EXECUTION_MINIMUM_CALIBRATION_SESSIONS = 40
JOINT_EXECUTION_MINIMUM_TEST_SESSIONS = 60
JOINT_EXECUTION_MINIMUM_SELECTION_FOLDS = 2
JOINT_EXECUTION_MINIMUM_LABEL_COVERAGE = 0.95
JOINT_EXECUTION_MINIMUM_BIN_SESSIONS = 20
JOINT_EXECUTION_CALIBRATION_BIN_COUNT = 5
JOINT_EXECUTION_BOOTSTRAP_SAMPLES = 1_000
JOINT_EXECUTION_L2_STRENGTH = 1.0
JOINT_EXECUTION_MAXIMUM_ITERATIONS = 100
JOINT_EXECUTION_CONVERGENCE_TOLERANCE = 1e-10
JOINT_EXECUTION_MAXIMUM_ECE = 0.05
JOINT_EXECUTION_COMPONENTS = ("entry_fill", "exit_executable", "net_positive")
JOINT_EXECUTION_DEPLOYMENT_ARTIFACT_SCHEMA_VERSION = "market-scan-joint-execution-deployment-artifact-v1"
JOINT_EXECUTION_DEPLOYMENT_CONTRACT_VERSION = "joint-execution-three-component-full-refit-v1"
JOINT_EXECUTION_DEPLOYMENT_MAXIMUM_AGE_HOURS = 36
JOINT_EXECUTION_CURRENT_PREDICTION_SCHEMA_VERSION = "market-scan-joint-execution-current-prediction-artifact-v1"
JOINT_EXECUTION_CURRENT_PREDICTION_CONTRACT_VERSION = "fixed-published-all-decisions-current-probability-v1"

_SHANGHAI = ZoneInfo("Asia/Shanghai")
_LEARNING_CORPUS_SEAL = object()
_VERIFIED_STUDY_SEAL = object()
_VERIFIED_DEPLOYMENT_SEAL = object()
_VERIFIED_CURRENT_PREDICTION_SEAL = object()


class JointExecutionProbabilityError(ValueError):
    """Raised when the three-component corpus or fit cannot replay."""


@dataclass(frozen=True)
class _EstimatorConfig:
    horizon: int = JOINT_EXECUTION_HORIZON
    minimum_train_sessions: int = JOINT_EXECUTION_MINIMUM_TRAIN_SESSIONS
    minimum_calibration_sessions: int = JOINT_EXECUTION_MINIMUM_CALIBRATION_SESSIONS
    minimum_test_sessions: int = JOINT_EXECUTION_MINIMUM_TEST_SESSIONS
    minimum_selection_folds: int = JOINT_EXECUTION_MINIMUM_SELECTION_FOLDS
    minimum_label_coverage: float = JOINT_EXECUTION_MINIMUM_LABEL_COVERAGE
    minimum_bin_sessions: int = JOINT_EXECUTION_MINIMUM_BIN_SESSIONS
    calibration_bin_count: int = JOINT_EXECUTION_CALIBRATION_BIN_COUNT
    bootstrap_samples: int = JOINT_EXECUTION_BOOTSTRAP_SAMPLES
    l2_strength: float = JOINT_EXECUTION_L2_STRENGTH
    maximum_iterations: int = JOINT_EXECUTION_MAXIMUM_ITERATIONS
    convergence_tolerance: float = JOINT_EXECUTION_CONVERGENCE_TOLERANCE
    preregistered_at: str = JOINT_EXECUTION_PREREGISTERED_AT

    def __post_init__(self) -> None:
        if self.horizon <= 0 or any(
            value <= 0
            for value in (
                self.minimum_train_sessions,
                self.minimum_calibration_sessions,
                self.minimum_test_sessions,
                self.minimum_selection_folds,
                self.minimum_bin_sessions,
                self.calibration_bin_count,
                self.bootstrap_samples,
                self.maximum_iterations,
            )
        ):
            raise ValueError("joint execution estimator thresholds must be positive")
        if not 0 < self.minimum_label_coverage <= 1:
            raise ValueError("joint execution label coverage threshold is invalid")
        if self.bootstrap_samples < 100:
            raise ValueError("joint execution bootstrap requires at least 100 samples")
        if self.l2_strength <= 0 or self.convergence_tolerance <= 0:
            raise ValueError("joint execution optimizer contract is invalid")
        _timestamp(self.preregistered_at, "preregistered_at")

    @property
    def gap_sessions(self) -> int:
        return self.horizon + 1

    @property
    def minimum_fit_sessions(self) -> int:
        return self.minimum_train_sessions + self.gap_sessions + self.minimum_calibration_sessions + self.gap_sessions + self.minimum_test_sessions

    @property
    def minimum_selection_sessions(self) -> int:
        return self.minimum_fit_sessions + (self.minimum_selection_folds - 1) * self.minimum_test_sessions


JOINT_EXECUTION_MINIMUM_SELECTION_SESSIONS = (
    JOINT_EXECUTION_MINIMUM_TRAIN_SESSIONS
    + 2 * JOINT_EXECUTION_TARGET_SESSION_OFFSET
    + JOINT_EXECUTION_MINIMUM_CALIBRATION_SESSIONS
    + JOINT_EXECUTION_MINIMUM_TEST_SESSIONS
    + (JOINT_EXECUTION_MINIMUM_SELECTION_FOLDS - 1) * JOINT_EXECUTION_MINIMUM_TEST_SESSIONS
)


@dataclass(frozen=True)
class _LearningRow:
    sample_id: str
    run_id: int
    session_date: str
    symbol: str
    features: Mapping[str, float]
    entry_fill: bool
    exit_executable: bool | None
    net_positive: bool | None
    joint_action_positive: bool
    net_return: float | None
    net_excess_return: float | None
    observed_at: str
    source_record_digest: str
    outcome_record_digest: str
    holding_path_digest: str
    benchmark_series_digest: str

    def payload(self) -> dict[str, object]:
        return {
            "sample_id": self.sample_id,
            "run_id": self.run_id,
            "session_date": self.session_date,
            "symbol": self.symbol,
            "features": {name: float(self.features[name]) for name in sorted(self.features)},
            "entry_fill": self.entry_fill,
            "exit_executable": self.exit_executable,
            "net_positive": self.net_positive,
            "joint_action_positive": self.joint_action_positive,
            "net_return": self.net_return,
            "net_excess_return": self.net_excess_return,
            "observed_at": self.observed_at,
            "source_record_digest": self.source_record_digest,
            "outcome_record_digest": self.outcome_record_digest,
            "holding_path_digest": self.holding_path_digest,
            "benchmark_series_digest": self.benchmark_series_digest,
        }


@dataclass(frozen=True)
class _DeploymentBuildContext:
    evidence: dict[str, object]
    generated: datetime
    rows: tuple[_LearningRow, ...]
    latest_observed: datetime


@dataclass(frozen=True)
class _DeploymentFit:
    split: Mapping[str, tuple[str, ...]]
    components: Mapping[str, object]
    base_rate: float
    calibration_predictions: list[dict[str, object]]
    offset: list[float]
    component_artifact_digest: str
    final_oos_digest: str


@dataclass(frozen=True)
class _DeploymentCalibration:
    base_rate: float
    predictions: list[dict[str, object]]
    offset: list[float]


@dataclass(frozen=True)
class _CurrentPredictionBuildContext:
    generated: datetime
    deployment_payload: dict[str, object]
    feature_contract: dict[str, object]
    feature_contract_digest: str


@dataclass(frozen=True)
class _FitReadiness:
    dates: tuple[str, ...]
    splits: tuple[_Split, ...]
    predated: list[str]
    reasons: list[str]


@dataclass(frozen=True)
class _OosFoldFit:
    folds: list[dict[str, object]]
    predictions: list[dict[str, object]]
    failure_reasons: list[str]


@dataclass(frozen=True)
class _V3CandidateContext:
    sample_id: str
    run_id: int
    symbol: str
    source: VerifiedJointExecutionSourceCorpus
    source_record: Mapping[str, object]
    outcome_record: Mapping[str, object]
    signal_session: str
    horizon: Mapping[str, object]
    scenario: Mapping[str, object]
    holding_path: Mapping[str, object]
    observed: Mapping[str, object]


@dataclass(frozen=True)
class _V3FoldContext:
    fold: Mapping[str, object]
    component_artifacts: Mapping[str, Mapping[str, object]]
    components: dict[str, float]
    probability: float
    fold_digest: str


@dataclass(frozen=True)
class _Split:
    train_dates: tuple[str, ...]
    train_gap_dates: tuple[str, ...]
    calibration_dates: tuple[str, ...]
    calibration_gap_dates: tuple[str, ...]
    test_dates: tuple[str, ...]

    def payload(self) -> dict[str, object]:
        return {
            "train_dates": list(self.train_dates),
            "train_gap_dates": list(self.train_gap_dates),
            "calibration_dates": list(self.calibration_dates),
            "calibration_gap_dates": list(self.calibration_gap_dates),
            "test_dates": list(self.test_dates),
        }


class VerifiedJointExecutionLearningCorpus(Sequence[Mapping[str, object]]):
    """Opaque exact join of source features and formal joint outcomes."""

    __slots__ = (
        "_encoded_rows",
        "_encoded_bindings",
        "corpus_digest",
        "feature_contract_digest",
        "feature_version",
        "feature_names",
        "label_contract_digest",
    )

    def __init__(
        self,
        encoded_rows: str,
        encoded_bindings: str,
        *,
        corpus_digest: str,
        feature_contract_digest: str,
        feature_version: str,
        feature_names: tuple[str, ...],
        label_contract_digest: str,
        _seal: object | None = None,
    ) -> None:
        if _seal is not _LEARNING_CORPUS_SEAL:
            raise TypeError("joint learning corpus can only be created by strict token join")
        self._encoded_rows = encoded_rows
        self._encoded_bindings = encoded_bindings
        self.corpus_digest = corpus_digest
        self.feature_contract_digest = feature_contract_digest
        self.feature_version = feature_version
        self.feature_names = feature_names
        self.label_contract_digest = label_contract_digest

    @property
    def rows(self) -> list[dict[str, object]]:
        value = json.loads(self._encoded_rows)
        if not isinstance(value, list):  # pragma: no cover - sealed invariant
            raise TypeError("verified joint learning rows are invalid")
        return [cast(dict[str, object], item) for item in value]

    @property
    def bindings(self) -> list[dict[str, object]]:
        value = json.loads(self._encoded_bindings)
        if not isinstance(value, list):  # pragma: no cover - sealed invariant
            raise TypeError("verified joint learning bindings are invalid")
        return [cast(dict[str, object], item) for item in value]

    @overload
    def __getitem__(self, index: int) -> Mapping[str, object]: ...

    @overload
    def __getitem__(self, index: slice) -> Sequence[Mapping[str, object]]: ...

    def __getitem__(self, index: int | slice) -> Mapping[str, object] | Sequence[Mapping[str, object]]:
        return self.rows[index]

    def __len__(self) -> int:
        return len(self.rows)

    def __iter__(self) -> Iterator[Mapping[str, object]]:
        return iter(self.rows)


class VerifiedJointExecutionProbabilityStudy(Mapping[str, object]):
    """Opaque study returned only after deterministic full-corpus refit."""

    __slots__ = ("_encoded", "evidence_digest")

    def __init__(
        self,
        encoded: str,
        *,
        evidence_digest: str,
        _seal: object | None = None,
    ) -> None:
        if _seal is not _VERIFIED_STUDY_SEAL:
            raise TypeError("joint probability study can only be created by strict replay")
        self._encoded = encoded
        self.evidence_digest = evidence_digest

    @property
    def payload(self) -> dict[str, object]:
        value = json.loads(self._encoded)
        if not isinstance(value, dict):  # pragma: no cover - sealed invariant
            raise TypeError("verified joint probability study is invalid")
        return cast(dict[str, object], value)

    def __getitem__(self, key: str) -> object:
        return self.payload[key]

    def __iter__(self) -> Iterator[str]:
        return iter(self.payload)

    def __len__(self) -> int:
        return len(self.payload)


class VerifiedJointExecutionDeploymentEstimator(Mapping[str, object]):
    """Opaque fresh full-refit three-component estimator for new decisions."""

    __slots__ = ("_encoded", "integrity_digest")

    def __init__(
        self,
        encoded: str,
        *,
        integrity_digest: str,
        _seal: object | None = None,
    ) -> None:
        if _seal is not _VERIFIED_DEPLOYMENT_SEAL:
            raise TypeError("joint deployment estimator can only be created by strict replay")
        self._encoded = encoded
        self.integrity_digest = integrity_digest

    @property
    def payload(self) -> dict[str, object]:
        value = json.loads(self._encoded)
        if not isinstance(value, dict):  # pragma: no cover - sealed invariant
            raise TypeError("verified joint deployment payload is invalid")
        return cast(dict[str, object], value)

    def __getitem__(self, key: str) -> object:
        return self.payload[key]

    def __iter__(self) -> Iterator[str]:
        return iter(self.payload)

    def __len__(self) -> int:
        return len(self.payload)


class VerifiedJointExecutionCurrentPredictionCorpus(Sequence[Mapping[str, object]]):
    """Opaque complete probability projection for one new official decision set."""

    __slots__ = (
        "_encoded_records",
        "artifact_digest",
        "deployment_artifact_digest",
        "decision_identity_digest",
        "decision_membership_digest",
        "generated_at",
        "run_id",
        "signal_session",
        "source_artifact_digest",
        "source_snapshot_digest",
    )

    def __init__(
        self,
        encoded_records: str,
        *,
        artifact_digest: str,
        deployment_artifact_digest: str,
        decision_identity_digest: str,
        decision_membership_digest: str,
        generated_at: str,
        run_id: int,
        signal_session: str,
        source_artifact_digest: str,
        source_snapshot_digest: str,
        _seal: object | None = None,
    ) -> None:
        if _seal is not _VERIFIED_CURRENT_PREDICTION_SEAL:
            raise TypeError("joint current prediction corpus can only be created by strict replay")
        self._encoded_records = encoded_records
        self.artifact_digest = artifact_digest
        self.deployment_artifact_digest = deployment_artifact_digest
        self.decision_identity_digest = decision_identity_digest
        self.decision_membership_digest = decision_membership_digest
        self.generated_at = generated_at
        self.run_id = run_id
        self.signal_session = signal_session
        self.source_artifact_digest = source_artifact_digest
        self.source_snapshot_digest = source_snapshot_digest

    @property
    def records(self) -> list[dict[str, object]]:
        value = json.loads(self._encoded_records)
        if not isinstance(value, list):  # pragma: no cover - sealed invariant
            raise TypeError("verified joint current predictions are invalid")
        return [cast(dict[str, object], item) for item in value]

    @overload
    def __getitem__(self, index: int) -> Mapping[str, object]: ...

    @overload
    def __getitem__(self, index: slice) -> Sequence[Mapping[str, object]]: ...

    def __getitem__(self, index: int | slice) -> Mapping[str, object] | Sequence[Mapping[str, object]]:
        return self.records[index]

    def __len__(self) -> int:
        return len(self.records)

    def __iter__(self) -> Iterator[Mapping[str, object]]:
        return iter(self.records)


def build_joint_execution_learning_corpus(
    sources: Sequence[VerifiedJointExecutionSourceCorpus],
    outcomes: Sequence[VerifiedJointExecutionOutcomeCorpus],
) -> VerifiedJointExecutionLearningCorpus:
    """Join exact source/outcome tokens without accepting serialized substitutes."""

    source_by_run, outcome_by_run = _verified_token_indexes(sources, outcomes)
    feature_contracts: dict[str, dict[str, object]] = {}
    label_digests: set[str] = set()
    rows: list[dict[str, object]] = []
    bindings: list[dict[str, object]] = []
    for run_id in sorted(source_by_run):
        source, outcome = source_by_run[run_id], outcome_by_run[run_id]
        _validate_token_pair(source, outcome)
        current_contract = _feature_contract(source.feature_schema)
        feature_contracts[_digest(current_contract)] = current_contract
        label_digests.add(outcome.label_contract_digest)
        joined = _join_run_rows(source, outcome)
        rows.extend(item.payload() for item in joined)
        bindings.append(_run_binding(source, outcome, len(joined)))
    _require_unique_signal_sessions(bindings)
    if len(feature_contracts) != 1 or len(label_digests) != 1:
        raise JointExecutionProbabilityError("joint learning corpus label/feature contract is not unique")
    feature_contract = next(iter(feature_contracts.values()))
    rows.sort(key=lambda item: (str(item["session_date"]), str(item["sample_id"])))
    _validate_learning_rows(rows)
    corpus_payload = {
        "feature_contract": feature_contract,
        "label_contract_digest": next(iter(label_digests)),
        "bindings": bindings,
        "rows": rows,
    }
    return VerifiedJointExecutionLearningCorpus(
        canonical_json_bytes(rows).decode("utf-8"),
        canonical_json_bytes(bindings).decode("utf-8"),
        corpus_digest=_digest(corpus_payload),
        feature_contract_digest=_digest(feature_contract),
        feature_version=str(feature_contract["version"]),
        feature_names=tuple(str(item) for item in cast(list[object], feature_contract["names"])),
        label_contract_digest=next(iter(label_digests)),
        _seal=_LEARNING_CORPUS_SEAL,
    )


def _verified_token_indexes(
    sources: Sequence[VerifiedJointExecutionSourceCorpus],
    outcomes: Sequence[VerifiedJointExecutionOutcomeCorpus],
) -> tuple[
    dict[int, VerifiedJointExecutionSourceCorpus],
    dict[int, VerifiedJointExecutionOutcomeCorpus],
]:
    if not sources or len(sources) != len(outcomes):
        raise JointExecutionProbabilityError("joint learning corpus requires paired nonempty source/outcome tokens")
    source_by_run: dict[int, VerifiedJointExecutionSourceCorpus] = {}
    outcome_by_run: dict[int, VerifiedJointExecutionOutcomeCorpus] = {}
    for source in sources:
        if not isinstance(source, VerifiedJointExecutionSourceCorpus):
            raise JointExecutionProbabilityError("joint learning source token is not verified")
        if source.run_id in source_by_run:
            raise JointExecutionProbabilityError("joint learning source run is duplicated")
        source_by_run[source.run_id] = source
    for outcome in outcomes:
        if not isinstance(outcome, VerifiedJointExecutionOutcomeCorpus):
            raise JointExecutionProbabilityError("joint learning outcome token is not verified")
        if outcome.run_id in outcome_by_run:
            raise JointExecutionProbabilityError("joint learning outcome run is duplicated")
        outcome_by_run[outcome.run_id] = outcome
    if set(source_by_run) != set(outcome_by_run):
        raise JointExecutionProbabilityError("joint learning source/outcome runs differ")
    return source_by_run, outcome_by_run


def _run_binding(
    source: VerifiedJointExecutionSourceCorpus,
    outcome: VerifiedJointExecutionOutcomeCorpus,
    record_count: int,
) -> dict[str, object]:
    return {
        "run_id": source.run_id,
        "signal_session": source.signal_session,
        "source_artifact_digest": source.artifact_digest,
        "source_snapshot_digest": source.source_snapshot_digest,
        "source_feature_schema_digest": source.feature_schema_digest,
        "outcome_artifact_digest": outcome.artifact_digest,
        "decision_identity_digest": source.decision_identity_digest,
        "decision_membership_digest": source.decision_membership_digest,
        "record_count": record_count,
    }


def _require_unique_signal_sessions(bindings: Sequence[Mapping[str, object]]) -> None:
    sessions = [str(item["signal_session"]) for item in bindings]
    if len(set(sessions)) != len(sessions):
        raise JointExecutionProbabilityError("joint learning corpus has multiple runs for one signal session")


def fit_joint_execution_probability(
    corpus: VerifiedJointExecutionLearningCorpus,
    *,
    generated_at: str,
) -> dict[str, object]:
    """Fit the immutable preregistered H5 candidate or return fail-closed progress."""

    if not isinstance(corpus, VerifiedJointExecutionLearningCorpus):
        raise JointExecutionProbabilityError("joint probability fit requires a verified learning corpus token")
    generated = _timestamp(generated_at, "generated_at")
    rows = tuple(_row_from_mapping(item) for item in corpus.rows)
    if rows and generated < max(_timestamp(item.observed_at, "observed_at") for item in rows):
        raise JointExecutionProbabilityError("joint probability evidence predates outcomes")
    return _fit_joint_rows(
        rows,
        corpus=corpus,
        generated_at=generated.isoformat(),
        config=_EstimatorConfig(),
        formal_candidate=True,
    )


def verify_joint_execution_probability_evidence(
    evidence: Mapping[str, object],
    corpus: VerifiedJointExecutionLearningCorpus | None = None,
) -> bool:
    """Verify the v1 envelope and optionally refit every OOS fold from tokens."""

    try:
        if set(evidence) != _evidence_keys():
            raise ValueError("joint probability evidence exact schema mismatch")
        if evidence.get("schema_version") != JOINT_EXECUTION_PROBABILITY_SCHEMA_VERSION:
            raise ValueError("joint probability schema version is unsupported")
        _timestamp(str(evidence["generated_at"]), "generated_at")
        expected = evidence.get("evidence_digest")
        unsigned = {key: value for key, value in evidence.items() if key != "evidence_digest"}
        if not isinstance(expected, str) or expected != _digest(unsigned):
            raise ValueError("joint probability evidence digest mismatch")
        _verify_joint_evidence_structure(evidence)
        if corpus is not None:
            rebuilt = fit_joint_execution_probability(
                corpus,
                generated_at=str(evidence["generated_at"]),
            )
            if rebuilt != dict(evidence):
                raise ValueError("joint probability evidence does not refit from corpus")
    except JointExecutionProbabilityError:
        raise
    except (KeyError, TypeError, ValueError) as exc:
        raise JointExecutionProbabilityError("joint execution probability evidence failed verification") from exc
    return True


def replay_and_verify_joint_execution_probability_evidence(
    evidence: Mapping[str, object],
    corpus: VerifiedJointExecutionLearningCorpus,
) -> VerifiedJointExecutionProbabilityStudy:
    verify_joint_execution_probability_evidence(evidence, corpus)
    encoded = canonical_json_bytes(evidence).decode("utf-8")
    return VerifiedJointExecutionProbabilityStudy(
        encoded,
        evidence_digest=str(evidence["evidence_digest"]),
        _seal=_VERIFIED_STUDY_SEAL,
    )


def build_joint_execution_probability_oos_corpus_v3(
    study: VerifiedJointExecutionProbabilityStudy,
    corpus: VerifiedJointExecutionLearningCorpus,
    sources: Sequence[VerifiedJointExecutionSourceCorpus],
    outcomes: Sequence[VerifiedJointExecutionOutcomeCorpus],
) -> VerifiedJointExecutionProbabilityCorpusV3:
    """Bind every selected OOS prediction back to its official evidence path.

    A serialized study is deliberately insufficient.  This adapter accepts only
    the opaque replay token plus the exact opaque source/outcome tokens used to
    create the learning corpus.  It returns the v3 whole-corpus token only when
    the preregistered statistical candidate is selected and every held-out
    decision replays to official bars, rules, corporate actions, costs, labels,
    benchmark series, and three component probabilities.
    """

    evidence = _verified_oos_study(study, corpus)
    source_by_run, outcome_by_run = _verified_token_indexes(sources, outcomes)
    _validate_oos_source_bindings(corpus, evidence, source_by_run, outcome_by_run)
    predictions = _oos_predictions(evidence)
    candidates = _oos_v3_candidates(
        predictions,
        evidence,
        corpus,
        source_by_run,
        outcome_by_run,
    )
    return build_joint_execution_probability_corpus_v3(candidates, predictions)


def _verified_oos_study(
    study: VerifiedJointExecutionProbabilityStudy,
    corpus: VerifiedJointExecutionLearningCorpus,
) -> dict[str, object]:
    if not isinstance(study, VerifiedJointExecutionProbabilityStudy):
        raise JointExecutionProbabilityError("joint OOS v3 binding requires a replay-verified probability study")
    if not isinstance(corpus, VerifiedJointExecutionLearningCorpus):
        raise JointExecutionProbabilityError("joint OOS v3 binding requires a verified learning corpus")
    evidence = study.payload
    verify_joint_execution_probability_evidence(evidence)
    if (
        study.evidence_digest != evidence.get("evidence_digest")
        or evidence.get("input_digest") != corpus.corpus_digest
        or evidence.get("status") != "calibrated_shadow"
        or evidence.get("fit_status") != "fitted_oos_three_component"
        or evidence.get("selection_qualified") is not True
    ):
        raise JointExecutionProbabilityError("joint OOS v3 binding requires a selected exact-corpus study")
    return evidence


def _validate_oos_source_bindings(
    corpus: VerifiedJointExecutionLearningCorpus,
    evidence: Mapping[str, object],
    source_by_run: Mapping[int, VerifiedJointExecutionSourceCorpus],
    outcome_by_run: Mapping[int, VerifiedJointExecutionOutcomeCorpus],
) -> None:
    expected_bindings = [_run_binding(source_by_run[run_id], outcome_by_run[run_id], len(source_by_run[run_id])) for run_id in sorted(source_by_run)]
    if corpus.bindings != expected_bindings or evidence.get("source_bindings") != expected_bindings:
        raise JointExecutionProbabilityError("joint OOS v3 source/outcome tokens do not bind the selected study")


def _oos_predictions(evidence: Mapping[str, object]) -> list[dict[str, object]]:
    predictions = [deepcopy(dict(_mapping(item, "study.predictions[]"))) for item in cast(list[object], evidence["predictions"])]
    predictions.sort(key=lambda item: (str(item.get("session_date")), str(item.get("sample_id"))))
    if not predictions:
        raise JointExecutionProbabilityError("joint OOS v3 study has no predictions")
    return predictions


def _oos_v3_candidates(
    predictions: Sequence[Mapping[str, object]],
    evidence: Mapping[str, object],
    corpus: VerifiedJointExecutionLearningCorpus,
    source_by_run: Mapping[int, VerifiedJointExecutionSourceCorpus],
    outcome_by_run: Mapping[int, VerifiedJointExecutionOutcomeCorpus],
) -> list[dict[str, object]]:
    prediction_by_id = _unique_mapping_index(predictions, "sample_id", "study prediction")
    learning_by_id = _unique_mapping_index(corpus.rows, "sample_id", "learning row")
    folds = [_mapping(item, "study.folds[]") for item in cast(list[object], evidence["folds"])]
    fold_by_id = _positive_int_mapping_index(folds, "fold_id", "study fold")
    group_identity_digests = _verify_oos_groups_cover_source_decisions(
        predictions,
        source_by_run,
    )
    source_records_by_run = {run_id: _unique_mapping_index(source.records, "decision_id", "source record") for run_id, source in source_by_run.items()}
    outcome_records_by_run = {run_id: _unique_mapping_index(outcome.records, "decision_id", "outcome record") for run_id, outcome in outcome_by_run.items()}

    return [
        _joint_execution_v3_candidate(
            prediction_by_id[sample_id],
            learning_by_id[sample_id],
            source_by_run,
            source_records_by_run,
            outcome_records_by_run,
            fold_by_id,
            group_identity_digests,
            generated_at=str(evidence["generated_at"]),
            feature_contract_digest=corpus.feature_contract_digest,
        )
        for sample_id in sorted(
            prediction_by_id,
            key=lambda item: (
                str(prediction_by_id[item]["session_date"]),
                item,
            ),
        )
    ]


def fit_joint_execution_deployment_estimator(
    corpus: VerifiedJointExecutionLearningCorpus,
    study: VerifiedJointExecutionProbabilityStudy,
    authorization: _probability.VerifiedProbabilityFilterAuthorization | object,
    *,
    generated_at: str,
) -> dict[str, object]:
    """Refit all three components only after exact OOS/filter authorization."""

    context = _deployment_build_context(
        corpus,
        study,
        authorization,
        generated_at=generated_at,
    )
    fitted = _fit_deployment_rows(corpus, context)
    verified_authorization = cast(
        _probability.VerifiedProbabilityFilterAuthorization,
        authorization,
    )
    payload = _deployment_payload(corpus, study, verified_authorization, context, fitted)
    return seal_joint_execution_deployment_artifact(
        payload,
        generated_at=context.generated.isoformat(),
    )


def _deployment_build_context(
    corpus: VerifiedJointExecutionLearningCorpus,
    study: VerifiedJointExecutionProbabilityStudy,
    authorization: _probability.VerifiedProbabilityFilterAuthorization | object,
    *,
    generated_at: str,
) -> _DeploymentBuildContext:
    if not isinstance(corpus, VerifiedJointExecutionLearningCorpus) or not isinstance(study, VerifiedJointExecutionProbabilityStudy):
        raise JointExecutionProbabilityError("joint deployment requires verified learning and study tokens")
    if not isinstance(authorization, _probability.VerifiedProbabilityFilterAuthorization):
        raise JointExecutionProbabilityError("joint deployment requires strict filter authorization")
    evidence = study.payload
    verify_joint_execution_probability_evidence(evidence)
    if (
        study.evidence_digest != evidence.get("evidence_digest")
        or not _deployment_corpus_extends_selected_study(corpus, evidence)
        or not _probability.probability_filter_qualified(evidence, authorization)
    ):
        raise JointExecutionProbabilityError("joint deployment OOS/filter evidence is not authorized")
    generated = _timestamp(generated_at, "deployment.generated_at")
    now = _probability.utc_now().astimezone(_SHANGHAI)
    if generated > now + timedelta(minutes=5):
        raise JointExecutionProbabilityError("joint deployment timestamp is in the future")
    evidence_generated = _timestamp(str(evidence["generated_at"]), "study.generated_at")
    authorization_generated = _timestamp(authorization.generated_at, "authorization.generated_at")
    rows = tuple(_row_from_mapping(item) for item in corpus.rows)
    latest_observed = max(_timestamp(item.observed_at, "outcome.observed_at") for item in rows)
    if generated < max(evidence_generated, authorization_generated, latest_observed):
        raise JointExecutionProbabilityError("joint deployment predates study, authorization, or official outcomes")
    if generated - latest_observed > timedelta(hours=JOINT_EXECUTION_DEPLOYMENT_MAXIMUM_AGE_HOURS):
        raise JointExecutionProbabilityError("joint deployment latest official H5 outcome is stale")
    return _DeploymentBuildContext(
        evidence=evidence,
        generated=generated,
        rows=rows,
        latest_observed=latest_observed,
    )


def _fit_deployment_rows(
    corpus: VerifiedJointExecutionLearningCorpus,
    context: _DeploymentBuildContext,
) -> _DeploymentFit:
    config = _EstimatorConfig()
    split = _joint_deployment_split(context.rows, config)
    partitions = _joint_deployment_partitions(context.rows, split)
    legacy_config = _legacy_config(config)
    components = {
        component: _fit_component(
            partitions,
            corpus.feature_names,
            component=cast(
                Literal["entry_fill", "exit_executable", "net_positive"],
                component,
            ),
            config=legacy_config,
        )
        for component in JOINT_EXECUTION_COMPONENTS
    }
    calibration = _deployment_calibration(
        partitions,
        components,
        corpus.corpus_digest,
        config,
    )
    component_artifact_digest = _digest(components)
    folds = cast(list[object], context.evidence["folds"])
    final_fold = _mapping(folds[-1], "study.folds[-1]")
    final_oos_digest = str(final_fold["component_artifact_digest"])
    if component_artifact_digest == final_oos_digest:
        raise JointExecutionProbabilityError("joint deployment cannot reuse final OOS component artifacts")
    return _DeploymentFit(
        split=split,
        components=components,
        base_rate=calibration.base_rate,
        calibration_predictions=calibration.predictions,
        offset=calibration.offset,
        component_artifact_digest=component_artifact_digest,
        final_oos_digest=final_oos_digest,
    )


def _deployment_calibration(
    partitions: Mapping[str, Sequence[_LearningRow]],
    components: Mapping[str, Mapping[str, object]],
    corpus_digest: str,
    config: _EstimatorConfig,
) -> _DeploymentCalibration:
    base_rate = _smoothed_rate([int(item.joint_action_positive) for item in partitions["calibration"]])
    predictions = [
        _held_out_prediction(item, components, fold_id=1, reference_base_rate=base_rate)
        for item in sorted(partitions["calibration"], key=lambda row: (row.session_date, row.sample_id))
    ]
    offset = _probability.probability_date_block_bootstrap_ci(
        [
            (
                str(item["session_date"]),
                int(cast(int, item["outcome"])) - float(cast(float, item["probability"])),
            )
            for item in predictions
        ],
        corpus_digest + ":joint-deployment-calibration-offset",
        config.bootstrap_samples,
        block_length_sessions=config.gap_sessions,
    )
    return _DeploymentCalibration(
        base_rate=base_rate,
        predictions=predictions,
        offset=offset,
    )


def _deployment_payload(
    corpus: VerifiedJointExecutionLearningCorpus,
    study: VerifiedJointExecutionProbabilityStudy,
    authorization: _probability.VerifiedProbabilityFilterAuthorization,
    context: _DeploymentBuildContext,
    fitted: _DeploymentFit,
) -> dict[str, object]:
    execution_bindings = _probability.probability_deployment_joint_bindings(authorization.payload)
    return {
        "contract_version": JOINT_EXECUTION_DEPLOYMENT_CONTRACT_VERSION,
        "generated_at": context.generated.isoformat(),
        "study_evidence_digest": study.evidence_digest,
        "authorization_digest": authorization.integrity_digest,
        "authorization_generated_at": authorization.generated_at,
        **execution_bindings,
        "selected_oos_corpus_digest": context.evidence["input_digest"],
        "corpus_digest": corpus.corpus_digest,
        "deployment_corpus_policy": ("exact_selected_oos_bindings_plus_strictly_later_official_sessions"),
        "source_bindings": corpus.bindings,
        "source_binding_digest": _digest(corpus.bindings),
        "deployment_corpus_appended_session_count": (len(corpus.bindings) - len(cast(list[object], context.evidence["source_bindings"]))),
        "feature_contract_digest": corpus.feature_contract_digest,
        "feature_version": corpus.feature_version,
        "feature_names": list(corpus.feature_names),
        "label_contract_digest": corpus.label_contract_digest,
        "horizon": JOINT_EXECUTION_HORIZON,
        "target": JOINT_EXECUTION_TARGET,
        "split_version": "joint-execution-deployment-train-gap-calibration-v1",
        "training_dates": list(fitted.split["train_dates"]),
        "purge_dates": list(fitted.split["gap_dates"]),
        "calibration_dates": list(fitted.split["calibration_dates"]),
        "training_cutoff": fitted.split["train_dates"][-1],
        "calibration_start": fitted.split["calibration_dates"][0],
        "calibration_cutoff": fitted.split["calibration_dates"][-1],
        "components": fitted.components,
        "component_artifact_digest": fitted.component_artifact_digest,
        "calibration_joint_base_rate": fitted.base_rate,
        "calibration_predictions": fitted.calibration_predictions,
        "calibration_predictions_digest": _digest(fitted.calibration_predictions),
        "calibration_offset_ci_95": fitted.offset,
        "latest_outcome_observed_at": context.latest_observed.isoformat(),
        "latest_signal_session": max(item.session_date for item in context.rows),
        "freshness": {
            "maximum_age_hours": JOINT_EXECUTION_DEPLOYMENT_MAXIMUM_AGE_HOURS,
            "requires_fresh_official_h5_outcome": True,
        },
        "oos_final_fold_component_artifact_digest": fitted.final_oos_digest,
        "oos_final_fold_reuse_forbidden": True,
    }


def seal_joint_execution_deployment_artifact(payload: Mapping[str, object], *, generated_at: str) -> dict[str, object]:
    generated = _timestamp(generated_at, "deployment.generated_at").isoformat()
    normalized = deepcopy(dict(payload))
    if normalized.get("generated_at") != generated:
        raise JointExecutionProbabilityError("joint deployment envelope/payload timestamp mismatch")
    identity = {"generated_at": generated, "payload": normalized}
    return {
        "schema_version": JOINT_EXECUTION_DEPLOYMENT_ARTIFACT_SCHEMA_VERSION,
        "generated_at": generated,
        "payload": normalized,
        "integrity": {
            "algorithm": "sha256",
            "scope": "generated_at+payload",
            "notice": "content_address_only_not_signature_strict_replay_required",
            "integrity_digest": _digest(identity),
        },
    }


def verify_joint_execution_deployment_artifact(
    artifact: Mapping[str, object],
    *,
    corpus: VerifiedJointExecutionLearningCorpus,
    study: VerifiedJointExecutionProbabilityStudy,
    authorization: _probability.VerifiedProbabilityFilterAuthorization | object,
    as_of: str | None = None,
) -> VerifiedJointExecutionDeploymentEstimator:
    """Replay the full refit and return the only deployment prediction token."""

    try:
        if set(artifact) != {"schema_version", "generated_at", "payload", "integrity"}:
            raise ValueError("joint deployment envelope exact schema mismatch")
        if artifact.get("schema_version") != JOINT_EXECUTION_DEPLOYMENT_ARTIFACT_SCHEMA_VERSION:
            raise ValueError("joint deployment schema is unsupported")
        generated_at = str(artifact["generated_at"])
        payload = _mapping(artifact["payload"], "deployment.payload")
        integrity = _mapping(artifact["integrity"], "deployment.integrity")
        if set(integrity) != {"algorithm", "scope", "notice", "integrity_digest"}:
            raise ValueError("joint deployment integrity exact schema mismatch")
        digest = str(integrity["integrity_digest"])
        if (
            integrity.get("algorithm") != "sha256"
            or integrity.get("scope") != "generated_at+payload"
            or integrity.get("notice") != "content_address_only_not_signature_strict_replay_required"
            or digest != _digest({"generated_at": generated_at, "payload": dict(payload)})
        ):
            raise ValueError("joint deployment content address mismatch")
        rebuilt = fit_joint_execution_deployment_estimator(
            corpus,
            study,
            authorization,
            generated_at=generated_at,
        )
        if rebuilt != dict(artifact):
            raise ValueError("joint deployment does not replay from its full corpus")
        if not _joint_deployment_is_fresh(payload, as_of):
            raise ValueError("joint deployment estimator is stale")
    except JointExecutionProbabilityError:
        raise
    except (KeyError, TypeError, ValueError) as exc:
        raise JointExecutionProbabilityError("joint deployment artifact failed strict verification") from exc
    return VerifiedJointExecutionDeploymentEstimator(
        canonical_json_bytes(payload).decode("utf-8"),
        integrity_digest=digest,
        _seal=_VERIFIED_DEPLOYMENT_SEAL,
    )


def predict_joint_execution_probability(
    study: VerifiedJointExecutionProbabilityStudy,
    deployment: VerifiedJointExecutionDeploymentEstimator,
    features: Mapping[str, float],
    *,
    sample_id: str,
    as_of: str | None = None,
) -> dict[str, object]:
    """Predict one new all-decisions action probability from a fresh refit."""

    payload, normalized = _joint_prediction_inputs(study, deployment, features, as_of)
    raw, calibrated = _component_probability_estimates(payload, normalized)
    return _joint_prediction_payload(
        deployment,
        payload,
        normalized,
        raw,
        calibrated,
        sample_id=sample_id,
    )


def _joint_prediction_inputs(
    study: VerifiedJointExecutionProbabilityStudy,
    deployment: VerifiedJointExecutionDeploymentEstimator,
    features: Mapping[str, float],
    as_of: str | None,
) -> tuple[dict[str, object], dict[str, float]]:
    if not isinstance(study, VerifiedJointExecutionProbabilityStudy) or not isinstance(deployment, VerifiedJointExecutionDeploymentEstimator):
        raise JointExecutionProbabilityError("joint prediction requires verified study/deployment tokens")
    evidence = study.payload
    payload = deployment.payload
    if (
        payload.get("contract_version") != JOINT_EXECUTION_DEPLOYMENT_CONTRACT_VERSION
        or payload.get("study_evidence_digest") != study.evidence_digest
        or evidence.get("selection_qualified") is not True
        or not _joint_deployment_is_fresh(payload, as_of)
    ):
        raise JointExecutionProbabilityError("joint deployment does not bind a selected fresh study")
    names = tuple(str(item) for item in cast(list[object], payload["feature_names"]))
    normalized = _numeric_features(features)
    if tuple(normalized) != names:
        raise JointExecutionProbabilityError("joint deployment feature schema does not match the new decision")
    return payload, normalized


def _component_probability_estimates(
    payload: Mapping[str, object],
    normalized: Mapping[str, float],
) -> tuple[dict[str, float], dict[str, float]]:
    artifacts = _mapping(payload["components"], "deployment.components")
    raw: dict[str, float] = {}
    calibrated: dict[str, float] = {}
    for component in JOINT_EXECUTION_COMPONENTS:
        artifact = _mapping(artifacts[component], f"deployment.{component}")
        raw[component] = _probability.probability_model_probability(_mapping(artifact["model"], f"deployment.{component}.model"), normalized)
        calibrated[component] = _probability.probability_platt_probability(
            _mapping(artifact["calibrator"], f"deployment.{component}.calibrator"),
            raw[component],
        )
    return raw, calibrated


def _joint_prediction_payload(
    deployment: VerifiedJointExecutionDeploymentEstimator,
    payload: Mapping[str, object],
    normalized: Mapping[str, float],
    raw: Mapping[str, float],
    calibrated: Mapping[str, float],
    *,
    sample_id: str,
) -> dict[str, object]:
    raw_probability = raw["entry_fill"] * raw["exit_executable"] * raw["net_positive"]
    probability = calibrated["entry_fill"] * calibrated["exit_executable"] * calibrated["net_positive"]
    offset = _float_pair(payload["calibration_offset_ci_95"], "calibration_offset")
    return {
        "sample_id": sample_id,
        "status": "calibrated_shadow",
        "probability": probability,
        "raw_probability": raw_probability,
        "component_probabilities": calibrated,
        "raw_component_probabilities": raw,
        "reference_base_rate": payload["calibration_joint_base_rate"],
        "calibration_bias_interval": list(offset),
        "calibration_adjusted_probability_interval": [
            min(1.0, max(0.0, probability + offset[0])),
            min(1.0, max(0.0, probability + offset[1])),
        ],
        "deployment_status": "fresh_verified_joint_execution_deployment",
        "deployment_generated_at": payload["generated_at"],
        "training_cutoff": payload["training_cutoff"],
        "calibration_cutoff": payload["calibration_cutoff"],
        "component_artifact_digest": payload["component_artifact_digest"],
        "deployment_artifact_digest": deployment.integrity_digest,
        "input_digest": payload["corpus_digest"],
        "feature_vector_digest": _digest(normalized),
    }


def joint_execution_deployment_is_fresh(
    deployment: VerifiedJointExecutionDeploymentEstimator | object,
    *,
    as_of: str | None = None,
) -> bool:
    """Return freshness only for an opaque replay-verified deployment token."""

    return bool(isinstance(deployment, VerifiedJointExecutionDeploymentEstimator) and _joint_deployment_is_fresh(deployment.payload, as_of))


def build_joint_execution_current_prediction_artifact(
    source: VerifiedJointExecutionSourceCorpus,
    study: VerifiedJointExecutionProbabilityStudy,
    deployment: VerifiedJointExecutionDeploymentEstimator,
    *,
    generated_at: str,
) -> dict[str, object]:
    """Predict every decision in one new official source without future labels."""

    context = _current_prediction_build_context(
        source,
        study,
        deployment,
        generated_at=generated_at,
    )
    records = _current_prediction_records(source, study, deployment, context.generated)
    payload = _current_prediction_payload(source, study, deployment, context, records)
    return seal_joint_execution_current_prediction_artifact(
        payload,
        generated_at=context.generated.isoformat(),
    )


def _current_prediction_build_context(
    source: VerifiedJointExecutionSourceCorpus,
    study: VerifiedJointExecutionProbabilityStudy,
    deployment: VerifiedJointExecutionDeploymentEstimator,
    *,
    generated_at: str,
) -> _CurrentPredictionBuildContext:
    if not isinstance(source, VerifiedJointExecutionSourceCorpus):
        raise JointExecutionProbabilityError("joint current prediction requires a verified official source token")
    if not isinstance(study, VerifiedJointExecutionProbabilityStudy) or not isinstance(deployment, VerifiedJointExecutionDeploymentEstimator):
        raise JointExecutionProbabilityError("joint current prediction requires verified study/deployment tokens")
    generated = _timestamp(generated_at, "current_prediction.generated_at")
    frozen = _timestamp(source.decision_frozen_at, "source.decision_frozen_at")
    if generated < frozen or generated.date().isoformat() != source.signal_session:
        raise JointExecutionProbabilityError("joint current prediction must be generated after freeze on signal session")
    deployment_payload = deployment.payload
    if not _joint_deployment_is_fresh(deployment_payload, generated.isoformat()):
        raise JointExecutionProbabilityError("joint current prediction requires a fresh deployment estimator")
    if source.signal_session <= str(deployment_payload["latest_signal_session"]):
        raise JointExecutionProbabilityError("joint current source must be strictly newer than deployment outcomes")
    feature_contract = _feature_contract(source.feature_schema)
    feature_contract_digest = _digest(feature_contract)
    if feature_contract_digest != deployment_payload.get("feature_contract_digest") or cast(list[object], feature_contract["names"]) != cast(
        list[object], deployment_payload["feature_names"]
    ):
        raise JointExecutionProbabilityError("joint current source feature contract does not match deployment")
    return _CurrentPredictionBuildContext(
        generated=generated,
        deployment_payload=deployment_payload,
        feature_contract=feature_contract,
        feature_contract_digest=feature_contract_digest,
    )


def _current_prediction_records(
    source: VerifiedJointExecutionSourceCorpus,
    study: VerifiedJointExecutionProbabilityStudy,
    deployment: VerifiedJointExecutionDeploymentEstimator,
    generated: datetime,
) -> list[dict[str, object]]:
    records = [
        _joint_current_prediction_record(
            source,
            source_record,
            study,
            deployment,
            generated_at=generated.isoformat(),
        )
        for source_record in source.records
    ]
    records.sort(key=lambda item: str(item["symbol"]))
    symbols = [str(item["symbol"]) for item in records]
    decision_ids = [str(item["decision_id"]) for item in records]
    if (
        len(records) != len(source)
        or len(set(symbols)) != len(records)
        or symbols != sorted(symbols)
        or _digest(symbols) != source.decision_membership_digest
        or _digest(decision_ids) != source.decision_identity_digest
    ):
        raise JointExecutionProbabilityError("joint current predictions do not cover the fixed decision set")
    return records


def _current_prediction_payload(
    source: VerifiedJointExecutionSourceCorpus,
    study: VerifiedJointExecutionProbabilityStudy,
    deployment: VerifiedJointExecutionDeploymentEstimator,
    context: _CurrentPredictionBuildContext,
    records: Sequence[Mapping[str, object]],
) -> dict[str, object]:
    return {
        "contract_version": JOINT_EXECUTION_CURRENT_PREDICTION_CONTRACT_VERSION,
        "generated_at": context.generated.isoformat(),
        "run": {
            "run_id": source.run_id,
            "signal_session": source.signal_session,
            "decision_frozen_at": source.decision_frozen_at,
            "decision_count": len(source),
            "source_artifact_digest": source.artifact_digest,
            "source_snapshot_digest": source.source_snapshot_digest,
            "source_feature_schema_digest": source.feature_schema_digest,
            "decision_identity_digest": source.decision_identity_digest,
            "decision_membership_digest": source.decision_membership_digest,
        },
        "study_evidence_digest": study.evidence_digest,
        "deployment_artifact_digest": deployment.integrity_digest,
        "authorization_digest": context.deployment_payload["authorization_digest"],
        "feature_contract": context.feature_contract,
        "feature_contract_digest": context.feature_contract_digest,
        "horizon": JOINT_EXECUTION_HORIZON,
        "target": JOINT_EXECUTION_TARGET,
        "filter_use": "authorized_current_probability",
        "production_ranking_use": "forbidden_without_v6_shadow_and_manual_promotion",
        "records": records,
        "record_count": len(records),
        "record_set_digest": _digest([item["record_digest"] for item in records]),
    }


def _joint_current_prediction_record(
    source: VerifiedJointExecutionSourceCorpus,
    source_record: Mapping[str, object],
    study: VerifiedJointExecutionProbabilityStudy,
    deployment: VerifiedJointExecutionDeploymentEstimator,
    *,
    generated_at: str,
) -> dict[str, object]:
    symbol = str(source_record["symbol"])
    sample_id = f"{source.run_id}:{symbol}:{JOINT_EXECUTION_HORIZON}:{JOINT_EXECUTION_TARGET}"
    estimate = predict_joint_execution_probability(
        study,
        deployment,
        _numeric_features(source_record["features"]),
        sample_id=sample_id,
        as_of=generated_at,
    )
    if estimate["feature_vector_digest"] != source_record["feature_vector_digest"]:
        raise JointExecutionProbabilityError("joint current prediction feature vector does not bind source record")
    record: dict[str, object] = {
        "sample_id": sample_id,
        "decision_id": str(source_record["decision_id"]),
        "run_id": source.run_id,
        "signal_session": source.signal_session,
        "symbol": symbol,
        "result_status": str(source_record["result_status"]),
        "feature_availability": str(source_record["feature_availability"]),
        "source_record_digest": str(source_record["record_digest"]),
        "source_feature_vector_digest": str(source_record["feature_vector_digest"]),
        "source_artifact_digest": source.artifact_digest,
        "source_snapshot_digest": source.source_snapshot_digest,
        **estimate,
    }
    record["record_digest"] = _digest(record)
    return record


def seal_joint_execution_current_prediction_artifact(payload: Mapping[str, object], *, generated_at: str) -> dict[str, object]:
    generated = _timestamp(generated_at, "current_prediction.generated_at").isoformat()
    normalized = deepcopy(dict(payload))
    if normalized.get("generated_at") != generated:
        raise JointExecutionProbabilityError("joint current prediction envelope/payload timestamp mismatch")
    identity = {"generated_at": generated, "payload": normalized}
    return {
        "schema_version": JOINT_EXECUTION_CURRENT_PREDICTION_SCHEMA_VERSION,
        "generated_at": generated,
        "payload": normalized,
        "integrity": {
            "algorithm": "sha256",
            "scope": "generated_at+payload",
            "notice": "content_address_only_not_signature_strict_replay_required",
            "integrity_digest": _digest(identity),
        },
    }


def verify_joint_execution_current_prediction_artifact(
    artifact: Mapping[str, object],
    *,
    source: VerifiedJointExecutionSourceCorpus,
    study: VerifiedJointExecutionProbabilityStudy,
    deployment: VerifiedJointExecutionDeploymentEstimator,
    as_of: str | None = None,
) -> VerifiedJointExecutionCurrentPredictionCorpus:
    """Replay a full current-batch projection and return its opaque token."""

    try:
        generated_at, payload, digest = _current_prediction_envelope(artifact)
        _replay_current_prediction_artifact(
            artifact,
            source,
            study,
            deployment,
            generated_at,
        )
        reference = as_of or generated_at
        if not _joint_deployment_is_fresh(deployment.payload, reference):
            raise ValueError("joint current prediction deployment is stale")
        run = _mapping(payload["run"], "current_prediction.run")
        records = [deepcopy(dict(_mapping(item, "current_prediction.records[]"))) for item in cast(list[object], payload["records"])]
    except JointExecutionProbabilityError:
        raise
    except (KeyError, TypeError, ValueError) as exc:
        raise JointExecutionProbabilityError("joint current prediction artifact failed strict verification") from exc
    return VerifiedJointExecutionCurrentPredictionCorpus(
        canonical_json_bytes(records).decode("utf-8"),
        artifact_digest=digest,
        deployment_artifact_digest=deployment.integrity_digest,
        decision_identity_digest=str(run["decision_identity_digest"]),
        decision_membership_digest=str(run["decision_membership_digest"]),
        generated_at=generated_at,
        run_id=int(cast(int, run["run_id"])),
        signal_session=str(run["signal_session"]),
        source_artifact_digest=source.artifact_digest,
        source_snapshot_digest=source.source_snapshot_digest,
        _seal=_VERIFIED_CURRENT_PREDICTION_SEAL,
    )


def _current_prediction_envelope(
    artifact: Mapping[str, object],
) -> tuple[str, dict[str, object], str]:
    if set(artifact) != {"schema_version", "generated_at", "payload", "integrity"}:
        raise ValueError("joint current prediction envelope exact schema mismatch")
    if artifact.get("schema_version") != JOINT_EXECUTION_CURRENT_PREDICTION_SCHEMA_VERSION:
        raise ValueError("joint current prediction schema is unsupported")
    generated_at = str(artifact["generated_at"])
    payload = _mapping(artifact["payload"], "current_prediction.payload")
    integrity = _mapping(artifact["integrity"], "current_prediction.integrity")
    if set(integrity) != {"algorithm", "scope", "notice", "integrity_digest"}:
        raise ValueError("joint current prediction integrity exact schema mismatch")
    digest = str(integrity["integrity_digest"])
    expected = _digest({"generated_at": generated_at, "payload": dict(payload)})
    if (
        integrity.get("algorithm") != "sha256"
        or integrity.get("scope") != "generated_at+payload"
        or integrity.get("notice") != "content_address_only_not_signature_strict_replay_required"
        or digest != expected
    ):
        raise ValueError("joint current prediction content address mismatch")
    return generated_at, dict(payload), digest


def _replay_current_prediction_artifact(
    artifact: Mapping[str, object],
    source: VerifiedJointExecutionSourceCorpus,
    study: VerifiedJointExecutionProbabilityStudy,
    deployment: VerifiedJointExecutionDeploymentEstimator,
    generated_at: str,
) -> None:
    rebuilt = build_joint_execution_current_prediction_artifact(
        source,
        study,
        deployment,
        generated_at=generated_at,
    )
    if rebuilt != dict(artifact):
        raise ValueError("joint current prediction does not replay from exact tokens")


def joint_execution_preregistration_contract() -> dict[str, object]:
    payload: dict[str, object] = {
        "version": "joint-execution-probability-preregistration-v1",
        "candidate_id": JOINT_EXECUTION_CANDIDATE_ID,
        "registered_at": JOINT_EXECUTION_PREREGISTERED_AT,
        "registration_scope": "local_content_address_pending_external_git_timestamp",
        "horizon": JOINT_EXECUTION_HORIZON,
        "target_session_offset": JOINT_EXECUTION_TARGET_SESSION_OFFSET,
        "target": "joint_execution_action_positive_net_excess",
        "primary_cost_profile": JOINT_EXECUTION_PRIMARY_PROFILE,
        "component_order": list(JOINT_EXECUTION_COMPONENTS),
        "joint_formula": ("P(entry_fill|I_D)*P(exit_executable|entry_fill,I_D)*" "P(net_excess_positive|entry_fill,exit_executable,I_D)"),
        "conditional_training_populations": {
            "entry_fill": "all_fixed_decisions",
            "exit_executable": "entry_fill_observed_true_only",
            "net_positive": "entry_fill_and_exit_executable_observed_true_only",
        },
        "held_out_application_population": {component: "every_fixed_test_decision" for component in JOINT_EXECUTION_COMPONENTS},
        "model": {
            "version": JOINT_EXECUTION_MODEL_VERSION,
            "algorithm": "standardized_l2_logistic_regression_newton",
            "l2_strength": JOINT_EXECUTION_L2_STRENGTH,
            "maximum_iterations": JOINT_EXECUTION_MAXIMUM_ITERATIONS,
            "convergence_tolerance": JOINT_EXECUTION_CONVERGENCE_TOLERANCE,
        },
        "calibrator": {
            "version": JOINT_EXECUTION_CALIBRATOR_VERSION,
            "algorithm": "independent_platt_per_component",
        },
        "split": _split_contract(_EstimatorConfig()),
        "evaluation": _evaluation_contract(_EstimatorConfig()),
        "candidate_family": [JOINT_EXECUTION_CANDIDATE_ID],
        "automatic_promotion": False,
    }
    payload["contract_digest"] = _digest(payload)
    return payload


def _validate_token_pair(
    source: VerifiedJointExecutionSourceCorpus,
    outcome: VerifiedJointExecutionOutcomeCorpus,
) -> None:
    if (
        source.run_id != outcome.run_id
        or source.signal_session != outcome.signal_session
        or source.decision_identity_digest != outcome.decision_identity_digest
        or source.decision_membership_digest != outcome.decision_membership_digest
        or source.feature_schema_digest != outcome.feature_schema_digest
        or len(source) != len(outcome)
    ):
        raise JointExecutionProbabilityError("joint learning source/outcome token boundary mismatch")


def _feature_contract(schema: Mapping[str, object]) -> dict[str, object]:
    required = {"version", "base_version", "imputation_policy", "names", "base_names"}
    if not required <= set(schema):
        raise JointExecutionProbabilityError("joint source feature schema is incomplete")
    names = [str(item) for item in cast(list[object], schema["names"])]
    base_names = [str(item) for item in cast(list[object], schema["base_names"])]
    if names != sorted(set(names)) or base_names != sorted(set(base_names)):
        raise JointExecutionProbabilityError("joint source feature names are not canonical")
    return {
        "version": str(schema["version"]),
        "base_version": str(schema["base_version"]),
        "imputation_policy": str(schema["imputation_policy"]),
        "names": names,
        "base_names": base_names,
        "per_signal_imputation_values_allowed": True,
        "imputation_values_must_be_frozen_at_signal": True,
    }


def _join_run_rows(
    source: VerifiedJointExecutionSourceCorpus,
    outcome: VerifiedJointExecutionOutcomeCorpus,
) -> tuple[_LearningRow, ...]:
    source_by_id = {str(item["decision_id"]): item for item in source.records}
    outcome_by_id = {str(item["decision_id"]): item for item in outcome.records}
    if set(source_by_id) != set(outcome_by_id):
        raise JointExecutionProbabilityError("joint learning decision rows are incomplete")
    output: list[_LearningRow] = []
    for decision_id in sorted(source_by_id):
        source_row, outcome_row = source_by_id[decision_id], outcome_by_id[decision_id]
        if str(outcome_row["source_record_digest"]) != str(source_row["record_digest"]):
            raise JointExecutionProbabilityError("joint learning source record digest mismatch")
        horizon = _selected_horizon(outcome_row)
        scenario = _selected_scenario(horizon)
        observed = _mapping(scenario["observed_outcome"], "observed_outcome")
        path = _mapping(horizon["holding_path"], "holding_path")
        features = _numeric_features(source_row["features"])
        symbol = str(source_row["symbol"])
        output.append(
            _LearningRow(
                sample_id=f"{source.run_id}:{symbol}:{JOINT_EXECUTION_HORIZON}:{JOINT_EXECUTION_TARGET}",
                run_id=source.run_id,
                session_date=source.signal_session,
                symbol=symbol,
                features=features,
                entry_fill=cast(bool, observed["entry_fill"]),
                exit_executable=cast(bool | None, observed["exit_executable"]),
                net_positive=cast(bool | None, observed["net_positive"]),
                joint_action_positive=cast(bool, observed["joint_action_positive"]),
                net_return=_optional_float(observed.get("net_return"), "net_return"),
                net_excess_return=_optional_float(observed.get("net_excess_return"), "net_excess_return"),
                observed_at=str(observed["observed_at"]),
                source_record_digest=str(source_row["record_digest"]),
                outcome_record_digest=str(outcome_row["record_digest"]),
                holding_path_digest=str(path["path_digest"]),
                benchmark_series_digest=str(scenario["benchmark_series_digest"]),
            )
        )
    return tuple(output)


def _selected_horizon(outcome_row: Mapping[str, object]) -> Mapping[str, object]:
    values = [_mapping(item, "outcome.horizons[]") for item in cast(list[object], outcome_row["horizons"])]
    selected = [item for item in values if item["horizon"] == JOINT_EXECUTION_HORIZON]
    if len(selected) != 1:
        raise JointExecutionProbabilityError("joint learning H5 outcome is missing")
    return selected[0]


def _selected_scenario(horizon: Mapping[str, object]) -> Mapping[str, object]:
    values = [_mapping(item, "outcome.scenarios[]") for item in cast(list[object], horizon["scenarios"])]
    selected = [item for item in values if item["profile_name"] == JOINT_EXECUTION_PRIMARY_PROFILE]
    if len(selected) != 1:
        raise JointExecutionProbabilityError("joint learning base-cost scenario is missing")
    return selected[0]


def _verify_oos_groups_cover_source_decisions(
    predictions: Sequence[Mapping[str, object]],
    source_by_run: Mapping[int, VerifiedJointExecutionSourceCorpus],
) -> dict[tuple[int, str], str]:
    grouped: dict[tuple[int, str], list[str]] = {}
    symbols_by_run = {run_id: {str(item["symbol"]) for item in source.records} for run_id, source in source_by_run.items()}
    for prediction in predictions:
        sample_id = str(prediction.get("sample_id") or "")
        run_id, symbol = _joint_sample_identity(sample_id)
        session = str(prediction.get("session_date") or "")
        source = source_by_run.get(run_id)
        if source is None or source.signal_session != session:
            raise JointExecutionProbabilityError("joint OOS prediction does not bind a verified signal source")
        if symbol not in symbols_by_run[run_id]:
            raise JointExecutionProbabilityError("joint OOS prediction symbol is outside the fixed source universe")
        grouped.setdefault((run_id, session), []).append(sample_id)

    digests: dict[tuple[int, str], str] = {}
    for (run_id, session), sample_ids in grouped.items():
        source = source_by_run[run_id]
        expected = sorted(f"{run_id}:{item['symbol']}:{JOINT_EXECUTION_HORIZON}:{JOINT_EXECUTION_TARGET}" for item in source.records)
        if sorted(sample_ids) != expected:
            raise JointExecutionProbabilityError("joint OOS predictions do not cover every fixed source decision")
        digests[(run_id, session)] = joint_execution_v3_decision_identity_digest(expected)
    return digests


def _joint_execution_v3_candidate(
    prediction: Mapping[str, object],
    learning: Mapping[str, object],
    source_by_run: Mapping[int, VerifiedJointExecutionSourceCorpus],
    source_records_by_run: Mapping[int, Mapping[str, Mapping[str, object]]],
    outcome_records_by_run: Mapping[int, Mapping[str, Mapping[str, object]]],
    fold_by_id: Mapping[int, Mapping[str, object]],
    group_identity_digests: Mapping[tuple[int, str], str],
    *,
    generated_at: str,
    feature_contract_digest: str,
) -> dict[str, object]:
    context = _v3_candidate_context(
        prediction,
        learning,
        source_by_run,
        source_records_by_run,
        outcome_records_by_run,
    )
    fold_context = _v3_fold_context(prediction, context.signal_session, fold_by_id)
    identity_digest = group_identity_digests[(context.run_id, context.signal_session)]
    universe_digest = _v3_universe_definition_digest(context.source)
    return {
        "sample_id": context.sample_id,
        "symbol": context.symbol,
        "signal_session": context.signal_session,
        "generated_at": generated_at,
        "evidence": _v3_candidate_evidence(
            context,
            fold_context,
            identity_digest,
            universe_digest,
            feature_contract_digest,
            prediction,
        ),
        "entry_state": context.horizon["entry_state"],
        "exit_state": context.horizon["exit_state"],
        "holding_path": context.holding_path,
        "costs": context.scenario["costs"],
        "observed_outcome": context.observed,
        "decision_set": _v3_decision_set(context, identity_digest, universe_digest),
        "probabilities": _v3_probability_payload(fold_context),
    }


def _v3_candidate_context(
    prediction: Mapping[str, object],
    learning: Mapping[str, object],
    source_by_run: Mapping[int, VerifiedJointExecutionSourceCorpus],
    source_records_by_run: Mapping[int, Mapping[str, Mapping[str, object]]],
    outcome_records_by_run: Mapping[int, Mapping[str, Mapping[str, object]]],
) -> _V3CandidateContext:
    sample_id = str(prediction["sample_id"])
    run_id, symbol = _joint_sample_identity(sample_id)
    source = source_by_run[run_id]
    decision_id = f"{run_id}:{symbol}"
    source_record = source_records_by_run[run_id].get(decision_id)
    outcome_record = outcome_records_by_run[run_id].get(decision_id)
    if source_record is None or outcome_record is None:
        raise JointExecutionProbabilityError("joint OOS decision is absent from verified source/outcome records")
    signal_session = source.signal_session
    if str(prediction["session_date"]) != signal_session:
        raise JointExecutionProbabilityError("joint OOS decision session does not bind its signal source")

    horizon = _selected_horizon(outcome_record)
    scenario = _selected_scenario(horizon)
    holding_path = _mapping(horizon["holding_path"], "outcome.holding_path")
    observed = _mapping(scenario["observed_outcome"], "outcome.observed_outcome")
    _verify_learning_prediction_bindings(
        prediction,
        learning,
        source_record,
        outcome_record,
        holding_path,
        scenario,
        observed,
    )
    return _V3CandidateContext(
        sample_id=sample_id,
        run_id=run_id,
        symbol=symbol,
        source=source,
        source_record=source_record,
        outcome_record=outcome_record,
        signal_session=signal_session,
        horizon=horizon,
        scenario=scenario,
        holding_path=holding_path,
        observed=observed,
    )


def _v3_fold_context(
    prediction: Mapping[str, object],
    signal_session: str,
    fold_by_id: Mapping[int, Mapping[str, object]],
) -> _V3FoldContext:
    fold_id = _positive_integer(prediction["fold_id"], "prediction.fold_id")
    fold = fold_by_id.get(fold_id)
    if fold is None:
        raise JointExecutionProbabilityError("joint OOS prediction fold is missing")
    split = _mapping(fold["split"], "fold.split")
    if signal_session not in cast(list[object], split["test_dates"]):
        raise JointExecutionProbabilityError("joint OOS prediction is outside its fold test sessions")
    component_artifacts = _component_artifact_index(fold)
    component_probabilities = _mapping(prediction["component_probabilities"], "prediction.component_probabilities")
    components = {name: _finite_float(component_probabilities[name], f"probability.{name}") for name in JOINT_EXECUTION_COMPONENTS}
    probability = _finite_float(prediction["probability"], "prediction.probability")
    component_digest = _digest({name: component_artifacts[name]["component_digest"] for name in JOINT_EXECUTION_COMPONENTS})
    if prediction.get("component_model_digest") != component_digest:
        raise JointExecutionProbabilityError("joint OOS component models do not bind their prediction")
    return _V3FoldContext(
        fold=fold,
        component_artifacts=component_artifacts,
        components=components,
        probability=probability,
        fold_digest=str(fold["fold_digest"]),
    )


def _v3_universe_definition_digest(
    source: VerifiedJointExecutionSourceCorpus,
) -> str:
    return _digest(
        {
            "version": "fixed-published-all-decisions-source-universe-v1",
            "source_artifact_digest": source.artifact_digest,
            "source_decision_identity_digest": source.decision_identity_digest,
            "source_feature_schema_digest": source.feature_schema_digest,
        }
    )


def _v3_candidate_evidence(
    context: _V3CandidateContext,
    fold_context: _V3FoldContext,
    identity_digest: str,
    universe_digest: str,
    feature_contract_digest: str,
    prediction: Mapping[str, object],
) -> dict[str, object]:
    decision_information_digest = _digest(
        {
            "source_record_digest": context.source_record["record_digest"],
            "feature_vector_digest": prediction["feature_vector_digest"],
            "source_snapshot_digest": context.source.source_snapshot_digest,
        }
    )
    calibrator_digest = _digest({name: fold_context.component_artifacts[name]["calibrator_digest"] for name in JOINT_EXECUTION_COMPONENTS})
    return {
        "entry_bar": context.horizon["entry_bar"],
        "exit_bar": context.horizon["exit_bar"],
        "entry_rules": context.horizon["entry_rules"],
        "exit_rules": context.horizon["exit_rules"],
        "entry_reference": context.horizon["entry_reference"],
        "exit_reference": context.horizon["exit_reference"],
        "participation": context.horizon["participation"],
        "benchmark": _v3_benchmark_evidence(context, identity_digest, universe_digest),
        "calibration": _v3_calibration_evidence(
            fold_context,
            calibrator_digest,
            feature_contract_digest,
            decision_information_digest,
            prediction_generated_at=context.source.decision_frozen_at,
        ),
    }


def _v3_decision_set(
    context: _V3CandidateContext,
    identity_digest: str,
    universe_digest: str,
) -> dict[str, object]:
    source = context.source
    return {
        "population_policy": ("all_fixed_full_market_decisions_including_unfilled_and_unexecutable"),
        "signal_session": context.signal_session,
        "horizon": JOINT_EXECUTION_HORIZON,
        "target": JOINT_EXECUTION_TARGET,
        "source_run_id": context.run_id,
        "expected_decision_count": len(source),
        "decision_identity_digest": identity_digest,
        "universe_definition_digest": universe_digest,
        "universe_membership_digest": source.decision_membership_digest,
        "source_snapshot_digest": source.source_snapshot_digest,
        "frozen_at": source.decision_frozen_at,
        "universe_frozen_before_outcomes": True,
    }


def _v3_benchmark_evidence(
    context: _V3CandidateContext,
    identity_digest: str,
    universe_digest: str,
) -> dict[str, object]:
    return {
        "universe_basis": "fixed_full_market_at_signal",
        "outcome_population": "all_decisions",
        "benchmark_method": "fixed_universe_leave_one_out",
        "universe_frozen_before_outcomes": True,
        "benchmark_predeclared": True,
        "subject_excluded": True,
        "universe_definition_digest": universe_digest,
        "universe_membership_digest": context.source.decision_membership_digest,
        "decision_cohort_digest": identity_digest,
        "benchmark_series_digest": context.scenario["benchmark_series_digest"],
    }


def _v3_calibration_evidence(
    fold_context: _V3FoldContext,
    calibrator_digest: str,
    feature_contract_digest: str,
    decision_information_digest: str,
    *,
    prediction_generated_at: str,
) -> dict[str, object]:
    artifacts = fold_context.component_artifacts
    return {
        "estimator_contract": "three_component_joint_chain",
        "training_cutoff": fold_context.fold["training_cutoff"],
        "prediction_generated_at": prediction_generated_at,
        "entry_model_digest": artifacts["entry_fill"]["model_digest"],
        "exit_model_digest": artifacts["exit_executable"]["model_digest"],
        "net_model_digest": artifacts["net_positive"]["model_digest"],
        "calibrator_digest": calibrator_digest,
        "feature_schema_digest": feature_contract_digest,
        "decision_information_digest": decision_information_digest,
        "out_of_sample_assessment_digest": fold_context.fold_digest,
        "out_of_sample_verified": True,
        "calibration_verified": True,
        "selection_qualified": True,
    }


def _v3_probability_payload(fold_context: _V3FoldContext) -> dict[str, object]:
    components = fold_context.components
    probability = fold_context.probability
    return {
        "entry_fill_probability": components["entry_fill"],
        "exit_executable_given_entry_probability": components["exit_executable"],
        "net_positive_given_entry_and_exit_probability": components["net_positive"],
        "joint_net_positive_probability": probability,
        "action_probability": probability,
    }


def _verify_learning_prediction_bindings(
    prediction: Mapping[str, object],
    learning: Mapping[str, object],
    source_record: Mapping[str, object],
    outcome_record: Mapping[str, object],
    holding_path: Mapping[str, object],
    scenario: Mapping[str, object],
    observed: Mapping[str, object],
) -> None:
    component_outcomes = _mapping(prediction["component_outcomes"], "prediction.component_outcomes")
    expected_components = {
        "entry_fill": int(cast(bool, observed["entry_fill"])),
        "exit_executable": (int(cast(bool, observed["exit_executable"])) if observed["exit_executable"] is not None else None),
        "net_positive": (int(cast(bool, observed["net_positive"])) if observed["net_positive"] is not None else None),
    }
    expected_learning = {
        "sample_id": prediction["sample_id"],
        "session_date": prediction["session_date"],
        "source_record_digest": source_record["record_digest"],
        "outcome_record_digest": outcome_record["record_digest"],
        "holding_path_digest": holding_path["path_digest"],
        "benchmark_series_digest": scenario["benchmark_series_digest"],
    }
    if any(learning.get(key) != value for key, value in expected_learning.items()):
        raise JointExecutionProbabilityError("joint OOS learning row does not bind official source/outcome evidence")
    if (
        prediction.get("source_record_digest") != source_record["record_digest"]
        or prediction.get("outcome_record_digest") != outcome_record["record_digest"]
        or prediction.get("holding_path_digest") != holding_path["path_digest"]
        or prediction.get("benchmark_series_digest") != scenario["benchmark_series_digest"]
        or prediction.get("feature_vector_digest") != _digest(_numeric_features(learning["features"]))
        or dict(component_outcomes) != expected_components
        or int(cast(int, prediction["outcome"])) != int(cast(bool, observed["joint_action_positive"]))
    ):
        raise JointExecutionProbabilityError("joint OOS prediction does not replay official labels/features")


def _component_artifact_index(
    fold: Mapping[str, object],
) -> dict[str, Mapping[str, object]]:
    raw = _mapping(fold["component_artifacts"], "fold.component_artifacts")
    if set(raw) != set(JOINT_EXECUTION_COMPONENTS):
        raise JointExecutionProbabilityError("joint OOS fold component artifacts are incomplete")
    output = {name: _mapping(raw[name], f"fold.component_artifacts.{name}") for name in JOINT_EXECUTION_COMPONENTS}
    for name, artifact in output.items():
        if artifact.get("component") != name:
            raise JointExecutionProbabilityError("joint OOS fold component identity mismatch")
    return output


def _joint_sample_identity(sample_id: str) -> tuple[int, str]:
    parts = sample_id.split(":")
    if len(parts) != 4 or not parts[0].isdigit() or int(parts[0]) <= 0 or parts[2] != str(JOINT_EXECUTION_HORIZON) or parts[3] != JOINT_EXECUTION_TARGET:
        raise JointExecutionProbabilityError("joint OOS sample identity does not match the registered estimand")
    symbol = parts[1]
    if len(symbol) != 9 or symbol[6:] not in {".SH", ".SZ", ".BJ"}:
        raise JointExecutionProbabilityError("joint OOS sample symbol is invalid")
    return int(parts[0]), symbol


def _unique_mapping_index(
    rows: Sequence[Mapping[str, object]],
    key: str,
    label: str,
) -> dict[str, Mapping[str, object]]:
    output: dict[str, Mapping[str, object]] = {}
    for row in rows:
        identity = str(row.get(key) or "")
        if not identity or identity in output:
            raise JointExecutionProbabilityError(f"{label} identity is empty or duplicated")
        output[identity] = row
    return output


def _positive_int_mapping_index(
    rows: Sequence[Mapping[str, object]],
    key: str,
    label: str,
) -> dict[int, Mapping[str, object]]:
    output: dict[int, Mapping[str, object]] = {}
    for row in rows:
        value = row.get(key)
        if isinstance(value, bool) or not isinstance(value, int) or value <= 0 or value in output:
            raise JointExecutionProbabilityError(f"{label} identity is invalid")
        output[value] = row
    return output


def _positive_integer(value: object, label: str) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or value <= 0:
        raise JointExecutionProbabilityError(f"{label} must be a positive integer")
    return value


def _validate_learning_rows(rows: Sequence[Mapping[str, object]]) -> None:
    if not rows:
        raise JointExecutionProbabilityError("joint learning rows are empty")
    identifiers = [str(item["sample_id"]) for item in rows]
    if len(set(identifiers)) != len(identifiers):
        raise JointExecutionProbabilityError("joint learning sample identity is duplicated")
    names: tuple[str, ...] | None = None
    for item in rows:
        features = _numeric_features(item["features"])
        current = tuple(sorted(features))
        names = current if names is None else names
        if current != names:
            raise JointExecutionProbabilityError("joint learning feature sets differ")
        _validate_learning_labels(item)


def _validate_learning_labels(item: Mapping[str, object]) -> None:
    entry = item["entry_fill"]
    exit_value = item["exit_executable"]
    net = item["net_positive"]
    joint = item["joint_action_positive"]
    if not isinstance(entry, bool) or not isinstance(joint, bool):
        raise JointExecutionProbabilityError("joint learning labels are not boolean")
    if entry is False and (exit_value is not None or net is not None or joint is True):
        raise JointExecutionProbabilityError("joint learning unfilled label shape is invalid")
    if entry is True and exit_value is False and (net is not None or joint is True):
        raise JointExecutionProbabilityError("joint learning unexecutable label shape is invalid")
    if entry is True and exit_value is True and (not isinstance(net, bool) or joint is not net):
        raise JointExecutionProbabilityError("joint learning net label shape is invalid")


def _row_from_mapping(value: Mapping[str, object]) -> _LearningRow:
    _validate_learning_rows([value])
    return _LearningRow(
        sample_id=str(value["sample_id"]),
        run_id=int(cast(int, value["run_id"])),
        session_date=str(value["session_date"]),
        symbol=str(value["symbol"]),
        features=_numeric_features(value["features"]),
        entry_fill=cast(bool, value["entry_fill"]),
        exit_executable=cast(bool | None, value["exit_executable"]),
        net_positive=cast(bool | None, value["net_positive"]),
        joint_action_positive=cast(bool, value["joint_action_positive"]),
        net_return=_optional_float(value.get("net_return"), "net_return"),
        net_excess_return=_optional_float(value.get("net_excess_return"), "net_excess_return"),
        observed_at=str(value["observed_at"]),
        source_record_digest=str(value["source_record_digest"]),
        outcome_record_digest=str(value["outcome_record_digest"]),
        holding_path_digest=str(value["holding_path_digest"]),
        benchmark_series_digest=str(value["benchmark_series_digest"]),
    )


def _fit_joint_rows(
    rows: Sequence[_LearningRow],
    *,
    corpus: VerifiedJointExecutionLearningCorpus,
    generated_at: str,
    config: _EstimatorConfig,
    formal_candidate: bool,
) -> dict[str, object]:
    readiness = _fit_readiness(rows, config, formal_candidate=formal_candidate)
    base = _evidence_base(
        corpus,
        generated_at=generated_at,
        config=config,
        formal_candidate=formal_candidate,
        dates=readiness.dates,
        row_count=len(rows),
        preregistration_predated_sessions=readiness.predated,
    )
    if readiness.reasons:
        return _insufficient_evidence(base, readiness.reasons, readiness.splits)
    fitted = _fit_oos_folds(rows, readiness.splits, corpus.feature_names, config)
    if fitted.failure_reasons:
        return _insufficient_evidence(base, fitted.failure_reasons, readiness.splits)
    return _completed_joint_evidence(
        base,
        rows,
        corpus,
        readiness,
        fitted,
        config,
        formal_candidate=formal_candidate,
    )


def _fit_readiness(
    rows: Sequence[_LearningRow],
    config: _EstimatorConfig,
    *,
    formal_candidate: bool,
) -> _FitReadiness:
    dates = tuple(sorted({item.session_date for item in rows}))
    splits = _splits(dates, config)
    preregistered = _timestamp(config.preregistered_at, "preregistered_at")
    predated = sorted({item.session_date for item in rows if _timestamp(item.observed_at, "observed_at") <= preregistered})
    reasons: list[str] = []
    if not rows:
        reasons.append("no_formal_joint_observations")
    if formal_candidate and predated:
        reasons.append("formal_candidate_outcomes_not_strictly_after_preregistration")
    if not splits:
        reasons.append("minimum_independent_sessions")
    return _FitReadiness(dates=dates, splits=splits, predated=predated, reasons=reasons)


def _fit_oos_folds(
    rows: Sequence[_LearningRow],
    splits: Sequence[_Split],
    feature_names: tuple[str, ...],
    config: _EstimatorConfig,
) -> _OosFoldFit:
    legacy_config = _legacy_config(config)
    folds: list[dict[str, object]] = []
    predictions: list[dict[str, object]] = []
    for fold_id, split in enumerate(splits, start=1):
        try:
            fold, fold_predictions = _fit_oos_fold(
                rows,
                split,
                feature_names,
                legacy_config,
                fold_id=fold_id,
            )
        except (
            _ComponentFitUnavailable,
            _probability.ProbabilityModelConvergenceError,
        ) as exc:
            return _OosFoldFit(
                folds=folds,
                predictions=predictions,
                failure_reasons=[f"fold_{fold_id}_{exc}"],
            )
        predictions.extend(fold_predictions)
        folds.append(fold)
    return _OosFoldFit(folds=folds, predictions=predictions, failure_reasons=[])


def _fit_oos_fold(
    rows: Sequence[_LearningRow],
    split: _Split,
    feature_names: tuple[str, ...],
    legacy_config: _probability.ProbabilityConfig,
    *,
    fold_id: int,
) -> tuple[dict[str, object], list[dict[str, object]]]:
    partitions = _partitions(rows, split)
    components = {
        component: _fit_component(
            partitions,
            feature_names,
            component=cast(
                Literal["entry_fill", "exit_executable", "net_positive"],
                component,
            ),
            config=legacy_config,
        )
        for component in JOINT_EXECUTION_COMPONENTS
    }
    calibration_rate = _smoothed_rate([int(item.joint_action_positive) for item in partitions["calibration"]])
    predictions = [
        _held_out_prediction(item, components, fold_id=fold_id, reference_base_rate=calibration_rate)
        for item in sorted(partitions["test"], key=lambda row: (row.session_date, row.sample_id))
    ]
    if len(predictions) != len(partitions["test"]):
        raise JointExecutionProbabilityError("joint component models were not applied to every held-out decision")
    payload: dict[str, object] = {
        "fold_id": fold_id,
        "split": split.payload(),
        "training_cutoff": split.train_dates[-1],
        "component_artifacts": components,
        "component_artifact_digest": _digest(components),
        "calibration_joint_base_rate": calibration_rate,
        "prediction_count": len(predictions),
        "held_out_decision_count": len(partitions["test"]),
        "all_components_applied_to_every_held_out_decision": True,
    }
    payload["fold_digest"] = _digest(payload)
    return payload, predictions


def _completed_joint_evidence(
    base: Mapping[str, object],
    rows: Sequence[_LearningRow],
    corpus: VerifiedJointExecutionLearningCorpus,
    readiness: _FitReadiness,
    fitted: _OosFoldFit,
    config: _EstimatorConfig,
    *,
    formal_candidate: bool,
) -> dict[str, object]:
    legacy_config = _legacy_config(config)
    folds, predictions = fitted.folds, fitted.predictions
    metrics = _probability.probability_prediction_metrics(
        predictions,
        legacy_config,
        corpus.corpus_digest,
    )
    selection = _selection_qualification(
        metrics,
        predictions,
        folds,
        config,
        formal_candidate=formal_candidate and not readiness.predated,
    )
    final_components = cast(dict[str, object], folds[-1]["component_artifacts"])
    evidence = dict(base)
    evidence.update(
        _completed_evidence_fields(
            rows,
            readiness,
            folds,
            predictions,
            metrics,
            selection,
            final_components,
            config,
        )
    )
    return _seal_evidence(evidence)


def _completed_evidence_fields(
    rows: Sequence[_LearningRow],
    readiness: _FitReadiness,
    folds: Sequence[Mapping[str, object]],
    predictions: Sequence[Mapping[str, object]],
    metrics: Mapping[str, object],
    selection: Mapping[str, object],
    final_components: Mapping[str, object],
    config: _EstimatorConfig,
) -> dict[str, object]:
    models = _final_component_values(final_components, "model")
    calibrators = _final_component_values(final_components, "calibrator")
    return {
        "status": "calibrated_shadow",
        "fit_status": "fitted_oos_three_component",
        "selection_qualified": selection["passed"],
        "selection_qualification": selection,
        "base_rate": _mean([float(cast(float, item["reference_base_rate"])) for item in predictions]),
        "actual_positive_rate_interval": cast(Mapping[str, object], metrics["calibrated"]).get("actual_positive_rate_ci_95"),
        "split": folds[-1]["split"],
        "counts": _counts(rows, readiness.dates, readiness.splits, predictions, config),
        "training_cutoff": folds[-1]["training_cutoff"],
        "model": {
            "version": JOINT_EXECUTION_MODEL_VERSION,
            "components": {
                **models,
            },
        },
        "calibrator": {
            "version": JOINT_EXECUTION_CALIBRATOR_VERSION,
            "components": {
                **calibrators,
            },
        },
        "calibration_metrics": metrics,
        "folds": folds,
        "predictions": predictions,
        "model_digest": _digest(models),
        "calibrator_digest": _digest(calibrators),
        "limitations": _selection_limitations(selection),
    }


def _final_component_values(
    components: Mapping[str, object],
    key: str,
) -> dict[str, object]:
    return {name: _mapping(components[name], f"final_components.{name}")[key] for name in JOINT_EXECUTION_COMPONENTS}


def _selection_limitations(selection: Mapping[str, object]) -> list[str]:
    limitations = [
        "shadow_only_no_production_ranking_effect",
        "filter_requires_separate_exact_authorization",
        "deployment_requires_fresh_three_component_refit",
    ]
    if selection["passed"] is not True:
        limitations.extend(f"selection_gate_failed:{name}" for name, passed in cast(Mapping[str, bool], selection["gates"]).items() if not passed)
    return limitations


class _ComponentFitUnavailable(ValueError):
    pass


def _joint_deployment_split(rows: Sequence[_LearningRow], config: _EstimatorConfig) -> dict[str, tuple[str, ...]]:
    dates = tuple(sorted({item.session_date for item in rows}))
    calibration_start = len(dates) - config.minimum_calibration_sessions
    train_end = calibration_start - config.gap_sessions
    if train_end < config.minimum_train_sessions or calibration_start >= len(dates):
        raise JointExecutionProbabilityError("joint deployment lacks a complete trailing calibration block")
    train = dates[:train_end]
    gap = dates[train_end:calibration_start]
    calibration = dates[calibration_start:]
    if len(gap) != config.gap_sessions or len(calibration) != config.minimum_calibration_sessions or train[-1] >= calibration[0]:
        raise JointExecutionProbabilityError("joint deployment split does not preserve its purge boundary")
    return {
        "train_dates": train,
        "gap_dates": gap,
        "calibration_dates": calibration,
    }


def _deployment_corpus_extends_selected_study(
    corpus: VerifiedJointExecutionLearningCorpus,
    evidence: Mapping[str, object],
) -> bool:
    """Allow only immutable OOS bindings followed by newer official sessions."""

    try:
        if not _deployment_schema_matches(corpus, evidence):
            return False
        selected = _selected_study_bindings(evidence)
        return _deployment_bindings_extend_selection(corpus, evidence, selected)
    except (KeyError, TypeError, ValueError):
        return False


def _deployment_schema_matches(
    corpus: VerifiedJointExecutionLearningCorpus,
    evidence: Mapping[str, object],
) -> bool:
    model = _mapping(evidence["model"], "study.model")
    components = _mapping(model["components"], "study.model.components")
    return bool(
        corpus.feature_version == evidence["feature_version"]
        and corpus.label_contract_digest == evidence["label_contract_digest"]
        and set(components) == set(JOINT_EXECUTION_COMPONENTS)
        and all(
            tuple(
                str(item)
                for item in cast(
                    list[object],
                    _mapping(
                        components[name],
                        f"study.model.components.{name}",
                    )["feature_names"],
                )
            )
            == corpus.feature_names
            for name in JOINT_EXECUTION_COMPONENTS
        )
    )


def _selected_study_bindings(evidence: Mapping[str, object]) -> list[dict[str, object]]:
    selected = [dict(_mapping(item, "study.source_bindings[]")) for item in cast(list[object], evidence["source_bindings"])]
    if not selected or evidence["source_binding_digest"] != _digest(selected):
        raise ValueError("selected study source bindings are invalid")
    return selected


def _deployment_bindings_extend_selection(
    corpus: VerifiedJointExecutionLearningCorpus,
    evidence: Mapping[str, object],
    selected: Sequence[Mapping[str, object]],
) -> bool:
    selected_by_run = {int(cast(int, item["run_id"])): item for item in selected}
    if len(selected_by_run) != len(selected):
        return False
    deployment_by_run = {int(cast(int, item["run_id"])): item for item in corpus.bindings}
    if any(deployment_by_run.get(run_id) != binding for run_id, binding in selected_by_run.items()):
        return False
    selected_last_session = max(str(item["signal_session"]) for item in selected)
    appended = [item for run_id, item in deployment_by_run.items() if run_id not in selected_by_run]
    if any(str(item["signal_session"]) <= selected_last_session for item in appended):
        return False
    return bool(appended or evidence["input_digest"] == corpus.corpus_digest)


def _joint_deployment_partitions(rows: Sequence[_LearningRow], split: Mapping[str, Sequence[str]]) -> dict[str, tuple[_LearningRow, ...]]:
    train_dates = frozenset(split["train_dates"])
    calibration_dates = frozenset(split["calibration_dates"])
    return {
        "train": tuple(item for item in rows if item.session_date in train_dates),
        "calibration": tuple(item for item in rows if item.session_date in calibration_dates),
        "test": (),
    }


def _joint_deployment_is_fresh(payload: Mapping[str, object], as_of: str | None) -> bool:
    try:
        reference = _timestamp(as_of, "deployment.as_of") if as_of is not None else _probability.utc_now().astimezone(_SHANGHAI)
        generated = _timestamp(str(payload["generated_at"]), "deployment.generated_at")
        latest = _timestamp(str(payload["latest_outcome_observed_at"]), "latest_outcome_observed_at")
        calibration_cutoff = datetime.fromisoformat(str(payload["calibration_cutoff"])).date()
    except (KeyError, TypeError, ValueError):
        return False
    maximum = timedelta(hours=JOINT_EXECUTION_DEPLOYMENT_MAXIMUM_AGE_HOURS)
    return bool(
        timedelta(0) <= reference - generated <= maximum
        and timedelta(0) <= reference - latest <= maximum
        and calibration_cutoff <= reference.date()
        and payload.get("oos_final_fold_reuse_forbidden") is True
    )


def _fit_component(
    partitions: Mapping[str, Sequence[_LearningRow]],
    feature_names: tuple[str, ...],
    *,
    component: Literal["entry_fill", "exit_executable", "net_positive"],
    config: _probability.ProbabilityConfig,
) -> dict[str, object]:
    selected: dict[str, list[_LearningRow]] = {
        name: [item for item in values if _component_label(item, component) is not None] for name, values in partitions.items()
    }
    for partition in ("train", "calibration"):
        partition_labels = {cast(int, _component_label(item, component)) for item in selected[partition]}
        if partition_labels != {0, 1}:
            raise _ComponentFitUnavailable(f"{component}_{partition}_class_diversity")
        if len({item.session_date for item in selected[partition]}) != len({item.session_date for item in partitions[partition]}):
            raise _ComponentFitUnavailable(f"{component}_{partition}_session_coverage")
    train_samples = _component_samples(selected["train"], component)
    calibration_samples = _component_samples(selected["calibration"], component)
    model = _probability.fit_probability_logistic_model(train_samples, feature_names, config)
    raw = [_probability.probability_model_probability(model, item.features) for item in calibration_samples]
    calibration_labels = [int(cast(int | bool, item.target)) for item in calibration_samples]
    calibrator = _probability.fit_probability_platt_calibrator(raw, calibration_labels, config)
    payload: dict[str, object] = {
        "component": component,
        "training_population": {
            "entry_fill": "all_fixed_decisions",
            "exit_executable": "observed_entry_fill_true",
            "net_positive": "observed_entry_and_exit_true",
        }[component],
        "held_out_application_population": "every_fixed_test_decision",
        "train_observation_count": len(train_samples),
        "train_session_count": len({item.session_date for item in selected["train"]}),
        "calibration_observation_count": len(calibration_samples),
        "calibration_session_count": len({item.session_date for item in selected["calibration"]}),
        "calibration_base_rate": sum(calibration_labels) / len(calibration_labels),
        "model": model,
        "model_digest": _digest(model),
        "calibrator": calibrator,
        "calibrator_digest": _digest(calibrator),
    }
    payload["component_digest"] = _digest(payload)
    return payload


def _component_samples(
    rows: Sequence[_LearningRow],
    component: Literal["entry_fill", "exit_executable", "net_positive"],
) -> tuple[_probability.ProbabilitySample, ...]:
    return tuple(
        _probability.ProbabilitySample(
            sample_id=f"{item.sample_id}:{component}",
            session_date=item.session_date,
            features=item.features,
            target=cast(int, _component_label(item, component)),
            executable=True,
        )
        for item in rows
    )


def _component_label(
    row: _LearningRow,
    component: Literal["entry_fill", "exit_executable", "net_positive"],
) -> int | None:
    if component == "entry_fill":
        return int(row.entry_fill)
    if component == "exit_executable":
        return int(cast(bool, row.exit_executable)) if row.entry_fill else None
    return int(cast(bool, row.net_positive)) if row.entry_fill and row.exit_executable is True else None


def _held_out_prediction(
    row: _LearningRow,
    components: Mapping[str, Mapping[str, object]],
    *,
    fold_id: int,
    reference_base_rate: float,
) -> dict[str, object]:
    raw: dict[str, float] = {}
    calibrated: dict[str, float] = {}
    for component in JOINT_EXECUTION_COMPONENTS:
        artifact = components[component]
        model = _mapping(artifact["model"], f"{component}.model")
        calibrator = _mapping(artifact["calibrator"], f"{component}.calibrator")
        raw[component] = _probability.probability_model_probability(model, row.features)
        calibrated[component] = _probability.probability_platt_probability(calibrator, raw[component])
    joint_raw = raw["entry_fill"] * raw["exit_executable"] * raw["net_positive"]
    joint = calibrated["entry_fill"] * calibrated["exit_executable"] * calibrated["net_positive"]
    return {
        "sample_id": row.sample_id,
        "session_date": row.session_date,
        "fold_id": fold_id,
        "outcome": int(row.joint_action_positive),
        "component_outcomes": {
            "entry_fill": int(row.entry_fill),
            "exit_executable": (int(row.exit_executable) if row.exit_executable is not None else None),
            "net_positive": int(row.net_positive) if row.net_positive is not None else None,
        },
        "raw_component_probabilities": raw,
        "component_probabilities": calibrated,
        "raw_probability": joint_raw,
        "probability": joint,
        "isotonic_probability": None,
        "reference_base_rate": reference_base_rate,
        "baseline_probability": reference_base_rate,
        "net_return": row.net_return,
        "net_excess_return": row.net_excess_return,
        "feature_vector_digest": _digest(row.features),
        "source_record_digest": row.source_record_digest,
        "outcome_record_digest": row.outcome_record_digest,
        "holding_path_digest": row.holding_path_digest,
        "benchmark_series_digest": row.benchmark_series_digest,
        "component_model_digest": _digest({name: components[name]["component_digest"] for name in JOINT_EXECUTION_COMPONENTS}),
    }


def _selection_qualification(
    metrics: Mapping[str, object],
    predictions: Sequence[Mapping[str, object]],
    folds: Sequence[Mapping[str, object]],
    config: _EstimatorConfig,
    *,
    formal_candidate: bool,
) -> dict[str, object]:
    calibrated = _mapping(metrics["calibrated"], "metrics.calibrated")
    stability = _mapping(metrics["fold_stability"], "metrics.fold_stability")
    bins = [_mapping(item, "calibration_bins[]") for item in cast(list[object], calibrated["calibration_bins"])]
    brier_skill = _optional_float(calibrated.get("brier_skill_score"), "brier_skill")
    brier_ci = _float_pair(calibrated.get("brier_improvement_vs_reference_ci_95"), "brier_ci")
    log_ci = _float_pair(calibrated.get("log_loss_improvement_vs_reference_ci_95"), "log_loss_ci")
    ece = cast(float, _optional_float(calibrated.get("ece"), "ece"))
    gates = {
        "formal_preregistered_candidate": formal_candidate,
        "complete_joint_label_contract_bound": True,
        "minimum_label_coverage": True,
        "all_component_models_applied_to_every_held_out_decision": all(
            item.get("all_components_applied_to_every_held_out_decision") is True and item.get("prediction_count") == item.get("held_out_decision_count")
            for item in folds
        ),
        "positive_oos_brier_skill": brier_skill is not None and brier_skill > 0,
        "effective_probability_stratification": bool(
            len(bins) >= 2
            and calibrated.get("bin_monotonic") is True
            and calibrated.get("highest_bin_above_base_rate") is True
            and all(int(cast(int, item["independent_session_count"])) >= config.minimum_bin_sessions for item in bins)
        ),
        "multiple_complete_oos_folds": len(folds) >= config.minimum_selection_folds,
        "positive_skill_in_every_complete_oos_fold": bool(
            stability.get("fold_count") == len(folds) and stability.get("all_folds_positive_brier_skill") is True
        ),
        "positive_brier_improvement_ci_95": brier_ci[0] > 0,
        "positive_log_loss_improvement_ci_95": log_ci[0] > 0,
        "ece_at_most_5pct": ece <= JOINT_EXECUTION_MAXIMUM_ECE,
    }
    return {
        "version": "joint-execution-statistical-selection-gates-v1",
        "passed": all(gates.values()),
        "gates": gates,
        "evaluated_complete_oos_folds": len(folds),
        "minimum_complete_oos_folds": config.minimum_selection_folds,
        "held_out_observation_count": len(predictions),
        "external_authorization_still_required": True,
    }


def _evidence_base(
    corpus: VerifiedJointExecutionLearningCorpus,
    *,
    generated_at: str,
    config: _EstimatorConfig,
    formal_candidate: bool,
    dates: Sequence[str],
    row_count: int,
    preregistration_predated_sessions: Sequence[str],
) -> dict[str, object]:
    contract = _contract(corpus, config, formal_candidate=formal_candidate)
    return {
        "schema_version": JOINT_EXECUTION_PROBABILITY_SCHEMA_VERSION,
        "status": "insufficient_data",
        "fit_status": "not_fitted",
        "selection_qualified": False,
        "selection_qualification": None,
        "probability": None,
        "horizon": config.horizon,
        "target_definition": "joint_execution_action_positive_net_excess",
        "base_rate": None,
        "actual_positive_rate_interval": None,
        "model_version": JOINT_EXECUTION_MODEL_VERSION,
        "feature_version": corpus.feature_version,
        "label_version": JOINT_EXECUTION_LABEL_VERSION,
        "cost_model_version": "cn-a-share-cost-model.v2",
        "label_contract_digest": corpus.label_contract_digest,
        "label_contract_binding": "complete_verified_official_outcome_token",
        "generated_at": generated_at,
        "input_digest": corpus.corpus_digest,
        "contract": contract,
        "source_bindings": corpus.bindings,
        "source_binding_digest": _digest(corpus.bindings),
        "preregistration_predated_sessions": list(preregistration_predated_sessions),
        "split": None,
        "counts": {
            "available_independent_session_count": len(dates),
            "observation_count": row_count,
            "eligible_observation_count": row_count,
            "label_coverage": 1.0 if row_count else 0.0,
            "minimum_fit_independent_sessions": config.minimum_fit_sessions,
            "minimum_selection_independent_sessions": config.minimum_selection_sessions,
            "walk_forward_fold_count": 0,
            "evaluated_fold_count": 0,
            "out_of_sample_session_count": 0,
            "out_of_sample_observation_count": 0,
        },
        "training_cutoff": None,
        "model": None,
        "calibrator": None,
        "calibration_metrics": None,
        "folds": [],
        "predictions": [],
        "model_digest": None,
        "calibrator_digest": None,
        "limitations": ["shadow_only_no_production_ranking_effect"],
    }


def _insufficient_evidence(
    base: Mapping[str, object],
    reasons: Sequence[str],
    splits: Sequence[_Split],
) -> dict[str, object]:
    evidence = deepcopy(dict(base))
    counts = cast(dict[str, object], evidence["counts"])
    counts["walk_forward_fold_count"] = len(splits)
    evidence["limitations"] = list(
        dict.fromkeys(
            [
                *cast(list[str], evidence["limitations"]),
                *reasons,
                "filter_and_production_ranking_fail_closed",
            ]
        )
    )
    return _seal_evidence(evidence)


def _contract(
    corpus: VerifiedJointExecutionLearningCorpus,
    config: _EstimatorConfig,
    *,
    formal_candidate: bool,
) -> dict[str, object]:
    preregistration = joint_execution_preregistration_contract()
    return {
        "schema_version": JOINT_EXECUTION_PROBABILITY_SCHEMA_VERSION,
        "candidate_id": JOINT_EXECUTION_CANDIDATE_ID,
        "formal_candidate": formal_candidate,
        "feature": {
            "version": corpus.feature_version,
            "names": list(corpus.feature_names),
            "contract_digest": corpus.feature_contract_digest,
            "completed_session_D_only": True,
        },
        "label": {
            "version": JOINT_EXECUTION_LABEL_VERSION,
            "target": "joint_execution_action_positive",
            "net_component_target": JOINT_EXECUTION_TARGET,
            "target_population": ("all_fixed_full_market_decisions_including_unfilled_and_unexecutable"),
            "observed_components": list(JOINT_EXECUTION_COMPONENTS),
            "selection_probability": "joint_execution_action_probability",
            "label_contract_digest": corpus.label_contract_digest,
        },
        "model": cast(dict[str, object], preregistration["model"]),
        "calibrator": cast(dict[str, object], preregistration["calibrator"]),
        "split": _split_contract(config),
        "evaluation": _evaluation_contract(config),
        "preregistration": preregistration,
        "production_effect": "none",
        "automatic_promotion": False,
    }


def _split_contract(config: _EstimatorConfig) -> dict[str, object]:
    return {
        "version": JOINT_EXECUTION_SPLIT_VERSION,
        "group": "signal_session",
        "walk_forward": "expanding_train_rolling_calibration_and_test",
        "random_split_forbidden": True,
        "minimum_train_sessions": config.minimum_train_sessions,
        "minimum_calibration_sessions": config.minimum_calibration_sessions,
        "minimum_test_sessions": config.minimum_test_sessions,
        "gap_sessions": config.gap_sessions,
        "target_session_offset": config.horizon + 1,
        "purge_rule": ("all_prior_labels_mature_strictly_before_next_partition_signal"),
        "minimum_fit_independent_sessions": config.minimum_fit_sessions,
        "minimum_selection_independent_sessions": config.minimum_selection_sessions,
    }


def _evaluation_contract(config: _EstimatorConfig) -> dict[str, object]:
    return {
        "minimum_label_coverage": config.minimum_label_coverage,
        "minimum_bin_sessions": config.minimum_bin_sessions,
        "calibration_bin_count": config.calibration_bin_count,
        "minimum_selection_folds": config.minimum_selection_folds,
        "bootstrap_samples": config.bootstrap_samples,
        "bootstrap": "deterministic_circular_moving_target_offset_block_95pct_v2",
        "bootstrap_block_length_sessions": config.horizon + 1,
        "requires_positive_oos_brier_skill": True,
        "requires_positive_skill_each_fold": True,
        "requires_monotonic_probability_bins": True,
        "requires_positive_brier_and_log_loss_ci_lower_bounds": True,
        "maximum_ece": JOINT_EXECUTION_MAXIMUM_ECE,
        "external_bh_fdr_drift_execution_authorization_required": True,
    }


def _legacy_config(config: _EstimatorConfig) -> _probability.ProbabilityConfig:
    return _probability.ProbabilityConfig(
        horizon=config.horizon,
        target="net_excess_positive",
        minimum_train_sessions=config.minimum_train_sessions,
        minimum_calibration_sessions=config.minimum_calibration_sessions,
        minimum_test_sessions=config.minimum_test_sessions,
        minimum_label_coverage=config.minimum_label_coverage,
        minimum_bin_sessions=config.minimum_bin_sessions,
        minimum_selection_folds=config.minimum_selection_folds,
        gap_sessions=config.gap_sessions,
        calibration_bin_count=config.calibration_bin_count,
        bootstrap_samples=config.bootstrap_samples,
        l2_strength=config.l2_strength,
        maximum_iterations=config.maximum_iterations,
        convergence_tolerance=config.convergence_tolerance,
    )


def _splits(dates: Sequence[str], config: _EstimatorConfig) -> tuple[_Split, ...]:
    canonical = tuple(sorted(set(dates)))
    required = config.minimum_fit_sessions
    if len(canonical) < required:
        return ()
    endpoints = list(range(required, len(canonical) + 1, config.minimum_test_sessions))
    output: list[_Split] = []
    for endpoint in endpoints:
        test_start = endpoint - config.minimum_test_sessions
        second_gap_start = test_start - config.gap_sessions
        calibration_start = second_gap_start - config.minimum_calibration_sessions
        first_gap_start = calibration_start - config.gap_sessions
        output.append(
            _Split(
                train_dates=canonical[:first_gap_start],
                train_gap_dates=canonical[first_gap_start:calibration_start],
                calibration_dates=canonical[calibration_start:second_gap_start],
                calibration_gap_dates=canonical[second_gap_start:test_start],
                test_dates=canonical[test_start:endpoint],
            )
        )
    return tuple(output)


def _partitions(rows: Sequence[_LearningRow], split: _Split) -> dict[str, tuple[_LearningRow, ...]]:
    date_sets = {
        "train": frozenset(split.train_dates),
        "calibration": frozenset(split.calibration_dates),
        "test": frozenset(split.test_dates),
    }
    return {name: tuple(item for item in rows if item.session_date in dates) for name, dates in date_sets.items()}


def _counts(
    rows: Sequence[_LearningRow],
    dates: Sequence[str],
    splits: Sequence[_Split],
    predictions: Sequence[Mapping[str, object]],
    config: _EstimatorConfig,
) -> dict[str, object]:
    return {
        "available_independent_session_count": len(dates),
        "observation_count": len(rows),
        "eligible_observation_count": len(rows),
        "label_coverage": 1.0,
        "minimum_fit_independent_sessions": config.minimum_fit_sessions,
        "minimum_selection_independent_sessions": config.minimum_selection_sessions,
        "walk_forward_fold_count": len(splits),
        "evaluated_fold_count": len(splits),
        "out_of_sample_session_count": len({str(item["session_date"]) for item in predictions}),
        "out_of_sample_observation_count": len(predictions),
    }


def _verify_joint_evidence_structure(evidence: Mapping[str, object]) -> None:
    _verify_joint_contract(evidence)
    predictions = evidence["predictions"]
    folds = evidence["folds"]
    if not isinstance(predictions, list) or not isinstance(folds, list):
        raise ValueError("joint probability predictions/folds are invalid")
    if evidence["status"] == "insufficient_data":
        if predictions or folds or evidence["model"] is not None:
            raise ValueError("insufficient joint study cannot carry fitted artifacts")
        return
    _verify_fitted_joint_predictions(evidence, predictions, folds)


def _verify_joint_contract(evidence: Mapping[str, object]) -> None:
    contract = _mapping(evidence["contract"], "contract")
    label = _mapping(contract["label"], "contract.label")
    if (
        label.get("version") != JOINT_EXECUTION_LABEL_VERSION
        or label.get("target") != "joint_execution_action_positive"
        or label.get("observed_components") != list(JOINT_EXECUTION_COMPONENTS)
        or label.get("selection_probability") != "joint_execution_action_probability"
    ):
        raise ValueError("joint probability label contract mismatch")
    if evidence["probability"] is not None:
        raise ValueError("joint probability study cannot expose one current probability")


def _verify_fitted_joint_predictions(
    evidence: Mapping[str, object],
    predictions: Sequence[object],
    folds: Sequence[object],
) -> None:
    if evidence["status"] != "calibrated_shadow" or not predictions or not folds:
        raise ValueError("joint probability fitted status is inconsistent")
    for prediction in predictions:
        row = _mapping(prediction, "prediction")
        components = _mapping(row["component_probabilities"], "components")
        expected = 1.0
        for name in JOINT_EXECUTION_COMPONENTS:
            expected *= _finite_float(components[name], f"components.{name}")
        if abs(expected - _finite_float(row["probability"], "probability")) > 1e-12:
            raise ValueError("joint probability is not the component product")
    if any(
        item.get("prediction_count") != item.get("held_out_decision_count") or item.get("all_components_applied_to_every_held_out_decision") is not True
        for item in (_mapping(value, "fold") for value in folds)
    ):
        raise ValueError("joint component held-out application is incomplete")


def _evidence_keys() -> set[str]:
    return {
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
        "source_bindings",
        "source_binding_digest",
        "preregistration_predated_sessions",
        "split",
        "counts",
        "training_cutoff",
        "model",
        "calibrator",
        "calibration_metrics",
        "folds",
        "predictions",
        "model_digest",
        "calibrator_digest",
        "limitations",
        "evidence_digest",
    }


def _seal_evidence(value: Mapping[str, object]) -> dict[str, object]:
    payload = deepcopy(dict(value))
    payload.pop("evidence_digest", None)
    payload["evidence_digest"] = _digest(payload)
    return payload


def _numeric_features(value: object) -> dict[str, float]:
    mapping = _mapping(value, "features")
    output = {str(name): _finite_float(raw, f"features.{name}") for name, raw in mapping.items()}
    if not output or list(output) != sorted(output):
        raise JointExecutionProbabilityError("joint learning features are not canonical")
    return output


def _optional_float(value: object, label: str) -> float | None:
    return None if value is None else _finite_float(value, label)


def _finite_float(value: object, label: str) -> float:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise JointExecutionProbabilityError(f"{label} must be numeric")
    output = float(value)
    if not isfinite(output):
        raise JointExecutionProbabilityError(f"{label} must be finite")
    return output


def _float_pair(value: object, label: str) -> tuple[float, float]:
    if not isinstance(value, list) or len(value) != 2:
        raise JointExecutionProbabilityError(f"{label} must be a two-number interval")
    return _finite_float(value[0], label), _finite_float(value[1], label)


def _smoothed_rate(values: Sequence[int]) -> float:
    if not values:
        raise JointExecutionProbabilityError("joint calibration labels are empty")
    return (sum(values) + 0.5) / (len(values) + 1.0)


def _mean(values: Sequence[float]) -> float:
    if not values:
        raise JointExecutionProbabilityError("joint probability mean is empty")
    return sum(values) / len(values)


def _mapping(value: object, label: str) -> Mapping[str, object]:
    if not isinstance(value, Mapping):
        raise JointExecutionProbabilityError(f"{label} must be an object")
    return cast(Mapping[str, object], value)


def _timestamp(value: str, label: str) -> datetime:
    try:
        parsed = datetime.fromisoformat(value)
    except (TypeError, ValueError) as exc:
        raise ValueError(f"{label} must be an ISO timestamp") from exc
    if parsed.tzinfo is None or parsed.utcoffset() is None:
        raise ValueError(f"{label} must include a timezone offset")
    return parsed.astimezone(_SHANGHAI)


def _digest(value: object) -> str:
    return sha256_hex(canonical_json_bytes(value))


__all__ = [
    "JOINT_EXECUTION_CANDIDATE_ID",
    "JOINT_EXECUTION_CURRENT_PREDICTION_SCHEMA_VERSION",
    "JOINT_EXECUTION_DEPLOYMENT_ARTIFACT_SCHEMA_VERSION",
    "JOINT_EXECUTION_DEPLOYMENT_MAXIMUM_AGE_HOURS",
    "JOINT_EXECUTION_HORIZON",
    "JOINT_EXECUTION_MINIMUM_SELECTION_SESSIONS",
    "JOINT_EXECUTION_PREREGISTERED_AT",
    "JOINT_EXECUTION_PROBABILITY_SCHEMA_VERSION",
    "JointExecutionProbabilityError",
    "VerifiedJointExecutionLearningCorpus",
    "VerifiedJointExecutionCurrentPredictionCorpus",
    "VerifiedJointExecutionDeploymentEstimator",
    "VerifiedJointExecutionProbabilityStudy",
    "build_joint_execution_learning_corpus",
    "build_joint_execution_current_prediction_artifact",
    "build_joint_execution_probability_oos_corpus_v3",
    "fit_joint_execution_deployment_estimator",
    "fit_joint_execution_probability",
    "joint_execution_preregistration_contract",
    "joint_execution_deployment_is_fresh",
    "predict_joint_execution_probability",
    "replay_and_verify_joint_execution_probability_evidence",
    "seal_joint_execution_deployment_artifact",
    "seal_joint_execution_current_prediction_artifact",
    "verify_joint_execution_current_prediction_artifact",
    "verify_joint_execution_deployment_artifact",
    "verify_joint_execution_probability_evidence",
]
