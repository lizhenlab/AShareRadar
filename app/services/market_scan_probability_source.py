"""Compact immutable point-in-time sources for probability research.

The runtime database deliberately retains far fewer full-market runs than a
walk-forward probability study needs.  This module provides a separate source
archive boundary: a caller attests that one run is the canonical published
official full-market snapshot and supplies its already verified point-in-time
evidence.  Only registered probability features, their evidence digest, and
the small instrument/execution metadata needed for later labels are retained.

Archives are canonical JSON compressed with deterministic gzip, published by
exclusive hard link, and addressed by the SHA-256 of their payload.  The digest
detects alteration; it is not a signature or an authenticity attestation.
This module has no SQLite, provider, scheduler, or production-ranking write
path, so runtime retention cleanup cannot delete an archived source snapshot.
"""

from __future__ import annotations

from collections import Counter
from collections.abc import Callable, Iterator, Mapping, Sequence
from copy import deepcopy
from datetime import date, datetime
import gzip
import json
from io import BytesIO
import math
from pathlib import Path
import re
import stat
from statistics import fmean
from typing import cast

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

from app.models.market_scan import MarketScanMode, MarketScanResultItem
from app.models.paper_trading import PaperInstrumentMetadata
from app.utils.clock import ASHARE_TIMEZONE
from app.services.market_scan_probability import (
    LEGACY_PROBABILITY_FEATURE_VERSION,
    PROBABILITY_FEATURE_VERSION,
    stable_probability_hash,
)
from app.services.market_scan_probability_research import probability_feature_vector
from app.services.market_scan_publication_decision import (
    MARKET_SCAN_PUBLISH_MIN_COVERAGE,
    MARKET_SCAN_PUBLISH_MIN_ELIGIBLE_RATIO,
)
from app.services.market_scan_score_dimensions import (
    MARKET_SCAN_EVIDENCE_CONTRACT_VERSION,
    MARKET_SCAN_EVIDENCE_LEGACY_CONTRACT_VERSION,
    MARKET_SCAN_EVIDENCE_LEGACY_V2_CONTRACT_VERSION,
    verify_market_scan_point_in_time_evidence,
    verify_market_scan_point_in_time_evidence_context,
)
from app.services.market_scan_scoring import (
    FULL_MARKET_LEGACY_V4_SCORE_RULE_VERSION,
    FULL_MARKET_SCORE_RULE_VERSION,
    MarketScanReplayError,
    market_scan_score_spec,
    market_scan_score_spec_v4,
    stable_score_spec_hash,
    verify_score_details,
)
from app.services.market_scan_universe import FULL_MARKET_SCOPE
from app.services.market_scan_modes import current_official_source_temporal_contract_matches
from app.services.paper_trading_rules import resolve_trade_rule_profile

exclusive_atomic_publish = publish_market_scan_artifact


LEGACY_PROBABILITY_SOURCE_ARTIFACT_SCHEMA_VERSION = "market-scan-probability-source-artifact-v1"
LEGACY_PROBABILITY_SOURCE_PAYLOAD_CONTRACT_VERSION = "market-scan-probability-source-snapshot-v1"
PREVIOUS_PROBABILITY_SOURCE_ARTIFACT_SCHEMA_VERSION = "market-scan-probability-source-artifact-v2"
PREVIOUS_PROBABILITY_SOURCE_PAYLOAD_CONTRACT_VERSION = "market-scan-probability-source-snapshot-v2"
PROBABILITY_SOURCE_ARTIFACT_SCHEMA_VERSION = "market-scan-probability-source-artifact-v3"
PROBABILITY_SOURCE_PAYLOAD_CONTRACT_VERSION = "market-scan-probability-source-snapshot-v3"
PROBABILITY_SOURCE_COMPRESSION = "gzip-canonical-json-v1"
PROBABILITY_SOURCE_DIGEST_ALGORITHM = "sha256"
PROBABILITY_SOURCE_DIGEST_SCOPE = "payload"
PROBABILITY_SOURCE_INTEGRITY_NOTICE = "integrity_digest_not_a_signature"
PROBABILITY_SOURCE_MAX_COMPRESSED_BYTES = 128 * 1024 * 1024
PROBABILITY_SOURCE_MAX_UNCOMPRESSED_BYTES = 512 * 1024 * 1024
PROBABILITY_SOURCE_MINIMUM_POPULATION = {
    "ALL": 4000,
    "SH": 1800,
    "SZ": 2500,
    "BJ": 200,
}
PROBABILITY_SOURCE_MINIMUM_COVERAGE = dict(MARKET_SCAN_PUBLISH_MIN_COVERAGE)
PROBABILITY_SOURCE_MINIMUM_ELIGIBLE_RATIO = dict(MARKET_SCAN_PUBLISH_MIN_ELIGIBLE_RATIO)
LEGACY_PROBABILITY_SOURCE_COVERAGE_CONTRACT_VERSION = "market-scan-probability-source-full-market-coverage-v1"
PROBABILITY_SOURCE_COVERAGE_CONTRACT_VERSION = "market-scan-probability-source-full-market-coverage-v2"
_FULL_MARKET_SCOPES = ("ALL", "SH", "SZ", "BJ")

_TOP_LEVEL_KEYS = frozenset({"schema_version", "captured_at", "payload", "integrity"})
_INTEGRITY_KEYS = frozenset({"algorithm", "scope", "integrity_digest", "notice", "compression"})
_PAYLOAD_KEYS = frozenset({"contract_version", "captured_at", "run", "cohort", "score_semantics", "feature_schema", "records", "quality"})
_LEGACY_RUN_KEYS = frozenset(
    {
        "run_id",
        "status",
        "mode",
        "scope",
        "rule_version",
        "quote_date",
        "data_date",
        "as_of",
        "total_count",
        "success_count",
        "canonical_published",
    }
)
_PREVIOUS_RUN_KEYS = _LEGACY_RUN_KEYS | frozenset(
    {
        "production_score_rule_version",
        "production_score_spec_hash",
        "full_market_coverage",
    }
)
_RUN_KEYS = _PREVIOUS_RUN_KEYS | {"skipped_count"}
_COHORT_KEYS = frozenset({"mode", "scope", "rule_version"})
_LEGACY_SCORE_SEMANTICS_KEYS = frozenset({"production_rule_version", "production_score", "source_rank", "probability_ranking_effect"})
_SCORE_SEMANTICS_KEYS = _LEGACY_SCORE_SEMANTICS_KEYS | frozenset({"production_score_spec_hash"})
_FEATURE_SCHEMA_KEYS = frozenset({"version", "names", "digest"})
_CAPTURE_RECORD_KEYS = frozenset({"symbol", "features", "dimensions", "source_evidence"})
_RECORD_KEYS = frozenset(
    {
        "symbol",
        "features",
        "feature_vector_digest",
        "dimensions",
        "source_evidence_contract_version",
        "source_evidence_digest",
        "instrument",
    }
)
_DIMENSION_KEYS = frozenset({"mode", "scope", "rule_version", "market", "board", "industry", "liquidity", "regime", "segment"})
_INSTRUMENT_KEYS = frozenset(
    {
        "market",
        "board",
        "industry",
        "source_industry",
        "liquidity",
        "regime",
        "segment",
        "list_date",
        "is_st",
        "is_new",
        "metadata_source",
        "metadata_effective_date",
        "metadata_degraded",
        "quote_timestamp",
        "quote_price",
        "quote_change_pct",
        "quote_turnover_rate",
        "quote_amount",
        "reported_volume_ratio",
        "data_quality_score",
        "adjustment_mode",
        "quote_fallback_used",
        "kline_fallback_used",
    }
)
_LEGACY_QUALITY_KEYS = frozenset(
    {
        "expected_record_count",
        "record_count",
        "record_coverage",
        "feature_count",
        "feature_value_count",
        "verified_source_digest_count",
        "source_digest_coverage",
        "metadata_degraded_count",
        "missing_list_date_count",
        "missing_metadata_source_count",
        "quote_fallback_count",
        "kline_fallback_count",
        "market_counts",
        "board_counts",
    }
)
_PREVIOUS_QUALITY_KEYS = _LEGACY_QUALITY_KEYS | frozenset(
    {
        "run_total_count",
        "run_success_count",
        "success_to_total_coverage",
        "strata_coverage",
        "full_market_coverage",
    }
)
_QUALITY_KEYS = _PREVIOUS_QUALITY_KEYS | {"run_skipped_count", "run_eligible_count"}
_STRATA_COVERAGE_KEYS = frozenset({"observed_count", "missing_count", "coverage", "counts"})
_FULL_MARKET_COVERAGE_KEYS = frozenset({"contract_version", "scopes"})
_PREVIOUS_FULL_MARKET_SCOPE_KEYS = frozenset(
    {
        "population_count",
        "eligible_count",
        "success_count",
        "eligible_ratio",
        "success_coverage",
        "minimum_population",
        "minimum_eligible_ratio",
        "minimum_success_coverage",
    }
)
_FULL_MARKET_SCOPE_KEYS = _PREVIOUS_FULL_MARKET_SCOPE_KEYS | {"missing_count", "skipped_count"}
_SUPPORTED_SOURCE_CONTRACTS = {
    LEGACY_PROBABILITY_SOURCE_ARTIFACT_SCHEMA_VERSION: LEGACY_PROBABILITY_SOURCE_PAYLOAD_CONTRACT_VERSION,
    PREVIOUS_PROBABILITY_SOURCE_ARTIFACT_SCHEMA_VERSION: PREVIOUS_PROBABILITY_SOURCE_PAYLOAD_CONTRACT_VERSION,
    PROBABILITY_SOURCE_ARTIFACT_SCHEMA_VERSION: PROBABILITY_SOURCE_PAYLOAD_CONTRACT_VERSION,
}
_SOURCE_FILENAME = re.compile(r"market-scan-probability-source-run-(\d+)-([0-9a-f]{64})\.json\.gz")
_SYMBOL = re.compile(r"\d{6}\.(SH|SZ|BJ)")
_SHA256 = re.compile(r"[0-9a-f]{64}")
_REGISTERED_FEATURE_NAMES = tuple(
    sorted(
        probability_feature_vector(
            {},
            market="SH",
            board="SH_MAIN",
            liquidity="medium",
            regime="neutral",
            industry="UNKNOWN",
        )
    )
)
_CATEGORICAL_FEATURES = frozenset(
    name
    for name in _REGISTERED_FEATURE_NAMES
    if name.startswith(("market_", "board_", "liquidity_", "regime_", "industry_bucket_")) and name not in {"market_strength", "board_relative_strength"}
)
_CATEGORICAL_FEATURES = _CATEGORICAL_FEATURES | {"is_st", "is_new"}
_CURRENT_WRITABLE_PRODUCTION_SCORE_SPEC_HASHES = frozenset(
    stable_score_spec_hash(market_scan_score_spec(min_data_quality_score=minimum))
    for minimum in range(101)
)
_LEGACY_READABLE_V4_PRODUCTION_SCORE_SPEC_HASHES = frozenset(
    stable_score_spec_hash(market_scan_score_spec_v4(min_data_quality_score=minimum))
    for minimum in range(101)
)
_READABLE_PRODUCTION_SCORE_SPEC_HASHES = {
    FULL_MARKET_LEGACY_V4_SCORE_RULE_VERSION: _LEGACY_READABLE_V4_PRODUCTION_SCORE_SPEC_HASHES,
    FULL_MARKET_SCORE_RULE_VERSION: _CURRENT_WRITABLE_PRODUCTION_SCORE_SPEC_HASHES,
}


class ProbabilitySourceError(ValueError):
    """Raised when a compact probability source archive is unsafe or invalid."""


def is_registered_production_score_contract(
    rule_version: object,
    spec_hash: object,
) -> bool:
    if not isinstance(rule_version, str):
        return False
    registered = _READABLE_PRODUCTION_SCORE_SPEC_HASHES.get(rule_version)
    return isinstance(spec_hash, str) and registered is not None and spec_hash in registered


def is_current_writable_production_score_contract(
    rule_version: object,
    spec_hash: object,
) -> bool:
    return (
        rule_version == FULL_MARKET_SCORE_RULE_VERSION
        and isinstance(spec_hash, str)
        and spec_hash in _CURRENT_WRITABLE_PRODUCTION_SCORE_SPEC_HASHES
    )


def validate_current_full_market_coverage(
    value: object,
    *,
    total_count: object,
    success_count: object,
    skipped_count: object | None = None,
) -> dict[str, object]:
    expected_skipped = (
        _nonnegative_integer(skipped_count, "run.skipped_count")
        if skipped_count is not None
        else None
    )
    return _validate_full_market_coverage(
        value,
        total_count=total_count,
        success_count=success_count,
        skipped_count=expected_skipped,
        legacy=False,
    )


def validate_previous_full_market_coverage(
    value: object,
    *,
    total_count: object,
    success_count: object,
) -> dict[str, object]:
    return _validate_full_market_coverage(
        value,
        total_count=total_count,
        success_count=success_count,
        skipped_count=None,
        legacy=True,
    )


def _validate_full_market_coverage(
    value: object,
    *,
    total_count: object,
    success_count: object,
    skipped_count: int | None,
    legacy: bool,
) -> dict[str, object]:
    coverage = _mapping(value, "full_market_coverage")
    _exact_keys(coverage, _FULL_MARKET_COVERAGE_KEYS, "full_market_coverage")
    contract_version = (
        LEGACY_PROBABILITY_SOURCE_COVERAGE_CONTRACT_VERSION
        if legacy
        else PROBABILITY_SOURCE_COVERAGE_CONTRACT_VERSION
    )
    if coverage.get("contract_version") != contract_version:
        raise ProbabilitySourceError("current source 全市场 coverage contract 不受支持")
    raw_scopes = _mapping(coverage.get("scopes"), "full_market_coverage.scopes")
    if set(raw_scopes) != set(_FULL_MARKET_SCOPES):
        raise ProbabilitySourceError("current source coverage 必须包含 ALL/SH/SZ/BJ")
    scopes = {
        scope: _validated_full_market_scope(
            _mapping(raw_scopes[scope], f"full_market_coverage.{scope}"),
            scope,
            legacy=legacy,
        )
        for scope in _FULL_MARKET_SCOPES
    }
    if scopes["ALL"] != _aggregate_full_market_scope(scopes, legacy=legacy):
        raise ProbabilitySourceError("current source ALL coverage 无法由三市场重放")
    expected_total = _nonnegative_integer(total_count, "run.total_count")
    expected_success = _nonnegative_integer(success_count, "run.success_count")
    if (
        scopes["ALL"]["population_count"] != expected_total
        or scopes["ALL"]["success_count"] != expected_success
    ):
        raise ProbabilitySourceError("current source coverage 与 run counts 冲突")
    if not legacy:
        bound_skipped = cast(int, scopes["ALL"]["skipped_count"])
        if skipped_count is not None and bound_skipped != skipped_count:
            raise ProbabilitySourceError("current source coverage 与 run.skipped_count 冲突")
        if scopes["ALL"]["eligible_count"] != expected_total - bound_skipped:
            raise ProbabilitySourceError("current source eligible/skipped 无法由 run.total_count 重放")
    return {
        "contract_version": contract_version,
        "scopes": scopes,
    }


def _validated_full_market_scope(
    value: Mapping[str, object],
    scope: str,
    *,
    legacy: bool,
) -> dict[str, object]:
    keys = _PREVIOUS_FULL_MARKET_SCOPE_KEYS if legacy else _FULL_MARKET_SCOPE_KEYS
    _exact_keys(value, keys, f"full_market_coverage.{scope}")
    population = _nonnegative_integer(value.get("population_count"), f"{scope}.population")
    eligible = _nonnegative_integer(value.get("eligible_count"), f"{scope}.eligible")
    success = _nonnegative_integer(value.get("success_count"), f"{scope}.success")
    if legacy:
        expected = _previous_full_market_scope_values(scope, population, eligible, success)
    else:
        missing = _nonnegative_integer(value.get("missing_count"), f"{scope}.missing")
        skipped = _nonnegative_integer(value.get("skipped_count"), f"{scope}.skipped")
        if success + missing != eligible or eligible + skipped != population:
            raise ProbabilitySourceError(f"current source {scope} eligible/missing/skipped 无法重放")
        expected = _full_market_scope_values(
            scope,
            population,
            success,
            missing,
            skipped,
        )
    if dict(value) != expected:
        raise ProbabilitySourceError(f"current source {scope} coverage 无法重放")
    if population < PROBABILITY_SOURCE_MINIMUM_POPULATION[scope]:
        raise ProbabilitySourceError(f"current source {scope} population 低于正式下限")
    if (
        cast(float, expected["eligible_ratio"]) < PROBABILITY_SOURCE_MINIMUM_ELIGIBLE_RATIO[scope]
        or cast(float, expected["success_coverage"]) < PROBABILITY_SOURCE_MINIMUM_COVERAGE[scope]
    ):
        raise ProbabilitySourceError(f"current source {scope} coverage 低于正式门槛")
    return expected


_VERIFIED_SOURCE_PROJECTION_SEAL = object()


class VerifiedProbabilitySourceProjection(Mapping[str, object]):
    """Opaque projection whose every current PIT record passed context replay."""

    __slots__ = ("_encoded",)

    def __init__(self, *, encoded: str, _seal: object | None = None) -> None:
        if _seal is not _VERIFIED_SOURCE_PROJECTION_SEAL:
            raise TypeError("source projection 只能由 context verifier 创建")
        self._encoded = encoded

    @property
    def payload(self) -> Mapping[str, object]:
        value = json.loads(self._encoded)
        if not isinstance(value, dict):  # pragma: no cover - constructor invariant
            raise TypeError("source projection payload 必须是 object")
        return cast(Mapping[str, object], value)

    def __getitem__(self, key: str) -> object:
        return self.payload[key]

    def __iter__(self) -> Iterator[str]:
        return iter(self.payload)

    def __len__(self) -> int:
        return len(self.payload)


def project_probability_source_capture(
    run: object,
    results: Sequence[object],
    *,
    canonical_published: bool,
) -> VerifiedProbabilitySourceProjection:
    """Project ``MarketScanRun``/``MarketScanResultItem``-like values for capture.

    Pydantic objects and plain mappings are accepted.  The helper has no data
    access: the caller must pass the complete result sequence for the selected
    canonical run and explicitly attest the publication boundary.
    """
    if canonical_published is not True:
        raise ProbabilitySourceError("source capture projection requires explicit canonical_published=True")
    raw_run = _object_mapping(run, "run")
    projected_run = _project_source_run(raw_run)
    items = [_object_mapping(item, "results[]") for item in results if str(_object_mapping(item, "results[]").get("status") or "") == "success"]
    expected = _run_id(projected_run.get("success_count"), "run.success_count")
    if len(items) != expected:
        raise ProbabilitySourceError(f"source capture projection requires every success row：{len(items)}/{expected}")
    score_contract = _production_score_contract(items)
    projected_run.update(score_contract)
    contexts = [_projection_context(item, projected_run) for item in items]
    market_strength = fmean(_number(item["raw_score"], "projection.raw_score") for item in contexts)
    board_strength = _relative_projection_strength(contexts, "board", market_strength)
    industry_strength = _relative_projection_strength(contexts, "industry", market_strength)
    regime = _projection_regime(items)
    records = [
        _project_capture_record(
            item,
            context,
            projected_run,
            market_strength=market_strength,
            board_relative_strength=board_strength.get(cast(str, context["board"]), 0.0),
            industry_relative_strength=industry_strength.get(cast(str, context["industry"]), 0.0),
            regime=regime,
        )
        for item, context in zip(items, contexts, strict=True)
    ]
    return _verified_source_projection(projected_run, records)


def _project_source_run(raw_run: Mapping[str, object]) -> dict[str, object]:
    projected: dict[str, object] = {
        "run_id": _run_id(raw_run.get("id", raw_run.get("run_id")), "run.id"),
        "status": raw_run.get("status"),
        "mode": raw_run.get("mode"),
        "scope": raw_run.get("scope"),
        "rule_version": raw_run.get("rule_version"),
        "quote_date": raw_run.get("quote_date") or raw_run.get("data_date"),
        "data_date": raw_run.get("data_date"),
        "as_of": _projection_timestamp(raw_run.get("as_of"), "run.as_of"),
        "total_count": raw_run.get("total_count"),
        "success_count": raw_run.get("success_count"),
        "skipped_count": raw_run.get("skipped_count"),
        "canonical_published": True,
    }
    raw_coverage = raw_run.get("full_market_coverage")
    coverage_builder = validate_current_full_market_coverage if raw_coverage is not None else _project_full_market_coverage
    projected["full_market_coverage"] = coverage_builder(
        raw_coverage if raw_coverage is not None else raw_run.get("market_progress"),
        total_count=projected.get("total_count"),
        success_count=projected.get("success_count"),
        skipped_count=projected.get("skipped_count"),
    )
    return projected


def _project_full_market_coverage(
    value: object,
    *,
    total_count: object,
    success_count: object,
    skipped_count: object | None = None,
) -> dict[str, object]:
    if not isinstance(value, Sequence) or isinstance(value, str | bytes):
        raise ProbabilitySourceError("current source 缺少逐市场 publication coverage")
    rows = [_object_mapping(item, "run.market_progress[]") for item in value]
    by_market = {str(item.get("market")): item for item in rows}
    if len(rows) != 3 or set(by_market) != {"SH", "SZ", "BJ"}:
        raise ProbabilitySourceError("current source 必须包含 SH/SZ/BJ 三市场 coverage")
    scopes = {market: _project_market_scope_coverage(by_market[market], market) for market in ("SH", "SZ", "BJ")}
    scopes["ALL"] = _aggregate_full_market_scope(scopes, legacy=False)
    coverage = {
        "contract_version": PROBABILITY_SOURCE_COVERAGE_CONTRACT_VERSION,
        "scopes": {scope: scopes[scope] for scope in _FULL_MARKET_SCOPES},
    }
    return validate_current_full_market_coverage(
        coverage,
        total_count=total_count,
        success_count=success_count,
        skipped_count=skipped_count,
    )


def _project_market_scope_coverage(
    row: Mapping[str, object],
    market: str,
) -> dict[str, object]:
    population = _nonnegative_integer(row.get("total_count"), f"{market}.total_count")
    processed = _nonnegative_integer(row.get("processed_count"), f"{market}.processed_count")
    success = _nonnegative_integer(row.get("success_count"), f"{market}.success_count")
    missing = _nonnegative_integer(row.get("missing_count"), f"{market}.missing_count")
    skipped = _nonnegative_integer(row.get("skipped_count"), f"{market}.skipped_count")
    if processed != population or success + missing + skipped != population:
        raise ProbabilitySourceError(f"current source {market} progress counts 无法重放")
    return _full_market_scope_values(market, population, success, missing, skipped)


def _aggregate_full_market_scope(
    scopes: Mapping[str, Mapping[str, object]],
    *,
    legacy: bool,
) -> dict[str, object]:
    if legacy:
        return _previous_full_market_scope_values(
            "ALL",
            sum(cast(int, scopes[market]["population_count"]) for market in ("SH", "SZ", "BJ")),
            sum(cast(int, scopes[market]["eligible_count"]) for market in ("SH", "SZ", "BJ")),
            sum(cast(int, scopes[market]["success_count"]) for market in ("SH", "SZ", "BJ")),
        )
    return _full_market_scope_values(
        "ALL",
        sum(cast(int, scopes[market]["population_count"]) for market in ("SH", "SZ", "BJ")),
        sum(cast(int, scopes[market]["success_count"]) for market in ("SH", "SZ", "BJ")),
        sum(cast(int, scopes[market]["missing_count"]) for market in ("SH", "SZ", "BJ")),
        sum(cast(int, scopes[market]["skipped_count"]) for market in ("SH", "SZ", "BJ")),
    )


def _full_market_scope_values(
    scope: str,
    population: int,
    success: int,
    missing: int,
    skipped: int,
) -> dict[str, object]:
    eligible = success + missing
    return {
        "population_count": population,
        "eligible_count": eligible,
        "success_count": success,
        "missing_count": missing,
        "skipped_count": skipped,
        "eligible_ratio": eligible / population if population else 0.0,
        "success_coverage": success / eligible if eligible else 0.0,
        "minimum_population": PROBABILITY_SOURCE_MINIMUM_POPULATION[scope],
        "minimum_eligible_ratio": PROBABILITY_SOURCE_MINIMUM_ELIGIBLE_RATIO[scope],
        "minimum_success_coverage": PROBABILITY_SOURCE_MINIMUM_COVERAGE[scope],
    }


def _previous_full_market_scope_values(
    scope: str,
    population: int,
    eligible: int,
    success: int,
) -> dict[str, object]:
    return {
        "population_count": population,
        "eligible_count": eligible,
        "success_count": success,
        "eligible_ratio": eligible / population if population else 0.0,
        "success_coverage": success / eligible if eligible else 0.0,
        "minimum_population": PROBABILITY_SOURCE_MINIMUM_POPULATION[scope],
        "minimum_eligible_ratio": PROBABILITY_SOURCE_MINIMUM_ELIGIBLE_RATIO[scope],
        "minimum_success_coverage": PROBABILITY_SOURCE_MINIMUM_COVERAGE[scope],
    }


def _verified_source_projection(
    run: Mapping[str, object],
    records: Sequence[Mapping[str, object]],
) -> VerifiedProbabilitySourceProjection:
    encoded = canonical_json_text({"run": run, "records": records})
    return VerifiedProbabilitySourceProjection(
        encoded=encoded,
        _seal=_VERIFIED_SOURCE_PROJECTION_SEAL,
    )


def _production_score_contract(items: Sequence[Mapping[str, object]]) -> dict[str, str]:
    contracts: set[tuple[str, str]] = set()
    for item in items:
        symbol = _symbol(item.get("symbol"))
        details = _mapping(item.get("score_details"), f"{symbol}.score_details")
        score_spec = _mapping(details.get("score_spec"), f"{symbol}.score_details.score_spec")
        rule_version = _text(
            score_spec.get("rule_version"),
            f"{symbol}.score_spec.rule_version",
        )
        declared_hash = _sha256(
            details.get("score_spec_hash"),
            f"{symbol}.score_details.score_spec_hash",
        )
        computed_hash = stable_score_spec_hash(score_spec)
        if (
            declared_hash != computed_hash
            or not is_current_writable_production_score_contract(rule_version, computed_hash)
        ):
            raise ProbabilitySourceError(f"{symbol} production score rule/spec 未注册或摘要冲突")
        _validate_production_score_replay(item, details, symbol)
        contracts.add((rule_version, computed_hash))
    if len(contracts) != 1:
        raise ProbabilitySourceError("source capture success rows 的生产评分合同不唯一")
    rule_version, spec_hash = next(iter(contracts))
    return {
        "production_score_rule_version": rule_version,
        "production_score_spec_hash": spec_hash,
    }


def _validate_production_score_replay(
    item: Mapping[str, object],
    details: Mapping[str, object],
    symbol: str,
) -> None:
    leader_score = _bounded_integer(
        item.get("leader_score"),
        f"{symbol}.leader_score",
        0,
        100,
    )
    final_score = _bounded_integer(
        item.get("score"),
        f"{symbol}.score",
        0,
        100,
    )
    try:
        replay = verify_score_details(
            details,
            expected_leader_score=leader_score,
            expected_final_score=final_score,
        )
    except MarketScanReplayError as exc:
        raise ProbabilitySourceError(f"{symbol} production score_details 无法确定性重放") from exc
    inputs = _mapping(details.get("inputs"), f"{symbol}.score_details.inputs")
    expected = {
        "raw_score": replay.raw_score,
        "trend_score": inputs.get("trend_score"),
        "change_pct": inputs.get("change_pct"),
        "volume_ratio": inputs.get("volume_ratio"),
        "amount": inputs.get("amount"),
        "turnover_rate": inputs.get("turnover_rate"),
        "data_quality_score": inputs.get("data_quality_score"),
    }
    for field, replayed in expected.items():
        persisted = item.get(field)
        if (
            persisted is None
            or replayed is None
            or not math.isclose(
                _number(persisted, f"{symbol}.{field}"),
                _number(replayed, f"{symbol}.score_details.inputs.{field}"),
                rel_tol=0,
                abs_tol=1e-12,
            )
        ):
            raise ProbabilitySourceError(f"{symbol} persisted {field} 与 production score_details 重放不一致")


def capture_source_snapshot(
    directory: str | Path,
    *,
    run: Mapping[str, object],
    records: Sequence[Mapping[str, object]],
    captured_at: str,
    projection_receipt: VerifiedProbabilitySourceProjection | object | None = None,
    before_publish: Callable[[], None] | None = None,
    database_path: str | Path | None = None,
) -> dict[str, object]:
    """Validate, compact, and atomically publish one canonical source snapshot."""
    artifact = build_probability_source_snapshot(
        run=run,
        records=records,
        captured_at=captured_at,
        projection_receipt=projection_receipt,
    )
    normalized_run = cast(Mapping[str, object], cast(Mapping[str, object], artifact["payload"])["run"])
    target = Path(directory).expanduser().absolute() / probability_source_snapshot_filename(
        cast(int, normalized_run["run_id"]),
        artifact,
    )
    try:
        require_project_managed_artifact_database(target, database_path, "research/market_scan_probability_source")
    except MarketScanArtifactLeaseError as exc:
        raise ProbabilitySourceError(str(exc)) from exc
    run_id = cast(int, normalized_run["run_id"])
    if database_path is None:
        _write_probability_source_snapshot(target, artifact, before_publish=before_publish)
    else:
        try:
            with verified_market_scan_artifact_publication(
                database_path,
                target,
                (run_id,),
                managed_directory="research/market_scan_probability_source",
            ):
                _write_probability_source_snapshot(target, artifact, before_publish=before_publish)
        except MarketScanArtifactLeaseError as exc:
            raise ProbabilitySourceError("上涨概率 source 来源批次已失效") from exc
    return _snapshot_info(target, artifact)


def build_probability_source_snapshot(
    *,
    run: Mapping[str, object],
    records: Sequence[Mapping[str, object]],
    captured_at: str,
    projection_receipt: VerifiedProbabilitySourceProjection | object | None = None,
) -> dict[str, object]:
    """Build a strict in-memory artifact without retaining full score details."""
    if not isinstance(projection_receipt, VerifiedProbabilitySourceProjection):
        raise ProbabilitySourceError("current source builder 需要 context-verified projection receipt")
    receipt = projection_receipt.payload
    if receipt.get("run") != run or receipt.get("records") != list(records):
        raise ProbabilitySourceError("source projection receipt 与 run/records 不一致")
    normalized_at = _timestamp(captured_at, "captured_at")
    normalized_run = _normalize_run(run, captured_at=normalized_at, exact_keys=False)
    normalized_records = _capture_records(
        records,
        normalized_run,
        _seal=_VERIFIED_SOURCE_PROJECTION_SEAL,
    )
    feature_schema = _feature_schema()
    payload: dict[str, object] = {
        "contract_version": PROBABILITY_SOURCE_PAYLOAD_CONTRACT_VERSION,
        "captured_at": normalized_at,
        "run": normalized_run,
        "cohort": _cohort(normalized_run),
        "score_semantics": _score_semantics(normalized_run),
        "feature_schema": feature_schema,
        "records": normalized_records,
        "quality": _quality(normalized_records, normalized_run, len(cast(list[str], feature_schema["names"]))),
    }
    artifact: dict[str, object] = {
        "schema_version": PROBABILITY_SOURCE_ARTIFACT_SCHEMA_VERSION,
        "captured_at": normalized_at,
        "payload": payload,
        "integrity": {
            "algorithm": PROBABILITY_SOURCE_DIGEST_ALGORITHM,
            "scope": PROBABILITY_SOURCE_DIGEST_SCOPE,
            "integrity_digest": probability_source_payload_digest(payload),
            "notice": PROBABILITY_SOURCE_INTEGRITY_NOTICE,
            "compression": PROBABILITY_SOURCE_COMPRESSION,
        },
    }
    return verify_probability_source_snapshot(artifact)


def verify_probability_source_snapshot(artifact: Mapping[str, object]) -> dict[str, object]:
    """Fail closed on schema, semantic, feature, quality, or digest conflicts."""
    normalized = cast(dict[str, object], _json_value(artifact, "artifact"))
    _exact_keys(normalized, _TOP_LEVEL_KEYS, "artifact")
    schema_version = str(normalized["schema_version"])
    expected_contract = _SUPPORTED_SOURCE_CONTRACTS.get(schema_version)
    if expected_contract is None:
        raise ProbabilitySourceError("上涨概率 source artifact schema_version 不受支持")
    captured_at = _timestamp(normalized["captured_at"], "artifact.captured_at")
    payload = _validate_payload(
        _mapping(normalized["payload"], "artifact.payload"),
        captured_at,
        expected_contract=expected_contract,
    )
    integrity = _mapping(normalized["integrity"], "artifact.integrity")
    _validate_integrity(integrity, payload)
    return {
        "schema_version": schema_version,
        "captured_at": captured_at,
        "payload": payload,
        "integrity": dict(integrity),
    }


def load_probability_source_snapshot(path: str | Path) -> dict[str, object]:
    """Load one deterministic gzip archive and verify content plus filename."""
    source = Path(path).expanduser().absolute()
    encoded = _read_regular_file(source)
    decoded = _decompress(encoded, source)
    artifact = _decode_artifact(decoded, source)
    verified = verify_probability_source_snapshot(artifact)
    _validate_source_filename(source, verified)
    if encoded != _compressed_artifact_bytes(verified):
        raise ProbabilitySourceError(f"上涨概率 source archive 不是规范确定性 gzip：{source}")
    return verified


def list_probability_source_snapshots(
    directory: str | Path,
    *,
    run_id: int | None = None,
) -> list[dict[str, object]]:
    """List verified source archives with compact quality and storage facts."""
    normalized_run_id = _optional_run_id(run_id)
    root = Path(directory).expanduser().absolute()
    try:
        if not path_has_only_trusted_aliases(root):
            raise ProbabilitySourceError(f"上涨概率 source archive 路径不是目录：{root}")
        facts = root.lstat()
    except ProbabilitySourceError:
        raise
    except FileNotFoundError:
        return []
    except (OSError, RuntimeError) as exc:
        raise ProbabilitySourceError(f"上涨概率 source archive 目录无法读取：{root}") from exc
    if not stat.S_ISDIR(facts.st_mode):
        raise ProbabilitySourceError(f"上涨概率 source archive 路径不是目录：{root}")
    paths = sorted(root.glob("market-scan-probability-source-run-*.json.gz"))
    output: list[dict[str, object]] = []
    for path in paths:
        encoded_run_id, _digest = _filename_identity(path)
        if normalized_run_id is None or encoded_run_id == normalized_run_id:
            output.append(_snapshot_info(path, load_probability_source_snapshot(path)))
    return sorted(output, key=lambda item: (str(item["captured_at"]), int(cast(int, item["run_id"])), str(item["digest"])))


def load_probability_source_snapshot_for_run(
    directory: str | Path,
    run_id: int,
) -> dict[str, object] | None:
    """Load the newest verified capture for a run, surviving process restart."""
    candidates = list_probability_source_snapshots(directory, run_id=run_id)
    if not candidates:
        return None
    newest_at = max(_parsed_timestamp(str(item["captured_at"])).timestamp() for item in candidates)
    newest = [item for item in candidates if _parsed_timestamp(str(item["captured_at"])).timestamp() == newest_at]
    if len(newest) != 1:
        raise ProbabilitySourceError(f"run {run_id} 存在同 captured_at 的冲突 source archives")
    return load_probability_source_snapshot(cast(str, newest[0]["path"]))


def canonical_probability_source_json(value: object) -> str:
    """Return canonical finite JSON used by both SHA-256 and gzip."""
    normalized = _json_value(value, "JSON")
    try:
        return canonical_json_text(normalized)
    except ArtifactCanonicalJsonError as exc:  # pragma: no cover - normalized above
        raise ProbabilitySourceError("上涨概率 source archive 不是有限 JSON") from exc


def probability_source_payload_digest(payload: Mapping[str, object]) -> str:
    """Return the content address for one normalized source payload."""
    return sha256_hex(canonical_probability_source_json(payload))


def probability_source_snapshot_filename(run_id: int, artifact: Mapping[str, object]) -> str:
    """Return the only accepted content-addressed filename."""
    normalized_run_id = _run_id(run_id, "run_id")
    verified = verify_probability_source_snapshot(artifact)
    payload = cast(Mapping[str, object], verified["payload"])
    archived_run = cast(Mapping[str, object], payload["run"])
    if archived_run["run_id"] != normalized_run_id:
        raise ProbabilitySourceError("上涨概率 source 文件 run_id 与 payload 冲突")
    integrity = cast(Mapping[str, object], verified["integrity"])
    return content_addressed_filename(
        "market-scan-probability-source-run",
        (normalized_run_id,),
        cast(str, integrity["integrity_digest"]),
        ".json.gz",
    )


def _validate_payload(
    payload: Mapping[str, object],
    captured_at: str,
    *,
    expected_contract: str,
) -> dict[str, object]:
    normalized = cast(dict[str, object], _json_value(payload, "payload"))
    _exact_keys(normalized, _PAYLOAD_KEYS, "payload")
    if normalized["contract_version"] != expected_contract:
        raise ProbabilitySourceError("上涨概率 source payload contract_version 不受支持")
    if _timestamp(normalized["captured_at"], "payload.captured_at") != captured_at:
        raise ProbabilitySourceError("上涨概率 source payload captured_at 与 artifact 冲突")
    legacy = expected_contract == LEGACY_PROBABILITY_SOURCE_PAYLOAD_CONTRACT_VERSION
    previous = expected_contract == PREVIOUS_PROBABILITY_SOURCE_PAYLOAD_CONTRACT_VERSION
    run = _normalize_run(
        _mapping(normalized["run"], "payload.run"),
        captured_at=captured_at,
        exact_keys=True,
        legacy=legacy,
        previous=previous,
    )
    _validate_cohort(_mapping(normalized["cohort"], "payload.cohort"), run)
    score_semantics = _validate_score_semantics(
        _mapping(normalized["score_semantics"], "payload.score_semantics"),
        run=run,
        legacy=legacy,
    )
    feature_schema = _validate_feature_schema(_mapping(normalized["feature_schema"], "payload.feature_schema"))
    records = _stored_records(
        normalized["records"],
        run,
        feature_version=str(feature_schema["version"]),
        legacy=legacy,
    )
    expected_quality = _quality(
        records,
        run,
        len(cast(list[str], feature_schema["names"])),
        legacy=legacy,
        previous=previous,
    )
    quality = _mapping(normalized["quality"], "payload.quality")
    _exact_keys(
        quality,
        _LEGACY_QUALITY_KEYS if legacy else _PREVIOUS_QUALITY_KEYS if previous else _QUALITY_KEYS,
        "payload.quality",
    )
    if quality != expected_quality:
        raise ProbabilitySourceError("上涨概率 source quality 不能由 records 重放")
    return {
        "contract_version": expected_contract,
        "captured_at": captured_at,
        "run": run,
        "cohort": _cohort(run),
        "score_semantics": score_semantics,
        "feature_schema": feature_schema,
        "records": records,
        "quality": expected_quality,
    }


def _validate_integrity(integrity: Mapping[str, object], payload: Mapping[str, object]) -> None:
    _exact_keys(integrity, _INTEGRITY_KEYS, "artifact.integrity")
    contract = {
        "algorithm": PROBABILITY_SOURCE_DIGEST_ALGORITHM,
        "scope": PROBABILITY_SOURCE_DIGEST_SCOPE,
        "notice": PROBABILITY_SOURCE_INTEGRITY_NOTICE,
        "compression": PROBABILITY_SOURCE_COMPRESSION,
    }
    if any(integrity.get(name) != value for name, value in contract.items()):
        raise ProbabilitySourceError("上涨概率 source integrity contract 冲突")
    digest = _sha256(integrity.get("integrity_digest"), "artifact.integrity.integrity_digest")
    if digest != probability_source_payload_digest(payload):
        raise ProbabilitySourceError("上涨概率 source payload digest 不一致")


def _normalize_run(
    run: Mapping[str, object],
    *,
    captured_at: str,
    exact_keys: bool,
    legacy: bool = False,
    previous: bool = False,
) -> dict[str, object]:
    if legacy and previous:
        raise ProbabilitySourceError("上涨概率 source run generation 冲突")
    _require_run_keys(run, exact_keys=exact_keys, legacy=legacy, previous=previous)
    run_id = _run_id(run.get("run_id"), "run.run_id")
    status = _text(run.get("status"), "run.status")
    if status not in {"success", "degraded"}:
        raise ProbabilitySourceError("上涨概率 source 仅接受已发布 success/degraded run")
    if run.get("mode") != "official" or run.get("scope") != FULL_MARKET_SCOPE:
        raise ProbabilitySourceError("上涨概率 source 仅接受 official 全市场 run")
    if run.get("canonical_published") is not True:
        raise ProbabilitySourceError("上涨概率 source run 必须由调用方确认 canonical_published")
    quote_date = _date_text(run.get("quote_date"), "run.quote_date")
    data_date = _date_text(run.get("data_date"), "run.data_date")
    if quote_date != data_date:
        raise ProbabilitySourceError("official source 的 quote_date/data_date 必须一致")
    as_of = _timestamp(run.get("as_of"), "run.as_of")
    _require_source_run_timing(
        quote_date,
        as_of,
        captured_at,
        current_contract=not (legacy or previous),
    )
    total_count = _nonnegative_integer(run.get("total_count"), "run.total_count")
    success_count = _run_id(run.get("success_count"), "run.success_count")
    if success_count > total_count:
        raise ProbabilitySourceError("上涨概率 source success_count 超过 total_count")
    normalized = {
        "run_id": run_id,
        "status": status,
        "mode": "official",
        "scope": FULL_MARKET_SCOPE,
        "rule_version": _text(run.get("rule_version"), "run.rule_version"),
        "quote_date": quote_date,
        "data_date": data_date,
        "as_of": as_of,
        "total_count": total_count,
        "success_count": success_count,
        "canonical_published": True,
    }
    if not legacy:
        normalized.update(
            _normalized_versioned_run_contract(
                run,
                total_count=total_count,
                success_count=success_count,
                previous=previous,
            )
        )
    return normalized


def _require_source_run_timing(
    quote_date: str,
    as_of: str,
    captured_at: str,
    *,
    current_contract: bool,
) -> None:
    parsed_as_of = _parsed_timestamp(as_of)
    parsed_captured_at = _parsed_timestamp(captured_at)
    if current_contract:
        valid = current_official_source_temporal_contract_matches(
            date.fromisoformat(quote_date),
            as_of=parsed_as_of,
            captured_at=parsed_captured_at,
        )
    else:
        valid = as_of[:10] == quote_date and parsed_captured_at >= parsed_as_of
    if not valid:
        raise ProbabilitySourceError("上涨概率 source as_of/date/captured_at 时间顺序无效")


def _require_run_keys(
    run: Mapping[str, object],
    *,
    exact_keys: bool,
    legacy: bool,
    previous: bool,
) -> None:
    if legacy:
        run_keys = _LEGACY_RUN_KEYS
    elif previous:
        run_keys = _PREVIOUS_RUN_KEYS
    else:
        run_keys = _RUN_KEYS
    if exact_keys:
        _exact_keys(run, run_keys, "run")
    missing = run_keys.difference(run)
    if missing:
        raise ProbabilitySourceError(f"上涨概率 source run 缺少字段：{sorted(missing)}")


def _normalized_versioned_run_contract(
    run: Mapping[str, object],
    *,
    total_count: int,
    success_count: int,
    previous: bool,
) -> dict[str, object]:
    normalized: dict[str, object] = dict(
        _registered_run_score_contract(run, current_writable=not previous),
    )
    if previous:
        normalized["full_market_coverage"] = validate_previous_full_market_coverage(
            run.get("full_market_coverage"),
            total_count=total_count,
            success_count=success_count,
        )
        return normalized
    skipped_count = _nonnegative_integer(run.get("skipped_count"), "run.skipped_count")
    if success_count + skipped_count > total_count:
        raise ProbabilitySourceError("上涨概率 source success/skipped_count 超过 total_count")
    normalized["skipped_count"] = skipped_count
    normalized["full_market_coverage"] = validate_current_full_market_coverage(
        run.get("full_market_coverage"),
        total_count=total_count,
        success_count=success_count,
        skipped_count=skipped_count,
    )
    return normalized


def _registered_run_score_contract(
    run: Mapping[str, object],
    *,
    current_writable: bool,
) -> dict[str, str]:
    production_rule = _text(
        run.get("production_score_rule_version"),
        "run.production_score_rule_version",
    )
    production_spec_hash = _sha256(
        run.get("production_score_spec_hash"),
        "run.production_score_spec_hash",
    )
    validator = (
        is_current_writable_production_score_contract
        if current_writable
        else is_registered_production_score_contract
    )
    if not validator(production_rule, production_spec_hash):
        raise ProbabilitySourceError("上涨概率 source production score rule/spec 未注册")
    return {
        "production_score_rule_version": production_rule,
        "production_score_spec_hash": production_spec_hash,
    }


def _capture_records(
    records: Sequence[Mapping[str, object]],
    run: Mapping[str, object],
    *,
    _seal: object | None = None,
) -> list[dict[str, object]]:
    if _seal is not _VERIFIED_SOURCE_PROJECTION_SEAL:
        raise ProbabilitySourceError("current source records 缺少 context-verified receipt")
    normalized = [_capture_record(record, run, _seal=_VERIFIED_SOURCE_PROJECTION_SEAL) for record in records]
    return _validated_record_set(normalized, run)


def _capture_record(
    record: Mapping[str, object],
    run: Mapping[str, object],
    *,
    _seal: object | None = None,
) -> dict[str, object]:
    if _seal is not _VERIFIED_SOURCE_PROJECTION_SEAL:
        raise ProbabilitySourceError("current source record 缺少 context-verified receipt")
    _exact_keys(record, _CAPTURE_RECORD_KEYS, "capture record")
    symbol = _symbol(record.get("symbol"))
    features = _features(record.get("features"))
    dimensions = _dimensions(record.get("dimensions"), run)
    evidence = _mapping(record.get("source_evidence"), f"record {symbol}.source_evidence")
    instrument, evidence_digest = _instrument_from_evidence(symbol, dimensions, evidence, run)
    stored = {
        "symbol": symbol,
        "features": features,
        "feature_vector_digest": stable_probability_hash(features),
        "dimensions": dimensions,
        "source_evidence_contract_version": MARKET_SCAN_EVIDENCE_CONTRACT_VERSION,
        "source_evidence_digest": evidence_digest,
        "instrument": instrument,
    }
    return _stored_record(stored, run)


def _stored_records(
    value: object,
    run: Mapping[str, object],
    *,
    feature_version: str,
    legacy: bool,
) -> list[dict[str, object]]:
    if not isinstance(value, list):
        raise ProbabilitySourceError("上涨概率 source records 必须是数组")
    return _validated_record_set(
        [
            _stored_record(
                _mapping(item, "payload.records[]"),
                run,
                feature_version=feature_version,
                legacy=legacy,
            )
            for item in value
        ],
        run,
    )


def _validated_record_set(records: Sequence[dict[str, object]], run: Mapping[str, object]) -> list[dict[str, object]]:
    ordered = sorted(records, key=lambda item: str(item["symbol"]))
    symbols = [str(item["symbol"]) for item in ordered]
    digests = [str(item["source_evidence_digest"]) for item in ordered]
    if len(symbols) != len(set(symbols)):
        raise ProbabilitySourceError("上涨概率 source records 含重复 symbol")
    if len(digests) != len(set(digests)):
        raise ProbabilitySourceError("上涨概率 source records 含重复 source evidence digest")
    expected = cast(int, run["success_count"])
    if len(ordered) != expected:
        raise ProbabilitySourceError(f"上涨概率 source 记录不完整：{len(ordered)}/{expected}")
    return ordered


def _stored_record(
    record: Mapping[str, object],
    run: Mapping[str, object],
    *,
    feature_version: str = PROBABILITY_FEATURE_VERSION,
    legacy: bool = False,
) -> dict[str, object]:
    _exact_keys(record, _RECORD_KEYS, "stored record")
    symbol = _symbol(record.get("symbol"))
    features = _features(record.get("features"))
    digest = _sha256(record.get("feature_vector_digest"), "record.feature_vector_digest")
    if digest != stable_probability_hash(features):
        raise ProbabilitySourceError(f"{symbol} feature_vector_digest 不一致")
    dimensions = _dimensions(record.get("dimensions"), run)
    evidence_contract = record.get("source_evidence_contract_version")
    supported_evidence_contracts = (
        {
            MARKET_SCAN_EVIDENCE_LEGACY_CONTRACT_VERSION,
            MARKET_SCAN_EVIDENCE_LEGACY_V2_CONTRACT_VERSION,
            MARKET_SCAN_EVIDENCE_CONTRACT_VERSION,
        }
        if legacy
        else {MARKET_SCAN_EVIDENCE_CONTRACT_VERSION}
    )
    if evidence_contract not in supported_evidence_contracts:
        raise ProbabilitySourceError(f"{symbol} source evidence contract 不受支持")
    source_digest = _sha256(record.get("source_evidence_digest"), "record.source_evidence_digest")
    instrument = _instrument(
        record.get("instrument"),
        symbol,
        dimensions,
        run,
        enforce_decision_time=not legacy,
    )
    _validate_feature_context(
        features,
        dimensions,
        instrument,
        symbol,
        feature_version=feature_version,
    )
    return {
        "symbol": symbol,
        "features": features,
        "feature_vector_digest": digest,
        "dimensions": dimensions,
        "source_evidence_contract_version": evidence_contract,
        "source_evidence_digest": source_digest,
        "instrument": instrument,
    }


def _instrument_from_evidence(
    symbol: str,
    dimensions: Mapping[str, object],
    evidence: Mapping[str, object],
    run: Mapping[str, object],
) -> tuple[dict[str, object], str]:
    if evidence.get("contract_version") != MARKET_SCAN_EVIDENCE_CONTRACT_VERSION:
        raise ProbabilitySourceError(f"{symbol} 必须使用当前 point-in-time evidence contract")
    if evidence.get("eligible_for_promotion_evidence") is not True or not verify_market_scan_point_in_time_evidence(evidence):
        raise ProbabilitySourceError(f"{symbol} point-in-time source evidence 未通过验证")
    payload = _mapping(evidence.get("payload"), f"{symbol}.source_evidence.payload")
    quote_timestamp = _validate_evidence_identity(symbol, dimensions, payload, run)
    list_date = _optional_date(payload.get("list_date"), f"{symbol}.list_date", maximum=cast(str, run["quote_date"]))
    instrument = {
        "market": dimensions["market"],
        "board": dimensions["board"],
        "industry": dimensions["industry"],
        "source_industry": _optional_text(payload.get("industry")),
        "liquidity": dimensions["liquidity"],
        "regime": dimensions["regime"],
        "segment": dimensions["segment"],
        "list_date": list_date,
        "is_st": _boolean(payload.get("is_st"), f"{symbol}.is_st"),
        "is_new": _boolean(payload.get("is_new"), f"{symbol}.is_new"),
        "metadata_source": _optional_text(payload.get("metadata_source")),
        "metadata_effective_date": run["quote_date"],
        "metadata_degraded": _boolean(payload.get("metadata_degraded"), f"{symbol}.metadata_degraded"),
        "quote_timestamp": quote_timestamp,
        "quote_price": _positive_number(payload.get("quote_price"), f"{symbol}.quote_price"),
        "quote_change_pct": _number(payload.get("quote_change_pct"), f"{symbol}.quote_change_pct"),
        "quote_turnover_rate": _optional_number(payload.get("quote_turnover_rate"), f"{symbol}.quote_turnover_rate"),
        "quote_amount": _nonnegative_number(payload.get("quote_amount"), f"{symbol}.quote_amount"),
        "reported_volume_ratio": _positive_number(payload.get("reported_volume_ratio"), f"{symbol}.reported_volume_ratio"),
        "data_quality_score": _bounded_integer(payload.get("data_quality_score"), f"{symbol}.data_quality_score", 0, 100),
        "adjustment_mode": _evidence_adjustment_mode(payload.get("bar_contract_61"), symbol),
        "quote_fallback_used": _boolean(payload.get("quote_fallback_used"), f"{symbol}.quote_fallback_used"),
        "kline_fallback_used": _boolean(payload.get("kline_fallback_used"), f"{symbol}.kline_fallback_used"),
    }
    return _instrument(instrument, symbol, dimensions, run), _sha256(evidence.get("payload_digest"), "source_evidence.payload_digest")


def _validate_evidence_identity(
    symbol: str,
    dimensions: Mapping[str, object],
    payload: Mapping[str, object],
    run: Mapping[str, object],
) -> str:
    expected = {
        "symbol": symbol,
        "mode": "official",
        "market": dimensions["market"],
        "quote_date": run["quote_date"],
        "data_date": run["data_date"],
    }
    if any(payload.get(name) != value for name, value in expected.items()):
        raise ProbabilitySourceError(f"{symbol} source evidence 身份与 run/dimensions 冲突")
    # Current provider evidence may contain a runtime-local market timestamp.
    # Its sealed payload is verified above before this projection-only
    # normalization. Artifacts themselves remain strict and timezone-aware.
    quote_timestamp = _projection_timestamp(
        payload.get("quote_timestamp"),
        f"{symbol}.quote_timestamp",
    )
    if quote_timestamp[:10] != run["quote_date"]:
        raise ProbabilitySourceError(f"{symbol} quote_timestamp 不属于 run.quote_date")
    if _parsed_timestamp(quote_timestamp) > _parsed_timestamp(cast(str, run["as_of"])):
        raise ProbabilitySourceError(f"{symbol} quote_timestamp 不能晚于 run.as_of")
    return quote_timestamp


def _instrument(
    value: object,
    symbol: str,
    dimensions: Mapping[str, object],
    run: Mapping[str, object],
    *,
    enforce_decision_time: bool = True,
) -> dict[str, object]:
    instrument = _mapping(value, f"{symbol}.instrument")
    _exact_keys(instrument, _INSTRUMENT_KEYS, f"{symbol}.instrument")
    cross_fields = ("market", "board", "industry", "liquidity", "regime", "segment")
    if any(instrument.get(name) != dimensions[name] for name in cross_fields):
        raise ProbabilitySourceError(f"{symbol} instrument 与 dimensions 冲突")
    if instrument.get("metadata_effective_date") != run["quote_date"] or instrument.get("adjustment_mode") != "qfq":
        raise ProbabilitySourceError(f"{symbol} instrument PIT date/adjustment_mode 冲突")
    normalized = dict(instrument)
    normalized["list_date"] = _optional_date(instrument.get("list_date"), f"{symbol}.list_date", maximum=cast(str, run["quote_date"]))
    normalized["source_industry"] = _optional_text(instrument.get("source_industry"))
    normalized["metadata_source"] = _optional_text(instrument.get("metadata_source"))
    normalized["is_st"] = _boolean(instrument.get("is_st"), f"{symbol}.is_st")
    normalized["is_new"] = _boolean(instrument.get("is_new"), f"{symbol}.is_new")
    normalized["metadata_degraded"] = _boolean(instrument.get("metadata_degraded"), f"{symbol}.metadata_degraded")
    normalized["quote_fallback_used"] = _boolean(instrument.get("quote_fallback_used"), f"{symbol}.quote_fallback_used")
    normalized["kline_fallback_used"] = _boolean(instrument.get("kline_fallback_used"), f"{symbol}.kline_fallback_used")
    quote_timestamp = instrument.get("quote_timestamp")
    normalized["quote_timestamp"] = (
        _timestamp(quote_timestamp, f"{symbol}.quote_timestamp") if enforce_decision_time else _text(quote_timestamp, f"{symbol}.quote_timestamp")
    )
    normalized["quote_price"] = _positive_number(instrument.get("quote_price"), f"{symbol}.quote_price")
    normalized["quote_change_pct"] = _number(instrument.get("quote_change_pct"), f"{symbol}.quote_change_pct")
    normalized["quote_turnover_rate"] = _optional_number(instrument.get("quote_turnover_rate"), f"{symbol}.quote_turnover_rate")
    normalized["quote_amount"] = _nonnegative_number(instrument.get("quote_amount"), f"{symbol}.quote_amount")
    normalized["reported_volume_ratio"] = _positive_number(instrument.get("reported_volume_ratio"), f"{symbol}.reported_volume_ratio")
    normalized["data_quality_score"] = _bounded_integer(instrument.get("data_quality_score"), f"{symbol}.data_quality_score", 0, 100)
    if cast(str, normalized["quote_timestamp"])[:10] != run["quote_date"]:
        raise ProbabilitySourceError(f"{symbol} instrument quote_timestamp 日期冲突")
    if enforce_decision_time and _parsed_timestamp(cast(str, normalized["quote_timestamp"])) > _parsed_timestamp(cast(str, run["as_of"])):
        raise ProbabilitySourceError(f"{symbol} instrument quote_timestamp 不能晚于 run.as_of")
    _validate_symbol_market_board(symbol, cast(str, dimensions["market"]), cast(str, dimensions["board"]))
    _validate_segment_flags(symbol, cast(str, dimensions["segment"]), cast(bool, normalized["is_st"]), cast(bool, normalized["is_new"]))
    return normalized


def _dimensions(value: object, run: Mapping[str, object]) -> dict[str, object]:
    dimensions = _mapping(value, "record.dimensions")
    _exact_keys(dimensions, _DIMENSION_KEYS, "record.dimensions")
    if any(dimensions.get(name) != run[name] for name in ("mode", "scope", "rule_version")):
        raise ProbabilitySourceError("上涨概率 source dimensions 与 run cohort 冲突")
    normalized: dict[str, object] = {name: _text(dimensions.get(name), f"dimensions.{name}") for name in _DIMENSION_KEYS}
    if normalized["market"] not in {"SH", "SZ", "BJ"}:
        raise ProbabilitySourceError("上涨概率 source dimensions.market 无效")
    if normalized["liquidity"] not in {"high", "medium", "low"}:
        raise ProbabilitySourceError("上涨概率 source dimensions.liquidity 无效")
    if normalized["regime"] not in {"strong", "neutral", "weak", "unknown"}:
        raise ProbabilitySourceError("上涨概率 source dimensions.regime 无效")
    if normalized["segment"] not in {"regular", "st", "new"}:
        raise ProbabilitySourceError("上涨概率 source dimensions.segment 无效")
    return normalized


def _features(value: object) -> dict[str, float]:
    values = _mapping(value, "record.features")
    if tuple(sorted(values)) != _REGISTERED_FEATURE_NAMES:
        raise ProbabilitySourceError("上涨概率 source features 与注册 feature schema 不一致")
    return {name: _number(values[name], f"features.{name}") for name in _REGISTERED_FEATURE_NAMES}


def _validate_feature_context(
    features: Mapping[str, float],
    dimensions: Mapping[str, object],
    instrument: Mapping[str, object],
    symbol: str,
    *,
    feature_version: str = PROBABILITY_FEATURE_VERSION,
) -> None:
    expected = probability_feature_vector(
        {},
        market=cast(str, dimensions["market"]),
        board=cast(str, dimensions["board"]),
        liquidity=cast(str, dimensions["liquidity"]),
        regime=cast(str, dimensions["regime"]),
        industry=cast(str, dimensions["industry"]),
        segment=cast(str, dimensions["segment"]),
        feature_version=feature_version,
    )
    if any(not math.isclose(features[name], expected[name], rel_tol=0, abs_tol=1e-12) for name in _CATEGORICAL_FEATURES):
        raise ProbabilitySourceError(f"{symbol} categorical features 与 PIT metadata 冲突")
    numeric_pairs = (
        ("change_pct", "quote_change_pct"),
        ("data_quality_score", "data_quality_score"),
        ("turnover_rate", "quote_turnover_rate"),
        ("volume_ratio", "reported_volume_ratio"),
    )
    for feature_name, instrument_name in numeric_pairs:
        expected_value = instrument[instrument_name]
        expected_number = _number(expected_value, f"instrument.{instrument_name}") if expected_value is not None else 0.0
        if not math.isclose(features[feature_name], expected_number, rel_tol=0, abs_tol=1e-8):
            raise ProbabilitySourceError(f"{symbol} {feature_name} 与 source evidence 冲突")
    quote_amount = _number(instrument["quote_amount"], "instrument.quote_amount")
    if not math.isclose(features["log_amount"], math.log1p(quote_amount), rel_tol=0, abs_tol=1e-8):
        raise ProbabilitySourceError(f"{symbol} log_amount 与 source evidence 冲突")


def _feature_schema(
    version: str = PROBABILITY_FEATURE_VERSION,
) -> dict[str, object]:
    if version not in {PROBABILITY_FEATURE_VERSION, LEGACY_PROBABILITY_FEATURE_VERSION}:
        raise ProbabilitySourceError("上涨概率 source feature_schema version 不受支持")
    names = list(_REGISTERED_FEATURE_NAMES)
    identity = {"version": version, "names": names}
    return {**identity, "digest": stable_probability_hash(identity)}


def _validate_feature_schema(value: Mapping[str, object]) -> dict[str, object]:
    _exact_keys(value, _FEATURE_SCHEMA_KEYS, "payload.feature_schema")
    version = _text(value.get("version"), "payload.feature_schema.version")
    expected = _feature_schema(version)
    if value != expected:
        raise ProbabilitySourceError("上涨概率 source feature_schema/version/digest 冲突")
    return expected


def _cohort(run: Mapping[str, object]) -> dict[str, object]:
    return {name: run[name] for name in ("mode", "scope", "rule_version")}


def _validate_cohort(value: Mapping[str, object], run: Mapping[str, object]) -> None:
    _exact_keys(value, _COHORT_KEYS, "payload.cohort")
    if value != _cohort(run):
        raise ProbabilitySourceError("上涨概率 source cohort 与 run 冲突")


def _score_semantics(run: Mapping[str, object] | None = None) -> dict[str, object]:
    rule_version = (
        run.get("production_score_rule_version")
        if run is not None
        else FULL_MARKET_LEGACY_V4_SCORE_RULE_VERSION
    )
    semantics = {
        "production_rule_version": rule_version,
        "production_score": "ordinal_state_score_not_upside_probability",
        "source_rank": "persisted_canonical_production_order",
        "probability_ranking_effect": "none",
    }
    if run is not None and run.get("production_score_spec_hash") is not None:
        semantics["production_score_spec_hash"] = run["production_score_spec_hash"]
    return semantics


def _validate_score_semantics(
    value: Mapping[str, object],
    *,
    run: Mapping[str, object] | None = None,
    legacy: bool = False,
) -> dict[str, object]:
    keys = _LEGACY_SCORE_SEMANTICS_KEYS if legacy else _SCORE_SEMANTICS_KEYS
    _exact_keys(value, keys, "payload.score_semantics")
    expected = _score_semantics(None if legacy else run)
    if value != expected:
        raise ProbabilitySourceError("上涨概率 source score semantics 冲突")
    return dict(expected)


def _quality(
    records: Sequence[Mapping[str, object]],
    run: Mapping[str, object],
    feature_count: int,
    *,
    legacy: bool = False,
    previous: bool = False,
) -> dict[str, object]:
    count = len(records)
    instruments = [cast(Mapping[str, object], record["instrument"]) for record in records]
    markets = Counter(str(value["market"]) for value in instruments)
    boards = Counter(str(value["board"]) for value in instruments)
    expected = cast(int, run["success_count"])
    quality = {
        "expected_record_count": expected,
        "record_count": count,
        "record_coverage": count / expected,
        "feature_count": feature_count,
        "feature_value_count": count * feature_count,
        "verified_source_digest_count": count,
        "source_digest_coverage": 1.0 if count else 0.0,
        "metadata_degraded_count": sum(bool(value["metadata_degraded"]) for value in instruments),
        "missing_list_date_count": sum(value["list_date"] is None for value in instruments),
        "missing_metadata_source_count": sum(value["metadata_source"] is None for value in instruments),
        "quote_fallback_count": sum(bool(value["quote_fallback_used"]) for value in instruments),
        "kline_fallback_count": sum(bool(value["kline_fallback_used"]) for value in instruments),
        "market_counts": dict(sorted(markets.items())),
        "board_counts": dict(sorted(boards.items())),
    }
    if legacy:
        return quality
    total = cast(int, run["total_count"])
    success = cast(int, run["success_count"])
    full_market_coverage = cast(
        Mapping[str, object],
        run["full_market_coverage"],
    )
    _validate_record_market_coverage(markets, full_market_coverage)
    quality.update(
        run_total_count=total,
        run_success_count=success,
        success_to_total_coverage=success / total,
        strata_coverage=_strata_coverage(records),
        full_market_coverage=dict(full_market_coverage),
    )
    if not previous:
        skipped = cast(int, run["skipped_count"])
        quality.update(
            run_skipped_count=skipped,
            run_eligible_count=total - skipped,
        )
    return quality


def _validate_record_market_coverage(
    markets: Mapping[str, int],
    coverage: Mapping[str, object],
) -> None:
    scopes = _mapping(coverage.get("scopes"), "full_market_coverage.scopes")
    for market in ("SH", "SZ", "BJ"):
        scope = _mapping(scopes.get(market), f"full_market_coverage.{market}")
        if markets.get(market, 0) != scope.get("success_count"):
            raise ProbabilitySourceError(f"current source {market} success coverage 与 records 冲突")


def _strata_coverage(records: Sequence[Mapping[str, object]]) -> dict[str, object]:
    dimensions = [cast(Mapping[str, object], item["dimensions"]) for item in records]
    return {name: _stratum_coverage(dimensions, name) for name in ("market", "board", "industry", "liquidity", "regime")}


def _stratum_coverage(
    dimensions: Sequence[Mapping[str, object]],
    name: str,
) -> dict[str, object]:
    values = [str(item[name]) for item in dimensions]
    missing = sum(_missing_stratum(value) for value in values)
    observed = len(values) - missing
    result = {
        "observed_count": observed,
        "missing_count": missing,
        "coverage": observed / len(values) if values else 0.0,
        "counts": dict(sorted(Counter(values).items())),
    }
    _exact_keys(result, _STRATA_COVERAGE_KEYS, f"quality.strata_coverage.{name}")
    return result


def _missing_stratum(value: str) -> bool:
    return value.strip().casefold() in {"unknown", "未知", "未分类", "--"}


def _score_archive_stats(path: Path, artifact: Mapping[str, object]) -> dict[str, object]:
    encoded = _read_regular_file(path)
    uncompressed = canonical_probability_source_json(artifact).encode("utf-8")
    return {
        "compressed_bytes": len(encoded),
        "uncompressed_bytes": len(uncompressed),
        "compression_ratio": round(len(encoded) / len(uncompressed), 6),
        "compressed_sha256": sha256_hex(encoded),
    }


def _snapshot_info(path: Path, artifact: Mapping[str, object]) -> dict[str, object]:
    verified = verify_probability_source_snapshot(artifact)
    payload = cast(Mapping[str, object], verified["payload"])
    run = cast(Mapping[str, object], payload["run"])
    integrity = cast(Mapping[str, object], verified["integrity"])
    return {
        "path": str(path.absolute()),
        "run_id": run["run_id"],
        "quote_date": run["quote_date"],
        "captured_at": verified["captured_at"],
        "digest": integrity["integrity_digest"],
        "feature_schema_digest": cast(Mapping[str, object], payload["feature_schema"])["digest"],
        "quality": deepcopy(payload["quality"]),
        "storage": _score_archive_stats(path, verified),
    }


def _write_probability_source_snapshot(
    path: Path,
    artifact: Mapping[str, object],
    *,
    before_publish: Callable[[], None] | None = None,
) -> Path:
    target = path.expanduser().absolute()
    verified = verify_probability_source_snapshot(artifact)
    encoded = _compressed_artifact_bytes(verified)
    try:
        exclusive_atomic_publish(
            target,
            encoded,
            max_bytes=PROBABILITY_SOURCE_MAX_COMPRESSED_BYTES,
            before_publish=before_publish,
        )
    except ArtifactContentConflictError as exc:
        raise ProbabilitySourceError("上涨概率 source archive 已存在且内容不同，拒绝覆盖") from exc
    except ArtifactPublishConflictError as exc:
        raise ProbabilitySourceError("上涨概率 source archive 并发发布冲突") from exc
    except ArtifactNotDirectoryError as exc:
        raise ProbabilitySourceError(f"上涨概率 source archive 输出目录必须是真实目录（不能是符号链接）：{target.parent}") from exc
    except ArtifactNotRegularError as exc:
        raise ProbabilitySourceError(f"上涨概率 source archive target 不是普通文件：{target}") from exc
    except ArtifactTooLargeError as exc:
        raise ProbabilitySourceError(f"上涨概率 source archive 超过压缩大小上限：{target}") from exc
    except ArtifactIOError as exc:
        raise ProbabilitySourceError(f"上涨概率 source archive 无法读取：{target}") from exc
    except ProbabilitySourceError:
        raise
    except OSError as exc:
        raise ProbabilitySourceError(f"上涨概率 source archive 写入失败：{target}") from exc
    return target


def _compressed_artifact_bytes(artifact: Mapping[str, object]) -> bytes:
    encoded = canonical_probability_source_json(artifact).encode("utf-8")
    if len(encoded) > PROBABILITY_SOURCE_MAX_UNCOMPRESSED_BYTES:
        raise ProbabilitySourceError("上涨概率 source archive 未压缩内容超过安全上限")
    compressed = gzip.compress(encoded, compresslevel=9, mtime=0)
    if len(compressed) > PROBABILITY_SOURCE_MAX_COMPRESSED_BYTES:
        raise ProbabilitySourceError("上涨概率 source archive 压缩内容超过安全上限")
    return compressed


def _read_regular_file(path: Path) -> bytes:
    try:
        return read_regular_file(path, max_bytes=PROBABILITY_SOURCE_MAX_COMPRESSED_BYTES)
    except ArtifactNotRegularError as exc:
        raise ProbabilitySourceError(f"上涨概率 source archive 必须是普通文件：{path}") from exc
    except ArtifactTooLargeError as exc:
        raise ProbabilitySourceError(f"上涨概率 source archive 超过压缩大小上限：{path}") from exc
    except ArtifactChangedError as exc:
        action = "读取目标在打开期间" if exc.stage == "open" else "在读取期间"
        raise ProbabilitySourceError(f"上涨概率 source archive {action}发生变化：{path}") from exc
    except ArtifactIOError as exc:
        raise ProbabilitySourceError(f"上涨概率 source archive 无法读取：{path}") from exc


def _decompress(encoded: bytes, path: Path) -> bytes:
    try:
        decoded = _bounded_gzip_bytes(encoded)
    except (gzip.BadGzipFile, EOFError, OSError) as exc:
        raise ProbabilitySourceError(f"上涨概率 source archive gzip 损坏：{path}") from exc
    if len(decoded) > PROBABILITY_SOURCE_MAX_UNCOMPRESSED_BYTES:
        raise ProbabilitySourceError(f"上涨概率 source archive 解压内容超过安全上限：{path}")
    return decoded


def _bounded_gzip_bytes(encoded: bytes) -> bytes:
    chunks: list[bytes] = []
    remaining = PROBABILITY_SOURCE_MAX_UNCOMPRESSED_BYTES + 1
    with gzip.GzipFile(fileobj=BytesIO(encoded), mode="rb") as stream:
        while remaining:
            chunk = stream.read(min(1024 * 1024, remaining))
            if not chunk:
                break
            chunks.append(chunk)
            remaining -= len(chunk)
    return b"".join(chunks)


def _decode_artifact(encoded: bytes, path: Path) -> Mapping[str, object]:
    try:
        decoded = decode_json_bytes(encoded)
    except ArtifactDuplicateKeyError as exc:
        raise ProbabilitySourceError(f"上涨概率 source archive 含重复 JSON key：{exc.key}") from exc
    except ArtifactNonFiniteConstantError as exc:
        raise ProbabilitySourceError(f"上涨概率 source archive 含非有限 JSON 常量：{exc.constant}") from exc
    except ArtifactIOError as exc:
        raise ProbabilitySourceError(f"上涨概率 source archive JSON 损坏：{path}") from exc
    if not isinstance(decoded, Mapping):
        raise ProbabilitySourceError("上涨概率 source archive 顶层必须是 JSON object")
    return cast(Mapping[str, object], decoded)


def _validate_source_filename(path: Path, artifact: Mapping[str, object]) -> None:
    run_id, digest = _filename_identity(path)
    payload = cast(Mapping[str, object], artifact["payload"])
    archived_run = cast(Mapping[str, object], payload["run"])
    integrity = cast(Mapping[str, object], artifact["integrity"])
    if run_id != archived_run["run_id"] or digest != integrity["integrity_digest"]:
        raise ProbabilitySourceError("上涨概率 source archive 文件名与内容地址冲突")


def _filename_identity(path: Path) -> tuple[int, str]:
    match = _SOURCE_FILENAME.fullmatch(path.name)
    if match is None:
        raise ProbabilitySourceError(f"上涨概率 source archive 文件名不规范：{path.name}")
    return int(match.group(1)), match.group(2)


def _evidence_adjustment_mode(value: object, symbol: str) -> str:
    if not isinstance(value, list) or not value:
        raise ProbabilitySourceError(f"{symbol} source evidence 缺少 bar_contract_61")
    modes: set[str] = set()
    for row in value:
        if not isinstance(row, list) or len(row) < 9:
            raise ProbabilitySourceError(f"{symbol} source evidence bar contract 无效")
        modes.add(str(row[6]))
    if modes != {"qfq"}:
        raise ProbabilitySourceError(f"{symbol} source evidence adjustment_mode 必须统一为 qfq")
    return "qfq"


def _validate_symbol_market_board(symbol: str, market: str, board: str) -> None:
    suffix = symbol.rsplit(".", 1)[-1]
    code = symbol.split(".", 1)[0]
    expected_board = (
        "BSE"
        if market == "BJ"
        else "STAR"
        if market == "SH" and code.startswith(("688", "689"))
        else "CHINEXT"
        if market == "SZ" and code.startswith(("300", "301"))
        else f"{market}_MAIN"
    )
    if suffix != market or board != expected_board:
        raise ProbabilitySourceError(f"{symbol} market/board 身份冲突")


def _validate_segment_flags(symbol: str, segment: str, is_st: bool, is_new: bool) -> None:
    expected = "st" if is_st else "new" if is_new else "regular"
    if segment != expected:
        raise ProbabilitySourceError(f"{symbol} segment 与 is_st/is_new 冲突")


def _projection_context(item: Mapping[str, object], run: Mapping[str, object]) -> dict[str, object]:
    symbol = _symbol(item.get("symbol"))
    if item.get("run_id") != run["run_id"]:
        raise ProbabilitySourceError(f"{symbol} result.run_id 与 run 冲突")
    market = _text(item.get("market"), f"{symbol}.market")
    board = _projected_board(symbol, market)
    is_st = _boolean(item.get("is_st"), f"{symbol}.is_st")
    is_new = _boolean(item.get("is_new"), f"{symbol}.is_new")
    raw_score_value = item.get("raw_score") if item.get("raw_score") is not None else item.get("score")
    return {
        "symbol": symbol,
        "market": market,
        "board": board,
        "industry": _projected_industry(item.get("industry")),
        "liquidity": _projected_liquidity(item.get("amount")),
        "segment": "st" if is_st else "new" if is_new else "regular",
        "is_st": is_st,
        "is_new": is_new,
        "raw_score": _number(raw_score_value, f"{symbol}.raw_score"),
    }


def _project_capture_record(
    item: Mapping[str, object],
    context: Mapping[str, object],
    run: Mapping[str, object],
    *,
    market_strength: float,
    board_relative_strength: float,
    industry_relative_strength: float,
    regime: str,
) -> dict[str, object]:
    symbol = cast(str, context["symbol"])
    values = _projection_factor_values(item, context, run)
    dimensions = {
        "mode": run["mode"],
        "scope": run["scope"],
        "rule_version": run["rule_version"],
        "market": context["market"],
        "board": context["board"],
        "industry": context["industry"],
        "liquidity": context["liquidity"],
        "regime": regime,
        "segment": context["segment"],
    }
    score_details = _mapping(item.get("score_details"), f"{symbol}.score_details")
    components = _mapping(score_details.get("components"), f"{symbol}.score_details.components")
    score_dimensions = _mapping(components.get("score_dimensions"), f"{symbol}.score_dimensions")
    evidence = _mapping(score_dimensions.get("point_in_time_evidence"), f"{symbol}.point_in_time_evidence")
    _validate_projection_evidence_context(item, evidence, run, symbol)
    return {
        "symbol": symbol,
        "features": probability_feature_vector(
            values,
            market=cast(str, context["market"]),
            board=cast(str, context["board"]),
            liquidity=cast(str, context["liquidity"]),
            regime=regime,
            industry=cast(str, context["industry"]),
            segment=cast(str, context["segment"]),
            market_strength=market_strength,
            board_relative_strength=board_relative_strength,
            industry_relative_strength=industry_relative_strength,
        ),
        "dimensions": dimensions,
        "source_evidence": evidence,
    }


def _validate_projection_evidence_context(
    item: Mapping[str, object],
    evidence: Mapping[str, object],
    run: Mapping[str, object],
    symbol: str,
) -> None:
    try:
        persisted = MarketScanResultItem.model_validate(item)
    except ValueError as exc:
        raise ProbabilitySourceError(f"{symbol} persisted result 上下文无效") from exc
    if not verify_market_scan_point_in_time_evidence_context(
        evidence,
        item=persisted,
        expected_data_date=cast(str, run["data_date"]),
        expected_quote_date=cast(str, run["quote_date"]),
        expected_as_of=cast(str, run["as_of"]),
        expected_mode=cast(MarketScanMode, run["mode"]),
    ):
        raise ProbabilitySourceError(f"{symbol} point-in-time evidence 未绑定 persisted result 上下文")


def _projection_factor_values(
    item: Mapping[str, object],
    context: Mapping[str, object],
    run: Mapping[str, object],
) -> dict[str, float]:
    values: dict[str, float] = {}
    direct = {
        "raw_score": item.get("raw_score") if item.get("raw_score") is not None else item.get("score"),
        "trend_score": item.get("trend_score"),
        "change_pct": item.get("change_pct"),
        "data_quality_score": item.get("data_quality_score"),
        "amount": item.get("amount"),
        "turnover_rate": item.get("turnover_rate"),
        "volume_ratio": item.get("volume_ratio"),
    }
    values.update({name: _number(value, f"result.{name}") for name, value in direct.items() if value is not None})
    score_details = _mapping(item.get("score_details"), "result.score_details")
    components = _mapping(score_details.get("components"), "result.score_details.components")
    _project_score_components(
        values,
        components,
        production_rule_version=_text(
            run.get("production_score_rule_version"),
            "run.production_score_rule_version",
        ),
    )
    dimensions = _mapping(components.get("score_dimensions"), "result.score_dimensions")
    scores = _mapping(dimensions.get("scores"), "result.score_dimensions.scores")
    for name in ("alpha_1d", "alpha_5d", "alpha_20d", "confidence", "risk", "tradability"):
        if scores.get(name) is not None:
            values[name] = _number(scores[name], f"score_dimensions.scores.{name}")
    raw_features = _mapping(dimensions.get("raw_features"), "result.score_dimensions.raw_features")
    for name, value in raw_features.items():
        values[f"feature_{name}"] = _number(value, f"score_dimensions.raw_features.{name}")
    values.update({"is_st": float(cast(bool, context["is_st"])), "is_new": float(cast(bool, context["is_new"]))})
    _project_price_limit_values(values, item, context, run)
    return values


def _project_score_components(
    values: dict[str, float],
    components: Mapping[str, object],
    *,
    production_rule_version: str,
) -> None:
    final_names, refinement_name, required_final = _score_component_generation(
        production_rule_version,
    )
    groups = (
        ("leader_score", ("base", "trend_delta", "unclamped", "score"), "leader_"),
        ("final_score", tuple(final_names), "final_"),
    )
    for group_name, names, prefix in groups:
        group = components.get(group_name)
        if isinstance(group, Mapping):
            for name in names:
                if group.get(name) is not None:
                    values[f"{prefix}{name}"] = _number(group[name], f"components.{group_name}.{name}")
    final_score = _mapping(components.get("final_score"), "components.final_score")
    if final_score.get(required_final) is None:
        raise ProbabilitySourceError(f"components.final_score.{required_final} 缺失")
    refinement = _mapping(components.get(refinement_name), f"components.{refinement_name}")
    if refinement.get("score") is None:
        raise ProbabilitySourceError(f"components.{refinement_name}.score 缺失")
    values["rank_refinement"] = _number(
        refinement["score"],
        f"components.{refinement_name}.score",
    )
    normalized = _mapping(
        refinement.get("normalized_inputs"),
        f"components.{refinement_name}.normalized_inputs",
    )
    for name, value in normalized.items():
        values[f"refinement_{name}"] = _number(
            value,
            f"components.{refinement_name}.normalized_inputs.{name}",
        )
    if production_rule_version == FULL_MARKET_SCORE_RULE_VERSION:
        values["final_rank_discount"] = 0.0


def _score_component_generation(
    production_rule_version: str,
) -> tuple[list[str], str, str]:
    final_names = ["quality_penalty", "base", "raw", "rounded", "score"]
    if production_rule_version == FULL_MARKET_LEGACY_V4_SCORE_RULE_VERSION:
        final_names.append("rank_discount")
        return final_names, "rank_refinement", "rank_discount"
    if production_rule_version == FULL_MARKET_SCORE_RULE_VERSION:
        final_names.append("continuous_trend_adjustment")
        return final_names, "continuous_trend", "continuous_trend_adjustment"
    raise ProbabilitySourceError("source feature projection production score rule 不受支持")


def _project_price_limit_values(
    values: dict[str, float],
    item: Mapping[str, object],
    context: Mapping[str, object],
    run: Mapping[str, object],
) -> None:
    symbol = cast(str, context["symbol"])
    metadata = PaperInstrumentMetadata(
        symbol=symbol,
        market=cast(str, context["market"]),
        list_date=cast(str | None, item.get("list_date")),
        is_st=cast(bool, context["is_st"]),
        status_effective_date=cast(str, run["quote_date"]),
        source="market-scan-probability-source-projection",
    )
    try:
        profile = resolve_trade_rule_profile(symbol, date.fromisoformat(cast(str, run["quote_date"])), metadata)
    except (KeyError, TypeError, ValueError):
        values["price_limit_profile_uncertain"] = 1.0
        return
    verified = profile.quality == "ok"
    values.update(
        {
            "price_limit_pct": float(profile.price_limit_pct or 0.0),
            "price_limit_profile_verified": float(verified),
            "price_limit_profile_uncertain": float(not verified),
            "price_limit_absent": float(verified and profile.price_limit_pct is None),
            "new_stock_no_limit_phase": float(cast(bool, context["is_new"]) and verified and profile.price_limit_pct is None),
        }
    )


def _relative_projection_strength(
    contexts: Sequence[Mapping[str, object]],
    name: str,
    market_strength: float,
) -> dict[str, float]:
    grouped: dict[str, list[float]] = {}
    for item in contexts:
        grouped.setdefault(cast(str, item[name]), []).append(_number(item["raw_score"], "projection.raw_score"))
    return {key: fmean(values) - market_strength for key, values in sorted(grouped.items())}


def _projection_regime(items: Sequence[Mapping[str, object]]) -> str:
    changes = [_number(item["change_pct"], "result.change_pct") for item in items if item.get("change_pct") is not None]
    average = fmean(changes) if changes else 0.0
    return "strong" if average >= 1 else "weak" if average <= -1 else "neutral"


def _projected_board(symbol: str, market: str) -> str:
    _validate_symbol_market_board(symbol, market, _derived_board(symbol, market))
    return _derived_board(symbol, market)


def _derived_board(symbol: str, market: str) -> str:
    code = symbol.split(".", 1)[0]
    if market == "BJ":
        return "BSE"
    if market == "SH" and code.startswith(("688", "689")):
        return "STAR"
    if market == "SZ" and code.startswith(("300", "301")):
        return "CHINEXT"
    return f"{market}_MAIN"


def _projected_industry(value: object) -> str:
    normalized = "".join(str(value or "").split()).strip("-/、，")
    aliases = {
        "信息传输、软件和信息技术服务业": "信息技术",
        "软件和信息技术服务业": "信息技术",
        "信息传输软件和信息技术服务业": "信息技术",
    }
    return aliases.get(normalized, normalized or "UNKNOWN")


def _projected_liquidity(value: object) -> str:
    amount = _nonnegative_number(value, "result.amount")
    return "high" if amount >= 1_000_000_000 else "medium" if amount >= 100_000_000 else "low"


def _object_mapping(value: object, path: str) -> Mapping[str, object]:
    if isinstance(value, Mapping):
        return _mapping(value, path)
    dump = getattr(value, "model_dump", None)
    if not callable(dump):
        raise ProbabilitySourceError(f"{path} 必须是 mapping 或支持 model_dump")
    projected = dump(mode="python")
    return _mapping(projected, path)


def _projection_timestamp(value: object, path: str) -> str:
    """Normalize legacy runtime-local timestamps only at the projection edge."""
    text = _text(value, path)
    try:
        parsed = datetime.fromisoformat(text[:-1] + "+00:00" if text.endswith("Z") else text)
    except ValueError as exc:
        raise ProbabilitySourceError(f"{path} 必须是 ISO-8601") from exc
    if parsed.tzinfo is None or parsed.utcoffset() is None:
        parsed = parsed.replace(tzinfo=ASHARE_TIMEZONE)
    return parsed.astimezone(ASHARE_TIMEZONE).isoformat()


def _json_value(value: object, path: str) -> object:
    if value is None or isinstance(value, str | bool):
        return value
    if isinstance(value, int) and not isinstance(value, bool):
        return value
    if isinstance(value, float):
        if not math.isfinite(value):
            raise ProbabilitySourceError(f"{path} 含非有限数值")
        return value
    if isinstance(value, Mapping):
        return _json_mapping(value, path)
    if isinstance(value, Sequence) and not isinstance(value, str | bytes | bytearray):
        return [_json_value(item, f"{path}[]") for item in value]
    raise ProbabilitySourceError(f"{path} 含不可序列化类型：{type(value).__name__}")


def _json_mapping(value: Mapping[object, object], path: str) -> dict[str, object]:
    normalized: dict[str, object] = {}
    for key, item in value.items():
        if not isinstance(key, str) or key in normalized:
            raise ProbabilitySourceError(f"{path} 含非字符串或重复 key")
        normalized[key] = _json_value(item, f"{path}.{key}")
    return normalized


def _mapping(value: object, path: str) -> Mapping[str, object]:
    if not isinstance(value, Mapping):
        raise ProbabilitySourceError(f"{path} 必须是 object")
    if any(not isinstance(key, str) for key in value):
        raise ProbabilitySourceError(f"{path} key 必须是字符串")
    return cast(Mapping[str, object], value)


def _exact_keys(value: Mapping[str, object], expected: frozenset[str], path: str) -> None:
    if set(value) != expected:
        raise ProbabilitySourceError(f"{path} 字段不完整或含未知字段")


def _text(value: object, path: str) -> str:
    if not isinstance(value, str) or not value.strip() or value != value.strip():
        raise ProbabilitySourceError(f"{path} 必须是非空规范字符串")
    return value


def _optional_text(value: object) -> str | None:
    if value is None:
        return None
    return _text(value, "optional text")


def _symbol(value: object) -> str:
    symbol = _text(value, "record.symbol")
    if _SYMBOL.fullmatch(symbol) is None:
        raise ProbabilitySourceError(f"上涨概率 source symbol 无效：{symbol}")
    return symbol


def _run_id(value: object, path: str) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or value <= 0:
        raise ProbabilitySourceError(f"{path} 必须是正整数")
    return value


def _optional_run_id(value: int | None) -> int | None:
    return None if value is None else _run_id(value, "run_id")


def _nonnegative_integer(value: object, path: str) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or value < 0:
        raise ProbabilitySourceError(f"{path} 必须是非负整数")
    return value


def _bounded_integer(value: object, path: str, lower: int, upper: int) -> int:
    parsed = _nonnegative_integer(value, path)
    if not lower <= parsed <= upper:
        raise ProbabilitySourceError(f"{path} 超出范围")
    return parsed


def _number(value: object, path: str) -> float:
    if isinstance(value, bool) or not isinstance(value, int | float) or not math.isfinite(float(value)):
        raise ProbabilitySourceError(f"{path} 必须是有限数值")
    return float(value)


def _optional_number(value: object, path: str) -> float | None:
    return None if value is None else _number(value, path)


def _positive_number(value: object, path: str) -> float:
    parsed = _number(value, path)
    if parsed <= 0:
        raise ProbabilitySourceError(f"{path} 必须大于 0")
    return parsed


def _nonnegative_number(value: object, path: str) -> float:
    parsed = _number(value, path)
    if parsed < 0:
        raise ProbabilitySourceError(f"{path} 必须大于等于 0")
    return parsed


def _boolean(value: object, path: str) -> bool:
    if not isinstance(value, bool):
        raise ProbabilitySourceError(f"{path} 必须是布尔值")
    return value


def _date_text(value: object, path: str) -> str:
    text = _text(value, path)
    try:
        parsed = date.fromisoformat(text)
    except ValueError as exc:
        raise ProbabilitySourceError(f"{path} 必须是 ISO 日期") from exc
    if parsed.isoformat() != text:
        raise ProbabilitySourceError(f"{path} 必须是规范 ISO 日期")
    return text


def _optional_date(value: object, path: str, *, maximum: str) -> str | None:
    if value is None:
        return None
    parsed = _date_text(value, path)
    if parsed > maximum:
        raise ProbabilitySourceError(f"{path} 不能晚于 point-in-time 日期")
    return parsed


def _timestamp(value: object, path: str) -> str:
    text = _text(value, path)
    _parsed_timestamp(text)
    return text


def _parsed_timestamp(value: str) -> datetime:
    try:
        parsed = datetime.fromisoformat(value[:-1] + "+00:00" if value.endswith("Z") else value)
    except ValueError as exc:
        raise ProbabilitySourceError("timestamp 必须是 ISO-8601") from exc
    if parsed.tzinfo is None or parsed.utcoffset() is None:
        raise ProbabilitySourceError("timestamp 必须包含时区")
    return parsed


def _sha256(value: object, path: str) -> str:
    if not isinstance(value, str) or _SHA256.fullmatch(value) is None:
        raise ProbabilitySourceError(f"{path} 必须是小写 SHA-256")
    return value


__all__ = [
    "LEGACY_PROBABILITY_SOURCE_ARTIFACT_SCHEMA_VERSION",
    "LEGACY_PROBABILITY_SOURCE_PAYLOAD_CONTRACT_VERSION",
    "PROBABILITY_SOURCE_ARTIFACT_SCHEMA_VERSION",
    "PROBABILITY_SOURCE_COMPRESSION",
    "PROBABILITY_SOURCE_INTEGRITY_NOTICE",
    "PROBABILITY_SOURCE_PAYLOAD_CONTRACT_VERSION",
    "ProbabilitySourceError",
    "VerifiedProbabilitySourceProjection",
    "build_probability_source_snapshot",
    "canonical_probability_source_json",
    "capture_source_snapshot",
    "list_probability_source_snapshots",
    "load_probability_source_snapshot",
    "load_probability_source_snapshot_for_run",
    "probability_source_payload_digest",
    "probability_source_snapshot_filename",
    "project_probability_source_capture",
    "validate_previous_full_market_coverage",
    "verify_probability_source_snapshot",
]
