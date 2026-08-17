"""Immutable fixed-session outcomes for archived probability sources.

This module closes the mechanical boundary between an archived point-in-time
full-market source and the eventual executable labels used by Shadow research.
It deliberately owns no scheduler, provider, database, API, or production
ranking path.  Callers provide daily bars; this module fixes target dates from
the trusted exchange calendar, never shifts a missing target to a later bar,
and publishes a replay-verified content-addressed artifact.

The SHA-256 values below are integrity digests, not signatures or authenticity
attestations.  Authenticity remains anchored in the already verified source
snapshot and the caller's trusted daily-bar acquisition boundary.
"""

from __future__ import annotations

from collections import defaultdict
from collections.abc import Callable, Mapping, Sequence
from copy import deepcopy
from dataclasses import asdict
from datetime import date, datetime
import gzip
from io import BytesIO
import math
from pathlib import Path
import re
import stat
from typing import Literal, cast

from app.artifacts.io import (
    ArtifactCanonicalJsonError,
    ArtifactChangedError,
    ArtifactContentConflictError,
    ArtifactDuplicateKeyError,
    ArtifactIOError,
    ArtifactNonFiniteConstantError,
    ArtifactNotDirectoryError,
    ArtifactNotRegularError,
    ArtifactPublishConflictError,
    ArtifactTooLargeError,
    canonical_json_text,
    content_addressed_filename,
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

from app.models.market import DAILY_KLINE_CONTRACT_VERSION, Kline
from app.models.paper_trading import CostProfileName
from app.services.market_scan_probability import ProbabilitySample, stable_probability_hash
from app.services.market_scan_probability_labels import (
    LEGACY_PROBABILITY_LABEL_VERSION,
    PROBABILITY_DEFAULT_HORIZONS,
    PROBABILITY_LABEL_VERSION,
    ProbabilityLabelConfig,
    ProbabilityLabelOutcome,
    build_probability_label_outcomes,
    probability_label_contract,
)
from app.services.market_scan_probability_research import ProbabilityResearchRow
from app.services.market_scan_probability_source import (
    load_probability_source_snapshot,
    verify_probability_source_snapshot,
)
from app.services.paper_trading_costs import available_cost_profiles
from app.services.trading_calendar import (
    ASHARE_TIMEZONE,
    TradingCalendarCoverageError,
    calendar_status,
    is_trading_day,
    latest_expected_daily_kline_date,
    next_trade_dates,
)
from app.utils.clock import market_now

exclusive_atomic_publish = publish_market_scan_artifact


PROBABILITY_OUTCOME_ARTIFACT_SCHEMA_VERSION = "market-scan-probability-outcome-artifact-v1"
PROBABILITY_OUTCOME_PAYLOAD_CONTRACT_VERSION = "market-scan-probability-outcomes-v1"
PROBABILITY_OUTCOME_CALENDAR_CONTRACT_VERSION = "trusted-fixed-exchange-session-grid-v1"
PROBABILITY_OUTCOME_BAR_EVIDENCE_VERSION = "qfq-daily-fixed-session-bar-evidence-v1"
PROBABILITY_OUTCOME_DIGEST_ALGORITHM = "sha256"
PROBABILITY_OUTCOME_DIGEST_SCOPE = "payload"
PROBABILITY_OUTCOME_INTEGRITY_NOTICE = "integrity_digest_not_a_signature"
PROBABILITY_OUTCOME_COMPRESSION = "gzip-canonical-json-v1"
PROBABILITY_OUTCOME_MINIMUM_LABEL_COVERAGE = 0.95
PROBABILITY_OUTCOME_MAX_COMPRESSED_BYTES = 128 * 1024 * 1024
PROBABILITY_OUTCOME_MAX_UNCOMPRESSED_BYTES = 512 * 1024 * 1024

ProbabilityOutcomeTarget = Literal["net_excess_positive", "absolute_net_positive"]
ProbabilityKlineLoader = Callable[[str, tuple[str, ...]], Sequence[Kline]]

_TOP_LEVEL_KEYS = frozenset({"schema_version", "generated_at", "payload", "integrity"})
_INTEGRITY_KEYS = frozenset({"algorithm", "scope", "integrity_digest", "notice", "compression"})
_PAYLOAD_KEYS = frozenset(
    {
        "contract_version",
        "generated_at",
        "as_of_date",
        "source",
        "cohort",
        "label_contract",
        "label_contract_digest",
        "calendar_contract",
        "records",
        "quality",
        "limitations",
    }
)
_SOURCE_KEYS = frozenset(
    {
        "schema_version",
        "payload_contract_version",
        "run_id",
        "quote_date",
        "data_date",
        "as_of",
        "captured_at",
        "record_count",
        "integrity_digest",
    }
)
_COHORT_KEYS = frozenset({"mode", "scope", "rule_version"})
_CALENDAR_KEYS = frozenset(
    {
        "version",
        "quote_date",
        "entry_session_date",
        "future_sessions",
        "horizon_exit_sessions",
        "session_grid_digest",
        "calendar_source",
        "calendar_provider_source",
        "calendar_updated_at",
        "missing_bar_policy",
    }
)
_RECORD_KEYS = frozenset(
    {
        "symbol",
        "feature_vector_digest",
        "source_evidence_digest",
        "instrument",
        "bar_evidence",
        "horizons",
    }
)
_INSTRUMENT_KEYS = frozenset({"market", "list_date", "is_st", "quote_amount", "adjustment_mode"})
_BAR_EVIDENCE_KEYS = frozenset({"version", "requested_dates", "observed_dates", "missing_dates", "bars", "bar_set_digest"})
_BAR_KEYS = frozenset(
    {
        "date",
        "open",
        "close",
        "high",
        "low",
        "volume",
        "adjustment_mode",
        "as_of",
        "data_version",
        "contract_version",
        "source",
        "fallback_used",
    }
)
_HORIZON_STATE_KEYS = frozenset({"horizon", "target_session_date", "maturity", "outcome"})
_OUTCOME_KEYS = frozenset(
    {
        "horizon",
        "status",
        "reason",
        "label",
        "gross_return",
        "net_return",
        "cost_drag",
        "entry_date",
        "exit_date",
        "entry_price",
        "exit_price",
        "model_limited",
        "rule_profile_verified",
        "daily_bar_model_limited",
    }
)
_QUALITY_KEYS = frozenset(
    {
        "source_record_count",
        "record_count",
        "record_coverage",
        "point_in_time_evidence_coverage",
        "horizons",
    }
)
_HORIZON_QUALITY_KEYS = frozenset(
    {
        "horizon",
        "target_session_date",
        "mature",
        "record_count",
        "mature_record_count",
        "data_available_record_count",
        "modelled_record_count",
        "unfilled_record_count",
        "data_unavailable_record_count",
        "eligible_observation_count",
        "label_coverage",
        "available_for_study",
        "minimum_label_coverage",
    }
)
_OUTCOME_FILENAME = re.compile(r"market-scan-probability-outcomes-run-(\d+)-through-(\d{4}-\d{2}-\d{2})-([0-9a-f]{64})\.json\.gz")
_SHA256 = re.compile(r"[0-9a-f]{64}")
_SYMBOL = re.compile(r"\d{6}\.(SH|SZ|BJ)")


class ProbabilityOutcomeError(ValueError):
    """Raised when probability outcome evidence is unsafe or inconsistent."""


class ProbabilityOutcomeSemanticDriftError(ProbabilityOutcomeError):
    """Raised for intact legacy evidence that cannot authorize current replay."""

    def __init__(
        self,
        message: str,
        *,
        run_id: int | None = None,
        as_of_date: str | None = None,
        generated_at: str | None = None,
        integrity_digest: str | None = None,
        source_digest: str | None = None,
    ) -> None:
        super().__init__(message)
        self.run_id = run_id
        self.as_of_date = as_of_date
        self.generated_at = generated_at
        self.integrity_digest = integrity_digest
        self.source_digest = source_digest


class _RecordSemanticDriftError(ProbabilityOutcomeError):
    """Internal marker accumulated until every sibling record is verified."""

    def __init__(self, message: str, record: dict[str, object]) -> None:
        super().__init__(message)
        self.record = record


def build_probability_outcome_artifact(
    source_snapshot: Mapping[str, object] | str | Path,
    kline_rows_by_symbol: Mapping[str, Sequence[Kline]],
    *,
    generated_at: str,
    as_of_date: str,
    config: ProbabilityLabelConfig | None = None,
    database_path: str | Path | None = None,
) -> dict[str, object]:
    """Build and deeply verify one fixed-session outcome artifact.

    ``as_of_date`` must be the trusted latest completed daily-K session chosen
    by the caller.  Rows after that date are ignored and can never influence an
    outcome.  A missing entry or exit session remains unavailable; it is never
    replaced by the next observed bar.
    """
    generated = _timestamp(generated_at, "generated_at")
    payload = _build_outcome_payload(
        _source_artifact(source_snapshot),
        kline_rows_by_symbol,
        generated_at=generated,
        as_of_date=as_of_date,
        config=config or ProbabilityLabelConfig(),
    )
    artifact: dict[str, object] = {
        "schema_version": PROBABILITY_OUTCOME_ARTIFACT_SCHEMA_VERSION,
        "generated_at": generated,
        "payload": payload,
        "integrity": _outcome_integrity(payload),
    }
    return verify_probability_outcome_artifact(artifact)


def _build_outcome_payload(
    source: Mapping[str, object],
    rows_by_symbol: Mapping[str, Sequence[Kline]],
    *,
    generated_at: str,
    as_of_date: str,
    config: ProbabilityLabelConfig,
) -> dict[str, object]:
    if config.horizons != PROBABILITY_DEFAULT_HORIZONS:
        raise ProbabilityOutcomeError("official outcome artifact 必须严格覆盖 H1/H5/H20")
    effective_as_of = _date_text(as_of_date, "as_of_date")
    source_payload = _mapping(source["payload"], "source.payload")
    source_run = _mapping(source_payload["run"], "source.payload.run")
    quote_date = _date_text(source_run["quote_date"], "source.run.quote_date")
    _validate_outcome_as_of(effective_as_of, quote_date)
    calendar = _trusted_calendar_contract(quote_date, config.horizons)
    label_contract = probability_label_contract(config)
    requested_dates = _requested_dates(calendar, effective_as_of)
    records = [
        _build_record(
            raw,
            source_run=source_run,
            calendar=calendar,
            as_of_date=effective_as_of,
            requested_dates=requested_dates,
            rows=rows_by_symbol.get(str(_mapping(raw, "source.records[]")["symbol"]), ()),
            config=config,
        )
        for raw in _sequence(source_payload["records"], "source.payload.records")
    ]
    records.sort(key=lambda item: str(item["symbol"]))
    source_summary = _source_summary(source)
    return {
        "contract_version": PROBABILITY_OUTCOME_PAYLOAD_CONTRACT_VERSION,
        "generated_at": generated_at,
        "as_of_date": effective_as_of,
        "source": source_summary,
        "cohort": deepcopy(source_payload["cohort"]),
        "label_contract": label_contract,
        "label_contract_digest": stable_probability_hash(label_contract),
        "calendar_contract": calendar,
        "records": records,
        "quality": _quality(records, source_summary, config.horizons),
        "limitations": _limitations(),
    }


def _validate_outcome_as_of(as_of_date: str, quote_date: str) -> None:
    if as_of_date < quote_date:
        raise ProbabilityOutcomeError("outcome as_of_date 不能早于 source quote_date")
    if not is_trading_day(date.fromisoformat(as_of_date)):
        raise ProbabilityOutcomeError("outcome as_of_date 必须是可信交易日")


def _outcome_integrity(payload: Mapping[str, object]) -> dict[str, object]:
    return {
        "algorithm": PROBABILITY_OUTCOME_DIGEST_ALGORITHM,
        "scope": PROBABILITY_OUTCOME_DIGEST_SCOPE,
        "integrity_digest": probability_outcome_payload_digest(payload),
        "notice": PROBABILITY_OUTCOME_INTEGRITY_NOTICE,
        "compression": PROBABILITY_OUTCOME_COMPRESSION,
    }


def publish_probability_outcome_artifact(
    directory: str | Path,
    source_snapshot: Mapping[str, object] | str | Path,
    kline_rows_by_symbol: Mapping[str, Sequence[Kline]],
    *,
    generated_at: str,
    as_of_date: str,
    config: ProbabilityLabelConfig | None = None,
    database_path: str | Path | None = None,
) -> dict[str, object]:
    """Build and exclusively publish one deterministic gzip artifact."""
    artifact = build_probability_outcome_artifact(
        source_snapshot,
        kline_rows_by_symbol,
        generated_at=generated_at,
        as_of_date=as_of_date,
        config=config,
    )
    return publish_built_probability_outcome_artifact(directory, artifact, database_path=database_path)


def publish_built_probability_outcome_artifact(
    directory: str | Path,
    artifact: Mapping[str, object],
    *,
    database_path: str | Path | None = None,
) -> dict[str, object]:
    """Publish an already verified candidate without rebuilding 5,000+ rows."""
    verified = verify_probability_outcome_artifact(artifact)
    payload = _mapping(verified["payload"], "payload")
    source = _mapping(payload["source"], "payload.source")
    target = Path(directory).expanduser().absolute() / probability_outcome_artifact_filename(verified)
    try:
        require_project_managed_artifact_database(target, database_path, "research/market_scan_probability_outcomes")
    except MarketScanArtifactLeaseError as exc:
        raise ProbabilityOutcomeError(str(exc)) from exc
    run_id = int(cast(int, source["run_id"]))
    if database_path is None:
        _write_artifact(target, verified)
    else:
        try:
            with verified_market_scan_artifact_publication(
                database_path,
                target,
                (run_id,),
                managed_directory="research/market_scan_probability_outcomes",
            ):
                _write_artifact(target, verified)
        except MarketScanArtifactLeaseError as exc:
            raise ProbabilityOutcomeError("outcome artifact 来源批次已失效") from exc
    return _artifact_info(target, verified, run_id=run_id)


def probability_outcome_required_dates(
    source_snapshot: Mapping[str, object] | str | Path,
    *,
    as_of_date: str,
    config: ProbabilityLabelConfig | None = None,
) -> tuple[str, ...]:
    """Return the small exact-session set needed by the matured horizons."""
    source = _source_artifact(source_snapshot)
    settings = config or ProbabilityLabelConfig()
    payload = _mapping(source["payload"], "source.payload")
    run = _mapping(payload["run"], "source.run")
    quote_date = _date_text(run["quote_date"], "source.run.quote_date")
    effective_as_of = _date_text(as_of_date, "as_of_date")
    _validate_outcome_as_of(effective_as_of, quote_date)
    calendar = _trusted_calendar_contract(quote_date, settings.horizons)
    return _requested_dates(calendar, effective_as_of)


def mature_probability_source_snapshot(
    source_path: str | Path,
    outcome_directory: str | Path,
    kline_loader: ProbabilityKlineLoader,
    *,
    now: datetime | None = None,
    generated_at: str | None = None,
    as_of_date: str | None = None,
    config: ProbabilityLabelConfig | None = None,
    database_path: str | Path | None = None,
) -> dict[str, object]:
    """Synchronously load bars, mature one source, and publish its artifact.

    ``kline_loader`` receives ``(symbol, exact_requested_dates)``.  It may return
    fewer rows; missing fixed sessions are sealed as unavailable and never
    shifted.  In normal operation ``as_of_date`` is omitted and derived from
    the latest expected published daily K line at ``now``.  The explicit date
    exists for deterministic replay and tests.
    """
    source = load_probability_source_snapshot(source_path)
    settings = config or ProbabilityLabelConfig()
    effective_as_of = _date_text(as_of_date, "as_of_date") if as_of_date is not None else latest_expected_daily_kline_date(now).isoformat()
    effective_generated_at = _generated_at(generated_at, now)
    source_payload = _mapping(source["payload"], "source.payload")
    quote_date = _date_text(_mapping(source_payload["run"], "source.run")["quote_date"], "quote_date")
    calendar = _trusted_calendar_contract(quote_date, settings.horizons)
    requested_dates = _requested_dates(calendar, effective_as_of)
    rows_by_symbol: dict[str, Sequence[Kline]] = {}
    if requested_dates:
        for raw in _sequence(source_payload["records"], "source.records"):
            symbol = _symbol(_mapping(raw, "source.records[]")["symbol"])
            try:
                rows_by_symbol[symbol] = tuple(kline_loader(symbol, requested_dates))
            except Exception as exc:
                raise ProbabilityOutcomeError(f"{symbol} outcome K线加载失败") from exc
    return publish_probability_outcome_artifact(
        outcome_directory,
        source,
        rows_by_symbol,
        generated_at=effective_generated_at,
        as_of_date=effective_as_of,
        config=settings,
        database_path=database_path,
    )


def verify_probability_outcome_artifact(artifact: Mapping[str, object]) -> dict[str, object]:
    """Fail closed on structure, digests, fixed dates, and semantic replay."""
    normalized = _json_mapping(artifact, "artifact")
    _exact_keys(normalized, _TOP_LEVEL_KEYS, "artifact")
    if normalized["schema_version"] != PROBABILITY_OUTCOME_ARTIFACT_SCHEMA_VERSION:
        raise ProbabilityOutcomeError("outcome artifact schema_version 不受支持")
    generated_at = _timestamp(normalized["generated_at"], "artifact.generated_at")
    payload = _validate_payload(_mapping(normalized["payload"], "artifact.payload"), generated_at)
    integrity = _mapping(normalized["integrity"], "artifact.integrity")
    _exact_keys(integrity, _INTEGRITY_KEYS, "artifact.integrity")
    expected = {
        "algorithm": PROBABILITY_OUTCOME_DIGEST_ALGORITHM,
        "scope": PROBABILITY_OUTCOME_DIGEST_SCOPE,
        "notice": PROBABILITY_OUTCOME_INTEGRITY_NOTICE,
        "compression": PROBABILITY_OUTCOME_COMPRESSION,
    }
    if any(integrity.get(name) != value for name, value in expected.items()):
        raise ProbabilityOutcomeError("outcome artifact integrity contract 冲突")
    digest = _sha256(integrity.get("integrity_digest"), "integrity.integrity_digest")
    if digest != probability_outcome_payload_digest(payload):
        raise ProbabilityOutcomeError("outcome artifact payload digest 不一致")
    return {
        "schema_version": PROBABILITY_OUTCOME_ARTIFACT_SCHEMA_VERSION,
        "generated_at": generated_at,
        "payload": payload,
        "integrity": dict(integrity),
    }


def load_probability_outcome_artifact(path: str | Path) -> dict[str, object]:
    """Load one canonical deterministic-gzip artifact and verify its filename."""
    source = Path(path).expanduser().absolute()
    encoded = _read_artifact_bytes(source)
    decoded = _decompress(encoded, source)
    artifact = _decode_artifact(decoded, source)
    mechanically_verified = _verify_loaded_artifact_envelope(source, encoded, artifact)
    try:
        return verify_probability_outcome_artifact(mechanically_verified)
    except ProbabilityOutcomeSemanticDriftError as exc:
        raise _bound_semantic_drift(exc, mechanically_verified) from exc


def _bound_semantic_drift(
    error: ProbabilityOutcomeSemanticDriftError,
    artifact: Mapping[str, object],
) -> ProbabilityOutcomeSemanticDriftError:
    payload = _mapping(artifact["payload"], "payload")
    source = _mapping(payload["source"], "payload.source")
    integrity = _mapping(artifact["integrity"], "integrity")
    return ProbabilityOutcomeSemanticDriftError(
        str(error),
        run_id=_positive_integer(source["run_id"], "payload.source.run_id"),
        as_of_date=_date_text(payload["as_of_date"], "payload.as_of_date"),
        generated_at=_timestamp(artifact["generated_at"], "artifact.generated_at"),
        integrity_digest=_sha256(integrity["integrity_digest"], "integrity.integrity_digest"),
        source_digest=_sha256(source["integrity_digest"], "payload.source.integrity_digest"),
    )


def _verify_loaded_artifact_envelope(
    path: Path,
    encoded: bytes,
    artifact: Mapping[str, object],
) -> dict[str, object]:
    """Verify immutable bytes before a known legacy semantic error may escape."""
    normalized = _json_mapping(artifact, "artifact")
    _exact_keys(normalized, _TOP_LEVEL_KEYS, "artifact")
    if normalized["schema_version"] != PROBABILITY_OUTCOME_ARTIFACT_SCHEMA_VERSION:
        raise ProbabilityOutcomeError("outcome artifact schema_version 不受支持")
    _timestamp(normalized["generated_at"], "artifact.generated_at")
    payload = _mapping(normalized["payload"], "artifact.payload")
    integrity = _mapping(normalized["integrity"], "artifact.integrity")
    _exact_keys(integrity, _INTEGRITY_KEYS, "artifact.integrity")
    expected = {
        "algorithm": PROBABILITY_OUTCOME_DIGEST_ALGORITHM,
        "scope": PROBABILITY_OUTCOME_DIGEST_SCOPE,
        "notice": PROBABILITY_OUTCOME_INTEGRITY_NOTICE,
        "compression": PROBABILITY_OUTCOME_COMPRESSION,
    }
    if any(integrity.get(name) != value for name, value in expected.items()):
        raise ProbabilityOutcomeError("outcome artifact integrity contract 冲突")
    digest = _sha256(integrity.get("integrity_digest"), "integrity.integrity_digest")
    if digest != probability_outcome_payload_digest(payload):
        raise ProbabilityOutcomeError("outcome artifact payload digest 不一致")
    _validate_filename(path, normalized)
    if encoded != _compressed_canonical_artifact_bytes(normalized):
        raise ProbabilityOutcomeError(f"outcome artifact 不是规范确定性 gzip：{path}")
    return normalized


def list_probability_outcome_artifacts(
    directory: str | Path,
    *,
    run_id: int | None = None,
) -> list[dict[str, object]]:
    """List deeply verified outcome artifacts, optionally for one run."""
    root = Path(directory).expanduser().absolute()
    try:
        if not path_has_only_trusted_aliases(root):
            raise ProbabilityOutcomeError(f"outcome artifact 路径不是目录：{root}")
        facts = root.lstat()
    except ProbabilityOutcomeError:
        raise
    except FileNotFoundError:
        return []
    except (OSError, RuntimeError) as exc:
        raise ProbabilityOutcomeError(f"outcome artifact 目录无法读取：{root}") from exc
    if not stat.S_ISDIR(facts.st_mode):
        raise ProbabilityOutcomeError(f"outcome artifact 路径不是目录：{root}")
    output: list[dict[str, object]] = []
    for path in sorted(root.glob("market-scan-probability-outcomes-run-*.json.gz")):
        encoded_run, _as_of, _digest = _filename_identity(path)
        if run_id is None or encoded_run == _positive_integer(run_id, "run_id"):
            artifact = load_probability_outcome_artifact(path)
            output.append(_artifact_info(path, artifact, run_id=encoded_run))
    return sorted(output, key=lambda item: (str(item["as_of_date"]), str(item["generated_at"]), str(item["digest"])))


def load_probability_outcome_artifact_for_run(
    directory: str | Path,
    run_id: int,
) -> dict[str, object] | None:
    """Select the unique latest-as-of artifact for a source run."""
    candidates = list_probability_outcome_artifacts(directory, run_id=run_id)
    if not candidates:
        return None
    newest_as_of = max(str(item["as_of_date"]) for item in candidates)
    latest = [item for item in candidates if item["as_of_date"] == newest_as_of]
    newest_at = max(_timestamp_order(item["generated_at"], "generated_at") for item in latest)
    newest = [item for item in latest if _timestamp_order(item["generated_at"], "generated_at") == newest_at]
    digests = {str(item["digest"]) for item in newest}
    if len(digests) != 1:
        raise ProbabilityOutcomeError(f"run {run_id} 在同一 as_of/generated_at 存在冲突 outcome artifacts")
    return load_probability_outcome_artifact(cast(str, newest[0]["path"]))


def probability_outcome_payload_digest(payload: Mapping[str, object]) -> str:
    """Return the SHA-256 content address of a normalized payload."""
    return sha256_hex(_canonical_json(payload))


def probability_outcome_artifact_filename(artifact: Mapping[str, object]) -> str:
    """Return the only accepted filename for an outcome artifact."""
    verified = verify_probability_outcome_artifact(artifact)
    payload = _mapping(verified["payload"], "payload")
    source = _mapping(payload["source"], "payload.source")
    integrity = _mapping(verified["integrity"], "integrity")
    return content_addressed_filename(
        "market-scan-probability-outcomes-run",
        (int(cast(int, source["run_id"])), "through", str(payload["as_of_date"])),
        str(integrity["integrity_digest"]),
        ".json.gz",
    )


def probability_research_rows_from_outcome_artifacts(
    source_snapshots: Sequence[Mapping[str, object] | str | Path],
    outcome_artifacts: Sequence[Mapping[str, object] | str | Path],
) -> tuple[ProbabilityResearchRow, ...]:
    """Strictly join compact outcomes back to their immutable source rows."""
    selected = _selected_artifacts(outcome_artifacts)
    _require_compatible_corpus(selected)
    sources = _selected_sources(source_snapshots)
    rows: list[ProbabilityResearchRow] = []
    occupied_sessions: dict[tuple[str, str, str, str], int] = {}
    for artifact in selected:
        artifact_rows, cohort, run_id, session_date = _research_rows_for_artifact(artifact, sources)
        session_key = (str(cohort["mode"]), str(cohort["scope"]), str(cohort["rule_version"]), session_date)
        previous_run = occupied_sessions.setdefault(session_key, run_id)
        if previous_run != run_id:
            raise ProbabilityOutcomeError("outcome corpus 同一 cohort/session 含多个 run")
        rows.extend(artifact_rows)
    return tuple(sorted(rows, key=lambda row: (row.session_date, row.run_id, row.symbol)))


def probability_samples_from_outcome_artifacts(
    source_snapshots: Sequence[Mapping[str, object] | str | Path],
    outcome_artifacts: Sequence[Mapping[str, object] | str | Path],
    *,
    horizon: int,
    target: ProbabilityOutcomeTarget,
) -> tuple[ProbabilitySample, ...]:
    """Project complete outcome artifacts into model-ready samples."""
    if horizon not in PROBABILITY_DEFAULT_HORIZONS:
        raise ProbabilityOutcomeError("probability sample horizon 必须是 1/5/20")
    if target not in {"net_excess_positive", "absolute_net_positive"}:
        raise ProbabilityOutcomeError("probability sample target 不受支持")
    rows = probability_research_rows_from_outcome_artifacts(source_snapshots, outcome_artifacts)
    benchmarks = _sample_market_benchmarks(rows, horizon)
    return tuple(_probability_sample(row, horizon, target, benchmarks.get(row.run_id)) for row in rows)


def _sample_market_benchmarks(
    rows: Sequence[ProbabilityResearchRow],
    horizon: int,
) -> dict[int, float]:
    benchmark_values: dict[int, list[float]] = defaultdict(list)
    for row in rows:
        outcome = row.labels.get(horizon)
        if outcome is not None and _modelled_outcome(outcome):
            benchmark_values[row.run_id].append(cast(float, outcome.net_return))
    return {run_id: sum(values) / len(values) for run_id, values in benchmark_values.items() if values}


def _probability_sample(
    row: ProbabilityResearchRow,
    horizon: int,
    target: ProbabilityOutcomeTarget,
    benchmark: float | None,
) -> ProbabilitySample:
    outcome = row.labels.get(horizon)
    executable = _modelled_outcome(outcome) and row.source_evidence_digest is not None
    net_return = outcome.net_return if executable and outcome is not None else None
    net_excess = net_return - benchmark if net_return is not None and benchmark is not None else None
    target_return = net_excess if target == "net_excess_positive" else net_return
    return ProbabilitySample(
        sample_id=f"{row.run_id}:{row.symbol}:{horizon}:{target}",
        session_date=row.session_date,
        features=row.features,
        target=int(target_return > 0) if target_return is not None else None,
        executable=bool(executable),
        net_return=net_return,
        net_excess_return=net_excess,
    )


def probability_outcome_corpus_progress(
    artifacts: Sequence[Mapping[str, object] | str | Path],
) -> dict[str, object]:
    """Aggregate archived/mature/available counts for source-research progress."""
    selected = _selected_artifacts(artifacts)
    _require_compatible_corpus(selected)
    output: dict[str, object] = {}
    for horizon in PROBABILITY_DEFAULT_HORIZONS:
        archived_sessions = len(selected)
        observations = 0
        mature_sessions = 0
        available_sessions = 0
        mature_observations = 0
        eligible_observations = 0
        for artifact in selected:
            payload = _mapping(artifact["payload"], "payload")
            source = _mapping(payload["source"], "payload.source")
            quality = _mapping(_mapping(payload["quality"], "payload.quality")["horizons"], "quality.horizons")
            horizon_quality = _mapping(quality[str(horizon)], f"quality.horizons.{horizon}")
            observations += int(cast(int, source["record_count"]))
            if horizon_quality["mature"] is True:
                mature_sessions += 1
                mature_observations += int(cast(int, horizon_quality["mature_record_count"]))
                eligible_observations += int(cast(int, horizon_quality["eligible_observation_count"]))
                if horizon_quality["available_for_study"] is True:
                    available_sessions += 1
        output[str(horizon)] = {
            "archived_independent_session_count": archived_sessions,
            "mature_label_session_count": mature_sessions,
            "available_independent_session_count": available_sessions,
            "observation_count": observations,
            "mature_observation_count": mature_observations,
            "eligible_observation_count": eligible_observations,
            "label_coverage": eligible_observations / mature_observations if mature_observations else 0.0,
            "minimum_label_coverage": PROBABILITY_OUTCOME_MINIMUM_LABEL_COVERAGE,
        }
    return output


def _build_record(
    raw: object,
    *,
    source_run: Mapping[str, object],
    calendar: Mapping[str, object],
    as_of_date: str,
    requested_dates: tuple[str, ...],
    rows: Sequence[Kline],
    config: ProbabilityLabelConfig,
) -> dict[str, object]:
    source_record = _mapping(raw, "source.records[]")
    symbol = _symbol(source_record["symbol"])
    instrument = _mapping(source_record["instrument"], f"source record {symbol}.instrument")
    normalized_instrument = {
        "market": _text(instrument["market"], f"{symbol}.market"),
        "list_date": _optional_date_text(instrument.get("list_date"), f"{symbol}.list_date"),
        "is_st": _boolean(instrument["is_st"], f"{symbol}.is_st"),
        "quote_amount": _nonnegative_number(instrument["quote_amount"], f"{symbol}.quote_amount"),
        "adjustment_mode": _text(instrument["adjustment_mode"], f"{symbol}.adjustment_mode"),
    }
    if normalized_instrument["adjustment_mode"] != "qfq":
        raise ProbabilityOutcomeError(f"{symbol} source instrument adjustment_mode 必须为 qfq")
    bar_evidence = _bar_evidence(symbol, rows, requested_dates, as_of_date)
    normalized_rows = [_kline_from_mapping(_mapping(item, "bar evidence bar")) for item in cast(list[object], bar_evidence["bars"])]
    horizons = _build_horizon_states(
        symbol=symbol,
        quote_date=str(source_run["quote_date"]),
        instrument=normalized_instrument,
        calendar=calendar,
        as_of_date=as_of_date,
        rows=normalized_rows,
        config=config,
    )
    return {
        "symbol": symbol,
        "feature_vector_digest": _sha256(source_record["feature_vector_digest"], f"{symbol}.feature_vector_digest"),
        "source_evidence_digest": _sha256(source_record["source_evidence_digest"], f"{symbol}.source_evidence_digest"),
        "instrument": normalized_instrument,
        "bar_evidence": bar_evidence,
        "horizons": horizons,
    }


def _build_horizon_states(
    *,
    symbol: str,
    quote_date: str,
    instrument: Mapping[str, object],
    calendar: Mapping[str, object],
    as_of_date: str,
    rows: Sequence[Kline],
    config: ProbabilityLabelConfig,
) -> dict[str, object]:
    future_dates = tuple(str(value) for value in _sequence(calendar["future_sessions"], "calendar.future_sessions"))
    exits = _mapping(calendar["horizon_exit_sessions"], "calendar.horizon_exit_sessions")
    bar_dates = {row.date for row in rows}
    outcomes = build_probability_label_outcomes(
        symbol=symbol,
        market=str(instrument["market"]),
        list_date=cast(str | None, instrument["list_date"]),
        is_st=cast(bool, instrument["is_st"]),
        quote_date=quote_date,
        amount=float(cast(float, instrument["quote_amount"])),
        rows=rows,
        eligible_dates=future_dates,
        config=config,
    )
    states: dict[str, object] = {}
    for horizon in config.horizons:
        target_date = str(exits[str(horizon)])
        if target_date > as_of_date:
            states[str(horizon)] = {
                "horizon": horizon,
                "target_session_date": target_date,
                "maturity": "not_mature",
                "outcome": None,
            }
            continue
        outcome = _fixed_session_outcome(
            outcomes[horizon],
            horizon=horizon,
            quote_date=quote_date,
            future_dates=future_dates,
            bar_dates=bar_dates,
        )
        states[str(horizon)] = {
            "horizon": horizon,
            "target_session_date": target_date,
            "maturity": "mature",
            "outcome": asdict(outcome),
        }
    return states


def _fixed_session_outcome(
    outcome: ProbabilityLabelOutcome,
    *,
    horizon: int,
    quote_date: str,
    future_dates: Sequence[str],
    bar_dates: set[str],
) -> ProbabilityLabelOutcome:
    entry_date = future_dates[0]
    exit_date = future_dates[horizon]
    previous_exit_date = future_dates[horizon - 1]
    missing: tuple[str, str] | None = None
    for required_date, reason in (
        (quote_date, "fixed_entry_previous_session_bar_missing"),
        (entry_date, "fixed_entry_session_bar_missing"),
        (previous_exit_date, "fixed_exit_previous_session_bar_missing"),
        (exit_date, "fixed_exit_session_bar_missing"),
    ):
        if required_date not in bar_dates:
            missing = required_date, reason
            break
    if missing is None:
        return outcome
    missing_date, reason = missing
    return ProbabilityLabelOutcome(
        horizon=horizon,
        status="data_unavailable",
        reason=reason,
        entry_date=entry_date,
        exit_date=exit_date,
        rule_profile_verified=outcome.rule_profile_verified,
        model_limited=outcome.model_limited,
        daily_bar_model_limited=outcome.daily_bar_model_limited,
    )


def _bar_evidence(
    symbol: str,
    rows: Sequence[Kline],
    requested_dates: tuple[str, ...],
    as_of_date: str,
) -> dict[str, object]:
    requested = set(requested_dates)
    selected: dict[str, dict[str, object]] = {}
    for row in rows:
        normalized = _normalized_bar(row, symbol, as_of_date)
        row_date = str(normalized["date"])
        if row_date not in requested:
            continue
        previous = selected.get(row_date)
        if previous is not None and previous != normalized:
            raise ProbabilityOutcomeError(f"{symbol} 固定会话 {row_date} 存在冲突K线")
        selected[row_date] = normalized
    bars = [selected[value] for value in requested_dates if value in selected]
    observed_dates = [str(item["date"]) for item in bars]
    evidence: dict[str, object] = {
        "version": PROBABILITY_OUTCOME_BAR_EVIDENCE_VERSION,
        "requested_dates": list(requested_dates),
        "observed_dates": observed_dates,
        "missing_dates": [value for value in requested_dates if value not in selected],
        "bars": bars,
        "bar_set_digest": stable_probability_hash(bars),
    }
    return evidence


def _normalized_bar(row: Kline, symbol: str, as_of_date: str) -> dict[str, object]:
    row_date = _date_text(row.date, f"{symbol}.bar.date")
    if row_date > as_of_date:
        raise ProbabilityOutcomeError(f"{symbol} outcome K线晚于 as_of_date")
    values = {
        "open": _positive_number(row.open, f"{symbol}.{row_date}.open"),
        "close": _positive_number(row.close, f"{symbol}.{row_date}.close"),
        "high": _positive_number(row.high, f"{symbol}.{row_date}.high"),
        "low": _positive_number(row.low, f"{symbol}.{row_date}.low"),
        "volume": _nonnegative_number(row.volume, f"{symbol}.{row_date}.volume"),
    }
    if values["high"] < max(values["open"], values["close"], values["low"]):
        raise ProbabilityOutcomeError(f"{symbol} {row_date} high 与 OHLC 冲突")
    if values["low"] > min(values["open"], values["close"], values["high"]):
        raise ProbabilityOutcomeError(f"{symbol} {row_date} low 与 OHLC 冲突")
    if row.adjustment_mode != "qfq" or row.contract_version != DAILY_KLINE_CONTRACT_VERSION:
        raise ProbabilityOutcomeError(f"{symbol} {row_date} K线不是 qfq/{DAILY_KLINE_CONTRACT_VERSION}")
    data_version = _text(row.data_version, f"{symbol}.{row_date}.data_version")
    if data_version == "unknown":
        raise ProbabilityOutcomeError(f"{symbol} {row_date} K线 data_version 未知")
    return {
        "date": row_date,
        **values,
        "adjustment_mode": "qfq",
        "as_of": _optional_text(row.as_of),
        "data_version": data_version,
        "contract_version": DAILY_KLINE_CONTRACT_VERSION,
        "source": _text(row.source, f"{symbol}.{row_date}.source"),
        "fallback_used": bool(row.fallback_used),
    }


def _trusted_calendar_contract(quote_date: str, horizons: Sequence[int]) -> dict[str, object]:
    signal_date = date.fromisoformat(quote_date)
    if not is_trading_day(signal_date):
        raise ProbabilityOutcomeError(f"source quote_date 不是可信交易日：{quote_date}")
    future = _fixed_future_sessions(signal_date, horizons)
    future_dates = [value.isoformat() for value in future]
    status = calendar_status(future[-1])
    if not status.covered:
        raise ProbabilityOutcomeError("可信交易日历没有覆盖最长 outcome 目标会话")
    exits = {str(horizon): future_dates[horizon] for horizon in horizons}
    grid_identity = {
        "quote_date": quote_date,
        "future_sessions": future_dates,
        "horizon_exit_sessions": exits,
    }
    return {
        "version": PROBABILITY_OUTCOME_CALENDAR_CONTRACT_VERSION,
        "quote_date": quote_date,
        "entry_session_date": future_dates[0],
        "future_sessions": future_dates,
        "horizon_exit_sessions": exits,
        "session_grid_digest": stable_probability_hash(grid_identity),
        "calendar_source": status.source.value,
        "calendar_provider_source": status.provider_source,
        "calendar_updated_at": (
            status.updated_at.replace(tzinfo=ASHARE_TIMEZONE).isoformat()
            if status.updated_at is not None and status.updated_at.tzinfo is None
            else status.updated_at.isoformat()
            if status.updated_at is not None
            else None
        ),
        "missing_bar_policy": "fixed_session_unavailable_never_shift_v1",
    }


def _requested_dates(calendar: Mapping[str, object], as_of_date: str) -> tuple[str, ...]:
    quote_date = str(calendar["quote_date"])
    future = tuple(str(value) for value in _sequence(calendar["future_sessions"], "calendar.future_sessions"))
    exits = _mapping(calendar["horizon_exit_sessions"], "calendar.horizon_exit_sessions")
    mature_horizons = [int(value) for value, target in exits.items() if str(target) <= as_of_date]
    if not mature_horizons:
        return ()
    required = {quote_date, future[0]}
    for horizon in mature_horizons:
        required.update((future[horizon - 1], future[horizon]))
    return tuple(value for value in (quote_date, *future) if value in required)


def _source_summary(source: Mapping[str, object]) -> dict[str, object]:
    payload = _mapping(source["payload"], "source.payload")
    run = _mapping(payload["run"], "source.payload.run")
    quality = _mapping(payload["quality"], "source.payload.quality")
    integrity = _mapping(source["integrity"], "source.integrity")
    return {
        "schema_version": str(source["schema_version"]),
        "payload_contract_version": str(payload["contract_version"]),
        "run_id": int(cast(int, run["run_id"])),
        "quote_date": str(run["quote_date"]),
        "data_date": str(run["data_date"]),
        "as_of": str(run["as_of"]),
        "captured_at": str(source["captured_at"]),
        "record_count": int(cast(int, quality["record_count"])),
        "integrity_digest": str(integrity["integrity_digest"]),
    }


def _quality(
    records: Sequence[Mapping[str, object]],
    source: Mapping[str, object],
    horizons: Sequence[int],
) -> dict[str, object]:
    expected = int(cast(int, source["record_count"]))
    horizon_quality = {str(horizon): _horizon_quality(records, horizon, expected) for horizon in horizons}
    return {
        "source_record_count": expected,
        "record_count": len(records),
        "record_coverage": len(records) / expected if expected else 0.0,
        "point_in_time_evidence_coverage": 1.0 if len(records) == expected and expected > 0 else 0.0,
        "horizons": horizon_quality,
    }


def _horizon_quality(
    records: Sequence[Mapping[str, object]],
    horizon: int,
    expected: int,
) -> dict[str, object]:
    states = [_mapping(_mapping(record["horizons"], "record.horizons")[str(horizon)], "horizon state") for record in records]
    mature = bool(states and all(state["maturity"] == "mature" for state in states))
    outcomes = [_mapping(state["outcome"], "horizon outcome") for state in states if state["maturity"] == "mature"]
    status_counts = {status: sum(outcome["status"] == status for outcome in outcomes) for status in ("modelled", "unfilled", "data_unavailable")}
    modelled = status_counts["modelled"]
    coverage = modelled / expected if mature and expected else 0.0
    return {
        "horizon": horizon,
        "target_session_date": states[0]["target_session_date"] if states else None,
        "mature": mature,
        "record_count": len(states),
        "mature_record_count": len(outcomes),
        "data_available_record_count": modelled + status_counts["unfilled"],
        "modelled_record_count": modelled,
        "unfilled_record_count": status_counts["unfilled"],
        "data_unavailable_record_count": status_counts["data_unavailable"],
        "eligible_observation_count": modelled,
        "label_coverage": coverage,
        "available_for_study": mature and coverage >= PROBABILITY_OUTCOME_MINIMUM_LABEL_COVERAGE,
        "minimum_label_coverage": PROBABILITY_OUTCOME_MINIMUM_LABEL_COVERAGE,
    }


def _validate_payload(payload: Mapping[str, object], generated_at: str) -> dict[str, object]:
    normalized = _json_mapping(payload, "payload")
    _exact_keys(normalized, _PAYLOAD_KEYS, "payload")
    if normalized["contract_version"] != PROBABILITY_OUTCOME_PAYLOAD_CONTRACT_VERSION:
        raise ProbabilityOutcomeError("outcome payload contract_version 不受支持")
    if _timestamp(normalized["generated_at"], "payload.generated_at") != generated_at:
        raise ProbabilityOutcomeError("outcome payload/top-level generated_at 冲突")
    as_of_date = _date_text(normalized["as_of_date"], "payload.as_of_date")
    source = _validate_source(_mapping(normalized["source"], "payload.source"), as_of_date)
    cohort = _validate_cohort(_mapping(normalized["cohort"], "payload.cohort"))
    label_contract, config = _validate_label_contract(_mapping(normalized["label_contract"], "payload.label_contract"))
    label_digest = _sha256(normalized["label_contract_digest"], "payload.label_contract_digest")
    if label_digest != stable_probability_hash(label_contract):
        raise ProbabilityOutcomeError("outcome label_contract_digest 不一致")
    calendar = _validate_calendar(
        _mapping(normalized["calendar_contract"], "payload.calendar_contract"),
        quote_date=str(source["quote_date"]),
        horizons=config.horizons,
    )
    records, drifted_symbols = _validate_records(
        _sequence(normalized["records"], "payload.records"),
        source=source,
        calendar=calendar,
        as_of_date=as_of_date,
        config=config,
    )
    if [record["symbol"] for record in records] != sorted(str(record["symbol"]) for record in records):
        raise ProbabilityOutcomeError("outcome records 必须按 symbol 排序")
    if len({str(record["symbol"]) for record in records}) != len(records):
        raise ProbabilityOutcomeError("outcome records 含重复 symbol")
    quality = _json_mapping(_mapping(normalized["quality"], "payload.quality"), "payload.quality")
    _exact_keys(quality, _QUALITY_KEYS, "payload.quality")
    expected_quality = _quality(records, source, config.horizons)
    if quality != expected_quality:
        raise ProbabilityOutcomeError("outcome quality 不能由 records 重放")
    limitations = [_text(value, "payload.limitations[]") for value in _sequence(normalized["limitations"], "payload.limitations")]
    if limitations != _limitations():
        raise ProbabilityOutcomeError("outcome limitations contract 冲突")
    if drifted_symbols:
        raise ProbabilityOutcomeSemanticDriftError(
            f"{','.join(drifted_symbols)} outcome 使用旧规则画像语义，不能授权当前重放",
            run_id=int(cast(int, source["run_id"])),
        )
    return {
        "contract_version": PROBABILITY_OUTCOME_PAYLOAD_CONTRACT_VERSION,
        "generated_at": generated_at,
        "as_of_date": as_of_date,
        "source": source,
        "cohort": cohort,
        "label_contract": label_contract,
        "label_contract_digest": label_digest,
        "calendar_contract": calendar,
        "records": records,
        "quality": quality,
        "limitations": limitations,
    }


def _validate_records(
    values: Sequence[object],
    *,
    source: Mapping[str, object],
    calendar: Mapping[str, object],
    as_of_date: str,
    config: ProbabilityLabelConfig,
) -> tuple[list[dict[str, object]], list[str]]:
    records: list[dict[str, object]] = []
    drifted_symbols: list[str] = []
    for item in values:
        try:
            record = _validate_record(
                _mapping(item, "payload.records[]"),
                source=source,
                calendar=calendar,
                as_of_date=as_of_date,
                config=config,
            )
        except _RecordSemanticDriftError as exc:
            record = exc.record
            drifted_symbols.append(str(record["symbol"]))
        records.append(record)
    return records, drifted_symbols


def _validate_source(source: Mapping[str, object], as_of_date: str) -> dict[str, object]:
    normalized = _json_mapping(source, "payload.source")
    _exact_keys(normalized, _SOURCE_KEYS, "payload.source")
    output = {
        "schema_version": _text(normalized["schema_version"], "source.schema_version"),
        "payload_contract_version": _text(normalized["payload_contract_version"], "source.payload_contract_version"),
        "run_id": _positive_integer(normalized["run_id"], "source.run_id"),
        "quote_date": _date_text(normalized["quote_date"], "source.quote_date"),
        "data_date": _date_text(normalized["data_date"], "source.data_date"),
        "as_of": _timestamp(normalized["as_of"], "source.as_of"),
        "captured_at": _timestamp(normalized["captured_at"], "source.captured_at"),
        "record_count": _positive_integer(normalized["record_count"], "source.record_count"),
        "integrity_digest": _sha256(normalized["integrity_digest"], "source.integrity_digest"),
    }
    if output["quote_date"] != output["data_date"] or str(output["quote_date"]) > as_of_date:
        raise ProbabilityOutcomeError("outcome source 日期身份冲突")
    return output


def _validate_cohort(cohort: Mapping[str, object]) -> dict[str, object]:
    normalized = _json_mapping(cohort, "payload.cohort")
    _exact_keys(normalized, _COHORT_KEYS, "payload.cohort")
    output: dict[str, object] = {name: _text(normalized[name], f"cohort.{name}") for name in _COHORT_KEYS}
    if output["mode"] != "official":
        raise ProbabilityOutcomeError("outcome artifact 只接受 official source cohort")
    return output


def _validate_label_contract(contract: Mapping[str, object]) -> tuple[dict[str, object], ProbabilityLabelConfig]:
    normalized = _json_mapping(contract, "payload.label_contract")
    horizons = tuple(_positive_integer(value, "label_contract.horizons[]") for value in _sequence(normalized.get("horizons"), "label_contract.horizons"))
    if horizons != tuple(sorted(set(horizons))) or any(value not in PROBABILITY_DEFAULT_HORIZONS for value in horizons):
        raise ProbabilityOutcomeError("outcome label horizons 必须是有序的 1/5/20 子集")
    profile_id = _text(normalized.get("cost_profile_id"), "label_contract.cost_profile_id")
    profiles = {profile.profile_id: profile for profile in available_cost_profiles()}
    if profile_id not in profiles:
        raise ProbabilityOutcomeError("outcome cost_profile_id 不受支持")
    profile_name = next(
        name
        for name in cast(tuple[CostProfileName, ...], ("base", "conservative", "stress"))
        if profiles[profile_id].profile_id == profile_id and profile_id.startswith(f"{name}-")
    )
    config = ProbabilityLabelConfig(
        horizons=horizons,
        cost_profile=profile_name,
        execution_notional=_positive_number(normalized.get("execution_notional"), "label_contract.execution_notional"),
        max_daily_participation_rate=_bounded_rate(
            normalized.get("max_daily_participation_rate"),
            "label_contract.max_daily_participation_rate",
        ),
    )
    label_version = _text(normalized.get("label_version"), "label_contract.label_version")
    if label_version not in {PROBABILITY_LABEL_VERSION, LEGACY_PROBABILITY_LABEL_VERSION}:
        raise ProbabilityOutcomeError("outcome label contract version 不受支持")
    expected = probability_label_contract(config, label_version=label_version)
    if normalized != expected:
        raise ProbabilityOutcomeError("outcome label contract 与当前可执行标签契约冲突")
    return expected, config


def _validate_calendar(
    calendar: Mapping[str, object],
    *,
    quote_date: str,
    horizons: Sequence[int],
) -> dict[str, object]:
    normalized = _json_mapping(calendar, "payload.calendar_contract")
    _exact_keys(normalized, _CALENDAR_KEYS, "payload.calendar_contract")
    if normalized["version"] != PROBABILITY_OUTCOME_CALENDAR_CONTRACT_VERSION:
        raise ProbabilityOutcomeError("outcome calendar contract version 不受支持")
    future, exits = _validated_calendar_grid(normalized, quote_date, horizons)
    if normalized["quote_date"] != quote_date or normalized["entry_session_date"] != future[0]:
        raise ProbabilityOutcomeError("outcome calendar quote/entry date 冲突")
    if normalized["missing_bar_policy"] != "fixed_session_unavailable_never_shift_v1":
        raise ProbabilityOutcomeError("outcome missing bar policy 不受支持")
    return {
        "version": PROBABILITY_OUTCOME_CALENDAR_CONTRACT_VERSION,
        "quote_date": quote_date,
        "entry_session_date": future[0],
        "future_sessions": future,
        "horizon_exit_sessions": exits,
        "session_grid_digest": str(normalized["session_grid_digest"]),
        "calendar_source": _text(normalized["calendar_source"], "calendar.calendar_source"),
        "calendar_provider_source": _optional_text(normalized["calendar_provider_source"]),
        "calendar_updated_at": _optional_timestamp(normalized["calendar_updated_at"], "calendar.calendar_updated_at"),
        "missing_bar_policy": "fixed_session_unavailable_never_shift_v1",
    }


def _validated_calendar_grid(
    normalized: Mapping[str, object],
    quote_date: str,
    horizons: Sequence[int],
) -> tuple[list[str], dict[str, object]]:
    future = [_date_text(value, "calendar.future_sessions[]") for value in _sequence(normalized["future_sessions"], "calendar.future_sessions")]
    if len(future) != max(horizons) + 1 or future != sorted(set(future)) or any(value <= quote_date for value in future):
        raise ProbabilityOutcomeError("outcome fixed future session grid 无效")
    expected_future = [value.isoformat() for value in _fixed_future_sessions(date.fromisoformat(quote_date), horizons)]
    if future != expected_future:
        raise ProbabilityOutcomeError("outcome future sessions 与可信交易日历冲突")
    exits = _json_mapping(_mapping(normalized["horizon_exit_sessions"], "calendar.horizon_exit_sessions"), "horizon exits")
    expected_exits = {str(horizon): future[horizon] for horizon in horizons}
    if exits != expected_exits:
        raise ProbabilityOutcomeError("outcome horizon exit sessions 与固定网格冲突")
    grid_identity = {"quote_date": quote_date, "future_sessions": future, "horizon_exit_sessions": exits}
    if normalized["session_grid_digest"] != stable_probability_hash(grid_identity):
        raise ProbabilityOutcomeError("outcome session_grid_digest 不一致")
    return future, exits


def _validate_record(
    record: Mapping[str, object],
    *,
    source: Mapping[str, object],
    calendar: Mapping[str, object],
    as_of_date: str,
    config: ProbabilityLabelConfig,
) -> dict[str, object]:
    normalized = _json_mapping(record, "payload.records[]")
    _exact_keys(normalized, _RECORD_KEYS, "payload.records[]")
    symbol = _symbol(normalized["symbol"])
    feature_digest = _sha256(normalized["feature_vector_digest"], f"{symbol}.feature_vector_digest")
    source_digest = _sha256(normalized["source_evidence_digest"], f"{symbol}.source_evidence_digest")
    instrument = _validate_instrument(_mapping(normalized["instrument"], f"{symbol}.instrument"), symbol)
    bar_evidence = _validate_bar_evidence(
        _mapping(normalized["bar_evidence"], f"{symbol}.bar_evidence"),
        symbol,
        requested_dates=_requested_dates(calendar, as_of_date),
        as_of_date=as_of_date,
    )
    rows = [_kline_from_mapping(_mapping(value, "bar")) for value in cast(list[object], bar_evidence["bars"])]
    expected_horizons = _build_horizon_states(
        symbol=symbol,
        quote_date=str(source["quote_date"]),
        instrument=instrument,
        calendar=calendar,
        as_of_date=as_of_date,
        rows=rows,
        config=config,
    )
    horizons = _json_mapping(_mapping(normalized["horizons"], f"{symbol}.horizons"), f"{symbol}.horizons")
    if horizons != expected_horizons:
        if _legacy_rule_profile_semantic_drift(horizons, expected_horizons):
            raise _RecordSemanticDriftError(
                f"{symbol} outcome 使用旧规则画像语义",
                {
                    "symbol": symbol,
                    "feature_vector_digest": feature_digest,
                    "source_evidence_digest": source_digest,
                    "instrument": instrument,
                    "bar_evidence": bar_evidence,
                    "horizons": horizons,
                },
            )
        raise ProbabilityOutcomeError(f"{symbol} outcome horizons 不能由固定会话K线重放")
    return {
        "symbol": symbol,
        "feature_vector_digest": feature_digest,
        "source_evidence_digest": source_digest,
        "instrument": instrument,
        "bar_evidence": bar_evidence,
        "horizons": horizons,
    }


def _legacy_rule_profile_semantic_drift(
    recorded: Mapping[str, object],
    replayed: Mapping[str, object],
) -> bool:
    """Recognize only the old degraded-profile shape fixed at calendar bounds."""
    if recorded.keys() != replayed.keys():
        return False
    changed = False
    for horizon, raw_recorded in recorded.items():
        stored = _mapping(raw_recorded, f"horizons.{horizon}")
        current = _mapping(replayed.get(horizon), f"replayed_horizons.{horizon}")
        if stored == current:
            continue
        stable_fields = ("horizon", "target_session_date", "maturity")
        if any(stored.get(name) != current.get(name) for name in stable_fields):
            return False
        stored_outcome = _mapping(stored.get("outcome"), f"horizons.{horizon}.outcome")
        current_outcome = _mapping(current.get("outcome"), f"replayed_horizons.{horizon}.outcome")
        if not _is_legacy_degraded_profile(stored_outcome, current_outcome):
            return False
        changed = True
    return changed


def _is_legacy_degraded_profile(
    stored: Mapping[str, object],
    current: Mapping[str, object],
) -> bool:
    horizon = current.get("horizon")
    if not isinstance(horizon, int) or isinstance(horizon, bool) or horizon <= 0:
        return False
    reason = stored.get("reason")
    expected = {
        "horizon": horizon,
        "status": "data_unavailable",
        "reason": reason,
        "label": None,
        "gross_return": None,
        "net_return": None,
        "cost_drag": None,
        "entry_date": current.get("entry_date"),
        "exit_date": None,
        "entry_price": None,
        "exit_price": None,
        "model_limited": False,
        "rule_profile_verified": False,
        "daily_bar_model_limited": False,
    }
    if reason == "exit_rule_profile_degraded":
        expected.update(
            entry_price=current.get("entry_price"),
            exit_date=current.get("exit_date"),
        )
    return (
        reason in {"entry_rule_profile_degraded", "exit_rule_profile_degraded"}
        and dict(stored) == expected
        and current.get("rule_profile_verified") is True
    )


def _validate_instrument(
    instrument: Mapping[str, object],
    symbol: str,
) -> dict[str, object]:
    normalized = _json_mapping(instrument, f"{symbol}.instrument")
    _exact_keys(normalized, _INSTRUMENT_KEYS, f"{symbol}.instrument")
    output: dict[str, object] = {
        "market": _text(normalized["market"], f"{symbol}.market"),
        "list_date": _optional_date_text(normalized["list_date"], f"{symbol}.list_date"),
        "is_st": _boolean(normalized["is_st"], f"{symbol}.is_st"),
        "quote_amount": _nonnegative_number(normalized["quote_amount"], f"{symbol}.quote_amount"),
        "adjustment_mode": _text(normalized["adjustment_mode"], f"{symbol}.adjustment_mode"),
    }
    if output["market"] != symbol[-2:] or output["adjustment_mode"] != "qfq":
        raise ProbabilityOutcomeError(f"{symbol} instrument market/qfq 冲突")
    return output


def _validate_bar_evidence(
    evidence: Mapping[str, object],
    symbol: str,
    *,
    requested_dates: tuple[str, ...],
    as_of_date: str,
) -> dict[str, object]:
    normalized = _json_mapping(evidence, f"{symbol}.bar_evidence")
    _exact_keys(normalized, _BAR_EVIDENCE_KEYS, f"{symbol}.bar_evidence")
    if normalized["version"] != PROBABILITY_OUTCOME_BAR_EVIDENCE_VERSION:
        raise ProbabilityOutcomeError(f"{symbol} bar evidence version 不受支持")
    requested = tuple(_date_text(value, "bar_evidence.requested_dates[]") for value in _sequence(normalized["requested_dates"], "requested dates"))
    if requested != requested_dates:
        raise ProbabilityOutcomeError(f"{symbol} bar requested_dates 与固定日历/as_of 冲突")
    bars = [
        _normalized_bar(_kline_from_mapping(_mapping(value, "bar_evidence.bars[]")), symbol, as_of_date)
        for value in _sequence(normalized["bars"], "bar_evidence.bars")
    ]
    observed = [str(bar["date"]) for bar in bars]
    if observed != sorted(set(observed)) or any(value not in requested for value in observed):
        raise ProbabilityOutcomeError(f"{symbol} bar evidence 日期无序、重复或越界")
    missing = [value for value in requested if value not in set(observed)]
    if normalized["observed_dates"] != observed or normalized["missing_dates"] != missing:
        raise ProbabilityOutcomeError(f"{symbol} bar evidence observed/missing dates 冲突")
    if normalized["bar_set_digest"] != stable_probability_hash(bars):
        raise ProbabilityOutcomeError(f"{symbol} bar_set_digest 不一致")
    return {
        "version": PROBABILITY_OUTCOME_BAR_EVIDENCE_VERSION,
        "requested_dates": list(requested),
        "observed_dates": observed,
        "missing_dates": missing,
        "bars": bars,
        "bar_set_digest": str(normalized["bar_set_digest"]),
    }


def _kline_from_mapping(value: Mapping[str, object]) -> Kline:
    _exact_keys(value, _BAR_KEYS, "bar")
    return Kline(
        date=str(value["date"]),
        open=float(cast(float, value["open"])),
        close=float(cast(float, value["close"])),
        high=float(cast(float, value["high"])),
        low=float(cast(float, value["low"])),
        volume=float(cast(float, value["volume"])),
        adjustment_mode=cast(Literal["qfq"], value["adjustment_mode"]),
        as_of=cast(str | None, value["as_of"]),
        data_version=str(value["data_version"]),
        contract_version=str(value["contract_version"]),
        source=cast(str | None, value["source"]),
        fallback_used=bool(value["fallback_used"]),
    )


def _outcome_from_mapping(value: Mapping[str, object]) -> ProbabilityLabelOutcome:
    _exact_keys(value, _OUTCOME_KEYS, "outcome")
    return ProbabilityLabelOutcome(
        horizon=int(cast(int, value["horizon"])),
        status=cast(Literal["modelled", "unfilled", "data_unavailable"], value["status"]),
        reason=str(value["reason"]),
        label=cast(int | None, value["label"]),
        gross_return=cast(float | None, value["gross_return"]),
        net_return=cast(float | None, value["net_return"]),
        cost_drag=cast(float | None, value["cost_drag"]),
        entry_date=cast(str | None, value["entry_date"]),
        exit_date=cast(str | None, value["exit_date"]),
        entry_price=cast(float | None, value["entry_price"]),
        exit_price=cast(float | None, value["exit_price"]),
        model_limited=bool(value["model_limited"]),
        rule_profile_verified=bool(value["rule_profile_verified"]),
        daily_bar_model_limited=bool(value["daily_bar_model_limited"]),
    )


def _selected_artifacts(
    values: Sequence[Mapping[str, object] | str | Path],
) -> list[dict[str, object]]:
    loaded = [_source_outcome_artifact(value) for value in values]
    by_run: dict[int, list[dict[str, object]]] = defaultdict(list)
    for artifact in loaded:
        source = _mapping(_mapping(artifact["payload"], "payload")["source"], "payload.source")
        by_run[int(cast(int, source["run_id"]))].append(artifact)
    selected: list[dict[str, object]] = []
    for run_id, candidates in by_run.items():
        newest_as_of = max(str(_mapping(item["payload"], "payload")["as_of_date"]) for item in candidates)
        latest_as_of = [item for item in candidates if _mapping(item["payload"], "payload")["as_of_date"] == newest_as_of]
        newest_at = max(_timestamp_order(item["generated_at"], "generated_at") for item in latest_as_of)
        newest = [item for item in latest_as_of if _timestamp_order(item["generated_at"], "generated_at") == newest_at]
        identities = {str(_mapping(item["integrity"], "integrity")["integrity_digest"]) for item in newest}
        if len(identities) != 1:
            raise ProbabilityOutcomeError(f"run {run_id} 最新 as_of 存在冲突 outcome artifacts")
        selected.append(newest[0])
    return sorted(
        selected,
        key=lambda item: (
            str(_mapping(_mapping(item["payload"], "payload")["source"], "source")["quote_date"]),
            int(cast(int, _mapping(_mapping(item["payload"], "payload")["source"], "source")["run_id"])),
        ),
    )


def _selected_sources(
    values: Sequence[Mapping[str, object] | str | Path],
) -> dict[tuple[int, str], dict[str, object]]:
    output: dict[tuple[int, str], dict[str, object]] = {}
    for value in values:
        artifact = _source_artifact(value)
        payload = _mapping(artifact["payload"], "source.payload")
        run = _mapping(payload["run"], "source.run")
        integrity = _mapping(artifact["integrity"], "source.integrity")
        key = int(cast(int, run["run_id"])), str(integrity["integrity_digest"])
        previous = output.setdefault(key, artifact)
        if previous != artifact:
            raise ProbabilityOutcomeError(f"source run {key[0]} 同 digest 内容冲突")
    return output


def _research_rows_for_artifact(
    artifact: Mapping[str, object],
    sources: Mapping[tuple[int, str], Mapping[str, object]],
) -> tuple[list[ProbabilityResearchRow], Mapping[str, object], int, str]:
    payload = _mapping(artifact["payload"], "payload")
    source = _mapping(payload["source"], "payload.source")
    cohort = _mapping(payload["cohort"], "payload.cohort")
    run_id = int(cast(int, source["run_id"]))
    source_artifact = sources.get((run_id, str(source["integrity_digest"])))
    if source_artifact is None:
        raise ProbabilityOutcomeError(f"outcome run {run_id} 缺少对应 source snapshot")
    archived_payload = _joined_source_payload(source_artifact, payload, source, cohort, run_id)
    score_semantics = archived_payload.get("score_semantics")
    normalized_score_semantics = _mapping(score_semantics, "source.score_semantics") if score_semantics is not None else {}
    source_records = {
        str(_mapping(item, "source.records[]")["symbol"]): _mapping(item, "source.records[]")
        for item in _sequence(archived_payload["records"], "source.records")
    }
    outcome_records = {
        str(_mapping(item, "payload.records[]")["symbol"]): _mapping(item, "payload.records[]") for item in _sequence(payload["records"], "payload.records")
    }
    if set(source_records) != set(outcome_records):
        raise ProbabilityOutcomeError(f"outcome run {run_id} 与 source symbol 集合冲突")
    session_date = str(source["quote_date"])
    rows = [
        _joined_research_row(
            source_records[symbol],
            outcome_records[symbol],
            cohort=cohort,
            score_semantics=normalized_score_semantics,
            run_id=run_id,
            session_date=session_date,
            source_integrity_digest=str(source["integrity_digest"]),
        )
        for symbol in sorted(outcome_records)
    ]
    return rows, cohort, run_id, session_date


def _joined_source_payload(
    source_artifact: Mapping[str, object],
    outcome_payload: Mapping[str, object],
    outcome_source: Mapping[str, object],
    outcome_cohort: Mapping[str, object],
    run_id: int,
) -> Mapping[str, object]:
    payload = _mapping(source_artifact["payload"], "source.payload")
    integrity = _mapping(source_artifact["integrity"], "source.integrity")
    archived_run = _mapping(payload["run"], "source.run")
    archived_cohort = _mapping(payload["cohort"], "source.cohort")
    if integrity["integrity_digest"] != outcome_source["integrity_digest"]:
        raise ProbabilityOutcomeError(f"outcome run {run_id} 与 source payload digest 冲突")
    if archived_run["run_id"] != run_id or archived_cohort != outcome_cohort:
        raise ProbabilityOutcomeError(f"outcome run {run_id} 与 source run/cohort 冲突")
    return payload


def _joined_research_row(
    source_record: Mapping[str, object],
    outcome_record: Mapping[str, object],
    *,
    cohort: Mapping[str, object],
    score_semantics: Mapping[str, object],
    run_id: int,
    session_date: str,
    source_integrity_digest: str,
) -> ProbabilityResearchRow:
    symbol = str(outcome_record["symbol"])
    if (
        symbol != source_record["symbol"]
        or outcome_record["feature_vector_digest"] != source_record["feature_vector_digest"]
        or outcome_record["source_evidence_digest"] != source_record["source_evidence_digest"]
    ):
        raise ProbabilityOutcomeError(f"{symbol} outcome/source 逐股身份或 digest 冲突")
    _validate_joined_instrument(symbol, source_record, outcome_record)
    states = _mapping(outcome_record["horizons"], "record.horizons")
    labels = {
        int(horizon): _outcome_from_mapping(_mapping(_mapping(state, "horizon state")["outcome"], "outcome"))
        for horizon, state in states.items()
        if _mapping(state, "horizon state")["maturity"] == "mature"
    }
    dimensions = {name: str(value) for name, value in _mapping(source_record["dimensions"], "source record dimensions").items()}
    return ProbabilityResearchRow(
        run_id=run_id,
        symbol=symbol,
        session_date=session_date,
        features={
            name: _finite_number(value, f"source record features.{name}")
            for name, value in _mapping(source_record["features"], "source record features").items()
        },
        labels=labels,
        mature_horizons=frozenset(labels),
        dimensions=dimensions,
        source_evidence_digest=str(outcome_record["source_evidence_digest"]),
        source_integrity_digest=source_integrity_digest,
        mode=str(cohort["mode"]),
        scope=str(cohort["scope"]),
        rule_version=str(cohort["rule_version"]),
        production_score_rule_version=(
            str(score_semantics["production_rule_version"]) if score_semantics.get("production_score_spec_hash") is not None else None
        ),
        production_score_spec_hash=(
            str(score_semantics["production_score_spec_hash"]) if score_semantics.get("production_score_spec_hash") is not None else None
        ),
    )


def _validate_joined_instrument(
    symbol: str,
    source_record: Mapping[str, object],
    outcome_record: Mapping[str, object],
) -> None:
    source = _mapping(source_record["instrument"], "source record instrument")
    expected = {name: source[name] for name in ("market", "list_date", "is_st", "quote_amount", "adjustment_mode")}
    if _mapping(outcome_record["instrument"], "outcome record instrument") != expected:
        raise ProbabilityOutcomeError(f"{symbol} outcome/source instrument 冲突")


def _require_compatible_corpus(artifacts: Sequence[Mapping[str, object]]) -> None:
    if not artifacts:
        return
    identities = {
        (
            str(_mapping(item["payload"], "payload")["label_contract_digest"]),
            str(_mapping(_mapping(item["payload"], "payload")["source"], "source")["payload_contract_version"]),
        )
        for item in artifacts
    }
    if len(identities) != 1:
        raise ProbabilityOutcomeError("outcome corpus 混合了不兼容的 label/feature contracts")


def _modelled_outcome(outcome: ProbabilityLabelOutcome | None) -> bool:
    return bool(outcome is not None and outcome.status == "modelled" and outcome.rule_profile_verified and outcome.net_return is not None)


def _source_artifact(value: Mapping[str, object] | str | Path) -> dict[str, object]:
    return load_probability_source_snapshot(value) if isinstance(value, str | Path) else verify_probability_source_snapshot(value)


def _source_outcome_artifact(value: Mapping[str, object] | str | Path) -> dict[str, object]:
    return load_probability_outcome_artifact(value) if isinstance(value, str | Path) else verify_probability_outcome_artifact(value)


def _write_artifact(path: Path, artifact: Mapping[str, object]) -> None:
    encoded = _compressed_artifact_bytes(artifact)
    try:
        exclusive_atomic_publish(path, encoded, max_bytes=PROBABILITY_OUTCOME_MAX_COMPRESSED_BYTES)
    except ArtifactContentConflictError as exc:
        raise ProbabilityOutcomeError("outcome artifact 已存在且内容不同，拒绝覆盖") from exc
    except ArtifactPublishConflictError as exc:
        raise ProbabilityOutcomeError("outcome artifact 并发发布冲突") from exc
    except ArtifactNotDirectoryError as exc:
        raise ProbabilityOutcomeError(f"outcome artifact 输出目录必须是真实目录：{path.parent}") from exc
    except ArtifactNotRegularError as exc:
        raise ProbabilityOutcomeError(f"outcome artifact target 不是普通文件：{path}") from exc
    except ArtifactTooLargeError as exc:
        raise ProbabilityOutcomeError(f"outcome artifact 超过压缩大小上限：{path}") from exc
    except ArtifactIOError as exc:
        raise ProbabilityOutcomeError(f"outcome artifact 写入失败：{path}") from exc


def _artifact_info(path: Path, artifact: Mapping[str, object], *, run_id: int) -> dict[str, object]:
    payload = _mapping(artifact["payload"], "payload")
    integrity = _mapping(artifact["integrity"], "integrity")
    return {
        "path": str(path.absolute()),
        "run_id": run_id,
        "quote_date": _mapping(payload["source"], "source")["quote_date"],
        "as_of_date": payload["as_of_date"],
        "generated_at": artifact["generated_at"],
        "digest": integrity["integrity_digest"],
        "source_digest": _mapping(payload["source"], "source")["integrity_digest"],
        "label_contract_digest": payload["label_contract_digest"],
        "quality": deepcopy(payload["quality"]),
        "storage": {
            "compressed_bytes": path.stat().st_size,
            "uncompressed_bytes": len(_canonical_json(artifact).encode("utf-8")),
        },
    }


def _compressed_artifact_bytes(artifact: Mapping[str, object]) -> bytes:
    return _compressed_canonical_artifact_bytes(verify_probability_outcome_artifact(artifact))


def _compressed_canonical_artifact_bytes(artifact: Mapping[str, object]) -> bytes:
    encoded = _canonical_json(artifact).encode("utf-8")
    if len(encoded) > PROBABILITY_OUTCOME_MAX_UNCOMPRESSED_BYTES:
        raise ProbabilityOutcomeError("outcome artifact 未压缩内容超过安全上限")
    compressed = gzip.compress(encoded, compresslevel=9, mtime=0)
    if len(compressed) > PROBABILITY_OUTCOME_MAX_COMPRESSED_BYTES:
        raise ProbabilityOutcomeError("outcome artifact 压缩内容超过安全上限")
    return compressed


def _read_artifact_bytes(path: Path) -> bytes:
    try:
        return read_regular_file(path, max_bytes=PROBABILITY_OUTCOME_MAX_COMPRESSED_BYTES)
    except ArtifactNotRegularError as exc:
        raise ProbabilityOutcomeError(f"outcome artifact 必须是普通文件：{path}") from exc
    except ArtifactTooLargeError as exc:
        raise ProbabilityOutcomeError(f"outcome artifact 超过压缩大小上限：{path}") from exc
    except ArtifactChangedError as exc:
        raise ProbabilityOutcomeError(f"outcome artifact 在读取期间发生变化：{path}") from exc
    except ArtifactIOError as exc:
        raise ProbabilityOutcomeError(f"outcome artifact 无法读取：{path}") from exc


def _decompress(encoded: bytes, path: Path) -> bytes:
    try:
        chunks: list[bytes] = []
        remaining = PROBABILITY_OUTCOME_MAX_UNCOMPRESSED_BYTES + 1
        with gzip.GzipFile(fileobj=BytesIO(encoded), mode="rb") as stream:
            while remaining:
                chunk = stream.read(min(1024 * 1024, remaining))
                if not chunk:
                    break
                chunks.append(chunk)
                remaining -= len(chunk)
        decoded = b"".join(chunks)
    except (gzip.BadGzipFile, EOFError, OSError) as exc:
        raise ProbabilityOutcomeError(f"outcome artifact gzip 损坏：{path}") from exc
    if len(decoded) > PROBABILITY_OUTCOME_MAX_UNCOMPRESSED_BYTES:
        raise ProbabilityOutcomeError(f"outcome artifact 解压内容超过安全上限：{path}")
    return decoded


def _decode_artifact(encoded: bytes, path: Path) -> Mapping[str, object]:
    try:
        decoded = decode_json_bytes(encoded)
    except ArtifactDuplicateKeyError as exc:
        raise ProbabilityOutcomeError(f"outcome artifact 含重复 JSON key：{exc.key}") from exc
    except ArtifactNonFiniteConstantError as exc:
        raise ProbabilityOutcomeError(f"outcome artifact 含非有限 JSON 常量：{exc.constant}") from exc
    except ArtifactIOError as exc:
        raise ProbabilityOutcomeError(f"outcome artifact JSON 损坏：{path}") from exc
    if not isinstance(decoded, Mapping):
        raise ProbabilityOutcomeError("outcome artifact 顶层必须是 object")
    return cast(Mapping[str, object], decoded)


def _validate_filename(path: Path, artifact: Mapping[str, object]) -> None:
    run_id, as_of_date, digest = _filename_identity(path)
    payload = _mapping(artifact.get("payload"), "payload")
    source = _mapping(payload.get("source"), "payload.source")
    integrity = _mapping(artifact.get("integrity"), "integrity")
    encoded_run = _positive_integer(source.get("run_id"), "payload.source.run_id")
    encoded_as_of = _date_text(payload.get("as_of_date"), "payload.as_of_date")
    encoded_digest = _sha256(integrity.get("integrity_digest"), "integrity.integrity_digest")
    if run_id != encoded_run or as_of_date != encoded_as_of or digest != encoded_digest:
        raise ProbabilityOutcomeError("outcome artifact 文件名与内容地址冲突")


def _filename_identity(path: Path) -> tuple[int, str, str]:
    match = _OUTCOME_FILENAME.fullmatch(path.name)
    if match is None:
        raise ProbabilityOutcomeError(f"outcome artifact 文件名不规范：{path.name}")
    return int(match.group(1)), match.group(2), match.group(3)


def _limitations() -> list[str]:
    return [
        "shadow_research_only_no_production_ranking_effect",
        "daily_bar_cannot_reconstruct_intraday_queue_or_execution_order",
        "signal_date_instrument_status_held_constant_through_horizon",
        "missing_fixed_session_bar_is_unavailable_and_never_shifted",
        "integrity_digest_is_not_an_authenticity_signature",
    ]


def _generated_at(value: str | None, now: datetime | None) -> str:
    if value is not None:
        return _timestamp(value, "generated_at")
    current = now or market_now()
    if current.tzinfo is None or current.utcoffset() is None:
        current = current.replace(tzinfo=ASHARE_TIMEZONE)
    return current.astimezone(ASHARE_TIMEZONE).isoformat()


def _fixed_future_sessions(signal_date: date, horizons: Sequence[int]) -> tuple[date, ...]:
    try:
        return next_trade_dates(signal_date, max(horizons) + 1)
    except TradingCalendarCoverageError as exc:
        raise ProbabilityOutcomeError("可信交易日历不足以固定 outcome 目标会话") from exc


def _canonical_json(value: object) -> str:
    try:
        return canonical_json_text(value)
    except ArtifactCanonicalJsonError as exc:
        raise ProbabilityOutcomeError("outcome artifact 必须是有限规范 JSON") from exc


def _json_mapping(value: Mapping[str, object], path: str) -> dict[str, object]:
    try:
        encoded = canonical_json_text(value)
        decoded = decode_json_bytes(encoded.encode("utf-8"))
    except ArtifactIOError as exc:
        raise ProbabilityOutcomeError(f"{path} 不是有限 JSON object") from exc
    if not isinstance(decoded, dict):
        raise ProbabilityOutcomeError(f"{path} 必须是 object")
    return cast(dict[str, object], decoded)


def _mapping(value: object, path: str) -> Mapping[str, object]:
    if not isinstance(value, Mapping) or any(not isinstance(key, str) for key in value):
        raise ProbabilityOutcomeError(f"{path} 必须是 object")
    return cast(Mapping[str, object], value)


def _sequence(value: object, path: str) -> list[object]:
    if not isinstance(value, list | tuple):
        raise ProbabilityOutcomeError(f"{path} 必须是数组")
    return list(value)


def _exact_keys(value: Mapping[str, object], expected: frozenset[str], path: str) -> None:
    if set(value) != expected:
        raise ProbabilityOutcomeError(f"{path} keys 与契约冲突")


def _text(value: object, path: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise ProbabilityOutcomeError(f"{path} 必须是非空字符串")
    return value


def _optional_text(value: object) -> str | None:
    if value is None:
        return None
    return _text(value, "optional text")


def _symbol(value: object) -> str:
    normalized = _text(value, "symbol")
    if _SYMBOL.fullmatch(normalized) is None:
        raise ProbabilityOutcomeError("outcome symbol 无效")
    return normalized


def _sha256(value: object, path: str) -> str:
    normalized = _text(value, path)
    if _SHA256.fullmatch(normalized) is None:
        raise ProbabilityOutcomeError(f"{path} 必须是 sha256")
    return normalized


def _timestamp(value: object, path: str) -> str:
    normalized = _text(value, path)
    try:
        parsed = datetime.fromisoformat(normalized.replace("Z", "+00:00"))
    except ValueError as exc:
        raise ProbabilityOutcomeError(f"{path} 不是 ISO timestamp") from exc
    if parsed.tzinfo is None or parsed.utcoffset() is None:
        raise ProbabilityOutcomeError(f"{path} 必须包含时区")
    return normalized


def _optional_timestamp(value: object, path: str) -> str | None:
    return None if value is None else _timestamp(value, path)


def _timestamp_order(value: object, path: str) -> float:
    return datetime.fromisoformat(_timestamp(value, path).replace("Z", "+00:00")).timestamp()


def _date_text(value: object, path: str) -> str:
    normalized = _text(value, path)
    try:
        parsed = date.fromisoformat(normalized)
    except ValueError as exc:
        raise ProbabilityOutcomeError(f"{path} 不是 YYYY-MM-DD") from exc
    if parsed.isoformat() != normalized:
        raise ProbabilityOutcomeError(f"{path} 不是规范 YYYY-MM-DD")
    return normalized


def _optional_date_text(value: object, path: str) -> str | None:
    return None if value is None else _date_text(value, path)


def _positive_integer(value: object, path: str) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or value <= 0:
        raise ProbabilityOutcomeError(f"{path} 必须是正整数")
    return value


def _finite_number(value: object, path: str) -> float:
    if isinstance(value, bool) or not isinstance(value, int | float) or not math.isfinite(float(value)):
        raise ProbabilityOutcomeError(f"{path} 必须是有限数")
    return float(value)


def _positive_number(value: object, path: str) -> float:
    number = _finite_number(value, path)
    if number <= 0:
        raise ProbabilityOutcomeError(f"{path} 必须大于0")
    return number


def _nonnegative_number(value: object, path: str) -> float:
    number = _finite_number(value, path)
    if number < 0:
        raise ProbabilityOutcomeError(f"{path} 不能小于0")
    return number


def _bounded_rate(value: object, path: str) -> float:
    number = _finite_number(value, path)
    if not 0 < number <= 1:
        raise ProbabilityOutcomeError(f"{path} 必须位于 (0,1]")
    return number


def _boolean(value: object, path: str) -> bool:
    if not isinstance(value, bool):
        raise ProbabilityOutcomeError(f"{path} 必须是 boolean")
    return value


def _finite_number_mapping(value: object, path: str) -> dict[str, float]:
    mapping = _mapping(value, path)
    return {name: _finite_number(item, f"{path}.{name}") for name, item in sorted(mapping.items())}


__all__ = [
    "PROBABILITY_OUTCOME_ARTIFACT_SCHEMA_VERSION",
    "PROBABILITY_OUTCOME_MINIMUM_LABEL_COVERAGE",
    "PROBABILITY_OUTCOME_PAYLOAD_CONTRACT_VERSION",
    "ProbabilityKlineLoader",
    "ProbabilityOutcomeError",
    "ProbabilityOutcomeTarget",
    "build_probability_outcome_artifact",
    "list_probability_outcome_artifacts",
    "load_probability_outcome_artifact",
    "load_probability_outcome_artifact_for_run",
    "mature_probability_source_snapshot",
    "probability_outcome_artifact_filename",
    "probability_outcome_corpus_progress",
    "probability_outcome_payload_digest",
    "probability_outcome_required_dates",
    "probability_research_rows_from_outcome_artifacts",
    "probability_samples_from_outcome_artifacts",
    "publish_built_probability_outcome_artifact",
    "publish_probability_outcome_artifact",
    "verify_probability_outcome_artifact",
]
