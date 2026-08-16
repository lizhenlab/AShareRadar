"""Read-only historical OHLCV replay sources for Shadow probability research.

The replay cohort is deliberately separate from persisted full-market scans.  It
uses only qfq daily bars whose dates are no later than each signal session, and
it never claims that the reconstructed universe or metadata is an official
point-in-time market snapshot.
"""

from __future__ import annotations

from collections import Counter
from collections.abc import Iterator, Mapping, Sequence
from contextlib import contextmanager
from dataclasses import asdict, dataclass
from datetime import date
import hashlib
import json
import math
import os
from pathlib import Path
import sqlite3
from statistics import fmean, pstdev
from typing import Literal, Protocol, cast

from app.artifacts.io import (
    ArtifactContentConflictError,
    ArtifactDuplicateKeyError,
    ArtifactIOError,
    ArtifactNonFiniteConstantError,
    ArtifactPublishConflictError,
    ArtifactTooLargeError,
    decode_json_bytes,
    exclusive_atomic_publish,
    read_regular_file,
)
from app.models.paper_trading import CostProfileName, PaperCostProfile
from app.services.market_scan_probability import (
    ProbabilityConfig,
    ProbabilitySample,
    fit_shadow_probability,
    predict_shadow_probability,
)
from app.services.paper_trading_costs import resolve_cost_profile, trade_costs
from app.services.trading_calendar import is_trading_day, next_trade_dates
from app.utils.clock import utc_now


HISTORICAL_REPLAY_SCHEMA_VERSION = "market-scan-probability-historical-replay-v1"
HISTORICAL_REPLAY_ARTIFACT_SCHEMA_VERSION = (
    "market-scan-probability-historical-replay-artifact-v1"
)
HISTORICAL_REPLAY_COHORT_MODE = "historical_replay_v1"
HISTORICAL_REPLAY_SCOPE = "qfq_kline_daily_deterministic_market_sample"
HISTORICAL_REPLAY_FEATURE_VERSION = "historical-replay-common-ohlcv-v1"
HISTORICAL_REPLAY_LABEL_VERSION = "historical-replay-fixed-session-cost-label-v1"
HISTORICAL_REPLAY_HORIZONS = (1, 5, 20)
HISTORICAL_REPLAY_FEATURE_NAMES = (
    "atr14_pct",
    "close_location_value",
    "close_return_1d",
    "close_return_5d",
    "close_return_20d",
    "close_to_sma5",
    "close_to_sma20",
    "drawdown_20d",
    "intraday_range_pct",
    "close_return_volatility_20d",
    "volume_ratio_5_to_20",
)
_MINIMUM_FEATURE_BARS = 21
_DEFAULT_SYMBOL_LIMIT = 300
_MAXIMUM_SYMBOL_LIMIT = 500
_MAXIMUM_REPLAY_RECORDS = 100_000
_MAXIMUM_ARTIFACT_BYTES = 256 * 1024 * 1024
_SQLITE_SIDECAR_SUFFIXES = ("-wal", "-shm", "-journal")
_SQLITE_SIDECAR_POLICY = "reject_db_wal_shm_journal_v1"
_INTEGRITY_NOTICE = "sha256_integrity_not_signature_or_official_snapshot_attestation"
_GLOBAL_LIMITATIONS = (
    "survivorship_bias_current_qfq_cache_universe",
    "historical_listing_and_delisting_membership_unavailable",
    "historical_st_status_unavailable",
    "historical_amount_unavailable_capacity_not_modelled",
    "historical_turnover_rate_unavailable",
    "historical_price_limit_tradeability_not_modelled",
    "qfq_history_may_have_been_rebased_after_signal_date",
    "original_provider_vintage_unavailable_date_cutoff_only",
    "deterministic_market_sample_not_full_historical_universe",
    "research_only_never_official_or_production_ranking_input",
)


class HistoricalReplayError(ValueError):
    """Raised when a replay source or immutable artifact fails closed."""


class OHLCVBar(Protocol):
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
class HistoricalReplayConfig:
    start_date: str
    end_date: str
    minimum_history_bars: int = 61
    horizons: tuple[int, ...] = HISTORICAL_REPLAY_HORIZONS
    cost_profile: CostProfileName = "base"
    execution_notional: float = 100_000.0
    symbol_limit: int = _DEFAULT_SYMBOL_LIMIT
    symbols: tuple[str, ...] = ()

    def __post_init__(self) -> None:
        start, end = _iso_date(self.start_date, "start_date"), _iso_date(self.end_date, "end_date")
        if start > end:
            raise ValueError("start_date 不能晚于 end_date")
        if self.minimum_history_bars < _MINIMUM_FEATURE_BARS:
            raise ValueError(f"minimum_history_bars 不能小于 {_MINIMUM_FEATURE_BARS}")
        if self.horizons != HISTORICAL_REPLAY_HORIZONS:
            raise ValueError("historical replay horizons 固定为 1/5/20")
        if not math.isfinite(self.execution_notional) or self.execution_notional <= 0:
            raise ValueError("execution_notional 必须是正有限数")
        if isinstance(self.symbol_limit, bool) or not 1 <= self.symbol_limit <= _MAXIMUM_SYMBOL_LIMIT:
            raise ValueError(f"symbol_limit 必须在 1 到 {_MAXIMUM_SYMBOL_LIMIT} 之间")
        normalized = tuple(dict.fromkeys(value.strip().upper() for value in self.symbols if value.strip()))
        if len(normalized) > self.symbol_limit:
            raise ValueError("显式 symbols 数量不能超过 symbol_limit")
        object.__setattr__(self, "symbols", normalized)


@dataclass
class _ReplayBar:
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
    source: str
    fallback_used: bool


@dataclass(frozen=True)
class _ReplayContext:
    signal_dates: tuple[str, ...]
    target_dates: Mapping[str, tuple[str, ...]]
    database_max_date: str | None
    cost_profile: PaperCostProfile
    execution_notional: float
    selected_symbols: tuple[str, ...]
    universe_symbol_count: int
    universe_market_counts: Mapping[str, int]
    selected_market_counts: Mapping[str, int]
    sampling_strategy: str


@dataclass
class _ReplayAccumulator:
    records: list[dict[str, object]]
    exclusions: Counter[str]
    source_row_count: int = 0
    source_symbol_count: int = 0


def historical_replay_feature_values(
    rows: Sequence[OHLCVBar],
    *,
    signal_date: str,
) -> tuple[float, ...]:
    """Build the registered common OHLCV feature vector with a strict D cutoff."""
    cutoff = _iso_date(signal_date, "signal_date").isoformat()
    eligible = tuple(sorted((row for row in rows if row.date <= cutoff), key=lambda row: row.date))
    if len(eligible) < _MINIMUM_FEATURE_BARS:
        raise HistoricalReplayError("公共OHLCV特征至少需要21根截至D的日K")
    window = eligible[-_MINIMUM_FEATURE_BARS:]
    _validate_feature_window(window, cutoff)
    closes = [float(row.close) for row in window]
    volumes = [float(row.volume) for row in window]
    returns = [closes[index] / closes[index - 1] - 1 for index in range(1, len(closes))]
    true_ranges = _true_ranges(window[-15:])
    current = window[-1]
    values = {
        "atr14_pct": fmean(true_ranges) / current.close,
        "close_location_value": _close_location(current),
        "close_return_1d": closes[-1] / closes[-2] - 1,
        "close_return_5d": closes[-1] / closes[-6] - 1,
        "close_return_20d": closes[-1] / closes[-21] - 1,
        "close_to_sma5": closes[-1] / fmean(closes[-5:]) - 1,
        "close_to_sma20": closes[-1] / fmean(closes[-20:]) - 1,
        "drawdown_20d": closes[-1] / max(closes[-20:]) - 1,
        "intraday_range_pct": (current.high - current.low) / current.close,
        "close_return_volatility_20d": pstdev(returns),
        "volume_ratio_5_to_20": _safe_ratio(fmean(volumes[-5:]), fmean(volumes[-20:])),
    }
    vector = tuple(float(values[name]) for name in HISTORICAL_REPLAY_FEATURE_NAMES)
    if any(not math.isfinite(value) for value in vector):
        raise HistoricalReplayError("公共OHLCV特征包含非有限数")
    return vector


def evaluate_market_scan_probability_replay(
    database_path: str | Path,
    *,
    config: HistoricalReplayConfig,
    generated_at: str | None = None,
) -> dict[str, object]:
    """Read qfq history through one query-only snapshot and build replay rows."""
    path = Path(database_path).expanduser().resolve()
    timestamp = generated_at or utc_now().isoformat(timespec="seconds")
    signal_dates = tuple(value.isoformat() for value in _trusted_trade_dates(config))
    targets = {value: _fixed_target_dates(value) for value in signal_dates}
    maximum_target = max((dates[-1] for dates in targets.values()), default=config.end_date)
    before = _file_fingerprint(path)
    with _readonly_connection(path) as conn:
        selection = _select_symbols(conn, config)
        _require_bounded_replay(signal_dates, selection[0])
        database_range = _database_qfq_range(conn, selection[0])
        context = _ReplayContext(
            signal_dates,
            targets,
            database_range[1],
            resolve_cost_profile(config.cost_profile),
            config.execution_notional,
            selection[0],
            selection[1],
            selection[2],
            selection[3],
            selection[4],
        )
        accumulator = _evaluate_series(conn, config, maximum_target, context)
    after = _file_fingerprint(path)
    if before != after:
        raise HistoricalReplayError("historical replay 静态 SQLite 在只读评估期间发生外部变化")
    payload = _report_payload(path, config, timestamp, context, accumulator, database_range, before, after)
    _validate_report(payload, replay_probability_fit=False)
    return payload


def build_historical_replay_artifact(
    report: Mapping[str, object],
) -> dict[str, object]:
    """Seal one validated replay report into a content-addressed artifact."""
    normalized = _json_copy(report)
    _validate_report(normalized)
    artifact: dict[str, object] = {
        "schema_version": HISTORICAL_REPLAY_ARTIFACT_SCHEMA_VERSION,
        "generated_at": normalized["generated_at"],
        "payload": normalized,
    }
    artifact["integrity"] = {
        "algorithm": "sha256",
        "integrity_digest": _sha256_json(artifact),
        "notice": _INTEGRITY_NOTICE,
    }
    return artifact


def verify_historical_replay_artifact(
    artifact: Mapping[str, object],
) -> dict[str, object]:
    """Deep-check schema, row identities, fixed sessions, labels and SHA-256."""
    normalized = _json_copy(artifact)
    if set(normalized) != {"schema_version", "generated_at", "payload", "integrity"}:
        raise HistoricalReplayError("historical replay artifact 顶层字段无效")
    if normalized["schema_version"] != HISTORICAL_REPLAY_ARTIFACT_SCHEMA_VERSION:
        raise HistoricalReplayError("historical replay artifact schema_version 不受支持")
    integrity = _mapping(normalized["integrity"], "integrity")
    _validate_integrity(integrity, normalized)
    payload = _mapping(normalized["payload"], "payload")
    _validate_report(payload)
    if normalized["generated_at"] != payload["generated_at"]:
        raise HistoricalReplayError("artifact 与 payload generated_at 冲突")
    return normalized


def _verified_artifact_identity(artifact: Mapping[str, object]) -> dict[str, object]:
    normalized = _json_copy(artifact)
    if set(normalized) != {"schema_version", "generated_at", "payload", "integrity"}:
        raise HistoricalReplayError("historical replay artifact 顶层字段无效")
    if normalized["schema_version"] != HISTORICAL_REPLAY_ARTIFACT_SCHEMA_VERSION:
        raise HistoricalReplayError("historical replay artifact schema_version 不受支持")
    _validate_integrity(_mapping(normalized["integrity"], "integrity"), normalized)
    payload = _mapping(normalized["payload"], "payload")
    if payload.get("generated_at") != normalized["generated_at"]:
        raise HistoricalReplayError("artifact 与 payload generated_at 冲突")
    return normalized


def load_historical_replay_artifact(path: str | Path) -> dict[str, object]:
    """Load strict finite JSON and verify the entire replay artifact."""
    source = Path(path).expanduser().absolute()
    try:
        decoded = decode_json_bytes(
            read_regular_file(source, max_bytes=_MAXIMUM_ARTIFACT_BYTES),
        )
    except ArtifactDuplicateKeyError as exc:
        raise HistoricalReplayError(
            f"historical replay JSON 包含重复key：{exc.key}"
        ) from exc
    except ArtifactNonFiniteConstantError as exc:
        raise HistoricalReplayError(
            f"historical replay JSON 包含非有限常量：{exc.constant}"
        ) from exc
    except ArtifactIOError as exc:
        raise HistoricalReplayError(f"historical replay artifact 读取失败：{source}") from exc
    if not isinstance(decoded, Mapping):
        raise HistoricalReplayError("historical replay artifact 顶层必须是 object")
    return verify_historical_replay_artifact(decoded)


def write_historical_replay_artifact(
    path: str | Path,
    artifact: Mapping[str, object],
    *,
    database_path: str | Path,
) -> Path:
    """Publish verified JSON exclusively without replacing or linking SQLite."""
    target = Path(path).expanduser().absolute()
    database = Path(database_path).expanduser().resolve()
    _reject_database_target(target, database)
    verified = verify_historical_replay_artifact(artifact)
    encoded = canonical_historical_replay_json(verified).encode("utf-8")
    try:
        exclusive_atomic_publish(
            target,
            encoded,
            max_bytes=_MAXIMUM_ARTIFACT_BYTES,
            before_publish=lambda: _reject_database_target(target, database),
        )
    except ArtifactTooLargeError as exc:
        raise HistoricalReplayError(
            "historical replay artifact 超过256MiB安全上限，请缩小日期或symbol分片"
        ) from exc
    except ArtifactContentConflictError as exc:
        raise HistoricalReplayError(
            f"historical replay artifact 已存在且内容不同：{target}"
        ) from exc
    except ArtifactPublishConflictError as exc:
        raise HistoricalReplayError(
            f"historical replay artifact 并发发布冲突：{target}"
        ) from exc
    except HistoricalReplayError:
        raise
    except ArtifactIOError as exc:
        raise HistoricalReplayError(
            f"historical replay artifact 写入失败：{target}"
        ) from exc
    except OSError as exc:
        raise HistoricalReplayError(
            f"historical replay artifact 写入失败：{target}"
        ) from exc
    return target


def replay_rows_to_probability_samples(
    artifact: Mapping[str, object],
    *,
    horizon: Literal[1, 5, 20] = 5,
    target: Literal["net_return_positive"] = "net_return_positive",
) -> tuple[ProbabilitySample, ...]:
    """Convert replay rows to the existing grouped-date probability input type."""
    if target != "net_return_positive":
        raise ValueError("historical replay v1 仅提供 net_return_positive 标签")
    verified = verify_historical_replay_artifact(artifact)
    payload = _mapping(verified["payload"], "payload")
    names = tuple(cast(Sequence[str], _mapping(payload["feature_contract"], "feature_contract")["feature_names"]))
    return tuple(_record_probability_sample(record, names, horizon) for record in _records(payload))


def forecast_historical_replay_shadow(
    artifact: Mapping[str, object],
    current_rows: Sequence[OHLCVBar],
    *,
    signal_date: str,
    horizon: Literal[1, 5, 20] = 5,
    sample_id: str = "historical-replay-current-ohlcv",
) -> dict[str, object]:
    """Fit the isolated replay cohort and optionally forecast one current OHLCV row."""
    verified = verify_historical_replay_artifact(artifact)
    payload = _mapping(verified["payload"], "payload")
    samples = _payload_probability_samples(payload, horizon)
    evidence = _fit_replay_samples(samples, horizon, str(payload["generated_at"]))
    vector = historical_replay_feature_values(current_rows, signal_date=signal_date)
    if evidence.get("status") != "calibrated_shadow":
        return _forecast_unavailable(payload, evidence, horizon, signal_date, vector)
    features = dict(zip(HISTORICAL_REPLAY_FEATURE_NAMES, vector, strict=True))
    estimate = predict_shadow_probability(evidence, features, sample_id=sample_id)
    return {
        "cohort": payload["cohort"],
        "status": estimate["status"],
        "probability": estimate.get("probability"),
        "confidence_interval": estimate.get("confidence_interval"),
        "horizon": horizon,
        "target": "net_return_positive",
        "signal_date": signal_date,
        "feature_values": list(vector),
        "training_evidence_digest": evidence.get("evidence_digest"),
        "production_ranking_effect": "none",
    }


def historical_replay_artifact_filename(artifact: Mapping[str, object]) -> str:
    verified = _verified_artifact_identity(artifact)
    payload = _mapping(verified["payload"], "payload")
    config = _mapping(payload["config"], "config")
    digest = str(_mapping(verified["integrity"], "integrity")["integrity_digest"])
    return f"market-scan-probability-historical-replay-{config['start_date']}-{config['end_date']}-{digest}.json"


def canonical_historical_replay_json(value: object) -> str:
    _validate_json_tree(value, "json")
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"), allow_nan=False)


def _evaluate_series(
    conn: sqlite3.Connection,
    config: HistoricalReplayConfig,
    maximum_target: str,
    context: _ReplayContext,
) -> _ReplayAccumulator:
    accumulator = _ReplayAccumulator([], Counter())
    for symbol, rows in _series_rows(conn, context.selected_symbols, maximum_target):
        accumulator.source_symbol_count += 1
        accumulator.source_row_count += len(rows)
        _evaluate_symbol(symbol, rows, config, context, accumulator)
    return accumulator


def _evaluate_symbol(
    symbol: str,
    rows: Sequence[_ReplayBar],
    config: HistoricalReplayConfig,
    context: _ReplayContext,
    accumulator: _ReplayAccumulator,
) -> None:
    by_date = {row.date: row for row in rows}
    ordered = tuple(sorted(rows, key=lambda row: row.date))
    for signal_date in context.signal_dates:
        signal = by_date.get(signal_date)
        history = tuple(row for row in ordered if row.date <= signal_date)
        reason = _signal_exclusion_reason(signal, history, config.minimum_history_bars)
        if reason is not None:
            accumulator.exclusions[reason] += 1
            continue
        assert signal is not None
        record = _replay_record(symbol, signal_date, history, by_date, context, config)
        accumulator.records.append(record)


def _replay_record(
    symbol: str,
    signal_date: str,
    history: Sequence[_ReplayBar],
    by_date: Mapping[str, _ReplayBar],
    context: _ReplayContext,
    config: HistoricalReplayConfig,
) -> dict[str, object]:
    features = historical_replay_feature_values(history, signal_date=signal_date)
    target_dates = context.target_dates[signal_date]
    entry = _entry_payload(by_date.get(target_dates[0]), target_dates[0], context.database_max_date)
    outcomes = [
        _outcome_payload(entry, by_date.get(target_dates[horizon]), target_dates[horizon], horizon, context)
        for horizon in HISTORICAL_REPLAY_HORIZONS
    ]
    feature_rows = tuple(history[-_MINIMUM_FEATURE_BARS:])
    record: dict[str, object] = {
        "sample_id": f"{HISTORICAL_REPLAY_COHORT_MODE}:{signal_date}:{symbol}",
        "symbol": symbol,
        "signal_date": signal_date,
        "feature_values": list(features),
        "feature_vector_digest": _sha256_json({"names": HISTORICAL_REPLAY_FEATURE_NAMES, "values": features}),
        "feature_source": _feature_source(feature_rows, len(history)),
        "entry": entry,
        "outcomes": outcomes,
        "record_limitations": _record_limitations(feature_rows, entry, outcomes, config),
    }
    record["record_digest"] = _sha256_json(record)
    return record


def _entry_payload(bar: _ReplayBar | None, target_date: str, maximum_date: str | None) -> dict[str, object]:
    status, reason = _fixed_bar_status(bar, target_date, maximum_date, "entry")
    payload: dict[str, object] = {
        "session_date": target_date,
        "status": status,
        "reason": reason,
        "open": bar.open if status == "available" and bar is not None else None,
        "volume": bar.volume if status == "available" and bar is not None else None,
        "data_version": bar.data_version if bar is not None else None,
        "contract_version": bar.contract_version if bar is not None else None,
        "bar_digest": _bar_digest(bar) if bar is not None else None,
    }
    return payload


def _outcome_payload(
    entry: Mapping[str, object],
    exit_bar: _ReplayBar | None,
    target_date: str,
    horizon: int,
    context: _ReplayContext,
) -> dict[str, object]:
    entry_status = str(entry["status"])
    if entry_status != "available":
        return _unavailable_outcome(horizon, target_date, str(entry["reason"]))
    status, reason = _fixed_bar_status(exit_bar, target_date, context.database_max_date, "exit")
    if status != "available" or exit_bar is None:
        return _unavailable_outcome(horizon, target_date, reason)
    if exit_bar.contract_version != entry["contract_version"] or exit_bar.data_version != entry["data_version"]:
        return _unavailable_outcome(horizon, target_date, "fixed_exit_contract_conflict")
    return _modelled_outcome(entry, exit_bar, target_date, horizon, context)


def _modelled_outcome(
    entry: Mapping[str, object],
    exit_bar: _ReplayBar,
    target_date: str,
    horizon: int,
    context: _ReplayContext,
) -> dict[str, object]:
    entry_price = float(cast(float, entry["open"]))
    quantity = (math.floor(context.execution_notional / entry_price) // 100) * 100
    if quantity < 100:
        return _unavailable_outcome(horizon, target_date, "minimum_board_lot_unaffordable")
    return _modelled_outcome_for_quantity(
        entry_price, exit_bar, target_date, horizon, context.cost_profile, quantity,
    )


def _modelled_outcome_for_quantity(
    entry_price: float,
    exit_bar: _ReplayBar,
    target_date: str,
    horizon: int,
    profile: PaperCostProfile,
    quantity: int,
) -> dict[str, object]:
    buy_amount, sell_amount = entry_price * quantity, exit_bar.close * quantity
    buy_cost = trade_costs(profile, side="buy", gross_amount=buy_amount).total
    sell_cost = trade_costs(profile, side="sell", gross_amount=sell_amount).total
    gross = exit_bar.close / entry_price - 1
    net = (sell_amount - sell_cost - buy_amount - buy_cost) / (buy_amount + buy_cost)
    return {
        "horizon": horizon,
        "target_session_date": target_date,
        "status": "modelled",
        "reason": "fixed_session_close",
        "exit_close": exit_bar.close,
        "exit_volume": exit_bar.volume,
        "exit_bar_digest": _bar_digest(exit_bar),
        "quantity": quantity,
        "buy_cost": buy_cost,
        "sell_cost": sell_cost,
        "gross_return": gross,
        "net_return": net,
        "cost_drag": gross - net,
        "gross_positive": gross > 0,
        "net_positive": net > 0,
    }


def _unavailable_outcome(horizon: int, target_date: str, reason: str) -> dict[str, object]:
    return {
        "horizon": horizon,
        "target_session_date": target_date,
        "status": "not_mature" if reason.startswith("fixed_") and reason.endswith("_not_mature") else "data_unavailable",
        "reason": reason,
        "exit_close": None,
        "exit_volume": None,
        "exit_bar_digest": None,
        "quantity": None,
        "buy_cost": None,
        "sell_cost": None,
        "gross_return": None,
        "net_return": None,
        "cost_drag": None,
        "gross_positive": None,
        "net_positive": None,
    }


def _report_payload(
    path: Path,
    config: HistoricalReplayConfig,
    generated_at: str,
    context: _ReplayContext,
    accumulator: _ReplayAccumulator,
    database_range: tuple[str | None, str | None],
    before: Mapping[str, object],
    after: Mapping[str, object],
) -> dict[str, object]:
    quality = _quality_payload(config, context, accumulator)
    payload: dict[str, object] = {
        "schema_version": HISTORICAL_REPLAY_SCHEMA_VERSION,
        "generated_at": generated_at,
        "status": _report_status(quality),
        "cohort": _cohort_payload(),
        "config": _config_payload(config),
        "source": _source_payload(path, context, accumulator, database_range, before, after),
        "feature_contract": _feature_contract(),
        "cost_contract": _cost_contract(context.cost_profile, config.execution_notional),
        "metadata_contract": _metadata_contract(),
        "records": accumulator.records,
        "quality": quality,
        "limitations": list(_GLOBAL_LIMITATIONS),
    }
    payload["probability_fit"] = _probability_fit_payload(payload, generated_at)
    return payload


def _quality_payload(
    config: HistoricalReplayConfig,
    context: _ReplayContext,
    accumulator: _ReplayAccumulator,
) -> dict[str, object]:
    horizon_rows = {
        str(horizon): _horizon_quality(accumulator.records, horizon, context.database_max_date)
        for horizon in HISTORICAL_REPLAY_HORIZONS
    }
    return {
        "requested_signal_session_count": len(context.signal_dates),
        "requested_signal_dates": list(context.signal_dates),
        "source_symbol_count": accumulator.source_symbol_count,
        "universe_symbol_count": context.universe_symbol_count,
        "selected_symbol_count": len(context.selected_symbols),
        "universe_market_counts": dict(context.universe_market_counts),
        "selected_market_counts": dict(context.selected_market_counts),
        "sampling_strategy": context.sampling_strategy,
        "source_row_count": accumulator.source_row_count,
        "record_count": len(accumulator.records),
        "record_independent_session_count": len({str(item["signal_date"]) for item in accumulator.records}),
        "excluded_candidate_count": sum(accumulator.exclusions.values()),
        "exclusion_reason_counts": dict(sorted(accumulator.exclusions.items())),
        "horizons": horizon_rows,
        "registered_probability_split_defaults": _registered_split_defaults(),
        "minimum_history_bars": config.minimum_history_bars,
    }


def _probability_fit_payload(
    payload: Mapping[str, object],
    generated_at: str,
) -> dict[str, object]:
    return {
        "cohort": _cohort_payload(),
        "target": "net_return_positive",
        "probability": None,
        "horizons": {
            str(horizon): _fit_evidence_summary(
                _fit_replay_samples(_payload_probability_samples(payload, horizon), horizon, generated_at),
                horizon,
            )
            for horizon in HISTORICAL_REPLAY_HORIZONS
        },
        "production_ranking_effect": "none",
        "automatic_promotion": False,
    }


def _fit_replay_samples(
    samples: Sequence[ProbabilitySample],
    horizon: int,
    generated_at: str,
) -> dict[str, object]:
    return fit_shadow_probability(
        samples,
        config=ProbabilityConfig(horizon=horizon, target="net_return_positive"),
        generated_at=generated_at,
    )


def _fit_evidence_summary(evidence: Mapping[str, object], horizon: int) -> dict[str, object]:
    return {
        "status": evidence.get("status"),
        "probability": None,
        "horizon": horizon,
        "target": "net_return_positive",
        "counts": evidence.get("counts"),
        "training_cutoff": evidence.get("training_cutoff"),
        "base_rate": evidence.get("base_rate"),
        "calibration_metrics": evidence.get("calibration_metrics"),
        "input_digest": evidence.get("input_digest"),
        "model_digest": evidence.get("model_digest"),
        "calibrator_digest": evidence.get("calibrator_digest"),
        "evidence_digest": evidence.get("evidence_digest"),
        "limitations": evidence.get("limitations"),
        "minimum_required_independent_session_count": _minimum_registered_sessions(horizon),
    }


def _payload_probability_samples(
    payload: Mapping[str, object],
    horizon: int,
) -> tuple[ProbabilitySample, ...]:
    contract = _mapping(payload["feature_contract"], "feature_contract")
    names = tuple(cast(Sequence[str], contract["feature_names"]))
    return tuple(_record_probability_sample(record, names, horizon) for record in _records(payload))


def _forecast_unavailable(
    payload: Mapping[str, object],
    evidence: Mapping[str, object],
    horizon: int,
    signal_date: str,
    vector: Sequence[float],
) -> dict[str, object]:
    return {
        "cohort": payload["cohort"],
        "status": "insufficient_data",
        "probability": None,
        "confidence_interval": None,
        "horizon": horizon,
        "target": "net_return_positive",
        "signal_date": signal_date,
        "feature_values": list(vector),
        "training_evidence_digest": evidence.get("evidence_digest"),
        "counts": evidence.get("counts"),
        "limitations": evidence.get("limitations"),
        "production_ranking_effect": "none",
    }


def _horizon_quality(
    records: Sequence[Mapping[str, object]],
    horizon: int,
    maximum_date: str | None,
    *,
    minimum_required_sessions: int | None = None,
) -> dict[str, object]:
    outcomes = [_outcome_for(record, horizon) for record in records]
    modelled = [item for item in outcomes if item["status"] == "modelled"]
    modelled_dates = {
        str(record["signal_date"])
        for record, outcome in zip(records, outcomes, strict=True)
        if outcome["status"] == "modelled"
    }
    mature = [item for item in outcomes if maximum_date is not None and str(item["target_session_date"]) <= maximum_date]
    required = minimum_required_sessions or _minimum_registered_sessions(horizon)
    return {
        "requested_record_count": len(records),
        "mature_record_count": len(mature),
        "modelled_record_count": len(modelled),
        "modelled_independent_session_count": len(modelled_dates),
        "label_coverage": len(modelled) / len(mature) if mature else 0.0,
        "minimum_required_independent_session_count": required,
        "registered_split_status": "ready" if len(modelled_dates) >= required else "insufficient_data",
    }


def _validate_report(
    report: Mapping[str, object],
    *,
    replay_probability_fit: bool = True,
) -> None:
    required = {
        "schema_version", "generated_at", "status", "cohort", "config", "source",
        "feature_contract", "cost_contract", "metadata_contract", "records", "quality", "limitations",
        "probability_fit",
    }
    if set(report) != required or report.get("schema_version") != HISTORICAL_REPLAY_SCHEMA_VERSION:
        raise HistoricalReplayError("historical replay report schema 无效")
    _validate_cohort(_mapping(report["cohort"], "cohort"))
    config = _mapping(report["config"], "config")
    parsed_config = _validated_report_config(config)
    feature_contract = _mapping(report["feature_contract"], "feature_contract")
    if feature_contract != _feature_contract():
        raise HistoricalReplayError("historical replay feature contract 冲突")
    names = tuple(cast(Sequence[str], feature_contract["feature_names"]))
    source = _mapping(report["source"], "source")
    _validate_source(source, parsed_config)
    records = _records(report)
    _validate_records(records, names, config)
    quality = _mapping(report["quality"], "quality")
    legacy_split = _uses_superseded_probability_split(quality)
    _validate_quality(quality, records, parsed_config, source)
    _validate_cost_contract(_mapping(report["cost_contract"], "cost_contract"), config)
    if report["metadata_contract"] != _metadata_contract():
        raise HistoricalReplayError("historical replay metadata contract 冲突")
    if report["limitations"] != list(_GLOBAL_LIMITATIONS):
        raise HistoricalReplayError("historical replay 全局 limitations 冲突")
    _validate_probability_fit(
        report,
        _mapping(report["probability_fit"], "probability_fit"),
        replay=replay_probability_fit and not legacy_split,
    )
    if report["status"] != _report_status(_mapping(report["quality"], "quality")):
        raise HistoricalReplayError("historical replay status 与质量摘要冲突")


def _validate_records(
    records: Sequence[Mapping[str, object]],
    feature_names: Sequence[str],
    config: Mapping[str, object],
) -> None:
    identities: set[str] = set()
    for record in records:
        sample_id = str(record.get("sample_id") or "")
        expected = f"{HISTORICAL_REPLAY_COHORT_MODE}:{record.get('signal_date')}:{record.get('symbol')}"
        if sample_id != expected or sample_id in identities:
            raise HistoricalReplayError("historical replay record identity 重复或冲突")
        identities.add(sample_id)
        _validate_feature_record(record, feature_names)
        _validate_record_sessions(record, config)
        _validate_record_digest(record)


def _validate_feature_record(record: Mapping[str, object], feature_names: Sequence[str]) -> None:
    values = record.get("feature_values")
    if not isinstance(values, list) or len(values) != len(feature_names):
        raise HistoricalReplayError("historical replay feature_values 数量无效")
    numeric = tuple(_finite_number(value, "feature_values") for value in values)
    expected = _sha256_json({"names": tuple(feature_names), "values": numeric})
    if record.get("feature_vector_digest") != expected:
        raise HistoricalReplayError("historical replay feature vector digest 冲突")


def _validate_record_sessions(record: Mapping[str, object], config: Mapping[str, object]) -> None:
    signal = _iso_date(str(record.get("signal_date")), "signal_date")
    start, end = _iso_date(str(config["start_date"]), "start_date"), _iso_date(str(config["end_date"]), "end_date")
    if not start <= signal <= end:
        raise HistoricalReplayError("historical replay signal_date 超出配置范围")
    expected = tuple(value.isoformat() for value in next_trade_dates(signal, max(HISTORICAL_REPLAY_HORIZONS) + 1))
    entry = _mapping(record.get("entry"), "entry")
    if entry.get("session_date") != expected[0]:
        raise HistoricalReplayError("historical replay entry 不是固定D+1交易日")
    outcomes = record.get("outcomes")
    if not isinstance(outcomes, list) or [item.get("horizon") for item in outcomes if isinstance(item, Mapping)] != list(HISTORICAL_REPLAY_HORIZONS):
        raise HistoricalReplayError("historical replay outcomes 矩阵无效")
    for raw in outcomes:
        outcome = _mapping(raw, "outcome")
        horizon = int(cast(int, outcome["horizon"]))
        if outcome.get("target_session_date") != expected[horizon]:
            raise HistoricalReplayError("historical replay target 被顺延或日期冲突")
        _validate_outcome(entry, outcome, config)


def _validate_outcome(
    entry: Mapping[str, object],
    outcome: Mapping[str, object],
    config: Mapping[str, object],
) -> None:
    status = outcome.get("status")
    if status not in {"modelled", "not_mature", "data_unavailable"}:
        raise HistoricalReplayError("historical replay outcome status 无效")
    numeric = ("quantity", "buy_cost", "sell_cost", "gross_return", "net_return", "cost_drag")
    if status != "modelled":
        if any(outcome.get(name) is not None for name in numeric):
            raise HistoricalReplayError("不可用 historical replay outcome 携带收益")
        return
    entry_price = _finite_number(entry.get("open"), "entry.open")
    exit_price = _finite_number(outcome.get("exit_close"), "exit_close")
    gross = _finite_number(outcome.get("gross_return"), "gross_return")
    net = _finite_number(outcome.get("net_return"), "net_return")
    drag = _finite_number(outcome.get("cost_drag"), "cost_drag")
    expected_net = _validated_modelled_costs(entry_price, exit_price, outcome, config)
    if not math.isclose(gross, exit_price / entry_price - 1, rel_tol=0, abs_tol=1e-12):
        raise HistoricalReplayError("historical replay gross_return 无法由固定bar重放")
    if not math.isclose(drag, gross - net, rel_tol=0, abs_tol=1e-12):
        raise HistoricalReplayError("historical replay cost_drag 无法重放")
    if not math.isclose(net, expected_net, rel_tol=0, abs_tol=1e-12):
        raise HistoricalReplayError("historical replay net_return 无法由注册成本重放")
    if outcome.get("gross_positive") is not (gross > 0) or outcome.get("net_positive") is not (net > 0):
        raise HistoricalReplayError("historical replay 正收益标签冲突")


def _validated_modelled_costs(
    entry_price: float,
    exit_price: float,
    outcome: Mapping[str, object],
    config: Mapping[str, object],
) -> float:
    quantity = _positive_integer(outcome.get("quantity"), "quantity")
    notional = _finite_number(config.get("execution_notional"), "execution_notional")
    expected_quantity = (math.floor(notional / entry_price) // 100) * 100
    if quantity != expected_quantity:
        raise HistoricalReplayError("historical replay quantity 未使用注册 execution_notional")
    profile = resolve_cost_profile(cast(CostProfileName, str(config["cost_profile"])))
    buy_amount, sell_amount = entry_price * quantity, exit_price * quantity
    buy_cost = trade_costs(profile, side="buy", gross_amount=buy_amount).total
    sell_cost = trade_costs(profile, side="sell", gross_amount=sell_amount).total
    if not math.isclose(_finite_number(outcome.get("buy_cost"), "buy_cost"), buy_cost, rel_tol=0, abs_tol=1e-12):
        raise HistoricalReplayError("historical replay buy_cost 与注册成本不一致")
    if not math.isclose(_finite_number(outcome.get("sell_cost"), "sell_cost"), sell_cost, rel_tol=0, abs_tol=1e-12):
        raise HistoricalReplayError("historical replay sell_cost 与注册成本不一致")
    return (sell_amount - sell_cost - buy_amount - buy_cost) / (buy_amount + buy_cost)


def _validate_cost_contract(contract: Mapping[str, object], config: Mapping[str, object]) -> None:
    profile = resolve_cost_profile(cast(CostProfileName, str(config["cost_profile"])))
    if contract != _cost_contract(profile, float(cast(float, config["execution_notional"]))):
        raise HistoricalReplayError("historical replay cost contract 与注册配置冲突")


def _validate_probability_fit(
    report: Mapping[str, object],
    value: Mapping[str, object],
    *,
    replay: bool,
) -> None:
    _validate_probability_fit_header(value)
    horizons = _mapping(value.get("horizons"), "probability_fit.horizons")
    _validate_probability_fit_horizons(horizons)
    expected = _probability_fit_payload(report, str(report["generated_at"])) if replay else value
    if replay and value != expected:
        raise HistoricalReplayError("historical replay probability fit 无法由records确定性重建")


def _validate_probability_fit_header(value: Mapping[str, object]) -> None:
    if set(value) != {
        "cohort", "target", "probability", "horizons", "production_ranking_effect",
        "automatic_promotion",
    }:
        raise HistoricalReplayError("historical replay probability fit 字段无效")
    if value.get("cohort") != _cohort_payload() or value.get("target") != "net_return_positive":
        raise HistoricalReplayError("historical replay probability fit cohort/target 冲突")
    if value.get("probability") is not None or value.get("production_ranking_effect") != "none":
        raise HistoricalReplayError("historical replay probability fit 不能影响生产排名")
    if value.get("automatic_promotion") is not False:
        raise HistoricalReplayError("historical replay probability fit 不能自动晋级")


def _validate_probability_fit_horizons(horizons: Mapping[str, object]) -> None:
    if set(horizons) != {str(horizon) for horizon in HISTORICAL_REPLAY_HORIZONS}:
        raise HistoricalReplayError("historical replay probability fit horizons 冲突")
    for horizon in HISTORICAL_REPLAY_HORIZONS:
        evidence = _mapping(horizons.get(str(horizon)), f"probability_fit.horizons.{horizon}")
        if evidence.get("horizon") != horizon or evidence.get("probability") is not None:
            raise HistoricalReplayError("historical replay cohort fit 不能伪造单点概率")
        if evidence.get("status") not in {"insufficient_data", "calibrated_shadow"}:
            raise HistoricalReplayError("historical replay probability fit status 无效")


def _validate_quality(
    quality: Mapping[str, object],
    records: Sequence[Mapping[str, object]],
    config: HistoricalReplayConfig,
    source: Mapping[str, object],
) -> None:
    required = {
        "requested_signal_session_count", "requested_signal_dates", "source_symbol_count",
        "universe_symbol_count", "selected_symbol_count", "universe_market_counts",
        "selected_market_counts", "sampling_strategy", "source_row_count", "record_count",
        "record_independent_session_count", "excluded_candidate_count", "exclusion_reason_counts",
        "horizons", "registered_probability_split_defaults", "minimum_history_bars",
    }
    if set(quality) != required:
        raise HistoricalReplayError("historical replay quality 字段无效")
    split_defaults = _validated_registered_split_defaults(quality)
    _validate_quality_identity(quality, records, config, source)
    horizons = _mapping(quality.get("horizons"), "quality.horizons")
    if set(horizons) != {str(horizon) for horizon in HISTORICAL_REPLAY_HORIZONS}:
        raise HistoricalReplayError("historical replay quality horizons 冲突")
    for horizon in HISTORICAL_REPLAY_HORIZONS:
        value = _mapping(horizons.get(str(horizon)), f"quality.horizons.{horizon}")
        registered = _mapping(split_defaults[str(horizon)], f"split_defaults.{horizon}")
        expected = _horizon_quality(
            records,
            horizon,
            cast(str | None, source.get("maximum_qfq_date")),
            minimum_required_sessions=int(cast(int, registered["minimum_required_independent_session_count"])),
        )
        if value != expected:
            raise HistoricalReplayError("historical replay horizon quality 无法由records重放")


def _validate_quality_identity(
    quality: Mapping[str, object],
    records: Sequence[Mapping[str, object]],
    config: HistoricalReplayConfig,
    source: Mapping[str, object],
) -> None:
    dates = tuple(value.isoformat() for value in _trusted_trade_dates(config))
    expected = {
        "requested_signal_session_count": len(dates), "requested_signal_dates": list(dates),
        "source_symbol_count": source["source_symbol_count"],
        "universe_symbol_count": source["universe_symbol_count"],
        "selected_symbol_count": source["selected_symbol_count"],
        "universe_market_counts": source["universe_market_counts"],
        "selected_market_counts": source["selected_market_counts"],
        "sampling_strategy": source["sampling_strategy"], "source_row_count": source["source_row_count"],
        "record_count": len(records),
        "record_independent_session_count": len({str(item["signal_date"]) for item in records}),
        "minimum_history_bars": config.minimum_history_bars,
    }
    if any(quality.get(key) != value for key, value in expected.items()):
        raise HistoricalReplayError("historical replay quality identity 无法重放")
    _validate_quality_exclusions(quality, records, len(dates), int(cast(int, source["source_symbol_count"])))


def _validated_registered_split_defaults(quality: Mapping[str, object]) -> Mapping[str, object]:
    value = _mapping(
        quality.get("registered_probability_split_defaults"),
        "quality.registered_probability_split_defaults",
    )
    if value != _registered_split_defaults() and value != _superseded_registered_split_defaults():
        raise HistoricalReplayError("historical replay probability split defaults 不受支持")
    return value


def _uses_superseded_probability_split(quality: Mapping[str, object]) -> bool:
    return quality.get("registered_probability_split_defaults") == _superseded_registered_split_defaults()


def _validate_quality_exclusions(
    quality: Mapping[str, object],
    records: Sequence[Mapping[str, object]],
    requested_sessions: int,
    source_symbols: int,
) -> None:
    reasons = _mapping(quality.get("exclusion_reason_counts"), "exclusion_reason_counts")
    if any(isinstance(value, bool) or not isinstance(value, int) or value < 0 for value in reasons.values()):
        raise HistoricalReplayError("historical replay exclusion reason count 无效")
    excluded = sum(cast(int, value) for value in reasons.values())
    if quality.get("excluded_candidate_count") != excluded:
        raise HistoricalReplayError("historical replay exclusion count 冲突")
    if len(records) + excluded != requested_sessions * source_symbols:
        raise HistoricalReplayError("historical replay candidate accounting 无法重放")


def _record_probability_sample(
    record: Mapping[str, object],
    names: Sequence[str],
    horizon: int,
) -> ProbabilitySample:
    if horizon not in HISTORICAL_REPLAY_HORIZONS:
        raise ValueError("horizon 必须是 1、5 或 20")
    outcome = _outcome_for(record, horizon)
    modelled = outcome["status"] == "modelled"
    net_return = float(cast(float, outcome["net_return"])) if modelled else None
    values = cast(Sequence[float], record["feature_values"])
    return ProbabilitySample(
        sample_id=f"{record['sample_id']}:{horizon}:net_return_positive",
        session_date=str(record["signal_date"]),
        features=dict(zip(names, values, strict=True)),
        target=int(net_return > 0) if net_return is not None else None,
        executable=modelled,
        net_return=net_return,
    )


def _series_rows(
    conn: sqlite3.Connection,
    symbols: Sequence[str],
    maximum_target: str,
) -> Iterator[tuple[str, tuple[_ReplayBar, ...]]]:
    clauses, parameters = ["adjustment_mode = 'qfq'", "date <= ?"], [maximum_target]
    clauses.append(f"symbol IN ({','.join('?' for _ in symbols)})")
    parameters.extend(symbols)
    cursor = conn.execute(
        f"""
        SELECT symbol, date, open, close, high, low, volume, adjustment_mode,
               data_version, contract_version, source, fallback_used
        FROM kline_daily
        WHERE {' AND '.join(clauses)}
        ORDER BY symbol ASC, date ASC
        """,
        parameters,
    )
    symbol = ""
    values: list[_ReplayBar] = []
    for row in cursor:
        parsed = _row_bar(row)
        if symbol and parsed.symbol != symbol:
            yield symbol, tuple(values)
            values = []
        symbol = parsed.symbol
        values.append(parsed)
    if symbol:
        yield symbol, tuple(values)


def _select_symbols(
    conn: sqlite3.Connection,
    config: HistoricalReplayConfig,
) -> tuple[tuple[str, ...], int, dict[str, int], dict[str, int], str]:
    rows = conn.execute(
        "SELECT DISTINCT symbol FROM kline_daily WHERE adjustment_mode = 'qfq' ORDER BY symbol ASC"
    ).fetchall()
    universe = tuple(str(row[0]) for row in rows)
    universe_counts = _market_counts(universe)
    if config.symbols:
        available = frozenset(universe)
        selected = tuple(symbol for symbol in config.symbols if symbol in available)
        strategy = "explicit_symbol_list_available_qfq_only"
    else:
        selected = _balanced_market_sample(universe, config.symbol_limit)
        strategy = "deterministic_sha256_balanced_round_robin_SH_SZ_BJ_v1"
    if not selected:
        raise HistoricalReplayError("所选范围没有可用 qfq symbol")
    return selected, len(universe), universe_counts, _market_counts(selected), strategy


def _balanced_market_sample(symbols: Sequence[str], limit: int) -> tuple[str, ...]:
    grouped: dict[str, list[str]] = {market: [] for market in ("SH", "SZ", "BJ", "OTHER")}
    for symbol in symbols:
        grouped[_symbol_market(symbol)].append(symbol)
    for values in grouped.values():
        values.sort(key=lambda value: (hashlib.sha256(value.encode("utf-8")).hexdigest(), value))
    ordered: list[str] = []
    markets = ("SH", "SZ", "BJ", "OTHER")
    index = 0
    while len(ordered) < min(limit, len(symbols)):
        added = False
        for market in markets:
            values = grouped[market]
            if index < len(values) and len(ordered) < limit:
                ordered.append(values[index])
                added = True
        if not added:
            break
        index += 1
    return tuple(ordered)


def _market_counts(symbols: Sequence[str]) -> dict[str, int]:
    counts = Counter(_symbol_market(symbol) for symbol in symbols)
    return {market: counts.get(market, 0) for market in ("SH", "SZ", "BJ", "OTHER")}


def _symbol_market(symbol: str) -> str:
    suffix = symbol.rsplit(".", 1)[-1].upper() if "." in symbol else "OTHER"
    return suffix if suffix in {"SH", "SZ", "BJ"} else "OTHER"


def _require_bounded_replay(signal_dates: Sequence[str], symbols: Sequence[str]) -> None:
    candidate_count = len(signal_dates) * len(symbols)
    if candidate_count > _MAXIMUM_REPLAY_RECORDS:
        raise HistoricalReplayError(
            f"historical replay 候选 {candidate_count} 超过 {_MAXIMUM_REPLAY_RECORDS} 安全上限；"
            "请缩小日期范围或 symbol_limit 后分片"
        )


def _row_bar(row: sqlite3.Row) -> _ReplayBar:
    return _ReplayBar(
        symbol=str(row["symbol"]), date=str(row["date"]), open=float(row["open"]),
        close=float(row["close"]), high=float(row["high"]), low=float(row["low"]),
        volume=float(row["volume"]), adjustment_mode=str(row["adjustment_mode"]),
        data_version=str(row["data_version"] or ""), contract_version=str(row["contract_version"] or ""),
        source=str(row["source"] or ""), fallback_used=bool(row["fallback_used"]),
    )


def _trusted_trade_dates(config: HistoricalReplayConfig) -> tuple[date, ...]:
    start, end = _iso_date(config.start_date, "start_date"), _iso_date(config.end_date, "end_date")
    current = start if is_trading_day(start) else next_trade_dates(start, 1)[0]
    values: list[date] = []
    while current <= end:
        values.append(current)
        current = next_trade_dates(current, 1)[0]
    return tuple(values)


def _fixed_target_dates(signal_date: str) -> tuple[str, ...]:
    signal = _iso_date(signal_date, "signal_date")
    return tuple(value.isoformat() for value in next_trade_dates(signal, max(HISTORICAL_REPLAY_HORIZONS) + 1))


def _signal_exclusion_reason(
    signal: _ReplayBar | None,
    history: Sequence[_ReplayBar],
    minimum_history: int,
) -> str | None:
    if signal is None:
        return "signal_bar_missing"
    if len(history) < minimum_history:
        return "minimum_history_not_met"
    try:
        _validate_feature_window(tuple(history[-_MINIMUM_FEATURE_BARS:]), signal.date)
    except HistoricalReplayError:
        return "invalid_or_conflicting_feature_window"
    return None


def _validate_feature_window(rows: Sequence[OHLCVBar], cutoff: str) -> None:
    if len(rows) != _MINIMUM_FEATURE_BARS or rows[-1].date != cutoff:
        raise HistoricalReplayError("公共OHLCV特征窗口必须以D结束且恰好21根")
    if len({row.date for row in rows}) != len(rows):
        raise HistoricalReplayError("公共OHLCV特征窗口日期重复")
    contracts = {(row.adjustment_mode, row.data_version, row.contract_version) for row in rows}
    if len(contracts) != 1 or next(iter(contracts))[0] != "qfq":
        raise HistoricalReplayError("公共OHLCV特征窗口必须是单一可审计qfq契约")
    for row in rows:
        _validate_bar_values(row)


def _validate_bar_values(row: OHLCVBar) -> None:
    values = (row.open, row.close, row.high, row.low, row.volume)
    if any(not math.isfinite(float(value)) for value in values):
        raise HistoricalReplayError("OHLCV bar 包含非有限数")
    if row.open <= 0 or row.close <= 0 or row.low <= 0 or row.volume < 0:
        raise HistoricalReplayError("OHLCV bar 数值范围无效")
    if row.high < max(row.open, row.close, row.low) or row.low > min(row.open, row.close):
        raise HistoricalReplayError("OHLCV bar 包含关系无效")
    if not row.data_version or not row.contract_version:
        raise HistoricalReplayError("OHLCV bar 版本证据缺失")


def _true_ranges(rows: Sequence[OHLCVBar]) -> list[float]:
    return [
        max(current.high - current.low, abs(current.high - previous.close), abs(current.low - previous.close))
        for previous, current in zip(rows, rows[1:], strict=False)
    ]


def _close_location(row: OHLCVBar) -> float:
    spread = row.high - row.low
    return (row.close - row.low) / spread if spread > 0 else 0.5


def _safe_ratio(numerator: float, denominator: float) -> float:
    return numerator / denominator if denominator > 0 else 0.0


def _fixed_bar_status(
    bar: _ReplayBar | None,
    target_date: str,
    maximum_date: str | None,
    role: str,
) -> tuple[str, str]:
    if maximum_date is None or target_date > maximum_date:
        return "not_mature", f"fixed_{role}_not_mature"
    if bar is None:
        return "data_unavailable", f"fixed_{role}_bar_missing_no_shift"
    try:
        _validate_bar_values(bar)
    except HistoricalReplayError:
        return "data_unavailable", f"fixed_{role}_bar_invalid"
    if bar.volume <= 0:
        return "data_unavailable", f"fixed_{role}_suspended_or_zero_volume"
    return "available", f"fixed_{role}_bar"


def _feature_source(rows: Sequence[_ReplayBar], available_history: int) -> dict[str, object]:
    contracts = {(row.data_version, row.contract_version) for row in rows}
    version, contract = next(iter(contracts))
    return {
        "point_in_time_policy": "bar_date_lte_signal_date_no_future_rows",
        "history_start": rows[0].date,
        "history_end": rows[-1].date,
        "feature_bar_count": len(rows),
        "available_history_bar_count": available_history,
        "adjustment_mode": "qfq",
        "data_version": version,
        "contract_version": contract,
        "ohlcv_window_digest": _sha256_json([_bar_contract(row) for row in rows]),
        "contains_fallback_bar": any(row.fallback_used for row in rows),
    }


def _record_limitations(
    feature_rows: Sequence[_ReplayBar],
    entry: Mapping[str, object],
    outcomes: Sequence[Mapping[str, object]],
    config: HistoricalReplayConfig,
) -> list[str]:
    values: list[str] = []
    if any(row.fallback_used for row in feature_rows):
        values.append("feature_window_contains_fallback_bar")
    if entry["status"] != "available":
        values.append(str(entry["reason"]))
    values.extend(str(item["reason"]) for item in outcomes if item["status"] != "modelled")
    if config.symbols:
        values.append("explicit_symbol_subset_replay")
    return list(dict.fromkeys(values))


def _bar_contract(row: _ReplayBar) -> list[object]:
    return [row.date, row.open, row.close, row.high, row.low, row.volume, row.data_version, row.contract_version]


def _bar_digest(row: _ReplayBar) -> str:
    return _sha256_json(_bar_contract(row))


def _cohort_payload() -> dict[str, object]:
    return {
        "mode": HISTORICAL_REPLAY_COHORT_MODE,
        "scope": HISTORICAL_REPLAY_SCOPE,
        "rule_version": HISTORICAL_REPLAY_FEATURE_VERSION,
        "official": False,
        "live_cohort_compatible": False,
    }


def _config_payload(config: HistoricalReplayConfig) -> dict[str, object]:
    payload = asdict(config)
    payload["horizons"] = list(config.horizons)
    payload["symbols"] = list(config.symbols)
    return payload


def _feature_contract() -> dict[str, object]:
    return {
        "version": HISTORICAL_REPLAY_FEATURE_VERSION,
        "feature_names": list(HISTORICAL_REPLAY_FEATURE_NAMES),
        "minimum_feature_bars": _MINIMUM_FEATURE_BARS,
        "availability_cutoff": "date_lte_signal_date",
        "shared_with_official_pit_ohlcv": True,
    }


def _cost_contract(profile: PaperCostProfile, notional: float) -> dict[str, object]:
    return {
        "profile_id": profile.profile_id,
        "version": profile.version,
        "cost_profile": profile.name,
        "execution_notional": notional,
        "quantity_rule": "floor(notional/entry_open/100)*100",
        "components": ["commission", "stamp_tax", "transfer_fee", "slippage"],
    }


def _metadata_contract() -> dict[str, object]:
    return {
        "universe_membership": "unknown_current_qfq_cache_survivors",
        "historical_st_status": None,
        "historical_amount": None,
        "historical_turnover_rate": None,
        "capacity_modelled": False,
    }


def _source_payload(
    path: Path,
    context: _ReplayContext,
    accumulator: _ReplayAccumulator,
    database_range: tuple[str | None, str | None],
    before: Mapping[str, object],
    after: Mapping[str, object],
) -> dict[str, object]:
    return {
        "database": _portable_path(path),
        "sqlite_mode": "ro",
        "sqlite_immutable": True,
        "sqlite_sidecar_policy": _SQLITE_SIDECAR_POLICY,
        "query_only": True,
        "snapshot_transaction": True,
        "adjustment_mode": "qfq",
        "minimum_qfq_date": database_range[0],
        "maximum_qfq_date": database_range[1],
        "source_symbol_count": accumulator.source_symbol_count,
        "universe_symbol_count": context.universe_symbol_count,
        "selected_symbol_count": len(context.selected_symbols),
        "universe_market_counts": dict(context.universe_market_counts),
        "selected_market_counts": dict(context.selected_market_counts),
        "sampling_strategy": context.sampling_strategy,
        "source_row_count": accumulator.source_row_count,
        "database_file_fingerprint_before": dict(before),
        "database_file_fingerprint_after": dict(after),
        "database_concurrent_external_change_detected": before != after,
    }


def _registered_split_defaults() -> dict[str, object]:
    return {
        str(horizon): {
            "minimum_train_sessions": 120,
            "minimum_calibration_sessions": 40,
            "minimum_test_sessions": 60,
            "gap_sessions": horizon + 1,
            "minimum_required_independent_session_count": _minimum_registered_sessions(horizon),
        }
        for horizon in HISTORICAL_REPLAY_HORIZONS
    }


def _superseded_registered_split_defaults() -> dict[str, object]:
    return {
        str(horizon): {
            "minimum_train_sessions": 120,
            "minimum_calibration_sessions": 40,
            "minimum_test_sessions": 60,
            "gap_sessions": horizon,
            "minimum_required_independent_session_count": 220 + 2 * horizon,
        }
        for horizon in HISTORICAL_REPLAY_HORIZONS
    }


def _minimum_registered_sessions(horizon: int) -> int:
    config = ProbabilityConfig(horizon=horizon, target="net_return_positive")
    return (
        config.minimum_train_sessions
        + config.minimum_calibration_sessions
        + config.minimum_test_sessions
        + 2 * config.effective_gap_sessions
    )


def _report_status(quality: Mapping[str, object]) -> str:
    horizons = _mapping(quality.get("horizons"), "quality.horizons")
    h5 = _mapping(horizons.get("5"), "quality.horizons.5")
    return "ready" if int(cast(int, h5.get("modelled_record_count") or 0)) > 0 else "insufficient_data"


def _validate_cohort(cohort: Mapping[str, object]) -> None:
    if cohort != _cohort_payload():
        raise HistoricalReplayError("historical replay cohort 不能伪装 official/live cohort")


def _validated_report_config(config: Mapping[str, object]) -> HistoricalReplayConfig:
    try:
        parsed = HistoricalReplayConfig(
            start_date=str(config["start_date"]), end_date=str(config["end_date"]),
            minimum_history_bars=int(cast(int, config["minimum_history_bars"])),
            horizons=tuple(cast(Sequence[int], config["horizons"])),
            cost_profile=cast(CostProfileName, str(config["cost_profile"])),
            execution_notional=float(cast(float, config["execution_notional"])),
            symbol_limit=int(cast(int, config["symbol_limit"])),
            symbols=tuple(cast(Sequence[str], config["symbols"])),
        )
    except (KeyError, TypeError, ValueError) as exc:
        raise HistoricalReplayError("historical replay config 无效") from exc
    if config != _config_payload(parsed):
        raise HistoricalReplayError("historical replay config 字段或规范化值冲突")
    return parsed


def _validate_source(source: Mapping[str, object], config: HistoricalReplayConfig) -> None:
    required = {
        "database", "sqlite_mode", "sqlite_immutable", "sqlite_sidecar_policy", "query_only",
        "snapshot_transaction", "adjustment_mode",
        "minimum_qfq_date", "maximum_qfq_date", "source_symbol_count", "universe_symbol_count",
        "selected_symbol_count", "universe_market_counts", "selected_market_counts",
        "sampling_strategy", "source_row_count", "database_file_fingerprint_before",
        "database_file_fingerprint_after", "database_concurrent_external_change_detected",
    }
    if set(source) != required:
        raise HistoricalReplayError("historical replay source 字段无效")
    if (
        source["sqlite_mode"],
        source["sqlite_immutable"],
        source["sqlite_sidecar_policy"],
        source["query_only"],
        source["snapshot_transaction"],
    ) != ("ro", True, _SQLITE_SIDECAR_POLICY, True, True):
        raise HistoricalReplayError("historical replay source 不是只读 snapshot")
    if source["adjustment_mode"] != "qfq" or not _safe_portable_database(source["database"]):
        raise HistoricalReplayError("historical replay source qfq/database 契约无效")
    _validate_source_counts(source, config)
    _validate_source_dates(source)
    before = _source_fingerprint(source["database_file_fingerprint_before"])
    after = _source_fingerprint(source["database_file_fingerprint_after"])
    if source["database_concurrent_external_change_detected"] is not (before != after):
        raise HistoricalReplayError("historical replay source 并发变化标记冲突")


def _validate_source_counts(source: Mapping[str, object], config: HistoricalReplayConfig) -> None:
    universe = _nonnegative_integer(source["universe_symbol_count"], "universe_symbol_count")
    selected = _nonnegative_integer(source["selected_symbol_count"], "selected_symbol_count")
    source_symbols = _nonnegative_integer(source["source_symbol_count"], "source_symbol_count")
    source_rows = _nonnegative_integer(source["source_row_count"], "source_row_count")
    universe_markets = _market_count_mapping(source["universe_market_counts"], "universe_market_counts")
    selected_markets = _market_count_mapping(source["selected_market_counts"], "selected_market_counts")
    if sum(universe_markets.values()) != universe or sum(selected_markets.values()) != selected:
        raise HistoricalReplayError("historical replay source market counts 冲突")
    if not 0 < selected <= min(universe, config.symbol_limit) or source_symbols > selected:
        raise HistoricalReplayError("historical replay source symbol counts 冲突")
    if source_rows < source_symbols:
        raise HistoricalReplayError("historical replay source row count 冲突")
    expected = (
        "explicit_symbol_list_available_qfq_only"
        if config.symbols else "deterministic_sha256_balanced_round_robin_SH_SZ_BJ_v1"
    )
    if source["sampling_strategy"] != expected:
        raise HistoricalReplayError("historical replay source sampling strategy 冲突")


def _validate_source_dates(source: Mapping[str, object]) -> None:
    minimum, maximum = source["minimum_qfq_date"], source["maximum_qfq_date"]
    if minimum is None and maximum is None:
        return
    if not isinstance(minimum, str) or not isinstance(maximum, str):
        raise HistoricalReplayError("historical replay source qfq date range 无效")
    if _iso_date(minimum, "minimum_qfq_date") > _iso_date(maximum, "maximum_qfq_date"):
        raise HistoricalReplayError("historical replay source qfq date range 逆序")


def _source_fingerprint(value: object) -> dict[str, int]:
    mapped = _mapping(value, "database_file_fingerprint")
    if set(mapped) != {"size", "mtime_ns"}:
        raise HistoricalReplayError("historical replay source fingerprint 字段无效")
    return {
        key: _nonnegative_integer(mapped[key], f"database_file_fingerprint.{key}")
        for key in ("size", "mtime_ns")
    }


def _market_count_mapping(value: object, label: str) -> dict[str, int]:
    mapped = _mapping(value, label)
    markets = ("SH", "SZ", "BJ", "OTHER")
    if set(mapped) != set(markets):
        raise HistoricalReplayError(f"{label} 市场字段无效")
    return {market: _nonnegative_integer(mapped[market], f"{label}.{market}") for market in markets}


def _nonnegative_integer(value: object, label: str) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or value < 0:
        raise HistoricalReplayError(f"{label} 必须是非负整数")
    return value


def _safe_portable_database(value: object) -> bool:
    if not isinstance(value, str) or not value.strip():
        return False
    path = Path(value)
    return not path.is_absolute() and ".." not in path.parts


def _validate_integrity(integrity: Mapping[str, object], artifact: Mapping[str, object]) -> None:
    if set(integrity) != {"algorithm", "integrity_digest", "notice"}:
        raise HistoricalReplayError("historical replay integrity 字段无效")
    if integrity.get("algorithm") != "sha256" or integrity.get("notice") != _INTEGRITY_NOTICE:
        raise HistoricalReplayError("historical replay integrity contract 无效")
    unsigned = {key: value for key, value in artifact.items() if key != "integrity"}
    if integrity.get("integrity_digest") != _sha256_json(unsigned):
        raise HistoricalReplayError("historical replay integrity digest 不一致")


def _validate_record_digest(record: Mapping[str, object]) -> None:
    unsigned = {key: value for key, value in record.items() if key != "record_digest"}
    if record.get("record_digest") != _sha256_json(unsigned):
        raise HistoricalReplayError("historical replay record digest 不一致")


def _database_qfq_range(
    conn: sqlite3.Connection,
    symbols: Sequence[str],
) -> tuple[str | None, str | None]:
    clauses = ["adjustment_mode = 'qfq'", f"symbol IN ({','.join('?' for _ in symbols)})"]
    row = conn.execute(
        f"SELECT MIN(date), MAX(date) FROM kline_daily WHERE {' AND '.join(clauses)}",
        list(symbols),
    ).fetchone()
    return (
        str(row[0]) if row is not None and row[0] is not None else None,
        str(row[1]) if row is not None and row[1] is not None else None,
    )


@contextmanager
def _readonly_connection(path: Path) -> Iterator[sqlite3.Connection]:
    _require_static_sqlite_source(path)
    try:
        conn = sqlite3.connect(f"{path.as_uri()}?mode=ro&immutable=1", uri=True)
    except sqlite3.Error as exc:
        raise HistoricalReplayError(f"只读 SQLite 无法打开：{path}") from exc
    try:
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA query_only = ON")
        conn.execute("BEGIN")
        yield conn
        conn.rollback()
    except BaseException as exc:
        try:
            conn.close()
        except BaseException as close_error:
            raise exc from close_error
        raise
    else:
        conn.close()
        _require_static_sqlite_source(path)


def _require_static_sqlite_source(path: Path) -> None:
    if not path.is_file():
        raise HistoricalReplayError(f"historical replay 静态 SQLite 不存在：{path}")
    present = [
        suffix
        for suffix in _SQLITE_SIDECAR_SUFFIXES
        if (candidate := Path(f"{path}{suffix}")).exists() or candidate.is_symlink()
    ]
    if present:
        raise HistoricalReplayError(
            "historical replay 静态 SQLite 存在 sidecar，拒绝未检查点或活动数据库："
            + ",".join(present),
        )


def _records(payload: Mapping[str, object]) -> list[Mapping[str, object]]:
    records = payload.get("records")
    if not isinstance(records, list) or any(not isinstance(item, Mapping) for item in records):
        raise HistoricalReplayError("historical replay records 无效")
    return cast(list[Mapping[str, object]], records)


def _outcome_for(record: Mapping[str, object], horizon: int) -> Mapping[str, object]:
    outcomes = record.get("outcomes")
    if not isinstance(outcomes, list):
        raise HistoricalReplayError("historical replay outcomes 缺失")
    for outcome in outcomes:
        if isinstance(outcome, Mapping) and outcome.get("horizon") == horizon:
            return outcome
    raise HistoricalReplayError(f"historical replay H{horizon} outcome 缺失")


def _mapping(value: object, label: str) -> dict[str, object]:
    if not isinstance(value, Mapping):
        raise HistoricalReplayError(f"{label} 必须是 object")
    return dict(value)


def _finite_number(value: object, label: str) -> float:
    if isinstance(value, bool) or not isinstance(value, int | float):
        raise HistoricalReplayError(f"{label} 必须是有限数")
    parsed = float(value)
    if not math.isfinite(parsed):
        raise HistoricalReplayError(f"{label} 必须是有限数")
    return parsed


def _positive_integer(value: object, label: str) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or value <= 0:
        raise HistoricalReplayError(f"{label} 必须是正整数")
    return value


def _iso_date(value: str, label: str) -> date:
    try:
        return date.fromisoformat(value)
    except ValueError as exc:
        raise ValueError(f"{label} 必须是 YYYY-MM-DD") from exc


def _file_fingerprint(path: Path) -> dict[str, object]:
    facts = path.stat()
    return {"size": facts.st_size, "mtime_ns": facts.st_mtime_ns}


def _portable_path(path: Path) -> str:
    try:
        return path.relative_to(Path.cwd().resolve()).as_posix()
    except ValueError:
        return path.name


def _json_copy(value: Mapping[str, object]) -> dict[str, object]:
    _validate_json_tree(value, "json")
    return cast(dict[str, object], json.loads(canonical_historical_replay_json(value)))


def _validate_json_tree(value: object, label: str) -> None:
    if value is None or isinstance(value, str | bool | int):
        return
    if isinstance(value, float):
        if not math.isfinite(value):
            raise HistoricalReplayError(f"{label} 包含非有限数")
        return
    if isinstance(value, list | tuple):
        for index, item in enumerate(value):
            _validate_json_tree(item, f"{label}[{index}]")
        return
    if isinstance(value, Mapping):
        for key, item in value.items():
            if not isinstance(key, str):
                raise HistoricalReplayError(f"{label} 包含非字符串key")
            _validate_json_tree(item, f"{label}.{key}")
        return
    raise HistoricalReplayError(f"{label} 包含不可序列化类型")


def _sha256_json(value: object) -> str:
    return hashlib.sha256(canonical_historical_replay_json(value).encode("utf-8")).hexdigest()


def _reject_database_target(target: Path, database: Path) -> None:
    if target == database:
        raise HistoricalReplayError("historical replay artifact 不能覆盖 SQLite")
    try:
        if target.exists() and database.exists() and os.path.samefile(target, database):
            raise HistoricalReplayError("historical replay artifact 不能硬链接 SQLite")
    except OSError as exc:
        raise HistoricalReplayError("无法验证 artifact 与 SQLite 路径隔离") from exc
__all__ = [
    "HISTORICAL_REPLAY_ARTIFACT_SCHEMA_VERSION",
    "HISTORICAL_REPLAY_COHORT_MODE",
    "HISTORICAL_REPLAY_FEATURE_NAMES",
    "HISTORICAL_REPLAY_HORIZONS",
    "HISTORICAL_REPLAY_SCHEMA_VERSION",
    "HistoricalReplayConfig",
    "HistoricalReplayError",
    "build_historical_replay_artifact",
    "canonical_historical_replay_json",
    "evaluate_market_scan_probability_replay",
    "forecast_historical_replay_shadow",
    "historical_replay_artifact_filename",
    "historical_replay_feature_values",
    "load_historical_replay_artifact",
    "replay_rows_to_probability_samples",
    "verify_historical_replay_artifact",
    "write_historical_replay_artifact",
]
