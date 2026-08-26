"""Immutable probability-aware v6 ranking contracts.

The current v5 score and every historical rank remain the source publication.
This module can only produce a separate v6 derived publication from opaque,
strictly replayed joint-execution probability tokens.  A selected probability
study is still insufficient: a preregistered shadow comparison and an
out-of-band pinned human promotion control must both pass first.
"""

from __future__ import annotations

from collections import Counter, defaultdict
from collections.abc import Iterator, Mapping, Sequence
from copy import deepcopy
from dataclasses import dataclass, field
from datetime import datetime
from itertools import combinations
import json
from math import erf, isclose, isfinite, sqrt
from random import Random
from statistics import mean, pstdev
from typing import Literal, cast, overload
from zoneinfo import ZoneInfo

from app.artifacts.io import canonical_json_bytes, sha256_hex
from app.services.joint_execution_probability_v3 import (
    VerifiedJointExecutionProbabilityCorpusV3,
)
from app.services.market_scan_joint_execution_probability import (
    JOINT_EXECUTION_HORIZON,
    VerifiedJointExecutionCurrentPredictionCorpus,
    VerifiedJointExecutionDeploymentEstimator,
    VerifiedJointExecutionProbabilityStudy,
)
from app.services.market_scan_joint_execution_source import (
    VerifiedJointExecutionSourceCorpus,
)
from app.services.market_scan_scoring import FULL_MARKET_SCORE_RULE_VERSION


PROBABILITY_RANKING_SCORE_RULE_VERSION = "full-market-score-v6"
PROBABILITY_RANKING_SCORE_SPEC_SCHEMA_VERSION = 6
PROBABILITY_RANKING_ALGORITHM_VERSION = (
    "v5-plus-bounded-centered-joint-h5-probability-v1"
)
PROBABILITY_RANKING_PREREGISTERED_AT = "2026-08-22T22:32:47+08:00"
PROBABILITY_RANKING_PROBABILITY_SCALE = 20.0
PROBABILITY_RANKING_MAXIMUM_ADJUSTMENT = 6.0
PROBABILITY_RANKING_TOP_N = 100
PROBABILITY_RANKING_MINIMUM_SHADOW_SESSIONS = 60
PROBABILITY_RANKING_DRIFT_WINDOW_SESSIONS = 30
PROBABILITY_RANKING_BOOTSTRAP_SAMPLES = 1_000
PROBABILITY_RANKING_SHADOW_SCHEMA_VERSION = (
    "market-scan-probability-ranking-shadow-artifact-v1"
)
PROBABILITY_RANKING_MANUAL_CONTROL_SCHEMA_VERSION = (
    "market-scan-probability-ranking-manual-control-artifact-v1"
)
PROBABILITY_RANKING_PUBLICATION_SCHEMA_VERSION = (
    "market-scan-probability-ranking-publication-artifact-v1"
)
_SHANGHAI = ZoneInfo("Asia/Shanghai")
_SHADOW_SEAL = object()
_CONTROL_SEAL = object()
_PUBLICATION_SEAL = object()


class ProbabilityRankingError(ValueError):
    """Raised when a v6 ranking authority or replay contract is invalid."""


class VerifiedProbabilityRankingShadowEvaluation:
    """Opaque result of a full preregistered v5-versus-v6 OOS replay."""

    __slots__ = ("_encoded", "integrity_digest", "qualified")

    def __init__(
        self,
        encoded: str,
        *,
        integrity_digest: str,
        qualified: bool,
        _seal: object | None = None,
    ) -> None:
        if _seal is not _SHADOW_SEAL:
            raise TypeError("v6 ranking shadow token can only be created by strict replay")
        self._encoded = encoded
        self.integrity_digest = integrity_digest
        self.qualified = qualified

    @property
    def payload(self) -> dict[str, object]:
        value = json.loads(self._encoded)
        if not isinstance(value, dict):  # pragma: no cover - sealed invariant
            raise TypeError("verified ranking shadow payload is invalid")
        return cast(dict[str, object], value)


class VerifiedProbabilityRankingManualControl:
    """Opaque out-of-band-pinned human promotion or rollback decision."""

    __slots__ = (
        "_encoded",
        "action",
        "effective_after_run_id",
        "generated_at",
        "integrity_digest",
    )

    def __init__(
        self,
        encoded: str,
        *,
        action: Literal["promote", "rollback"],
        effective_after_run_id: int,
        generated_at: str,
        integrity_digest: str,
        _seal: object | None = None,
    ) -> None:
        if _seal is not _CONTROL_SEAL:
            raise TypeError("v6 ranking manual control requires strict pinned verification")
        self._encoded = encoded
        self.action = action
        self.effective_after_run_id = effective_after_run_id
        self.generated_at = generated_at
        self.integrity_digest = integrity_digest

    @property
    def payload(self) -> dict[str, object]:
        value = json.loads(self._encoded)
        if not isinstance(value, dict):  # pragma: no cover - sealed invariant
            raise TypeError("verified ranking control payload is invalid")
        return cast(dict[str, object], value)


class VerifiedProbabilityRankingPublication(Sequence[Mapping[str, object]]):
    """Opaque immutable v6 publication for one new official base run."""

    __slots__ = (
        "_encoded_payload",
        "artifact_digest",
        "base_snapshot_digest",
        "generated_at",
        "promotion_digest",
        "run_id",
        "score_spec_hash",
    )

    def __init__(
        self,
        encoded_payload: str,
        *,
        artifact_digest: str,
        base_snapshot_digest: str,
        generated_at: str,
        promotion_digest: str,
        run_id: int,
        score_spec_hash: str,
        _seal: object | None = None,
    ) -> None:
        if _seal is not _PUBLICATION_SEAL:
            raise TypeError("v6 ranking publication can only be created by strict replay")
        self._encoded_payload = encoded_payload
        self.artifact_digest = artifact_digest
        self.base_snapshot_digest = base_snapshot_digest
        self.generated_at = generated_at
        self.promotion_digest = promotion_digest
        self.run_id = run_id
        self.score_spec_hash = score_spec_hash

    @property
    def payload(self) -> dict[str, object]:
        value = json.loads(self._encoded_payload)
        if not isinstance(value, dict):  # pragma: no cover - sealed invariant
            raise TypeError("verified ranking publication payload is invalid")
        return cast(dict[str, object], value)

    @property
    def records(self) -> list[dict[str, object]]:
        value = self.payload.get("records")
        if not isinstance(value, list):  # pragma: no cover - sealed invariant
            raise TypeError("verified ranking publication records are invalid")
        return [cast(dict[str, object], item) for item in value]

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

    def __iter__(self) -> Iterator[Mapping[str, object]]:
        return iter(self.records)

    def record_by_symbol(self) -> dict[str, dict[str, object]]:
        return {str(item["symbol"]): item for item in self.records}


@dataclass(frozen=True)
class _ShadowDecision:
    sample_id: str
    run_id: int
    session: str
    symbol: str
    base_raw_score: float
    probability: float
    reference_base_rate: float
    adjustment: float
    v6_raw_score: float
    net_excess_return: float
    unresolved_exit: bool
    capacity_exceeded: bool
    categories: Mapping[str, str]


@dataclass(frozen=True)
class _ShadowSessionEvaluation:
    summary: dict[str, object]
    v5_return: float
    v6_return: float
    overlap: float
    v5_symbols: set[str]
    v6_symbols: set[str]
    unresolved: int
    capacity_failures: int
    v5_exposure: dict[str, float]
    v6_exposure: dict[str, float]
    complete_top_n: bool


@dataclass
class _ShadowAnalysisState:
    summaries: list[dict[str, object]] = field(default_factory=list)
    v5_returns: list[float] = field(default_factory=list)
    v6_returns: list[float] = field(default_factory=list)
    overlaps: list[float] = field(default_factory=list)
    v5_turnover: list[float] = field(default_factory=list)
    v6_turnover: list[float] = field(default_factory=list)
    previous_v5: set[str] | None = None
    previous_v6: set[str] | None = None
    unresolved: int = 0
    capacity_failures: int = 0
    top_observations: int = 0
    complete_top_n: bool = True
    exposure_maxima: dict[str, float] = field(
        default_factory=lambda: {"market": 0.0, "liquidity": 0.0, "industry_bucket": 0.0}
    )
    exposure_lifts: dict[str, float] = field(
        default_factory=lambda: {"market": 0.0, "liquidity": 0.0, "industry_bucket": 0.0}
    )

    def add(self, evaluation: _ShadowSessionEvaluation) -> None:
        size = int(cast(int, evaluation.summary["top_n"]))
        if self.previous_v5 is not None:
            self.v5_turnover.append(
                1 - len(self.previous_v5 & evaluation.v5_symbols) / size
            )
            self.v6_turnover.append(
                1 - len(cast(set[str], self.previous_v6) & evaluation.v6_symbols) / size
            )
        self.previous_v5 = evaluation.v5_symbols
        self.previous_v6 = evaluation.v6_symbols
        self.summaries.append(evaluation.summary)
        self.v5_returns.append(evaluation.v5_return)
        self.v6_returns.append(evaluation.v6_return)
        self.overlaps.append(evaluation.overlap)
        self.unresolved += evaluation.unresolved
        self.capacity_failures += evaluation.capacity_failures
        self.top_observations += size
        self.complete_top_n = self.complete_top_n and evaluation.complete_top_n
        for name in self.exposure_maxima:
            self.exposure_maxima[name] = max(
                self.exposure_maxima[name], evaluation.v6_exposure[name]
            )
            self.exposure_lifts[name] = max(
                self.exposure_lifts[name],
                evaluation.v6_exposure[name] - evaluation.v5_exposure[name],
            )


@dataclass(frozen=True)
class _ShadowStatistics:
    deltas: list[float]
    delta_ci: tuple[float, float]
    pbo: float | None
    dsr: float | None
    first: list[float]
    last: list[float]
    v5_drawdown: float
    v6_drawdown: float
    average_v5_turnover: float
    average_v6_turnover: float
    capacity_coverage: float


def probability_ranking_score_spec() -> dict[str, object]:
    """Return the exact immutable v6 score/rank contract."""

    return {
        "schema_version": PROBABILITY_RANKING_SCORE_SPEC_SCHEMA_VERSION,
        "rule_version": PROBABILITY_RANKING_SCORE_RULE_VERSION,
        "algorithm_version": PROBABILITY_RANKING_ALGORITHM_VERSION,
        "preregistered_at": PROBABILITY_RANKING_PREREGISTERED_AT,
        "base_score_contract": {
            "rule_version": FULL_MARKET_SCORE_RULE_VERSION,
            "immutability": "read_only_source_publication",
            "historical_rewrite": "forbidden",
        },
        "probability_input": {
            "horizon": JOINT_EXECUTION_HORIZON,
            "target": "joint_execution_action_positive_net_excess",
            "authority": "verified_current_all_decisions_probability_token",
            "decision_time_only": True,
            "missing_success_probability_policy": "fail_entire_v6_publication",
        },
        "transform": {
            "formula": "clip((probability-reference_base_rate)*20,-6,6)",
            "scale": PROBABILITY_RANKING_PROBABILITY_SCALE,
            "minimum_adjustment": -PROBABILITY_RANKING_MAXIMUM_ADJUSTMENT,
            "maximum_adjustment": PROBABILITY_RANKING_MAXIMUM_ADJUSTMENT,
        },
        "final_score": {
            "raw_formula": "clip(v5_raw_score+probability_adjustment,0,100)",
            "raw_decimals": 6,
            "integer_formula": "python_round_half_even(v6_raw_score)",
            "clamp": [0, 100],
        },
        "ranking": {
            "tie_break": [["raw_score", "desc"], ["symbol", "asc"]],
            "population": "v5_success_rows_only_exactly_once",
        },
        "promotion": {
            "shadow_required": True,
            "explicit_human_control_required": True,
            "automatic_promotion": False,
            "only_runs_strictly_after_effective_after_run_id": True,
            "rollback_control_supported": True,
        },
    }


def probability_ranking_score_spec_hash() -> str:
    return _digest(probability_ranking_score_spec())


def probability_ranking_adjustment(probability: float, base_rate: float) -> float:
    probability = _probability(probability, "probability")
    base_rate = _probability(base_rate, "reference_base_rate")
    return round(
        min(
            PROBABILITY_RANKING_MAXIMUM_ADJUSTMENT,
            max(
                -PROBABILITY_RANKING_MAXIMUM_ADJUSTMENT,
                (probability - base_rate) * PROBABILITY_RANKING_PROBABILITY_SCALE,
            ),
        ),
        6,
    )


def probability_ranking_raw_score(
    base_raw_score: float,
    probability: float,
    base_rate: float,
) -> tuple[float, float, int]:
    base = _score(base_raw_score, "base_raw_score")
    adjustment = probability_ranking_adjustment(probability, base_rate)
    raw = round(min(100.0, max(0.0, base + adjustment)), 6)
    return adjustment, raw, min(100, max(0, round(raw)))


def build_probability_ranking_shadow_artifact(
    oos_corpus: VerifiedJointExecutionProbabilityCorpusV3,
    sources: Sequence[VerifiedJointExecutionSourceCorpus],
    study: VerifiedJointExecutionProbabilityStudy,
    *,
    generated_at: str,
) -> dict[str, object]:
    """Build the compact preregistered v5-versus-v6 OOS comparison."""

    _require_shadow_tokens(oos_corpus, sources, study)
    generated = _timestamp(generated_at, "shadow.generated_at")
    evidence = study.payload
    if generated < _timestamp(str(evidence["generated_at"]), "study.generated_at"):
        raise ProbabilityRankingError("ranking shadow predates selected probability study")
    decisions = _shadow_decisions(oos_corpus, sources, study)
    analysis = _shadow_analysis(decisions)
    source_run_ids = sorted({item.run_id for item in decisions})
    payload: dict[str, object] = {
        "contract_version": "probability-ranking-preregistered-shadow-v1",
        "generated_at": generated.isoformat(),
        "score_rule_version": PROBABILITY_RANKING_SCORE_RULE_VERSION,
        "score_spec_hash": probability_ranking_score_spec_hash(),
        "score_spec": probability_ranking_score_spec(),
        "study_evidence_digest": study.evidence_digest,
        "oos_corpus_digest": oos_corpus.integrity_digest,
        "source_run_ids": source_run_ids,
        "source_binding_digest": _digest(
            [
                {
                    "run_id": item.run_id,
                    "signal_session": item.signal_session,
                    "source_artifact_digest": item.artifact_digest,
                    "source_snapshot_digest": item.source_snapshot_digest,
                }
                for item in sorted(sources, key=lambda value: value.run_id)
                if item.run_id in source_run_ids
            ]
        ),
        "decision_count": len(decisions),
        **analysis,
        "automatic_promotion": False,
        "production_effect": "none_until_manual_promotion",
    }
    return _seal_envelope(
        PROBABILITY_RANKING_SHADOW_SCHEMA_VERSION,
        payload,
        generated_at=generated.isoformat(),
    )


def verify_probability_ranking_shadow_artifact(
    artifact: Mapping[str, object],
    *,
    oos_corpus: VerifiedJointExecutionProbabilityCorpusV3,
    sources: Sequence[VerifiedJointExecutionSourceCorpus],
    study: VerifiedJointExecutionProbabilityStudy,
) -> VerifiedProbabilityRankingShadowEvaluation:
    payload, digest = _verify_envelope(
        artifact,
        schema_version=PROBABILITY_RANKING_SHADOW_SCHEMA_VERSION,
    )
    rebuilt = build_probability_ranking_shadow_artifact(
        oos_corpus,
        sources,
        study,
        generated_at=str(artifact["generated_at"]),
    )
    if rebuilt != dict(artifact):
        raise ProbabilityRankingError("v6 ranking shadow does not replay")
    qualified = payload.get("qualified") is True
    return VerifiedProbabilityRankingShadowEvaluation(
        canonical_json_bytes(payload).decode("utf-8"),
        integrity_digest=digest,
        qualified=qualified,
        _seal=_SHADOW_SEAL,
    )


def seal_probability_ranking_manual_control_artifact(
    payload: Mapping[str, object],
    *,
    generated_at: str,
) -> dict[str, object]:
    """Content-address a human-authored control; this does not authorize it."""

    return _seal_envelope(
        PROBABILITY_RANKING_MANUAL_CONTROL_SCHEMA_VERSION,
        payload,
        generated_at=generated_at,
    )


def verify_probability_ranking_manual_control_artifact(
    artifact: Mapping[str, object],
    *,
    expected_digest: str,
    shadow: VerifiedProbabilityRankingShadowEvaluation | None,
) -> VerifiedProbabilityRankingManualControl:
    """Verify an externally pinned explicit promotion or rollback decision."""

    payload, digest = _verify_envelope(
        artifact,
        schema_version=PROBABILITY_RANKING_MANUAL_CONTROL_SCHEMA_VERSION,
    )
    if digest != expected_digest:
        raise ProbabilityRankingError("manual ranking control does not match pinned digest")
    action = _manual_control_action(payload)
    _validate_manual_control_schema(payload, action)
    effective, generated = _validate_manual_control_common(payload, artifact)
    if action == "promote":
        _validate_manual_promotion(payload, shadow, effective, generated)
    else:
        _validate_manual_rollback(payload)
    return VerifiedProbabilityRankingManualControl(
        canonical_json_bytes(payload).decode("utf-8"),
        action=action,
        effective_after_run_id=effective,
        generated_at=generated.isoformat(),
        integrity_digest=digest,
        _seal=_CONTROL_SEAL,
    )


def _manual_control_action(
    payload: Mapping[str, object],
) -> Literal["promote", "rollback"]:
    action = str(payload.get("action") or "")
    if action not in {"promote", "rollback"}:
        raise ProbabilityRankingError("manual ranking control action is invalid")
    return cast(Literal["promote", "rollback"], action)


def _validate_manual_control_schema(
    payload: Mapping[str, object],
    action: Literal["promote", "rollback"],
) -> None:
    common = {
        "contract_version",
        "action",
        "generated_at",
        "reviewer_id",
        "reviewer_role",
        "change_ticket",
        "effective_after_run_id",
        "automatic",
        "reason",
        "rollback_acknowledged",
    }
    expected = (
        common
        | {
            "shadow_artifact_digest",
            "study_evidence_digest",
            "score_rule_version",
            "score_spec_hash",
        }
        if action == "promote"
        else common | {"promotion_digest", "publication_artifact_digest"}
    )
    if set(payload) != expected:
        raise ProbabilityRankingError("manual ranking control exact schema mismatch")


def _validate_manual_control_common(
    payload: Mapping[str, object],
    artifact: Mapping[str, object],
) -> tuple[int, datetime]:
    if (
        payload.get("contract_version")
        != "probability-ranking-explicit-human-control-v1"
        or payload.get("automatic") is not False
        or payload.get("rollback_acknowledged") is not True
    ):
        raise ProbabilityRankingError("manual ranking control safety contract is invalid")
    for name in ("reviewer_id", "reviewer_role", "change_ticket", "reason"):
        _nonempty_text(payload.get(name), f"control.{name}")
    effective = _positive_int(payload.get("effective_after_run_id"), "effective_after_run_id")
    generated = _timestamp(str(payload.get("generated_at")), "control.generated_at")
    if generated.isoformat() != artifact.get("generated_at"):
        raise ProbabilityRankingError("manual ranking control timestamp mismatch")
    return effective, generated


def _validate_manual_promotion(
    payload: Mapping[str, object],
    shadow: VerifiedProbabilityRankingShadowEvaluation | None,
    effective: int,
    generated: datetime,
) -> None:
    if shadow is None or not shadow.qualified:
        raise ProbabilityRankingError("manual promotion requires a qualified shadow token")
    shadow_payload = shadow.payload
    if (
        payload.get("shadow_artifact_digest") != shadow.integrity_digest
        or payload.get("study_evidence_digest") != shadow_payload.get("study_evidence_digest")
        or payload.get("score_rule_version") != PROBABILITY_RANKING_SCORE_RULE_VERSION
        or payload.get("score_spec_hash") != probability_ranking_score_spec_hash()
        or generated <= _timestamp(str(shadow_payload["generated_at"]), "shadow.generated_at")
        or effective < max(cast(list[int], shadow_payload["source_run_ids"]))
    ):
        raise ProbabilityRankingError("manual promotion does not bind qualified shadow evidence")


def _validate_manual_rollback(payload: Mapping[str, object]) -> None:
    _digest_text(payload.get("promotion_digest"), "control.promotion_digest")
    publication = payload.get("publication_artifact_digest")
    if publication is not None:
        _digest_text(publication, "control.publication_artifact_digest")


def build_probability_ranking_publication_artifact(
    source: VerifiedJointExecutionSourceCorpus,
    predictions: VerifiedJointExecutionCurrentPredictionCorpus,
    study: VerifiedJointExecutionProbabilityStudy,
    deployment: VerifiedJointExecutionDeploymentEstimator,
    promotion: VerifiedProbabilityRankingManualControl,
    *,
    generated_at: str,
) -> dict[str, object]:
    """Build v6 rows without mutating their v5 source publication."""

    _require_publication_tokens(source, predictions, study, deployment, promotion)
    generated = _timestamp(generated_at, "ranking_publication.generated_at")
    _validate_publication_timing(source, predictions, promotion, generated)
    candidates = _publication_candidates(source, predictions)
    records = _rank_publication_candidates(candidates, source.run_id)
    payload = _ranking_publication_payload(
        source,
        predictions,
        study,
        deployment,
        promotion,
        generated,
        records,
    )
    return _seal_envelope(
        PROBABILITY_RANKING_PUBLICATION_SCHEMA_VERSION,
        payload,
        generated_at=generated.isoformat(),
    )


def _validate_publication_timing(
    source: VerifiedJointExecutionSourceCorpus,
    predictions: VerifiedJointExecutionCurrentPredictionCorpus,
    promotion: VerifiedProbabilityRankingManualControl,
    generated: datetime,
) -> None:
    if (
        source.run_id <= promotion.effective_after_run_id
        or generated < _timestamp(predictions.generated_at, "predictions.generated_at")
        or generated < _timestamp(promotion.generated_at, "promotion.generated_at")
    ):
        raise ProbabilityRankingError(
            "v6 ranking publication must be a new run after promotion and prediction"
        )


def _publication_candidates(
    source: VerifiedJointExecutionSourceCorpus,
    predictions: VerifiedJointExecutionCurrentPredictionCorpus,
) -> list[dict[str, object]]:
    source_by_symbol = {str(item["symbol"]): item for item in source.records}
    prediction_by_symbol = {str(item["symbol"]): item for item in predictions.records}
    if set(source_by_symbol) != set(prediction_by_symbol):
        raise ProbabilityRankingError("v6 ranking prediction/source decision sets differ")
    candidates: list[dict[str, object]] = []
    for symbol, record in sorted(source_by_symbol.items()):
        if record.get("result_status") != "success":
            continue
        prediction = prediction_by_symbol[symbol]
        features = _mapping(record.get("features"), "source.features")
        base_raw = _score(features.get("raw_score"), "source.raw_score")
        base_score = _integer_score(
            features.get("final_score_score"),
            "source.final_score_score",
        )
        probability = _probability(prediction.get("probability"), "prediction.probability")
        base_rate = _probability(
            prediction.get("reference_base_rate"),
            "prediction.reference_base_rate",
        )
        adjustment, raw_score, score = probability_ranking_raw_score(
            base_raw,
            probability,
            base_rate,
        )
        candidates.append(
            {
                "symbol": symbol,
                "base_score": base_score,
                "base_raw_score": base_raw,
                "probability": probability,
                "reference_base_rate": base_rate,
                "probability_adjustment": adjustment,
                "score": score,
                "raw_score": raw_score,
                "source_record_digest": record["record_digest"],
                "prediction_record_digest": prediction["record_digest"],
            }
        )
    if not candidates:
        raise ProbabilityRankingError("v6 ranking has no successful base rows")
    return candidates


def _rank_publication_candidates(
    candidates: Sequence[Mapping[str, object]],
    run_id: int,
) -> list[dict[str, object]]:
    base_order = sorted(
        candidates,
        key=lambda item: (
            -_finite_float(item["base_raw_score"], "candidate.base_raw_score"),
            str(item["symbol"]),
        ),
    )
    base_rank = {str(item["symbol"]): index for index, item in enumerate(base_order, 1)}
    v6_order = sorted(
        candidates,
        key=lambda item: (
            -_finite_float(item["raw_score"], "candidate.raw_score"),
            str(item["symbol"]),
        ),
    )
    records: list[dict[str, object]] = []
    for rank, item in enumerate(v6_order, 1):
        row = {
            **item,
            "run_id": run_id,
            "base_rank": base_rank[str(item["symbol"])],
            "rank": rank,
            "score_rule_version": PROBABILITY_RANKING_SCORE_RULE_VERSION,
            "score_spec_hash": probability_ranking_score_spec_hash(),
        }
        row["record_digest"] = _digest(row)
        records.append(row)
    return records


def _ranking_publication_payload(
    source: VerifiedJointExecutionSourceCorpus,
    predictions: VerifiedJointExecutionCurrentPredictionCorpus,
    study: VerifiedJointExecutionProbabilityStudy,
    deployment: VerifiedJointExecutionDeploymentEstimator,
    promotion: VerifiedProbabilityRankingManualControl,
    generated: datetime,
    records: Sequence[Mapping[str, object]],
) -> dict[str, object]:
    return {
        "contract_version": "derived-immutable-v6-production-ranking-v1",
        "generated_at": generated.isoformat(),
        "run_id": source.run_id,
        "signal_session": source.signal_session,
        "score_rule_version": PROBABILITY_RANKING_SCORE_RULE_VERSION,
        "score_spec_hash": probability_ranking_score_spec_hash(),
        "score_spec": probability_ranking_score_spec(),
        "base_score_rule_version": FULL_MARKET_SCORE_RULE_VERSION,
        "base_snapshot_digest": source.source_snapshot_digest,
        "joint_source_artifact_digest": source.artifact_digest,
        "current_prediction_artifact_digest": predictions.artifact_digest,
        "study_evidence_digest": study.evidence_digest,
        "deployment_artifact_digest": deployment.integrity_digest,
        "promotion_digest": promotion.integrity_digest,
        "effective_after_run_id": promotion.effective_after_run_id,
        "records": records,
        "record_count": len(records),
        "record_set_digest": _digest([item["record_digest"] for item in records]),
        "base_v5_mutated": False,
        "historical_ranks_mutated": False,
        "rollback_control_required_to_disable": True,
    }


def verify_probability_ranking_publication_artifact(
    artifact: Mapping[str, object],
    *,
    source: VerifiedJointExecutionSourceCorpus,
    predictions: VerifiedJointExecutionCurrentPredictionCorpus,
    study: VerifiedJointExecutionProbabilityStudy,
    deployment: VerifiedJointExecutionDeploymentEstimator,
    promotion: VerifiedProbabilityRankingManualControl,
) -> VerifiedProbabilityRankingPublication:
    payload, digest = _verify_envelope(
        artifact,
        schema_version=PROBABILITY_RANKING_PUBLICATION_SCHEMA_VERSION,
    )
    rebuilt = build_probability_ranking_publication_artifact(
        source,
        predictions,
        study,
        deployment,
        promotion,
        generated_at=str(artifact["generated_at"]),
    )
    if rebuilt != dict(artifact):
        raise ProbabilityRankingError("v6 ranking publication does not replay")
    return VerifiedProbabilityRankingPublication(
        canonical_json_bytes(payload).decode("utf-8"),
        artifact_digest=digest,
        base_snapshot_digest=str(payload["base_snapshot_digest"]),
        generated_at=str(payload["generated_at"]),
        promotion_digest=str(payload["promotion_digest"]),
        run_id=int(cast(int, payload["run_id"])),
        score_spec_hash=str(payload["score_spec_hash"]),
        _seal=_PUBLICATION_SEAL,
    )


def _require_shadow_tokens(
    oos_corpus: object,
    sources: Sequence[VerifiedJointExecutionSourceCorpus],
    study: object,
) -> None:
    if not isinstance(oos_corpus, VerifiedJointExecutionProbabilityCorpusV3):
        raise ProbabilityRankingError("ranking shadow requires verified OOS v3 corpus")
    if not isinstance(study, VerifiedJointExecutionProbabilityStudy):
        raise ProbabilityRankingError("ranking shadow requires verified selected study")
    if not sources or any(
        not isinstance(item, VerifiedJointExecutionSourceCorpus) for item in sources
    ):
        raise ProbabilityRankingError("ranking shadow requires verified source tokens")
    evidence = study.payload
    if evidence.get("selection_qualified") is not True:
        raise ProbabilityRankingError("ranking shadow requires selected probability evidence")


def _shadow_decisions(
    oos_corpus: VerifiedJointExecutionProbabilityCorpusV3,
    sources: Sequence[VerifiedJointExecutionSourceCorpus],
    study: VerifiedJointExecutionProbabilityStudy,
) -> tuple[_ShadowDecision, ...]:
    source_by_run = _unique_sources(sources)
    predictions = {
        str(item["sample_id"]): item
        for item in cast(list[dict[str, object]], study.payload["predictions"])
    }
    output: list[_ShadowDecision] = []
    seen: set[str] = set()
    for report in oos_corpus.reports:
        decision = _shadow_decision(report, predictions, source_by_run, seen)
        if decision is not None:
            output.append(decision)
    if not output:
        raise ProbabilityRankingError("ranking shadow has no successful OOS decisions")
    output.sort(key=lambda item: (item.session, item.symbol))
    return tuple(output)


def _shadow_decision(
    report: Mapping[str, object],
    predictions: Mapping[str, Mapping[str, object]],
    source_by_run: Mapping[int, VerifiedJointExecutionSourceCorpus],
    seen: set[str],
) -> _ShadowDecision | None:
    sample_id, run_id, symbol, source_record = _shadow_report_source(
        report, predictions, source_by_run, seen
    )
    if source_record.get("result_status") != "success":
        return None
    prediction = predictions[sample_id]
    probability, base_rate, base_raw, adjustment, v6_raw = _shadow_scores(
        report,
        prediction,
        source_record,
    )
    net_excess, unresolved = _shadow_outcome(report)
    entry_state = _mapping(report.get("entry_state"), "report.entry_state")
    features = _mapping(source_record.get("features"), "source.features")
    return _ShadowDecision(
        sample_id=sample_id,
        run_id=run_id,
        session=str(report["signal_session"]),
        symbol=symbol,
        base_raw_score=base_raw,
        probability=probability,
        reference_base_rate=base_rate,
        adjustment=adjustment,
        v6_raw_score=v6_raw,
        net_excess_return=net_excess,
        unresolved_exit=unresolved,
        capacity_exceeded=entry_state.get("execution_state") == "capacity_exceeded",
        categories=_ranking_categories(features),
    )


def _shadow_report_source(
    report: Mapping[str, object],
    predictions: Mapping[str, Mapping[str, object]],
    source_by_run: Mapping[int, VerifiedJointExecutionSourceCorpus],
    seen: set[str],
) -> tuple[str, int, str, Mapping[str, object]]:
    sample_id = str(report["sample_id"])
    if sample_id in seen or sample_id not in predictions:
        raise ProbabilityRankingError("ranking shadow OOS identity is incomplete")
    seen.add(sample_id)
    decision_set = _mapping(report.get("decision_set"), "report.decision_set")
    run_id = _positive_int(decision_set.get("source_run_id"), "source_run_id")
    source = source_by_run.get(run_id)
    if source is None or decision_set.get("source_snapshot_digest") != source.source_snapshot_digest:
        raise ProbabilityRankingError("ranking shadow source snapshot does not bind OOS report")
    symbol = str(report["symbol"])
    source_record = next((item for item in source.records if item.get("symbol") == symbol), None)
    if source_record is None:
        raise ProbabilityRankingError("ranking shadow source decision is missing")
    return sample_id, run_id, symbol, source_record


def _shadow_scores(
    report: Mapping[str, object],
    prediction: Mapping[str, object],
    source_record: Mapping[str, object],
) -> tuple[float, float, float, float, float]:
    report_probabilities = _mapping(report.get("probabilities"), "report.probabilities")
    probability = _probability(prediction.get("probability"), "prediction.probability")
    action_probability = _probability(
        report_probabilities.get("action_probability"), "action_probability"
    )
    if not isclose(probability, action_probability, rel_tol=0, abs_tol=1e-12):
        raise ProbabilityRankingError("ranking shadow report/prediction probability differs")
    base_rate = _probability(
        prediction.get("reference_base_rate"), "prediction.reference_base_rate"
    )
    features = _mapping(source_record.get("features"), "source.features")
    base_raw = _score(features.get("raw_score"), "source.raw_score")
    adjustment, v6_raw, _score_value = probability_ranking_raw_score(
        base_raw, probability, base_rate
    )
    return probability, base_rate, base_raw, adjustment, v6_raw


def _shadow_outcome(report: Mapping[str, object]) -> tuple[float, bool]:
    outcome = _mapping(report.get("observed_outcome"), "report.outcome")
    unresolved = outcome.get("entry_fill") is True and outcome.get("exit_executable") is not True
    raw_return = outcome.get("net_excess_return")
    net_excess = 0.0 if raw_return is None else _finite_float(raw_return, "outcome.net_excess_return")
    if net_excess <= -1:
        raise ProbabilityRankingError("ranking shadow return is outside wealth domain")
    return net_excess, unresolved


def _shadow_analysis(decisions: Sequence[_ShadowDecision]) -> dict[str, object]:
    grouped: dict[str, list[_ShadowDecision]] = defaultdict(list)
    for item in decisions:
        grouped[item.session].append(item)
    sessions = sorted(grouped)
    state = _ShadowAnalysisState()
    for session in sessions:
        state.add(_shadow_session_evaluation(session, grouped[session]))
    statistics = _shadow_statistics(state, sessions, decisions)
    gates = _shadow_gates(state, statistics, sessions)
    return {
        "qualified": all(gates.values()),
        "gates": gates,
        "failed_gates": sorted(name for name, passed in gates.items() if not passed),
        "metrics": _shadow_metrics(state, statistics, sessions, decisions),
        "session_summaries": state.summaries,
        "session_summary_digest": _digest(state.summaries),
    }


def _shadow_session_evaluation(
    session: str,
    rows: Sequence[_ShadowDecision],
) -> _ShadowSessionEvaluation:
    size = min(PROBABILITY_RANKING_TOP_N, len(rows))
    v5 = sorted(rows, key=lambda item: (-item.base_raw_score, item.symbol))[:size]
    v6 = sorted(rows, key=lambda item: (-item.v6_raw_score, item.symbol))[:size]
    v5_symbols = {item.symbol for item in v5}
    v6_symbols = {item.symbol for item in v6}
    overlap = len(v5_symbols & v6_symbols) / size
    v5_return = mean(item.net_excess_return for item in v5)
    v6_return = mean(item.net_excess_return for item in v6)
    unresolved = sum(item.unresolved_exit for item in v6)
    capacity_failures = sum(item.capacity_exceeded for item in v6)
    v5_exposure, v6_exposure = _exposure(v5), _exposure(v6)
    summary = {
        "signal_session": session,
        "success_decision_count": len(rows),
        "top_n": size,
        "v5_top_symbol_digest": _digest(sorted(v5_symbols)),
        "v6_top_symbol_digest": _digest(sorted(v6_symbols)),
        "top_overlap": overlap,
        "v5_net_excess_return": v5_return,
        "v6_net_excess_return": v6_return,
        "net_excess_delta": v6_return - v5_return,
        "v6_unresolved_exit_count": unresolved,
        "v6_capacity_exceeded_count": capacity_failures,
        "v5_exposure": v5_exposure,
        "v6_exposure": v6_exposure,
    }
    return _ShadowSessionEvaluation(
        summary=summary,
        v5_return=v5_return,
        v6_return=v6_return,
        overlap=overlap,
        v5_symbols=v5_symbols,
        v6_symbols=v6_symbols,
        unresolved=unresolved,
        capacity_failures=capacity_failures,
        v5_exposure=v5_exposure,
        v6_exposure=v6_exposure,
        complete_top_n=len(rows) >= PROBABILITY_RANKING_TOP_N,
    )


def _shadow_statistics(
    state: _ShadowAnalysisState,
    sessions: Sequence[str],
    decisions: Sequence[_ShadowDecision],
) -> _ShadowStatistics:
    deltas = [
        v6 - v5 for v5, v6 in zip(state.v5_returns, state.v6_returns, strict=True)
    ]
    delta_ci = _block_bootstrap_mean_ci(
        sessions,
        deltas,
        seed=_digest([[item.session, item.sample_id] for item in decisions]),
    )
    return _ShadowStatistics(
        deltas=deltas,
        delta_ci=delta_ci,
        pbo=_probability_of_backtest_overfitting(state.v5_returns, state.v6_returns),
        dsr=_deflated_sharpe_probability(deltas),
        first=deltas[:PROBABILITY_RANKING_DRIFT_WINDOW_SESSIONS],
        last=deltas[-PROBABILITY_RANKING_DRIFT_WINDOW_SESSIONS:],
        v5_drawdown=_maximum_drawdown(state.v5_returns),
        v6_drawdown=_maximum_drawdown(state.v6_returns),
        average_v5_turnover=mean(state.v5_turnover) if state.v5_turnover else 1.0,
        average_v6_turnover=mean(state.v6_turnover) if state.v6_turnover else 1.0,
        capacity_coverage=(
            1 - state.capacity_failures / state.top_observations
            if state.top_observations
            else 0.0
        ),
    )


def _shadow_gates(
    state: _ShadowAnalysisState,
    statistics: _ShadowStatistics,
    sessions: Sequence[str],
) -> dict[str, bool]:
    return {
        "minimum_60_independent_shadow_sessions": len(sessions)
        >= PROBABILITY_RANKING_MINIMUM_SHADOW_SESSIONS,
        "complete_top100_each_session": state.complete_top_n,
        "preregistration_predates_every_shadow_session": all(
            session > PROBABILITY_RANKING_PREREGISTERED_AT[:10]
            for session in sessions
        ),
        "mean_costed_net_excess_delta_positive": mean(statistics.deltas) > 0,
        "costed_net_excess_delta_ci95_lower_positive": statistics.delta_ci[0] > 0,
        "first_30_session_drift_window_positive": len(statistics.first)
        >= PROBABILITY_RANKING_DRIFT_WINDOW_SESSIONS
        and mean(statistics.first) > 0,
        "last_30_session_drift_window_positive": len(statistics.last)
        >= PROBABILITY_RANKING_DRIFT_WINDOW_SESSIONS
        and mean(statistics.last) > 0,
        "maximum_drawdown_not_materially_worse": statistics.v6_drawdown <= 0.20
        and statistics.v6_drawdown <= statistics.v5_drawdown + 0.02,
        "turnover_within_preregistered_budget": statistics.average_v6_turnover <= 0.80
        and statistics.average_v6_turnover <= statistics.average_v5_turnover * 1.20 + 0.05,
        "capacity_coverage_at_least_95pct": statistics.capacity_coverage >= 0.95,
        "no_unresolved_filled_exit_in_v6_top100": state.unresolved == 0,
        "market_exposure_within_70pct": state.exposure_maxima["market"] <= 0.70,
        "liquidity_exposure_within_35pct": state.exposure_maxima["liquidity"] <= 0.35,
        "industry_bucket_exposure_within_35pct": state.exposure_maxima["industry_bucket"]
        <= 0.35,
        "exposure_lift_within_10pct": max(state.exposure_lifts.values()) <= 0.10,
        "average_top100_overlap_at_least_70pct": mean(state.overlaps) >= 0.70,
        "pbo_at_most_20pct": statistics.pbo is not None and statistics.pbo <= 0.20,
        "deflated_sharpe_probability_at_least_95pct": statistics.dsr is not None
        and statistics.dsr >= 0.95,
    }


def _shadow_metrics(
    state: _ShadowAnalysisState,
    statistics: _ShadowStatistics,
    sessions: Sequence[str],
    decisions: Sequence[_ShadowDecision],
) -> dict[str, object]:
    return {
            "independent_session_count": len(sessions),
            "decision_count": len(decisions),
            "top_n": PROBABILITY_RANKING_TOP_N,
            "mean_v5_costed_net_excess_return": mean(state.v5_returns),
            "mean_v6_costed_net_excess_return": mean(state.v6_returns),
            "mean_costed_net_excess_delta": mean(statistics.deltas),
            "costed_net_excess_delta_ci95": list(statistics.delta_ci),
            "v5_maximum_drawdown": statistics.v5_drawdown,
            "v6_maximum_drawdown": statistics.v6_drawdown,
            "v5_average_turnover": statistics.average_v5_turnover,
            "v6_average_turnover": statistics.average_v6_turnover,
            "capacity_coverage": statistics.capacity_coverage,
            "unresolved_filled_exit_count": state.unresolved,
            "average_top_overlap": mean(state.overlaps),
            "maximum_exposure": state.exposure_maxima,
            "maximum_exposure_lift": state.exposure_lifts,
            "probability_of_backtest_overfitting": statistics.pbo,
            "deflated_sharpe_probability": statistics.dsr,
            "first_30_mean_delta": mean(statistics.first) if statistics.first else None,
            "last_30_mean_delta": mean(statistics.last) if statistics.last else None,
    }


def _require_publication_tokens(
    source: object,
    predictions: object,
    study: object,
    deployment: object,
    promotion: object,
) -> None:
    if not isinstance(source, VerifiedJointExecutionSourceCorpus):
        raise ProbabilityRankingError("v6 publication requires verified source token")
    if not isinstance(predictions, VerifiedJointExecutionCurrentPredictionCorpus):
        raise ProbabilityRankingError("v6 publication requires current prediction token")
    if not isinstance(study, VerifiedJointExecutionProbabilityStudy):
        raise ProbabilityRankingError("v6 publication requires selected study token")
    if not isinstance(deployment, VerifiedJointExecutionDeploymentEstimator):
        raise ProbabilityRankingError("v6 publication requires deployment token")
    if not isinstance(promotion, VerifiedProbabilityRankingManualControl):
        raise ProbabilityRankingError("v6 publication requires manual control token")
    if promotion.action != "promote":
        raise ProbabilityRankingError("rollback control cannot authorize v6 publication")
    deployment_payload = deployment.payload
    if (
        source.run_id != predictions.run_id
        or source.artifact_digest != predictions.source_artifact_digest
        or source.source_snapshot_digest != predictions.source_snapshot_digest
        or predictions.deployment_artifact_digest != deployment.integrity_digest
        or deployment_payload.get("study_evidence_digest") != study.evidence_digest
        or promotion.payload.get("study_evidence_digest") != study.evidence_digest
        or promotion.payload.get("score_spec_hash")
        != probability_ranking_score_spec_hash()
    ):
        raise ProbabilityRankingError("v6 publication authority bindings differ")


def _unique_sources(
    sources: Sequence[VerifiedJointExecutionSourceCorpus],
) -> dict[int, VerifiedJointExecutionSourceCorpus]:
    output: dict[int, VerifiedJointExecutionSourceCorpus] = {}
    for source in sources:
        if source.run_id in output:
            raise ProbabilityRankingError("ranking shadow source run is duplicated")
        output[source.run_id] = source
    return output


def _ranking_categories(features: Mapping[str, object]) -> dict[str, str]:
    output: dict[str, str] = {}
    for group, prefix in (
        ("market", "market_"),
        ("liquidity", "liquidity_"),
        ("industry_bucket", "industry_bucket_"),
    ):
        names = sorted(
            name
            for name, value in features.items()
            if name.startswith(prefix)
            and isclose(_finite_float(value, f"features.{name}"), 1.0, rel_tol=0, abs_tol=1e-12)
        )
        if len(names) != 1:
            raise ProbabilityRankingError(f"ranking feature category {group} is not one-hot")
        output[group] = names[0]
    return output


def _exposure(rows: Sequence[_ShadowDecision]) -> dict[str, float]:
    if not rows:
        return {"market": 0.0, "liquidity": 0.0, "industry_bucket": 0.0}
    return {
        group: max(Counter(item.categories[group] for item in rows).values()) / len(rows)
        for group in ("market", "liquidity", "industry_bucket")
    }


def _block_bootstrap_mean_ci(
    sessions: Sequence[str],
    values: Sequence[float],
    *,
    seed: str,
) -> tuple[float, float]:
    if len(sessions) != len(values) or not values:
        raise ProbabilityRankingError("ranking bootstrap session/value contract is invalid")
    block = JOINT_EXECUTION_HORIZON + 1
    rng = Random(int(seed[:16], 16))
    samples: list[float] = []
    for _ in range(PROBABILITY_RANKING_BOOTSTRAP_SAMPLES):
        selected: list[float] = []
        while len(selected) < len(values):
            start = rng.randrange(len(values))
            selected.extend(values[(start + offset) % len(values)] for offset in range(block))
        samples.append(mean(selected[: len(values)]))
    samples.sort()
    lower = samples[int(0.025 * len(samples))]
    upper = samples[min(len(samples) - 1, int(0.975 * len(samples)))]
    return lower, upper


def _probability_of_backtest_overfitting(
    v5: Sequence[float],
    v6: Sequence[float],
) -> float | None:
    if len(v5) != len(v6) or len(v5) < 16:
        return None
    block_count = 8
    blocks = [list(range(index, len(v5), block_count)) for index in range(block_count)]
    selected_cases = 0
    overfit_cases = 0
    for chosen in combinations(range(block_count), block_count // 2):
        overfit = _pbo_case_overfits(chosen, blocks, v5, v6)
        if overfit is None:
            continue
        selected_cases += 1
        if overfit:
            overfit_cases += 1
    return 1.0 if not selected_cases else overfit_cases / selected_cases


def _pbo_case_overfits(
    chosen: Sequence[int],
    blocks: Sequence[Sequence[int]],
    v5: Sequence[float],
    v6: Sequence[float],
) -> bool | None:
    selected = frozenset(chosen)
    train_indexes = [index for block_index in chosen for index in blocks[block_index]]
    train_delta = mean(v6[index] - v5[index] for index in train_indexes)
    if train_delta <= 0:
        return None
    test_indexes = [
        index
        for block_index in range(len(blocks))
        if block_index not in selected
        for index in blocks[block_index]
    ]
    return mean(v6[index] - v5[index] for index in test_indexes) <= 0


def _deflated_sharpe_probability(values: Sequence[float]) -> float | None:
    if len(values) < PROBABILITY_RANKING_MINIMUM_SHADOW_SESSIONS:
        return None
    sigma = pstdev(values)
    if sigma <= 0:
        return None
    sample_mean = mean(values)
    centered = [(item - sample_mean) / sigma for item in values]
    skew = mean(item**3 for item in centered)
    kurtosis = mean(item**4 for item in centered)
    sharpe = sample_mean / sigma
    denominator = sqrt(
        max(
            1e-12,
            (1 - skew * sharpe + ((kurtosis - 1) / 4) * sharpe**2)
            / (len(values) - 1),
        )
    )
    z = sharpe / denominator
    return 0.5 * (1 + erf(z / sqrt(2)))


def _maximum_drawdown(values: Sequence[float]) -> float:
    wealth = 1.0
    peak = 1.0
    maximum = 0.0
    for value in values:
        wealth *= 1 + value
        peak = max(peak, wealth)
        maximum = max(maximum, 1 - wealth / peak)
    return maximum


def _seal_envelope(
    schema_version: str,
    payload: Mapping[str, object],
    *,
    generated_at: str,
) -> dict[str, object]:
    generated = _timestamp(generated_at, "artifact.generated_at").isoformat()
    normalized = deepcopy(dict(payload))
    if normalized.get("generated_at") != generated:
        raise ProbabilityRankingError("ranking artifact payload/envelope timestamp differs")
    identity = {"generated_at": generated, "payload": normalized}
    return {
        "schema_version": schema_version,
        "generated_at": generated,
        "payload": normalized,
        "integrity": {
            "algorithm": "sha256",
            "scope": "generated_at+payload",
            "notice": "content_address_only_strict_replay_required",
            "integrity_digest": _digest(identity),
        },
    }


def _verify_envelope(
    artifact: Mapping[str, object],
    *,
    schema_version: str,
) -> tuple[Mapping[str, object], str]:
    try:
        if set(artifact) != {"schema_version", "generated_at", "payload", "integrity"}:
            raise ValueError("envelope exact schema mismatch")
        if artifact.get("schema_version") != schema_version:
            raise ValueError("schema version mismatch")
        generated = _timestamp(str(artifact["generated_at"]), "artifact.generated_at")
        payload = _mapping(artifact["payload"], "artifact.payload")
        integrity = _mapping(artifact["integrity"], "artifact.integrity")
        if set(integrity) != {"algorithm", "scope", "notice", "integrity_digest"}:
            raise ValueError("integrity exact schema mismatch")
        digest = str(integrity["integrity_digest"])
        if (
            payload.get("generated_at") != generated.isoformat()
            or integrity.get("algorithm") != "sha256"
            or integrity.get("scope") != "generated_at+payload"
            or integrity.get("notice") != "content_address_only_strict_replay_required"
            or digest
            != _digest({"generated_at": generated.isoformat(), "payload": dict(payload)})
        ):
            raise ValueError("integrity replay mismatch")
    except ProbabilityRankingError:
        raise
    except (KeyError, TypeError, ValueError) as exc:
        raise ProbabilityRankingError("probability ranking artifact verification failed") from exc
    return payload, digest


def _mapping(value: object, label: str) -> Mapping[str, object]:
    if not isinstance(value, Mapping):
        raise ProbabilityRankingError(f"{label} must be an object")
    return cast(Mapping[str, object], value)


def _timestamp(value: str, label: str) -> datetime:
    try:
        parsed = datetime.fromisoformat(value)
    except (TypeError, ValueError) as exc:
        raise ProbabilityRankingError(f"{label} must be an ISO timestamp") from exc
    if parsed.tzinfo is None or parsed.utcoffset() is None:
        raise ProbabilityRankingError(f"{label} must include timezone")
    return parsed.astimezone(_SHANGHAI)


def _probability(value: object, label: str) -> float:
    output = _finite_float(value, label)
    if not 0 <= output <= 1:
        raise ProbabilityRankingError(f"{label} must be in [0,1]")
    return output


def _score(value: object, label: str) -> float:
    output = _finite_float(value, label)
    if not 0 <= output <= 100:
        raise ProbabilityRankingError(f"{label} must be in [0,100]")
    return output


def _integer_score(value: object, label: str) -> int:
    if isinstance(value, bool) or not isinstance(value, int | float):
        raise ProbabilityRankingError(f"{label} must be numeric")
    number = float(value)
    if not number.is_integer() or not 0 <= number <= 100:
        raise ProbabilityRankingError(f"{label} must be an integer score")
    return int(number)


def _finite_float(value: object, label: str) -> float:
    if isinstance(value, bool) or not isinstance(value, int | float):
        raise ProbabilityRankingError(f"{label} must be numeric")
    output = float(value)
    if not isfinite(output):
        raise ProbabilityRankingError(f"{label} must be finite")
    return output


def _positive_int(value: object, label: str) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or value <= 0:
        raise ProbabilityRankingError(f"{label} must be a positive integer")
    return value


def _nonempty_text(value: object, label: str) -> str:
    if not isinstance(value, str) or not value or value != value.strip():
        raise ProbabilityRankingError(f"{label} must be normalized nonempty text")
    return value


def _digest_text(value: object, label: str) -> str:
    text = _nonempty_text(value, label)
    if len(text) != 64 or any(item not in "0123456789abcdef" for item in text):
        raise ProbabilityRankingError(f"{label} must be a sha256 digest")
    return text


def _digest(value: object) -> str:
    return sha256_hex(canonical_json_bytes(value))


__all__ = [
    "PROBABILITY_RANKING_MANUAL_CONTROL_SCHEMA_VERSION",
    "PROBABILITY_RANKING_PREREGISTERED_AT",
    "PROBABILITY_RANKING_PUBLICATION_SCHEMA_VERSION",
    "PROBABILITY_RANKING_SCORE_RULE_VERSION",
    "PROBABILITY_RANKING_SHADOW_SCHEMA_VERSION",
    "ProbabilityRankingError",
    "VerifiedProbabilityRankingManualControl",
    "VerifiedProbabilityRankingPublication",
    "VerifiedProbabilityRankingShadowEvaluation",
    "build_probability_ranking_publication_artifact",
    "build_probability_ranking_shadow_artifact",
    "probability_ranking_adjustment",
    "probability_ranking_raw_score",
    "probability_ranking_score_spec",
    "probability_ranking_score_spec_hash",
    "seal_probability_ranking_manual_control_artifact",
    "verify_probability_ranking_manual_control_artifact",
    "verify_probability_ranking_publication_artifact",
    "verify_probability_ranking_shadow_artifact",
]
