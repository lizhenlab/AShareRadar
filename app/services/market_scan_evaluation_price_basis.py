"""Pure admission checks for frozen-signal versus mutable forward qfq prices.

Match the future-range contract: a valid frozen signal OHLC must overlap the
forward vintage exactly. Data-version labels may differ after a cache refresh;
the overlap proves the observed price basis, without assuming labels are prices.
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
import math
import sqlite3
from typing import cast

from app.models.market import DAILY_KLINE_CONTRACT_VERSION
from app.repositories.market_scan_mapping import decode_result_payload
from app.services.data_quality_kline import is_demo_kline_source
from app.services.market_scan_score_dimensions import (
    MARKET_SCAN_EVIDENCE_CONTRACT_VERSION,
    verify_market_scan_point_in_time_evidence,
)
from app.utils.market_data import finite_float, valid_non_negative_number, valid_ohlc
from app.utils.market_time import market_datetime_epoch


def forward_price_basis_status(
    run: sqlite3.Row,
    result: sqlite3.Row,
    bars: Sequence[sqlite3.Row],
) -> str:
    """Validate only supplied persisted evidence; never fetch or repair inputs."""
    evidence = _point_in_time_evidence(result)
    if evidence is None:
        return "signal_price_basis_evidence_missing"
    if (
        evidence.get("contract_version") != MARKET_SCAN_EVIDENCE_CONTRACT_VERSION
        or not verify_market_scan_point_in_time_evidence(evidence)
    ):
        return "signal_price_basis_evidence_invalid"
    payload = evidence.get("payload")
    if not isinstance(payload, Mapping) or not _identity_matches(payload, run, result):
        return "signal_price_basis_identity_conflict"
    signal = _signal_bar(payload, str(run["data_date"]), str(run["as_of"]))
    if signal is None:
        return "signal_price_basis_bar_invalid"
    return _target_overlap_status(str(run["data_date"]), signal, bars)


def _target_overlap_status(data_date: str, signal: Sequence[float], bars: Sequence[sqlite3.Row]) -> str:
    overlapping = [row for row in bars if str(row["date"]) == data_date]
    if not overlapping:
        return "target_adjustment_overlap_missing"
    if len(overlapping) != 1 or not valid_forward_price_bar(overlapping[0]):
        return "target_adjustment_overlap_invalid"
    current = overlapping[0]
    pairs = zip((current[name] for name in ("open", "close", "high", "low")), signal, strict=True)
    if not all(math.isclose(float(left), float(right), rel_tol=0, abs_tol=1e-8) for left, right in pairs):
        return "target_adjustment_rebase_conflict"
    return "verified"


def valid_forward_price_bar(row: sqlite3.Row) -> bool:
    """Use the same qfq, version and OHLCV requirements as fixed-session labels."""
    try:
        source = row["source"] if "source" in row.keys() else None
        return (
            row["adjustment_mode"] == "qfq"
            and row["contract_version"] == DAILY_KLINE_CONTRACT_VERSION
            and bool(str(row["data_version"] or "").strip())
            and valid_ohlc(row["open"], row["close"], row["high"], row["low"])
            and valid_non_negative_number(row["volume"])
            and (source is None or isinstance(source, str))
            and not is_demo_kline_source(source)
        )
    except (IndexError, KeyError, TypeError):
        return False


def _point_in_time_evidence(result: sqlite3.Row) -> Mapping[str, object] | None:
    _metrics, details = decode_result_payload(result["metrics_json"])
    components = details.get("components")
    dimensions = components.get("score_dimensions") if isinstance(components, Mapping) else None
    evidence = dimensions.get("point_in_time_evidence") if isinstance(dimensions, Mapping) else None
    return evidence if isinstance(evidence, Mapping) else None


def _identity_matches(payload: Mapping[str, object], run: sqlite3.Row, result: sqlite3.Row) -> bool:
    expected = {
        "symbol": result["symbol"], "market": result["market"],
        "quote_date": run["quote_date"] or run["data_date"], "data_date": run["data_date"],
        "mode": run["mode"] or "official", "adjustment_mode": "qfq",
    }
    if result["adjustment_mode"] != "qfq" or any(payload.get(key) != value for key, value in expected.items()):
        return False
    frozen, persisted = finite_float(payload.get("quote_price")), finite_float(result["price"])
    quote_epoch = market_datetime_epoch(str(payload.get("quote_timestamp") or ""))
    decision_epoch = market_datetime_epoch(str(run["as_of"]))
    return (
        frozen is not None and persisted is not None and frozen > 0 and persisted > 0
        and math.isclose(frozen, persisted, rel_tol=0, abs_tol=1e-8)
        and quote_epoch is not None and decision_epoch is not None and quote_epoch <= decision_epoch
    )


def _signal_bar(payload: Mapping[str, object], data_date: str, as_of: str) -> tuple[float, ...] | None:
    contracts = payload.get("bar_contract_61")
    if not isinstance(contracts, list) or len(contracts) != 61:
        return None
    if any(not isinstance(row, list) or len(row) != 10 for row in contracts):
        return None
    rows = cast(list[list[object]], contracts)
    dates = [str(row[0]) for row in rows]
    versions = {str(row[7] or "") for row in rows}
    if dates != sorted(set(dates)) or dates[-1] != data_date or len(versions) != 1 or not all(versions):
        return None
    if not _signal_rows_valid(rows, as_of):
        return None
    return tuple(float(cast(float, value)) for value in rows[-1][1:5])


def _signal_rows_valid(rows: Sequence[Sequence[object]], as_of: str) -> bool:
    decision_epoch = market_datetime_epoch(as_of)
    if decision_epoch is None:
        return False
    previous_epoch = float("-inf")
    for row in rows:
        raw = dict(zip(
            ("date", "open", "close", "high", "low", "volume", "adjustment_mode", "data_version", "contract_version", "as_of"),
            row, strict=True,
        ))
        if not valid_forward_price_bar(cast(sqlite3.Row, raw)):
            return False
        snapshot = str(row[9])
        snapshot_epoch = market_datetime_epoch(f"{snapshot} 00:00:00" if len(snapshot) == 10 else snapshot)
        start_epoch = market_datetime_epoch(f"{row[0]} 00:00:00")
        if (
            snapshot_epoch is None or start_epoch is None
            or not start_epoch <= snapshot_epoch <= decision_epoch or snapshot_epoch < previous_epoch
        ):
            return False
        previous_epoch = snapshot_epoch
    return True
