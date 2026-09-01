"""Read-only, reproducible Choice/Tencent adjustment diagnostics, never authority.

The comparison deliberately reconstructs prices independently of the Choice
history converter. Matching direction labels does not establish matching price
features, volume conventions, fitted probabilities, or executable returns.
"""

from __future__ import annotations

from collections import Counter, defaultdict
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, replace
from itertools import groupby
import math
from pathlib import Path
import sqlite3
from statistics import fmean, median
from typing import Any, cast

from app.artifacts.io import canonical_json_bytes, decode_json_bytes, path_has_only_trusted_aliases, read_regular_file, sha256_hex
from app.services.choice_experimental_history import require_offline_choice_calendar
from app.services.choice_research import DAILY_FIELDS, OPTIONS, make_plan, normalize, request
from app.services.choice_research_store import ChoiceDataset
from app.services.choice_sdk import ChoiceError
from app.services.market_scan_probability_history import load_market_scan_probability_history_manifest
from app.services.market_scan_probability_replay import HISTORICAL_REPLAY_FEATURE_NAMES, OHLCVBar, historical_replay_feature_values


COMPARISON_SCHEMA = "choice-tencent-history-comparison-v1"
MAX_FILE_BYTES = 256 * 1024 * 1024
MAX_CHOICE_ROWS = 100_000
EXAMPLE_LIMIT = 10
TENCENT_VOLUME_TO_SHARES = 100.0
_PRICE_FIELDS = ("open", "high", "low", "close")
_TRADED_QUALITIES = frozenset({"traded_bar_not_execution_proof", "single_price_limit_up", "single_price_limit_down", "single_price_unknown"})
LIMITATIONS = (
    "retrospective_download_and_reconstruction_not_original_point_in_time_vintage",
    "internal_factor_reference_consistency_is_not_vendor_delivered_qfq_validation",
    "overlap_cohort_is_not_a_representative_market_or_provider_equivalence_test",
    "direction_label_agreement_does_not_establish_feature_or_calibration_equivalence",
    "volume_multiplier_is_a_declared_comparison_convention_not_proof_of_identical_volume_scope",
    "constant_anchor_cancellation_does_not_cover_revised_past_factors_or_corporate_action_records",
    "price_scale_invariance_does_not_apply_to_board_lot_affordability_or_rounded_minimum_commission_labels",
    "no_fitting_no_execution_claim_no_runtime_model_or_production_authorization",
)
RawSeries = dict[str, dict[str, dict[str, Any]]]


@dataclass(frozen=True)
class _Bar:
    date: str
    open: float
    high: float
    low: float
    close: float
    volume: float
    adjustment_mode: str = "qfq"
    data_version: str = "comparison-only-retrospective"
    contract_version: str = "comparison-only-ohlcv-v1"


def _safe_path(path: Path) -> Path:
    path = path.expanduser().absolute()
    if ".workbuddy-ai" in path.parts or not path_has_only_trusted_aliases(path):
        raise ChoiceError("comparison refuses protected paths or untrusted aliases")
    return path


def _file_digest(path: Path, *, database: bool = False) -> str:
    path = _safe_path(path)
    if database and any(Path(str(path) + suffix).exists() or Path(str(path) + suffix).is_symlink()
                        for suffix in ("-wal", "-shm", "-journal")):
        raise ChoiceError("comparison requires static databases without sidecars")
    content = read_regular_file(path, max_bytes=MAX_FILE_BYTES)
    if database and (not content.startswith(b"SQLite format 3\x00") or content[18:20] != b"\x01\x01"):
        raise ChoiceError("comparison requires rollback-journal SQLite archives")
    return sha256_hex(content)


def _choice_plan(plan: dict[str, Any]) -> None:
    if plan.get("schema_version") != "choice-research-dataset-v1":
        raise ChoiceError("comparison requires a base Choice research archive")
    rebuilt = make_plan(plan["start_date"], plan["end_date"], plan["symbol_limit"], plan["preferred_symbols"], plan["event_symbols"])
    if rebuilt != plan:
        raise ChoiceError("comparison Choice plan does not match the registered raw/shares contract")


def _daily_descriptor(descriptor: dict[str, Any]) -> tuple[list[str], list[str]]:
    symbols, sessions = descriptor.get("symbols"), descriptor.get("sessions")
    if not isinstance(symbols, list) or not 1 <= len(symbols) <= 20 or len(set(symbols)) != len(symbols):
        raise ChoiceError("invalid comparison daily symbol batch")
    if not isinstance(sessions, list) or not 1 <= len(sessions) <= 800 or sessions != sorted(set(sessions)):
        raise ChoiceError("invalid comparison daily session grid")
    expected = request("daily", "csd", [",".join(symbols), ",".join(DAILY_FIELDS), sessions[0], sessions[-1],
        f"Period=1,AdjustFlag=1,FillData=0,Order=1,{OPTIONS}"], symbols=symbols, fields=DAILY_FIELDS, sessions=sessions)
    if descriptor != expected:
        raise ChoiceError("comparison refuses changed raw adjustment, fill, or field options")
    return symbols, sessions


def _verify_daily_receipt(source: ChoiceDataset, receipt: tuple[Any, ...], fingerprints: dict[Path, str]) -> list[Any]:
    key, encoded, raw_digest, captured_at, records_digest, record_count = receipt
    descriptor = decode_json_bytes(encoded.encode())
    if not isinstance(descriptor, dict) or ChoiceDataset.key(descriptor) != key:
        raise ChoiceError("comparison daily receipt descriptor mismatch")
    _daily_descriptor(descriptor)
    raw = source.cached(descriptor)
    if raw is None or raw["captured_at"] != captured_at:
        raise ChoiceError("comparison daily raw archive is missing or changed")
    expected = [list(row) for row in normalize(raw)]
    stored = [[kind, symbol, day, decode_json_bytes(payload.encode())] for kind, symbol, day, payload in source.db.execute(
        "SELECT kind,symbol,as_of,payload_json FROM records WHERE request_key=? ORDER BY ordinal", (key,))]
    if len(stored) != len(expected) or len(expected) != record_count:
        raise ChoiceError("comparison daily record count mismatch")
    if any(sha256_hex(canonical_json_bytes(value)) != records_digest for value in (expected, stored)):
        raise ChoiceError("comparison daily normalized records disagree with the raw replay")
    fingerprints[source.directory / "raw" / f"{key}.json"] = raw_digest
    return expected


def _read_choice(directory: Path, fingerprints: dict[Path, str]) -> tuple[RawSeries, tuple[str, ...], dict[str, Any]]:
    directory = _safe_path(directory)
    fingerprints[directory / "plan.json"] = _file_digest(directory / "plan.json")
    series: RawSeries = defaultdict(dict)
    with ChoiceDataset.open_readonly(directory) as source:
        source.db.execute("BEGIN")
        _choice_plan(source.plan)
        count = source.db.execute("SELECT COUNT(*) FROM records WHERE kind='daily'").fetchone()[0]
        if not 1 <= count <= MAX_CHOICE_ROWS:
            raise ChoiceError("comparison daily row count is empty or exceeds its bound")
        receipts = source.db.execute("SELECT * FROM requests WHERE json_extract(descriptor_json,'$.kind')='daily' ORDER BY request_key").fetchall()
        if not 1 <= len(receipts) <= 12:
            raise ChoiceError("comparison daily request count is empty or exceeds its bound")
        for receipt in receipts:
            for _kind, symbol, day, values in _verify_daily_receipt(source, receipt, fingerprints):
                if day in series[symbol]:
                    raise ChoiceError("comparison refuses duplicate symbol/session observations")
                series[symbol][day] = values
        sessions = tuple(sorted(next(iter(series.values()))))
        if any(tuple(sorted(values)) != sessions for values in series.values()):
            raise ChoiceError("comparison daily requests do not share one complete session grid")
        if len(series) != source.plan["symbol_limit"] or sum(map(len, series.values())) != count:
            raise ChoiceError("comparison source daily grid is incomplete or contains orphan rows")
        if not source.plan["start_date"] <= sessions[0] <= sessions[-1] <= source.plan["end_date"]:
            raise ChoiceError("comparison daily grid is outside the saved plan")
    return dict(series), sessions, {"daily_raw_requests_verified": len(receipts), "daily_rows_verified": count,
        "symbols": len(series), "sessions": len(sessions), "daily_only_replay_not_full_archive_verification": True}


def _read_tencent(database: Path, manifest_path: Path, symbols: Sequence[str]) -> tuple[dict[str, dict[str, _Bar]], dict[str, Any]]:
    manifest = load_market_scan_probability_history_manifest(manifest_path, database_path=database)
    payload = cast(dict[str, Any], manifest["payload"])
    facts = payload["database"]
    series: dict[str, dict[str, _Bar]] = defaultdict(dict)
    connection = sqlite3.connect(database.as_uri() + "?mode=ro&immutable=1", uri=True)
    try:
        connection.execute("PRAGMA query_only=ON")
        connection.row_factory = sqlite3.Row
        query = f"SELECT * FROM kline_daily WHERE symbol IN ({','.join('?' for _ in symbols)}) ORDER BY symbol,date"
        for row in connection.execute(query, tuple(symbols)):
            series[row["symbol"]][row["date"]] = _Bar(**{name: row[name] for name in (
                "date", *_PRICE_FIELDS, "volume", "adjustment_mode", "data_version", "contract_version")})
    finally:
        connection.close()
    return dict(series), {"manifest_digest": cast(dict[str, Any], manifest["integrity"])["integrity_digest"],
        "database_sha256": facts["sha256"], "symbols": facts["symbol_count"], "rows": facts["row_count"],
        "start": facts["bar_start"], "end": facts["bar_end"], "deep_manifest_verified": True}


def _positive(value: Any) -> bool:
    return type(value) in {int, float} and math.isfinite(value) and value > 0


def _traded(row: Mapping[str, Any]) -> bool:
    return row.get("quality") in _TRADED_QUALITIES and all(_positive(row.get(name)) for name in (
        "OPEN", "HIGH", "LOW", "CLOSE", "TAFACTOR", "VOLUME", "AMOUNT"))


def _adjusted(row: Mapping[str, Any], day: str, anchor: float) -> _Bar:
    ratio = row["TAFACTOR"] / anchor
    prices = {name: row[name.upper()] * ratio for name in _PRICE_FIELDS}
    if not _positive(ratio) or not all(_positive(value) for value in prices.values()):
        raise ChoiceError("comparison reconstruction overflowed or underflowed")
    return _Bar(date=day, **prices, volume=float(row["VOLUME"]))


def _distribution(values: Sequence[float]) -> dict[str, float | int | None]:
    ordered = sorted(values)
    if any(not math.isfinite(value) for value in ordered):
        raise ChoiceError("comparison metric is non-finite")
    if not ordered:
        return {"count": 0, "min": None, "median": None, "p95": None, "max": None, "mean": None}
    return {"count": len(ordered), "min": ordered[0], "median": median(ordered),
        "p95": ordered[math.ceil(.95 * len(ordered)) - 1], "max": ordered[-1], "mean": fmean(ordered)}


def _factor_pair(symbol: str, day: str, previous: Mapping[str, Any], current: Mapping[str, Any]) -> dict[str, Any] | None:
    if not all(_positive(value) for value in (previous.get("CLOSE"), current.get("PRECLOSE"), previous.get("TAFACTOR"), current.get("TAFACTOR"))):
        return None
    actual = current["TAFACTOR"] / previous["TAFACTOR"]
    expected = previous["CLOSE"] / current["PRECLOSE"]
    if not _positive(actual) or not _positive(expected):
        raise ChoiceError("comparison factor ratio overflowed or underflowed")
    return {"symbol": symbol, "date": day, "previous_close": previous["CLOSE"], "preclose": current["PRECLOSE"],
        "previous_factor": previous["TAFACTOR"], "factor": current["TAFACTOR"], "factor_ratio": actual,
        "reference_ratio": expected, "ratio_error_ppm": (actual / expected - 1) * 1e6,
        "inverse_ratio_error_ppm": (1 / actual / expected - 1) * 1e6,
        "price_residual_yuan": previous["CLOSE"] / actual - current["PRECLOSE"]}


def factor_reference_comparison(series: RawSeries, sessions: Sequence[str]) -> dict[str, Any]:
    pairs = [result for symbol, rows in sorted(series.items()) for previous, day in zip(sessions, sessions[1:], strict=False)
             if previous in rows and day in rows and (result := _factor_pair(symbol, day, rows[previous], rows[day])) is not None]
    changed = [row for row in pairs if row["factor"] != row["previous_factor"]]
    return {"valid_adjacent_pairs": len(pairs), "factor_changes": len(changed),
        "changed_symbol_count": len({row["symbol"] for row in changed}),
        "change_market_counts": dict(sorted(Counter(row["symbol"][-2:] for row in changed).items())),
        "factor_ratio": _distribution([row["factor_ratio"] for row in changed]),
        "abs_ratio_error_ppm_changed": _distribution([abs(row["ratio_error_ppm"]) for row in changed]),
        "abs_inverse_ratio_error_ppm_changed": _distribution([abs(row["inverse_ratio_error_ppm"]) for row in changed]),
        "abs_price_residual_yuan_all": _distribution([abs(row["price_residual_yuan"]) for row in pairs]),
        "abs_price_residual_yuan_changed": _distribution([abs(row["price_residual_yuan"]) for row in changed]),
        "largest_residual_examples": sorted(changed, key=lambda row: abs(row["price_residual_yuan"]), reverse=True)[:EXAMPLE_LIMIT],
        "interpretation": "observational_consistency_only_not_native_qfq_or_execution_attestation"}


def _price_volume_comparison(raw: dict[str, dict[str, Any]], adjusted: dict[str, _Bar], tencent: dict[str, _Bar],
                             dates: Sequence[str], scale: float) -> dict[str, Any]:
    price_errors: list[float] = []
    close_bps: list[float] = []
    volume_ratios: list[float] = []
    volume_bps: list[float] = []
    volume_examples: list[dict[str, Any]] = []
    for day, choice in adjusted.items():
        other = tencent[day]
        price_errors.extend(abs(getattr(choice, name) - getattr(other, name) * scale) for name in _PRICE_FIELDS)
        close_bps.append(abs(choice.close / (other.close * scale) - 1) * 1e4)
        if _positive(other.volume):
            volume_ratios.append(choice.volume / other.volume)
            difference = choice.volume - other.volume * TENCENT_VOLUME_TO_SHARES
            relative = difference / (other.volume * TENCENT_VOLUME_TO_SHARES)
            volume_bps.append(abs(relative) * 1e4)
            volume_examples.append({"date": day, "choice_shares": choice.volume, "tencent_volume": other.volume,
                "difference_shares": difference, "relative_difference_pct": relative * 100})
    returns = [abs(adjusted[day].close / adjusted[previous].close - tencent[day].close / tencent[previous].close) * 1e4
               for previous, day in zip(dates, dates[1:], strict=False) if previous in adjusted and day in adjusted]
    return {"ohlc_abs_yuan": _distribution(price_errors), "close_relative_bps": _distribution(close_bps),
        "adjacent_session_return_diff_bps": _distribution(returns), "choice_volume_over_tencent": _distribution(volume_ratios),
        "volume_abs_difference_bps_after_x100": _distribution(volume_bps),
        "volume_over_100_shares_disagreement_count": sum(abs(row["difference_shares"]) > 100 for row in volume_examples),
        "volume_over_one_percent_disagreement_count": sum(abs(row["relative_difference_pct"]) > 1 for row in volume_examples),
        "largest_volume_differences": sorted(volume_examples, key=lambda row: abs(row["relative_difference_pct"]), reverse=True)[:EXAMPLE_LIMIT],
        "constant_offset_segments": _offset_segments(raw, adjusted, tencent, scale)}


def _offset_segments(raw: dict[str, dict[str, Any]], adjusted: dict[str, _Bar], tencent: dict[str, _Bar], scale: float) -> list[dict[str, Any]]:
    segments = []
    for factor, group in groupby(sorted(adjusted), key=lambda day: raw[day]["TAFACTOR"]):
        dates = list(group)
        differences = [raw[day][name.upper()] - getattr(tencent[day], name) * scale for day in dates for name in _PRICE_FIELDS]
        offset = median(differences)
        segments.append({"factor": factor, "start": dates[0], "end": dates[-1], "observations": len(dates),
            "raw_minus_scaled_tencent_median": offset,
            "constant_offset_max_abs_residual": max(abs(value - offset) for value in differences)})
    return segments


def _vector(rows: Sequence[_Bar]) -> tuple[float, ...]:
    return historical_replay_feature_values(cast(Sequence[OHLCVBar], rows), signal_date=rows[-1].date)


def _feature_comparison(symbol: str, choice: dict[str, _Bar], tencent: dict[str, _Bar], dates: Sequence[str],
                        differences: dict[str, list[float]], worst: dict[str, dict[str, Any]]) -> dict[str, Any]:
    compared = 0
    for index in range(20, len(dates)):
        window = dates[index - 20:index + 1]
        if not all(day in choice and day in tencent for day in window):
            continue
        left, right = _vector([choice[day] for day in window]), _vector([tencent[day] for day in window])
        for name, a, b in zip(HISTORICAL_REPLAY_FEATURE_NAMES, left, right, strict=True):
            difference = abs(a - b)
            differences[name].append(difference)
            if difference > worst.get(name, {}).get("absolute_difference", -1):
                worst[name] = {"symbol": symbol, "signal_date": dates[index], "choice": a, "tencent": b, "absolute_difference": difference}
        compared += 1
    return {"compared_fixed_session_windows": compared, "missing_or_restricted_windows": max(0, len(dates) - 20) - compared}


def _direction_comparison(choice: dict[str, _Bar], tencent: dict[str, _Bar], dates: Sequence[str]) -> dict[str, Any]:
    result = {}
    for horizon in (1, 2, 5):
        examples: list[dict[str, Any]] = []
        return_errors: list[float] = []
        compared = disagreements = 0
        for index in range(60, len(dates) - horizon):
            signal, target = dates[index], dates[index + horizon]
            required = [*dates[index - 20:index + 1], target]
            if not all(day in choice and day in tencent for day in required):
                continue
            compared += 1
            c_up, t_up = choice[target].close > choice[signal].close, tencent[target].close > tencent[signal].close
            c_return, t_return = choice[target].close / choice[signal].close - 1, tencent[target].close / tencent[signal].close - 1
            return_errors.append(abs(c_return - t_return) * 1e4)
            if c_up != t_up:
                disagreements += 1
                if len(examples) < EXAMPLE_LIMIT:
                    examples.append({"signal_date": signal, "target_date": target, "choice_return": c_return, "tencent_return": t_return})
        result[str(horizon)] = {"compared": compared, "sign_disagreements": disagreements,
            "return_difference_bps": _distribution(return_errors), "examples": examples}
    return result


def _shared_comparison(choice: RawSeries, tencent: dict[str, dict[str, _Bar]], sessions: Sequence[str]) -> dict[str, Any]:
    differences: dict[str, list[float]] = {name: [] for name in HISTORICAL_REPLAY_FEATURE_NAMES}
    worst: dict[str, dict[str, Any]] = {}
    comparisons: list[dict[str, Any]] = []
    for symbol in sorted(set(choice) & set(tencent)):
        raw, other = choice[symbol], tencent[symbol]
        first, last = min(other, default="9999-12-31"), max(other, default="")
        # Keep missing interior sessions in the grid: never shift D+h or bridge a gap.
        dates = [day for day in sessions if first <= day <= last]
        valid = [day for day in dates if day in other and _traded(raw[day]) and _positive(other[day].volume)]
        if not valid:
            comparisons.append({"symbol": symbol, "common_grid_sessions": len(dates), "compared_sessions": 0})
            continue
        anchor = valid[-1]
        adjusted = {day: _adjusted(raw[day], day, raw[anchor]["TAFACTOR"]) for day in valid}
        scale = raw[anchor]["CLOSE"] / other[anchor].close
        comparisons.append({"symbol": symbol, "common_grid_sessions": len(dates), "compared_sessions": len(valid),
            "anchor": {"date": anchor, "choice_factor": raw[anchor]["TAFACTOR"], "choice_raw_close": raw[anchor]["CLOSE"],
                       "tencent_close": other[anchor].close, "tencent_price_multiplier": scale},
            **_price_volume_comparison(raw, adjusted, other, dates, scale),
            "feature_windows": _feature_comparison(symbol, adjusted, other, dates, differences, worst),
            "direction_labels": _direction_comparison(adjusted, other, dates)})
    return {"symbol_count": len(comparisons), "market_counts": dict(sorted(Counter(row["symbol"][-2:] for row in comparisons).items())),
        "tencent_volume_to_shares_multiplier": TENCENT_VOLUME_TO_SHARES, "symbols": comparisons,
        "feature_absolute_differences": {name: {**_distribution(values), "worst": worst.get(name)} for name, values in differences.items()}}


def _sample_anchor_window(index: int, total: int, rows: Mapping[str, dict[str, Any]], window: Sequence[str]) -> bool:
    return index % 37 == 0 or index == total - 1 or rows[window[-1]]["TAFACTOR"] != rows[window[-2]]["TAFACTOR"]


def _anchor_window_error(rows: Mapping[str, dict[str, Any]], window: Sequence[str], last_factor: float,
                         maxima: dict[str, float]) -> float:
    fixed = [_adjusted(rows[day], day, last_factor) for day in window]
    signal = [_adjusted(rows[day], day, rows[window[-1]]["TAFACTOR"]) for day in window]
    left, right = _vector(fixed), _vector(signal)
    for name, a, b in zip(HISTORICAL_REPLAY_FEATURE_NAMES, left, right, strict=True):
        maxima[name] = max(maxima[name], abs(a - b))
    future = replace(fixed[-1], date="9999-12-31", open=1e100, close=1e100, high=1e100, low=1e100, volume=1e100,
                     data_version="must-be-ignored-future")
    after = historical_replay_feature_values(cast(Sequence[OHLCVBar], [*fixed, future]), signal_date=window[-1])
    return max(abs(a - b) for a, b in zip(left, after, strict=True))


def scale_invariance_comparison(series: RawSeries, sessions: Sequence[str]) -> dict[str, Any]:
    maxima = {name: 0.0 for name in HISTORICAL_REPLAY_FEATURE_NAMES}
    windows = tested = invalid = 0
    future_error = 0.0
    for rows in series.values():
        accepted = [day for day in sessions if day in rows and _traded(rows[day])]
        if not accepted:
            invalid += max(0, len(sessions) - 20)
            continue
        last_factor = rows[accepted[-1]]["TAFACTOR"]
        for index in range(20, len(sessions)):
            window = sessions[index - 20:index + 1]
            if not all(day in rows and _traded(rows[day]) for day in window):
                invalid += 1
                continue
            windows += 1
            if not _sample_anchor_window(index, len(sessions), rows, window):
                continue
            future_error = max(future_error, _anchor_window_error(rows, window, last_factor, maxima))
            tested += 1
    return {"feature_names": list(HISTORICAL_REPLAY_FEATURE_NAMES), "lookback_sessions": 21,
        "valid_fixed_session_windows": windows, "invalid_or_restricted_windows": invalid, "tested_windows": tested,
        "sampling": "every_37th_grid_index_plus_factor_changes_plus_last_session",
        "last_traded_anchor_vs_signal_anchor_max_abs_error": maxima, "appended_future_bar_max_abs_error": future_error,
        "conclusion_scope": "same_historical_factors_and_common_positive_scale_only_not_pit_or_cost_label_invariance"}


def compare_choice_tencent_history(choice_directory: Path, tencent_database: Path, tencent_manifest: Path) -> dict[str, Any]:
    """Verify immutable sources, compare overlapping sessions, and return only JSON."""
    require_offline_choice_calendar()
    choice_directory = _safe_path(choice_directory)
    tencent_database, tencent_manifest = _safe_path(tencent_database), _safe_path(tencent_manifest)
    databases = {choice_directory / "choice_research.sqlite3", tencent_database}
    fingerprints = {path: _file_digest(path, database=True) for path in databases}
    fingerprints[tencent_manifest] = _file_digest(tencent_manifest)
    choice, sessions, choice_facts = _read_choice(choice_directory, fingerprints)
    tencent, tencent_facts = _read_tencent(tencent_database, tencent_manifest, sorted(choice))
    result = {"schema_version": COMPARISON_SCHEMA, "read_only": True, "sdk_requests": 0,
        "official": False, "formal_equivalent_pit": False, "filter_qualified": False,
        "production_ranking_effect": "none", "source_equivalence": "not_established", "runtime_model_replacement": False,
        "choice": {"directory": str(choice_directory), **choice_facts},
        "tencent": {"database": str(tencent_database), "manifest": str(tencent_manifest), **tencent_facts},
        "factor_reference_comparison": factor_reference_comparison(choice, sessions),
        "shared_comparison": _shared_comparison(choice, tencent, sessions),
        "scale_invariance": scale_invariance_comparison(choice, sessions), "limitations": list(LIMITATIONS)}
    for path, digest in fingerprints.items():
        if _file_digest(path, database=path in databases) != digest:
            raise ChoiceError("comparison input changed during the read-only audit")
    result["input_sha256"] = {str(path): digest for path, digest in sorted(fingerprints.items())}
    result["input_hashes_unchanged"] = True
    return result
