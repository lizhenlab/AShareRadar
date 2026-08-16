"""Read-only progress projection for archived live probability sources."""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from copy import deepcopy
from datetime import datetime
import hashlib
import json
from pathlib import Path
import stat
from threading import Lock, RLock
from typing import cast

from app.artifacts.io import path_has_only_trusted_aliases
from app.services.market_scan_probability import (
    PROBABILITY_FEATURE_VERSION,
    PROBABILITY_LABEL_VERSION,
    PROBABILITY_MODEL_VERSION,
    ProbabilityConfig,
    build_probability_contract,
    stable_probability_hash,
)
from app.services.market_scan_probability_research import (
    PROBABILITY_ABSOLUTE_TARGET,
    PROBABILITY_PRIMARY_TARGET,
)
from app.services.market_scan_probability_source import (
    PROBABILITY_SOURCE_ARTIFACT_SCHEMA_VERSION,
    PROBABILITY_SOURCE_PAYLOAD_CONTRACT_VERSION,
    ProbabilitySourceError,
    load_probability_source_snapshot,
)
from app.services.market_scan_probability_outcomes import (
    ProbabilityOutcomeError,
    ProbabilityOutcomeSemanticDriftError,
    load_probability_outcome_artifact,
)
from app.services.market_scan_probability_fit_assessment import (
    PROBABILITY_FIT_MAX_SESSIONS,
    load_probability_fit_assessment,
)
from app.services.market_scan_probability_labels import probability_label_contract
from app.services.market_scan_probability_store import not_generated_probability_research
from app.services.trading_calendar import (
    TradingCalendarCoverageError,
    latest_expected_daily_kline_date,
    next_trade_dates,
)


PROBABILITY_SOURCE_RESEARCH_SCHEMA_VERSION = "market-scan-probability-source-research-v1"
_HORIZONS = (1, 5, 20)
_TARGETS = (PROBABILITY_PRIMARY_TARGET, PROBABILITY_ABSOLUTE_TARGET)
_FileFingerprint = tuple[Path, int, int, int, int, int, int]
_DirectorySnapshot = tuple[tuple[int, int, int, int] | None, tuple[_FileFingerprint, ...]]
_SourceSummary = dict[str, object]
_OutcomeSummary = dict[str, object]
_FitSummary = dict[str, object]
_SOURCE_CORPUS_CONTRACT_VERSION = "market-scan-probability-source-corpus-v1"
_STABLE_SNAPSHOT_READ_ATTEMPTS = 3


class MarketScanProbabilitySourceResearchStore:
    """Cache verified source manifests and expose honest sample progress."""

    def __init__(
        self,
        directory: str | Path,
        *,
        outcome_directory: str | Path | None = None,
        fit_directory: str | Path | None = None,
    ) -> None:
        self.directory = Path(directory).expanduser().absolute()
        self.outcome_directory = (
            Path(outcome_directory).expanduser().absolute()
            if outcome_directory is not None
            else self.directory.parent / "market_scan_probability_outcomes"
        )
        self.fit_directory = (
            Path(fit_directory).expanduser().absolute()
            if fit_directory is not None
            else self.directory.parent / "market_scan_probability_fit"
        )
        self._lock = RLock()
        self._refresh_lock = Lock()
        self._snapshot: tuple[_DirectorySnapshot, _DirectorySnapshot, _DirectorySnapshot, str] | None = None
        self._research_by_run: dict[int, dict[str, object]] = {}
        self._summary_by_fingerprint: dict[_FileFingerprint, _SourceSummary] = {}
        self._outcome_by_fingerprint: dict[_FileFingerprint, _OutcomeSummary] = {}
        self._fit_by_fingerprint: dict[_FileFingerprint, _FitSummary] = {}
        self._excluded_outcome_run_ids: frozenset[int] = frozenset()

    def research_projection(self, run_id: int) -> dict[str, object]:
        self._refresh_if_changed(blocking=False)
        with self._lock:
            return deepcopy(
                self._research_by_run.get(run_id, not_generated_probability_research(run_id))
            )

    def preload(self) -> int:
        """Verify archives and atomically publish the compact in-memory run index."""
        self._refresh_if_changed(blocking=True)
        with self._lock:
            return len(self._research_by_run)

    def _refresh_if_changed(self, *, blocking: bool) -> None:
        observed = self._observed_snapshot()
        with self._lock:
            if self._snapshot == observed:
                return
        acquired = self._refresh_lock.acquire(blocking=blocking)
        if not acquired:
            # Non-blocking warm reads return the previous complete projection
            # while the sole refresher verifies/decompresses the next snapshot.
            return
        try:
            with self._lock:
                candidate_cache, candidate_outcomes, candidate_fits = self._candidate_caches()
            for _attempt in range(_STABLE_SNAPSHOT_READ_ATTEMPTS):
                source_snapshot = _directory_snapshot(
                    self.directory, "market-scan-probability-source-run-*.json.gz"
                )
                outcome_snapshot = _directory_snapshot(
                    self.outcome_directory, "market-scan-probability-outcomes-run-*.json.gz"
                )
                fit_snapshot = _directory_snapshot(
                    self.fit_directory, "market-scan-probability-fit-through-run-*.json.gz"
                )
                effective_as_of = latest_expected_daily_kline_date().isoformat()
                snapshot = source_snapshot, outcome_snapshot, fit_snapshot, effective_as_of
                with self._lock:
                    if self._snapshot == snapshot:
                        return
                # Deep verification/decompression deliberately happens outside the
                # projection-state lock. Readers only ever see the previous complete
                # index or the new complete index, never a partially refreshed cache.
                summaries = _snapshot_summaries(source_snapshot, candidate_cache)
                outcomes, excluded_outcome_run_ids = _snapshot_outcomes(
                    outcome_snapshot,
                    candidate_outcomes,
                )
                fits = _snapshot_fits(fit_snapshot, candidate_fits)
                candidate_cache.update(summaries)
                candidate_outcomes.update(outcomes)
                candidate_fits.update(fits)
                if (
                    _directory_snapshot(self.directory, "market-scan-probability-source-run-*.json.gz"),
                    _directory_snapshot(self.outcome_directory, "market-scan-probability-outcomes-run-*.json.gz"),
                    _directory_snapshot(self.fit_directory, "market-scan-probability-fit-through-run-*.json.gz"),
                    latest_expected_daily_kline_date().isoformat(),
                ) != snapshot:
                    continue
                self._commit_refresh(
                    snapshot,
                    summaries,
                    outcomes,
                    fits,
                    excluded_outcome_run_ids,
                    effective_as_of,
                )
                return
            raise ProbabilitySourceError("上涨概率 source archive 目录在多次读取期间持续变化，请重试")
        finally:
            self._refresh_lock.release()

    def _commit_refresh(
        self,
        snapshot: tuple[_DirectorySnapshot, _DirectorySnapshot, _DirectorySnapshot, str],
        summaries: dict[_FileFingerprint, _SourceSummary],
        outcomes: dict[_FileFingerprint, _OutcomeSummary],
        fits: dict[_FileFingerprint, _FitSummary],
        excluded_run_ids: frozenset[int],
        effective_as_of: str,
    ) -> None:
        index = _research_index(
            tuple(summaries.values()),
            tuple(outcomes.values()),
            tuple(fits.values()),
            effective_as_of=effective_as_of,
            excluded_outcome_run_ids=excluded_run_ids,
        )
        with self._lock:
            self._research_by_run = index
            self._summary_by_fingerprint = summaries
            self._outcome_by_fingerprint = outcomes
            self._fit_by_fingerprint = fits
            self._excluded_outcome_run_ids = excluded_run_ids
            self._snapshot = snapshot

    def _candidate_caches(
        self,
    ) -> tuple[
        dict[_FileFingerprint, _SourceSummary],
        dict[_FileFingerprint, _OutcomeSummary],
        dict[_FileFingerprint, _FitSummary],
    ]:
        return (
            dict(self._summary_by_fingerprint),
            dict(self._outcome_by_fingerprint),
            dict(self._fit_by_fingerprint),
        )

    def _observed_snapshot(
        self,
    ) -> tuple[_DirectorySnapshot, _DirectorySnapshot, _DirectorySnapshot, str]:
        return (
            _directory_snapshot(self.directory, "market-scan-probability-source-run-*.json.gz"),
            _directory_snapshot(
                self.outcome_directory, "market-scan-probability-outcomes-run-*.json.gz",
            ),
            _directory_snapshot(
                self.fit_directory, "market-scan-probability-fit-through-run-*.json.gz",
            ),
            latest_expected_daily_kline_date().isoformat(),
        )


def _snapshot_summaries(
    snapshot: _DirectorySnapshot,
    cache: Mapping[_FileFingerprint, _SourceSummary],
) -> dict[_FileFingerprint, _SourceSummary]:
    return {fingerprint: _fingerprint_summary(fingerprint, cache) for fingerprint in snapshot[1]}


def _fingerprint_summary(
    fingerprint: _FileFingerprint,
    cache: Mapping[_FileFingerprint, _SourceSummary],
) -> _SourceSummary:
    cached = cache.get(fingerprint)
    if cached is not None:
        return cached
    return _compact_source_summary(load_probability_source_snapshot(fingerprint[0]))


def _snapshot_outcomes(
    snapshot: _DirectorySnapshot,
    cache: Mapping[_FileFingerprint, _OutcomeSummary],
) -> tuple[dict[_FileFingerprint, _OutcomeSummary], frozenset[int]]:
    outcomes: dict[_FileFingerprint, _OutcomeSummary] = {}
    excluded_run_ids: set[int] = set()
    for fingerprint in snapshot[1]:
        try:
            outcomes[fingerprint] = _fingerprint_outcome(fingerprint, cache)
        except ProbabilityOutcomeSemanticDriftError as exc:
            # Intact legacy evidence remains on disk for audit, but cannot enter
            # the current replay/probability projection.
            if exc.run_id is None:
                raise ProbabilitySourceError("legacy outcome semantic drift 缺少 run 绑定") from exc
            excluded_run_ids.add(exc.run_id)
            continue
    return outcomes, frozenset(excluded_run_ids)


def _fingerprint_outcome(
    fingerprint: _FileFingerprint,
    cache: Mapping[_FileFingerprint, _OutcomeSummary],
) -> _OutcomeSummary:
    cached = cache.get(fingerprint)
    if cached is not None:
        return cached
    try:
        return _compact_outcome_summary(load_probability_outcome_artifact(fingerprint[0]))
    except ProbabilityOutcomeSemanticDriftError:
        raise
    except ProbabilityOutcomeError as exc:
        raise ProbabilitySourceError("上涨概率 outcome archive 校验失败") from exc


def _snapshot_fits(
    snapshot: _DirectorySnapshot,
    cache: Mapping[_FileFingerprint, _FitSummary],
) -> dict[_FileFingerprint, _FitSummary]:
    return {fingerprint: _fingerprint_fit(fingerprint, cache) for fingerprint in snapshot[1]}


def _fingerprint_fit(
    fingerprint: _FileFingerprint,
    cache: Mapping[_FileFingerprint, _FitSummary],
) -> _FitSummary:
    cached = cache.get(fingerprint)
    if cached is not None:
        return cached
    try:
        artifact = load_probability_fit_assessment(fingerprint[0])
    except ValueError as exc:
        raise ProbabilitySourceError("上涨概率 fit assessment 校验失败") from exc
    return _compact_fit_summary(artifact)


def _research_index(
    summaries: Sequence[_SourceSummary],
    outcomes: Sequence[_OutcomeSummary] = (),
    fits: Sequence[_FitSummary] = (),
    *,
    effective_as_of: str | None = None,
    excluded_outcome_run_ids: frozenset[int] = frozenset(),
) -> dict[int, dict[str, object]]:
    newest_by_run = _newest_capture_by_run(summaries)
    canonical = _canonical_sources(tuple(newest_by_run.values()))
    corpora = _cumulative_source_corpora(tuple(canonical.values()))
    selected_outcomes = _newest_outcome_by_run(outcomes)
    progress = _outcome_progress_by_run(
        tuple(canonical.values()),
        outcomes,
        effective_as_of=effective_as_of or latest_expected_daily_kline_date().isoformat(),
    )
    fit_by_run = _trusted_fits_by_run(
        _newest_fit_by_run(fits),
        tuple(canonical.values()),
        excluded_outcome_run_ids,
    )
    return {
        run_id: _source_research(
            summary,
            corpus=corpora[run_id],
            progress=progress[run_id],
            fit=_fit_for_source(
                summary,
                selected_outcomes.get(run_id),
                fit_by_run.get(run_id),
                canonical_sources=tuple(canonical.values()),
                outcomes=selected_outcomes,
            ),
            excluded_outcome_semantic_drift=run_id in excluded_outcome_run_ids,
        )
        for run_id, summary in canonical.items()
    }


def _fit_for_source(
    source: _SourceSummary,
    outcome: _OutcomeSummary | None,
    fit: _FitSummary | None,
    *,
    canonical_sources: Sequence[_SourceSummary],
    outcomes: Mapping[int, _OutcomeSummary],
) -> _FitSummary | None:
    if fit is not None and _cohort_key(fit) != _cohort_key(source):
        raise ProbabilitySourceError(f"run {_run_id(source)} fit/source cohort 不一致")
    if fit is not None and fit.get("through_source_digest") != source.get("integrity_digest"):
        raise ProbabilitySourceError(f"run {_run_id(source)} fit/source content digest 不一致")
    if fit is not None and (
        outcome is None or fit.get("through_outcome_digest") != outcome.get("integrity_digest")
    ):
        raise ProbabilitySourceError(f"run {_run_id(source)} fit/outcome content digest 不一致")
    if fit is not None and fit.get("input_pair_digest") != _fit_input_pair_digest(
        source,
        canonical_sources,
        outcomes,
    ):
        raise ProbabilitySourceError(f"run {_run_id(source)} fit rolling corpus digest 不一致")
    return fit


def _trusted_fits_by_run(
    fits: Mapping[int, _FitSummary],
    sources: Sequence[_SourceSummary],
    excluded_run_ids: frozenset[int],
) -> dict[int, _FitSummary]:
    if not excluded_run_ids:
        return dict(fits)
    source_by_run = {_run_id(source): source for source in sources}
    trusted: dict[int, _FitSummary] = {}
    for run_id, fit in fits.items():
        through = source_by_run.get(run_id)
        if through is None:
            trusted[run_id] = fit
            continue
        dependent_ids = {
            _run_id(source)
            for source in sources
            if _source_contract_key(source) == _source_contract_key(through)
            and _source_progress_order(source) <= _source_progress_order(through)
        }
        if dependent_ids.isdisjoint(excluded_run_ids):
            trusted[run_id] = fit
    return trusted


def _fit_input_pair_digest(
    through: _SourceSummary,
    sources: Sequence[_SourceSummary],
    outcomes: Mapping[int, _OutcomeSummary],
) -> str:
    candidates = sorted(
        (
            source
            for source in sources
            if _source_contract_key(source) == _source_contract_key(through)
            and _source_progress_order(source) <= _source_progress_order(through)
        ),
        key=_source_progress_order,
    )[-PROBABILITY_FIT_MAX_SESSIONS:]
    pairs: list[tuple[str, str]] = []
    for source in candidates:
        outcome = outcomes.get(_run_id(source))
        if outcome is None:
            raise ProbabilitySourceError("上涨概率 fit rolling corpus 缺少 outcome")
        pairs.append((str(source["integrity_digest"]), str(outcome["integrity_digest"])))
    return stable_probability_hash(pairs)


def _newest_capture_by_run(
    summaries: Sequence[_SourceSummary],
) -> dict[int, _SourceSummary]:
    grouped: dict[int, list[_SourceSummary]] = {}
    for summary in summaries:
        grouped.setdefault(_run_id(summary), []).append(summary)
    selected: dict[int, _SourceSummary] = {}
    for run_id, captures in grouped.items():
        newest_at = max(_timestamp_order(item["captured_at"], "captured_at") for item in captures)
        newest = [
            item
            for item in captures
            if _timestamp_order(item["captured_at"], "captured_at") == newest_at
        ]
        if len(newest) != 1:
            raise ProbabilitySourceError(f"run {run_id} 存在同 captured_at 的冲突 source archives")
        selected[run_id] = newest[0]
    return selected


def _canonical_sources(
    summaries: Sequence[_SourceSummary],
) -> dict[int, _SourceSummary]:
    grouped: dict[tuple[str, str, str, str, str, str], list[_SourceSummary]] = {}
    for summary in summaries:
        key = (*_source_contract_key(summary), str(summary["quote_date"]))
        grouped.setdefault(key, []).append(summary)
    canonical = [max(values, key=_source_canonical_order) for values in grouped.values()]
    return {_run_id(summary): summary for summary in canonical}


def _source_research(
    summary: _SourceSummary,
    *,
    corpus: Mapping[str, object],
    progress: Mapping[str, object],
    fit: Mapping[str, object] | None,
    excluded_outcome_semantic_drift: bool = False,
) -> dict[str, object]:
    progress_horizons = _mapping(progress["horizons"], "progress.horizons")
    horizons = {
        str(horizon): {
            target: _source_horizon_summary(
                horizon,
                target,
                progress=_mapping(progress_horizons[str(horizon)], f"progress.horizons.{horizon}"),
                fit=_fit_evidence(fit, horizon, target),
            )
            for target in _TARGETS
        }
        for horizon in _HORIZONS
    }
    primary = _mapping(
        _mapping(horizons["5"], "horizons.5")[PROBABILITY_PRIMARY_TARGET],
        "horizons.5.primary",
    )
    return {
        "schema_version": PROBABILITY_SOURCE_RESEARCH_SCHEMA_VERSION,
        "run_id": summary["run_id"],
        "status": "insufficient_data",
        "default_horizon": 5,
        "primary_target": PROBABILITY_PRIMARY_TARGET,
        "horizons": horizons,
        "generated_at": corpus["generated_at"],
        "integrity_digest": corpus["integrity_digest"],
        "source_corpus": dict(corpus),
        "outcome_progress": dict(progress),
        "pipeline_stage": primary["pipeline_stage"],
        "next_maturity_date": primary["next_maturity_date"],
        "maintenance_status": primary["maintenance_status"],
        "fit_status": primary["fit_status"],
        "fit_selection_qualified": primary["fit_selection_qualified"],
        "fit_selection_qualification": primary["fit_selection_qualification"],
        "selection_qualified": False,
        "selection_status": primary["selection_status"],
        "integrity_notice": "source_corpus_integrity_digest_not_probability_model_evidence",
        "production_ranking_effect": "none",
        "automatic_promotion": False,
        "outcome_evidence_status": (
            "legacy_semantic_drift_excluded"
            if excluded_outcome_semantic_drift
            else "current_replay_only"
        ),
        "run_binding": _source_run_binding(summary),
    }


def _source_run_binding(summary: Mapping[str, object]) -> dict[str, object]:
    cohort = _mapping(summary["cohort"], "source.cohort")
    score_contract = _mapping(summary.get("score_contract") or {}, "source.score_contract")
    rule_version = str(cohort["rule_version"])
    suffix = rule_version.rsplit(":", 1)[-1]
    scan_hash = suffix if len(suffix) == 64 and all(value in "0123456789abcdef" for value in suffix) else None
    production_hash = score_contract.get("production_score_spec_hash")
    production_rule = score_contract.get("production_score_rule_version")
    verified = bool(
        summary.get("artifact_schema_version") == PROBABILITY_SOURCE_ARTIFACT_SCHEMA_VERSION
        and summary.get("payload_contract_version") == PROBABILITY_SOURCE_PAYLOAD_CONTRACT_VERSION
        and scan_hash is not None
        and isinstance(production_rule, str)
        and production_rule
        and isinstance(production_hash, str)
        and len(production_hash) == 64
        and all(value in "0123456789abcdef" for value in production_hash)
    )
    return {
        "schema_version": "market-scan-probability-run-binding-v1",
        "binding_status": "verified" if verified else "legacy_unbound",
        "legacy": not verified,
        "run_id": summary["run_id"],
        "mode": cohort["mode"],
        "scope": cohort["scope"],
        "rule_version": rule_version,
        "quote_date": summary["quote_date"],
        "data_date": summary["quote_date"],
        "scan_rule_hash": scan_hash,
        "production_score_rule_version": production_rule,
        "production_score_spec_hash": production_hash,
        "cohort_contract": dict(cohort),
        "record_contract_version": "market-scan-probability-source-corpus-v1",
        "source_integrity_digest": summary["integrity_digest"],
    }


def _source_horizon_summary(
    horizon: int,
    target: str,
    *,
    progress: Mapping[str, object],
    fit: Mapping[str, object] | None,
) -> dict[str, object]:
    label_contract = probability_label_contract()
    config = ProbabilityConfig(
        horizon=horizon,
        target="net_excess_positive" if target == PROBABILITY_PRIMARY_TARGET else "net_return_positive",
        cost_model_version=str(label_contract["cost_model_version"]),
        label_contract=label_contract,
    )
    counts = _progress_counts(progress, config)
    projection = _source_fit_projection(fit, _pipeline_stage(progress, config))
    stage = str(projection["pipeline_stage"])
    limitations = _progress_limitations(stage)
    return {
        "status": "insufficient_data",
        "probability": None,
        "horizon": horizon,
        "target": target,
        "target_definition": target,
        "base_rate": None,
        "counts": _source_partition_counts(counts),
        "contract": build_probability_contract(config),
        "model_version": PROBABILITY_MODEL_VERSION,
        "feature_version": PROBABILITY_FEATURE_VERSION,
        "label_version": PROBABILITY_LABEL_VERSION,
        "cost_model_version": config.cost_model_version,
        "point_in_time_evidence": {
            "eligible_count": counts["observation_count"],
            "verified_count": counts["observation_count"],
            "coverage": 1.0,
        },
        **projection,
        "next_maturity_date": progress.get("next_maturity_date"),
        "maintenance_status": _maintenance_status(stage, progress),
        "limitations": limitations,
        "automatic_promotion": False,
        "filter_qualified": False,
        "filter_qualification_evaluation": None,
    }


def _source_partition_counts(counts: Mapping[str, int | float]) -> dict[str, int | float]:
    return {
        "training_session_count": 0,
        "calibration_session_count": 0,
        "test_session_count": 0,
        **counts,
        "walk_forward_fold_count": 0,
        "evaluated_fold_count": 0,
        "out_of_sample_session_count": 0,
        "out_of_sample_observation_count": 0,
        "unused_tail_session_count": 0,
    }


def _source_fit_projection(
    fit: Mapping[str, object] | None,
    default_stage: str,
) -> dict[str, object]:
    sampled = bool(
        fit is not None
        and fit.get("fit_status") == "sampled_oos_assessment"
        and fit.get("deterministic_replay_verified") is True
    )
    if sampled:
        assert fit is not None
        return {
            "pipeline_stage": "sampled_fit_assessed",
            "training_cutoff": fit.get("training_cutoff"),
            "fit_status": "sampled_oos_assessment",
            "fit_evidence_digest": fit.get("evidence_digest"),
            "fit_replay_verified": True,
            "fit_selection_qualified": False,
            "fit_selection_qualification": {
                "passed": False,
                "reason": "sampled_market_benchmark_not_full_market_contract",
            },
            "selection_qualified": False,
            "selection_status": "projection_pending",
        }
    return {
        "pipeline_stage": "fit_insufficient" if fit is not None else default_stage,
        "training_cutoff": fit.get("training_cutoff") if fit is not None else None,
        "fit_status": fit.get("fit_status") if fit is not None else _fit_status(default_stage),
        "fit_evidence_digest": fit.get("evidence_digest") if fit is not None else None,
        "fit_replay_verified": False,
        "fit_selection_qualified": False,
        "fit_selection_qualification": None,
        "selection_qualified": False,
        "selection_status": "fail_closed_no_verified_fit_evidence",
    }


def _progress_counts(
    progress: Mapping[str, object],
    config: ProbabilityConfig,
) -> dict[str, int | float]:
    return {
        "available_independent_session_count": int(cast(int, progress["available_independent_session_count"])),
        "archived_independent_session_count": int(cast(int, progress["archived_independent_session_count"])),
        "mature_label_session_count": int(cast(int, progress["mature_label_session_count"])),
        "observation_count": int(cast(int, progress["observation_count"])),
        "mature_observation_count": int(cast(int, progress["mature_observation_count"])),
        "eligible_observation_count": int(cast(int, progress["eligible_observation_count"])),
        "label_coverage": float(cast(float, progress["label_coverage"])),
        "minimum_label_coverage": config.minimum_label_coverage,
        "minimum_required_independent_session_count": _required_session_count(config),
    }


def _required_session_count(config: ProbabilityConfig) -> int:
    return (
        config.minimum_train_sessions
        + config.minimum_calibration_sessions
        + config.minimum_test_sessions
        + 2 * config.effective_gap_sessions
    )


def _pipeline_stage(progress: Mapping[str, object], config: ProbabilityConfig) -> str:
    if int(cast(int, progress["outcome_artifact_count"])) == 0:
        return "source_archived"
    mature = int(cast(int, progress["mature_label_session_count"]))
    if mature == 0:
        return "waiting_labels"
    available = int(cast(int, progress["available_independent_session_count"]))
    coverage = float(cast(float, progress["label_coverage"]))
    if available < _required_session_count(config) or coverage < config.minimum_label_coverage:
        return "fit_insufficient"
    return "labels_matured"


def _fit_status(stage: str) -> str:
    return {
        "source_archived": "not_started",
        "waiting_labels": "not_started",
        "fit_insufficient": "not_fitted",
        "labels_matured": "ready_for_verified_fit",
        "projection_pending": "fitted_oos",
    }.get(stage, "not_fitted")


def _maintenance_status(stage: str, progress: Mapping[str, object]) -> str:
    if bool(progress.get("maintenance_due")):
        return "outcome_maintenance_overdue"
    if stage == "source_archived":
        return "initial_outcome_pending"
    if stage == "waiting_labels":
        return "waiting_fixed_horizon_labels"
    if bool(progress.get("has_mature_missing_bars")):
        return "mature_labels_missing_fixed_bars"
    if stage == "fit_insufficient":
        return "labels_matured_fit_threshold_pending"
    if stage == "sampled_fit_assessed":
        return "individual_probability_projection_pending"
    return "verified_fit_pending"


def _progress_limitations(stage: str) -> list[str]:
    codes = ["live_point_in_time_source_archived"]
    if stage in {"source_archived", "waiting_labels"}:
        codes.append("waiting_fixed_horizon_labels")
    if stage in {"source_archived", "waiting_labels", "fit_insufficient"}:
        codes.extend(("minimum_independent_sessions", "minimum_label_coverage"))
    if stage == "labels_matured":
        codes.append("verified_fit_assessment_pending")
    if stage in {"projection_pending", "sampled_fit_assessed"}:
        codes.append("individual_probability_projection_not_published")
        codes.append("bounded_sample_benchmark_not_full_market_contract_selection_forbidden")
    codes.append("shadow_only_no_production_ranking_effect")
    return codes


def _fit_evidence(
    fit: Mapping[str, object] | None,
    horizon: int,
    target: str,
) -> Mapping[str, object] | None:
    if fit is None:
        return None
    horizons = _mapping(fit["horizons"], "fit.horizons")
    raw_horizon = horizons.get(str(horizon))
    if not isinstance(raw_horizon, Mapping):
        return None
    raw = raw_horizon.get(target)
    if not isinstance(raw, Mapping):
        return None
    horizon_fitted = raw.get("fit_status") == "fitted_oos"
    return {
        **raw,
        "fit_status": fit["fit_status"] if horizon_fitted else raw.get("fit_status", "not_fitted"),
        "deterministic_replay_verified": (
            horizon_fitted
            and
            fit["fit_replay_verified"] is True
            and raw.get("deterministic_replay_verified") is True
        ),
        "training_cutoff": fit["training_cutoff"],
        "evidence_digest": fit["integrity_digest"],
        "selection_qualified": False,
        "selection_qualification": deepcopy(fit["fit_selection_qualification"]),
    }


def _compact_source_summary(artifact: Mapping[str, object]) -> _SourceSummary:
    payload = _mapping(artifact["payload"], "payload")
    run = _mapping(payload["run"], "payload.run")
    cohort = _mapping(payload["cohort"], "payload.cohort")
    quality = _mapping(payload["quality"], "payload.quality")
    integrity = _mapping(artifact["integrity"], "integrity")
    score_semantics = _mapping(payload["score_semantics"], "payload.score_semantics")
    return {
        "artifact_schema_version": str(artifact["schema_version"]),
        "payload_contract_version": str(payload["contract_version"]),
        "captured_at": str(artifact["captured_at"]),
        "run_id": int(cast(int, run["run_id"])),
        "quote_date": str(run["quote_date"]),
        "as_of": str(run["as_of"]),
        "cohort": {
            "mode": str(cohort["mode"]),
            "scope": str(cohort["scope"]),
            "rule_version": str(cohort["rule_version"]),
        },
        "score_contract": {
            "production_score_rule_version": score_semantics.get("production_rule_version"),
            "production_score_spec_hash": score_semantics.get("production_score_spec_hash"),
        },
        "record_count": int(cast(int, quality["record_count"])),
        "integrity_digest": str(integrity["integrity_digest"]),
    }


def _compact_outcome_summary(artifact: Mapping[str, object]) -> _OutcomeSummary:
    payload = _mapping(artifact["payload"], "outcome.payload")
    source = _mapping(payload["source"], "outcome.source")
    cohort = _mapping(payload["cohort"], "outcome.cohort")
    quality = _mapping(payload["quality"], "outcome.quality")
    integrity = _mapping(artifact["integrity"], "outcome.integrity")
    return {
        "run_id": int(cast(int, source["run_id"])),
        "generated_at": str(artifact["generated_at"]),
        "as_of_date": str(payload["as_of_date"]),
        "source_integrity_digest": str(source["integrity_digest"]),
        "integrity_digest": str(integrity["integrity_digest"]),
        "cohort": {
            "mode": str(cohort["mode"]),
            "scope": str(cohort["scope"]),
            "rule_version": str(cohort["rule_version"]),
        },
        "horizons": deepcopy(_mapping(quality["horizons"], "outcome.quality.horizons")),
    }


def _compact_fit_summary(artifact: Mapping[str, object]) -> _FitSummary:
    payload = _mapping(artifact["payload"], "fit.payload")
    integrity = _mapping(artifact["integrity"], "fit.integrity")
    members = cast(Sequence[Mapping[str, object]], payload["members"])
    if not members:
        raise ProbabilitySourceError("上涨概率 fit assessment members 缺失")
    return {
        "through_run_id": int(cast(int, payload["through_run_id"])),
        "generated_at": str(artifact["generated_at"]),
        "cohort": deepcopy(_mapping(payload["cohort"], "fit.cohort")),
        "horizons": deepcopy(_mapping(payload["horizons"], "fit.horizons")),
        "fit_status": str(payload["fit_status"]),
        "fit_replay_verified": payload["fit_replay_verified"] is True,
        "fit_selection_qualification": deepcopy(
            _mapping(payload["fit_selection_qualification"], "fit.selection")
        ),
        "training_cutoff": str(payload["training_cutoff"]),
        "through_source_digest": str(members[-1]["source_content_digest"]),
        "through_outcome_digest": str(members[-1]["outcome_content_digest"]),
        "input_pair_digest": str(payload["input_pair_digest"]),
        "integrity_digest": str(integrity["integrity_digest"]),
    }


def _newest_fit_by_run(fits: Sequence[_FitSummary]) -> dict[int, _FitSummary]:
    selected: dict[int, _FitSummary] = {}
    for fit in fits:
        run_id = int(cast(int, fit["through_run_id"]))
        previous = selected.get(run_id)
        if previous is None or _timestamp_order(fit["generated_at"], "fit.generated_at") > _timestamp_order(
            previous["generated_at"], "fit.generated_at"
        ):
            selected[run_id] = fit
        elif previous is not None and _timestamp_order(fit["generated_at"], "fit.generated_at") == _timestamp_order(
            previous["generated_at"], "fit.generated_at"
        ) and fit["integrity_digest"] != previous["integrity_digest"]:
            raise ProbabilitySourceError(f"run {run_id} 同generated_at存在冲突 fit assessments")
    return selected


def _outcome_progress_by_run(
    sources: Sequence[_SourceSummary],
    outcomes: Sequence[_OutcomeSummary],
    *,
    effective_as_of: str,
) -> dict[int, dict[str, object]]:
    selected = _newest_outcome_by_run(outcomes)
    grouped: dict[tuple[str, str, str, str, str], list[_SourceSummary]] = {}
    for source in sources:
        grouped.setdefault(_source_contract_key(source), []).append(source)
    progress: dict[int, dict[str, object]] = {}
    for cohort_sources in grouped.values():
        progress.update(
            _cohort_outcome_progress(
                cohort_sources,
                selected,
                effective_as_of=effective_as_of,
            )
        )
    return progress


def _newest_outcome_by_run(
    outcomes: Sequence[_OutcomeSummary],
) -> dict[int, _OutcomeSummary]:
    selected: dict[int, _OutcomeSummary] = {}
    for item in outcomes:
        run_id = _run_id(item)
        previous = selected.get(run_id)
        if previous is None or _outcome_order(item) > _outcome_order(previous):
            selected[run_id] = item
        elif _outcome_order(item) == _outcome_order(previous) and (
            item["integrity_digest"] != previous["integrity_digest"]
        ):
            raise ProbabilitySourceError(f"run {run_id} 存在冲突 outcome archives")
    return selected


def _cohort_outcome_progress(
    sources: Sequence[_SourceSummary],
    outcomes: Mapping[int, _OutcomeSummary],
    *,
    effective_as_of: str,
) -> dict[int, dict[str, object]]:
    counters = {horizon: _empty_outcome_counter() for horizon in _HORIZONS}
    output: dict[int, dict[str, object]] = {}
    observation_count = 0
    outcome_count = 0
    future_dates: dict[int, list[str]] = {horizon: [] for horizon in _HORIZONS}
    for source_count, source in enumerate(sorted(sources, key=_source_progress_order), start=1):
        observation_count += int(cast(int, source["record_count"]))
        outcome = outcomes.get(_run_id(source))
        if outcome is not None:
            _validate_outcome_source(source, outcome)
            outcome_count += 1
        maturity_dates = _maturity_dates(source, outcome)
        horizon_progress: dict[str, object] = {}
        for horizon in _HORIZONS:
            quality = _outcome_quality(outcome, horizon)
            maturity_date = maturity_dates.get(str(horizon))
            if quality is not None:
                _accumulate_outcome_quality(counters[horizon], quality)
            _accumulate_maturity_state(
                counters[horizon],
                future_dates[horizon],
                maturity_date=maturity_date,
                quality=quality,
                effective_as_of=effective_as_of,
            )
            horizon_progress[str(horizon)] = _horizon_progress(
                counters[horizon],
                source_count=source_count,
                observation_count=observation_count,
                outcome_count=outcome_count,
                next_maturity_date=min(future_dates[horizon], default=None),
            )
        output[_run_id(source)] = {
            "contract_version": "fixed-session-outcome-progress-v1",
            "outcome_artifact_count": outcome_count,
            "latest_outcome_as_of": outcome.get("as_of_date") if outcome else None,
            "horizons": horizon_progress,
        }
    return output


def _empty_outcome_counter() -> dict[str, int]:
    return {
        "mature_sessions": 0,
        "available_sessions": 0,
        "mature_observations": 0,
        "eligible": 0,
        "missing_sessions": 0,
        "overdue_sessions": 0,
    }


def _validate_outcome_source(source: _SourceSummary, outcome: _OutcomeSummary) -> None:
    if outcome["source_integrity_digest"] != source["integrity_digest"]:
        raise ProbabilitySourceError(f"run {_run_id(source)} outcome/source digest 不一致")
    if _cohort_key(outcome) != _cohort_key(source):
        raise ProbabilitySourceError(f"run {_run_id(source)} outcome/source cohort 不一致")


def _outcome_quality(
    outcome: _OutcomeSummary | None,
    horizon: int,
) -> Mapping[str, object] | None:
    if outcome is None:
        return None
    horizons = _mapping(outcome["horizons"], "outcome.horizons")
    return _mapping(horizons[str(horizon)], f"outcome.horizons.{horizon}")


def _accumulate_outcome_quality(counter: dict[str, int], quality: Mapping[str, object]) -> None:
    if quality["mature"] is not True:
        return
    counter["mature_sessions"] += 1
    counter["mature_observations"] += int(cast(int, quality["mature_record_count"]))
    counter["eligible"] += int(cast(int, quality["eligible_observation_count"]))
    if quality["available_for_study"] is True:
        counter["available_sessions"] += 1
    if int(cast(int, quality["data_unavailable_record_count"])) > 0:
        counter["missing_sessions"] += 1


def _horizon_progress(
    counter: Mapping[str, int],
    *,
    source_count: int,
    observation_count: int,
    outcome_count: int,
    next_maturity_date: str | None,
) -> dict[str, object]:
    mature_observations = counter["mature_observations"]
    return {
        "archived_independent_session_count": source_count,
        "mature_label_session_count": counter["mature_sessions"],
        "available_independent_session_count": counter["available_sessions"],
        "observation_count": observation_count,
        "mature_observation_count": mature_observations,
        "eligible_observation_count": counter["eligible"],
        "label_coverage": counter["eligible"] / mature_observations if mature_observations else 0.0,
        "outcome_artifact_count": outcome_count,
        "next_maturity_date": next_maturity_date,
        "maintenance_due": counter["overdue_sessions"] > 0,
        "overdue_session_count": counter["overdue_sessions"],
        "has_mature_missing_bars": counter["missing_sessions"] > 0,
        "mature_missing_bar_session_count": counter["missing_sessions"],
    }


def _accumulate_maturity_state(
    counter: dict[str, int],
    future_dates: list[str],
    *,
    maturity_date: str | None,
    quality: Mapping[str, object] | None,
    effective_as_of: str,
) -> None:
    mature = quality is not None and quality["mature"] is True
    if mature or maturity_date is None:
        return
    if maturity_date <= effective_as_of:
        counter["overdue_sessions"] += 1
    else:
        future_dates.append(maturity_date)


def _maturity_dates(
    source: _SourceSummary,
    outcome: _OutcomeSummary | None,
) -> dict[str, str | None]:
    if outcome is not None:
        horizons = _mapping(outcome["horizons"], "outcome.horizons")
        return {
            str(horizon): str(_mapping(horizons[str(horizon)], "horizon")["target_session_date"])
            for horizon in _HORIZONS
        }
    try:
        fixed = next_trade_dates(
            datetime.fromisoformat(str(source["quote_date"])).date(),
            max(_HORIZONS) + 1,
        )
    except (TradingCalendarCoverageError, ValueError):
        return {str(horizon): None for horizon in _HORIZONS}
    return {str(horizon): fixed[horizon].isoformat() for horizon in _HORIZONS}


def _outcome_order(summary: Mapping[str, object]) -> tuple[str, float]:
    return (
        str(summary["as_of_date"]),
        _timestamp_order(summary["generated_at"], "generated_at"),
    )


def _cumulative_source_corpora(summaries: Sequence[_SourceSummary]) -> dict[int, dict[str, object]]:
    grouped: dict[tuple[str, str, str, str, str], list[_SourceSummary]] = {}
    for summary in summaries:
        grouped.setdefault(_source_contract_key(summary), []).append(summary)
    corpora: dict[int, dict[str, object]] = {}
    for cohort in grouped.values():
        corpora.update(_cohort_source_corpora(cohort))
    return corpora


def _cohort_source_corpora(summaries: Sequence[_SourceSummary]) -> dict[int, dict[str, object]]:
    ordered = sorted(summaries, key=_source_progress_order)
    corpora: dict[int, dict[str, object]] = {}
    newest_capture: _SourceSummary | None = None
    previous_digest: str | None = None
    observation_count = 0
    for source_count, current in enumerate(ordered, start=1):
        observation_count += int(cast(int, current["record_count"]))
        newest_capture = max(
            (item for item in (newest_capture, current) if item is not None),
            key=_source_capture_order,
        )
        corpus = _source_corpus(
            current,
            newest_capture=newest_capture,
            previous_digest=previous_digest,
            source_count=source_count,
            observation_count=observation_count,
        )
        previous_digest = str(corpus["integrity_digest"])
        corpora[_run_id(current)] = corpus
    return corpora


def _source_corpus(
    current: _SourceSummary,
    *,
    newest_capture: _SourceSummary,
    previous_digest: str | None,
    source_count: int,
    observation_count: int,
) -> dict[str, object]:
    member = {
        "run_id": current["run_id"],
        "quote_date": current["quote_date"],
        "as_of": current["as_of"],
        "captured_at": current["captured_at"],
        "record_count": current["record_count"],
        "source_integrity_digest": current["integrity_digest"],
    }
    identity: dict[str, object] = {
        "contract_version": _SOURCE_CORPUS_CONTRACT_VERSION,
        "cohort": dict(_mapping(current["cohort"], "summary.cohort")),
        "through": {
            "quote_date": current["quote_date"],
            "as_of": current["as_of"],
            "run_id": current["run_id"],
        },
        "generated_at": newest_capture["captured_at"],
        "source_count": source_count,
        "archived_independent_session_count": source_count,
        "observation_count": observation_count,
    }
    digest_input = {
        **identity,
        "previous_integrity_digest": previous_digest,
        "source": member,
    }
    digest = hashlib.sha256(
        json.dumps(digest_input, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode("utf-8")
    ).hexdigest()
    return {**identity, "integrity_digest": digest}


def _source_capture_order(summary: Mapping[str, object]) -> tuple[float, str, int]:
    return (
        _timestamp_order(summary["captured_at"], "captured_at"),
        str(summary["captured_at"]),
        _run_id(summary),
    )


def _cohort_key(summary: Mapping[str, object]) -> tuple[str, str, str]:
    cohort = _mapping(summary["cohort"], "summary.cohort")
    return (str(cohort["mode"]), str(cohort["scope"]), str(cohort["rule_version"]))


def _source_contract_key(
    summary: Mapping[str, object],
) -> tuple[str, str, str, str, str]:
    score = _mapping(summary.get("score_contract") or {}, "summary.score_contract")
    return (
        *_cohort_key(summary),
        str(score.get("production_score_rule_version") or "legacy_unbound"),
        str(score.get("production_score_spec_hash") or "legacy_unbound"),
    )


def _run_id(summary: Mapping[str, object]) -> int:
    return int(cast(int, summary["run_id"]))


def _source_canonical_order(summary: Mapping[str, object]) -> tuple[float, int]:
    return _timestamp_order(summary["as_of"], "run.as_of"), _run_id(summary)


def _source_progress_order(summary: Mapping[str, object]) -> tuple[str, float, int]:
    return (
        str(summary["quote_date"]),
        _timestamp_order(summary["as_of"], "run.as_of"),
        _run_id(summary),
    )


def _timestamp_order(value: object, path: str) -> float:
    try:
        parsed = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    except ValueError as exc:
        raise ProbabilitySourceError(f"上涨概率 source research {path} 无效") from exc
    if parsed.tzinfo is None or parsed.utcoffset() is None:
        raise ProbabilitySourceError(f"上涨概率 source research {path} 必须包含时区")
    return parsed.timestamp()


def _mapping(value: object, path: str) -> Mapping[str, object]:
    if not isinstance(value, Mapping):
        raise ProbabilitySourceError(f"上涨概率 source research {path} 必须是 object")
    return cast(Mapping[str, object], value)


def _directory_snapshot(directory: Path, pattern: str) -> _DirectorySnapshot:
    try:
        if not path_has_only_trusted_aliases(directory):
            raise ProbabilitySourceError(f"上涨概率 source archive 路径不是目录：{directory}")
        facts = directory.lstat()
    except ProbabilitySourceError:
        raise
    except FileNotFoundError:
        return None, ()
    except (OSError, RuntimeError) as exc:
        raise ProbabilitySourceError(f"上涨概率 source archive 目录无法读取：{directory}") from exc
    if not stat.S_ISDIR(facts.st_mode):
        raise ProbabilitySourceError(f"上涨概率 source archive 路径不是目录：{directory}")
    identity = (facts.st_dev, facts.st_ino, facts.st_mtime_ns, facts.st_ctime_ns)
    try:
        fingerprints = tuple(_file_fingerprint(path) for path in sorted(directory.glob(pattern)))
    except OSError as exc:
        raise ProbabilitySourceError(f"上涨概率 source archive 目录无法完整扫描：{directory}") from exc
    return identity, fingerprints


def _file_fingerprint(path: Path) -> _FileFingerprint:
    facts = path.lstat()
    if not stat.S_ISREG(facts.st_mode):
        raise ProbabilitySourceError(f"上涨概率 source archive 必须是普通文件：{path}")
    return (
        path,
        facts.st_dev,
        facts.st_ino,
        facts.st_mode,
        facts.st_size,
        facts.st_mtime_ns,
        facts.st_ctime_ns,
    )


__all__ = [
    "PROBABILITY_SOURCE_RESEARCH_SCHEMA_VERSION",
    "MarketScanProbabilitySourceResearchStore",
]
