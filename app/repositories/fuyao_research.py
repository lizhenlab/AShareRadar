"""Append-only source observations and persistent request budget in a sidecar DB."""

from __future__ import annotations

import hashlib
import json
from pathlib import Path
import sqlite3
import threading
from typing import Any

from app.db.connection import SQLiteConnectionFactory
from app.models.fuyao import FINANCIAL_PARTIAL_WARNING, FinancialReportBundle
from app.models.fuyao_research import FuyaoJob, FuyaoObservation
from app.utils.audit_time import audit_now_text


_FINANCIAL_PERIODS_SQL = """WITH periods AS (
    SELECT o.id,
           json_extract(p.value,'$.period_end') AS period_end,
           json_extract(p.value,'$.period_type') AS period_type,
           ROW_NUMBER() OVER (
               PARTITION BY json_extract(p.value,'$.period_end'),json_extract(p.value,'$.period_type')
               ORDER BY (COALESCE(json_array_length(p.value,'$.statements'),0) > 0) DESC,o.id DESC
           ) AS period_rank
    FROM observations o, json_each(o.payload,'$.report.periods') p
    WHERE o.capability='financials' AND o.symbol=? AND (
        json_array_length(p.value,'$.statements') > 0 OR EXISTS (
            SELECT 1 FROM json_each(p.value,'$.metrics') f
            WHERE json_extract(f.value,'$.value') IS NOT NULL
               OR length(trim(COALESCE(json_extract(f.value,'$.raw_value'),''),char(9)||char(10)||char(11)||char(12)||char(13)||' ')) > 0
        )
    )
)
SELECT p.id,json_extract(o.payload,'$.report') AS report,p.period_end,p.period_type
FROM periods p JOIN observations o ON o.id=p.id WHERE p.period_rank=1
ORDER BY p.id DESC,p.period_end DESC,p.period_type"""


class FuyaoResearchRepository:
    def __init__(self, path: Path) -> None:
        self.path = path
        self._connections = SQLiteConnectionFactory(path)
        self._lock = threading.RLock()
        self._ready = False

    def initialize(self) -> None:
        with self._lock:
            if self._ready:
                return
            self.path.parent.mkdir(parents=True, exist_ok=True)
            with self._connections.connect() as conn:
                conn.execute("CREATE TABLE IF NOT EXISTS observations (id INTEGER PRIMARY KEY, capability TEXT NOT NULL, "
                             "symbol TEXT NOT NULL, fetched_at TEXT NOT NULL, digest TEXT NOT NULL, payload TEXT NOT NULL)")
                conn.execute("CREATE INDEX IF NOT EXISTS observations_lookup ON observations(capability,symbol,id DESC)")
                conn.execute("CREATE TABLE IF NOT EXISTS request_budget (day TEXT PRIMARY KEY, calls INTEGER NOT NULL)")
                conn.execute("CREATE TABLE IF NOT EXISTS jobs (id TEXT PRIMARY KEY, payload TEXT NOT NULL)")
                conn.execute("CREATE UNIQUE INDEX IF NOT EXISTS jobs_retry_parent ON jobs(json_extract(payload,'$.parent_job_id')) "
                             "WHERE json_extract(payload,'$.parent_job_id') IS NOT NULL")
                conn.commit()
            self._ready = True

    def reserve_request(self, day: str, limit: int) -> None:
        self.initialize()
        with self._lock, self._connections.connect() as conn:
            conn.execute("BEGIN IMMEDIATE")
            row = conn.execute("SELECT calls FROM request_budget WHERE day=?", (day,)).fetchone()
            count = row[0] if row else 0
            if count >= limit:
                raise RuntimeError("扶摇本地每日请求上限已到达")
            conn.execute("INSERT INTO request_budget VALUES (?,1) ON CONFLICT(day) DO UPDATE SET calls=calls+1", (day,))
            conn.commit()

    def request_count(self, day: str) -> int:
        if not self.path.is_file():
            return 0
        self.initialize()
        with self._connections.connect() as conn:
            row = conn.execute("SELECT calls FROM request_budget WHERE day=?", (day,)).fetchone()
        return int(row[0]) if row else 0

    def save_observation(self, capability: str, symbol: str, fetched_at: str, payload: dict[str, Any]) -> FuyaoObservation:
        encoded, digest = _encode_observation(payload)
        self.initialize()
        with self._lock, self._connections.connect() as conn:
            conn.execute("BEGIN IMMEDIATE")
            cursor = conn.execute("INSERT INTO observations(capability,symbol,fetched_at,digest,payload) VALUES (?,?,?,?,?)",
                                  (capability, symbol, fetched_at, digest, encoded))
            result = FuyaoObservation(id=cursor.lastrowid or 0, capability=capability, symbol=symbol,
                                     fetched_at=fetched_at, digest=digest, payload=payload)
            conn.commit()
        return result

    def publish_item(self, job: FuyaoJob, capability: str, symbol: str, fetched_at: str, payload: dict[str, Any]) -> FuyaoJob:
        """Publish one observation and its retry checkpoint in the same transaction."""
        encoded, digest = _encode_observation(payload)
        updated = job.model_copy(deep=True)
        updated.completed += 1
        updated.updated_at = audit_now_text()
        if capability in {"financials", "valuations"} and symbol not in updated.completed_symbols:
            updated.completed_symbols.append(symbol)
        self.initialize()
        with self._lock, self._connections.connect() as conn:
            conn.execute("BEGIN IMMEDIATE")
            conn.execute("INSERT INTO observations(capability,symbol,fetched_at,digest,payload) VALUES (?,?,?,?,?)",
                         (capability, symbol, fetched_at, digest, encoded))
            conn.execute("UPDATE jobs SET payload=? WHERE id=?", (updated.model_dump_json(), updated.id))
            if conn.execute("SELECT changes()").fetchone()[0] != 1:
                raise ValueError("同步任务不存在，未发布观察")
            stored = FuyaoJob.model_validate_json(conn.execute("SELECT payload FROM jobs WHERE id=?", (updated.id,)).fetchone()[0])
            if stored != updated:
                raise ValueError("同步任务校验不一致，未发布观察")
            conn.commit()
        return stored

    def latest(self, capability: str, symbol: str) -> FuyaoObservation | None:
        if not self.path.is_file():
            return None
        self.initialize()
        with self._connections.connect() as conn:
            row = conn.execute("SELECT * FROM observations WHERE capability=? AND symbol=? ORDER BY id DESC LIMIT 1",
                               (capability, symbol)).fetchone()
        return self._observation(row) if row else None

    def observations(self, capability: str, symbol: str, limit: int = 30) -> list[FuyaoObservation]:
        if not self.path.is_file():
            return []
        self.initialize()
        with self._connections.connect() as conn:
            rows = conn.execute("SELECT * FROM observations WHERE capability=? AND symbol=? ORDER BY id DESC LIMIT ?",
                                (capability, symbol, min(100, max(1, limit)))).fetchall()
        return [self._observation(row) for row in rows]

    def financials(self, symbol: str) -> FinancialReportBundle | None:
        """Read one coherent observation per period, preferring statements to indicators alone."""
        if not self.path.is_file():
            return None
        self.initialize()
        with self._connections.connect() as conn:
            rows = conn.execute(_FINANCIAL_PERIODS_SQL, (symbol,)).fetchall()
        if not rows:
            return None
        reports: dict[int, FinancialReportBundle] = {}
        periods = []
        for row in rows:
            if row["id"] not in reports:
                report = FinancialReportBundle.model_validate_json(row["report"])
                if report.symbol != symbol:
                    raise ValueError("财报观察的证券身份不一致")
                reports[row["id"]] = report
            report = reports[row["id"]]
            period = next(item for item in report.periods
                          if (item.period_end, item.period_type) == (row["period_end"], row["period_type"]))
            periods.append(period.model_copy(update={"fetched_at": report.fetched_at, "source": report.source}))
        ordered = sorted(periods, key=lambda item: (item.period_end, item.period_type == "annual"), reverse=True)
        warnings = list(dict.fromkeys(warning for report in reports.values() for warning in report.warnings
                                     if warning != FINANCIAL_PARTIAL_WARNING))
        if any(period.alignment != "complete" for period in ordered):
            warnings.append(FINANCIAL_PARTIAL_WARNING)
        return next(iter(reports.values())).model_copy(update={"periods": ordered, "warnings": warnings})

    def valuation_history(self, symbol: str, limit: int = 100) -> list[FuyaoObservation]:
        """Select the latest observation per Shanghai day before limiting days."""
        if not self.path.is_file():
            return []
        self.initialize()
        with self._connections.connect() as conn:
            rows = conn.execute("""WITH daily AS (
                SELECT *, ROW_NUMBER() OVER (
                    PARTITION BY strftime('%Y-%m-%d',ashare_audit_epoch(fetched_at),'unixepoch','+8 hours')
                    ORDER BY ashare_audit_epoch(fetched_at) DESC,id DESC) AS daily_rank
                FROM observations WHERE capability='valuations' AND symbol=? AND ashare_audit_epoch(fetched_at) IS NOT NULL)
                SELECT id,capability,symbol,fetched_at,digest,payload FROM daily WHERE daily_rank=1
                ORDER BY ashare_audit_epoch(fetched_at) DESC,id DESC LIMIT ?""", (symbol, min(100, max(1, limit)))).fetchall()
        return [self._observation(row) for row in rows]

    def save_job(self, job: FuyaoJob) -> None:
        self.initialize()
        with self._lock, self._connections.connect() as conn:
            conn.execute("INSERT INTO jobs VALUES (?,?) ON CONFLICT(id) DO UPDATE SET payload=excluded.payload",
                         (job.id, job.model_dump_json()))
            conn.commit()

    def jobs(self, limit: int = 20) -> list[FuyaoJob]:
        if not self.path.is_file():
            return []
        self.initialize()
        with self._connections.connect() as conn:
            rows = conn.execute("SELECT payload FROM jobs ORDER BY rowid DESC LIMIT ?", (min(100, max(1, limit)),)).fetchall()
        return [FuyaoJob.model_validate_json(row[0]) for row in rows]

    def job(self, job_id: str) -> FuyaoJob | None:
        if not self.path.is_file():
            return None
        self.initialize()
        with self._connections.connect() as conn:
            row = conn.execute("SELECT payload FROM jobs WHERE id=?", (job_id,)).fetchone()
        return FuyaoJob.model_validate_json(row[0]) if row else None

    def retry_child(self, parent_id: str) -> FuyaoJob | None:
        if not self.path.is_file():
            return None
        self.initialize()
        with self._connections.connect() as conn:
            row = conn.execute("SELECT payload FROM jobs WHERE json_extract(payload,'$.parent_job_id')=?", (parent_id,)).fetchone()
        return FuyaoJob.model_validate_json(row[0]) if row else None

    def unfinished_jobs(self) -> list[FuyaoJob]:
        if not self.path.is_file():
            return []
        self.initialize()
        with self._connections.connect() as conn:
            rows = conn.execute("SELECT payload FROM jobs WHERE json_extract(payload,'$.status') IN ('running','cancelling')").fetchall()
        return [FuyaoJob.model_validate_json(row[0]) for row in rows]

    @staticmethod
    def _observation(row: sqlite3.Row) -> FuyaoObservation:
        return FuyaoObservation(id=row[0], capability=row[1], symbol=row[2], fetched_at=row[3], digest=row[4],
                                payload=json.loads(row[5]))


def _encode_observation(payload: dict[str, Any]) -> tuple[str, str]:
    encoded = json.dumps(payload, ensure_ascii=False, sort_keys=True, separators=(",", ":"), allow_nan=False)
    if len(encoded.encode()) > 16 * 1024 * 1024:
        raise ValueError("扶摇单份研究记录超出大小限制")
    return encoded, hashlib.sha256(encoded.encode()).hexdigest()
