"""Immutable evidence for individual-stock D+2/D+3/D+4 probability research.

The builder deliberately reads the attested research-history SQLite, not the
mutable production cache.  Its replay cohort is useful for model diagnostics,
but is explicitly non-official and can never by itself unlock a displayed
single-stock probability.
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import date, datetime, time
import math
from pathlib import Path
import re
import sqlite3
import stat
from typing import Iterator, cast

from app.artifacts.io import (
    ArtifactIOError,
    canonical_json_bytes,
    decode_json_bytes,
    path_has_only_trusted_aliases,
    read_regular_file,
    sha256_hex,
)
from app.db.market_scan_artifact_lease import (
    MarketScanArtifactLeaseError,
    publish_market_scan_artifact,
    require_project_managed_artifact_database,
    verified_market_scan_artifact_publication,
)

from app.models.paper_trading import PaperCostProfile
from app.services.market_scan_probability import (
    PROBABILITY_BASELINE_VERSION,
    PROBABILITY_CALIBRATOR_VERSION,
    PROBABILITY_COST_MODEL_VERSION,
    PROBABILITY_FEATURE_VERSION,
    PROBABILITY_ISOTONIC_CALIBRATOR_VERSION,
    PROBABILITY_LABEL_VERSION,
    PROBABILITY_MODEL_VERSION,
    PROBABILITY_SCHEMA_VERSION,
    PROBABILITY_SPLIT_VERSION,
    ProbabilityConfig,
    ProbabilitySample,
    fit_shadow_probability,
)
from app.services.market_scan_probability_history import (
    load_market_scan_probability_history_manifest,
)
from app.services.market_scan_probability_replay import (
    HISTORICAL_REPLAY_FEATURE_NAMES,
    HISTORICAL_REPLAY_FEATURE_VERSION,
    HISTORICAL_REPLAY_HORIZONS,
    historical_replay_feature_values,
)
from app.services.market_scan_probability_source import (
    ProbabilitySourceError,
    is_current_writable_production_score_contract,
    is_registered_production_score_contract,
    load_probability_source_snapshot,
    validate_current_full_market_coverage,
    validate_previous_full_market_coverage,
)
from app.services.market_scan_modes import current_official_source_temporal_contract_matches
from app.services.market_scan_probability_source import (
    PREVIOUS_PROBABILITY_SOURCE_ARTIFACT_SCHEMA_VERSION,
    PREVIOUS_PROBABILITY_SOURCE_PAYLOAD_CONTRACT_VERSION,
    PROBABILITY_SOURCE_ARTIFACT_SCHEMA_VERSION,
    PROBABILITY_SOURCE_PAYLOAD_CONTRACT_VERSION,
)
from app.services.market_scan_score_dimensions import (
    MARKET_SCAN_EVIDENCE_CONTRACT_VERSION,
)
from app.services.trading_calendar import (
    TradingCalendarCoverageError,
    is_trading_day,
    latest_expected_daily_kline_date,
)
from app.services.paper_trading_costs import resolve_cost_profile, trade_costs
from app.utils.clock import ASHARE_TIMEZONE, utc_now
from app.utils.audit_time import parse_audit_time

exclusive_atomic_publish = publish_market_scan_artifact


INDIVIDUAL_PROBABILITY_ASSESSMENT_SCHEMA_VERSION = "individual-upside-probability-assessment-v4-source-intake-bound"
LEGACY_INDIVIDUAL_PROBABILITY_ASSESSMENT_SCHEMA_VERSION = "individual-upside-probability-assessment-v2-estimator-bound"
SUPERSEDED_INDIVIDUAL_PROBABILITY_ASSESSMENT_SCHEMA_VERSIONS = (
    "individual-upside-probability-assessment-v1",
    "individual-upside-probability-assessment-v3-source-contract-bound",
)
INDIVIDUAL_PROBABILITY_TARGET_VERSION = "individual-upside-net-return-label-v1"
INDIVIDUAL_PROBABILITY_HOLDING_SESSIONS = (1, 2, 3)
INDIVIDUAL_PROBABILITY_DISPLAY_DAYS = (2, 3, 4)
INDIVIDUAL_PROBABILITY_ASSESSMENT_PREFIX = "individual-upside-probability-assessment"
INDIVIDUAL_PROBABILITY_ARTIFACT_MAX_BYTES = 2 * 1024 * 1024
INDIVIDUAL_PROBABILITY_HISTORY_DATABASE_MAX_BYTES = 512 * 1024 * 1024
INDIVIDUAL_PROBABILITY_EXECUTION_NOTIONAL = 100_000.0
INDIVIDUAL_PROBABILITY_MIN_OFFICIAL_SOURCE_COVERAGE = 0.98
INDIVIDUAL_PROBABILITY_OFFICIAL_SOURCE_MATURITY_TIME = time(15, 15)
_MINIMUM_HISTORY_BARS = 61
_SHA256_LENGTH = 64
_SHA256 = re.compile(r"[0-9a-f]{64}")
_LEGACY_V2_LIMITATIONS = (
    "historical_replay_is_not_official_point_in_time_evidence",
    "survivorship_bias_from_fixed_current_sample",
    "historical_listing_st_and_delisting_membership_unavailable",
    "qfq_provider_vintage_is_one_attested_snapshot_not_daily_vintages",
    "amount_and_turnover_unavailable_capacity_not_modelled",
    "D_plus_1_open_is_daily_bar_proxy_not_proven_executable_fill",
    "daily_price_limit_fill_and_exit_tradeability_not_modelled",
    "shadow_research_only_no_production_ranking_or_advice_effect",
)
_LIMITATIONS = (
    *_LEGACY_V2_LIMITATIONS,
    "compact_horizon_metrics_not_independently_replayable",
)
_INTEGRITY_NOTICE = "sha256_integrity_not_signature_or_official_snapshot_attestation"


class IndividualProbabilityArtifactError(ValueError):
    """Raised when individual-probability evidence cannot be trusted."""


@dataclass
class _HistoryBar:
    symbol: str
    date: str
    open: float
    close: float
    high: float
    low: float
    volume: float
    adjustment_mode: str
    data_version: str
    contract_version: str


@dataclass(frozen=True)
class _AssessmentSource:
    manifest_digest: str
    database_sha256: str
    database_path: Path
    database_bytes: bytes
    database_identity: tuple[int, int, int, int]


@dataclass(frozen=True)
class _OfficialPitSource:
    data_date: str
    run_id: int
    integrity_digest: str
    source_schema_version: str
    source_contract_version: str
    feature_version: str
    source_evidence_contract_version: str
    as_of: str
    captured_at: str
    run_rule_version: str
    production_score_rule_version: str
    production_score_spec_hash: str
    total_count: int
    success_count: int
    record_count: int
    success_to_total_coverage: float
    full_market_coverage: dict[str, object]


@dataclass(frozen=True)
class _DatabaseSnapshot:
    encoded: bytes
    identity: tuple[int, int, int, int]


@dataclass(frozen=True)
class _ValidatedSource:
    start: date
    end: date
    sessions: int
    symbols: int
    records: int
    official: bool


@dataclass(frozen=True)
class _ValidatedOfficialPit:
    dates: tuple[date, ...]
    sources: tuple[_OfficialPitSource, ...]
    count: int
    required: int
    ready: bool


@dataclass(frozen=True)
class _ValidatedHorizon:
    counts: dict[str, int]
    selection_qualified: bool
    gate_reasons: tuple[str, ...]
    training_cutoff: date | None


def individual_probability_target_contract() -> dict[str, object]:
    profile = resolve_cost_profile("base")
    return {
        "version": INDIVIDUAL_PROBABILITY_TARGET_VERSION,
        "signal_cutoff": "completed_session_D_close",
        "entry": "D_plus_1_official_daily_open_proxy_no_shift",
        "exits": {
            "D+2": "D_plus_2_close_holding_session_1",
            "D+3": "D_plus_3_close_holding_session_2",
            "D+4": "D_plus_4_close_holding_session_3",
        },
        "target": "round_trip_net_return_after_declared_costs_gt_0_daily_bar_proxy",
        "cost_profile": profile.profile_id,
        "execution_notional": INDIVIDUAL_PROBABILITY_EXECUTION_NOTIONAL,
        "feature_version": HISTORICAL_REPLAY_FEATURE_VERSION,
        "point_in_time_required": True,
    }


def individual_probability_estimator_contract() -> dict[str, object]:
    """Freeze every core estimator/split dependency used by assessment v2.

    The individual assessment reuses the market-scan estimator.  Persisting
    only its output version is insufficient: a core split/config change could
    otherwise reinterpret an old artifact under the same assessment schema.
    Exact verification below deliberately makes such drift fail closed and
    requires a new assessment schema plus a rebuilt content-addressed artifact.
    """
    representative = ProbabilityConfig(
        horizon=max(INDIVIDUAL_PROBABILITY_HOLDING_SESSIONS),
        target="net_return_positive",
    )
    return {
        "probability_schema_version": PROBABILITY_SCHEMA_VERSION,
        "model_version": PROBABILITY_MODEL_VERSION,
        "calibrator_version": PROBABILITY_CALIBRATOR_VERSION,
        "isotonic_calibrator_version": PROBABILITY_ISOTONIC_CALIBRATOR_VERSION,
        "baseline_version": PROBABILITY_BASELINE_VERSION,
        "estimator_feature_version": PROBABILITY_FEATURE_VERSION,
        "estimator_label_version": PROBABILITY_LABEL_VERSION,
        "cost_model_version": PROBABILITY_COST_MODEL_VERSION,
        "split_version": PROBABILITY_SPLIT_VERSION,
        "target": "net_return_positive",
        "purge_rule": ("all_prior_partition_labels_mature_strictly_before_next_partition_signal"),
        "minimum_label_coverage": representative.minimum_label_coverage,
        "minimum_bin_sessions": representative.minimum_bin_sessions,
        "calibration_bin_count": representative.calibration_bin_count,
        "minimum_isotonic_calibration_sessions": (representative.minimum_isotonic_calibration_sessions),
        "empirical_bayes_bin_count": representative.empirical_bayes_bin_count,
        "empirical_bayes_prior_strength": representative.empirical_bayes_prior_strength,
        "bootstrap_samples": representative.bootstrap_samples,
        "l2_strength": representative.l2_strength,
        "maximum_iterations": representative.maximum_iterations,
        "convergence_tolerance": representative.convergence_tolerance,
        "required_official_pit_sessions": required_official_pit_sessions(),
        "horizon_split_contracts": {
            str(holding): _individual_probability_horizon_split_contract(holding) for holding in INDIVIDUAL_PROBABILITY_HOLDING_SESSIONS
        },
    }


def _individual_probability_horizon_split_contract(holding: int) -> dict[str, object]:
    config = ProbabilityConfig(horizon=holding, target="net_return_positive")
    minimum_fit = config.minimum_train_sessions + config.minimum_calibration_sessions + config.minimum_test_sessions + 2 * config.effective_gap_sessions
    return {
        "holding_sessions": holding,
        "entry_session_offset": 1,
        "target_session_offset": config.target_session_offset,
        "gap_sessions": config.effective_gap_sessions,
        "minimum_train_sessions": config.minimum_train_sessions,
        "minimum_calibration_sessions": config.minimum_calibration_sessions,
        "minimum_test_sessions": config.minimum_test_sessions,
        "minimum_selection_folds": config.minimum_selection_folds,
        "minimum_fit_independent_sessions": minimum_fit,
        "minimum_selection_independent_sessions": (minimum_fit + (config.minimum_selection_folds - 1) * config.minimum_test_sessions),
    }


def build_individual_probability_assessment(
    history_manifest_path: str | Path,
    *,
    official_source_paths: Sequence[str | Path] = (),
    generated_at: str | None = None,
) -> dict[str, object]:
    """Build compact D+2/D+3/D+4 OOS evidence from one attested history DB."""
    timestamp = generated_at or utc_now().isoformat(timespec="seconds")
    _validated_assessment_generated_at(timestamp)
    source = _assessment_source(history_manifest_path)
    official_sources = _official_pit_sources(
        official_source_paths,
        generated_at=timestamp,
    )
    with _readonly_database(source.database_bytes) as connection:
        series = _history_series(connection)
    signal_dates, samples = _probability_samples(series)
    horizons = _fit_assessment_horizons(samples, official_sources, timestamp)
    _assert_history_database_unchanged(source)
    payload = _assessment_payload(source, official_sources, signal_dates, samples, series, horizons)
    return _seal_assessment(payload, timestamp)


def _assessment_source(history_manifest_path: str | Path) -> _AssessmentSource:
    manifest = load_market_scan_probability_history_manifest(history_manifest_path)
    payload = _mapping(manifest["payload"], "manifest.payload")
    database = _mapping(payload["database"], "manifest.payload.database")
    database_path = Path(str(database["path"])).expanduser().absolute()
    manifest_digest = str(_mapping(manifest["integrity"], "manifest.integrity")["integrity_digest"])
    database_sha256 = str(database["sha256"])
    size = _positive_int(database.get("size_bytes"), "manifest database size")
    snapshot = _verified_history_database_bytes(database_path, database_sha256, size)
    return _AssessmentSource(
        manifest_digest,
        database_sha256,
        database_path,
        snapshot.encoded,
        snapshot.identity,
    )


def _fit_assessment_horizons(
    samples: Mapping[int, Sequence[ProbabilitySample]],
    official_sources: Sequence[_OfficialPitSource],
    generated_at: str,
) -> dict[str, object]:
    return {
        str(holding): _compact_horizon_evidence(
            fit_shadow_probability(
                samples[holding],
                config=ProbabilityConfig(horizon=holding, target="net_return_positive"),
                generated_at=generated_at,
            ),
            holding=holding,
            official_session_count=len(official_sources),
        )
        for holding in INDIVIDUAL_PROBABILITY_HOLDING_SESSIONS
    }


def _assessment_payload(
    source: _AssessmentSource,
    official_sources: Sequence[_OfficialPitSource],
    signal_dates: Sequence[str],
    samples: Mapping[int, Sequence[ProbabilitySample]],
    series: Mapping[str, Sequence[_HistoryBar]],
    horizons: Mapping[str, object],
) -> dict[str, object]:
    required_official = required_official_pit_sessions()
    official_dates = [item.data_date for item in official_sources]
    payload: dict[str, object] = {
        "target_contract": individual_probability_target_contract(),
        "estimator_contract": individual_probability_estimator_contract(),
        "source": {
            "history_manifest_digest": source.manifest_digest,
            "history_database_sha256": source.database_sha256,
            "history_database_file": source.database_path.name,
            "historical_replay_official": False,
            "historical_replay_session_count": len(signal_dates),
            "historical_replay_start_date": signal_dates[0],
            "historical_replay_end_date": signal_dates[-1],
            "symbol_count": len(series),
            "record_count": sum(len(values) for values in samples.values()),
        },
        "official_pit": {
            "session_dates": official_dates,
            "session_count": len(official_dates),
            "required_session_count": required_official,
            "ready": len(official_dates) >= required_official,
            "sources": [_official_source_identity(item) for item in official_sources],
        },
        "horizons": horizons,
        "limitations": list(_LIMITATIONS),
        "production_effect": "none",
    }
    return payload


def _official_source_identity(item: _OfficialPitSource) -> dict[str, object]:
    return {
        "data_date": item.data_date,
        "run_id": item.run_id,
        "integrity_digest": item.integrity_digest,
        "source_schema_version": item.source_schema_version,
        "source_contract_version": item.source_contract_version,
        "feature_version": item.feature_version,
        "source_evidence_contract_version": item.source_evidence_contract_version,
        "as_of": item.as_of,
        "captured_at": item.captured_at,
        "run_rule_version": item.run_rule_version,
        "production_score_rule_version": item.production_score_rule_version,
        "production_score_spec_hash": item.production_score_spec_hash,
        "total_count": item.total_count,
        "success_count": item.success_count,
        "record_count": item.record_count,
        "success_to_total_coverage": item.success_to_total_coverage,
        "full_market_coverage": item.full_market_coverage,
    }


def _seal_assessment(payload: Mapping[str, object], generated_at: str) -> dict[str, object]:
    artifact: dict[str, object] = {
        "schema_version": INDIVIDUAL_PROBABILITY_ASSESSMENT_SCHEMA_VERSION,
        "generated_at": generated_at,
        "payload": dict(payload),
    }
    artifact["integrity"] = {
        "algorithm": "sha256",
        "integrity_digest": sha256_hex(canonical_json_bytes(artifact)),
        "notice": _INTEGRITY_NOTICE,
    }
    return verify_individual_probability_assessment(artifact)


def required_official_pit_sessions() -> int:
    """Registered minimum for two complete OOS folds at the longest horizon."""
    config = ProbabilityConfig(horizon=max(INDIVIDUAL_PROBABILITY_HOLDING_SESSIONS))
    return (
        config.minimum_train_sessions
        + 2 * config.effective_gap_sessions
        + config.minimum_calibration_sessions
        + config.minimum_test_sessions * config.minimum_selection_folds
    )


def verify_individual_probability_assessment(
    value: Mapping[str, object],
) -> dict[str, object]:
    artifact = _json_mapping(value)
    if set(artifact) != {"schema_version", "generated_at", "payload", "integrity"}:
        raise IndividualProbabilityArtifactError("个股上涨概率 assessment 顶层字段无效")
    schema_version = artifact["schema_version"]
    if schema_version in SUPERSEDED_INDIVIDUAL_PROBABILITY_ASSESSMENT_SCHEMA_VERSIONS:
        raise IndividualProbabilityArtifactError("个股上涨概率 superseded assessment 仅供历史审计，不能作为当前 runtime 证据")
    if schema_version not in {
        INDIVIDUAL_PROBABILITY_ASSESSMENT_SCHEMA_VERSION,
        LEGACY_INDIVIDUAL_PROBABILITY_ASSESSMENT_SCHEMA_VERSION,
    }:
        raise IndividualProbabilityArtifactError("个股上涨概率 assessment schema 不受支持")
    _validated_assessment_generated_at(artifact["generated_at"])
    integrity = _mapping(artifact["integrity"], "integrity")
    if set(integrity) != {"algorithm", "integrity_digest", "notice"}:
        raise IndividualProbabilityArtifactError("个股上涨概率 assessment integrity 字段无效")
    unsigned = {key: item for key, item in artifact.items() if key != "integrity"}
    expected = sha256_hex(canonical_json_bytes(unsigned))
    if integrity.get("algorithm") != "sha256" or integrity.get("integrity_digest") != expected or integrity.get("notice") != _INTEGRITY_NOTICE:
        raise IndividualProbabilityArtifactError("个股上涨概率 assessment digest 不一致")
    _validate_payload(
        _mapping(artifact["payload"], "payload"),
        generated_at=str(artifact["generated_at"]),
        legacy_source_binding=(schema_version == LEGACY_INDIVIDUAL_PROBABILITY_ASSESSMENT_SCHEMA_VERSION),
    )
    return artifact


def load_individual_probability_assessment(path: str | Path) -> dict[str, object]:
    try:
        decoded = decode_json_bytes(read_regular_file(path, max_bytes=INDIVIDUAL_PROBABILITY_ARTIFACT_MAX_BYTES))
    except ArtifactIOError as exc:
        raise IndividualProbabilityArtifactError("个股上涨概率 assessment 无法安全读取") from exc
    if not isinstance(decoded, Mapping):
        raise IndividualProbabilityArtifactError("个股上涨概率 assessment 顶层必须是 object")
    return verify_individual_probability_assessment(decoded)


def write_individual_probability_assessment(
    directory: str | Path,
    artifact: Mapping[str, object],
    *,
    database_path: str | Path | None = None,
) -> Path:
    verified = verify_individual_probability_assessment(artifact)
    digest = str(_mapping(verified["integrity"], "integrity")["integrity_digest"])
    target = Path(directory).expanduser().absolute() / (f"{INDIVIDUAL_PROBABILITY_ASSESSMENT_PREFIX}-{digest}.json")
    try:
        require_project_managed_artifact_database(target, database_path, "research/individual_probability")
        encoded = canonical_json_bytes(verified)
        if database_path is None:
            exclusive_atomic_publish(target, encoded, max_bytes=INDIVIDUAL_PROBABILITY_ARTIFACT_MAX_BYTES)
        else:
            payload = _mapping(verified["payload"], "payload")
            run_ids = tuple(sorted(_assessment_publication_run_ids(payload)))
            with verified_market_scan_artifact_publication(
                database_path,
                target,
                run_ids,
                managed_directory="research/individual_probability",
            ):
                exclusive_atomic_publish(target, encoded, max_bytes=INDIVIDUAL_PROBABILITY_ARTIFACT_MAX_BYTES)
    except MarketScanArtifactLeaseError as exc:
        raise IndividualProbabilityArtifactError("个股上涨概率 assessment 来源批次已失效") from exc
    except ArtifactIOError as exc:
        raise IndividualProbabilityArtifactError("个股上涨概率 assessment 无法发布") from exc
    return target


def _assessment_publication_run_ids(value: object) -> set[int]:
    found: set[int] = set()
    pending = [value]
    while pending:
        current = pending.pop()
        if isinstance(current, Mapping):
            for key, item in current.items():
                if str(key).endswith("run_id") and isinstance(item, int) and not isinstance(item, bool) and item > 0:
                    found.add(item)
                pending.append(item)
        elif isinstance(current, list | tuple):
            pending.extend(current)
    if not found:
        raise IndividualProbabilityArtifactError("受管 assessment 缺少来源 run manifest")
    return found


def _official_pit_sources(
    paths: Sequence[str | Path],
    *,
    generated_at: str | None = None,
) -> tuple[_OfficialPitSource, ...]:
    sources: dict[str, _OfficialPitSource] = {}
    for path in paths:
        snapshot = load_probability_source_snapshot(path)
        payload = _mapping(snapshot["payload"], "source.payload")
        run = _mapping(payload["run"], "source.payload.run")
        if run.get("mode") != "official" or run.get("canonical_published") is not True:
            continue
        source = _official_pit_source(snapshot, payload, run)
        if source is None:
            continue
        if generated_at is not None:
            _validate_official_source_available_at(snapshot, generated_at)
        previous = sources.get(source.data_date)
        if previous is not None and previous != source:
            raise IndividualProbabilityArtifactError("同一 official PIT 日期绑定了冲突 source")
        sources[source.data_date] = source
    ordered = tuple(
        sorted(
            sources.values(),
            key=lambda item: (item.data_date, item.run_id, item.integrity_digest),
        )
    )
    _validate_cross_date_source_contract(ordered)
    return ordered


def _official_pit_source(
    snapshot: Mapping[str, object],
    payload: Mapping[str, object],
    run: Mapping[str, object],
) -> _OfficialPitSource | None:
    if snapshot.get("schema_version") != PROBABILITY_SOURCE_ARTIFACT_SCHEMA_VERSION:
        return None
    if payload.get("contract_version") != PROBABILITY_SOURCE_PAYLOAD_CONTRACT_VERSION:
        raise IndividualProbabilityArtifactError("official PIT source payload contract 不是当前版本")
    feature_schema = _mapping(payload.get("feature_schema"), "source.payload.feature_schema")
    feature_version = feature_schema.get("version")
    if feature_version != PROBABILITY_FEATURE_VERSION:
        raise IndividualProbabilityArtifactError("official PIT source feature contract 不是当前版本")
    records = payload.get("records")
    if not isinstance(records, list) or not records:
        raise IndividualProbabilityArtifactError("official PIT source records 无效")
    evidence_versions = {_mapping(value, "source record").get("source_evidence_contract_version") for value in records}
    if evidence_versions != {MARKET_SCAN_EVIDENCE_CONTRACT_VERSION}:
        raise IndividualProbabilityArtifactError("official PIT source evidence contract 不是当前版本")
    return _current_official_pit_source(snapshot, payload, run, records)


def _current_official_pit_source(
    snapshot: Mapping[str, object],
    payload: Mapping[str, object],
    run: Mapping[str, object],
    records: Sequence[object],
) -> _OfficialPitSource:
    data_date = _source_data_date(run)
    run_id = _positive_int(run.get("run_id"), "official PIT source run_id")
    as_of = _source_timestamp(run.get("as_of"), "official PIT source as_of")
    captured_at = _source_timestamp(
        snapshot.get("captured_at"),
        "official PIT source captured_at",
    )
    _validate_source_maturity(data_date, as_of, captured_at, current_contract=True)
    run_rule = _required_text(run.get("rule_version"), "official PIT run rule")
    production_rule = _required_text(
        run.get("production_score_rule_version"),
        "official PIT production score rule",
    )
    production_hash = _required_sha256(
        run.get("production_score_spec_hash"),
        "official PIT production score spec hash",
    )
    if not is_current_writable_production_score_contract(production_rule, production_hash):
        raise IndividualProbabilityArtifactError("official PIT production score rule/spec 未注册")
    total, success, record_count, coverage = _official_source_counts(
        payload,
        run,
        records,
    )
    full_market_coverage = _validated_full_market_coverage(
        run.get("full_market_coverage"),
        total_count=total,
        success_count=success,
    )
    digest = _official_source_digest(snapshot)
    return _OfficialPitSource(
        data_date,
        run_id,
        digest,
        PROBABILITY_SOURCE_ARTIFACT_SCHEMA_VERSION,
        PROBABILITY_SOURCE_PAYLOAD_CONTRACT_VERSION,
        PROBABILITY_FEATURE_VERSION,
        MARKET_SCAN_EVIDENCE_CONTRACT_VERSION,
        as_of,
        captured_at,
        run_rule,
        production_rule,
        production_hash,
        total,
        success,
        record_count,
        coverage,
        full_market_coverage,
    )


def _official_source_digest(snapshot: Mapping[str, object]) -> str:
    integrity = _mapping(snapshot.get("integrity"), "source.integrity")
    return _required_sha256(
        integrity.get("integrity_digest"),
        "official PIT source digest",
    )


def _source_data_date(run: Mapping[str, object]) -> str:
    data_date = run.get("data_date")
    _iso_date(data_date, "official PIT source data_date")
    if run.get("quote_date") != data_date:
        raise IndividualProbabilityArtifactError("official PIT source quote_date/data_date 冲突")
    return cast(str, data_date)


def _official_source_counts(
    payload: Mapping[str, object],
    run: Mapping[str, object],
    records: Sequence[object],
) -> tuple[int, int, int, float]:
    total = _positive_int(run.get("total_count"), "official PIT total_count")
    success = _positive_int(run.get("success_count"), "official PIT success_count")
    record_count = len(records)
    if success > total or record_count != success:
        raise IndividualProbabilityArtifactError("official PIT source records/counts 不完整")
    quality = _mapping(payload.get("quality"), "official PIT source quality")
    if quality.get("full_market_coverage") != run.get("full_market_coverage"):
        raise IndividualProbabilityArtifactError("official PIT source full-market coverage 与 run 冲突")
    coverage = _finite_coverage(
        quality.get("success_to_total_coverage"),
        "official PIT source coverage",
    )
    count_binding = {
        "run_total_count": total,
        "run_success_count": success,
        "record_count": record_count,
        "expected_record_count": success,
    }
    if any(quality.get(name) != value for name, value in count_binding.items()):
        raise IndividualProbabilityArtifactError("official PIT source quality/counts 冲突")
    if quality.get("record_coverage") != 1.0 or not math.isclose(
        coverage,
        success / total,
        rel_tol=0,
        abs_tol=1e-12,
    ):
        raise IndividualProbabilityArtifactError("official PIT source coverage 无法重放")
    if coverage < INDIVIDUAL_PROBABILITY_MIN_OFFICIAL_SOURCE_COVERAGE:
        raise IndividualProbabilityArtifactError("official PIT source coverage 低于预注册门槛")
    return total, success, record_count, coverage


def _validate_cross_date_source_contract(
    sources: Sequence[_OfficialPitSource],
) -> None:
    if len({item.run_id for item in sources}) != len(sources):
        raise IndividualProbabilityArtifactError("official PIT 同一 run_id 不能跨日期复用")
    contracts = {
        (
            item.source_schema_version,
            item.source_contract_version,
            item.feature_version,
            item.source_evidence_contract_version,
            item.run_rule_version,
            item.production_score_rule_version,
            item.production_score_spec_hash,
        )
        for item in sources
    }
    if len(contracts) > 1:
        raise IndividualProbabilityArtifactError("official PIT 跨日 source 合同不唯一")


def _history_series(connection: sqlite3.Connection) -> dict[str, tuple[_HistoryBar, ...]]:
    rows = connection.execute(
        "SELECT symbol,date,open,close,high,low,volume,adjustment_mode,"
        "data_version,contract_version FROM kline_daily "
        "WHERE adjustment_mode='qfq' ORDER BY symbol,date"
    )
    grouped: dict[str, list[_HistoryBar]] = {}
    for row in rows:
        bar = _HistoryBar(
            symbol=str(row["symbol"]),
            date=str(row["date"]),
            open=float(row["open"]),
            close=float(row["close"]),
            high=float(row["high"]),
            low=float(row["low"]),
            volume=float(row["volume"]),
            adjustment_mode=str(row["adjustment_mode"]),
            data_version=str(row["data_version"]),
            contract_version=str(row["contract_version"]),
        )
        grouped.setdefault(bar.symbol, []).append(bar)
    if not grouped:
        raise IndividualProbabilityArtifactError("认证历史数据库没有 qfq 日K")
    series = {symbol: tuple(values) for symbol, values in grouped.items()}
    date_contracts = {tuple(row.date for row in values) for values in series.values()}
    if len(date_contracts) != 1:
        raise IndividualProbabilityArtifactError("认证历史数据库的 symbol 日期合同不一致")
    return series


def _probability_samples(
    series: Mapping[str, Sequence[_HistoryBar]],
) -> tuple[tuple[str, ...], dict[int, tuple[ProbabilitySample, ...]]]:
    first = next(iter(series.values()))
    # Keep the exact certified 1/5/20 replay cohort: 61 bars at D and enough
    # future sessions for the old longest label. This yields 279 independent D
    # sessions from the attested 360-bar history without selecting a new window.
    future_reserve = max(HISTORICAL_REPLAY_HORIZONS) + 1
    indices = range(_MINIMUM_HISTORY_BARS - 1, len(first) - future_reserve)
    signal_dates = tuple(first[index].date for index in indices)
    if not signal_dates:
        raise IndividualProbabilityArtifactError("认证历史不足以形成固定 replay 窗口")
    mutable: dict[int, list[ProbabilitySample]] = {holding: [] for holding in INDIVIDUAL_PROBABILITY_HOLDING_SESSIONS}
    profile = resolve_cost_profile("base")
    for symbol, bars in series.items():
        for index, signal_date in zip(indices, signal_dates, strict=True):
            vector = historical_replay_feature_values(bars[: index + 1], signal_date=signal_date)
            features = dict(zip(HISTORICAL_REPLAY_FEATURE_NAMES, vector, strict=True))
            entry = bars[index + 1]
            for holding in INDIVIDUAL_PROBABILITY_HOLDING_SESSIONS:
                exit_bar = bars[index + holding + 1]
                target, net_return, executable = _net_label(
                    entry,
                    exit_bar,
                    profile,
                    signal_bar=bars[index],
                )
                mutable[holding].append(
                    ProbabilitySample(
                        sample_id=(f"individual-history-replay-v1:{signal_date}:{symbol}:" f"holding-{holding}:net-return-positive"),
                        session_date=signal_date,
                        features=features,
                        target=target,
                        executable=executable,
                        net_return=net_return,
                    )
                )
    return signal_dates, {key: tuple(values) for key, values in mutable.items()}


def _net_label(
    entry: _HistoryBar,
    exit_bar: _HistoryBar,
    profile: PaperCostProfile,
    *,
    signal_bar: _HistoryBar | None = None,
) -> tuple[int | None, float | None, bool]:
    contract_bars = (entry, exit_bar) if signal_bar is None else (signal_bar, entry, exit_bar)
    contracts = {(bar.adjustment_mode, bar.data_version, bar.contract_version) for bar in contract_bars}
    if (
        len(contracts) != 1
        or next(iter(contracts))[0] != "qfq"
        or not all(_valid_label_bar(bar) for bar in (entry, exit_bar))
        or date.fromisoformat(entry.date) < date.fromisoformat(profile.effective_from)
    ):
        return None, None, False
    quantity = (math.floor(INDIVIDUAL_PROBABILITY_EXECUTION_NOTIONAL / entry.open) // 100) * 100
    if quantity < 100:
        return None, None, False
    buy_amount, sell_amount = entry.open * quantity, exit_bar.close * quantity
    buy_cost = trade_costs(profile, side="buy", gross_amount=buy_amount).total
    sell_cost = trade_costs(profile, side="sell", gross_amount=sell_amount).total
    net_return = (sell_amount - sell_cost - buy_amount - buy_cost) / (buy_amount + buy_cost)
    return int(net_return > 0), net_return, True


def _valid_label_bar(bar: _HistoryBar) -> bool:
    values = (bar.open, bar.close, bar.high, bar.low, bar.volume)
    try:
        date.fromisoformat(bar.date)
    except ValueError:
        return False
    return bool(
        all(math.isfinite(value) for value in values)
        and bar.open > 0
        and bar.close > 0
        and bar.low > 0
        and bar.volume > 0
        and bar.high >= max(bar.open, bar.close, bar.low)
        and bar.low <= min(bar.open, bar.close)
        and bar.data_version
        and bar.contract_version
    )


def _compact_horizon_evidence(
    evidence: Mapping[str, object],
    *,
    holding: int,
    official_session_count: int,
) -> dict[str, object]:
    counts = _mapping(evidence.get("counts"), "probability.counts")
    metrics_container = evidence.get("calibration_metrics")
    metrics: Mapping[str, object] | None = None
    fold_stability: Mapping[str, object] | None = None
    if isinstance(metrics_container, Mapping):
        candidate = metrics_container.get("calibrated")
        metrics = candidate if isinstance(candidate, Mapping) else None
        fold_candidate = metrics_container.get("fold_stability")
        fold_stability = fold_candidate if isinstance(fold_candidate, Mapping) else None
    selection = evidence.get("selection_qualification")
    selection_mapping = selection if isinstance(selection, Mapping) else {}
    gates = selection_mapping.get("gates")
    gate_mapping = gates if isinstance(gates, Mapping) else {}
    reasons = ["historical_replay_not_official_point_in_time"]
    required = required_official_pit_sessions()
    if official_session_count < required:
        reasons.append("official_pit_sessions_below_registered_minimum")
    reasons.extend(f"selection_gate_failed:{name}" for name, passed in sorted(gate_mapping.items()) if passed is not True)
    raw_limitations = evidence.get("limitations")
    if isinstance(raw_limitations, list):
        reasons.extend(str(value) for value in raw_limitations if isinstance(value, str))
    return {
        "display_day": holding + 1,
        "holding_sessions": holding,
        "fit_status": evidence.get("status"),
        # This compact study is fitted exclusively from the explicitly
        # non-official history replay. A strong historical diagnostic must not
        # become current-product authorization merely because enough unrelated
        # official source identities have accumulated.
        "selection_qualified": False,
        "counts": _public_counts(counts),
        "training_cutoff": evidence.get("training_cutoff"),
        "base_rate": evidence.get("base_rate"),
        "calibration_metrics": _public_metrics(
            metrics,
            fold_stability=fold_stability,
            selection=selection_mapping,
        ),
        "model_version": evidence.get("model_version") or PROBABILITY_MODEL_VERSION,
        "feature_version": HISTORICAL_REPLAY_FEATURE_VERSION,
        "estimator_feature_version": evidence.get("feature_version") or PROBABILITY_FEATURE_VERSION,
        "evidence_digest": evidence.get("evidence_digest"),
        "gate_reasons": list(dict.fromkeys(reasons)),
    }


def _public_counts(value: Mapping[str, object]) -> dict[str, int]:
    keys = (
        "observation_count",
        "eligible_observation_count",
        "out_of_sample_observation_count",
        "out_of_sample_session_count",
        "evaluated_fold_count",
    )
    result = {key: _nonnegative_int(value.get(key), f"counts.{key}") for key in keys}
    result["independent_session_count"] = _nonnegative_int(
        value.get("available_independent_session_count"),
        "counts.available_independent_session_count",
    )
    return result


def _public_metrics(
    value: Mapping[str, object] | None,
    *,
    fold_stability: Mapping[str, object] | None,
    selection: Mapping[str, object],
) -> dict[str, object] | None:
    if value is None:
        return None
    keys = (
        "brier_score",
        "reference_brier_score",
        "brier_skill_score",
        "ece",
        "auc",
        "actual_positive_rate",
        "actual_positive_rate_ci_95",
        "bin_monotonic",
        "highest_bin_above_base_rate",
    )
    result = {key: value.get(key) for key in keys}
    bins = value.get("calibration_bins")
    if isinstance(bins, list):
        sessions = [_nonnegative_int(_mapping(item, "calibration bin").get("independent_session_count"), "calibration bin sessions") for item in bins]
        result.update(
            selection_gate_version=selection.get("version"),
            calibration_bin_count=len(sessions),
            minimum_calibration_bin_session_count=min(sessions, default=0),
        )
    else:
        result.update(
            selection_gate_version=None,
            calibration_bin_count=None,
            minimum_calibration_bin_session_count=None,
        )
    result["all_folds_positive_brier_skill"] = fold_stability.get("all_folds_positive_brier_skill") if fold_stability is not None else None
    return result


def _validate_payload(
    payload: Mapping[str, object],
    *,
    generated_at: str,
    legacy_source_binding: bool,
) -> None:
    if set(payload) != {
        "target_contract",
        "estimator_contract",
        "source",
        "official_pit",
        "horizons",
        "limitations",
        "production_effect",
    }:
        raise IndividualProbabilityArtifactError("个股上涨概率 assessment payload 字段无效")
    if payload.get("target_contract") != individual_probability_target_contract():
        raise IndividualProbabilityArtifactError("个股上涨概率 target contract 冲突")
    if payload.get("estimator_contract") != individual_probability_estimator_contract():
        raise IndividualProbabilityArtifactError("个股上涨概率 estimator contract 冲突")
    source = _validate_source(_mapping(payload["source"], "payload.source"))
    official = _validate_official_pit(
        _mapping(payload["official_pit"], "payload.official_pit"),
        legacy_source_binding=legacy_source_binding,
    )
    _validate_official_pit_timing(official, generated_at)
    horizons = _mapping(payload["horizons"], "payload.horizons")
    if set(horizons) != {"1", "2", "3"}:
        raise IndividualProbabilityArtifactError("个股上涨概率 horizons 必须为 1/2/3")
    validated = tuple(
        _validate_horizon(
            _mapping(horizons[str(holding)], f"horizons.{holding}"),
            holding,
            legacy_source_binding=legacy_source_binding,
        )
        for holding in INDIVIDUAL_PROBABILITY_HOLDING_SESSIONS
    )
    _validate_horizon_set(validated, source, official)
    expected_limitations = _LEGACY_V2_LIMITATIONS if legacy_source_binding else _LIMITATIONS
    if payload.get("limitations") != list(expected_limitations) or payload.get("production_effect") != "none":
        raise IndividualProbabilityArtifactError("个股上涨概率 limitations/production effect 冲突")


def _validate_source(source: Mapping[str, object]) -> _ValidatedSource:
    required_source = {
        "history_manifest_digest",
        "history_database_sha256",
        "history_database_file",
        "historical_replay_official",
        "historical_replay_session_count",
        "historical_replay_start_date",
        "historical_replay_end_date",
        "symbol_count",
        "record_count",
    }
    if set(source) != required_source or source.get("historical_replay_official") is not False:
        raise IndividualProbabilityArtifactError("个股上涨概率 source 字段无效")
    for name in ("history_manifest_digest", "history_database_sha256"):
        value = source.get(name)
        if not isinstance(value, str) or len(value) != _SHA256_LENGTH or _SHA256.fullmatch(value) is None:
            raise IndividualProbabilityArtifactError(f"个股上涨概率 {name} 无效")
    sessions = _positive_int(source.get("historical_replay_session_count"), "historical sessions")
    symbols = _positive_int(source.get("symbol_count"), "symbol count")
    records = _positive_int(source.get("record_count"), "record count")
    if records != sessions * symbols * len(INDIVIDUAL_PROBABILITY_HOLDING_SESSIONS):
        raise IndividualProbabilityArtifactError("个股上涨概率 record count 无法重建")
    start = _iso_date(source.get("historical_replay_start_date"), "historical replay start")
    end = _iso_date(source.get("historical_replay_end_date"), "historical replay end")
    if start > end:
        raise IndividualProbabilityArtifactError("个股上涨概率历史日期范围颠倒")
    filename = source.get("history_database_file")
    if not isinstance(filename, str) or not filename or Path(filename).name != filename:
        raise IndividualProbabilityArtifactError("个股上涨概率 history database file 无效")
    return _ValidatedSource(start, end, sessions, symbols, records, False)


def _validate_official_pit(
    official: Mapping[str, object],
    *,
    legacy_source_binding: bool,
) -> _ValidatedOfficialPit:
    if set(official) != {
        "session_dates",
        "session_count",
        "required_session_count",
        "ready",
        "sources",
    }:
        raise IndividualProbabilityArtifactError("个股上涨概率 official PIT 字段无效")
    dates = official.get("session_dates")
    if not isinstance(dates, list) or any(not isinstance(value, str) for value in dates) or dates != sorted(set(dates)):
        raise IndividualProbabilityArtifactError("个股上涨概率 official PIT dates 无效")
    parsed_dates = tuple(_iso_date(value, "official PIT date") for value in dates)
    count = _nonnegative_int(official.get("session_count"), "official session count")
    required = _positive_int(official.get("required_session_count"), "required official sessions")
    if count != len(dates) or required != required_official_pit_sessions():
        raise IndividualProbabilityArtifactError("个股上涨概率 official PIT 计数冲突")
    if official.get("ready") is not (count >= required):
        raise IndividualProbabilityArtifactError("个股上涨概率 official PIT ready 冲突")
    sources = _validated_official_sources(
        official.get("sources"),
        legacy_source_binding=legacy_source_binding,
    )
    if tuple(item.data_date for item in sources) != tuple(cast(list[str], dates)):
        raise IndividualProbabilityArtifactError("official PIT source 日期集合冲突")
    return _ValidatedOfficialPit(
        parsed_dates,
        sources,
        count,
        required,
        count >= required,
    )


def _validated_official_sources(
    value: object,
    *,
    legacy_source_binding: bool,
) -> tuple[_OfficialPitSource, ...]:
    if not isinstance(value, list):
        raise IndividualProbabilityArtifactError("official PIT sources 必须是数组")
    sources = tuple(
        _validated_official_source(
            _mapping(item, "official PIT source"),
            legacy_source_binding=legacy_source_binding,
        )
        for item in value
    )
    ordered = tuple(sorted(sources, key=lambda item: (item.data_date, item.run_id, item.integrity_digest)))
    if sources != ordered or len({item.data_date for item in sources}) != len(sources):
        raise IndividualProbabilityArtifactError("official PIT sources 排序或日期唯一性冲突")
    if not legacy_source_binding:
        _validate_cross_date_source_contract(sources)
    return sources


def _validated_official_source(
    value: Mapping[str, object],
    *,
    legacy_source_binding: bool,
) -> _OfficialPitSource:
    identity_keys = {"data_date", "run_id", "integrity_digest"}
    version_keys = {
        "source_schema_version",
        "source_contract_version",
        "feature_version",
        "source_evidence_contract_version",
    }
    intake_keys = {
        "as_of",
        "captured_at",
        "run_rule_version",
        "production_score_rule_version",
        "production_score_spec_hash",
        "total_count",
        "success_count",
        "record_count",
        "success_to_total_coverage",
        "full_market_coverage",
    }
    expected_keys = identity_keys if legacy_source_binding else identity_keys | version_keys | intake_keys
    if set(value) != expected_keys:
        raise IndividualProbabilityArtifactError("official PIT source 字段无效")
    data_date, run_id, digest = _validated_official_source_identity(value)
    if legacy_source_binding:
        return _legacy_validated_official_source(data_date, run_id, digest)
    return _current_validated_official_source(value, data_date, run_id, digest)


def _validated_official_source_identity(
    value: Mapping[str, object],
) -> tuple[str, int, str]:
    raw_date = value.get("data_date")
    _iso_date(raw_date, "official PIT source data_date")
    run_id = _positive_int(value.get("run_id"), "official PIT source run_id")
    digest = value.get("integrity_digest")
    if not isinstance(digest, str) or _SHA256.fullmatch(digest) is None:
        raise IndividualProbabilityArtifactError("official PIT source digest 无效")
    return cast(str, raw_date), run_id, digest


def _legacy_validated_official_source(
    data_date: str,
    run_id: int,
    digest: str,
) -> _OfficialPitSource:
    return _OfficialPitSource(
        data_date,
        run_id,
        digest,
        "legacy_unbound",
        "legacy_unbound",
        "legacy_unbound",
        "legacy_unbound",
        "legacy_unbound",
        "legacy_unbound",
        "legacy_unbound",
        "legacy_unbound",
        "legacy_unbound",
        0,
        0,
        0,
        0.0,
        {},
    )


def _current_validated_official_source(
    value: Mapping[str, object],
    data_date: str,
    run_id: int,
    digest: str,
) -> _OfficialPitSource:
    previous, source_schema, source_contract = _validated_source_versions(value)
    as_of, captured_at, run_rule, production_rule, production_hash = (
        _validated_source_identity(value, data_date=data_date, previous=previous)
    )
    total, success, records, coverage = _validated_current_source_counts(value)
    full_market_coverage = _validated_full_market_coverage(
        value.get("full_market_coverage"),
        total_count=total,
        success_count=success,
        previous=previous,
    )
    return _OfficialPitSource(
        data_date, run_id, digest, source_schema, source_contract,
        PROBABILITY_FEATURE_VERSION, MARKET_SCAN_EVIDENCE_CONTRACT_VERSION,
        as_of, captured_at, run_rule, production_rule, production_hash,
        total, success, records, coverage, full_market_coverage,
    )


def _validated_source_versions(
    value: Mapping[str, object],
) -> tuple[bool, str, str]:
    source_schema = value.get("source_schema_version")
    previous = source_schema == PREVIOUS_PROBABILITY_SOURCE_ARTIFACT_SCHEMA_VERSION
    expected_schema = (
        PREVIOUS_PROBABILITY_SOURCE_ARTIFACT_SCHEMA_VERSION
        if previous
        else PROBABILITY_SOURCE_ARTIFACT_SCHEMA_VERSION
    )
    expected_contract = (
        PREVIOUS_PROBABILITY_SOURCE_PAYLOAD_CONTRACT_VERSION
        if previous
        else PROBABILITY_SOURCE_PAYLOAD_CONTRACT_VERSION
    )
    expected = {
        "source_schema_version": expected_schema,
        "source_contract_version": expected_contract,
        "feature_version": PROBABILITY_FEATURE_VERSION,
        "source_evidence_contract_version": MARKET_SCAN_EVIDENCE_CONTRACT_VERSION,
    }
    if any(value.get(name) != required for name, required in expected.items()):
        raise IndividualProbabilityArtifactError("official PIT source 版本绑定不是当前合同")
    return previous, expected_schema, expected_contract


def _validated_source_identity(
    value: Mapping[str, object],
    *,
    data_date: str,
    previous: bool,
) -> tuple[str, str, str, str, str]:
    as_of = _source_timestamp(value.get("as_of"), "official PIT source as_of")
    captured_at = _source_timestamp(
        value.get("captured_at"),
        "official PIT source captured_at",
    )
    _validate_source_maturity(
        data_date,
        as_of,
        captured_at,
        current_contract=not previous,
    )
    run_rule = _required_text(value.get("run_rule_version"), "official PIT run rule")
    production_rule = _required_text(
        value.get("production_score_rule_version"),
        "official PIT production score rule",
    )
    production_hash = _required_sha256(
        value.get("production_score_spec_hash"),
        "official PIT production score spec hash",
    )
    score_validator = (
        is_registered_production_score_contract
        if previous
        else is_current_writable_production_score_contract
    )
    if not score_validator(production_rule, production_hash):
        raise IndividualProbabilityArtifactError("official PIT production score rule/spec 未注册")
    return as_of, captured_at, run_rule, production_rule, production_hash


def _validated_current_source_counts(
    value: Mapping[str, object],
) -> tuple[int, int, int, float]:
    total = _positive_int(value.get("total_count"), "official PIT total_count")
    success = _positive_int(value.get("success_count"), "official PIT success_count")
    records = _positive_int(value.get("record_count"), "official PIT record_count")
    coverage = _finite_coverage(
        value.get("success_to_total_coverage"),
        "official PIT source coverage",
    )
    if (
        success > total
        or records != success
        or not math.isclose(coverage, success / total, rel_tol=0, abs_tol=1e-12)
        or coverage < INDIVIDUAL_PROBABILITY_MIN_OFFICIAL_SOURCE_COVERAGE
    ):
        raise IndividualProbabilityArtifactError("official PIT source counts/coverage 冲突")
    return total, success, records, coverage


def _validated_full_market_coverage(
    value: object,
    *,
    total_count: int,
    success_count: int,
    previous: bool = False,
) -> dict[str, object]:
    try:
        if previous:
            return validate_previous_full_market_coverage(
                value,
                total_count=total_count,
                success_count=success_count,
            )
        return validate_current_full_market_coverage(
            value,
            total_count=total_count,
            success_count=success_count,
        )
    except ProbabilitySourceError as exc:
        raise IndividualProbabilityArtifactError("official PIT source full-market coverage 无效") from exc


def _validate_official_pit_timing(
    official: _ValidatedOfficialPit,
    generated_at: str,
) -> None:
    generated = _validated_assessment_generated_at(generated_at)
    if not official.dates:
        return
    try:
        latest_mature_date = latest_expected_daily_kline_date(generated)
    except TradingCalendarCoverageError as exc:
        raise IndividualProbabilityArtifactError("可信交易日历无法验证 official PIT 成熟边界") from exc
    if official.dates[-1] > latest_mature_date:
        raise IndividualProbabilityArtifactError("official PIT 日期晚于 assessment 生成时点的已完成交易日")
    if any(not is_trading_day(value) for value in official.dates):
        raise IndividualProbabilityArtifactError("official PIT 日期必须是可信交易日")
    current_sources = (item for item in official.sources if item.captured_at != "legacy_unbound")
    if any(parse_audit_time(item.captured_at) > generated for item in current_sources):
        raise IndividualProbabilityArtifactError("official PIT source captured_at 不能晚于 assessment generated_at")


def _validate_official_source_available_at(
    snapshot: Mapping[str, object],
    generated_at: str,
) -> None:
    generated = _validated_assessment_generated_at(generated_at)
    captured_at = snapshot.get("captured_at")
    if not isinstance(captured_at, str):
        raise IndividualProbabilityArtifactError("official PIT source captured_at 无效")
    try:
        captured = parse_audit_time(captured_at)
    except ValueError as exc:
        raise IndividualProbabilityArtifactError("official PIT source captured_at 无效") from exc
    if captured > generated:
        raise IndividualProbabilityArtifactError("official PIT source captured_at 不能晚于 assessment generated_at")


def _source_timestamp(value: object, label: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise IndividualProbabilityArtifactError(f"{label} 无效")
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
        if parsed.tzinfo is None or parsed.utcoffset() is None:
            raise ValueError("timezone required")
        parse_audit_time(value)
    except ValueError as exc:
        raise IndividualProbabilityArtifactError(f"{label} 无效") from exc
    return value


def _validate_source_maturity(
    data_date: str,
    as_of: str,
    captured_at: str,
    *,
    current_contract: bool,
) -> None:
    maturity = datetime.combine(
        date.fromisoformat(data_date),
        INDIVIDUAL_PROBABILITY_OFFICIAL_SOURCE_MATURITY_TIME,
        tzinfo=ASHARE_TIMEZONE,
    )
    as_of_time = parse_audit_time(as_of)
    captured_time = parse_audit_time(captured_at)
    local_date = date.fromisoformat(data_date)
    if current_contract:
        valid = current_official_source_temporal_contract_matches(
            local_date,
            as_of=as_of_time,
            captured_at=captured_time,
        )
    else:
        valid = (
            as_of_time.astimezone(ASHARE_TIMEZONE).date() == local_date
            and captured_time.astimezone(ASHARE_TIMEZONE).date() == local_date
            and as_of_time >= maturity
            and captured_time >= maturity
            and as_of_time <= captured_time
        )
    if not valid:
        raise IndividualProbabilityArtifactError("official PIT source as_of/captured_at 未达到 15:15 成熟边界或顺序冲突")


def _required_text(value: object, label: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise IndividualProbabilityArtifactError(f"{label} 无效")
    return value


def _required_sha256(value: object, label: str) -> str:
    if not isinstance(value, str) or _SHA256.fullmatch(value) is None:
        raise IndividualProbabilityArtifactError(f"{label} 无效")
    return value


def _finite_coverage(value: object, label: str) -> float:
    if isinstance(value, bool) or not isinstance(value, int | float) or not math.isfinite(float(value)) or not 0 <= float(value) <= 1:
        raise IndividualProbabilityArtifactError(f"{label} 无效")
    return float(value)


def _validated_assessment_generated_at(value: object) -> datetime:
    if not isinstance(value, str) or not value.strip():
        raise IndividualProbabilityArtifactError("个股上涨概率 assessment generated_at 无效")
    try:
        raw = datetime.fromisoformat(value.replace("Z", "+00:00"))
        if raw.tzinfo is None or raw.utcoffset() is None:
            raise ValueError("timezone required")
        parsed = parse_audit_time(value)
    except ValueError as exc:
        raise IndividualProbabilityArtifactError("个股上涨概率 assessment generated_at 必须是含时区的有效时间") from exc
    if parsed > utc_now():
        raise IndividualProbabilityArtifactError("个股上涨概率 assessment generated_at 不能晚于当前时间")
    return parsed.astimezone(ASHARE_TIMEZONE)


def _validate_horizon(
    value: Mapping[str, object],
    holding: int,
    *,
    legacy_source_binding: bool,
) -> _ValidatedHorizon:
    _validate_horizon_identity(value, holding)
    counts = _validated_horizon_counts(value.get("counts"))
    reasons = _validated_gate_reasons(value.get("gate_reasons"))
    selection = value.get("selection_qualified")
    _validate_horizon_selection(value.get("fit_status"), selection, reasons)
    cutoff = _validate_horizon_evidence_fields(
        value,
        legacy_source_binding=legacy_source_binding,
    )
    _validate_fold_state(value, counts)
    return _ValidatedHorizon(counts, cast(bool, selection), reasons, cutoff)


def _validate_horizon_identity(value: Mapping[str, object], holding: int) -> None:
    keys = {
        "display_day",
        "holding_sessions",
        "fit_status",
        "selection_qualified",
        "counts",
        "training_cutoff",
        "base_rate",
        "calibration_metrics",
        "model_version",
        "feature_version",
        "estimator_feature_version",
        "evidence_digest",
        "gate_reasons",
    }
    if set(value) != keys or value.get("display_day") != holding + 1 or value.get("holding_sessions") != holding:
        raise IndividualProbabilityArtifactError("个股上涨概率 horizon 身份冲突")
    if value.get("fit_status") not in {"insufficient_data", "calibrated_shadow"}:
        raise IndividualProbabilityArtifactError("个股上涨概率 fit status 无效")


def _validated_horizon_counts(value: object) -> dict[str, int]:
    counts = _mapping(value, "horizon.counts")
    expected = {
        "observation_count",
        "eligible_observation_count",
        "independent_session_count",
        "out_of_sample_observation_count",
        "out_of_sample_session_count",
        "evaluated_fold_count",
    }
    if set(counts) != expected:
        raise IndividualProbabilityArtifactError("个股上涨概率 counts 无效")
    parsed = {name: _nonnegative_int(counts[name], f"horizon counts.{name}") for name in expected}
    if parsed["eligible_observation_count"] > parsed["observation_count"]:
        raise IndividualProbabilityArtifactError("eligible observations 不能超过 observations")
    if parsed["out_of_sample_observation_count"] > parsed["eligible_observation_count"]:
        raise IndividualProbabilityArtifactError("OOS observations 不能超过 eligible observations")
    if parsed["out_of_sample_session_count"] > parsed["independent_session_count"]:
        raise IndividualProbabilityArtifactError("OOS sessions 不能超过 independent sessions")
    return parsed


def _validated_gate_reasons(value: object) -> tuple[str, ...]:
    reasons = value
    if (
        not isinstance(reasons, list)
        or not reasons
        or any(not isinstance(reason, str) or not reason for reason in reasons)
        or len(reasons) != len(set(reasons))
    ):
        raise IndividualProbabilityArtifactError("个股上涨概率 gate reasons 无效")
    return tuple(cast(list[str], reasons))


def _validate_horizon_selection(
    fit_status: object,
    selection: object,
    reasons: Sequence[str],
) -> None:
    if not isinstance(selection, bool):
        raise IndividualProbabilityArtifactError("个股上涨概率 selection 状态无效")
    blockers = (
        "historical_replay_not_official_point_in_time",
        "official_pit_sessions_below_registered_minimum",
    )
    blocked = any(reason in blockers or reason.startswith("selection_gate_failed:") for reason in reasons)
    if selection and (fit_status != "calibrated_shadow" or blocked):
        raise IndividualProbabilityArtifactError("个股上涨概率 selection 与门禁原因冲突")


def _validate_horizon_evidence_fields(
    value: Mapping[str, object],
    *,
    legacy_source_binding: bool,
) -> date | None:
    _validate_optional_rate(value.get("base_rate"), "base_rate")
    _validate_horizon_versions(value)
    _validate_evidence_digest(value.get("evidence_digest"))
    metrics = value.get("calibration_metrics")
    if metrics is not None:
        _validate_metrics(
            _mapping(metrics, "horizon.calibration_metrics"),
            legacy_source_binding=legacy_source_binding,
        )
    return _optional_iso_date(value.get("training_cutoff"), "horizon training cutoff")


def _validate_optional_rate(value: object, label: str) -> None:
    if value is None:
        return
    if isinstance(value, bool) or not isinstance(value, int | float) or not math.isfinite(float(value)) or not 0 <= float(value) <= 1:
        raise IndividualProbabilityArtifactError(f"个股上涨概率 {label} 无效")


def _validate_horizon_versions(value: Mapping[str, object]) -> None:
    expected = {
        "model_version": PROBABILITY_MODEL_VERSION,
        "feature_version": HISTORICAL_REPLAY_FEATURE_VERSION,
        "estimator_feature_version": PROBABILITY_FEATURE_VERSION,
    }
    if any(value.get(name) != registered for name, registered in expected.items()):
        raise IndividualProbabilityArtifactError("个股上涨概率 horizon 版本与 estimator contract 冲突")


def _validate_evidence_digest(value: object) -> None:
    if not isinstance(value, str) or _SHA256.fullmatch(value) is None:
        raise IndividualProbabilityArtifactError("个股上涨概率 evidence_digest 无效")


def _validate_fold_state(value: Mapping[str, object], counts: Mapping[str, int]) -> None:
    if counts["evaluated_fold_count"] == 0:
        _validate_zero_fold_state(value, counts)
        return
    if (
        value.get("calibration_metrics") is None
        or value.get("training_cutoff") is None
        or value.get("base_rate") is None
        or counts["out_of_sample_observation_count"] == 0
        or counts["out_of_sample_session_count"] == 0
    ):
        raise IndividualProbabilityArtifactError("完成 OOS fold 的证据字段不完整")


def _validate_zero_fold_state(value: Mapping[str, object], counts: Mapping[str, int]) -> None:
    if (
        value.get("fit_status") != "insufficient_data"
        or value.get("selection_qualified") is not False
        or value.get("calibration_metrics") is not None
        or value.get("training_cutoff") is not None
        or value.get("base_rate") is not None
        or counts["out_of_sample_observation_count"] != 0
        or counts["out_of_sample_session_count"] != 0
    ):
        raise IndividualProbabilityArtifactError("零 OOS fold 的证据状态冲突")


def _validate_horizon_set(
    horizons: Sequence[_ValidatedHorizon],
    source: _ValidatedSource,
    official: _ValidatedOfficialPit,
) -> None:
    expected_observations = source.records // len(INDIVIDUAL_PROBABILITY_HOLDING_SESSIONS)
    if any(item.counts["observation_count"] != expected_observations for item in horizons):
        raise IndividualProbabilityArtifactError("horizon observation count 与 source 冲突")
    if any(item.counts != horizons[0].counts for item in horizons[1:]):
        raise IndividualProbabilityArtifactError("三个 horizon 的 replay counts 不一致")
    if any(item.training_cutoff is not None and not source.start <= item.training_cutoff <= source.end for item in horizons):
        raise IndividualProbabilityArtifactError("horizon training cutoff 超出历史范围")
    for item in horizons:
        _validate_gate_binding(item, source, official)


def _validate_gate_binding(
    horizon: _ValidatedHorizon,
    source: _ValidatedSource,
    official: _ValidatedOfficialPit,
) -> None:
    replay_reason = "historical_replay_not_official_point_in_time"
    official_reason = "official_pit_sessions_below_registered_minimum"
    if (replay_reason in horizon.gate_reasons) is source.official:
        raise IndividualProbabilityArtifactError("historical replay gate reason 与 source 冲突")
    if (official_reason in horizon.gate_reasons) is official.ready:
        raise IndividualProbabilityArtifactError("official PIT gate reason 与 session count 冲突")
    if horizon.selection_qualified and (not source.official or not official.ready):
        raise IndividualProbabilityArtifactError("selection 不能绕过 source/PIT 门禁")


def _validate_metrics(
    value: Mapping[str, object],
    *,
    legacy_source_binding: bool,
) -> None:
    legacy_keys = {
        "brier_score",
        "reference_brier_score",
        "brier_skill_score",
        "ece",
        "auc",
        "actual_positive_rate",
        "actual_positive_rate_ci_95",
        "bin_monotonic",
        "highest_bin_above_base_rate",
    }
    intake_keys = {
        "selection_gate_version",
        "calibration_bin_count",
        "minimum_calibration_bin_session_count",
        "all_folds_positive_brier_skill",
    }
    expected = legacy_keys if legacy_source_binding else legacy_keys | intake_keys
    if set(value) != expected:
        raise IndividualProbabilityArtifactError("个股上涨概率 calibration metrics 字段无效")
    _validate_metric_numbers(value)
    _validate_metric_interval(value)
    boolean_fields = ["bin_monotonic", "highest_bin_above_base_rate"]
    if not legacy_source_binding:
        boolean_fields.append("all_folds_positive_brier_skill")
    for name in boolean_fields:
        if value.get(name) is not None and not isinstance(value.get(name), bool):
            raise IndividualProbabilityArtifactError(f"个股上涨概率 metric {name} 无效")
    if legacy_source_binding:
        return
    version = value.get("selection_gate_version")
    if version not in {None, "market-scan-probability-selection-gates-v1"}:
        raise IndividualProbabilityArtifactError("个股上涨概率 selection gate version 无效")
    for name in ("calibration_bin_count", "minimum_calibration_bin_session_count"):
        raw = value.get(name)
        if raw is not None:
            _nonnegative_int(raw, f"metric {name}")


def _validate_metric_numbers(value: Mapping[str, object]) -> None:
    bounded = {
        "brier_score",
        "reference_brier_score",
        "ece",
        "auc",
        "actual_positive_rate",
    }
    numeric = {*bounded, "brier_skill_score"}
    for name in numeric:
        raw = value.get(name)
        if raw is not None and (isinstance(raw, bool) or not isinstance(raw, int | float) or not math.isfinite(float(raw))):
            raise IndividualProbabilityArtifactError(f"个股上涨概率 metric {name} 无效")
        if name in bounded and raw is not None and not 0 <= float(raw) <= 1:
            raise IndividualProbabilityArtifactError(f"个股上涨概率 metric {name} 超出 [0,1]")


def _validate_metric_interval(value: Mapping[str, object]) -> None:
    interval = value.get("actual_positive_rate_ci_95")
    if interval is None:
        return
    if not isinstance(interval, list) or len(interval) != 2:
        raise IndividualProbabilityArtifactError("个股上涨概率历史正例率 CI 无效")
    lower, upper = interval
    if any(isinstance(item, bool) or not isinstance(item, int | float) for item in interval):
        raise IndividualProbabilityArtifactError("个股上涨概率历史正例率 CI 无效")
    if not 0 <= float(lower) <= float(upper) <= 1:
        raise IndividualProbabilityArtifactError("个股上涨概率历史正例率 CI 无效")
    rate = value.get("actual_positive_rate")
    if rate is not None and not float(lower) <= float(cast(float, rate)) <= float(upper):
        raise IndividualProbabilityArtifactError("个股上涨概率历史正例率 CI 未覆盖正例率")


def _verified_history_database_bytes(
    path: Path,
    expected_sha256: str,
    expected_size: int,
) -> _DatabaseSnapshot:
    if _SHA256.fullmatch(expected_sha256) is None:
        raise IndividualProbabilityArtifactError("认证历史 SQLite manifest SHA-256 无效")
    if expected_size > INDIVIDUAL_PROBABILITY_HISTORY_DATABASE_MAX_BYTES:
        raise IndividualProbabilityArtifactError("认证历史 SQLite 超出独立大小上限")
    _reject_history_database_sidecars(path)
    before = _regular_file_identity(path)
    try:
        encoded = read_regular_file(path, max_bytes=expected_size)
    except ArtifactIOError as exc:
        raise IndividualProbabilityArtifactError("认证历史 SQLite 无法安全读取") from exc
    _reject_history_database_sidecars(path)
    after = _regular_file_identity(path)
    if before != after:
        raise IndividualProbabilityArtifactError("认证历史 SQLite 在读取期间发生替换或修改")
    if len(encoded) != expected_size or sha256_hex(encoded) != expected_sha256:
        raise IndividualProbabilityArtifactError("认证历史 SQLite 与 manifest 大小或摘要冲突")
    return _DatabaseSnapshot(encoded, after)


def _assert_history_database_unchanged(source: _AssessmentSource) -> None:
    snapshot = _verified_history_database_bytes(
        source.database_path,
        source.database_sha256,
        len(source.database_bytes),
    )
    if snapshot.identity != source.database_identity or snapshot.encoded != source.database_bytes:
        raise IndividualProbabilityArtifactError("认证历史 SQLite 在评估期间发生替换或修改")


def _reject_history_database_sidecars(path: Path) -> None:
    for suffix in ("-wal", "-shm", "-journal"):
        candidate = Path(f"{path}{suffix}")
        try:
            present = candidate.is_symlink() or candidate.exists()
        except OSError as exc:
            raise IndividualProbabilityArtifactError("认证历史 SQLite sidecar 无法检查") from exc
        if present:
            raise IndividualProbabilityArtifactError("认证历史 SQLite 必须无 WAL/SHM/journal sidecar")


def _regular_file_identity(path: Path) -> tuple[int, int, int, int]:
    try:
        trusted = path_has_only_trusted_aliases(path)
        facts = path.lstat()
    except (OSError, RuntimeError) as exc:
        raise IndividualProbabilityArtifactError("认证历史 SQLite 路径不可安全读取") from exc
    if not trusted or not stat.S_ISREG(facts.st_mode):
        raise IndividualProbabilityArtifactError("认证历史 SQLite 必须是无链接普通文件")
    return facts.st_dev, facts.st_ino, facts.st_size, facts.st_mtime_ns


@contextmanager
def _readonly_database(encoded: bytes) -> Iterator[sqlite3.Connection]:
    try:
        connection = sqlite3.connect(":memory:")
        connection.deserialize(encoded)
        connection.row_factory = sqlite3.Row
        connection.execute("PRAGMA query_only=ON")
        connection.execute("BEGIN")
        yield connection
        connection.rollback()
    except sqlite3.Error as exc:
        raise IndividualProbabilityArtifactError("认证历史 SQLite 无法只读评估") from exc
    finally:
        if "connection" in locals():
            connection.close()


def _mapping(value: object, label: str) -> dict[str, object]:
    if not isinstance(value, Mapping):
        raise IndividualProbabilityArtifactError(f"{label} 必须是 object")
    return dict(value)


def _json_mapping(value: Mapping[str, object]) -> dict[str, object]:
    try:
        decoded = decode_json_bytes(canonical_json_bytes(value))
    except ArtifactIOError as exc:
        raise IndividualProbabilityArtifactError("个股上涨概率 assessment 不是有限 JSON") from exc
    return cast(dict[str, object], decoded)


def _positive_int(value: object, label: str) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or value <= 0:
        raise IndividualProbabilityArtifactError(f"{label} 必须是正整数")
    return value


def _nonnegative_int(value: object, label: str) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or value < 0:
        raise IndividualProbabilityArtifactError(f"{label} 必须是非负整数")
    return value


def _iso_date(value: object, label: str) -> date:
    if not isinstance(value, str):
        raise IndividualProbabilityArtifactError(f"{label} 必须是 ISO 日期")
    try:
        return date.fromisoformat(value)
    except ValueError as exc:
        raise IndividualProbabilityArtifactError(f"{label} 必须是 ISO 日期") from exc


def _optional_iso_date(value: object, label: str) -> date | None:
    return None if value is None else _iso_date(value, label)


__all__ = [
    "INDIVIDUAL_PROBABILITY_ASSESSMENT_PREFIX",
    "INDIVIDUAL_PROBABILITY_ASSESSMENT_SCHEMA_VERSION",
    "INDIVIDUAL_PROBABILITY_DISPLAY_DAYS",
    "INDIVIDUAL_PROBABILITY_HOLDING_SESSIONS",
    "INDIVIDUAL_PROBABILITY_HISTORY_DATABASE_MAX_BYTES",
    "IndividualProbabilityArtifactError",
    "LEGACY_INDIVIDUAL_PROBABILITY_ASSESSMENT_SCHEMA_VERSION",
    "SUPERSEDED_INDIVIDUAL_PROBABILITY_ASSESSMENT_SCHEMA_VERSIONS",
    "build_individual_probability_assessment",
    "individual_probability_target_contract",
    "individual_probability_estimator_contract",
    "load_individual_probability_assessment",
    "required_official_pit_sessions",
    "verify_individual_probability_assessment",
    "write_individual_probability_assessment",
]
