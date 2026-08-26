"""Formal maintenance for all-decisions H5 execution probability evidence.

This service never upgrades qfq history.  It starts from current published
probability-source snapshots, replays the complete database decision set, and
requires raw-file-verified licensed official sessions for the signal and the
whole D+1..D+6 holding path.  Every downstream authority remains opaque and is
reconstructed from those inputs on restart.
"""

from __future__ import annotations

from collections.abc import Iterator, Mapping, Sequence
from contextlib import contextmanager
from copy import deepcopy
from dataclasses import dataclass
from datetime import datetime
import gzip
from io import BytesIO
from pathlib import Path
import re
from threading import RLock
from typing import cast

from app.artifacts.io import (
    ArtifactIOError,
    canonical_json_bytes,
    decode_json_bytes,
    exclusive_atomic_publish,
    path_has_only_trusted_aliases,
    read_regular_file,
)
from app.services.joint_execution_probability_v3 import (
    VerifiedJointExecutionProbabilityCorpusV3,
    encode_joint_execution_probability_corpus_v3,
)
from app.services.market_scan_contracts import (
    MarketScanCacheProtocol,
    MarketScanSettingsProtocol,
)
from app.services.market_scan_joint_execution_outcomes import (
    VerifiedJointExecutionOutcomeCorpus,
    build_joint_execution_outcome_artifact,
    replay_and_verify_joint_execution_outcome_artifact,
)
from app.services.market_scan_joint_execution_probability import (
    JOINT_EXECUTION_HORIZON,
    JOINT_EXECUTION_MINIMUM_SELECTION_SESSIONS,
    JointExecutionProbabilityError,
    VerifiedJointExecutionCurrentPredictionCorpus,
    VerifiedJointExecutionDeploymentEstimator,
    VerifiedJointExecutionLearningCorpus,
    VerifiedJointExecutionProbabilityStudy,
    build_joint_execution_current_prediction_artifact,
    build_joint_execution_learning_corpus,
    build_joint_execution_probability_oos_corpus_v3,
    fit_joint_execution_deployment_estimator,
    fit_joint_execution_probability,
    joint_execution_deployment_is_fresh,
    replay_and_verify_joint_execution_probability_evidence,
    verify_joint_execution_current_prediction_artifact,
    verify_joint_execution_deployment_artifact,
)
from app.services.market_scan_joint_execution_source import (
    VerifiedJointExecutionSourceCorpus,
    build_joint_execution_source_artifact,
    replay_and_verify_joint_execution_source_artifact,
)
from app.services.market_scan_official_execution import VerifiedOfficialExecutionSession
from app.services.market_scan_official_execution_store import (
    MarketScanOfficialExecutionStore,
    OfficialExecutionStoreStatus,
)
from app.services.market_scan_probability import (
    VerifiedProbabilityFilterAuthorization,
    build_probability_filter_qualification,
    probability_filter_qualified,
    verify_probability_filter_authorization_artifact,
)
from app.services.market_scan_probability_research import PROBABILITY_PRIMARY_TARGET
from app.services.market_scan_probability_ranking import (
    PROBABILITY_RANKING_SCORE_RULE_VERSION,
    ProbabilityRankingError,
    VerifiedProbabilityRankingManualControl,
    VerifiedProbabilityRankingPublication,
    VerifiedProbabilityRankingShadowEvaluation,
    build_probability_ranking_publication_artifact,
    build_probability_ranking_shadow_artifact,
    verify_probability_ranking_manual_control_artifact,
    verify_probability_ranking_publication_artifact,
    verify_probability_ranking_shadow_artifact,
)
from app.services.market_scan_probability_ranking_store import (
    MarketScanProbabilityRankingStore,
)
from app.services.market_scan_probability_source import (
    PROBABILITY_SOURCE_ARTIFACT_SCHEMA_VERSION,
    list_probability_source_snapshots,
    load_probability_source_snapshot,
)
from app.services.trading_calendar import next_trade_dates
from app.utils.clock import market_now


JOINT_EXECUTION_RESEARCH_RELATIVE_PATH = Path("research/market_scan_joint_execution")
JOINT_EXECUTION_MAINTENANCE_CONTRACT_VERSION = (
    "market-scan-joint-execution-maintenance-v1"
)
_MAX_COMPRESSED_ARTIFACT_BYTES = 256 * 1024 * 1024
_MAX_UNCOMPRESSED_ARTIFACT_BYTES = 1024 * 1024 * 1024
_DIGEST = re.compile(r"[0-9a-f]{64}")
_CURRENT_RESEARCH_SUMMARY_KEYS = (
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
    "counts",
    "training_cutoff",
    "calibration_metrics",
    "model_digest",
    "calibrator_digest",
    "limitations",
    "evidence_digest",
)


@dataclass(frozen=True)
class JointExecutionMaintenanceSummary:
    status: str
    source_archive_count: int
    current_source_count: int
    verified_source_count: int
    mature_h5_session_count: int
    selection_minimum_session_count: int
    selection_qualified: bool
    authorization_configured: bool
    authorization_verified: bool
    deployment_verified: bool
    current_prediction_run_id: int | None
    current_prediction_count: int
    official_execution: Mapping[str, object]
    generated_at: str
    probability_ranking_status: str = "shadow_not_available"
    probability_ranking_shadow_digest: str | None = None
    probability_ranking_shadow_qualified: bool = False
    probability_ranking_control_configured: bool = False
    probability_ranking_control_verified: bool = False
    probability_ranking_run_id: int | None = None
    probability_ranking_count: int = 0
    blockers: tuple[str, ...] = ()
    failures: tuple[str, ...] = ()

    @property
    def degraded(self) -> bool:
        return bool(self.failures)

    @property
    def filter_ready(self) -> bool:
        return bool(
            self.status == "current_prediction_ready"
            and self.selection_qualified
            and self.authorization_verified
            and self.deployment_verified
            and self.current_prediction_count > 0
        )

    def payload(self) -> dict[str, object]:
        ranking_effect = (
            "v6_active_for_exact_published_run"
            if self.probability_ranking_status == "production_ranking_ready"
            else "rollback_active_v5_only"
            if self.probability_ranking_status == "rollback_active"
            else "none_without_v6_manual_promotion"
        )
        return {
            "contract_version": JOINT_EXECUTION_MAINTENANCE_CONTRACT_VERSION,
            "status": self.status,
            "source_archive_count": self.source_archive_count,
            "current_source_count": self.current_source_count,
            "verified_source_count": self.verified_source_count,
            "mature_h5_session_count": self.mature_h5_session_count,
            "selection_minimum_session_count": self.selection_minimum_session_count,
            "selection_qualified": self.selection_qualified,
            "authorization_configured": self.authorization_configured,
            "authorization_verified": self.authorization_verified,
            "deployment_verified": self.deployment_verified,
            "current_prediction_run_id": self.current_prediction_run_id,
            "current_prediction_count": self.current_prediction_count,
            "filter_ready": self.filter_ready,
            "official_execution": dict(self.official_execution),
            "generated_at": self.generated_at,
            "probability_ranking_status": self.probability_ranking_status,
            "probability_ranking_shadow_digest": (
                self.probability_ranking_shadow_digest
            ),
            "probability_ranking_shadow_qualified": (
                self.probability_ranking_shadow_qualified
            ),
            "probability_ranking_control_configured": (
                self.probability_ranking_control_configured
            ),
            "probability_ranking_control_verified": (
                self.probability_ranking_control_verified
            ),
            "probability_ranking_run_id": self.probability_ranking_run_id,
            "probability_ranking_count": self.probability_ranking_count,
            "probability_ranking_rule_version": (
                PROBABILITY_RANKING_SCORE_RULE_VERSION
            ),
            "blockers": list(self.blockers),
            "failures": list(self.failures),
            "qfq_history_formal_upgrade_forbidden": True,
            "production_ranking_effect": ranking_effect,
        }

    def message(self) -> str:
        return (
            f"联合执行 H5 维护：official source {self.verified_source_count} 个，"
            f"成熟会话 {self.mature_h5_session_count}/"
            f"{self.selection_minimum_session_count}，状态 {self.status}"
        )


@dataclass(frozen=True)
class _SourceInput:
    artifact: dict[str, object]
    run_id: int
    signal_session: str
    as_of: str
    captured_at: str
    path: Path


@dataclass(frozen=True)
class _MaturePair:
    source: VerifiedJointExecutionSourceCorpus
    outcome: VerifiedJointExecutionOutcomeCorpus


@dataclass(frozen=True)
class _CurrentAuthority:
    source_input: _SourceInput
    source: VerifiedJointExecutionSourceCorpus
    study: VerifiedJointExecutionProbabilityStudy
    authorization: VerifiedProbabilityFilterAuthorization
    deployment: VerifiedJointExecutionDeploymentEstimator
    predictions: VerifiedJointExecutionCurrentPredictionCorpus
    ranking_shadow: VerifiedProbabilityRankingShadowEvaluation | None = None
    ranking_control: VerifiedProbabilityRankingManualControl | None = None
    ranking_publication: VerifiedProbabilityRankingPublication | None = None


@dataclass
class _MaintenanceRunState:
    generated_at: str
    official_status: Mapping[str, object]
    authorization_configured: bool
    ranking_control_configured: bool
    source_inputs: tuple[_SourceInput, ...]
    source_tokens: list[VerifiedJointExecutionSourceCorpus]
    mature_pairs: list[_MaturePair]
    blockers: list[str]
    failures: list[str]


@dataclass(frozen=True)
class _AuthorizedMaintenance:
    study: VerifiedJointExecutionProbabilityStudy
    selected_pairs: tuple[_MaturePair, ...]
    oos_v3: VerifiedJointExecutionProbabilityCorpusV3
    authorization: VerifiedProbabilityFilterAuthorization
    deployment: VerifiedJointExecutionDeploymentEstimator
    deployment_pairs: tuple[_MaturePair, ...]


@dataclass(frozen=True)
class _RankingMaintenance:
    shadow: VerifiedProbabilityRankingShadowEvaluation | None
    control: VerifiedProbabilityRankingManualControl | None
    status: str


@dataclass(frozen=True)
class _HistoricalDeploymentAuthority:
    study: VerifiedJointExecutionProbabilityStudy
    selected_corpus: VerifiedJointExecutionLearningCorpus
    selected_pairs: tuple[_MaturePair, ...]
    deployment: VerifiedJointExecutionDeploymentEstimator


class MarketScanJointExecutionMaintenanceService:
    """Replay and advance the formal H5 chain without auto-promoting ranking."""

    def __init__(
        self,
        cache: MarketScanCacheProtocol,
        official_store: MarketScanOfficialExecutionStore,
        *,
        authorization_path: str | Path | None = None,
        authorization_digest: str | None = None,
        ranking_control_path: str | Path | None = None,
        ranking_control_digest: str | None = None,
        source_directory: str | Path | None = None,
        research_directory: str | Path | None = None,
        ranking_store: MarketScanProbabilityRankingStore | None = None,
    ) -> None:
        self.cache = cache
        self.official_store = official_store
        data_directory = Path(cache.path).expanduser().absolute().parent
        self.source_directory = Path(
            source_directory
            or data_directory / "research" / "market_scan_probability_source"
        ).expanduser().absolute()
        self.research_directory = Path(
            research_directory or data_directory / JOINT_EXECUTION_RESEARCH_RELATIVE_PATH
        ).expanduser().absolute()
        self.authorization_path = (
            Path(authorization_path).expanduser().absolute()
            if authorization_path is not None
            else self.research_directory / "authorization" / "active.json"
        )
        self.authorization_digest = authorization_digest
        self.ranking_control_path = (
            Path(ranking_control_path).expanduser().absolute()
            if ranking_control_path is not None
            else self.research_directory / "ranking-control" / "active.json"
        )
        self.ranking_control_digest = ranking_control_digest
        self.ranking_store = ranking_store or MarketScanProbabilityRankingStore(
            cache.path
        )
        self._lock = RLock()
        self._maintenance_running = False
        self._current_authority: _CurrentAuthority | None = None
        self._ranking_publications: dict[
            int, VerifiedProbabilityRankingPublication
        ] = {}
        self._active_ranking_control: VerifiedProbabilityRankingManualControl | None = (
            None
        )
        self._last_summary: JointExecutionMaintenanceSummary | None = None

    @classmethod
    def from_settings(
        cls,
        cache: MarketScanCacheProtocol,
        settings: MarketScanSettingsProtocol,
        official_store: MarketScanOfficialExecutionStore | None = None,
    ) -> "MarketScanJointExecutionMaintenanceService":
        return cls(
            cache,
            official_store or MarketScanOfficialExecutionStore.from_settings(settings),
            authorization_path=settings.market_scan_joint_execution_authorization_path,
            authorization_digest=settings.market_scan_joint_execution_authorization_digest,
            ranking_control_path=settings.market_scan_probability_ranking_control_path,
            ranking_control_digest=settings.market_scan_probability_ranking_control_digest,
        )

    def run(self, *, now: datetime | None = None) -> JointExecutionMaintenanceSummary:
        with self._lock:
            if self._maintenance_running:
                raise RuntimeError("joint execution maintenance is already running")
            self._maintenance_running = True
            self._revoke_current_authority()
            try:
                self._last_summary = self._maintenance_summary("maintenance_pending")
                summary = self._run_locked(now or market_now())
                self._last_summary = summary
                return summary
            except BaseException as exc:
                self._revoke_current_authority()
                self._last_summary = self._maintenance_summary(
                    "maintenance_failed", failures=(_short_error(exc),)
                )
                raise
            finally:
                self._maintenance_running = False

    @contextmanager
    def _projection_read(self) -> Iterator[bool]:
        acquired = self._lock.acquire(blocking=False)
        try:
            # RLock reentry succeeds in maintenance callbacks too. Those reads must
            # not observe a partially rebuilt authority, even on the writer thread.
            yield acquired and not self._maintenance_running
        finally:
            if acquired:
                self._lock.release()

    def _revoke_current_authority(self) -> None:
        self._current_authority = None
        self._active_ranking_control = None
        self._ranking_publications = {}

    def _maintenance_summary(
        self, status: str, *, failures: tuple[str, ...] = ()
    ) -> JointExecutionMaintenanceSummary:
        registry_digest = getattr(self.official_store, "registry_digest", None)
        official = OfficialExecutionStoreStatus(
            configured=registry_digest is not None,
            status="maintenance_pending" if status == "maintenance_pending" else "store_unavailable",
            registry_digest=registry_digest,
            verified_session_count=0,
            first_session_date=None,
            latest_session_date=None,
            failures=failures,
        ).payload()
        return self._summary(
            status=status,
            generated_at=market_now().isoformat(),
            official_status=official,
            authorization_configured=self.authorization_digest is not None,
            probability_ranking_control_configured=self.ranking_control_digest is not None,
            blockers=(f"joint_execution_{status}",),
            failures=failures,
        )

    def status_projection(self) -> dict[str, object]:
        with self._projection_read() as available:
            if not available:
                return self._maintenance_summary("maintenance_pending").payload()
            if self._last_summary is None:
                official = self.official_store.status().payload()
                return self._summary(
                    status="maintenance_not_run",
                    generated_at=market_now().isoformat(),
                    official_status=official,
                    authorization_configured=self.authorization_digest is not None,
                    probability_ranking_control_configured=(
                        self.ranking_control_digest is not None
                    ),
                    blockers=("joint_execution_maintenance_not_run",),
                ).payload()
            return self._last_summary.payload()

    def has_current_projection(self, run_id: int, *, as_of: str | None = None) -> bool:
        with self._projection_read() as available:
            return available and self._authority_is_current(run_id, as_of=as_of)

    def filter_qualified(self, run_id: int, *, as_of: str | None = None) -> bool:
        with self._projection_read() as available:
            if not available:
                return False
            authority = self._current_authority
            return bool(
                authority is not None
                and self._authority_is_current(run_id, as_of=as_of)
                and probability_filter_qualified(
                    authority.study.payload,
                    authority.authorization,
                )
            )

    def research_projection(self, run_id: int) -> dict[str, object]:
        research, _records = self.run_projection(run_id, symbols=())
        return research

    def run_projection(
        self,
        run_id: int,
        *,
        symbols: Sequence[str] | None = None,
    ) -> tuple[dict[str, object], dict[str, dict[str, object]]]:
        with self._projection_read() as available:
            if not available:
                return _not_generated_joint_projection(
                    run_id, self._maintenance_summary("maintenance_pending").payload()
                ), {}
            if not self._authority_is_current(run_id):
                return _not_generated_joint_projection(
                    run_id,
                    self.status_projection(),
                ), {}
            authority = cast(_CurrentAuthority, self._current_authority)
            research = _current_research_projection(authority)
            records = _current_record_projection(authority.predictions, symbols=symbols)
            return deepcopy(research), deepcopy(records)

    def has_production_ranking(self, run_id: int) -> bool:
        with self._projection_read() as available:
            if not available:
                return False
            publication = self._ranking_publications.get(run_id)
            if publication is None:
                return False
            try:
                return bool(
                    not self.ranking_store.is_rolled_back(publication)
                    and self.ranking_store.verify_mirror(publication)
                )
            except Exception:
                return False

    def production_ranking_projection(
        self,
        run_id: int,
    ) -> tuple[dict[str, object], dict[str, dict[str, object]]]:
        with self._projection_read() as available:
            if not available:
                return _inactive_production_ranking_projection(run_id, "maintenance_pending"), {}
            if not self.has_production_ranking(run_id):
                reason = (
                    "maintenance_failed"
                    if self._last_summary is not None
                    and self._last_summary.status == "maintenance_failed"
                    else None
                )
                return _inactive_production_ranking_projection(run_id, reason), {}
            publication = self._ranking_publications[run_id]
            context = {
                "contract_version": "market-scan-probability-ranking-projection-v1",
                "status": "active",
                "run_id": run_id,
                "score_rule_version": PROBABILITY_RANKING_SCORE_RULE_VERSION,
                "score_spec_hash": publication.score_spec_hash,
                "artifact_digest": publication.artifact_digest,
                "promotion_digest": publication.promotion_digest,
                "generated_at": publication.generated_at,
                "record_count": len(publication),
                "base_snapshot_digest": publication.base_snapshot_digest,
                "base_v5_mutated": False,
                "historical_ranks_mutated": False,
                "rollback_available": True,
            }
            return deepcopy(context), deepcopy(publication.record_by_symbol())

    def _authority_is_current(self, run_id: int, *, as_of: str | None = None) -> bool:
        authority = self._current_authority
        if authority is None or authority.predictions.run_id != run_id:
            return False
        try:
            official_ready = self.official_store.status().formal_evidence_available
        except Exception:
            return False
        return bool(
            official_ready
            and joint_execution_deployment_is_fresh(
                authority.deployment,
                as_of=as_of,
            )
        )

    def _run_locked(
        self, current: datetime
    ) -> JointExecutionMaintenanceSummary:
        if current.tzinfo is None or current.utcoffset() is None:
            raise ValueError("joint execution maintenance now must be timezone-aware")
        self._ranking_publications = {}
        generated_at = current.isoformat()
        official_status = self.official_store.status().payload()
        authorization_configured = self.authorization_digest is not None
        ranking_control_configured = self.ranking_control_digest is not None
        if official_status.get("formal_evidence_available") is not True:
            return self._summary(
                status="official_execution_unavailable",
                generated_at=generated_at,
                official_status=official_status,
                authorization_configured=authorization_configured,
                probability_ranking_control_configured=ranking_control_configured,
                blockers=tuple(
                    str(item) for item in cast(list[object], official_status["failures"])
                ),
            )
        sessions = self.official_store.sessions()
        sessions_by_date = {item.session_date: item for item in sessions}
        state = _MaintenanceRunState(
            generated_at=generated_at,
            official_status=official_status,
            authorization_configured=authorization_configured,
            ranking_control_configured=ranking_control_configured,
            source_inputs=_canonical_source_inputs(self.source_directory),
            source_tokens=[],
            mature_pairs=[],
            blockers=[],
            failures=[],
        )
        self._collect_mature_sources(state, sessions_by_date)
        if not state.mature_pairs:
            return self._waiting_mature_summary(state)
        state.failures.extend(
            self._replay_historical_rankings(state.source_tokens, state.mature_pairs)
        )
        corpus = _learning_corpus(state.mature_pairs)
        study = self._study_for_corpus(corpus, generated_at=generated_at)
        selection_qualified = study.payload["selection_qualified"] is True
        if not authorization_configured:
            state.blockers.append("exact_probability_filter_authorization_not_pinned")
            return self._selection_waiting_summary(state, selection_qualified)
        try:
            authorized = self._authorized_maintenance(state)
        except Exception as exc:
            state.failures.append(f"authorization/deployment: {_short_error(exc)}")
            return self._authorization_blocked_summary(state, selection_qualified)
        ranking = self._ranking_maintenance(state, authorized)
        return self._current_maintenance_summary(state, authorized, ranking)

    def _collect_mature_sources(
        self,
        state: _MaintenanceRunState,
        sessions_by_date: Mapping[str, VerifiedOfficialExecutionSession],
    ) -> None:
        for source_input in state.source_inputs:
            try:
                source_token = self._source_token(
                    source_input,
                    sessions_by_date,
                    generated_at=state.generated_at,
                )
                if source_token is None:
                    state.blockers.append(
                        f"official_signal_session_missing:{source_input.signal_session}"
                    )
                    continue
                state.source_tokens.append(source_token)
                pair = self._mature_pair(
                    source_token,
                    sessions_by_date,
                    generated_at=state.generated_at,
                )
                if pair is None:
                    state.blockers.append(
                        f"official_h5_path_waiting:{source_input.signal_session}"
                    )
                    continue
                state.mature_pairs.append(pair)
            except Exception as exc:  # isolate one immutable run
                state.failures.append(f"run {source_input.run_id}: {_short_error(exc)}")

    def _authorized_maintenance(
        self,
        state: _MaintenanceRunState,
    ) -> _AuthorizedMaintenance:
        authorization_artifact = _load_pinned_authorization(
            self.authorization_path,
            cast(str, self.authorization_digest),
        )
        study, corpus, selected_pairs = self._selected_study(
            authorization_artifact,
            state.mature_pairs,
        )
        oos_v3 = build_joint_execution_probability_oos_corpus_v3(
            study,
            corpus,
            [item.source for item in selected_pairs],
            [item.outcome for item in selected_pairs],
        )
        self._publish_encoded(
            self.research_directory / "oos-v3",
            f"joint-execution-oos-v3-{oos_v3.integrity_digest}.json.gz",
            encode_joint_execution_probability_corpus_v3(oos_v3),
        )
        authorization = verify_probability_filter_authorization_artifact(
            authorization_artifact,
            study.payload,
        )
        self._publish_envelope(
            "authorizations",
            "probability-filter-authorization",
            authorization_artifact,
        )
        deployment_pairs = _deployment_pairs(selected_pairs, state.mature_pairs)
        deployment = self._deployment_for_corpus(
            _learning_corpus(deployment_pairs),
            study,
            authorization,
            generated_at=state.generated_at,
        )
        return _AuthorizedMaintenance(
            study=study,
            selected_pairs=selected_pairs,
            oos_v3=oos_v3,
            authorization=authorization,
            deployment=deployment,
            deployment_pairs=deployment_pairs,
        )

    def _ranking_maintenance(
        self,
        state: _MaintenanceRunState,
        authorized: _AuthorizedMaintenance,
    ) -> _RankingMaintenance:
        shadow: VerifiedProbabilityRankingShadowEvaluation | None = None
        control: VerifiedProbabilityRankingManualControl | None = None
        status = "shadow_evaluation_unavailable"
        try:
            shadow = self._ranking_shadow_for_oos(
                authorized.oos_v3,
                [item.source for item in authorized.selected_pairs],
                authorized.study,
                generated_at=state.generated_at,
            )
            if not shadow.qualified:
                status = "shadow_gates_failed"
                state.blockers.append("probability_ranking_shadow_gates_failed")
                self._active_ranking_control = None
            elif not state.ranking_control_configured:
                status = "waiting_explicit_human_promotion"
                state.blockers.append("probability_ranking_manual_control_not_pinned")
                self._active_ranking_control = None
            else:
                control_artifact = _load_pinned_authorization(
                    self.ranking_control_path,
                    cast(str, self.ranking_control_digest),
                )
                control = verify_probability_ranking_manual_control_artifact(
                    control_artifact,
                    expected_digest=cast(str, self.ranking_control_digest),
                    shadow=shadow,
                )
                self._publish_envelope(
                    "ranking-controls",
                    f"probability-ranking-control-{control.action}",
                    control_artifact,
                )
                self._active_ranking_control = control
                if control.action == "rollback":
                    self.ranking_store.record_rollback(control)
                    status = "rollback_active"
                    state.blockers.append("probability_ranking_manual_rollback_active")
                else:
                    status = "promotion_verified_waiting_new_official_batch"
        except Exception as exc:
            state.failures.append(
                f"probability ranking shadow/control: {_short_error(exc)}"
            )
            status = "shadow_or_control_verification_failed"
            self._active_ranking_control = None
        return _RankingMaintenance(shadow=shadow, control=control, status=status)

    def _current_maintenance_summary(
        self,
        state: _MaintenanceRunState,
        authorized: _AuthorizedMaintenance,
        ranking: _RankingMaintenance,
    ) -> JointExecutionMaintenanceSummary:
        current_source = _current_prediction_source(
            state.source_tokens,
            latest_training_session=max(
                item.source.signal_session for item in authorized.deployment_pairs
            ),
            generated_at=state.generated_at,
        )
        if current_source is None:
            state.blockers.append("new_same_session_official_source_not_available")
            return self._waiting_new_source_summary(state, ranking)
        current_token = self._current_prediction_for_source(
            current_source,
            authorized.study,
            authorized.deployment,
            generated_at=state.generated_at,
        )
        current_source_input = next(
            item for item in state.source_inputs if item.run_id == current_source.run_id
        )
        publication, ranking_status = self._publish_current_ranking(
            state,
            authorized,
            ranking,
            current_source,
            current_token,
        )
        self._current_authority = _CurrentAuthority(
            source_input=current_source_input,
            source=current_source,
            study=authorized.study,
            authorization=authorized.authorization,
            deployment=authorized.deployment,
            predictions=current_token,
            ranking_shadow=ranking.shadow,
            ranking_control=ranking.control,
            ranking_publication=publication,
        )
        return self._ready_summary(
            state,
            ranking,
            ranking_status,
            current_token,
            publication,
        )

    def _publish_current_ranking(
        self,
        state: _MaintenanceRunState,
        authorized: _AuthorizedMaintenance,
        ranking: _RankingMaintenance,
        source: VerifiedJointExecutionSourceCorpus,
        predictions: VerifiedJointExecutionCurrentPredictionCorpus,
    ) -> tuple[VerifiedProbabilityRankingPublication | None, str]:
        control = ranking.control
        if control is None or control.action != "promote":
            return None, ranking.status
        if source.run_id <= control.effective_after_run_id:
            state.blockers.append("probability_ranking_requires_post_promotion_run")
            return None, "promotion_verified_waiting_new_official_batch"
        try:
            publication, path = self._ranking_publication_for_tokens(
                source,
                predictions,
                authorized.study,
                authorized.deployment,
                control,
                generated_at=state.generated_at,
            )
            self.ranking_store.publish(publication, artifact_path=path)
            self._ranking_publications[source.run_id] = publication
            return publication, "production_ranking_ready"
        except Exception as exc:
            state.failures.append(f"probability ranking publication: {_short_error(exc)}")
            return None, "production_ranking_publication_failed"

    def _ready_summary(
        self,
        state: _MaintenanceRunState,
        ranking: _RankingMaintenance,
        ranking_status: str,
        predictions: VerifiedJointExecutionCurrentPredictionCorpus,
        publication: VerifiedProbabilityRankingPublication | None,
    ) -> JointExecutionMaintenanceSummary:
        return self._summary(
            status="current_prediction_ready",
            generated_at=state.generated_at,
            official_status=state.official_status,
            source_archive_count=len(state.source_inputs),
            current_source_count=len(state.source_inputs),
            verified_source_count=len(state.source_tokens),
            mature_h5_session_count=len(state.mature_pairs),
            selection_qualified=True,
            authorization_configured=True,
            authorization_verified=True,
            deployment_verified=True,
            current_prediction_run_id=predictions.run_id,
            current_prediction_count=len(predictions),
            probability_ranking_status=ranking_status,
            probability_ranking_shadow_digest=(
                ranking.shadow.integrity_digest if ranking.shadow is not None else None
            ),
            probability_ranking_shadow_qualified=bool(
                ranking.shadow is not None and ranking.shadow.qualified
            ),
            probability_ranking_control_configured=state.ranking_control_configured,
            probability_ranking_control_verified=ranking.control is not None,
            probability_ranking_run_id=(
                publication.run_id if publication is not None else None
            ),
            probability_ranking_count=(
                len(publication) if publication is not None else 0
            ),
            blockers=tuple(dict.fromkeys(state.blockers)),
            failures=tuple(state.failures),
        )

    def _waiting_mature_summary(
        self,
        state: _MaintenanceRunState,
    ) -> JointExecutionMaintenanceSummary:
        return self._summary(
            status="waiting_mature_official_h5",
            generated_at=state.generated_at,
            official_status=state.official_status,
            source_archive_count=len(state.source_inputs),
            current_source_count=len(state.source_inputs),
            verified_source_count=len(state.source_tokens),
            authorization_configured=state.authorization_configured,
            probability_ranking_control_configured=state.ranking_control_configured,
            blockers=tuple(dict.fromkeys(state.blockers)),
            failures=tuple(state.failures),
        )

    def _selection_waiting_summary(
        self,
        state: _MaintenanceRunState,
        selection_qualified: bool,
    ) -> JointExecutionMaintenanceSummary:
        status = (
            "selection_passed_waiting_authorization"
            if selection_qualified
            else "selection_evidence_accumulating"
        )
        return self._summary(
            status=status,
            generated_at=state.generated_at,
            official_status=state.official_status,
            source_archive_count=len(state.source_inputs),
            current_source_count=len(state.source_inputs),
            verified_source_count=len(state.source_tokens),
            mature_h5_session_count=len(state.mature_pairs),
            selection_qualified=selection_qualified,
            authorization_configured=False,
            probability_ranking_control_configured=state.ranking_control_configured,
            blockers=tuple(dict.fromkeys(state.blockers)),
            failures=tuple(state.failures),
        )

    def _authorization_blocked_summary(
        self,
        state: _MaintenanceRunState,
        selection_qualified: bool,
    ) -> JointExecutionMaintenanceSummary:
        return self._summary(
            status="authorization_or_deployment_blocked",
            generated_at=state.generated_at,
            official_status=state.official_status,
            source_archive_count=len(state.source_inputs),
            current_source_count=len(state.source_inputs),
            verified_source_count=len(state.source_tokens),
            mature_h5_session_count=len(state.mature_pairs),
            selection_qualified=selection_qualified,
            authorization_configured=True,
            probability_ranking_control_configured=state.ranking_control_configured,
            blockers=tuple(dict.fromkeys(state.blockers)),
            failures=tuple(state.failures),
        )

    def _waiting_new_source_summary(
        self,
        state: _MaintenanceRunState,
        ranking: _RankingMaintenance,
    ) -> JointExecutionMaintenanceSummary:
        return self._summary(
            status="deployment_ready_waiting_new_official_batch",
            generated_at=state.generated_at,
            official_status=state.official_status,
            source_archive_count=len(state.source_inputs),
            current_source_count=len(state.source_inputs),
            verified_source_count=len(state.source_tokens),
            mature_h5_session_count=len(state.mature_pairs),
            selection_qualified=True,
            authorization_configured=True,
            authorization_verified=True,
            deployment_verified=True,
            probability_ranking_status=ranking.status,
            probability_ranking_shadow_digest=(
                ranking.shadow.integrity_digest if ranking.shadow is not None else None
            ),
            probability_ranking_shadow_qualified=bool(
                ranking.shadow is not None and ranking.shadow.qualified
            ),
            probability_ranking_control_configured=state.ranking_control_configured,
            probability_ranking_control_verified=ranking.control is not None,
            blockers=tuple(dict.fromkeys(state.blockers)),
            failures=tuple(state.failures),
        )

    def _source_token(
        self,
        source_input: _SourceInput,
        sessions: Mapping[str, VerifiedOfficialExecutionSession],
        *,
        generated_at: str,
    ) -> VerifiedJointExecutionSourceCorpus | None:
        official = sessions.get(source_input.signal_session)
        if official is None:
            return None
        with self.cache.verified_market_scan_read(source_input.run_id) as verified:
            execution = dict(verified.execution_session_evidence())
        directory = self.research_directory / "sources"
        existing = _single_managed_artifact(
            directory,
            f"joint-execution-source-run-{source_input.run_id}-*.json.gz",
        )
        if existing is not None:
            return replay_and_verify_joint_execution_source_artifact(
                existing,
                source_input.artifact,
                execution,
                official,
            )
        artifact = build_joint_execution_source_artifact(
            source_input.artifact,
            execution,
            official,
            generated_at=generated_at,
        )
        token = replay_and_verify_joint_execution_source_artifact(
            artifact,
            source_input.artifact,
            execution,
            official,
        )
        self._publish_encoded(
            directory,
            (
                f"joint-execution-source-run-{source_input.run_id}-"
                f"{token.artifact_digest}.json.gz"
            ),
            canonical_json_bytes(artifact),
        )
        return token

    def _mature_pair(
        self,
        source: VerifiedJointExecutionSourceCorpus,
        sessions: Mapping[str, VerifiedOfficialExecutionSession],
        *,
        generated_at: str,
    ) -> _MaturePair | None:
        required = tuple(
            item.isoformat()
            for item in next_trade_dates(
                datetime.fromisoformat(source.signal_session).date(),
                JOINT_EXECUTION_HORIZON + 1,
            )
        )
        if any(item not in sessions for item in required):
            return None
        official_path = {item: sessions[item] for item in required}
        directory = self.research_directory / "outcomes-h5"
        existing = _single_managed_artifact(
            directory,
            f"joint-execution-outcome-h5-run-{source.run_id}-*.json.gz",
        )
        if existing is not None:
            outcome = replay_and_verify_joint_execution_outcome_artifact(
                existing,
                source,
                official_path,
            )
            return _MaturePair(source=source, outcome=outcome)
        artifact = build_joint_execution_outcome_artifact(
            source,
            official_path,
            generated_at=generated_at,
            horizons=(JOINT_EXECUTION_HORIZON,),
        )
        outcome = replay_and_verify_joint_execution_outcome_artifact(
            artifact,
            source,
            official_path,
        )
        self._publish_encoded(
            directory,
            (
                f"joint-execution-outcome-h5-run-{source.run_id}-"
                f"{outcome.artifact_digest}.json.gz"
            ),
            canonical_json_bytes(artifact),
        )
        return _MaturePair(source=source, outcome=outcome)

    def _selected_study(
        self,
        authorization: Mapping[str, object],
        mature_pairs: Sequence[_MaturePair],
    ) -> tuple[
        VerifiedJointExecutionProbabilityStudy,
        VerifiedJointExecutionLearningCorpus,
        tuple[_MaturePair, ...],
    ]:
        payload = _mapping(authorization["payload"], "authorization.payload")
        binding = _mapping(payload["evidence_binding"], "evidence_binding")
        digest = str(binding["evidence_digest"])
        if _DIGEST.fullmatch(digest) is None:
            raise JointExecutionProbabilityError(
                "authorization selected evidence digest is invalid"
            )
        artifact = _read_gzip_json(
            self.research_directory
            / "studies"
            / f"joint-execution-study-{digest}.json.gz"
        )
        bindings = cast(list[object], artifact["source_bindings"])
        run_ids = {
            int(cast(int, _mapping(item, "study.source_bindings[]")["run_id"]))
            for item in bindings
        }
        selected_pairs = tuple(
            item for item in mature_pairs if item.source.run_id in run_ids
        )
        if len(selected_pairs) != len(run_ids):
            raise JointExecutionProbabilityError(
                "authorized study source/outcome tokens are no longer available"
            )
        corpus = _learning_corpus(selected_pairs)
        study = replay_and_verify_joint_execution_probability_evidence(artifact, corpus)
        if study.evidence_digest != digest:
            raise JointExecutionProbabilityError(
                "authorization selected study digest does not replay"
            )
        return study, corpus, selected_pairs

    def _study_for_corpus(
        self,
        corpus: VerifiedJointExecutionLearningCorpus,
        *,
        generated_at: str,
    ) -> VerifiedJointExecutionProbabilityStudy:
        directory = self.research_directory / "studies"
        matches: list[tuple[datetime, str, VerifiedJointExecutionProbabilityStudy]] = []
        for path in _managed_artifact_paths(
            directory,
            "joint-execution-study-*.json.gz",
        ):
            artifact = _read_gzip_json(path)
            if artifact.get("input_digest") != corpus.corpus_digest:
                continue
            digest = str(artifact.get("evidence_digest") or "")
            if path.name != f"joint-execution-study-{digest}.json.gz":
                raise JointExecutionProbabilityError(
                    "joint execution study filename/content digest mismatch"
                )
            study = replay_and_verify_joint_execution_probability_evidence(
                artifact,
                corpus,
            )
            matches.append(
                (
                    _timestamp(str(artifact["generated_at"])),
                    study.evidence_digest,
                    study,
                )
            )
        if matches:
            return min(matches, key=lambda item: (item[0], item[1]))[2]
        artifact = fit_joint_execution_probability(
            corpus,
            generated_at=generated_at,
        )
        study = replay_and_verify_joint_execution_probability_evidence(
            artifact,
            corpus,
        )
        self._publish_study(artifact)
        return study

    def _publish_study(self, artifact: Mapping[str, object]) -> None:
        digest = str(artifact["evidence_digest"])
        self._publish_encoded(
            self.research_directory / "studies",
            f"joint-execution-study-{digest}.json.gz",
            canonical_json_bytes(dict(artifact)),
        )

    def _ranking_shadow_for_oos(
        self,
        oos_corpus: VerifiedJointExecutionProbabilityCorpusV3,
        sources: Sequence[VerifiedJointExecutionSourceCorpus],
        study: VerifiedJointExecutionProbabilityStudy,
        *,
        generated_at: str,
    ) -> VerifiedProbabilityRankingShadowEvaluation:
        directory = self.research_directory / "ranking-shadows"
        matches: list[
            tuple[datetime, str, VerifiedProbabilityRankingShadowEvaluation]
        ] = []
        for path in _managed_artifact_paths(
            directory,
            "probability-ranking-shadow-*.json.gz",
        ):
            artifact = _read_gzip_json(path)
            payload = _mapping(artifact.get("payload"), "ranking shadow.payload")
            if (
                payload.get("oos_corpus_digest") != oos_corpus.integrity_digest
                or payload.get("study_evidence_digest") != study.evidence_digest
            ):
                continue
            token = verify_probability_ranking_shadow_artifact(
                artifact,
                oos_corpus=oos_corpus,
                sources=sources,
                study=study,
            )
            if path.name != f"probability-ranking-shadow-{token.integrity_digest}.json.gz":
                raise ProbabilityRankingError(
                    "ranking shadow filename/content digest mismatch"
                )
            matches.append(
                (
                    _timestamp(str(artifact["generated_at"])),
                    token.integrity_digest,
                    token,
                )
            )
        if matches:
            return min(matches, key=lambda item: (item[0], item[1]))[2]
        artifact = build_probability_ranking_shadow_artifact(
            oos_corpus,
            sources,
            study,
            generated_at=generated_at,
        )
        token = verify_probability_ranking_shadow_artifact(
            artifact,
            oos_corpus=oos_corpus,
            sources=sources,
            study=study,
        )
        self._publish_envelope(
            "ranking-shadows",
            "probability-ranking-shadow",
            artifact,
        )
        return token

    def _replay_historical_rankings(
        self,
        sources: Sequence[VerifiedJointExecutionSourceCorpus],
        mature_pairs: Sequence[_MaturePair],
    ) -> list[str]:
        directory = self.research_directory / "production-rankings-v6"
        grouped: dict[int, list[Path]] = {}
        failures: list[str] = []
        for path in _managed_artifact_paths(
            directory,
            "probability-ranking-v6-run-*.json.gz",
        ):
            try:
                artifact = _read_gzip_json(path)
                payload = _mapping(
                    artifact.get("payload"),
                    "ranking publication.payload",
                )
                run_id = int(cast(int, payload["run_id"]))
                if run_id <= 0:
                    raise ProbabilityRankingError(
                        "historical v6 ranking run_id is invalid"
                    )
                grouped.setdefault(run_id, []).append(path)
            except Exception as exc:
                failures.append(
                    f"historical probability ranking {path.name}: {_short_error(exc)}"
                )
        source_by_run = {item.run_id: item for item in sources}
        for run_id, paths in sorted(grouped.items()):
            if len(paths) != 1:
                failures.append(
                    f"historical probability ranking run {run_id}: "
                    "multiple immutable publications"
                )
                continue
            try:
                publication = self._replay_historical_ranking(
                    paths[0],
                    source_by_run,
                    mature_pairs,
                )
                self.ranking_store.publish(
                    publication,
                    artifact_path=paths[0],
                )
                self._ranking_publications[run_id] = publication
            except Exception as exc:
                self._ranking_publications.pop(run_id, None)
                failures.append(
                    f"historical probability ranking run {run_id}: "
                    f"{_short_error(exc)}"
                )
        return failures

    def _replay_historical_ranking(
        self,
        path: Path,
        sources: Mapping[int, VerifiedJointExecutionSourceCorpus],
        mature_pairs: Sequence[_MaturePair],
    ) -> VerifiedProbabilityRankingPublication:
        artifact = _read_gzip_json(path)
        payload = _mapping(artifact.get("payload"), "ranking publication.payload")
        run_id = int(cast(int, payload["run_id"]))
        source = _historical_ranking_source(payload, sources, run_id)
        authority = self._historical_deployment_authority(payload, mature_pairs)
        predictions = self._historical_predictions(
            payload,
            source,
            authority.study,
            authority.deployment,
            run_id,
        )
        promotion = self._historical_promotion(payload, authority)
        publication = verify_probability_ranking_publication_artifact(
            artifact,
            source=source,
            predictions=predictions,
            study=authority.study,
            deployment=authority.deployment,
            promotion=promotion,
        )
        expected_name = f"probability-ranking-v6-run-{run_id}-{publication.artifact_digest}.json.gz"
        if path.name != expected_name:
            raise ProbabilityRankingError(
                "historical v6 ranking filename/content digest mismatch"
            )
        return publication

    def _historical_deployment_authority(
        self,
        payload: Mapping[str, object],
        mature_pairs: Sequence[_MaturePair],
    ) -> _HistoricalDeploymentAuthority:
        study, selected_corpus, selected_pairs = self._study_by_digest(
            str(payload["study_evidence_digest"]),
            mature_pairs,
        )
        deployment_digest = str(payload["deployment_artifact_digest"])
        deployment_artifact = _read_gzip_json(
            self.research_directory
            / "deployments"
            / f"joint-execution-deployment-{deployment_digest}.json.gz"
        )
        deployment_payload = _mapping(
            deployment_artifact.get("payload"),
            "deployment.payload",
        )
        deployment_pairs = _pairs_for_exact_bindings(
            deployment_payload.get("source_bindings"),
            mature_pairs,
            label="deployment.source_bindings",
        )
        deployment_corpus = _learning_corpus(deployment_pairs)
        authorization_digest = str(deployment_payload["authorization_digest"])
        authorization_artifact = _read_gzip_json(
            self.research_directory
            / "authorizations"
            / f"probability-filter-authorization-{authorization_digest}.json.gz"
        )
        authorization = verify_probability_filter_authorization_artifact(
            authorization_artifact,
            study.payload,
        )
        if authorization.integrity_digest != authorization_digest:
            raise ProbabilityRankingError(
                "historical deployment authorization digest mismatch"
            )
        deployment = verify_joint_execution_deployment_artifact(
            deployment_artifact,
            corpus=deployment_corpus,
            study=study,
            authorization=authorization,
            as_of=str(deployment_artifact["generated_at"]),
        )
        if deployment.integrity_digest != deployment_digest:
            raise ProbabilityRankingError(
                "historical deployment digest mismatch"
            )
        return _HistoricalDeploymentAuthority(
            study=study,
            selected_corpus=selected_corpus,
            selected_pairs=selected_pairs,
            deployment=deployment,
        )

    def _historical_predictions(
        self,
        payload: Mapping[str, object],
        source: VerifiedJointExecutionSourceCorpus,
        study: VerifiedJointExecutionProbabilityStudy,
        deployment: VerifiedJointExecutionDeploymentEstimator,
        run_id: int,
    ) -> VerifiedJointExecutionCurrentPredictionCorpus:
        prediction_digest = str(payload["current_prediction_artifact_digest"])
        prediction_artifact = _read_gzip_json(
            self.research_directory
            / "current-predictions"
            / (
                f"joint-execution-current-run-{run_id}-"
                f"{prediction_digest}.json.gz"
            )
        )
        predictions = verify_joint_execution_current_prediction_artifact(
            prediction_artifact,
            source=source,
            study=study,
            deployment=deployment,
            as_of=str(prediction_artifact["generated_at"]),
        )
        if predictions.artifact_digest != prediction_digest:
            raise ProbabilityRankingError(
                "historical current prediction digest mismatch"
            )
        return predictions

    def _historical_promotion(
        self,
        payload: Mapping[str, object],
        authority: _HistoricalDeploymentAuthority,
    ) -> VerifiedProbabilityRankingManualControl:
        promotion_digest = str(payload["promotion_digest"])
        control_artifact = _read_gzip_json(
            self.research_directory
            / "ranking-controls"
            / f"probability-ranking-control-promote-{promotion_digest}.json.gz"
        )
        control_payload = _mapping(
            control_artifact.get("payload"),
            "ranking control.payload",
        )
        oos_v3 = build_joint_execution_probability_oos_corpus_v3(
            authority.study,
            authority.selected_corpus,
            [item.source for item in authority.selected_pairs],
            [item.outcome for item in authority.selected_pairs],
        )
        shadow_digest = str(control_payload["shadow_artifact_digest"])
        shadow_artifact = _read_gzip_json(
            self.research_directory
            / "ranking-shadows"
            / f"probability-ranking-shadow-{shadow_digest}.json.gz"
        )
        shadow = verify_probability_ranking_shadow_artifact(
            shadow_artifact,
            oos_corpus=oos_v3,
            sources=[item.source for item in authority.selected_pairs],
            study=authority.study,
        )
        return verify_probability_ranking_manual_control_artifact(
            control_artifact,
            expected_digest=promotion_digest,
            shadow=shadow,
        )

    def _study_by_digest(
        self,
        digest: str,
        mature_pairs: Sequence[_MaturePair],
    ) -> tuple[
        VerifiedJointExecutionProbabilityStudy,
        VerifiedJointExecutionLearningCorpus,
        tuple[_MaturePair, ...],
    ]:
        if _DIGEST.fullmatch(digest) is None:
            raise JointExecutionProbabilityError(
                "historical study digest is invalid"
            )
        artifact = _read_gzip_json(
            self.research_directory
            / "studies"
            / f"joint-execution-study-{digest}.json.gz"
        )
        selected_pairs = _pairs_for_exact_bindings(
            artifact.get("source_bindings"),
            mature_pairs,
            label="study.source_bindings",
        )
        corpus = _learning_corpus(selected_pairs)
        study = replay_and_verify_joint_execution_probability_evidence(
            artifact,
            corpus,
        )
        if study.evidence_digest != digest:
            raise JointExecutionProbabilityError(
                "historical study digest does not replay"
            )
        return study, corpus, selected_pairs

    def _deployment_for_corpus(
        self,
        corpus: VerifiedJointExecutionLearningCorpus,
        study: VerifiedJointExecutionProbabilityStudy,
        authorization: VerifiedProbabilityFilterAuthorization,
        *,
        generated_at: str,
    ) -> VerifiedJointExecutionDeploymentEstimator:
        directory = self.research_directory / "deployments"
        matches = self._matching_deployments(
            directory,
            corpus,
            study,
            authorization,
            generated_at,
        )
        if matches:
            return min(matches, key=lambda item: (item[0], item[1]))[2]
        return self._build_deployment(corpus, study, authorization, generated_at)

    def _matching_deployments(
        self,
        directory: Path,
        corpus: VerifiedJointExecutionLearningCorpus,
        study: VerifiedJointExecutionProbabilityStudy,
        authorization: VerifiedProbabilityFilterAuthorization,
        generated_at: str,
    ) -> list[tuple[datetime, str, VerifiedJointExecutionDeploymentEstimator]]:
        matches: list[
            tuple[datetime, str, VerifiedJointExecutionDeploymentEstimator]
        ] = []
        for path in _managed_artifact_paths(
            directory,
            "joint-execution-deployment-*.json.gz",
        ):
            artifact = _read_gzip_json(path)
            payload = _mapping(artifact.get("payload"), "deployment.payload")
            if (
                payload.get("corpus_digest") != corpus.corpus_digest
                or payload.get("study_evidence_digest") != study.evidence_digest
                or payload.get("authorization_digest")
                != authorization.integrity_digest
            ):
                continue
            artifact_generated_at = str(artifact["generated_at"])
            token = verify_joint_execution_deployment_artifact(
                artifact,
                corpus=corpus,
                study=study,
                authorization=authorization,
                as_of=artifact_generated_at,
            )
            if path.name != (
                f"joint-execution-deployment-{token.integrity_digest}.json.gz"
            ):
                raise JointExecutionProbabilityError(
                    "joint deployment filename/content digest mismatch"
                )
            if not joint_execution_deployment_is_fresh(
                token,
                as_of=generated_at,
            ):
                continue
            matches.append(
                (
                    _timestamp(artifact_generated_at),
                    token.integrity_digest,
                    token,
                )
            )
        return matches

    def _build_deployment(
        self,
        corpus: VerifiedJointExecutionLearningCorpus,
        study: VerifiedJointExecutionProbabilityStudy,
        authorization: VerifiedProbabilityFilterAuthorization,
        generated_at: str,
    ) -> VerifiedJointExecutionDeploymentEstimator:
        artifact = fit_joint_execution_deployment_estimator(
            corpus,
            study,
            authorization,
            generated_at=generated_at,
        )
        token = verify_joint_execution_deployment_artifact(
            artifact,
            corpus=corpus,
            study=study,
            authorization=authorization,
            as_of=generated_at,
        )
        self._publish_envelope(
            "deployments",
            "joint-execution-deployment",
            artifact,
        )
        return token

    def _current_prediction_for_source(
        self,
        source: VerifiedJointExecutionSourceCorpus,
        study: VerifiedJointExecutionProbabilityStudy,
        deployment: VerifiedJointExecutionDeploymentEstimator,
        *,
        generated_at: str,
    ) -> VerifiedJointExecutionCurrentPredictionCorpus:
        directory = self.research_directory / "current-predictions"
        matches = self._matching_current_predictions(
            directory,
            source,
            study,
            deployment,
            generated_at,
        )
        if matches:
            return min(matches, key=lambda item: (item[0], item[1]))[2]
        return self._build_current_prediction(source, study, deployment, generated_at)

    def _matching_current_predictions(
        self,
        directory: Path,
        source: VerifiedJointExecutionSourceCorpus,
        study: VerifiedJointExecutionProbabilityStudy,
        deployment: VerifiedJointExecutionDeploymentEstimator,
        generated_at: str,
    ) -> list[tuple[datetime, str, VerifiedJointExecutionCurrentPredictionCorpus]]:
        matches: list[
            tuple[datetime, str, VerifiedJointExecutionCurrentPredictionCorpus]
        ] = []
        for path in _managed_artifact_paths(
            directory,
            f"joint-execution-current-run-{source.run_id}-*.json.gz",
        ):
            artifact = _read_gzip_json(path)
            payload = _mapping(artifact.get("payload"), "current prediction.payload")
            run = _mapping(payload.get("run"), "current prediction.run")
            if (
                run.get("source_artifact_digest") != source.artifact_digest
                or payload.get("study_evidence_digest") != study.evidence_digest
                or payload.get("deployment_artifact_digest")
                != deployment.integrity_digest
            ):
                continue
            token = verify_joint_execution_current_prediction_artifact(
                artifact,
                source=source,
                study=study,
                deployment=deployment,
                as_of=generated_at,
            )
            if path.name != (
                f"joint-execution-current-run-{source.run_id}-"
                f"{token.artifact_digest}.json.gz"
            ):
                raise JointExecutionProbabilityError(
                    "joint current prediction filename/content digest mismatch"
                )
            matches.append(
                (
                    _timestamp(token.generated_at),
                    token.artifact_digest,
                    token,
                )
            )
        return matches

    def _build_current_prediction(
        self,
        source: VerifiedJointExecutionSourceCorpus,
        study: VerifiedJointExecutionProbabilityStudy,
        deployment: VerifiedJointExecutionDeploymentEstimator,
        generated_at: str,
    ) -> VerifiedJointExecutionCurrentPredictionCorpus:
        artifact = build_joint_execution_current_prediction_artifact(
            source,
            study,
            deployment,
            generated_at=generated_at,
        )
        token = verify_joint_execution_current_prediction_artifact(
            artifact,
            source=source,
            study=study,
            deployment=deployment,
            as_of=generated_at,
        )
        self._publish_envelope(
            "current-predictions",
            f"joint-execution-current-run-{source.run_id}",
            artifact,
        )
        return token

    def _ranking_publication_for_tokens(
        self,
        source: VerifiedJointExecutionSourceCorpus,
        predictions: VerifiedJointExecutionCurrentPredictionCorpus,
        study: VerifiedJointExecutionProbabilityStudy,
        deployment: VerifiedJointExecutionDeploymentEstimator,
        promotion: VerifiedProbabilityRankingManualControl,
        *,
        generated_at: str,
    ) -> tuple[VerifiedProbabilityRankingPublication, Path]:
        directory = self.research_directory / "production-rankings-v6"
        matches = self._matching_ranking_publications(
            directory,
            source,
            predictions,
            study,
            deployment,
            promotion,
        )
        if matches:
            selected = min(matches, key=lambda item: (item[0], item[1]))
            return selected[2], selected[3]
        return self._build_ranking_publication(
            source,
            predictions,
            study,
            deployment,
            promotion,
            generated_at,
        )

    def _matching_ranking_publications(
        self,
        directory: Path,
        source: VerifiedJointExecutionSourceCorpus,
        predictions: VerifiedJointExecutionCurrentPredictionCorpus,
        study: VerifiedJointExecutionProbabilityStudy,
        deployment: VerifiedJointExecutionDeploymentEstimator,
        promotion: VerifiedProbabilityRankingManualControl,
    ) -> list[tuple[datetime, str, VerifiedProbabilityRankingPublication, Path]]:
        matches: list[
            tuple[datetime, str, VerifiedProbabilityRankingPublication, Path]
        ] = []
        for path in _managed_artifact_paths(
            directory,
            f"probability-ranking-v6-run-{source.run_id}-*.json.gz",
        ):
            artifact = _read_gzip_json(path)
            payload = _mapping(artifact.get("payload"), "ranking publication.payload")
            if (
                payload.get("joint_source_artifact_digest")
                != source.artifact_digest
                or payload.get("current_prediction_artifact_digest")
                != predictions.artifact_digest
                or payload.get("study_evidence_digest") != study.evidence_digest
                or payload.get("deployment_artifact_digest")
                != deployment.integrity_digest
                or payload.get("promotion_digest") != promotion.integrity_digest
            ):
                continue
            token = verify_probability_ranking_publication_artifact(
                artifact,
                source=source,
                predictions=predictions,
                study=study,
                deployment=deployment,
                promotion=promotion,
            )
            if path.name != (
                f"probability-ranking-v6-run-{source.run_id}-"
                f"{token.artifact_digest}.json.gz"
            ):
                raise ProbabilityRankingError(
                    "v6 ranking publication filename/content digest mismatch"
                )
            matches.append(
                (
                    _timestamp(token.generated_at),
                    token.artifact_digest,
                    token,
                    path,
                )
            )
        return matches

    def _build_ranking_publication(
        self,
        source: VerifiedJointExecutionSourceCorpus,
        predictions: VerifiedJointExecutionCurrentPredictionCorpus,
        study: VerifiedJointExecutionProbabilityStudy,
        deployment: VerifiedJointExecutionDeploymentEstimator,
        promotion: VerifiedProbabilityRankingManualControl,
        generated_at: str,
    ) -> tuple[VerifiedProbabilityRankingPublication, Path]:
        artifact = build_probability_ranking_publication_artifact(
            source,
            predictions,
            study,
            deployment,
            promotion,
            generated_at=generated_at,
        )
        token = verify_probability_ranking_publication_artifact(
            artifact,
            source=source,
            predictions=predictions,
            study=study,
            deployment=deployment,
            promotion=promotion,
        )
        path = self._publish_envelope(
            "production-rankings-v6",
            f"probability-ranking-v6-run-{source.run_id}",
            artifact,
        )
        return token, path

    def _publish_envelope(
        self,
        subdirectory: str,
        prefix: str,
        artifact: Mapping[str, object],
    ) -> Path:
        integrity = _mapping(artifact["integrity"], "artifact.integrity")
        digest = str(integrity["integrity_digest"])
        return self._publish_encoded(
            self.research_directory / subdirectory,
            f"{prefix}-{digest}.json.gz",
            canonical_json_bytes(dict(artifact)),
        )

    def _publish_encoded(self, directory: Path, name: str, encoded: bytes) -> Path:
        if not name.endswith(".json.gz") or ".." in name or "/" in name or "\\" in name:
            raise ValueError("joint execution artifact filename is unsafe")
        compressed = gzip.compress(encoded, compresslevel=9, mtime=0)
        exclusive_atomic_publish(
            directory / name,
            compressed,
            max_bytes=_MAX_COMPRESSED_ARTIFACT_BYTES,
        )
        return directory / name

    @staticmethod
    def _summary(
        *,
        status: str,
        generated_at: str,
        official_status: Mapping[str, object],
        source_archive_count: int = 0,
        current_source_count: int = 0,
        verified_source_count: int = 0,
        mature_h5_session_count: int = 0,
        selection_qualified: bool = False,
        authorization_configured: bool = False,
        authorization_verified: bool = False,
        deployment_verified: bool = False,
        current_prediction_run_id: int | None = None,
        current_prediction_count: int = 0,
        probability_ranking_status: str = "shadow_not_available",
        probability_ranking_shadow_digest: str | None = None,
        probability_ranking_shadow_qualified: bool = False,
        probability_ranking_control_configured: bool = False,
        probability_ranking_control_verified: bool = False,
        probability_ranking_run_id: int | None = None,
        probability_ranking_count: int = 0,
        blockers: tuple[str, ...] = (),
        failures: tuple[str, ...] = (),
    ) -> JointExecutionMaintenanceSummary:
        return JointExecutionMaintenanceSummary(
            status=status,
            source_archive_count=source_archive_count,
            current_source_count=current_source_count,
            verified_source_count=verified_source_count,
            mature_h5_session_count=mature_h5_session_count,
            selection_minimum_session_count=JOINT_EXECUTION_MINIMUM_SELECTION_SESSIONS,
            selection_qualified=selection_qualified,
            authorization_configured=authorization_configured,
            authorization_verified=authorization_verified,
            deployment_verified=deployment_verified,
            current_prediction_run_id=current_prediction_run_id,
            current_prediction_count=current_prediction_count,
            official_execution=dict(official_status),
            generated_at=generated_at,
            probability_ranking_status=probability_ranking_status,
            probability_ranking_shadow_digest=probability_ranking_shadow_digest,
            probability_ranking_shadow_qualified=(
                probability_ranking_shadow_qualified
            ),
            probability_ranking_control_configured=(
                probability_ranking_control_configured
            ),
            probability_ranking_control_verified=(
                probability_ranking_control_verified
            ),
            probability_ranking_run_id=probability_ranking_run_id,
            probability_ranking_count=probability_ranking_count,
            blockers=blockers,
            failures=failures,
        )


def _historical_ranking_source(
    payload: Mapping[str, object],
    sources: Mapping[int, VerifiedJointExecutionSourceCorpus],
    run_id: int,
) -> VerifiedJointExecutionSourceCorpus:
    source = sources.get(run_id)
    if (
        source is None
        or source.artifact_digest != payload.get("joint_source_artifact_digest")
    ):
        raise ProbabilityRankingError("historical v6 ranking source token is unavailable")
    return source


def _canonical_source_inputs(directory: Path) -> tuple[_SourceInput, ...]:
    candidates: dict[str, _SourceInput] = {}
    for info in list_probability_source_snapshots(directory):
        path = Path(str(info["path"])).expanduser().absolute()
        artifact = load_probability_source_snapshot(path)
        if artifact.get("schema_version") != PROBABILITY_SOURCE_ARTIFACT_SCHEMA_VERSION:
            continue
        payload = _mapping(artifact["payload"], "source.payload")
        run = _mapping(payload["run"], "source.run")
        if run.get("mode") != "official":
            continue
        item = _SourceInput(
            artifact=artifact,
            run_id=int(cast(int, run["run_id"])),
            signal_session=str(run["quote_date"]),
            as_of=str(run["as_of"]),
            captured_at=str(artifact["captured_at"]),
            path=path,
        )
        previous = candidates.get(item.signal_session)
        if previous is None or (
            _timestamp(item.as_of),
            _timestamp(item.captured_at),
            item.run_id,
        ) > (
            _timestamp(previous.as_of),
            _timestamp(previous.captured_at),
            previous.run_id,
        ):
            candidates[item.signal_session] = item
    return tuple(
        sorted(candidates.values(), key=lambda item: (item.signal_session, item.run_id))
    )


def _learning_corpus(
    pairs: Sequence[_MaturePair],
) -> VerifiedJointExecutionLearningCorpus:
    if not pairs:
        raise JointExecutionProbabilityError("joint learning requires mature official pairs")
    ordered = tuple(
        sorted(pairs, key=lambda item: (item.source.signal_session, item.source.run_id))
    )
    return build_joint_execution_learning_corpus(
        [item.source for item in ordered],
        [item.outcome for item in ordered],
    )


def _deployment_pairs(
    selected: Sequence[_MaturePair],
    available: Sequence[_MaturePair],
) -> tuple[_MaturePair, ...]:
    selected_ids = {item.source.run_id for item in selected}
    selected_last = max(item.source.signal_session for item in selected)
    output = tuple(
        item
        for item in sorted(
            available,
            key=lambda pair: (pair.source.signal_session, pair.source.run_id),
        )
        if item.source.run_id in selected_ids
        or item.source.signal_session > selected_last
    )
    if not selected_ids <= {item.source.run_id for item in output}:
        raise JointExecutionProbabilityError(
            "deployment corpus dropped an authorized OOS source binding"
        )
    return output


def _pairs_for_exact_bindings(
    value: object,
    available: Sequence[_MaturePair],
    *,
    label: str,
) -> tuple[_MaturePair, ...]:
    if not isinstance(value, list) or not value:
        raise JointExecutionProbabilityError(f"{label} must be a nonempty list")
    run_ids: list[int] = []
    for index, item in enumerate(value):
        binding = _mapping(item, f"{label}[{index}]")
        run_id = binding.get("run_id")
        if isinstance(run_id, bool) or not isinstance(run_id, int) or run_id <= 0:
            raise JointExecutionProbabilityError(f"{label} run_id is invalid")
        run_ids.append(run_id)
    if len(set(run_ids)) != len(run_ids):
        raise JointExecutionProbabilityError(f"{label} contains duplicate run_id")
    by_run = {item.source.run_id: item for item in available}
    if any(run_id not in by_run for run_id in run_ids):
        raise JointExecutionProbabilityError(
            f"{label} source/outcome tokens are unavailable"
        )
    selected = tuple(by_run[run_id] for run_id in run_ids)
    corpus = _learning_corpus(selected)
    if corpus.bindings != value:
        raise JointExecutionProbabilityError(
            f"{label} differs from replayed source/outcome bindings"
        )
    return selected


def _current_prediction_source(
    sources: Sequence[VerifiedJointExecutionSourceCorpus],
    *,
    latest_training_session: str,
    generated_at: str,
) -> VerifiedJointExecutionSourceCorpus | None:
    generated_date = _timestamp(generated_at).date().isoformat()
    eligible = [
        item
        for item in sources
        if item.signal_session > latest_training_session
        and item.signal_session == generated_date
        and _timestamp(item.decision_frozen_at) <= _timestamp(generated_at)
    ]
    if not eligible:
        return None
    return max(eligible, key=lambda item: (item.signal_session, item.run_id))


def _load_pinned_authorization(path: Path, expected_digest: str) -> dict[str, object]:
    if _DIGEST.fullmatch(expected_digest) is None:
        raise JointExecutionProbabilityError("authorization pinned digest is invalid")
    value = decode_json_bytes(
        read_regular_file(path, max_bytes=_MAX_UNCOMPRESSED_ARTIFACT_BYTES)
    )
    artifact = dict(_mapping(value, "authorization"))
    integrity = _mapping(artifact.get("integrity"), "authorization.integrity")
    if integrity.get("integrity_digest") != expected_digest:
        raise JointExecutionProbabilityError(
            "authorization file does not match its out-of-band pinned digest"
        )
    return artifact


def _single_managed_artifact(directory: Path, pattern: str) -> dict[str, object] | None:
    paths = _managed_artifact_paths(directory, pattern)
    if not paths:
        return None
    if len(paths) != 1:
        raise JointExecutionProbabilityError(
            "joint execution managed run has ambiguous immutable artifacts"
        )
    return _read_gzip_json(paths[0])


def _managed_artifact_paths(directory: Path, pattern: str) -> tuple[Path, ...]:
    if not directory.exists():
        return ()
    if (
        not path_has_only_trusted_aliases(directory)
        or not directory.is_dir()
        or directory.is_symlink()
    ):
        raise JointExecutionProbabilityError(
            "joint execution managed directory is unsafe"
        )
    return tuple(sorted(directory.glob(pattern)))


def _read_gzip_json(path: Path) -> dict[str, object]:
    try:
        compressed = read_regular_file(path, max_bytes=_MAX_COMPRESSED_ARTIFACT_BYTES)
        output = bytearray()
        with gzip.GzipFile(fileobj=BytesIO(compressed), mode="rb") as stream:
            while chunk := stream.read(1024 * 1024):
                output.extend(chunk)
                if len(output) > _MAX_UNCOMPRESSED_ARTIFACT_BYTES:
                    raise JointExecutionProbabilityError(
                        "joint execution artifact exceeds uncompressed size limit"
                    )
        value = decode_json_bytes(bytes(output))
        artifact = dict(_mapping(value, "joint execution artifact"))
        if canonical_json_bytes(artifact) != bytes(output):
            raise JointExecutionProbabilityError(
                "joint execution artifact is not canonical JSON"
            )
        return artifact
    except (ArtifactIOError, OSError, EOFError) as exc:
        raise JointExecutionProbabilityError(
            "joint execution managed artifact cannot be read"
        ) from exc


def _current_research_projection(
    authority: _CurrentAuthority,
) -> dict[str, object]:
    evidence = authority.study.payload
    deployment = authority.deployment.payload
    filter_evaluation = build_probability_filter_qualification(
        evidence,
        authority.authorization,
    )
    qualified = probability_filter_qualified(evidence, authority.authorization)
    summary = _current_research_summary(
        authority,
        evidence,
        deployment,
        filter_evaluation,
        qualified,
    )
    return {
        "schema_version": "market-scan-joint-execution-current-projection-v1",
        "record_contract_version": (
            "market-scan-joint-execution-current-record-projection-v1"
        ),
        "run_id": authority.predictions.run_id,
        "status": "calibrated_shadow",
        "default_horizon": JOINT_EXECUTION_HORIZON,
        "primary_target": PROBABILITY_PRIMARY_TARGET,
        "horizons": {
            "1": {},
            str(JOINT_EXECUTION_HORIZON): {PROBABILITY_PRIMARY_TARGET: summary},
            "20": {},
        },
        "generated_at": authority.predictions.generated_at,
        "integrity_digest": authority.predictions.artifact_digest,
        "integrity_notice": (
            "content_address_only_strict_opaque_replay_completed_server_side"
        ),
        "run_binding": _current_run_binding(authority),
        "authority_backend": "joint_execution_opaque_v1",
        "filter_qualified": qualified,
        "production_ranking_effect": "none",
        "automatic_promotion": False,
    }


def _current_research_summary(
    authority: _CurrentAuthority,
    evidence: Mapping[str, object],
    deployment: Mapping[str, object],
    filter_evaluation: Mapping[str, object],
    qualified: bool,
) -> dict[str, object]:
    summary = {name: deepcopy(evidence[name]) for name in _CURRENT_RESEARCH_SUMMARY_KEYS}
    summary.update(
        {
            "filter_qualified": qualified,
            "filter_qualification_evaluation": dict(filter_evaluation),
            "deployment_status": "fresh_verified_joint_execution_deployment",
            "deployment_generated_at": deployment["generated_at"],
            "deployment_artifact_digest": authority.deployment.integrity_digest,
            "deployment_training_cutoff": deployment["training_cutoff"],
            "deployment_calibration_cutoff": deployment["calibration_cutoff"],
            "current_prediction_artifact_digest": authority.predictions.artifact_digest,
            "current_prediction_count": len(authority.predictions),
            "production_ranking_effect": "none",
            "automatic_promotion": False,
        }
    )
    return summary


def _current_run_binding(authority: _CurrentAuthority) -> dict[str, object]:
    artifact = authority.source_input.artifact
    payload = _mapping(artifact["payload"], "source.payload")
    run = _mapping(payload["run"], "source.run")
    cohort = _mapping(payload["cohort"], "source.cohort")
    integrity = _mapping(artifact["integrity"], "source.integrity")
    rule_version = str(run["rule_version"])
    return {
        "schema_version": "market-scan-probability-run-binding-v1",
        "binding_status": "verified",
        "legacy": False,
        "run_id": authority.predictions.run_id,
        "mode": run["mode"],
        "scope": run["scope"],
        "rule_version": rule_version,
        "quote_date": run["quote_date"],
        "data_date": run["data_date"],
        "scan_rule_hash": rule_version.rsplit(":", 1)[-1],
        "production_score_rule_version": run["production_score_rule_version"],
        "production_score_spec_hash": run["production_score_spec_hash"],
        "source_integrity_digest": integrity["integrity_digest"],
        "cohort_contract": dict(cohort),
        "record_contract_version": (
            "market-scan-joint-execution-current-record-projection-v1"
        ),
        "source_snapshot_digest": authority.source.source_snapshot_digest,
        "joint_source_artifact_digest": authority.source.artifact_digest,
        "decision_identity_digest": authority.source.decision_identity_digest,
        "decision_membership_digest": authority.source.decision_membership_digest,
    }


def _current_record_projection(
    predictions: VerifiedJointExecutionCurrentPredictionCorpus,
    *,
    symbols: Sequence[str] | None,
) -> dict[str, dict[str, object]]:
    selected = frozenset(symbols) if symbols is not None else None
    output: dict[str, dict[str, object]] = {}
    for row in predictions.records:
        symbol = str(row["symbol"])
        if selected is not None and symbol not in selected:
            continue
        bias = cast(list[object], row["calibration_bias_interval"])
        adjusted = cast(list[object], row["calibration_adjusted_probability_interval"])
        details = deepcopy(row)
        details.update(
            {
                "status": "calibrated_shadow",
                "target": PROBABILITY_PRIMARY_TARGET,
                "target_definition": "joint_execution_action_positive_net_excess",
                "horizon": JOINT_EXECUTION_HORIZON,
                "holding_period_sessions": JOINT_EXECUTION_HORIZON,
                "target_session_offset": JOINT_EXECUTION_HORIZON + 1,
                "calibration_bias_interval": _interval_projection(
                    bias,
                    semantics="signed_observed_rate_minus_probability_bias",
                ),
                "calibration_adjusted_probability_interval": _interval_projection(
                    adjusted,
                    semantics=(
                        "calibration_adjusted_probability_interval_not_individual_outcome_interval"
                    ),
                ),
                "filter_qualified": True,
                "production_ranking_effect": "none",
            }
        )
        output[symbol] = {
            str(JOINT_EXECUTION_HORIZON): {
                PROBABILITY_PRIMARY_TARGET: details,
            }
        }
    return output


def _interval_projection(
    value: Sequence[object],
    *,
    semantics: str,
) -> dict[str, object]:
    if len(value) != 2:
        raise JointExecutionProbabilityError("joint probability interval is invalid")
    return {
        "level": 0.95,
        "lower": value[0],
        "upper": value[1],
        "method": "date_block_bootstrap_calibration_offset",
        "semantics": semantics,
    }


def _inactive_production_ranking_projection(
    run_id: int, reason: str | None = None
) -> dict[str, object]:
    context: dict[str, object] = {
        "contract_version": "market-scan-probability-ranking-projection-v1",
        "status": "inactive",
        "run_id": run_id,
        "score_rule_version": PROBABILITY_RANKING_SCORE_RULE_VERSION,
        "base_v5_mutated": False,
        "historical_ranks_mutated": False,
    }
    if reason is not None:
        context["reason"] = reason
    return context


def _not_generated_joint_projection(
    run_id: int,
    status: Mapping[str, object],
) -> dict[str, object]:
    return {
        "schema_version": "market-scan-joint-execution-current-projection-v1",
        "run_id": run_id,
        "status": "not_generated",
        "availability": str(status.get("status") or "maintenance_not_run"),
        "pipeline_stage": str(status.get("status") or "maintenance_not_run"),
        "default_horizon": JOINT_EXECUTION_HORIZON,
        "primary_target": PROBABILITY_PRIMARY_TARGET,
        "horizons": {"1": {}, "5": {}, "20": {}},
        "generated_at": status.get("generated_at"),
        "run_binding": None,
        "authority_backend": "joint_execution_opaque_v1",
        "filter_qualified": False,
        "production_ranking_effect": "none",
        "automatic_promotion": False,
        "joint_execution_evidence": dict(status),
        "limitations": list(cast(Sequence[object], status.get("blockers") or ())),
    }


def _mapping(value: object, label: str) -> Mapping[str, object]:
    if not isinstance(value, Mapping):
        raise JointExecutionProbabilityError(f"{label} must be an object")
    return cast(Mapping[str, object], value)


def _timestamp(value: str) -> datetime:
    parsed = datetime.fromisoformat(value)
    if parsed.tzinfo is None or parsed.utcoffset() is None:
        raise ValueError("joint execution timestamp must include timezone")
    return parsed


def _short_error(exc: BaseException) -> str:
    return f"{type(exc).__name__}: {' '.join(str(exc).split())[:600]}"


__all__ = [
    "JOINT_EXECUTION_MAINTENANCE_CONTRACT_VERSION",
    "JOINT_EXECUTION_MINIMUM_SELECTION_SESSIONS",
    "JOINT_EXECUTION_RESEARCH_RELATIVE_PATH",
    "JointExecutionMaintenanceSummary",
    "MarketScanJointExecutionMaintenanceService",
]
