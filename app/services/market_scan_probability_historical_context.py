"""Compact, non-authorizing projection of verified historical replay research.

The full historical replay artifact is intentionally expensive to verify.  An
offline builder performs that complete replay once, then publishes a small
content-addressed context artifact that binds the full file by size, SHA-256,
and its internal integrity digest.  Runtime reads remain fast and can never
grant live probability, ranking, or filtering authority.
"""

from __future__ import annotations

from collections.abc import Mapping
from copy import deepcopy
from datetime import datetime
import hashlib
import math
from pathlib import Path
import re
import stat
from threading import Lock, RLock
from typing import Final, cast

from app.artifacts.io import (
    ArtifactIOError,
    canonical_json_bytes,
    decode_json_bytes,
    exclusive_atomic_publish,
    path_has_only_trusted_aliases,
    read_regular_file,
    sha256_hex,
)
from app.services.market_scan_probability_replay import (
    HISTORICAL_REPLAY_ARTIFACT_SCHEMA_VERSION,
    HISTORICAL_REPLAY_COHORT_MODE,
    HISTORICAL_REPLAY_HORIZONS,
    HistoricalReplayError,
    verify_historical_replay_artifact,
)


HISTORICAL_CONTEXT_SCHEMA_VERSION: Final = "market-scan-probability-historical-context-v1"
HISTORICAL_CONTEXT_ARTIFACT_SCHEMA_VERSION: Final = "market-scan-probability-historical-context-artifact-v1"
HISTORICAL_CONTEXT_RELATIVE_PATH: Final = Path("research/market_scan_probability_replay")
HISTORICAL_CONTEXT_PREFIX: Final = "market-scan-probability-historical-context"
HISTORICAL_CONTEXT_MAX_BYTES: Final = 512 * 1024
HISTORICAL_REPLAY_MAX_BYTES: Final = 256 * 1024 * 1024
_INTEGRITY_NOTICE: Final = "sha256_integrity_not_signature_or_official_snapshot_attestation"
_CONTEXT_PATTERN = re.compile(rf"{HISTORICAL_CONTEXT_PREFIX}-([0-9a-f]{{64}})\.json")
_REPLAY_PATTERN = re.compile(r"market-scan-probability-historical-replay-\d{4}-\d{2}-\d{2}-" r"\d{4}-\d{2}-\d{2}-([0-9a-f]{64})\.json")
_SHA256_PATTERN = re.compile(r"[0-9a-f]{64}")
_HISTORICAL_ONLY_LIMITATIONS = (
    "historical_replay_not_live_probability_contract",
    "historical_replay_not_filter_authority",
    "historical_replay_target_is_absolute_net_return_positive",
    "historical_replay_contract_is_superseded",
)


class HistoricalProbabilityContextError(ValueError):
    """Historical context is malformed, unbound, or changed."""


_FileFingerprint = tuple[Path, int, int, int, int, int, int]
_DirectoryIdentity = tuple[int, int, int, int]
_DirectorySnapshot = tuple[_DirectoryIdentity | None, tuple[_FileFingerprint, ...]]


class MarketScanHistoricalProbabilityContextStore:
    """Read one compact historical context without weakening live evidence."""

    def __init__(self, directory: Path) -> None:
        self.directory = directory.expanduser().absolute()
        self._lock = RLock()
        self._refresh_lock = Lock()
        self._preload_pending = False
        self._preload_leases = 0
        self._snapshot: _DirectorySnapshot | None = None
        self._projection: dict[str, object] | None = None

    def preload(self) -> int:
        try:
            self._refresh_if_changed(blocking=True)
            with self._lock:
                return int(self._projection is not None)
        finally:
            self.clear_preload_pending()

    def research_projection(self) -> dict[str, object]:
        if not self.refresh_pending():
            self._refresh_if_changed(blocking=False)
        with self._lock:
            if self._projection is None:
                return not_generated_historical_probability_context()
            return deepcopy(self._projection)

    def mark_preload_pending(self) -> None:
        """Keep interactive reads non-blocking before a worker acquires the lock."""
        with self._lock:
            self._preload_pending = True

    def clear_preload_pending(self) -> None:
        with self._lock:
            self._preload_pending = False

    def acquire_preload_lease(self) -> None:
        """A coordinator owns this independently of a single preload attempt."""
        with self._lock:
            self._preload_leases += 1

    def release_preload_lease(self) -> None:
        with self._lock:
            if self._preload_leases <= 0:
                raise RuntimeError("历史概率 context preload lease 未持有")
            self._preload_leases -= 1

    def refresh_pending(self) -> bool:
        with self._lock:
            scheduled = self._preload_pending or self._preload_leases > 0
        return scheduled or self._refresh_lock.locked()

    def _refresh_if_changed(self, *, blocking: bool) -> None:
        observed = _directory_snapshot(self.directory)
        with self._lock:
            if observed == self._snapshot:
                return
        acquired = self._refresh_lock.acquire(blocking=blocking)
        if not acquired:
            # Readers keep using the previous complete projection while the sole
            # refresher verifies the bound full-replay files outside the state lock.
            return
        try:
            snapshot = _directory_snapshot(self.directory)
            with self._lock:
                if snapshot == self._snapshot:
                    return
            projection = _load_newest_projection(self.directory, snapshot)
            if _directory_snapshot(self.directory) != snapshot:
                raise HistoricalProbabilityContextError("历史概率研究目录在读取期间发生变化，请重试")
            with self._lock:
                self._projection = projection
                self._snapshot = snapshot
        finally:
            self._refresh_lock.release()


def build_historical_probability_context(
    replay_artifact_path: str | Path,
) -> dict[str, object]:
    """Deep-verify a replay and derive its compact runtime context."""

    source = Path(replay_artifact_path).expanduser().absolute()
    try:
        encoded = read_regular_file(source, max_bytes=HISTORICAL_REPLAY_MAX_BYTES)
        decoded = decode_json_bytes(encoded)
        if not isinstance(decoded, Mapping):
            raise HistoricalProbabilityContextError("历史概率完整重放产物必须是 object")
        replay = verify_historical_replay_artifact(decoded)
    except (ArtifactIOError, HistoricalReplayError) as exc:
        raise HistoricalProbabilityContextError("历史概率完整重放产物校验失败") from exc
    return _build_context_from_verified_replay(
        replay,
        source_filename=source.name,
        source_bytes=len(encoded),
        source_sha256=hashlib.sha256(encoded).hexdigest(),
    )


def publish_historical_probability_context(
    replay_artifact_path: str | Path,
    output_directory: str | Path,
) -> Path:
    """Publish the compact context beside its immutable full replay."""

    source = Path(replay_artifact_path).expanduser().absolute()
    directory = Path(output_directory).expanduser().absolute()
    if source.parent != directory:
        raise HistoricalProbabilityContextError("历史概率上下文必须与完整重放产物发布在同一目录")
    context = build_historical_probability_context(source)
    digest = str(cast(Mapping[str, object], context["integrity"])["integrity_digest"])
    target = directory / f"{HISTORICAL_CONTEXT_PREFIX}-{digest}.json"
    try:
        exclusive_atomic_publish(
            target,
            canonical_json_bytes(context),
            max_bytes=HISTORICAL_CONTEXT_MAX_BYTES,
        )
    except ArtifactIOError as exc:
        raise HistoricalProbabilityContextError("历史概率上下文发布失败") from exc
    return target


def verify_historical_probability_context(
    artifact: Mapping[str, object],
) -> dict[str, object]:
    normalized = _json_copy(artifact)
    if set(normalized) != {"schema_version", "generated_at", "payload", "integrity"}:
        raise HistoricalProbabilityContextError("历史概率上下文字段无效")
    if normalized["schema_version"] != HISTORICAL_CONTEXT_ARTIFACT_SCHEMA_VERSION:
        raise HistoricalProbabilityContextError("历史概率上下文版本不受支持")
    payload = _mapping(normalized["payload"], "payload")
    integrity = _mapping(normalized["integrity"], "integrity")
    if set(integrity) != {"algorithm", "integrity_digest", "notice"}:
        raise HistoricalProbabilityContextError("历史概率上下文摘要字段无效")
    if integrity.get("algorithm") != "sha256" or integrity.get("notice") != _INTEGRITY_NOTICE:
        raise HistoricalProbabilityContextError("历史概率上下文摘要合同无效")
    expected = sha256_hex(
        canonical_json_bytes(
            {
                "schema_version": normalized["schema_version"],
                "generated_at": normalized["generated_at"],
                "payload": payload,
            }
        )
    )
    if integrity.get("integrity_digest") != expected:
        raise HistoricalProbabilityContextError("历史概率上下文摘要不一致")
    _validate_payload(payload, generated_at=normalized["generated_at"])
    return normalized


def historical_probability_context_filename(artifact: Mapping[str, object]) -> str:
    verified = verify_historical_probability_context(artifact)
    digest = str(_mapping(verified["integrity"], "integrity")["integrity_digest"])
    return f"{HISTORICAL_CONTEXT_PREFIX}-{digest}.json"


def not_generated_historical_probability_context() -> dict[str, object]:
    return {
        "schema_version": HISTORICAL_CONTEXT_SCHEMA_VERSION,
        "status": "not_generated",
        "availability": "historical_context_not_generated",
        "generated_at": None,
        "target": "net_return_positive",
        "cohort": None,
        "sample": None,
        "horizons": {},
        "source_artifact": None,
        "production_ranking_effect": "none",
        "selection_qualified": False,
        "filter_qualified": False,
        "limitations": list(_HISTORICAL_ONLY_LIMITATIONS),
    }


def unavailable_historical_probability_context() -> dict[str, object]:
    context = not_generated_historical_probability_context()
    context["status"] = "unavailable"
    context["availability"] = "historical_context_integrity_unavailable"
    return context


def _build_context_from_verified_replay(
    replay: Mapping[str, object],
    *,
    source_filename: str,
    source_bytes: int,
    source_sha256: str,
) -> dict[str, object]:
    payload = _mapping(replay.get("payload"), "replay.payload")
    integrity = _mapping(replay.get("integrity"), "replay.integrity")
    quality = _mapping(payload.get("quality"), "replay.quality")
    fit = _mapping(payload.get("probability_fit"), "replay.probability_fit")
    fit_horizons = _mapping(fit.get("horizons"), "replay.probability_fit.horizons")
    quality_horizons = _mapping(quality.get("horizons"), "replay.quality.horizons")
    config = _mapping(payload.get("config"), "replay.config")
    horizons = _historical_horizon_projections(fit_horizons, quality_horizons)
    limitations = [
        *[str(item) for item in _sequence(payload.get("limitations"), "replay.limitations")],
        *_HISTORICAL_ONLY_LIMITATIONS,
    ]
    generated_at = _required_text(payload.get("generated_at"), "replay.generated_at")
    compact_payload: dict[str, object] = {
        "schema_version": HISTORICAL_CONTEXT_SCHEMA_VERSION,
        "status": "ready",
        "availability": _historical_availability(horizons),
        "generated_at": generated_at,
        "target": "net_return_positive",
        "cohort": deepcopy(_mapping(payload.get("cohort"), "replay.cohort")),
        "sample": _historical_sample_projection(config, quality, quality_horizons),
        "horizons": horizons,
        "source_artifact": _historical_source_projection(
            replay,
            integrity,
            filename=source_filename,
            source_bytes=source_bytes,
            source_sha256=source_sha256,
        ),
        "production_ranking_effect": "none",
        "selection_qualified": False,
        "filter_qualified": False,
        "limitations": list(dict.fromkeys(limitations)),
    }
    artifact: dict[str, object] = {
        "schema_version": HISTORICAL_CONTEXT_ARTIFACT_SCHEMA_VERSION,
        "generated_at": generated_at,
        "payload": compact_payload,
    }
    artifact["integrity"] = {
        "algorithm": "sha256",
        "integrity_digest": sha256_hex(canonical_json_bytes(artifact)),
        "notice": _INTEGRITY_NOTICE,
    }
    return verify_historical_probability_context(artifact)


def _historical_horizon_projections(
    fit_horizons: Mapping[str, object],
    quality_horizons: Mapping[str, object],
) -> dict[str, object]:
    return {
        str(horizon): _historical_horizon_projection(
            horizon,
            _mapping(fit_horizons.get(str(horizon)), f"fit.horizons.{horizon}"),
            _mapping(quality_horizons.get(str(horizon)), f"quality.horizons.{horizon}"),
        )
        for horizon in HISTORICAL_REPLAY_HORIZONS
    }


def _historical_sample_projection(
    config: Mapping[str, object],
    quality: Mapping[str, object],
    quality_horizons: Mapping[str, object],
) -> dict[str, object]:
    coverage = min(
        _probability(
            _mapping(quality_horizons[str(horizon)], "quality horizon").get("label_coverage"),
            "quality.label_coverage",
        )
        for horizon in HISTORICAL_REPLAY_HORIZONS
    )
    return {
        "start_date": _required_text(config.get("start_date"), "config.start_date"),
        "end_date": _required_text(config.get("end_date"), "config.end_date"),
        "independent_session_count": _nonnegative_int(
            quality.get("record_independent_session_count"),
            "quality.record_independent_session_count",
        ),
        "record_count": _nonnegative_int(quality.get("record_count"), "quality.record_count"),
        "symbol_count": _positive_int(quality.get("selected_symbol_count"), "quality.selected_symbol_count"),
        "label_coverage": coverage,
    }


def _historical_source_projection(
    replay: Mapping[str, object],
    integrity: Mapping[str, object],
    *,
    filename: str,
    source_bytes: int,
    source_sha256: str,
) -> dict[str, object]:
    return {
        "schema_version": replay.get("schema_version"),
        "filename": filename,
        "bytes": source_bytes,
        "sha256": source_sha256,
        "integrity_digest": integrity.get("integrity_digest"),
        "full_replay_verified": True,
    }


def _historical_horizon_projection(
    horizon: int,
    fit: Mapping[str, object],
    quality: Mapping[str, object],
) -> dict[str, object]:
    counts = _mapping(fit.get("counts"), f"fit.horizons.{horizon}.counts")
    metrics_parent = _optional_mapping(fit.get("calibration_metrics"), f"fit.horizons.{horizon}.calibration_metrics")
    metrics = _optional_mapping(
        metrics_parent.get("calibrated"),
        f"fit.horizons.{horizon}.calibration_metrics.calibrated",
    )
    return {
        "horizon": horizon,
        "assessment_status": _required_text(fit.get("status"), "fit.status"),
        "probability": None,
        "base_rate": _optional_probability(fit.get("base_rate"), "fit.base_rate"),
        "available_independent_session_count": _nonnegative_int(
            counts.get("available_independent_session_count"),
            "fit.available_independent_session_count",
        ),
        "minimum_required_independent_session_count": _positive_int(
            quality.get("minimum_required_independent_session_count"),
            "quality.minimum_required_independent_session_count",
        ),
        "out_of_sample_session_count": _nonnegative_int(counts.get("out_of_sample_session_count"), "fit.out_of_sample_session_count"),
        "evaluated_fold_count": _nonnegative_int(counts.get("evaluated_fold_count"), "fit.evaluated_fold_count"),
        "observation_count": _nonnegative_int(counts.get("observation_count"), "fit.observation_count"),
        "auc": _optional_probability(metrics.get("auc"), "metrics.auc"),
        "brier_score": _optional_finite_number(metrics.get("brier_score"), "metrics.brier_score"),
        "brier_skill_score": _optional_finite_number(metrics.get("brier_skill_score"), "metrics.brier_skill_score"),
        "ece": _optional_probability(metrics.get("ece"), "metrics.ece"),
        "bin_monotonic": _optional_boolean(metrics.get("bin_monotonic"), "metrics.bin_monotonic"),
        "highest_bin_above_base_rate": _optional_boolean(
            metrics.get("highest_bin_above_base_rate"),
            "metrics.highest_bin_above_base_rate",
        ),
        "training_cutoff": _optional_text(fit.get("training_cutoff"), "fit.training_cutoff"),
        "limitations": [str(item) for item in _sequence(fit.get("limitations"), "fit.limitations")],
    }


def _historical_availability(horizons: Mapping[str, object]) -> str:
    summaries = [_mapping(horizons[str(horizon)], f"horizons.{horizon}") for horizon in HISTORICAL_REPLAY_HORIZONS]
    if all(
        summary.get("assessment_status") == "calibrated_shadow"
        and isinstance(summary.get("brier_skill_score"), float)
        and cast(float, summary["brier_skill_score"]) > 0
        and summary.get("highest_bin_above_base_rate") is True
        and isinstance(summary.get("evaluated_fold_count"), int)
        and cast(int, summary["evaluated_fold_count"]) >= 2
        for summary in summaries
    ):
        return "historical_shadow_calibrated_reference_only"
    if any(_historical_horizon_has_no_skill(summary) for summary in summaries):
        return "historical_replay_no_verified_predictive_skill"
    return "historical_replay_insufficient_evidence"


def _historical_horizon_has_no_skill(summary: Mapping[str, object]) -> bool:
    brier_skill = summary.get("brier_skill_score")
    auc = summary.get("auc")
    return (isinstance(brier_skill, float) and brier_skill <= 0) or (isinstance(auc, float) and auc <= 0.5)


def _load_newest_projection(
    directory: Path,
    snapshot: _DirectorySnapshot,
) -> dict[str, object] | None:
    candidates = [item for item in snapshot[1] if _CONTEXT_PATTERN.fullmatch(item[0].name)]
    if not candidates:
        return None
    loaded: list[tuple[tuple[str, str, str], dict[str, object]]] = []
    for fingerprint in candidates:
        artifact = _load_context_file(fingerprint[0])
        digest = str(_mapping(artifact["integrity"], "integrity")["integrity_digest"])
        match = _CONTEXT_PATTERN.fullmatch(fingerprint[0].name)
        if match is None or match.group(1) != digest:
            raise HistoricalProbabilityContextError("历史概率上下文文件名与摘要不一致")
        payload = _mapping(artifact["payload"], "payload")
        _verify_bound_source(directory, _mapping(payload["source_artifact"], "source_artifact"))
        sample = _mapping(payload["sample"], "sample")
        loaded.append(
            (
                (
                    str(sample["end_date"]),
                    str(payload["generated_at"]),
                    digest,
                ),
                deepcopy(dict(payload)),
            )
        )
    return max(loaded, key=lambda item: item[0])[1]


def _load_context_file(path: Path) -> dict[str, object]:
    try:
        decoded = decode_json_bytes(read_regular_file(path, max_bytes=HISTORICAL_CONTEXT_MAX_BYTES))
    except ArtifactIOError as exc:
        raise HistoricalProbabilityContextError("历史概率上下文读取失败") from exc
    if not isinstance(decoded, Mapping):
        raise HistoricalProbabilityContextError("历史概率上下文必须是 object")
    return verify_historical_probability_context(decoded)


def _verify_bound_source(directory: Path, source: Mapping[str, object]) -> None:
    filename = _validate_source_filename_binding(source)
    target = directory / filename
    expected_bytes = _positive_int(source.get("bytes"), "source_artifact.bytes")
    try:
        encoded = read_regular_file(target, max_bytes=HISTORICAL_REPLAY_MAX_BYTES)
    except ArtifactIOError as exc:
        raise HistoricalProbabilityContextError("历史概率完整重放源不可用") from exc
    if len(encoded) != expected_bytes:
        raise HistoricalProbabilityContextError("历史概率完整重放源大小不一致")
    if hashlib.sha256(encoded).hexdigest() != _required_digest(source.get("sha256"), "source_artifact.sha256"):
        raise HistoricalProbabilityContextError("历史概率完整重放源文件摘要不一致")


def _validate_source_filename_binding(source: Mapping[str, object]) -> str:
    filename = _required_text(source.get("filename"), "source_artifact.filename")
    if Path(filename).name != filename:
        raise HistoricalProbabilityContextError("历史概率源文件名无效")
    match = _REPLAY_PATTERN.fullmatch(filename)
    digest = _required_digest(source.get("integrity_digest"), "source artifact digest")
    if match is None or match.group(1) != digest:
        raise HistoricalProbabilityContextError("历史概率源文件名与摘要不一致")
    return filename


def _validate_payload(payload: Mapping[str, object], *, generated_at: object) -> None:
    expected = {
        "schema_version",
        "status",
        "availability",
        "generated_at",
        "target",
        "cohort",
        "sample",
        "horizons",
        "source_artifact",
        "production_ranking_effect",
        "selection_qualified",
        "filter_qualified",
        "limitations",
    }
    if set(payload) != expected:
        raise HistoricalProbabilityContextError("历史概率上下文 payload 字段无效")
    if payload.get("schema_version") != HISTORICAL_CONTEXT_SCHEMA_VERSION:
        raise HistoricalProbabilityContextError("历史概率上下文 payload 版本无效")
    if payload.get("status") != "ready" or payload.get("generated_at") != generated_at:
        raise HistoricalProbabilityContextError("历史概率上下文状态或时间无效")
    _aware_datetime(_required_text(generated_at, "generated_at"))
    _validate_research_boundary(payload)
    _validate_context_cohort(payload.get("cohort"))
    _validate_context_sample(payload.get("sample"))
    _validate_context_horizons(payload.get("horizons"))
    _validate_context_source(payload.get("source_artifact"))
    _validate_context_limitations(payload.get("limitations"))


def _validate_research_boundary(payload: Mapping[str, object]) -> None:
    if payload.get("availability") not in {
        "historical_shadow_calibrated_reference_only",
        "historical_replay_no_verified_predictive_skill",
        "historical_replay_insufficient_evidence",
    }:
        raise HistoricalProbabilityContextError("历史概率上下文可用状态无效")
    if (
        payload.get("target") != "net_return_positive"
        or payload.get("production_ranking_effect") != "none"
        or payload.get("selection_qualified") is not False
        or payload.get("filter_qualified") is not False
    ):
        raise HistoricalProbabilityContextError("历史概率上下文越过研究边界")


def _validate_context_cohort(value: object) -> None:
    cohort = _mapping(value, "cohort")
    if cohort.get("mode") != HISTORICAL_REPLAY_COHORT_MODE or cohort.get("official") is not False or cohort.get("live_cohort_compatible") is not False:
        raise HistoricalProbabilityContextError("历史概率 cohort 边界无效")


def _validate_context_sample(value: object) -> None:
    sample = _mapping(value, "sample")
    if set(sample) != {
        "start_date",
        "end_date",
        "independent_session_count",
        "record_count",
        "symbol_count",
        "label_coverage",
    }:
        raise HistoricalProbabilityContextError("历史概率样本摘要字段无效")
    _required_text(sample.get("start_date"), "sample.start_date")
    _required_text(sample.get("end_date"), "sample.end_date")
    _nonnegative_int(sample.get("independent_session_count"), "sample sessions")
    _nonnegative_int(sample.get("record_count"), "sample records")
    _positive_int(sample.get("symbol_count"), "sample symbols")
    _probability(sample.get("label_coverage"), "sample label coverage")


def _validate_context_horizons(value: object) -> None:
    horizons = _mapping(value, "horizons")
    if set(horizons) != {str(item) for item in HISTORICAL_REPLAY_HORIZONS}:
        raise HistoricalProbabilityContextError("历史概率周期字段无效")
    for horizon in HISTORICAL_REPLAY_HORIZONS:
        _validate_horizon(_mapping(horizons[str(horizon)], f"horizons.{horizon}"), horizon)


def _validate_context_source(value: object) -> None:
    source = _mapping(value, "source_artifact")
    if set(source) != {
        "schema_version",
        "filename",
        "bytes",
        "sha256",
        "integrity_digest",
        "full_replay_verified",
    }:
        raise HistoricalProbabilityContextError("历史概率源摘要字段无效")
    if source.get("schema_version") != HISTORICAL_REPLAY_ARTIFACT_SCHEMA_VERSION or source.get("full_replay_verified") is not True:
        raise HistoricalProbabilityContextError("历史概率完整重放验证状态无效")
    _positive_int(source.get("bytes"), "source bytes")
    _required_digest(source.get("sha256"), "source sha256")
    _validate_source_filename_binding(source)


def _validate_context_limitations(value: object) -> None:
    limitations = _sequence(value, "limitations")
    if not all(isinstance(item, str) and item for item in limitations):
        raise HistoricalProbabilityContextError("历史概率局限字段无效")
    if not set(_HISTORICAL_ONLY_LIMITATIONS).issubset(set(limitations)):
        raise HistoricalProbabilityContextError("历史概率研究边界声明不完整")


def _validate_horizon(value: Mapping[str, object], horizon: int) -> None:
    expected = {
        "horizon",
        "assessment_status",
        "probability",
        "base_rate",
        "available_independent_session_count",
        "minimum_required_independent_session_count",
        "out_of_sample_session_count",
        "evaluated_fold_count",
        "observation_count",
        "auc",
        "brier_score",
        "brier_skill_score",
        "ece",
        "bin_monotonic",
        "highest_bin_above_base_rate",
        "training_cutoff",
        "limitations",
    }
    if set(value) != expected or value.get("horizon") != horizon:
        raise HistoricalProbabilityContextError("历史概率周期摘要字段无效")
    if value.get("assessment_status") not in {"insufficient_data", "calibrated_shadow"}:
        raise HistoricalProbabilityContextError("历史概率周期评估状态无效")
    if value.get("probability") is not None:
        raise HistoricalProbabilityContextError("历史概率上下文不能发布逐股概率")
    _optional_probability(value.get("base_rate"), "horizon base rate")
    _nonnegative_int(value.get("available_independent_session_count"), "available sessions")
    _positive_int(value.get("minimum_required_independent_session_count"), "minimum sessions")
    _nonnegative_int(value.get("out_of_sample_session_count"), "oos sessions")
    _nonnegative_int(value.get("evaluated_fold_count"), "fold count")
    _nonnegative_int(value.get("observation_count"), "observation count")
    _optional_probability(value.get("auc"), "auc")
    _optional_finite_number(value.get("brier_score"), "brier score")
    _optional_finite_number(value.get("brier_skill_score"), "brier skill")
    _optional_probability(value.get("ece"), "ece")
    _optional_boolean(value.get("bin_monotonic"), "bin monotonic")
    _optional_boolean(value.get("highest_bin_above_base_rate"), "highest bin")
    _optional_text(value.get("training_cutoff"), "training cutoff")
    if not all(isinstance(item, str) and item for item in _sequence(value.get("limitations"), "limitations")):
        raise HistoricalProbabilityContextError("历史概率周期局限字段无效")


def _directory_snapshot(directory: Path) -> _DirectorySnapshot:
    if not directory.exists():
        return None, ()
    try:
        if not path_has_only_trusted_aliases(directory):
            raise HistoricalProbabilityContextError("历史概率研究目录不能是路径别名")
        facts = directory.lstat()
        if not stat.S_ISDIR(facts.st_mode) or stat.S_ISLNK(facts.st_mode):
            raise HistoricalProbabilityContextError("历史概率研究目录无效")
        identity = (facts.st_dev, facts.st_ino, facts.st_mtime_ns, facts.st_ctime_ns)
        files: list[_FileFingerprint] = []
        for candidate in directory.iterdir():
            if not (_CONTEXT_PATTERN.fullmatch(candidate.name) or _REPLAY_PATTERN.fullmatch(candidate.name)):
                continue
            item = candidate.lstat()
            if not stat.S_ISREG(item.st_mode) or stat.S_ISLNK(item.st_mode) or item.st_nlink != 1:
                raise HistoricalProbabilityContextError("历史概率研究文件必须是单链接普通文件")
            files.append(
                (
                    candidate.absolute(),
                    item.st_dev,
                    item.st_ino,
                    item.st_size,
                    item.st_mtime_ns,
                    item.st_ctime_ns,
                    item.st_nlink,
                )
            )
        return identity, tuple(sorted(files, key=lambda item: str(item[0])))
    except HistoricalProbabilityContextError:
        raise
    except OSError as exc:
        raise HistoricalProbabilityContextError("历史概率研究目录读取失败") from exc


def _mapping(value: object, label: str) -> dict[str, object]:
    if not isinstance(value, Mapping):
        raise HistoricalProbabilityContextError(f"{label} 必须是 object")
    return {str(key): item for key, item in value.items()}


def _optional_mapping(value: object, label: str) -> dict[str, object]:
    return {} if value is None else _mapping(value, label)


def _sequence(value: object, label: str) -> list[object]:
    if not isinstance(value, list):
        raise HistoricalProbabilityContextError(f"{label} 必须是 array")
    return value


def _required_text(value: object, label: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise HistoricalProbabilityContextError(f"{label} 必须是非空文本")
    return value


def _optional_text(value: object, label: str) -> str | None:
    return None if value is None else _required_text(value, label)


def _required_digest(value: object, label: str) -> str:
    text = _required_text(value, label)
    if _SHA256_PATTERN.fullmatch(text) is None:
        raise HistoricalProbabilityContextError(f"{label} 必须是 SHA-256")
    return text


def _positive_int(value: object, label: str) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or value <= 0:
        raise HistoricalProbabilityContextError(f"{label} 必须是正整数")
    return value


def _nonnegative_int(value: object, label: str) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or value < 0:
        raise HistoricalProbabilityContextError(f"{label} 必须是非负整数")
    return value


def _finite_number(value: object, label: str) -> float:
    if isinstance(value, bool) or not isinstance(value, int | float):
        raise HistoricalProbabilityContextError(f"{label} 必须是有限数")
    number = float(value)
    if not math.isfinite(number):
        raise HistoricalProbabilityContextError(f"{label} 必须是有限数")
    return number


def _optional_finite_number(value: object, label: str) -> float | None:
    return None if value is None else _finite_number(value, label)


def _probability(value: object, label: str) -> float:
    number = _finite_number(value, label)
    if not 0 <= number <= 1:
        raise HistoricalProbabilityContextError(f"{label} 必须在 0 到 1 之间")
    return number


def _optional_probability(value: object, label: str) -> float | None:
    return None if value is None else _probability(value, label)


def _boolean(value: object, label: str) -> bool:
    if not isinstance(value, bool):
        raise HistoricalProbabilityContextError(f"{label} 必须是 boolean")
    return value


def _optional_boolean(value: object, label: str) -> bool | None:
    return None if value is None else _boolean(value, label)


def _aware_datetime(value: str) -> datetime:
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError as exc:
        raise HistoricalProbabilityContextError("历史概率上下文时间无效") from exc
    if parsed.tzinfo is None or parsed.utcoffset() is None:
        raise HistoricalProbabilityContextError("历史概率上下文时间必须带时区")
    return parsed


def _json_copy(value: Mapping[str, object]) -> dict[str, object]:
    try:
        decoded = decode_json_bytes(canonical_json_bytes(dict(value)))
    except ArtifactIOError as exc:
        raise HistoricalProbabilityContextError("历史概率上下文不是有限 JSON") from exc
    if not isinstance(decoded, dict):
        raise HistoricalProbabilityContextError("历史概率上下文必须是 object")
    return cast(dict[str, object], decoded)


__all__ = [
    "HISTORICAL_CONTEXT_ARTIFACT_SCHEMA_VERSION",
    "HISTORICAL_CONTEXT_RELATIVE_PATH",
    "HISTORICAL_CONTEXT_SCHEMA_VERSION",
    "HistoricalProbabilityContextError",
    "MarketScanHistoricalProbabilityContextStore",
    "build_historical_probability_context",
    "historical_probability_context_filename",
    "not_generated_historical_probability_context",
    "publish_historical_probability_context",
    "unavailable_historical_probability_context",
    "verify_historical_probability_context",
]
