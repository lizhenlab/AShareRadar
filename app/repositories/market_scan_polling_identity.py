"""Cheap market-scan change detection with no publication authority."""

from __future__ import annotations

from collections.abc import Callable
from contextlib import AbstractContextManager
import hashlib
import json
from pathlib import Path
import sqlite3

from app.db.connection import SQLITE_AUDIT_EPOCH_FUNCTION
from app.models.market_scan import MARKET_SCAN_FULL_MARKET_SCOPE, MarketScanMode
from app.models.market_scan_polling import (
    MARKET_SCAN_POLLING_IDENTITY_AUTHORIZATION,
    MARKET_SCAN_POLLING_IDENTITY_SCHEMA_VERSION,
    MarketScanPollingIdentity,
    MarketScanPollingRunToken,
)


_TOKEN_DOMAIN = b"market-scan-polling-run-token-v1\x00"
_FINGERPRINT_DOMAIN = b"market-scan-polling-identity-v1\x00"
_ACTIVE_STATUSES = frozenset({"queued", "running", "cancelling"})
_HEADER_COLUMNS = (
    "id",
    "status",
    "mode",
    "scope",
    "trigger",
    "rule_version",
    "data_date",
    "quote_date",
    "updated_at",
    "finished_at",
    "snapshot_digest",
    "snapshot_seal_origin",
    "snapshot_sealed_at",
)
_HEADER_SELECT = ", ".join(_HEADER_COLUMNS)


class MarketScanPollingIdentityUnstable(RuntimeError):
    """The database identity changed while a polling projection was read."""


def read_market_scan_polling_identity(
    *,
    database_path: Path,
    read_snapshot: Callable[[], AbstractContextManager[sqlite3.Connection]],
    mode: MarketScanMode,
) -> MarketScanPollingIdentity:
    """Read two header tokens; callers must never use them as action evidence."""
    path = Path(database_path).resolve()
    file_identity = _file_identity(path)
    with read_snapshot() as conn:
        _require_read_snapshot(conn)
        schema_version = _schema_version(conn)
        database_identity = _database_identity(path, file_identity, schema_version)
        latest = _latest_row(conn)
        published = _latest_published_row(conn, mode)
        if schema_version != _schema_version(conn) or file_identity != _file_identity(path):
            raise MarketScanPollingIdentityUnstable("轮询期间数据库身份发生变化")
    latest_token = _run_token(latest, database_identity)
    published_token = _run_token(published, database_identity)
    fingerprint = _fingerprint(mode, database_identity, latest_token, published_token)
    return MarketScanPollingIdentity(
        authorization=MARKET_SCAN_POLLING_IDENTITY_AUTHORIZATION,
        schema_version=MARKET_SCAN_POLLING_IDENTITY_SCHEMA_VERSION,
        request_mode=mode,
        latest=latest_token,
        latest_published=published_token,
        fingerprint=fingerprint,
    )


def _latest_row(conn: sqlite3.Connection) -> sqlite3.Row | None:
    return conn.execute(
        f"SELECT {_HEADER_SELECT} FROM market_scan_run ORDER BY id DESC LIMIT 1"
    ).fetchone()


def _latest_published_row(
    conn: sqlite3.Connection,
    mode: MarketScanMode,
) -> sqlite3.Row | None:
    return conn.execute(
        f"""
        SELECT {_HEADER_SELECT}
        FROM market_scan_run
        WHERE status IN ('success', 'degraded') AND scope = ? AND mode = ?
        ORDER BY data_date DESC,
                 {SQLITE_AUDIT_EPOCH_FUNCTION}(finished_at) DESC,
                 id DESC
        LIMIT 1
        """,
        (MARKET_SCAN_FULL_MARKET_SCOPE, mode),
    ).fetchone()


def _run_token(
    row: sqlite3.Row | None,
    database_identity: str,
) -> MarketScanPollingRunToken:
    header = {column: row[column] for column in _HEADER_COLUMNS} if row is not None else None
    if header is not None and header["status"] in _ACTIVE_STATUSES:
        header["updated_at"] = None
    token = _digest(_TOKEN_DOMAIN, {"database_identity": database_identity, "header": header})
    return MarketScanPollingRunToken(
        run_id=int(row["id"]) if row is not None else None,
        token=token,
    )


def _fingerprint(
    mode: MarketScanMode,
    database_identity: str,
    latest: MarketScanPollingRunToken,
    published: MarketScanPollingRunToken,
) -> str:
    return _digest(
        _FINGERPRINT_DOMAIN,
        {
            "database_identity": database_identity,
            "request_mode": mode,
            "latest": latest.model_dump(mode="json"),
            "latest_published": published.model_dump(mode="json"),
        },
    )


def _digest(domain: bytes, value: object) -> str:
    encoded = json.dumps(
        value,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
        allow_nan=False,
    ).encode("utf-8")
    return hashlib.sha256(domain + encoded).hexdigest()


def _file_identity(path: Path) -> tuple[int, int]:
    stat = path.stat()
    return stat.st_dev, stat.st_ino


def _database_identity(
    path: Path,
    file_identity: tuple[int, int],
    schema_version: int,
) -> str:
    return _digest(
        b"market-scan-polling-database-identity-v1\x00",
        {
            "path": str(path),
            "device": file_identity[0],
            "inode": file_identity[1],
            "schema_version": schema_version,
        },
    )


def _schema_version(conn: sqlite3.Connection) -> int:
    return int(conn.execute("PRAGMA schema_version").fetchone()[0])


def _require_read_snapshot(conn: sqlite3.Connection) -> None:
    query_only = int(conn.execute("PRAGMA query_only").fetchone()[0])
    if not conn.in_transaction or query_only != 1:
        raise RuntimeError("轮询身份必须在 query-only 读事务中生成")


__all__ = ["MarketScanPollingIdentityUnstable", "read_market_scan_polling_identity"]
