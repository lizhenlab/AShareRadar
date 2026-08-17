"""Persistence for immutable, versioned StrategySpec records."""

from __future__ import annotations

import builtins
import sqlite3
import threading
from pathlib import Path

from app.models.strategy_lab import (
    StrategySpec,
    StrategySpecInput,
    StrategyVersionSummary,
)
from app.repositories.base import SQLiteRepository
from app.utils.errors import NotFoundError


class StrategyRevisionConflictError(ValueError):
    pass


class StrategyLabRepository(SQLiteRepository):
    def __init__(self, path: Path, lock: threading.RLock | None = None) -> None:
        super().__init__(Path(path), lock or threading.RLock())

    def create(
        self,
        spec: StrategySpecInput,
        *,
        fingerprint: str,
        timestamp: str,
    ) -> StrategySpec:
        with self._lock, self._connect() as conn:
            conn.execute("BEGIN IMMEDIATE")
            cursor = conn.execute(
                """
                INSERT INTO strategy_spec (current_revision, archived, created_at, updated_at)
                VALUES (1, 0, ?, ?)
                """,
                (timestamp, timestamp),
            )
            strategy_id = cursor.lastrowid
            if strategy_id is None:
                raise RuntimeError("策略保存失败")
            _insert_version(
                conn,
                strategy_id=int(strategy_id),
                revision=1,
                spec=spec,
                fingerprint=fingerprint,
                timestamp=timestamp,
            )
            row = _strategy_row(conn, int(strategy_id), revision=1)
        return _strategy_from_row(row)

    def strategy(self, strategy_id: int, *, revision: int | None = None) -> StrategySpec:
        with self._lock, self._connect() as conn:
            row = _strategy_row(conn, strategy_id, revision=revision)
        return _strategy_from_row(row)

    def list(
        self,
        *,
        page: int,
        page_size: int,
        include_archived: bool,
    ) -> tuple[list[StrategySpec], int]:
        offset = (page - 1) * page_size
        where = "1 = 1" if include_archived else "s.archived = 0"
        with self._lock, self._connect() as conn:
            total = int(
                conn.execute(f"SELECT COUNT(*) FROM strategy_spec AS s WHERE {where}").fetchone()[0]
            )
            rows = conn.execute(
                f"""
                SELECT {_STRATEGY_COLUMNS}
                FROM strategy_spec AS s
                JOIN strategy_spec_version AS v
                  ON v.strategy_id = s.id AND v.revision = s.current_revision
                WHERE {where}
                ORDER BY s.archived ASC, s.updated_at DESC, s.id DESC
                LIMIT ? OFFSET ?
                """,
                (page_size, offset),
            ).fetchall()
        return [_strategy_from_row(row) for row in rows], total

    def update(
        self,
        strategy_id: int,
        spec: StrategySpecInput,
        *,
        expected_revision: int,
        fingerprint: str,
        timestamp: str,
    ) -> StrategySpec:
        with self._lock, self._connect() as conn:
            conn.execute("BEGIN IMMEDIATE")
            head = _head_row(conn, strategy_id)
            current = int(head["current_revision"])
            if current != expected_revision:
                raise StrategyRevisionConflictError(
                    f"策略修订冲突：期望 {expected_revision}，当前 {current}"
                )
            revision = current + 1
            _insert_version(
                conn,
                strategy_id=strategy_id,
                revision=revision,
                spec=spec,
                fingerprint=fingerprint,
                timestamp=timestamp,
            )
            cursor = conn.execute(
                """
                UPDATE strategy_spec
                SET current_revision = ?, updated_at = ?
                WHERE id = ? AND current_revision = ?
                """,
                (revision, timestamp, strategy_id, expected_revision),
            )
            if cursor.rowcount != 1:
                raise StrategyRevisionConflictError("策略在保存期间被其他操作更新")
            row = _strategy_row(conn, strategy_id, revision=revision)
        return _strategy_from_row(row)

    def set_archived(
        self,
        strategy_id: int,
        *,
        expected_revision: int,
        archived: bool,
        timestamp: str,
    ) -> StrategySpec:
        with self._lock, self._connect() as conn:
            cursor = conn.execute(
                """
                UPDATE strategy_spec
                SET archived = ?, updated_at = ?
                WHERE id = ? AND current_revision = ?
                """,
                (int(archived), timestamp, strategy_id, expected_revision),
            )
            if cursor.rowcount != 1:
                _raise_missing_or_revision(conn, strategy_id, expected_revision)
            row = _strategy_row(conn, strategy_id, revision=expected_revision)
        return _strategy_from_row(row)

    def versions(self, strategy_id: int) -> builtins.list[StrategyVersionSummary]:
        with self._lock, self._connect() as conn:
            _head_row(conn, strategy_id)
            rows = conn.execute(
                """
                SELECT strategy_id, revision, fingerprint, name, created_at
                FROM strategy_spec_version
                WHERE strategy_id = ?
                ORDER BY revision DESC
                """,
                (strategy_id,),
            ).fetchall()
        return [StrategyVersionSummary.model_validate(dict(row)) for row in rows]


_STRATEGY_COLUMNS = """
s.id AS strategy_id,
v.revision AS strategy_version,
v.revision AS revision,
s.current_revision,
s.archived,
v.fingerprint,
v.spec_json,
s.created_at,
s.updated_at,
v.created_at AS version_created_at
"""


def _insert_version(
    conn: sqlite3.Connection,
    *,
    strategy_id: int,
    revision: int,
    spec: StrategySpecInput,
    fingerprint: str,
    timestamp: str,
) -> None:
    conn.execute(
        """
        INSERT INTO strategy_spec_version (
            strategy_id, revision, schema_version, name, description,
            spec_json, fingerprint, created_at
        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?)
        """,
        (
            strategy_id,
            revision,
            spec.schema_version,
            spec.name,
            spec.description,
            spec.model_dump_json(),
            fingerprint,
            timestamp,
        ),
    )


def _strategy_row(
    conn: sqlite3.Connection,
    strategy_id: int,
    *,
    revision: int | None,
) -> sqlite3.Row:
    head = _head_row(conn, strategy_id)
    selected_revision = int(head["current_revision"]) if revision is None else revision
    row = conn.execute(
        f"""
        SELECT {_STRATEGY_COLUMNS}
        FROM strategy_spec AS s
        JOIN strategy_spec_version AS v
          ON v.strategy_id = s.id
        WHERE s.id = ? AND v.revision = ?
        """,
        (strategy_id, selected_revision),
    ).fetchone()
    if row is None:
        raise NotFoundError(f"策略版本不存在：{strategy_id}@{selected_revision}")
    return row


def _head_row(conn: sqlite3.Connection, strategy_id: int) -> sqlite3.Row:
    row = conn.execute(
        "SELECT id, current_revision, archived, created_at, updated_at FROM strategy_spec WHERE id = ?",
        (strategy_id,),
    ).fetchone()
    if row is None:
        raise NotFoundError(f"策略不存在：{strategy_id}")
    return row


def _strategy_from_row(row: sqlite3.Row) -> StrategySpec:
    values = dict(row)
    values["archived"] = bool(values["archived"])
    values["spec"] = StrategySpecInput.model_validate_json(str(values.pop("spec_json")))
    return StrategySpec.model_validate(values)


def _raise_missing_or_revision(
    conn: sqlite3.Connection,
    strategy_id: int,
    expected_revision: int,
) -> None:
    row = conn.execute(
        "SELECT current_revision FROM strategy_spec WHERE id = ?",
        (strategy_id,),
    ).fetchone()
    if row is None:
        raise NotFoundError(f"策略不存在：{strategy_id}")
    raise StrategyRevisionConflictError(
        f"策略修订冲突：期望 {expected_revision}，当前 {int(row['current_revision'])}"
    )


__all__ = ["StrategyLabRepository", "StrategyRevisionConflictError"]
