"""Reconstruct a sealed input in isolation and verify its recorded frozen basket."""

from __future__ import annotations

from collections.abc import Mapping
from contextlib import closing
from dataclasses import asdict
import json
import math
from pathlib import Path
import re
import sqlite3
from typing import cast

from app.artifacts.io import canonical_json_bytes
from app.services.market_scan_trial_registry_contract import registry_object, registry_timestamp
from app.services.strategy_prospective_snapshot import visit_strategy_scan_archive
from app.services.strategy_template_tracking import freeze_strategy_template_session
from app.services.strategy_template_tracking_metrics import StrategyTrackingSession


_SQL_IDENTIFIER = re.compile(r"[a-z][a-z0-9_]{0,127}")


def replay_prospective_captured_session(
    directory: Path, specification: Mapping[str, object], receipt: Mapping[str, object],
) -> StrategyTrackingSession:
    """Recheck admission and selection using only the complete archived source tables."""
    payload = registry_object(receipt.get("payload"), "captured payload")
    if receipt.get("status") != "captured" or payload.get("status") != "captured":
        raise ValueError("prospective replay requires an on-time captured receipt")
    if type(payload.get("snapshot_result_count")) is not int or cast(int, payload["snapshot_result_count"]) <= 0:
        raise ValueError("captured source has no complete result set")
    with closing(sqlite3.connect(":memory:")) as conn:
        conn.row_factory = sqlite3.Row
        visit_strategy_scan_archive(
            directory, payload, on_header=lambda header: _restore_header(conn, header, specification, receipt),
            on_result=lambda row: _restore_row(conn, "market_scan_result", row),
        )
        conn.commit()
        conn.execute("PRAGMA query_only = ON")
        conn.execute("BEGIN")
        row = conn.execute("""SELECT r.*, c.production_score_spec_hash AS declared_score_spec_hash
            FROM market_scan_run r LEFT JOIN market_scan_rule_contract c ON c.rule_version = r.rule_version""").fetchone()
        session = freeze_strategy_template_session(
            conn, row, as_of=registry_timestamp(receipt["recorded_at"], "receipt time"),
            horizon=cast(int, specification["horizon"]), notional_cash_cny=cast(float, specification["notional_cash_cny"]),
            templates=cast(list[Mapping[str, object]], specification["templates"]),
        )
    replayed = json.loads(json.dumps(asdict(session), allow_nan=False))
    if canonical_json_bytes(replayed) != canonical_json_bytes(payload.get("session")):
        raise ValueError("captured basket differs from its archived source replay")
    return session


def _restore_header(
    conn: sqlite3.Connection, header: Mapping[str, object], specification: Mapping[str, object], receipt: Mapping[str, object],
) -> None:
    run = dict(registry_object(header["run"], "source run"))
    contract = registry_object(header["rule_contract"], "source rule contract")
    score = run.pop("declared_score_spec_hash", None)
    if run.get("data_date") != receipt["trade_date"] or run.get("quote_date") != receipt["trade_date"]:
        raise ValueError("captured source dates disagree with its receipt")
    if score != specification["production_score_spec_hash"] or contract.get("production_score_spec_hash") != score:
        raise ValueError("captured source score disagrees with its frozen plan")
    if contract.get("rule_version") != run.get("rule_version"):
        raise ValueError("captured source rule registration mismatch")
    _restore_row(conn, "market_scan_run", run)
    _restore_row(conn, "market_scan_rule_contract", contract)


def _restore_row(conn: sqlite3.Connection, table: str, row: Mapping[str, object]) -> None:
    columns = tuple(row)
    if not columns or len(columns) > 256 or any(_SQL_IDENTIFIER.fullmatch(key) is None for key in columns):
        raise ValueError("prospective source has invalid SQL columns")
    existing = tuple(str(item[1]) for item in conn.execute(f'PRAGMA table_info("{table}")'))
    names = ",".join(f'"{key}"' for key in columns)
    if not existing:
        conn.execute(f'CREATE TABLE "{table}" ({names})')
    elif columns != existing:
        raise ValueError("prospective source table columns changed between rows")
    values = tuple(_sql_scalar(value) for value in row.values())
    conn.execute(f'INSERT INTO "{table}" ({names}) VALUES ({",".join("?" for _ in columns)})', values)


def _sql_scalar(value: object) -> str | int | float | None:
    if value is None or type(value) is str:
        return value
    if type(value) is int and -(2 ** 63) <= value < 2 ** 63:
        return value
    if type(value) is float and math.isfinite(value):
        return value
    raise ValueError("prospective source has a non-SQL or out-of-range value")
