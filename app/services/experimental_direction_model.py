"""Offline D+1/D+2/D+5 close-direction models from verified static history.

These labels compare qfq closes, not entry/exit profitability. No H5 outcome,
calibrator, OOS result, runtime database or production authority is modified.
"""

from __future__ import annotations

from collections import Counter, defaultdict
from collections.abc import Mapping, Sequence
import hashlib
import math
from pathlib import Path
import sqlite3
from typing import Any, Literal, cast

from app.artifacts.io import canonical_json_bytes, exclusive_atomic_publish, path_has_only_trusted_aliases, read_regular_file, sha256_hex
from app.db.market_mappers import row_to_kline
from app.models.market import Kline
from app.services.experimental_probability_model import (
    DIRECTION_SCHEMA, MODEL_DIRECTORY, MODEL_MAX_BYTES, DirectionEvidence, ExperimentalEstimator, ExperimentalProbabilityUnavailable,
    fit_experimental_sample_parameters,
)
from app.services.market_scan_probability import ProbabilitySample
from app.services.market_scan_probability_history import load_market_scan_probability_history_manifest, trusted_probability_history_dates
from app.services.market_scan_probability_replay import HISTORICAL_REPLAY_FEATURE_NAMES, OHLCVBar, historical_replay_feature_values


def _static_database_digest(database: Path) -> str:
    if not path_has_only_trusted_aliases(database) or ".workbuddy-ai" in database.parts:
        raise ExperimentalProbabilityUnavailable("方向模型历史路径无效")
    if any(Path(str(database) + suffix).exists() or Path(str(database) + suffix).is_symlink()
           for suffix in ("-wal", "-shm", "-journal")):
        raise ExperimentalProbabilityUnavailable("方向模型仅接受无 sidecar 的静态历史库")
    return sha256_hex(read_regular_file(database, max_bytes=256 * 1024 * 1024))


def _read_history(database: Path) -> dict[str, list[Kline]]:
    series: dict[str, list[Kline]] = defaultdict(list)
    connection = sqlite3.connect(database.absolute().as_uri() + "?mode=ro&immutable=1", uri=True)
    connection.row_factory = sqlite3.Row
    try:
        connection.execute("PRAGMA query_only=ON")
        connection.execute("BEGIN")
        for row in connection.execute("SELECT * FROM kline_daily ORDER BY symbol,date"):
            series[row["symbol"]].append(row_to_kline(row))
    finally:
        connection.close()
    return dict(series)


def _direction_observation(window: Sequence[Kline], future: Kline | None) -> tuple[tuple[float, ...] | None, str]:
    if len(window) != 21:
        return None, "missing_signal_window"
    signal = window[-1]
    if signal.volume <= 0:
        return None, "signal_no_volume"
    if future is None:
        return None, "missing_fixed_target"
    if not math.isfinite(future.volume) or future.volume <= 0:
        return None, "target_no_volume"
    if not math.isfinite(future.close) or future.close <= 0:
        return None, "invalid_target_close"
    if (signal.adjustment_mode, signal.data_version, signal.contract_version) != (future.adjustment_mode, future.data_version, future.contract_version):
        return None, "target_contract_mismatch"
    try:
        return historical_replay_feature_values(cast(Sequence[OHLCVBar], window), signal_date=signal.date), ""
    except (ValueError, TypeError):
        return None, "invalid_signal_window"


def direction_samples(series: Mapping[str, Sequence[Kline]], sessions: Sequence[str], *, offset: int) -> tuple[
    list[ProbabilitySample], dict[str, str], list[str], dict[str, Any],
]:
    """Bind labels to the calendar, never to the next available stock bar."""
    if type(offset) is not int or offset not in {1, 2, 5} or len(series) * len(sessions) > 100_000:
        raise ExperimentalProbabilityUnavailable("方向模型周期或样本范围无效")
    if list(sessions) != sorted(set(sessions)):
        raise ExperimentalProbabilityUnavailable("方向模型交易日历重复或乱序")
    samples: list[ProbabilitySample] = []
    targets: dict[str, str] = {}
    symbols: set[str] = set()
    excluded: Counter[str] = Counter()
    digest = hashlib.sha256()
    for symbol, rows in sorted(series.items()):
        by_date = {row.date: row for row in rows}
        if len(by_date) != len(rows):
            raise ExperimentalProbabilityUnavailable("方向模型历史日期重复")
        for index in range(60, len(sessions)):
            if index + offset >= len(sessions):
                excluded["target_outside_source"] += 1
                continue
            signal_date, target_date = sessions[index], sessions[index + offset]
            window = [by_date[day] for day in sessions[index - 20:index + 1] if day in by_date]
            future = by_date.get(target_date)
            vector, reason = _direction_observation(window, future)
            if vector is None:
                excluded[reason] += 1
                continue
            assert future is not None
            sample_id = f"close-d{offset}:{signal_date}:{symbol}"
            label = int(future.close > window[-1].close)
            samples.append(ProbabilitySample(sample_id=sample_id, session_date=signal_date,
                features=dict(zip(HISTORICAL_REPLAY_FEATURE_NAMES, vector, strict=True)), target=label))
            targets[sample_id] = target_date
            symbols.add(symbol)
            digest.update(canonical_json_bytes([sample_id, signal_date, target_date, window[-1].close, future.close, list(vector), label]))
    return samples, targets, sorted(symbols), {"samples_digest": digest.hexdigest(), "excluded": dict(excluded)}


def build_direction_model(manifest_path: Path, database: Path, directory: Path, *, offset: Literal[1, 2, 5]) -> Path:
    """Verify all source rows, fit independent labels and publish only a new file."""
    before = _static_database_digest(database)
    manifest = load_market_scan_probability_history_manifest(manifest_path, database_path=database)
    payload = cast(dict[str, Any], manifest["payload"])
    facts = payload["database"]
    if before != facts["sha256"]:
        raise ExperimentalProbabilityUnavailable("方向模型历史库与 manifest 摘要不一致")
    sessions = trusted_probability_history_dates(payload["anchor_date"], payload["config"]["history_bars"])
    series = _read_history(database)
    if _static_database_digest(database) != before:
        raise ExperimentalProbabilityUnavailable("方向模型构建期间历史库发生变化")
    manifest_digest = cast(dict[str, Any], manifest["integrity"])["integrity_digest"]
    return _publish_direction_model(series, sessions, directory, offset=offset, source_filename=database.name,
        source_sha256=before, manifest_digest=manifest_digest, limitations=payload["limitations"])


def _choice_candidate_directory(directory: Path) -> Path:
    output = directory.expanduser().absolute()
    protected = {".workbuddy-ai", ".git", ".codex", ".agents", ".venv"}
    if protected.intersection(output.parts) or not path_has_only_trusted_aliases(output):
        raise ExperimentalProbabilityUnavailable("Choice 候选模型输出目录受保护或包含路径别名")
    if tuple(output.parts[-len(MODEL_DIRECTORY.parts):]) == MODEL_DIRECTORY.parts:
        raise ExperimentalProbabilityUnavailable("Choice 候选模型必须使用独立研究目录，不能自动替换在线模型")
    return output


def build_choice_direction_model(manifest_path: Path, database: Path, directory: Path, *, offset: Literal[1, 2, 5]) -> Path:
    """Fit isolated candidates without replacing the runtime Tencent models."""
    from app.services.choice_experimental_history import load_choice_experimental_history

    output = _choice_candidate_directory(directory)
    history = load_choice_experimental_history(manifest_path, database)
    sources = (Path(history.provenance["source"]["directory"]), manifest_path.parent, database.parent)
    if any(output.resolve().is_relative_to(source.expanduser().resolve()) for source in sources):
        raise ExperimentalProbabilityUnavailable("Choice 候选模型必须写入原始及派生历史档案之外的独立目录")
    return _publish_direction_model(history.series, history.sessions, output, offset=offset,
        source_filename=database.name, source_sha256=history.provenance["source_sha256"],
        manifest_digest=history.provenance["manifest_digest"], limitations=history.provenance["limitations"])


def _publish_direction_model(series: Mapping[str, Sequence[Kline]], sessions: Sequence[str], directory: Path, *,
    offset: Literal[1, 2, 5], source_filename: str, source_sha256: str, manifest_digest: str,
    limitations: list[str],
) -> Path:
    samples, targets, symbols, receipt = direction_samples(series, sessions, offset=offset)
    parameters = fit_experimental_sample_parameters(samples, targets, horizon=offset, purge_sessions=offset)
    estimator = ExperimentalEstimator(
        **parameters, schema_version=DIRECTION_SCHEMA, horizon=offset, target="close_return_positive",
        source_filename=source_filename, source_sha256=source_sha256, source_integrity_digest=manifest_digest,
        training_symbols=symbols, cost_contract={},
        historical_recipe_evaluation={"status": "not_evaluated", "target": "close_return_positive", "horizon": offset},
        direction_evidence=DirectionEvidence(target_session_offset=offset, history_manifest_digest=manifest_digest,
            calendar_digest=sha256_hex(canonical_json_bytes(list(sessions))), samples_digest=receipt["samples_digest"],
            source_start=sessions[0], source_end=sessions[-1], sample_count=len(samples), excluded=receipt["excluded"]),
        limitations=[*limitations, "close_direction_not_costed_profit_or_execution",
                     "independent_oos_not_evaluated", "personal_experimental_not_formal_authority",
                     "current_predictions_use_reconstructed_cache_not_original_pit", "new_universe_generalization_unverified"],
    )
    model = estimator.model_dump()
    digest = sha256_hex(canonical_json_bytes(model))
    target = directory / f"experimental-close-d{offset}-{digest}.json"
    exclusive_atomic_publish(target, canonical_json_bytes({"payload": model, "sha256": digest}), max_bytes=MODEL_MAX_BYTES)
    return target
