"""Idempotent production maintenance for archived probability labels.

This orchestration layer is deliberately fail-closed: it can mature immutable
outcome evidence, but it never changes full-market ranking or publishes a
probability merely because labels exist.
"""

from __future__ import annotations

from collections.abc import Callable, Iterable, Mapping, Sequence
from dataclasses import dataclass
from datetime import date, datetime
import inspect
from pathlib import Path
import stat
from threading import RLock
from typing import Protocol, TypeVar, cast

from app.artifacts.io import path_has_only_trusted_aliases
from app.models.market import (
    DEFAULT_DAILY_KLINE_ADJUSTMENT_MODE,
    Kline,
    KlineAdjustmentMode,
)
from app.services.market_scan_probability import stable_probability_hash
from app.services.market_scan_probability_fit_assessment import (
    PROBABILITY_FIT_ASSESSMENT_RELATIVE_PATH,
    PROBABILITY_FIT_MAX_SESSIONS,
    build_bounded_probability_fit_assessment,
    load_probability_fit_assessment,
    probability_fit_corpus_ready,
    publish_probability_fit_assessment,
)
from app.services.market_scan_probability_outcomes import (
    ProbabilityOutcomeError,
    ProbabilityOutcomeSemanticDriftError,
    build_probability_outcome_artifact,
    load_probability_outcome_artifact,
    probability_outcome_required_dates,
    publish_built_probability_outcome_artifact,
)
from app.services.market_scan_probability_source import (
    ProbabilitySourceError,
    load_probability_source_snapshot,
)
from app.services.trading_calendar import latest_expected_daily_kline_date, next_trade_dates
from app.utils.clock import market_now


PROBABILITY_OUTCOME_ARCHIVE_RELATIVE_PATH = "research/market_scan_probability_outcomes"
PROBABILITY_MAINTENANCE_CONTRACT_VERSION = "market-scan-probability-maintenance-v1"
_HORIZONS = (1, 5, 20)
_STABLE_READ_ATTEMPTS = 3
_MISSING_RETRY_MAX_ATTEMPTS = 5
_MISSING_RETRY_GRACE_SESSIONS = 5
_FileFingerprint = tuple[Path, int, int, int, int, int, int]
_DirectorySnapshot = tuple[tuple[int, int, int, int] | None, tuple[_FileFingerprint, ...]]


class ProbabilityMaintenanceCache(Protocol):
    """Minimal local cache boundary required by maintenance."""

    @property
    def path(self) -> Path: ...

    def get_klines_by_dates_many(
        self,
        symbols: Iterable[str],
        dates: Iterable[str],
        adjustment_mode: KlineAdjustmentMode = DEFAULT_DAILY_KLINE_ADJUSTMENT_MODE,
    ) -> dict[str, list[Kline]]: ...


@dataclass(frozen=True)
class ProbabilityMaintenanceSummary:
    source_count: int
    due_count: int
    published_count: int
    unchanged_count: int
    skipped_count: int
    failed_count: int
    fit_assessment_count: int
    fit_status: str
    as_of_date: str
    outcome_directory: str
    failures: tuple[str, ...] = ()

    @property
    def degraded(self) -> bool:
        return self.failed_count > 0

    def message(self) -> str:
        return (
            f"上涨概率标签维护 {self.as_of_date}：source {self.source_count} 个，"
            f"到期 {self.due_count} 个，新增 {self.published_count} 个，"
            f"无变化 {self.unchanged_count} 个，跳过 {self.skipped_count} 个，"
            f"失败 {self.failed_count} 个"
            f"，fit assessment {self.fit_assessment_count} 个（{self.fit_status}）"
        )


@dataclass(frozen=True)
class _SourceManifest:
    path: Path
    run_id: int
    quote_date: str
    as_of: str
    captured_at: str
    cohort: tuple[str, str, str]
    digest: str


@dataclass(frozen=True)
class _OutcomeManifest:
    path: Path
    run_id: int
    as_of_date: str
    generated_at: str
    digest: str
    source_digest: str
    horizon_quality: Mapping[str, Mapping[str, object]]
    state_digest: str


@dataclass(frozen=True)
class _OutcomeSemanticDriftManifest:
    path: Path
    run_id: int
    as_of_date: str
    generated_at: str
    digest: str
    source_digest: str


_OutcomeCatalogEntry = _OutcomeManifest | _OutcomeSemanticDriftManifest
_Manifest = TypeVar("_Manifest", _SourceManifest, _OutcomeCatalogEntry)


class MarketScanProbabilityMaintenanceService:
    """Mature only canonical point-in-time sources into compact outcomes."""

    def __init__(
        self,
        cache: ProbabilityMaintenanceCache,
        *,
        source_directory: str | Path | None = None,
        outcome_directory: str | Path | None = None,
    ) -> None:
        self.cache = cache
        data_directory = Path(cache.path).expanduser().absolute().parent
        self.source_directory = Path(
            source_directory or data_directory / "research" / "market_scan_probability_source"
        ).expanduser().absolute()
        self.outcome_directory = Path(
            outcome_directory or data_directory / PROBABILITY_OUTCOME_ARCHIVE_RELATIVE_PATH
        ).expanduser().absolute()
        self.fit_directory = data_directory / PROBABILITY_FIT_ASSESSMENT_RELATIVE_PATH
        self._lock = RLock()
        self._source_snapshot: _DirectorySnapshot | None = None
        self._outcome_snapshot: _DirectorySnapshot | None = None
        self._source_cache: dict[_FileFingerprint, _SourceManifest] = {}
        self._outcome_cache: dict[_FileFingerprint, _OutcomeCatalogEntry] = {}
        self._semantic_drift_by_run: dict[int, _OutcomeSemanticDriftManifest] = {}
        self._attempt_ledger: dict[str, dict[str, str]] = {}
        self._assessed_corpus_digests: dict[tuple[str, str, str], str] = {}
        self._fit_state_loaded = False

    def run(
        self,
        *,
        now: datetime | None = None,
        as_of_date: str | None = None,
    ) -> ProbabilityMaintenanceSummary:
        with self._lock:
            return self._run_locked(now=now, as_of_date=as_of_date)

    def _run_locked(
        self,
        *,
        now: datetime | None,
        as_of_date: str | None,
    ) -> ProbabilityMaintenanceSummary:
        current = now or market_now()
        effective_as_of = as_of_date or latest_expected_daily_kline_date(current).isoformat()
        sources = _canonical_sources(self._source_manifests())
        latest = _latest_outcomes(self._outcome_manifests())
        counts = {"due": 0, "published": 0, "unchanged": 0, "skipped": 0}
        failures: list[str] = []
        for source in sources:
            if _terminal_semantic_drift(
                source,
                latest.get(source.run_id),
                self._semantic_drift_by_run.get(source.run_id),
            ):
                counts["skipped"] += 1
                continue
            try:
                _maintain_source(
                    self,
                    source,
                    latest.get(source.run_id),
                    effective_as_of,
                    current,
                    counts,
                    self._attempt_ledger,
                )
            except Exception as exc:  # isolate one immutable source from the maintenance batch
                failures.append(f"run {source.run_id}: {_short_error(exc)}")
        fit_count, fit_status = self._maintain_fit(sources, latest, failures)
        return ProbabilityMaintenanceSummary(
            source_count=len(sources),
            due_count=counts["due"],
            published_count=counts["published"],
            unchanged_count=counts["unchanged"],
            skipped_count=counts["skipped"],
            failed_count=len(failures),
            fit_assessment_count=fit_count,
            fit_status=fit_status,
            as_of_date=effective_as_of,
            outcome_directory=str(self.outcome_directory),
            failures=tuple(failures[:5]),
        )

    def _maintain_fit(
        self,
        sources: Sequence[_SourceManifest],
        outcomes: Mapping[int, _OutcomeManifest],
        failures: list[str],
    ) -> tuple[int, str]:
        ready = _ready_fit_cohorts(sources, outcomes)
        if not ready:
            return 0, "threshold_pending"
        self._load_existing_fit_state()
        published = 0
        try:
            for complete in ready:
                published += self._maintain_fit_cohort(complete)
        except Exception as exc:
            failures.append(f"fit assessment: {_short_error(exc)}")
            return 0, "failed"
        return published, "projection_pending" if published else "unchanged"

    def _maintain_fit_cohort(
        self,
        pairs: Sequence[tuple[_SourceManifest, _OutcomeManifest]],
    ) -> int:
        complete = pairs[-PROBABILITY_FIT_MAX_SESSIONS:]
        corpus_digest = stable_probability_hash(
            [(source.digest, outcome.digest) for source, outcome in complete]
        )
        cohort = complete[0][0].cohort
        if corpus_digest == self._assessed_corpus_digests.get(cohort):
            return 0
        assessment = build_bounded_probability_fit_assessment(
            [source.path for source, _outcome in complete],
            [outcome.path for _source, outcome in complete],
            generated_at=max(outcome.generated_at for _source, outcome in complete),
        )
        _publish_with_optional_database(
            publish_probability_fit_assessment,
            self.fit_directory,
            assessment,
            database_path=self.cache.path,
        )
        self._assessed_corpus_digests[cohort] = corpus_digest
        return 1

    def _load_existing_fit_state(self) -> None:
        if self._fit_state_loaded:
            return
        snapshot = _directory_snapshot(
            self.fit_directory,
            "market-scan-probability-fit-through-run-*.json.gz",
        )
        for fingerprint in snapshot[1]:
            artifact = load_probability_fit_assessment(fingerprint[0])
            payload = _mapping(artifact["payload"], "fit.payload")
            cohort = _mapping(payload["cohort"], "fit.cohort")
            key = (str(cohort["mode"]), str(cohort["scope"]), str(cohort["rule_version"]))
            self._assessed_corpus_digests[key] = str(payload["input_pair_digest"])
        self._fit_state_loaded = True

    def _source_manifests(self) -> tuple[_SourceManifest, ...]:
        snapshot, cache = _stable_manifest_refresh(
            self.source_directory,
            "market-scan-probability-source-run-*.json.gz",
            self._source_snapshot,
            self._source_cache,
            _source_manifest,
        )
        self._source_snapshot, self._source_cache = snapshot, cache
        return tuple(cache.values())

    def _outcome_manifests(self) -> tuple[_OutcomeManifest, ...]:
        snapshot, cache = _stable_manifest_refresh(
            self.outcome_directory,
            "market-scan-probability-outcomes-run-*.json.gz",
            self._outcome_snapshot,
            self._outcome_cache,
            _outcome_catalog_entry,
        )
        self._outcome_snapshot, self._outcome_cache = snapshot, cache
        valid = tuple(item for item in cache.values() if isinstance(item, _OutcomeManifest))
        drifted = tuple(
            item for item in cache.values() if isinstance(item, _OutcomeSemanticDriftManifest)
        )
        self._semantic_drift_by_run = _latest_semantic_drifts(drifted)
        return valid


def _ready_fit_cohorts(
    sources: Sequence[_SourceManifest],
    outcomes: Mapping[int, _OutcomeManifest],
) -> list[list[tuple[_SourceManifest, _OutcomeManifest]]]:
    cohorts: dict[tuple[str, str, str], list[tuple[_SourceManifest, _OutcomeManifest]]] = {}
    for source in sources:
        outcome = outcomes.get(source.run_id)
        if outcome is not None:
            cohorts.setdefault(source.cohort, []).append((source, outcome))
    return [
        pairs
        for pairs in cohorts.values()
        if probability_fit_corpus_ready([outcome for _source, outcome in pairs])
    ]


def maintain_market_scan_probability(
    cache: ProbabilityMaintenanceCache,
    *,
    now: datetime | None = None,
    as_of_date: str | None = None,
    source_directory: str | Path | None = None,
    outcome_directory: str | Path | None = None,
) -> ProbabilityMaintenanceSummary:
    """Public synchronous entry point for scheduler, CLI, and deterministic tests."""
    return MarketScanProbabilityMaintenanceService(
        cache,
        source_directory=source_directory,
        outcome_directory=outcome_directory,
    ).run(now=now, as_of_date=as_of_date)


def _maintain_source(
    service: MarketScanProbabilityMaintenanceService,
    source: _SourceManifest,
    latest: _OutcomeManifest | None,
    as_of_date: str,
    now: datetime,
    counts: dict[str, int],
    ledger: dict[str, dict[str, str]],
) -> None:
    reason = _due_reason(source, latest, as_of_date)
    if reason is None:
        counts["skipped"] += 1
        return
    ledger_key = f"{source.run_id}:{source.digest}"
    previous_attempt = ledger.get(ledger_key)
    attempt_count = int((previous_attempt or {}).get("attempt_count", "0"))
    if reason == "retry" and attempt_count >= _MISSING_RETRY_MAX_ATTEMPTS:
        counts["skipped"] += 1
        return
    if (
        reason == "retry"
        and previous_attempt is not None
        and previous_attempt.get("as_of_date") == as_of_date
        and latest is not None
        and previous_attempt.get("state_digest") == latest.state_digest
    ):
        counts["skipped"] += 1
        return
    counts["due"] += 1
    source_artifact = load_probability_source_snapshot(source.path)
    needs_rows = reason != "initial" or _first_target_mature(source.quote_date, as_of_date)
    rows = _source_kline_rows(service.cache, source_artifact, as_of_date=as_of_date) if needs_rows else {}
    generated_at = now.isoformat()
    candidate = build_probability_outcome_artifact(
        source_artifact,
        rows,
        generated_at=generated_at,
        as_of_date=as_of_date,
    )
    candidate_state = _outcome_state_digest(candidate)
    ledger[ledger_key] = {
        "as_of_date": as_of_date,
        "state_digest": candidate_state,
        "attempt_count": str(attempt_count + 1 if reason == "retry" else 0),
    }
    if latest is not None and candidate_state == latest.state_digest:
        counts["unchanged"] += 1
        return
    _publish_with_optional_database(
        publish_built_probability_outcome_artifact,
        service.outcome_directory,
        candidate,
        database_path=service.cache.path,
    )
    counts["published"] += 1


def _publish_with_optional_database(
    publisher: Callable[..., object],
    directory: Path,
    artifact: Mapping[str, object],
    *,
    database_path: Path,
) -> object:
    if "database_path" in inspect.signature(publisher).parameters:
        return publisher(directory, artifact, database_path=database_path)
    return publisher(directory, artifact)


def _due_reason(
    source: _SourceManifest,
    latest: _OutcomeManifest | None,
    as_of_date: str,
) -> str | None:
    if as_of_date < source.quote_date:
        return None
    if latest is None:
        return "initial"
    if latest.source_digest != source.digest:
        raise ProbabilityOutcomeError(f"run {source.run_id} outcome/source digest 不一致")
    qualities = latest.horizon_quality
    if any(
        str(_mapping(qualities[str(horizon)], f"quality.{horizon}")["target_session_date"]) <= as_of_date
        and _mapping(qualities[str(horizon)], f"quality.{horizon}")["mature"] is not True
        for horizon in _HORIZONS
    ):
        return "maturity"
    if latest.as_of_date < as_of_date and any(
        _mature_missing_bars(_mapping(qualities[str(horizon)], f"quality.{horizon}"))
        for horizon in _HORIZONS
    ) and _retry_grace_open(qualities, as_of_date):
        return "retry"
    return None


def _retry_grace_open(
    qualities: Mapping[str, Mapping[str, object]],
    as_of_date: str,
) -> bool:
    missing_targets = [
        str(_mapping(qualities[str(horizon)], f"quality.{horizon}")["target_session_date"])
        for horizon in _HORIZONS
        if _mature_missing_bars(_mapping(qualities[str(horizon)], f"quality.{horizon}"))
    ]
    if not missing_targets:
        return False
    latest_target = max(missing_targets)
    grace = next_trade_dates(date.fromisoformat(latest_target), _MISSING_RETRY_GRACE_SESSIONS)
    return as_of_date <= grace[-1].isoformat()


def _mature_missing_bars(quality: Mapping[str, object]) -> bool:
    return quality["mature"] is True and int(cast(int, quality["data_unavailable_record_count"])) > 0


def _first_target_mature(quote_date: str, as_of_date: str) -> bool:
    fixed_sessions = next_trade_dates(date.fromisoformat(quote_date), max(_HORIZONS) + 1)
    return fixed_sessions[1].isoformat() <= as_of_date


def _source_kline_rows(
    cache: ProbabilityMaintenanceCache,
    source: Mapping[str, object],
    *,
    as_of_date: str,
) -> dict[str, list[Kline]]:
    payload = _mapping(source["payload"], "source.payload")
    records = _sequence(payload["records"], "source.records")
    symbols = tuple(str(_mapping(record, "source.records[]")["symbol"]) for record in records)
    requested_dates = probability_outcome_required_dates(
        source,
        as_of_date=as_of_date,
    )
    return cache.get_klines_by_dates_many(
        symbols,
        requested_dates,
        adjustment_mode="qfq",
    )


def _canonical_sources(manifests: Sequence[_SourceManifest]) -> tuple[_SourceManifest, ...]:
    newest_by_run: dict[int, _SourceManifest] = {}
    for item in manifests:
        previous = newest_by_run.get(item.run_id)
        if previous is None or _timestamp(item.captured_at) > _timestamp(previous.captured_at):
            newest_by_run[item.run_id] = item
    canonical: dict[tuple[str, str, str, str], _SourceManifest] = {}
    for item in newest_by_run.values():
        key = (*item.cohort, item.quote_date)
        previous = canonical.get(key)
        if previous is None or (_timestamp(item.as_of), item.run_id) > (_timestamp(previous.as_of), previous.run_id):
            canonical[key] = item
    return tuple(sorted(canonical.values(), key=lambda item: (item.quote_date, item.run_id)))


def _source_manifest(path: Path) -> _SourceManifest:
    artifact = load_probability_source_snapshot(path)
    payload = _mapping(artifact["payload"], "source.payload")
    run = _mapping(payload["run"], "source.run")
    cohort = _mapping(payload["cohort"], "source.cohort")
    integrity = _mapping(artifact["integrity"], "source.integrity")
    return _SourceManifest(
        path=path,
        run_id=int(cast(int, run["run_id"])),
        quote_date=str(run["quote_date"]),
        as_of=str(run["as_of"]),
        captured_at=str(artifact["captured_at"]),
        cohort=(str(cohort["mode"]), str(cohort["scope"]), str(cohort["rule_version"])),
        digest=str(integrity["integrity_digest"]),
    )


def _latest_outcomes(manifests: Sequence[_OutcomeManifest]) -> dict[int, _OutcomeManifest]:
    newest: dict[int, _OutcomeManifest] = {}
    for item in manifests:
        previous = newest.get(item.run_id)
        if previous is None or (item.as_of_date, _timestamp(item.generated_at)) > (
            previous.as_of_date,
            _timestamp(previous.generated_at),
        ):
            newest[item.run_id] = item
        elif previous is not None and (item.as_of_date, item.generated_at) == (
            previous.as_of_date,
            previous.generated_at,
        ) and item.digest != previous.digest:
            raise ProbabilityOutcomeError(f"run {item.run_id} 存在冲突 outcome artifacts")
    return newest


def _latest_semantic_drifts(
    manifests: Sequence[_OutcomeSemanticDriftManifest],
) -> dict[int, _OutcomeSemanticDriftManifest]:
    newest: dict[int, _OutcomeSemanticDriftManifest] = {}
    for item in manifests:
        previous = newest.get(item.run_id)
        if previous is None or _outcome_order(item) > _outcome_order(previous):
            newest[item.run_id] = item
        elif _outcome_order(item) == _outcome_order(previous) and item.digest != previous.digest:
            raise ProbabilityOutcomeError(
                f"run {item.run_id} 存在冲突 legacy semantic drift outcomes"
            )
    return newest


def _terminal_semantic_drift(
    source: _SourceManifest,
    valid: _OutcomeManifest | None,
    drifted: _OutcomeSemanticDriftManifest | None,
) -> bool:
    return bool(
        drifted is not None
        and drifted.source_digest == source.digest
        and (valid is None or _outcome_order(drifted) >= _outcome_order(valid))
    )


def _outcome_order(
    item: _OutcomeManifest | _OutcomeSemanticDriftManifest,
) -> tuple[str, float]:
    return item.as_of_date, _timestamp(item.generated_at)


def _outcome_catalog_entry(path: Path) -> _OutcomeCatalogEntry:
    try:
        return _outcome_manifest(path)
    except ProbabilityOutcomeSemanticDriftError as exc:
        identity = (
            exc.run_id,
            exc.as_of_date,
            exc.generated_at,
            exc.integrity_digest,
            exc.source_digest,
        )
        if any(value is None for value in identity):
            raise ProbabilityOutcomeError("legacy outcome semantic drift 缺少机械封存身份") from exc
        return _OutcomeSemanticDriftManifest(
            path=path,
            run_id=cast(int, exc.run_id),
            as_of_date=cast(str, exc.as_of_date),
            generated_at=cast(str, exc.generated_at),
            digest=cast(str, exc.integrity_digest),
            source_digest=cast(str, exc.source_digest),
        )


def _outcome_manifest(path: Path) -> _OutcomeManifest:
    artifact = load_probability_outcome_artifact(path)
    payload = _mapping(artifact["payload"], "outcome.payload")
    source = _mapping(payload["source"], "outcome.source")
    quality = _mapping(payload["quality"], "outcome.quality")
    integrity = _mapping(artifact["integrity"], "outcome.integrity")
    return _OutcomeManifest(
        path=path,
        run_id=int(cast(int, source["run_id"])),
        as_of_date=str(payload["as_of_date"]),
        generated_at=str(artifact["generated_at"]),
        digest=str(integrity["integrity_digest"]),
        source_digest=str(source["integrity_digest"]),
        horizon_quality=cast(Mapping[str, Mapping[str, object]], _mapping(quality["horizons"], "quality.horizons")),
        state_digest=_outcome_state_digest(artifact),
    )


def _outcome_state_digest(artifact: Mapping[str, object]) -> str:
    payload = dict(_mapping(artifact["payload"], "outcome.payload"))
    payload.pop("generated_at", None)
    payload.pop("as_of_date", None)
    return stable_probability_hash(payload)


def _directory_snapshot(directory: Path, pattern: str) -> _DirectorySnapshot:
    try:
        if not path_has_only_trusted_aliases(directory):
            raise ProbabilitySourceError(f"概率维护 archive 路径不是可信目录：{directory}")
        facts = directory.lstat()
    except FileNotFoundError:
        return None, ()
    if not stat.S_ISDIR(facts.st_mode):
        raise ProbabilitySourceError(f"概率维护 archive 路径不是目录：{directory}")
    identity = (facts.st_dev, facts.st_ino, facts.st_mtime_ns, facts.st_ctime_ns)
    fingerprints = tuple(_file_fingerprint(path) for path in sorted(directory.glob(pattern)))
    return identity, fingerprints


def _file_fingerprint(path: Path) -> _FileFingerprint:
    facts = path.lstat()
    if not stat.S_ISREG(facts.st_mode):
        raise ProbabilitySourceError(f"概率维护 artifact 必须是普通文件：{path}")
    return (
        path,
        facts.st_dev,
        facts.st_ino,
        facts.st_mode,
        facts.st_size,
        facts.st_mtime_ns,
        facts.st_ctime_ns,
    )


def _stable_manifest_refresh(
    directory: Path,
    pattern: str,
    previous: _DirectorySnapshot | None,
    cache: Mapping[_FileFingerprint, _Manifest],
    loader: Callable[[Path], _Manifest],
) -> tuple[_DirectorySnapshot, dict[_FileFingerprint, _Manifest]]:
    candidate = dict(cache)
    for _attempt in range(_STABLE_READ_ATTEMPTS):
        snapshot = _directory_snapshot(directory, pattern)
        if snapshot == previous:
            return snapshot, candidate
        refreshed: dict[_FileFingerprint, _Manifest] = {}
        for fingerprint in snapshot[1]:
            refreshed[fingerprint] = candidate.get(fingerprint) or loader(fingerprint[0])
        if _directory_snapshot(directory, pattern) == snapshot:
            return snapshot, refreshed
        candidate.update(refreshed)
    raise ProbabilitySourceError("概率维护 archive 目录在多次读取期间持续变化")


def _timestamp(value: str) -> float:
    parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    if parsed.tzinfo is None or parsed.utcoffset() is None:
        raise ProbabilityOutcomeError("概率维护 timestamp 必须包含时区")
    return parsed.timestamp()


def _mapping(value: object, path: str) -> Mapping[str, object]:
    if not isinstance(value, Mapping):
        raise ProbabilityOutcomeError(f"{path} 必须是 object")
    return cast(Mapping[str, object], value)


def _sequence(value: object, path: str) -> Sequence[object]:
    if not isinstance(value, Sequence) or isinstance(value, str | bytes):
        raise ProbabilityOutcomeError(f"{path} 必须是 array")
    return cast(Sequence[object], value)


def _short_error(exc: Exception) -> str:
    text = " ".join(str(exc).split()) or exc.__class__.__name__
    return text[:160]


__all__ = [
    "PROBABILITY_MAINTENANCE_CONTRACT_VERSION",
    "PROBABILITY_OUTCOME_ARCHIVE_RELATIVE_PATH",
    "MarketScanProbabilityMaintenanceService",
    "ProbabilityMaintenanceCache",
    "ProbabilityMaintenanceSummary",
    "maintain_market_scan_probability",
]
