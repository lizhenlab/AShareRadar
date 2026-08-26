"""Separate personal probability view; no production ranks or SQLite writes."""

from __future__ import annotations

from collections import Counter
from collections.abc import Sequence
from datetime import date, timedelta
import math
from pathlib import Path
import sqlite3
from typing import Any, Literal, TypeAlias, cast

from app.artifacts.io import canonical_json_bytes, path_has_only_trusted_aliases, sha256_hex
from app.db.market_mappers import row_to_kline
from app.models.market_scan import MarketScanResultItem, MarketScanRun
from app.repositories.market_scan_experimental import ExperimentalCandidate
from app.services.experimental_probability_model import (
    MODEL_DIRECTORY, ExperimentalEstimator, ExperimentalPredictionKind, ExperimentalProbabilityUnavailable,
    experimental_definition, experimental_probability, load_experimental_model,
)
from app.services.market_scan_probability_replay import OHLCVBar, historical_replay_feature_values
from app.services.trading_calendar import next_trade_dates, trading_date_range
from app.utils.clock import market_now


ExperimentalSort = Literal["probability", "base_rank"]
Candidate: TypeAlias = MarketScanResultItem | ExperimentalCandidate


def experimental_results(database: Path, run: MarketScanRun, items: Sequence[Candidate], *,
                         prediction_kind: ExperimentalPredictionKind = "net_h5",
                         minimum: float | None = None, market: str | None = None, keyword: str = "",
                         sort: ExperimentalSort = "probability", page: int = 1, page_size: int = 50) -> dict[str, Any]:
    _validate_query(minimum, market, keyword, sort, page, page_size)
    definition = experimental_definition(prediction_kind)
    estimator, digest = load_experimental_model(database.parent / MODEL_DIRECTORY, prediction_kind=prediction_kind)
    _validate_signal_date(estimator, run.data_date)
    target_date = next_trade_dates(date.fromisoformat(run.data_date), definition["target_offset"])[-1].isoformat()
    records, missing, input_digest = _current_predictions(database, run.data_date, items, estimator)
    records.sort(key=lambda row: (-row["probability"], row["symbol"]))
    for rank, record in enumerate(records, start=1):
        record["experimental_rank"] = rank
    filtered = _filter_records(records, minimum=minimum, market=market, keyword=keyword, sort=sort)
    evaluation = estimator.historical_recipe_evaluation
    offset = (page - 1) * page_size
    return {
        "schema_version": "market-scan-personal-experimental-probability-v1",
        "run_id": run.id, "mode": "personal_experimental", "experimental": True,
        "formal_filter_qualified": False, "production_ranking_effect": "none",
        "base_snapshot_digest": run.snapshot_digest, "base_rule_version": run.rule_version,
        "signal_date": run.data_date, "generated_at": market_now().isoformat(),
        "prediction_kind": prediction_kind, "horizon": estimator.horizon, "target": estimator.target,
        "reference": definition["reference"], "target_session_offset": definition["target_offset"],
        "target_session_date": target_date,
        "probability_label": definition["label"], "warning": definition["warning"],
        "input_digest": input_digest, "model_digest": digest,
        "model": {
            "generated_at": estimator.generated_at, "latest_label_date": estimator.latest_label_date,
            "expires_after_signal_date": estimator.expires_after_signal_date, "base_rate": estimator.base_rate,
            "train_session_count": estimator.train_session_count, "calibration_session_count": estimator.calibration_session_count,
            "train_record_count": estimator.train_record_count, "calibration_record_count": estimator.calibration_record_count,
            "training_symbol_count": len(estimator.training_symbols),
            "source_filename": estimator.source_filename, "source_integrity_digest": estimator.source_integrity_digest,
            "historical_recipe_evaluation": evaluation,
            "direction_evidence": estimator.direction_evidence.model_dump() if estimator.direction_evidence else None,
        },
        "coverage": {"successful_scan_count": len(items), "predicted_count": len(records),
                     "unavailable_count": sum(missing.values()), "unavailable_reasons": dict(missing)},
        "total": len(filtered), "page": page, "page_size": page_size,
        "page_count": (len(filtered) + page_size - 1) // page_size,
        "filters": {"prediction_kind": prediction_kind, "min_probability": minimum, "market": market, "keyword": keyword, "sort": sort},
        "items": filtered[offset:offset + page_size],
    }


def _validate_query(minimum: float | None, market: str | None, keyword: str,
                    sort: str, page: int, page_size: int) -> None:
    if minimum is not None and (isinstance(minimum, bool) or not math.isfinite(minimum) or not 0 <= minimum <= 1):
        raise ExperimentalProbabilityUnavailable("实验概率阈值必须在 0 到 1 之间")
    if market not in {None, "SH", "SZ", "BJ"} or sort not in {"probability", "base_rank"} or len(keyword) > 80:
        raise ExperimentalProbabilityUnavailable("实验筛选条件无效")
    if type(page) is not int or type(page_size) is not int or page < 1 or not 1 <= page_size <= 200:
        raise ExperimentalProbabilityUnavailable("实验分页条件无效")


def _validate_signal_date(estimator: ExperimentalEstimator, signal_date: str) -> None:
    signal = date.fromisoformat(signal_date)
    current = market_now()
    if signal > current.date() or (signal == current.date() and current.hour < 15):
        raise ExperimentalProbabilityUnavailable("实验模型仅使用已收盘日线，盘中当日日线尚不可用")
    if signal_date <= estimator.latest_label_date:
        raise ExperimentalProbabilityUnavailable("此批次早于实验模型标签截止日，禁止用未来训练结果回填历史概率")
    if signal_date > estimator.expires_after_signal_date:
        raise ExperimentalProbabilityUnavailable("实验模型已过期，需要用更新历史重新构建；未自动延期")


def _current_predictions(database: Path, signal_date: str, items: Sequence[Candidate],
                         estimator: ExperimentalEstimator) -> tuple[list[dict[str, Any]], Counter[str], str]:
    if len(items) > 10000 or not path_has_only_trusted_aliases(database):
        raise ExperimentalProbabilityUnavailable("实验行情源路径或批次数量无效")
    start = (date.fromisoformat(signal_date) - timedelta(days=90)).isoformat()
    expected_dates = None
    if estimator.target == "close_return_positive":
        sessions, _status = trading_date_range(date.fromisoformat(start), date.fromisoformat(signal_date))
        expected_dates = tuple(day.isoformat() for day in sessions[-21:])
    records: list[dict[str, Any]] = []
    missing: Counter[str] = Counter()
    fingerprints: list[dict[str, Any]] = []
    training_symbols = set(estimator.training_symbols)
    connection = sqlite3.connect(database.absolute().as_uri() + "?mode=ro", uri=True)
    connection.row_factory = sqlite3.Row
    try:
        connection.execute("PRAGMA query_only=ON")
        connection.execute("BEGIN")
        for item in items:
            raw_rows = connection.execute(
                "SELECT symbol,adjustment_mode,date,open,close,high,low,volume,as_of,"
                "data_version,contract_version,fallback_used,source,fetched_at FROM kline_daily "
                "WHERE symbol=? AND adjustment_mode='qfq' AND date BETWEEN ? AND ? ORDER BY date DESC LIMIT 21",
                (item.symbol, start, signal_date),
            ).fetchall()
            fingerprints.append({"symbol": item.symbol, "sha256": sha256_hex(canonical_json_bytes([dict(row) for row in raw_rows]))})
            record, reason = _predict_item(item, raw_rows, signal_date, estimator, training_symbols, expected_dates)
            if record is None:
                missing[reason] += 1
            else:
                records.append(record)
    finally:
        connection.close()
    return records, missing, sha256_hex(canonical_json_bytes(fingerprints))


def _predict_item(item: Candidate, raw_rows: Sequence[sqlite3.Row], signal_date: str,
                  estimator: ExperimentalEstimator, training_symbols: set[str],
                  expected_dates: tuple[str, ...] | None = None) -> tuple[dict[str, Any] | None, str]:
    if len(raw_rows) != 21 or raw_rows[0]["date"] != signal_date:
        return None, "missing_exact_date_or_21_bars"
    if expected_dates is not None and tuple(row["date"] for row in reversed(raw_rows)) != expected_dates:
        return None, "missing_fixed_session_window"
    try:
        bars = [row_to_kline(row) for row in reversed(raw_rows)]
        if bars[-1].volume <= 0:
            return None, "no_volume_on_signal_date"
        values = historical_replay_feature_values(cast(Sequence[OHLCVBar], bars), signal_date=signal_date)
        probability = experimental_probability(estimator, values)
    except ExperimentalProbabilityUnavailable:
        return None, "feature_out_of_distribution"
    except (ValueError, TypeError):
        return None, "invalid_or_mixed_history_contract"
    return {
        "symbol": item.symbol, "name": item.name, "market": item.market,
        "probability": probability, "base_rank": item.rank, "base_score": item.score,
        "base_raw_score": item.raw_score, "training_universe_member": item.symbol in training_symbols,
    }, ""


def _filter_records(records: list[dict[str, Any]], *, minimum: float | None, market: str | None,
                    keyword: str, sort: str) -> list[dict[str, Any]]:
    needle = keyword.strip().casefold()
    result = [row for row in records if (minimum is None or row["probability"] >= minimum)
              and (market is None or row["market"] == market)
              and (not needle or needle in f'{row["symbol"]} {row["name"]}'.casefold())]
    if sort == "base_rank":
        result.sort(key=lambda row: (row["base_rank"] or 100000, row["symbol"]))
    return result
