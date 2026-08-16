"""Persistence for version-pinned strategy automation and research plans."""

from __future__ import annotations

import json
import sqlite3
import threading
from pathlib import Path
from typing import Mapping

from app.artifacts.io import ArtifactIOError, canonical_json_text, decode_json_bytes, sha256_hex
from app.db.market_scan_action_source import require_market_scan_action_source
from app.models.market_scan import MARKET_SCAN_FULL_MARKET_SCOPE
from app.models.strategy_automation import (
    StrategyAlertCondition,
    StrategyAlertEvent,
    StrategyAlertEventPage,
    StrategySchedule,
    StrategyScheduleCreate,
    StrategySchedulePage,
    StrategySimulationPlan,
)
from app.repositories.base import SQLiteRepository
from app.utils.errors import NotFoundError


class StrategyAutomationIntegrityError(RuntimeError):
    """Stored automation evidence no longer matches its execution seal."""


class StrategyAutomationRepository(SQLiteRepository):
    def __init__(self, path: Path, lock: threading.RLock | None = None) -> None:
        super().__init__(Path(path), lock or threading.RLock())

    def create_schedule(
        self,
        payload: StrategyScheduleCreate,
        *,
        revision: int,
        fingerprint: str,
        timestamp: str,
    ) -> StrategySchedule:
        with self._lock, self._connect() as conn:
            cursor = conn.execute(
                """
                INSERT INTO strategy_schedule (
                    strategy_id, strategy_revision, strategy_fingerprint,
                    cadence, mode, notional_cash_cny, alert_conditions_json,
                    enabled, created_at, updated_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, 1, ?, ?)
                """,
                (
                    payload.strategy_id,
                    revision,
                    fingerprint,
                    payload.cadence,
                    payload.mode,
                    payload.notional_cash_cny,
                    json.dumps(
                        [item.model_dump(mode="json") for item in payload.alert_conditions],
                        ensure_ascii=False,
                        sort_keys=True,
                        separators=(",", ":"),
                    ),
                    timestamp,
                    timestamp,
                ),
            )
            schedule_id = cursor.lastrowid
            row = _schedule_row(conn, int(schedule_id or 0))
        return _schedule_from_row(row)

    def schedule(self, schedule_id: int) -> StrategySchedule:
        with self._lock, self._read_snapshot() as conn:
            row = _schedule_row(conn, schedule_id)
        return _schedule_from_row(row)

    def schedules(
        self,
        *,
        strategy_id: int | None,
        include_disabled: bool,
        page: int,
        page_size: int,
    ) -> StrategySchedulePage:
        clauses = []
        params: list[object] = []
        if strategy_id is not None:
            clauses.append("strategy_id = ?")
            params.append(strategy_id)
        if not include_disabled:
            clauses.append("enabled = 1")
        where = " AND ".join(clauses) if clauses else "1 = 1"
        offset = (page - 1) * page_size
        with self._lock, self._read_snapshot() as conn:
            total = int(conn.execute(f"SELECT COUNT(*) FROM strategy_schedule WHERE {where}", params).fetchone()[0])
            rows = conn.execute(
                f"""
                SELECT * FROM strategy_schedule WHERE {where}
                ORDER BY enabled DESC, updated_at DESC, id DESC
                LIMIT ? OFFSET ?
                """,
                [*params, page_size, offset],
            ).fetchall()
        return StrategySchedulePage(
            items=[_schedule_from_row(row) for row in rows],
            total=total,
            page=page,
            page_size=page_size,
            page_count=(total + page_size - 1) // page_size,
        )

    def enabled_schedules(self) -> list[StrategySchedule]:
        items: list[StrategySchedule] = []
        page_number = 1
        while True:
            page = self.schedules(
                strategy_id=None,
                include_disabled=False,
                page=page_number,
                page_size=100,
            )
            items.extend(page.items)
            if page_number >= page.page_count:
                return items
            page_number += 1

    def set_enabled(self, schedule_id: int, *, enabled: bool, timestamp: str) -> StrategySchedule:
        with self._lock, self._connect() as conn:
            cursor = conn.execute(
                "UPDATE strategy_schedule SET enabled = ?, updated_at = ? WHERE id = ?",
                (int(enabled), timestamp, schedule_id),
            )
            if cursor.rowcount != 1:
                raise NotFoundError(f"策略定时任务不存在：{schedule_id}")
            row = _schedule_row(conn, schedule_id)
        return _schedule_from_row(row)

    def latest_published_run_id(self, mode: str) -> int | None:
        with self._lock, self._read_snapshot() as conn:
            row = conn.execute(
                """
                SELECT id FROM market_scan_run
                WHERE mode = ? AND status IN ('success', 'degraded')
                  AND scope = ?
                ORDER BY data_date DESC, as_of DESC, id DESC
                LIMIT 1
                """,
                (mode, MARKET_SCAN_FULL_MARKET_SCOPE),
            ).fetchone()
            if row is None:
                return None
            run_id = int(row["id"])
            require_market_scan_action_source(conn, run_id)
            return run_id

    def claim_run(self, schedule_id: int, run_id: int, *, timestamp: str) -> bool:
        with self._lock, self._connect() as conn:
            conn.execute("BEGIN IMMEDIATE")
            existing = conn.execute(
                "SELECT status FROM strategy_schedule_run WHERE schedule_id = ? AND market_scan_run_id = ?",
                (schedule_id, run_id),
            ).fetchone()
            if existing is not None and str(existing["status"]) in {"running", "completed"}:
                return False
            if existing is None:
                conn.execute(
                    """
                    INSERT INTO strategy_schedule_run (
                        schedule_id, market_scan_run_id, status, started_at
                    ) VALUES (?, ?, 'running', ?)
                    """,
                    (schedule_id, run_id, timestamp),
                )
            else:
                conn.execute(
                    """
                    UPDATE strategy_schedule_run
                    SET status = 'running', execution_id = NULL, error = NULL,
                        started_at = ?, finished_at = NULL
                    WHERE schedule_id = ? AND market_scan_run_id = ?
                    """,
                    (timestamp, schedule_id, run_id),
                )
        return True

    def finish_run(
        self,
        schedule_id: int,
        run_id: int,
        *,
        execution_id: int | None,
        error: str | None,
        timestamp: str,
    ) -> None:
        status = "completed" if execution_id is not None else "failed"
        with self._lock, self._connect() as conn:
            conn.execute("BEGIN IMMEDIATE")
            conn.execute(
                """
                UPDATE strategy_schedule_run
                SET status = ?, execution_id = ?, error = ?, finished_at = ?
                WHERE schedule_id = ? AND market_scan_run_id = ?
                """,
                (status, execution_id, error, timestamp, schedule_id, run_id),
            )
            if execution_id is not None:
                conn.execute(
                    """
                    UPDATE strategy_schedule
                    SET last_execution_id = ?, last_market_scan_run_id = ?, updated_at = ?
                    WHERE id = ?
                    """,
                    (execution_id, run_id, timestamp, schedule_id),
                )

    def add_event(
        self,
        schedule: StrategySchedule,
        *,
        execution_id: int,
        execution_fingerprint: str,
        data_as_of: str,
        event_type: str,
        symbol: str | None,
        message: str,
        trigger: dict[str, object],
        timestamp: str,
    ) -> StrategyAlertEvent:
        rendered = json.dumps(trigger, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
        with self._lock, self._connect() as conn:
            cursor = conn.execute(
                """
                INSERT INTO strategy_alert_event (
                    schedule_id, strategy_id, strategy_revision, strategy_fingerprint,
                    execution_id, execution_fingerprint, data_as_of, event_type,
                    symbol, message, trigger_json, created_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    schedule.schedule_id,
                    schedule.strategy_id,
                    schedule.strategy_version,
                    schedule.strategy_fingerprint,
                    execution_id,
                    execution_fingerprint,
                    data_as_of,
                    event_type,
                    symbol,
                    message,
                    rendered,
                    timestamp,
                ),
            )
            row = conn.execute(
                "SELECT * FROM strategy_alert_event WHERE id = ?",
                (int(cursor.lastrowid or 0),),
            ).fetchone()
        return _event_from_row(row)

    def events(
        self,
        *,
        strategy_id: int | None,
        schedule_id: int | None,
        page: int,
        page_size: int,
    ) -> StrategyAlertEventPage:
        clauses = []
        params: list[object] = []
        if strategy_id is not None:
            clauses.append("strategy_id = ?")
            params.append(strategy_id)
        if schedule_id is not None:
            clauses.append("schedule_id = ?")
            params.append(schedule_id)
        where = " AND ".join(clauses) if clauses else "1 = 1"
        offset = (page - 1) * page_size
        with self._lock, self._read_snapshot() as conn:
            total = int(conn.execute(f"SELECT COUNT(*) FROM strategy_alert_event WHERE {where}", params).fetchone()[0])
            rows = conn.execute(
                f"""
                SELECT * FROM strategy_alert_event WHERE {where}
                ORDER BY created_at DESC, id DESC LIMIT ? OFFSET ?
                """,
                [*params, page_size, offset],
            ).fetchall()
        return StrategyAlertEventPage(
            items=[_event_from_row(row) for row in rows],
            total=total,
            page=page,
            page_size=page_size,
            page_count=(total + page_size - 1) // page_size,
        )

    def save_simulation_plan(
        self,
        *,
        values: dict[str, object],
        rendered_plan: str,
        timestamp: str,
    ) -> StrategySimulationPlan:
        with self._lock, self._connect() as conn:
            execution_id = _plan_execution_id(values)
            existing = _simulation_plan_row(conn, execution_id)
            if existing is None:
                conn.execute(
                    """
                    INSERT INTO strategy_simulation_plan (
                        execution_id, strategy_id, strategy_revision, strategy_fingerprint,
                        execution_fingerprint, rule_version, data_as_of,
                        cost_rule_fingerprint, status, plan_json, plan_digest, created_at
                    ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                    """,
                    (
                        values["execution_id"],
                        values["strategy_id"],
                        values["strategy_version"],
                        values["strategy_fingerprint"],
                        values["execution_fingerprint"],
                        values["rule_version"],
                        values["data_as_of"],
                        values["cost_rule_fingerprint"],
                        values["status"],
                        rendered_plan,
                        values["plan_digest"],
                        timestamp,
                    ),
                )
                row = _simulation_plan_row(conn, execution_id)
            else:
                if str(existing["plan_digest"]) != values["plan_digest"]:
                    raise StrategyAutomationIntegrityError("同一策略执行已存在不同的模拟计划摘要")
                row = existing
            if row is None:
                raise StrategyAutomationIntegrityError("模拟计划保存后无法读取")
            plan = _simulation_plan_from_row(row)
        return plan

    def simulation_plan(self, execution_id: int) -> StrategySimulationPlan | None:
        with self._lock, self._read_snapshot() as conn:
            row = _simulation_plan_row(conn, execution_id)
        return _simulation_plan_from_row(row) if row is not None else None


def _schedule_row(conn: sqlite3.Connection, schedule_id: int) -> sqlite3.Row:
    row = conn.execute("SELECT * FROM strategy_schedule WHERE id = ?", (schedule_id,)).fetchone()
    if row is None:
        raise NotFoundError(f"策略定时任务不存在：{schedule_id}")
    return row


def _schedule_from_row(row: sqlite3.Row) -> StrategySchedule:
    return StrategySchedule(
        schedule_id=int(row["id"]),
        strategy_id=int(row["strategy_id"]),
        strategy_version=int(row["strategy_revision"]),
        strategy_fingerprint=str(row["strategy_fingerprint"]),
        cadence=str(row["cadence"]),
        mode=str(row["mode"]),
        notional_cash_cny=float(row["notional_cash_cny"]),
        alert_conditions=[StrategyAlertCondition.model_validate(item) for item in json.loads(str(row["alert_conditions_json"]))],
        enabled=bool(row["enabled"]),
        last_execution_id=int(row["last_execution_id"]) if row["last_execution_id"] is not None else None,
        last_market_scan_run_id=(int(row["last_market_scan_run_id"]) if row["last_market_scan_run_id"] is not None else None),
        created_at=str(row["created_at"]),
        updated_at=str(row["updated_at"]),
    )


def _event_from_row(row: sqlite3.Row) -> StrategyAlertEvent:
    return StrategyAlertEvent(
        event_id=int(row["id"]),
        schedule_id=int(row["schedule_id"]),
        strategy_id=int(row["strategy_id"]),
        strategy_version=int(row["strategy_revision"]),
        strategy_fingerprint=str(row["strategy_fingerprint"]),
        execution_id=int(row["execution_id"]),
        execution_fingerprint=str(row["execution_fingerprint"]),
        data_as_of=str(row["data_as_of"]),
        event_type=str(row["event_type"]),
        symbol=str(row["symbol"]) if row["symbol"] is not None else None,
        message=str(row["message"]),
        trigger=json.loads(str(row["trigger_json"])),
        created_at=str(row["created_at"]),
    )


def _simulation_plan_from_row(row: sqlite3.Row) -> StrategySimulationPlan:
    payload = _simulation_plan_payload(row)
    _verify_simulation_plan_row(row, payload)
    return StrategySimulationPlan.model_validate(
        {
            **payload,
            "plan_id": int(row["id"]),
            "plan_digest": str(row["plan_digest"]),
            "created_at": str(row["created_at"]),
        }
    )


def _simulation_plan_row(
    conn: sqlite3.Connection,
    execution_id: int,
) -> sqlite3.Row | None:
    return conn.execute(
        """
        SELECT p.*,
               e.strategy_id AS execution_strategy_id,
               e.strategy_revision AS execution_strategy_revision,
               e.strategy_fingerprint AS source_strategy_fingerprint,
               e.execution_fingerprint AS source_execution_fingerprint,
               e.rule_version AS source_rule_version,
               e.data_as_of AS source_data_as_of,
               e.cost_rule_fingerprint AS source_cost_rule_fingerprint
        FROM strategy_simulation_plan AS p
        JOIN strategy_execution AS e ON e.id = p.execution_id
        WHERE p.execution_id = ?
        """,
        (execution_id,),
    ).fetchone()


def _plan_execution_id(values: Mapping[str, object]) -> int:
    value = values.get("execution_id")
    if not isinstance(value, int) or isinstance(value, bool) or value < 1:
        raise StrategyAutomationIntegrityError("模拟计划 execution_id 无效")
    return value


def _simulation_plan_payload(row: sqlite3.Row) -> dict[str, object]:
    try:
        payload = decode_json_bytes(str(row["plan_json"]).encode("utf-8"))
    except ArtifactIOError as exc:
        raise StrategyAutomationIntegrityError("模拟计划 JSON 无效") from exc
    if not isinstance(payload, dict) or not all(isinstance(key, str) for key in payload):
        raise StrategyAutomationIntegrityError("模拟计划根节点无效")
    digest = payload.get("plan_digest")
    unsigned = {name: value for name, value in payload.items() if name != "plan_digest"}
    if digest != str(row["plan_digest"]) or digest != _digest(unsigned):
        raise StrategyAutomationIntegrityError("模拟计划摘要不一致")
    return payload


def _verify_simulation_plan_row(
    row: sqlite3.Row,
    payload: Mapping[str, object],
) -> None:
    expected = {
        "execution_id": int(row["execution_id"]),
        "strategy_id": int(row["strategy_id"]),
        "strategy_version": int(row["strategy_revision"]),
        "strategy_fingerprint": str(row["strategy_fingerprint"]),
        "execution_fingerprint": str(row["execution_fingerprint"]),
        "rule_version": str(row["rule_version"]),
        "data_as_of": str(row["data_as_of"]),
        "cost_rule_fingerprint": str(row["cost_rule_fingerprint"]),
        "status": str(row["status"]),
    }
    if any(payload.get(name) != value for name, value in expected.items()):
        raise StrategyAutomationIntegrityError("模拟计划与数据库身份不一致")
    _verify_simulation_execution_binding(row)


def _verify_simulation_execution_binding(row: sqlite3.Row) -> None:
    expected = {
        "strategy_id": int(row["execution_strategy_id"]),
        "strategy_revision": int(row["execution_strategy_revision"]),
        "strategy_fingerprint": str(row["source_strategy_fingerprint"]),
        "execution_fingerprint": str(row["source_execution_fingerprint"]),
        "rule_version": str(row["source_rule_version"]),
        "data_as_of": str(row["source_data_as_of"]),
        "cost_rule_fingerprint": str(row["source_cost_rule_fingerprint"]),
    }
    if any(row[name] != value for name, value in expected.items()):
        raise StrategyAutomationIntegrityError("模拟计划未绑定原始策略执行")


def _digest(value: object) -> str:
    return sha256_hex(canonical_json_text(value))


__all__ = ["StrategyAutomationIntegrityError", "StrategyAutomationRepository"]
