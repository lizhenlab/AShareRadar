"""Manual request receipts remain idempotent after task-run history is pruned."""

from __future__ import annotations

import json
import sqlite3
from uuid import UUID

from app.db.system_mappers import row_to_task_run
from app.models.system import TaskRunRequestReceipt
from app.repositories.base import SQLiteRepository
from app.utils.audit_time import audit_now_text
from app.utils.errors import NotFoundError


def validate_task_request_id(value: str) -> str:
    try:
        canonical = str(UUID(value))
    except (ValueError, AttributeError) as exc:
        raise ValueError("任务请求编号无效") from exc
    if value != canonical:
        raise ValueError("任务请求编号无效")
    return canonical


class TaskRequestRepository(SQLiteRepository):
    def reserve_task_request(self, request_id: str, task_name: str | None) -> tuple[TaskRunRequestReceipt, bool]:
        validate_task_request_id(request_id)
        with self._lock, self._connect() as conn:
            conn.execute("BEGIN IMMEDIATE")
            existing = conn.execute("SELECT requested_task FROM task_run_request WHERE request_id=?", (request_id,)).fetchone()
            if existing is not None and existing["requested_task"] != task_name:
                raise ValueError("该请求编号已绑定其他任务")
            created = existing is None
            if created:
                conn.execute(
                    "INSERT INTO task_run_request(request_id, requested_task, status, created_at) VALUES (?, ?, 'accepted', ?)",
                    (request_id, task_name, audit_now_text()),
                )
            return _read_receipt(conn, request_id), created

    def task_request(self, request_id: str) -> TaskRunRequestReceipt:
        validate_task_request_id(request_id)
        with self._connect() as conn:
            conn.execute("BEGIN")
            return _read_receipt(conn, request_id)

    def find_task_request(self, request_id: str, task_name: str | None) -> TaskRunRequestReceipt | None:
        validate_task_request_id(request_id)
        with self._connect() as conn:
            conn.execute("BEGIN")
            row = conn.execute("SELECT requested_task FROM task_run_request WHERE request_id=?", (request_id,)).fetchone()
            if row is None:
                return None
            if row["requested_task"] != task_name:
                raise ValueError("该请求编号已绑定其他任务")
            return _read_receipt(conn, request_id)

    def attach_task_request_run(self, request_id: str, run_id: int) -> None:
        with self._lock, self._connect() as conn:
            conn.execute("BEGIN IMMEDIATE")
            run = conn.execute("SELECT task_name FROM task_run WHERE id=?", (run_id,)).fetchone()
            if run is None:
                raise ValueError("任务执行记录不存在")
            conn.execute(
                "INSERT INTO task_run_request_item(request_id, task_name, task_run_id) VALUES (?, ?, ?)",
                (request_id, run["task_name"], run_id),
            )
            conn.execute("UPDATE task_run_request SET status='running' WHERE request_id=? AND status='accepted'", (request_id,))

    def finish_task_request(self, request_id: str, status: str, messages: list[str]) -> None:
        encoded = json.dumps([str(message)[:800] for message in messages[:32]], ensure_ascii=False)
        with self._lock, self._connect() as conn:
            conn.execute(
                """UPDATE task_run_request SET status=?, finished_at=?, messages_json=?
                   WHERE request_id=? AND status IN ('accepted', 'running')""",
                (status, audit_now_text(), encoded, request_id),
            )

    def reconcile_task_requests(self) -> int:
        """Caller must own the scheduler lease and admit no new manual requests yet."""
        with self._lock, self._connect() as conn:
            conn.execute("BEGIN IMMEDIATE")
            stamp = audit_now_text()
            conn.execute(
                """UPDATE task_run SET status='cancelled', finished_at=?, message=?
                   WHERE status='running' AND id IN (
                     SELECT task_run_id FROM task_run_request_item JOIN task_run_request USING(request_id)
                     WHERE task_run_request.status IN ('accepted', 'running'))""",
                (stamp, "执行进程已退出，结果未完整确认"),
            )
            result = conn.execute(
                """UPDATE task_run_request SET status='interrupted', finished_at=?, messages_json=?
                   WHERE status IN ('accepted', 'running')""",
                (stamp, json.dumps(["执行进程已退出，结果未完整确认"], ensure_ascii=False)),
            )
            return max(0, result.rowcount)


def _read_receipt(conn: sqlite3.Connection, request_id: str) -> TaskRunRequestReceipt:
    row = conn.execute("SELECT * FROM task_run_request WHERE request_id=?", (request_id,)).fetchone()
    if row is None:
        raise NotFoundError("尚未找到该任务请求回执；不能据此认定任务已取消")
    runs = conn.execute(
        """SELECT task_run.* FROM task_run_request_item JOIN task_run ON task_run.id=task_run_id
           WHERE request_id=? ORDER BY task_run.id""", (request_id,),
    ).fetchall()
    return TaskRunRequestReceipt(
        request_id=row["request_id"], requested_task=row["requested_task"], status=row["status"],
        created_at=row["created_at"], finished_at=row["finished_at"], messages=json.loads(row["messages_json"]),
        runs=[row_to_task_run(run) for run in runs],
    )


__all__ = ["TaskRequestRepository", "validate_task_request_id"]
