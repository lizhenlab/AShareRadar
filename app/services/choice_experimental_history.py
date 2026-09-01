"""Replay-verified Choice bars for isolated, retrospective direction research.

This is a separate history contract, not a Tencent manifest or PIT evidence.
Only observed traded bars are converted; missing/restricted sessions are never
filled. The original Choice archive remains the authority for every derived row.
"""

from __future__ import annotations

from collections import Counter, defaultdict
from dataclasses import dataclass
from datetime import date, timedelta
import math
from pathlib import Path
import sqlite3
from typing import Any

from app.artifacts.io import (
    canonical_json_bytes, decode_json_bytes, exclusive_atomic_publish,
    path_has_only_trusted_aliases, read_regular_file, sha256_hex,
)
from app.config import env_bool
from app.models.market import Kline
from app.services.choice_research import DAILY_FIELDS, OPTIONS, choose_symbols, make_plan, normalize, request, snapshot_dates
from app.services.choice_research_store import ChoiceDataset
from app.services.choice_research_supplement import source_receipts_digest
from app.services.choice_sdk import ChoiceError
from app.services.market_scan_probability_history import trusted_probability_history_dates
from app.services.trading_calendar import previous_trade_date
from app.utils.clock import market_now


SCHEMA_VERSION = "choice-experimental-history-v1"
DATABASE_VERSION = 7413
MANIFEST_MAX_BYTES = 4 * 1024 * 1024
DATABASE_MAX_BYTES = 64 * 1024 * 1024
SOURCE_NAME = "Choice reconstructed factor-qfq (research only)"
LIMITATIONS = [
    "retrospective_provider_download_not_original_pit",
    "multiplicative_factor_reconstruction_not_vendor_delivered_qfq",
    "choice_and_tencent_adjustment_methods_not_assumed_equivalent",
    "monthly_union_diagnostic_cohort_not_representative_full_market",
    "excluded_sessions_not_forward_filled_or_shifted",
    "daily_bars_and_adjustment_factors_do_not_prove_execution",
    "corporate_action_and_delisted_history_coverage_not_complete",
    "historical_data_previously_observed_not_prospective_oos",
    "independent_exchange_membership_not_verified",
    "research_only_no_runtime_model_replacement_or_formal_authority",
]
_TRADED_QUALITIES = {
    "traded_bar_not_execution_proof", "single_price_limit_up",
    "single_price_limit_down", "single_price_unknown",
}
_COLUMNS = (
    "symbol", "adjustment_mode", "date", "open", "close", "high", "low", "volume",
    "as_of", "data_version", "contract_version", "fallback_used", "source", "fetched_at",
)
_TABLE_SQL = """CREATE TABLE kline_daily (
    symbol TEXT NOT NULL, adjustment_mode TEXT NOT NULL CHECK(adjustment_mode='qfq'),
    date TEXT NOT NULL, open REAL NOT NULL, close REAL NOT NULL, high REAL NOT NULL,
    low REAL NOT NULL, volume REAL NOT NULL, as_of TEXT NOT NULL, data_version TEXT NOT NULL,
    contract_version TEXT NOT NULL, fallback_used INTEGER NOT NULL CHECK(fallback_used=0),
    source TEXT NOT NULL, fetched_at TEXT NOT NULL, PRIMARY KEY(symbol,adjustment_mode,date)
)"""


@dataclass(frozen=True)
class ChoiceExperimentalHistory:
    series: dict[str, list[Kline]]
    sessions: tuple[str, ...]
    provenance: dict[str, Any]


@dataclass(frozen=True)
class ChoiceExperimentalHistoryBuild:
    manifest_path: Path
    database_path: Path
    summary: dict[str, Any]


def require_offline_choice_calendar() -> None:
    """Do not let an offline analysis implicitly start a calendar provider call."""
    if env_bool("ASHARE_RADAR_TRADE_CALENDAR_AUTO_FETCH", False, aliases=("TRADE_CALENDAR_AUTO_FETCH",)):
        raise ChoiceError("offline Choice research requires ASHARE_RADAR_TRADE_CALENDAR_AUTO_FETCH=0 (including its alias)")


def _guard(path: Path, *, database: bool = False) -> Path:
    source = path.expanduser().absolute()
    if ".workbuddy-ai" in source.parts or not path_has_only_trusted_aliases(source):
        raise ChoiceError("Choice experimental path is protected or traverses a symlink")
    if database and any(Path(str(source) + suffix).exists() or Path(str(source) + suffix).is_symlink()
                        for suffix in ("-wal", "-shm", "-journal")):
        raise ChoiceError("Choice experimental history requires a static database without sidecars")
    if database:
        with source.open("rb") as handle:
            header = handle.read(100)
        if not header.startswith(b"SQLite format 3\x00") or header[18:20] != b"\x01\x01":
            raise ChoiceError("Choice experimental history requires a rollback-journal SQLite archive")
    return source


def _source_contract(source: ChoiceDataset) -> tuple[tuple[str, ...], list[str]]:
    plan = source.plan
    if plan.get("schema_version") != "choice-research-dataset-v1":
        raise ChoiceError("Choice experimental history needs a base history archive")
    rebuilt = make_plan(plan["start_date"], plan["end_date"], plan["symbol_limit"],
                        plan["preferred_symbols"], plan["event_symbols"])
    if rebuilt != plan or plan["end_date"] >= market_now().date().isoformat():
        raise ChoiceError("Choice history plan is changed, unsupported, or not fully closed")
    sessions = _source_calendar(source)
    universe = {row[0] for row in source.db.execute("SELECT DISTINCT symbol FROM universe_membership")}
    symbols = choose_symbols(universe, plan)
    if len(symbols) * len(sessions) > 100_000:
        raise ChoiceError("Choice experimental grid exceeds the 100000-observation research bound")
    _validate_source_requests(source, sessions, symbols)
    grid = source.db.execute("SELECT symbol,session_date FROM daily_bars").fetchall()
    if len(grid) != len(symbols) * len(sessions) or set(grid) != {(symbol, day) for symbol in symbols for day in sessions}:
        raise ChoiceError("Choice source daily grid has missing, duplicate, or out-of-scope records")
    return sessions, symbols


def _source_calendar(source: ChoiceDataset) -> tuple[str, ...]:
    plan = source.plan
    sessions = tuple(row[0] for row in source.db.execute("SELECT as_of FROM records WHERE kind='calendar' ORDER BY as_of"))
    if not sessions or len(sessions) > 800 or len(set(sessions)) != len(sessions):
        raise ChoiceError("Choice history calendar is empty, duplicate, or excessive")
    expected = trusted_probability_history_dates(plan["end_date"], len(sessions))
    prior = previous_trade_date(date.fromisoformat(sessions[0]) - timedelta(days=1)).isoformat()
    if sessions != expected or not prior < plan["start_date"] <= sessions[0]:
        raise ChoiceError("Choice calendar does not match the complete trusted exchange-session grid")
    return sessions


def _validate_source_requests(source: ChoiceDataset, sessions: tuple[str, ...], symbols: list[str]) -> None:
    plan = source.plan
    calendar_request = request("calendar", "tradedates", [plan["start_date"], plan["end_date"], f"Market=CNSESH,{OPTIONS}"])
    expected_descriptors = [calendar_request]
    for day in snapshot_dates(list(sessions)):
        expected_descriptors.append(request("universe", "sector", ["001071", day, OPTIONS], as_of=day))
    for offset in range(0, len(symbols), 20):
        batch = symbols[offset:offset + 20]
        expected_descriptors.append(request("daily", "csd", [",".join(batch), ",".join(DAILY_FIELDS), sessions[0], sessions[-1],
            f"Period=1,AdjustFlag=1,FillData=0,Order=1,{OPTIONS}"], symbols=batch, fields=DAILY_FIELDS, sessions=list(sessions)))
    actual = {row[0] for row in source.db.execute("SELECT request_key FROM requests WHERE json_extract(descriptor_json,'$.kind') IN ('calendar','universe','daily')")}
    if actual != {ChoiceDataset.key(item) for item in expected_descriptors}:
        raise ChoiceError("Choice source calendar/universe/daily request contract is incomplete or changed")


def _positive(value: Any) -> bool:
    return type(value) in {int, float} and math.isfinite(value) and value > 0


def _rejection(row: dict[str, Any]) -> str | None:
    quality = row["quality"]
    if quality not in _TRADED_QUALITIES:
        return str(quality)
    if not _positive(row["TAFACTOR"]):
        return "invalid_adjustment_factor"
    if not all(_positive(row[name]) for name in ("OPEN", "HIGH", "LOW", "CLOSE", "VOLUME", "AMOUNT")):
        return "invalid_traded_observation"
    return None


def _convert(source: ChoiceDataset, sessions: tuple[str, ...], symbols: list[str], receipts: str) -> tuple[
    dict[str, list[Kline]], dict[str, Any],
]:
    accepted: dict[str, list[tuple[str, dict[str, Any], str]]] = defaultdict(list)
    excluded: list[dict[str, str]] = []
    for symbol, day, encoded, captured_at in source.db.execute("""SELECT r.symbol,r.as_of,r.payload_json,q.captured_at
        FROM records r JOIN requests q USING(request_key) WHERE r.kind='daily' ORDER BY r.symbol,r.as_of"""):
        raw = decode_json_bytes(encoded.encode())
        if not isinstance(raw, dict):
            raise ChoiceError("invalid Choice bar payload")
        reason = _rejection(raw)
        if reason:
            excluded.append({"symbol": symbol, "date": day, "reason": reason})
        else:
            accepted[symbol].append((day, raw, captured_at))
    if not accepted:
        raise ChoiceError("Choice source has no usable observed traded bars")
    # Keep all selected symbols, including a completely unavailable one, so
    # downstream test coverage cannot silently shrink its denominator.
    series: dict[str, list[Kline]] = {symbol: [] for symbol in symbols}
    anchors: dict[str, dict[str, Any]] = {}
    for symbol, records in sorted(accepted.items()):
        anchor_day, anchor_row, _ = records[-1]
        anchor = float(anchor_row["TAFACTOR"])
        anchors[symbol] = {"date": anchor_day, "factor": anchor}
        series[symbol] = [_adjusted_bar(day, raw, captured_at, anchor_day=anchor_day, anchor=anchor, receipts=receipts)
                          for day, raw, captured_at in records]
    counts = Counter(item["reason"] for item in excluded)
    return series, {
        "source_symbols": symbols, "accepted_symbols": sorted(accepted),
        "accepted_bars": sum(len(rows) for rows in series.values()),
        "expected_symbol_sessions": len(symbols) * len(sessions),
        "excluded_counts": dict(sorted(counts.items())), "excluded_records": excluded,
        "anchors": anchors,
        "adjustment": {"mode": "qfq", "method": "raw_price_times_TAFACTOR_divided_by_last_traded_TAFACTOR",
                       "anchor": "last_observed_traded_session_per_symbol", "volume": "unadjusted_shares",
                       "vendor_delivered_qfq": False, "price_scale_invariant_features_only": True},
    }


def _adjusted_bar(day: str, raw: dict[str, Any], captured_at: str, *, anchor_day: str, anchor: float, receipts: str) -> Kline:
    ratio = float(raw["TAFACTOR"]) / anchor
    prices = {name: float(raw[name]) * ratio for name in ("OPEN", "HIGH", "LOW", "CLOSE")}
    if not _positive(ratio) or not all(_positive(value) for value in prices.values()):
        raise ChoiceError("Choice factor reconstruction underflowed or overflowed")
    return Kline(date=day, open=prices["OPEN"], close=prices["CLOSE"], high=prices["HIGH"], low=prices["LOW"],
        volume=float(raw["VOLUME"]), adjustment_mode="qfq", as_of=anchor_day,
        data_version=f"choice-factor-qfq-v1:{receipts}", source=SOURCE_NAME, fetched_at=captured_at,
        from_cache=True, session_status="trading", adjustment_factor=ratio, point_in_time=False)


def _source_snapshot(directory: Path) -> tuple[dict[str, list[Kline]], tuple[str, ...], dict[str, Any]]:
    require_offline_choice_calendar()
    directory = _guard(directory)
    _guard(directory / "choice_research.sqlite3", database=True)
    with ChoiceDataset.open_readonly(directory) as source:
        source.db.execute("BEGIN")
        receipts = source_receipts_digest(source)
        source.verify(normalize)
        sessions, symbols = _source_contract(source)
        series, coverage = _convert(source, sessions, symbols, receipts)
        plan_digest = sha256_hex(canonical_json_bytes(source.plan))
    with ChoiceDataset.open_readonly(directory) as after:
        if source_receipts_digest(after) != receipts or sha256_hex(canonical_json_bytes(after.plan)) != plan_digest:
            raise ChoiceError("Choice source changed during conversion")
    return series, sessions, {
        "schema_version": SCHEMA_VERSION,
        "source": {"directory": str(directory.resolve()), "receipts_sha256": receipts,
                   "plan_sha256": plan_digest, "raw_replay_verified": True},
        "sessions": list(sessions), "calendar_sha256": sha256_hex(canonical_json_bytes(list(sessions))),
        "coverage": coverage, "limitations": LIMITATIONS,
        "official": False, "formal_equivalent_pit": False, "filter_qualified": False,
        "production_ranking_effect": "none",
    }


def _rows(series: dict[str, list[Kline]]) -> list[tuple[Any, ...]]:
    return [(symbol, row.adjustment_mode, row.date, row.open, row.close, row.high, row.low, row.volume,
             row.as_of, row.data_version, row.contract_version, int(row.fallback_used), row.source, row.fetched_at)
            for symbol, rows in sorted(series.items()) for row in rows]


def _database_bytes(rows: list[tuple[Any, ...]]) -> bytes:
    connection = sqlite3.connect(":memory:")
    try:
        connection.execute(f"PRAGMA user_version={DATABASE_VERSION}")
        connection.execute(_TABLE_SQL)
        connection.executemany(f"INSERT INTO kline_daily ({','.join(_COLUMNS)}) VALUES ({','.join('?' for _ in _COLUMNS)})", rows)
        connection.commit()
        return connection.serialize()
    finally:
        connection.close()


def build_choice_experimental_history(source_directory: Path, output_directory: Path) -> ChoiceExperimentalHistoryBuild:
    """Publish a new static research database and a separate, replayable manifest."""
    output = _guard(output_directory)
    source_path = _guard(source_directory).resolve()
    if output.resolve() == source_path or source_path in output.resolve().parents:
        raise ChoiceError("Choice derived history must be outside the original archive")
    series, _sessions, payload = _source_snapshot(source_directory)
    rows = _rows(series)
    encoded = _database_bytes(rows)
    database_digest = sha256_hex(encoded)
    database = output / f"choice-factor-qfq-{database_digest}.sqlite3"
    payload["database"] = {"filename": database.name, "sha256": database_digest,
                           "row_count": len(rows), "rows_sha256": sha256_hex(canonical_json_bytes([list(row) for row in rows]))}
    digest = sha256_hex(canonical_json_bytes(payload))
    manifest = output / f"choice-experimental-history-{digest}.manifest.json"
    exclusive_atomic_publish(database, encoded, max_bytes=DATABASE_MAX_BYTES)
    exclusive_atomic_publish(manifest, canonical_json_bytes({"payload": payload, "sha256": digest}), max_bytes=MANIFEST_MAX_BYTES)
    return ChoiceExperimentalHistoryBuild(manifest, database, {
        "status": "choice_experimental_history_built", "manifest": str(manifest), "database": str(database),
        "manifest_digest": digest, "sessions": len(payload["sessions"]),
        "symbols": len(series), "accepted_bars": len(rows), "excluded_counts": payload["coverage"]["excluded_counts"],
        "official": False, "formal_equivalent_pit": False, "filter_qualified": False, "production_ranking_effect": "none",
    })


def _manifest_payload(manifest_path: Path) -> tuple[dict[str, Any], str]:
    document = decode_json_bytes(read_regular_file(manifest_path, max_bytes=MANIFEST_MAX_BYTES))
    if not isinstance(document, dict) or set(document) != {"payload", "sha256"} or not isinstance(document["payload"], dict):
        raise ChoiceError("invalid Choice experimental manifest")
    payload, digest = document["payload"], document["sha256"]
    if (digest != sha256_hex(canonical_json_bytes(payload))
            or manifest_path.name != f"choice-experimental-history-{digest}.manifest.json"
            or payload.get("schema_version") != SCHEMA_VERSION):
        raise ChoiceError("Choice experimental manifest identity mismatch")
    return payload, digest


def load_choice_experimental_history(manifest_path: Path, database: Path) -> ChoiceExperimentalHistory:
    """Recompute all derived rows from the unchanged raw Choice source archive."""
    manifest_path, database = _guard(manifest_path), _guard(database, database=True)
    payload, digest = _manifest_payload(manifest_path)
    facts = payload.get("database", {})
    before = sha256_hex(read_regular_file(database, max_bytes=DATABASE_MAX_BYTES))
    if facts.get("filename") != database.name or facts.get("sha256") != before:
        raise ChoiceError("Choice experimental database digest mismatch")
    series, sessions, expected_payload = _source_snapshot(Path(payload["source"]["directory"]))
    expected_rows = _rows(series)
    expected_facts = {"filename": database.name, "sha256": before, "row_count": len(expected_rows),
                      "rows_sha256": sha256_hex(canonical_json_bytes([list(row) for row in expected_rows]))}
    if payload != {**expected_payload, "database": expected_facts}:
        raise ChoiceError("Choice experimental manifest does not replay from its original source")
    connection = sqlite3.connect(database.as_uri() + "?mode=ro&immutable=1", uri=True)
    try:
        connection.execute("PRAGMA query_only=ON")
        if connection.execute("PRAGMA user_version").fetchone()[0] != DATABASE_VERSION:
            raise ChoiceError("not a Choice experimental database")
        actual_rows = connection.execute(f"SELECT {','.join(_COLUMNS)} FROM kline_daily ORDER BY symbol,date").fetchall()
        if actual_rows != expected_rows or connection.execute("PRAGMA quick_check").fetchone()[0] != "ok":
            raise ChoiceError("Choice experimental database rows differ from raw replay")
    finally:
        connection.close()
    if sha256_hex(read_regular_file(database, max_bytes=DATABASE_MAX_BYTES)) != before:
        raise ChoiceError("Choice experimental database changed during verification")
    return ChoiceExperimentalHistory(series, sessions, {
        **payload, "manifest_digest": digest, "source_sha256": before,
        "source_integrity_digest": digest, "source_filename": database.name,
    })
