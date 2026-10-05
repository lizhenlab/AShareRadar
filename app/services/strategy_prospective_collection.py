"""Capture predeclared signal inputs before their deadlines, never backfill them."""

from __future__ import annotations

from collections.abc import Mapping
from contextlib import closing
from dataclasses import asdict
from datetime import date, datetime, time, timezone
import json
from pathlib import Path
import sqlite3
from typing import cast

from app.models.market_scan import MARKET_SCAN_FULL_MARKET_SCOPE
from app.services.strategy_prospective_plan import (
    append_strategy_prospective_receipt, read_strategy_prospective_state, strategy_prospective_status,
)
from app.services.strategy_prospective_snapshot import archive_strategy_scan
from app.services.strategy_template_tracking import freeze_strategy_template_session
from app.services.trading_calendar import ASHARE_TIMEZONE
from app.utils.audit_time import audit_time_epoch
from app.utils.clock import utc_now


def collect_strategy_prospective_inputs(database: Path, root: Path, plan_id: str) -> dict[str, object]:
    """Collect each due anchor once; a missed deadline stays missing on every retry."""
    plan, receipts = read_strategy_prospective_state(root, plan_id)
    spec = cast(dict[str, object], plan["specification"])
    existing = {str(item["trade_date"]) for item in receipts}
    added = []
    for day in cast(list[str], spec["signal_dates"]):
        now = utc_now()
        if day in existing:
            continue
        if day > now.astimezone(ASHARE_TIMEZONE).date().isoformat():
            break
        cutoff = datetime.combine(date.fromisoformat(day), time.fromisoformat(str(spec["cutoff_local"])), ASHARE_TIMEZONE)
        if now > cutoff:
            payload: dict[str, object] | None = {"status": "missing", "session": None, "available_at": None,
                                               "reason": "not_captured_before_deadline"}
        elif now.astimezone(ASHARE_TIMEZONE).time() < time(15, 15):
            break
        else:
            payload = _capture_source(database, root / plan_id, day, spec, now)
        if payload is None:
            break
        receipt = append_strategy_prospective_receipt(root, plan_id, day, payload)
        added.append({"trade_date": day, "status": receipt["status"], "digest": receipt["digest"]})
    return {"operation": "capture", "receipts_added": added, "state": strategy_prospective_status(root, plan_id)}


def _capture_source(database: Path, directory: Path, day: str, spec: Mapping[str, object], now: datetime) -> dict[str, object] | None:
    with closing(sqlite3.connect(f"{database.resolve().as_uri()}?mode=ro", uri=True)) as conn:
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA query_only = ON")
        conn.execute("BEGIN")
        row = _first_published_scan(conn, day, now)
        if row is None:
            return None
        payload = _freeze_source(conn, row, spec, now)
        payload.update(archive_strategy_scan(conn, row, directory))
        payload.update(run_id=int(row["id"]), data_date=row["data_date"], source_snapshot_digest=row["snapshot_digest"],
                       rule_version=row["rule_version"], score_spec_hash=row["declared_score_spec_hash"],
                       source_result_counts={key: row[key] for key in ("total_count", "success_count", "missing_count", "skipped_count")})
    return payload


def _first_published_scan(conn: sqlite3.Connection, day: str, now: datetime) -> sqlite3.Row | None:
    rows = conn.execute(
        """SELECT r.*, c.production_score_spec_hash AS declared_score_spec_hash
           FROM market_scan_run r LEFT JOIN market_scan_rule_contract c ON c.rule_version=r.rule_version
           WHERE r.data_date=? AND r.mode='official' AND r.scope=? AND r.status IN ('success','degraded')
           ORDER BY r.id LIMIT 101""", (day, MARKET_SCAN_FULL_MARKET_SCOPE),
    ).fetchall()
    if len(rows) > 100:
        raise ValueError("too many published scans for one prospective day")
    eligible = [row for row in rows if (stamp := audit_time_epoch(row["finished_at"])) is None or stamp <= now.timestamp()]
    return min(eligible, key=lambda row: (audit_time_epoch(row["finished_at"]) or float("-inf"), int(row["id"]))) if eligible else None


def _freeze_source(conn: sqlite3.Connection, row: sqlite3.Row, spec: Mapping[str, object], now: datetime) -> dict[str, object]:
    available = max(audit_time_epoch(row[key]) or 0 for key in ("finished_at", "snapshot_sealed_at"))
    payload: dict[str, object] = {"available_at": now.isoformat(), "observed_at": now.isoformat(),
                                  "source_available_at": datetime.fromtimestamp(available, timezone.utc).isoformat() if available else None,
                                  "status": "source_incomplete", "session": None}
    if row["declared_score_spec_hash"] != spec["production_score_spec_hash"]:
        return {**payload, "reason": "registered_score_contract_mismatch"}
    try:
        session = freeze_strategy_template_session(
            conn, row, as_of=now, horizon=int(cast(int, spec["horizon"])),
            notional_cash_cny=float(cast(float, spec["notional_cash_cny"])),
            templates=cast(list[Mapping[str, object]], spec["templates"]),
        )
    except ValueError as exc:
        reason = getattr(exc, "reason", "frozen_source_admission_failed")
        return {**payload, "reason": reason if isinstance(reason, str) else "frozen_source_admission_failed"}
    return {**payload, "available_at": payload["source_available_at"], "status": "captured",
            "session": json.loads(json.dumps(asdict(session), allow_nan=False)), "reason": None}
