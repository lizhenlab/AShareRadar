"""Persistent identity for one append-only notification stream."""

from __future__ import annotations

from dataclasses import dataclass
import re
import secrets
import sqlite3


ALERT_STREAM_TABLE = "alert_stream_state"
ALERT_STREAM_SCHEMA_SQL = """
CREATE TABLE alert_stream_state (
    singleton INTEGER PRIMARY KEY CHECK (singleton = 1),
    stream_id TEXT NOT NULL CHECK (
        typeof(stream_id) = 'text' AND length(stream_id) = 32
        AND stream_id NOT GLOB '*[^0-9a-f]*'
    ),
    baseline_event_id INTEGER NOT NULL CHECK (
        typeof(baseline_event_id) = 'integer' AND baseline_event_id >= 0
    )
)
""".strip()
_STREAM_ID = re.compile(r"[0-9a-f]{32}")


class AlertStreamStateError(RuntimeError):
    """The persisted stream identity cannot be safely interpreted."""


@dataclass(frozen=True)
class AlertStreamState:
    stream_id: str
    baseline_event_id: int


def alert_stream_table_exists(conn: sqlite3.Connection) -> bool:
    return conn.execute(
        "SELECT 1 FROM sqlite_schema WHERE name = ? COLLATE NOCASE",
        (ALERT_STREAM_TABLE,),
    ).fetchone() is not None


def read_alert_stream_state(conn: sqlite3.Connection) -> AlertStreamState:
    _require_stream_schema(conn)
    rows = conn.execute(
        "SELECT singleton, stream_id, baseline_event_id FROM alert_stream_state"
    ).fetchmany(2)
    if len(rows) != 1:
        raise AlertStreamStateError("通知流代次必须包含唯一状态行")
    singleton, stream_id, baseline = rows[0]
    if (
        type(singleton) is not int or singleton != 1
        or not isinstance(stream_id, str) or _STREAM_ID.fullmatch(stream_id) is None
        or type(baseline) is not int or baseline < 0
    ):
        raise AlertStreamStateError("通知流代次状态无效")
    if baseline > alert_event_high_water(conn):
        raise AlertStreamStateError("通知流基线超过事件高水位")
    return AlertStreamState(stream_id, baseline)


def ensure_alert_stream_state(conn: sqlite3.Connection) -> AlertStreamState:
    """Initialize a legacy database in the caller's transaction, preserving existing identity."""
    if not conn.in_transaction:
        conn.execute("BEGIN IMMEDIATE")
    if alert_stream_table_exists(conn):
        return read_alert_stream_state(conn)
    conn.execute(ALERT_STREAM_SCHEMA_SQL)
    state = AlertStreamState(_new_stream_id(), alert_event_high_water(conn))
    conn.execute(
        "INSERT INTO alert_stream_state (singleton, stream_id, baseline_event_id) VALUES (1, ?, ?)",
        (state.stream_id, state.baseline_event_id),
    )
    return read_alert_stream_state(conn)


def rotate_alert_stream_state(conn: sqlite3.Connection) -> AlertStreamState:
    """Start a new stream atomically with an authorized history replacement."""
    if not conn.in_transaction:
        raise AlertStreamStateError("通知流换代必须在显式写事务内执行")
    if not alert_stream_table_exists(conn):
        return ensure_alert_stream_state(conn)
    previous = read_alert_stream_state(conn)
    state = AlertStreamState(_new_stream_id(previous.stream_id), alert_event_high_water(conn))
    conn.execute(
        "UPDATE alert_stream_state SET stream_id = ?, baseline_event_id = ? WHERE singleton = 1",
        (state.stream_id, state.baseline_event_id),
    )
    return read_alert_stream_state(conn)


def alert_event_high_water(conn: sqlite3.Connection) -> int:
    tables = {str(row[0]) for row in conn.execute("SELECT name FROM sqlite_schema WHERE type = 'table'")}
    maximum = 0
    if "alert_event" in tables:
        value = conn.execute("SELECT MAX(id) FROM alert_event").fetchone()[0]
        if value is not None:
            maximum = _nonnegative_integer(value)
    if "sqlite_sequence" in tables:
        rows = conn.execute("SELECT seq FROM sqlite_sequence WHERE name = 'alert_event'").fetchmany(2)
        if len(rows) > 1:
            raise AlertStreamStateError("通知事件序列存在冲突")
        if rows:
            maximum = max(maximum, _nonnegative_integer(rows[0][0]))
    return maximum


def validate_existing_alert_stream_state(conn: sqlite3.Connection) -> None:
    """Reject malformed metadata without mutating backups or legacy databases."""
    if alert_stream_table_exists(conn):
        read_alert_stream_state(conn)


def _require_stream_schema(conn: sqlite3.Connection) -> None:
    rows = conn.execute(
        "SELECT type, name, sql FROM sqlite_schema "
        "WHERE name = ? COLLATE NOCASE OR tbl_name = ? COLLATE NOCASE",
        (ALERT_STREAM_TABLE, ALERT_STREAM_TABLE),
    ).fetchall()
    if len(rows) != 1:
        raise AlertStreamStateError("通知流代次表结构无效或存在额外对象")
    kind, name, sql = rows[0]
    if kind != "table" or name != ALERT_STREAM_TABLE or not isinstance(sql, str):
        raise AlertStreamStateError("通知流代次表结构无效")
    if " ".join(sql.split()).rstrip(";") != " ".join(ALERT_STREAM_SCHEMA_SQL.split()):
        raise AlertStreamStateError("通知流代次表结构不符合合同")


def _nonnegative_integer(value: object) -> int:
    if type(value) is not int or value < 0:
        raise AlertStreamStateError("通知事件高水位无效")
    return value


def _new_stream_id(previous: str | None = None) -> str:
    for _ in range(4):
        stream_id = secrets.token_hex(16)
        if _STREAM_ID.fullmatch(stream_id) is not None and stream_id != previous:
            return stream_id
    raise AlertStreamStateError("无法生成新的通知流代次")
