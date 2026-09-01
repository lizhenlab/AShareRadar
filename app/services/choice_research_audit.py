"""Offline Choice evidence and conservative quota audit; never releases reservations."""

from __future__ import annotations

from collections import Counter, defaultdict
from contextlib import ExitStack
from datetime import datetime
from pathlib import Path
import re
import sqlite3
from typing import Any
from zoneinfo import ZoneInfo

from app.artifacts.io import canonical_json_bytes, decode_json_bytes, exclusive_atomic_publish, path_has_only_trusted_aliases, read_regular_file, sha256_hex
from app.services.choice_research import OPTIONS, REFERENCE_FIELDS, SUSPENSION_FIELDS, normalize, request
from app.services.choice_research_store import ChoiceDataset, MAX_RAW_BYTES, quota_contract
from app.services.choice_research_supplement import SUPPLEMENT_VERSION, source_receipts_digest, supplement_coverage
from app.services.choice_research_universe import UNIVERSE_VERSION, universe_coverage
from app.services.choice_sdk import ChoiceError
from app.utils.clock import market_now


AUDIT_VERSION = "choice-research-offline-audit-v1"
LIMITS = {"EM_CSD": 450000, "EM_CSS": 200000, "EM_CTR": 3}
FUNCTIONS = {"csd": "EM_CSD", "css": "EM_CSS", "ctr": "EM_CTR"}
QUOTA_FIELDS = ("FUNCENAME", "SECUTYPE", "PERIOD", "STARTDATE", "ENDDATE", "THRESHOLD",
                "USEDDATA", "AVAILABEDATA", "EFFECTIVEDATE")
READ_ONLY_LIMITATIONS = [
    "retrospective_raw_replay_is_not_original_provider_vintage",
    "null_or_zero_limit_prices_do_not_prove_no_price_limit",
    "daily_bars_and_date_only_halts_do_not_prove_order_execution",
    "units_only_reservations_do_not_identify_provider_billing_or_settlement",
    "archived_statistics_are_not_live_quota_authorization",
]


def _safe_path(path: Path) -> Path:
    if ".workbuddy-ai" in path.parts or not path_has_only_trusted_aliases(path):
        raise ChoiceError("audit refuses forbidden paths or untrusted aliases")
    return path.resolve()


def _hashed_document(path: Path) -> dict[str, Any]:
    raw = read_regular_file(_safe_path(path), max_bytes=1024 * 1024)
    if path.stem.rsplit("-", 1)[-1] != sha256_hex(raw):
        raise ChoiceError("audit document content-address mismatch")
    value = decode_json_bytes(raw)
    if not isinstance(value, dict):
        raise ChoiceError("invalid audit document")
    return value


def _archive_files(directory: Path, prefix: str = "") -> list[Path]:
    directory = _safe_path(directory)
    if not directory.is_dir():
        return []
    files = sorted(path for path in directory.glob(f"{prefix}*.json") if re.fullmatch(re.escape(prefix) + r"[a-f0-9]{64}\.json", path.name))
    if len(files) > 5000:
        raise ChoiceError("audit archive exceeds bounded file count")
    return files


def _open_dataset(stack: ExitStack, directory: Path) -> ChoiceDataset:
    directory = _safe_path(directory)
    # This audit accepts quiescent rollback-journal archives only. A read-only WAL
    # connection could otherwise create a shared-memory sidecar on first access.
    _require_quiescent_database(directory / "choice_research.sqlite3")
    dataset = stack.enter_context(ChoiceDataset.open_readonly(directory))
    dataset.db.execute("BEGIN")
    return dataset


def _require_quiescent_database(path: Path) -> None:
    _safe_path(path)
    for suffix in ("-wal", "-journal", "-shm"):
        sidecar = path.with_name(path.name + suffix)
        if sidecar.exists() or sidecar.is_symlink():
            raise ChoiceError("audit requires a quiescent archive without journal sidecars")
    with path.open("rb") as handle:
        header = handle.read(100)
    if not header.startswith(b"SQLite format 3\x00") or header[18:20] != b"\x01\x01":
        raise ChoiceError("audit requires an existing rollback-journal SQLite archive")


def _check_source_bindings(base: ChoiceDataset, supplement: ChoiceDataset, universe: ChoiceDataset) -> None:
    if base.plan.get("schema_version") != "choice-research-dataset-v1":
        raise ChoiceError("audit requires a base history dataset")
    if supplement.plan.get("schema_version") != SUPPLEMENT_VERSION or universe.plan.get("schema_version") != UNIVERSE_VERSION:
        raise ChoiceError("audit requires explicit supplement and daily-universe plans")
    if _safe_path(Path(supplement.plan["source_directory"])) != base.directory:
        raise ChoiceError("supplement does not belong to the supplied base dataset")
    if supplement.plan["source_receipts_sha256"] != source_receipts_digest(base):
        raise ChoiceError("supplement source receipts changed")
    expected = [{"directory": str(source.directory), "receipts_sha256": source_receipts_digest(source)} for source in (base, supplement)]
    if universe.plan.get("sources") != expected:
        raise ChoiceError("daily universe source receipts do not match supplied datasets")
    sessions = [row[0] for row in base.db.execute("SELECT as_of FROM records WHERE kind='calendar' ORDER BY as_of")]
    if supplement.plan.get("sessions") != sessions or universe.plan.get("sessions") != sessions:
        raise ChoiceError("derived plans do not match the verified calendar")


def _receipts(dataset: ChoiceDataset) -> dict[str, dict[str, Any]]:
    rows = dataset.db.execute("SELECT request_key,descriptor_json,raw_sha256,captured_at FROM requests ORDER BY request_key")
    return {key: {"request_key": key, "request": decode_json_bytes(descriptor.encode()), "raw_sha256": digest,
                  "raw_path": str(dataset.directory / "raw" / f"{key}.json"), "captured_at": _timestamp(captured).isoformat()}
            for key, descriptor, digest, captured in rows}


def _timestamp(value: str) -> datetime:
    timestamp = datetime.fromisoformat(value)
    if timestamp.tzinfo is None:
        raise ChoiceError("audit timestamps must include a timezone")
    return timestamp.astimezone(ZoneInfo("Asia/Shanghai"))


def _verify_dataset(dataset: ChoiceDataset) -> tuple[dict, dict]:
    captured = {}

    def replay(payload: dict) -> list:
        captured[ChoiceDataset.key(payload["request"])] = _timestamp(payload["captured_at"]).isoformat()
        return normalize(payload)

    summary = dataset.verify(replay)
    receipts = _receipts(dataset)
    if any(receipt["captured_at"] != captured[key] for key, receipt in receipts.items()):
        raise ChoiceError("request timestamp differs from its verified raw response")
    return summary, receipts


def _records(dataset: ChoiceDataset, kind: str, receipts: dict[str, dict[str, Any]]) -> dict[tuple[str, str], dict[str, Any]]:
    result: dict[tuple[str, str], dict[str, Any]] = {}
    for symbol, day, payload, key in dataset.db.execute(
            "SELECT symbol,as_of,payload_json,request_key FROM records WHERE kind=? ORDER BY symbol,as_of", (kind,)):
        if (symbol, day) in result:
            raise ChoiceError("duplicate symbol/date evidence in Choice audit")
        result[(symbol, day)] = {"values": decode_json_bytes(payload.encode()), "receipt": receipts[key], "kind": kind}
    return result


def _positive(value: Any) -> bool:
    return not isinstance(value, bool) and isinstance(value, (int, float)) and value > 0


def _positive_references(evidence: dict[str, Any] | None) -> bool:
    return evidence is not None and all(_positive(evidence["values"].get(field)) for field in REFERENCE_FIELDS)


def _has_trades(row: dict[str, Any]) -> bool:
    return all(_positive(row.get(field)) for field in ("OPEN", "HIGH", "LOW", "CLOSE", "VOLUME", "AMOUNT"))


def _listing_context(metadata: dict, daily: dict, references: dict, sessions: list[str]) -> dict[str, dict[str, Any]]:
    dates: dict[str, set[str]] = defaultdict(set)
    evidence: dict[str, dict[str, Any]] = {}
    for (symbol, _), record in metadata.items():
        if record["values"].get("LISTDATE"):
            dates[symbol].add(record["values"]["LISTDATE"])
            evidence[symbol] = record
    result: dict[str, dict[str, Any]] = {}
    for symbol, listing_dates in sorted(dates.items()):
        if len(listing_dates) != 1:
            result[symbol] = {"consistent": False, "initial_gap_sessions": []}
            continue
        listing_date = next(iter(listing_dates))
        window = _initial_gap_window(symbol, listing_date, sessions, daily, references)
        result[symbol] = {"consistent": True, "listing_date": listing_date, "initial_gap_sessions": window,
                          "listing_date_is_retrospectively_reported": True, "metadata": evidence[symbol]}
    return result


def _initial_gap_window(symbol: str, listing_date: str, sessions: list[str], daily: dict, references: dict) -> list[str]:
    if listing_date not in sessions:
        return []
    window = []
    for day in sessions[sessions.index(listing_date):]:
        bar, reference = daily.get((symbol, day)), references.get((symbol, day))
        if bar is None or reference is None or not _has_trades(bar["values"]):
            break
        values = reference["values"]
        if not _positive(values.get("PRECLOSEEXCH")) or any(values.get(field) is not None for field in REFERENCE_FIELDS[1:]):
            break
        if values["PRECLOSEEXCH"] != bar["values"].get("PRECLOSE"):
            break
        window.append(day)
    return window


def _css_request(kind: str, symbol: str, day: str) -> dict[str, Any]:
    fields = SUSPENSION_FIELDS if kind == "suspension_detail" else REFERENCE_FIELDS
    descriptor = request(kind, "css", [symbol, ",".join(fields), f"TradeDate={day},EndDate={day},AdjustFlag=1,{OPTIONS}"],
                         as_of=day, symbols=[symbol], fields=fields)
    return {"request": descriptor, "estimated_cells": len(fields), "execute_automatically": False}


def _anomaly(symbol: str, day: str, bar: dict, reference: dict | None, status: dict | None, listing: dict) -> dict[str, Any]:
    row = bar["values"]
    initial_window = day in listing.get("initial_gap_sessions", [])
    halt = row.get("TRADESTATUS") == "盘中停牌"
    halt_explained = bool(halt and status and status["values"].get("TRADESTATUS") == row.get("TRADESTATUS")
                          and status["values"].get("SUSPENDSDATE") == day and status["values"].get("SUSPENDEDATE") == day)
    known_state = row.get("TRADESTATUS") in {"正常交易", "复牌"}
    explanation = "initial_listing_window_with_unpublished_limit_prices" if initial_window else "unexplained_reference_gap"
    return {
        "symbol": symbol, "session_date": day, "quality": row["quality"], "daily": bar,
        "reference": reference, "suspension_detail": status, "listing_context": listing,
        "offline_explanation": explanation, "archive_pattern_explained": initial_window and (known_state or halt_explained),
        "intraday_halt": halt, "halt_date_evidence_consistent": halt_explained,
        "uncertain_trading_state": row["quality"] == "unknown_or_intraday_restricted_state",
        "price_limit_regime_verified": False, "execution_eligible": None,
        "remaining_evidence_gaps": ["independent_exchange_price_limit_rule_not_verified", "order_execution_not_proven"]
            + (["intraday_halt_start_end_times_not_available"] if halt else []),
        "minimal_css_reference_recheck": _css_request("execution_reference", symbol, day),
    }


def _reference_audit(base: ChoiceDataset, supplement: ChoiceDataset, receipts: list[dict]) -> dict[str, Any]:
    daily = _records(base, "daily", receipts[0])
    metadata = _records(base, "metadata", receipts[0])
    additions = _records(supplement, "execution_reference", receipts[1])
    if metadata.keys() & additions.keys():
        raise ChoiceError("overlapping reference evidence requires explicit reconciliation")
    references = metadata | additions
    statuses = _records(supplement, "suspension_detail", receipts[1])
    sessions = supplement.plan["sessions"]
    listing = _listing_context(metadata, daily, references, sessions)
    ignored: Counter[str] = Counter()
    anomalies = []
    for (symbol, day), bar in sorted(daily.items()):
        reference = references.get((symbol, day))
        if _positive_references(reference):
            continue
        quality = bar["values"]["quality"]
        if quality in {"not_trading", "suspended"} and not _has_trades(bar["values"]):
            ignored[quality] += 1
            continue
        anomalies.append(_anomaly(symbol, day, bar, reference, statuses.get((symbol, day)), listing.get(symbol, {})))
    expected = {(symbol, day) for symbol in supplement.plan["symbols"] for day in sessions}
    return {"expected_sample_symbol_sessions": len(expected), "daily_rows": len(daily),
            "missing_daily_pairs": [list(pair) for pair in sorted(expected - daily.keys())],
            "missing_reference_pairs": [list(pair) for pair in sorted(expected - references.keys())],
            "reference_null_or_zero_by_nontrading_state": dict(ignored), "priority_anomalies": anomalies,
            "priority_anomaly_count": len(anomalies), "priority_quality_counts": dict(Counter(item["quality"] for item in anomalies)),
            "offline_explained_count": sum(item["archive_pattern_explained"] for item in anomalies)}


def _estimated_units(descriptor: dict[str, Any]) -> int:
    method = descriptor["method"]
    if method == "ctr":
        return 1
    count = len(descriptor["symbols"]) * len(descriptor["fields"])
    return count * len(descriptor["sessions"]) if method == "csd" else count


def _orphan_receipts(dataset: ChoiceDataset, known: dict[str, dict]) -> list[dict[str, Any]]:
    result = []
    for path in _archive_files(dataset.directory / "raw"):
        if path.stem in known:
            continue
        raw = decode_json_bytes(read_regular_file(path, max_bytes=MAX_RAW_BYTES))
        if not isinstance(raw, dict):
            raise ChoiceError("invalid orphan raw response")
        descriptor = raw["payload"]["request"]
        if ChoiceDataset.key(descriptor) != path.stem:
            raise ChoiceError("orphan Choice response filename/request mismatch")
        payload = dataset.cached(descriptor)
        if payload is None:
            raise ChoiceError("orphan Choice response disappeared")
        outcome = "unprojected_valid_response"
        try:
            normalize(payload)
        except ChoiceError:
            outcome = "unprojected_invalid_or_failed_response"
        result.append({"request_key": path.stem, "request": descriptor, "captured_at": _timestamp(payload["captured_at"]).isoformat(),
                       "raw_path": str(path), "outcome": outcome})
    return result


def _collection_failures(datasets: list[ChoiceDataset]) -> list[dict[str, Any]]:
    result = []
    for dataset in datasets:
        for path in _archive_files(dataset.directory, "progress-"):
            data = _hashed_document(path)
            error = str(data.get("error", ""))
            category = "other_collection_pause"
            if "timed out" in error:
                category = "native_call_timeout_outcome_uncertain"
            elif "datastatistics" in error:
                category = "account_statistics_failed_no_data_receipt"
            elif "request-count pause" in error:
                category = "planned_request_count_pause"
            result.append({"path": str(path), "generated_at": _timestamp(data["generated_at"]).isoformat(), "stage": data.get("stage"),
                           "category": category, "requests_this_run": data.get("requests_this_run")})
    return sorted(result, key=lambda item: str(item["generated_at"]))


def _quota_rows(data: dict[str, Any]) -> dict[str, dict[str, Any]]:
    if "quota" in data:
        rows = list(data["quota"].values())
    else:
        response = data["result"]
        if response.get("error_code") != 0:
            raise ChoiceError("failed account snapshot cannot prove a quota")
        rows = [dict(zip(response["indicators"], values, strict=True)) for values in response["data"].values()]
    result = {}
    for row in rows:
        function = row["FUNCENAME"]
        if function not in {*LIMITS, "EM_CFC"} or (function == "EM_CTR" and row.get("SECUTYPE") != "分红送转"):
            continue
        if function in result:
            raise ChoiceError("duplicate quota packages require manual accounting")
        selected = {field: row.get(field) for field in QUOTA_FIELDS}
        selected["AVAILABEDATA"] = int(selected["AVAILABEDATA"])
        if selected["PERIOD"] is not None:
            quota_contract(selected)
        if selected["AVAILABEDATA"] < 0:
            raise ChoiceError("invalid negative reported quota")
        result[function] = selected
    return result


def _account_snapshots(directories: list[Path]) -> list[dict[str, Any]]:
    result = []
    for directory in directories:
        for path in _archive_files(directory / "account"):
            data = _hashed_document(path)
            queried = _timestamp(data["queried_at"])
            result.append({"path": str(path), "queried_at": queried.isoformat(), "quota": _quota_rows(data)})
    return sorted(result, key=lambda item: (item["queried_at"], item["path"]))


def _ledger(control: Path) -> list[dict[str, Any]]:
    path = _safe_path(control / "budget.sqlite3")
    _require_quiescent_database(path)
    before = _database_identity(path)
    db = sqlite3.connect(path.as_uri() + "?mode=ro&immutable=1", uri=True)
    try:
        db.execute("PRAGMA query_only=ON")
        if db.execute("PRAGMA user_version").fetchone()[0] != 7411:
            raise ChoiceError("unsupported Choice budget ledger")
        columns = [row[1] for row in db.execute("PRAGMA table_info(reservations)")]
        if columns != ["period", "function", "units", "created_at"]:
            raise ChoiceError("changed ledger schema requires an updated audit")
        rows = db.execute("SELECT rowid,period,function,units,created_at FROM reservations ORDER BY rowid LIMIT 5001").fetchall()
        if _database_identity(path) != before:
            raise ChoiceError("quota ledger changed during the read-only audit")
        _require_quiescent_database(path)
        return _validated_reservations(rows)
    finally:
        db.close()


def _database_identity(path: Path) -> tuple[int, ...]:
    value = path.stat()
    return value.st_dev, value.st_ino, value.st_size, value.st_mtime_ns, value.st_ctime_ns


def _validated_reservations(rows: list[tuple]) -> list[dict[str, Any]]:
    if len(rows) > 5000:
        raise ChoiceError("quota ledger exceeds bounded audit scope")
    if any(not isinstance(row[3], int) or row[3] <= 0 for row in rows):
        raise ChoiceError("invalid reservation units")
    result = [dict(zip(["rowid", "period", "function", "units", "created_at"], row, strict=True)) for row in rows]
    for item in result:
        item["created_at"] = _timestamp(item["created_at"]).isoformat()
    if [item["created_at"] for item in result] != sorted(item["created_at"] for item in result):
        raise ChoiceError("reservation timestamps are not ordered")
    return result


def _receipt_calls(receipts: list[dict], orphans: list[dict]) -> list[dict[str, Any]]:
    calls = []
    for receipt in [*(item for group in receipts for item in group.values()), *orphans]:
        descriptor = receipt["request"]
        if descriptor["method"] not in FUNCTIONS:
            continue
        calls.append({"request_key": receipt["request_key"], "raw_path": receipt["raw_path"],
                      "function": FUNCTIONS[descriptor["method"]], "estimated_units": _estimated_units(descriptor),
                      "captured_at": receipt["captured_at"], "outcome": receipt.get("outcome", "completed_raw_replay_verified")})
    return sorted(calls, key=lambda item: (item["captured_at"], item["request_key"]))


def _reservation_candidates(rows: list[dict], calls: list[dict], failures: list[dict]) -> list[dict[str, Any]]:
    output = []
    for index, row in enumerate(rows):
        if row["function"] not in LIMITS:
            continue
        end = rows[index + 1]["created_at"] if index + 1 < len(rows) else "9999"
        candidates = [call["request_key"] for call in calls if call["function"] == row["function"]
                      and call["estimated_units"] == row["units"] and row["created_at"] <= call["captured_at"] <= end]
        pauses = [item["path"] for item in failures if row["created_at"] <= str(item["generated_at"]) <= end
                  and item["category"] == "native_call_timeout_outcome_uncertain"]
        output.append({**row, "receipt_candidates_by_time_and_cost": candidates, "timeout_progress_candidates": pauses,
                       "exact_request_binding_proven": False, "provider_settlement_proven": False})
    return output


def _period_usage(function: str, rows: list[dict], calls: list[dict], quota: dict) -> dict[str, Any]:
    period = f'{quota["STARTDATE"]}/{quota["ENDDATE"]}'
    reserved = [row for row in rows if row["function"] == function and row["period"] == period]
    current_calls = [item for item in calls if item["function"] == function
                     and quota["STARTDATE"] <= item["captured_at"][:10] <= quota["ENDDATE"]]
    used = sum(row["units"] for row in reserved)
    completed = sum(item["estimated_units"] for item in current_calls if item["outcome"] == "completed_raw_replay_verified")
    return {"period": period, "reservation_count": len(reserved), "reserved_units": used,
            "completed_receipt_count": sum(item["outcome"] == "completed_raw_replay_verified" for item in current_calls),
            "completed_receipt_estimated_units": completed, "reserved_minus_completed_estimated_units": used - completed}


def _function_quota(function: str, rows: list[dict], calls: list[dict], snapshots: list[dict], audited_at: datetime) -> dict[str, Any]:
    if not snapshots or function not in snapshots[-1]["quota"]:
        return {"status": "paused_missing_quota", "safe_units_at_snapshot": 0, "provider_settlement_proven": False}
    available = [item for item in snapshots if function in item["quota"]]
    first, latest = available[0], available[-1]
    quota = latest["quota"][function]
    usage = _period_usage(function, rows, calls, quota)
    today = audited_at.date().isoformat()
    in_period = str(quota["STARTDATE"]) <= today <= str(quota["ENDDATE"]) and today <= str(quota["EFFECTIVEDATE"])[:10]
    safe = max(0, min(LIMITS[function], quota["AVAILABEDATA"]) - usage["reserved_units"]) if in_period else 0
    same_period = first["quota"][function]["STARTDATE"] == quota["STARTDATE"] and first["quota"][function]["ENDDATE"] == quota["ENDDATE"]
    return {"status": "offline_css_plan_only_refresh_and_reserve_before_use" if safe and function == "EM_CSS" else "paused",
            **usage, "reported_remaining": quota["AVAILABEDATA"], "default_limit": LIMITS[function],
            "safe_units_at_snapshot": safe, "quota_period_valid_at_audit": in_period,
            "first_to_latest_reported_decrease": first["quota"][function]["AVAILABEDATA"] - quota["AVAILABEDATA"] if same_period else None,
            "latest_snapshot": {key: latest[key] for key in ("path", "queried_at")}, "provider_settlement_proven": False}


def _quota_audit(control: Path, datasets: list[ChoiceDataset], receipts: list[dict], audited_at: datetime) -> dict[str, Any]:
    orphans = [item for dataset, known in zip(datasets, receipts, strict=True) for item in _orphan_receipts(dataset, known)]
    failures = _collection_failures(datasets)
    snapshots = _account_snapshots([*(dataset.directory for dataset in datasets), control])
    snapshots = [item for item in snapshots if datetime.fromisoformat(item["queried_at"]) <= audited_at]
    rows = _ledger(control)
    calls = _receipt_calls(receipts, orphans)
    candidates = _reservation_candidates(rows, calls, failures)
    functions = {name: _function_quota(name, rows, calls, snapshots, audited_at) for name in LIMITS}
    return {"functions": functions, "account_snapshot_count": len(snapshots), "account_snapshots": snapshots,
            "latest_reported_quotas": snapshots[-1]["quota"] if snapshots else {},
            "ledger_path": str(control / "budget.sqlite3"), "ledger_reservations_sha256": sha256_hex(canonical_json_bytes(rows)),
            "ledger_row_count": len(rows), "ledger_totals": _ledger_totals(rows),
            "ledger_schema": ["period", "function", "units", "created_at"], "reservations_released": 0,
            "exact_request_binding_proven": False, "provider_settlement_proven": False,
            "mapping_warning": "time/cost matches are candidates only; timestamps have second precision and no request/provider settlement id",
            "reservation_candidates": candidates, "receipt_calls": calls, "orphan_raw_responses": orphans,
            "collection_progress": failures, "no_receipt_candidate_count": sum(not item["receipt_candidates_by_time_and_cost"] for item in candidates),
            "ambiguous_receipt_candidate_count": sum(len(item["receipt_candidates_by_time_and_cost"]) > 1 for item in candidates)}


def _ledger_totals(rows: list[dict]) -> dict[str, Any]:
    counts: Counter[str] = Counter()
    units: Counter[str] = Counter()
    for row in rows:
        counts[row["function"]] += 1
        units[row["function"]] += row["units"]
    return {"counts_by_function": dict(counts), "units_by_function": dict(units)}


def _minimal_rechecks(evidence: dict, quota: dict) -> dict[str, Any]:
    required: list[dict[str, Any]] = []
    optional: list[dict[str, Any]] = []
    for row in evidence["priority_anomalies"]:
        if not row["archive_pattern_explained"]:
            required.append(row["minimal_css_reference_recheck"])
        if row["intraday_halt"] or row["uncertain_trading_state"]:
            target = optional if row["halt_date_evidence_consistent"] else required
            target.append(_css_request("suspension_detail", row["symbol"], row["session_date"]))
    for item in optional:
        item["recommended"] = False
        item["reason"] = "already_raw_replay_verified; duplicate date-only fields cannot improve intraday execution evidence"
    return {"required_css_requests": required, "required_css_cells": sum(row["estimated_cells"] for row in required),
            "optional_css_spot_checks": optional, "optional_css_cells": sum(row["estimated_cells"] for row in optional),
            "css_safe_units_at_snapshot": quota["functions"]["EM_CSS"]["safe_units_at_snapshot"],
            "csd_paused": True, "ctr_paused": True, "release_reservations": False, "execute_automatically": False,
            "reason": "existing listing-window and halt-date evidence is reusable; repeat CSS cannot prove no-limit rules or intraday execution",
            "remaining_research_gaps": ["independent_exchange_membership_and_listing_rule_reconciliation",
                                        "original_vintage_and_precise_halt_intervals", "complete_corporate_actions", "order_execution_evidence"]}


def audit_choice_research(history_dir: Path, supplement_dir: Path, universe_dir: Path, control_dir: Path,
                          *, audited_at: datetime | None = None) -> dict[str, Any]:
    """Verify complete raw replays without mutating inputs or reserving quota."""
    instant = audited_at or market_now()
    if instant.tzinfo is None:
        raise ChoiceError("audit time must include a timezone")
    instant = instant.astimezone(ZoneInfo("Asia/Shanghai"))
    directories = [_safe_path(path) for path in (history_dir, supplement_dir, universe_dir, control_dir)]
    if len(set(directories)) != 4:
        raise ChoiceError("audit source and control directories must be distinct")
    with ExitStack() as stack:
        datasets = [_open_dataset(stack, path) for path in directories[:3]]
        verified = [_verify_dataset(dataset) for dataset in datasets]
        summaries, receipts = [item[0] for item in verified], [item[1] for item in verified]
        _check_source_bindings(*datasets)
        evidence = _reference_audit(datasets[0], datasets[1], receipts)
        quota = _quota_audit(directories[3], datasets, receipts, instant)
        return {"schema_version": AUDIT_VERSION, "audited_at": instant.isoformat(), "status": "offline_evidence_audit_complete",
                "read_only": True, "raw_replay_verified": True, "sdk_requests": 0, "reservations_released": 0,
                "raw_replay_scope": "completed_request_records; orphan responses reported separately without projection",
                "official": False, "formal_equivalent_pit": False, "filter_qualified": False, "production_ranking_effect": "none",
                "datasets": summaries, "reference_audit": evidence, "quota_reconciliation": quota,
                "supplement_coverage": supplement_coverage(datasets[0], datasets[1]),
                "universe_coverage": universe_coverage(datasets[2].plan, datasets),
                "minimal_recheck_plan": _minimal_rechecks(evidence, quota), "limitations": READ_ONLY_LIMITATIONS}


def publish_choice_audit(report: dict[str, Any], output_dir: Path, source_directories: list[Path]) -> Path:
    """Opt-in immutable report publication; the input archives remain read-only."""
    target = _safe_path(output_dir)
    protected = {".workbuddy-ai", ".git", ".codex", ".agents", ".venv"}
    if protected.intersection(target.parts) or target.parent == target:
        raise ChoiceError("audit output directory is protected")
    sources = [_safe_path(path) for path in source_directories]
    if any(target == source or source in target.parents for source in sources):
        raise ChoiceError("audit output cannot be inside a source or control directory")
    encoded = canonical_json_bytes(report)
    path = target / f"choice-research-audit-{sha256_hex(encoded)}.json"
    exclusive_atomic_publish(path, encoded, max_bytes=16 * 1024 * 1024)
    return path
