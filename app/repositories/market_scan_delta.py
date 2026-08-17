"""One-snapshot reads for immutable, same-cohort market-scan comparisons."""

from __future__ import annotations

from dataclasses import dataclass
import json
from pathlib import Path
import sqlite3
import threading

from app.db.connection import SQLITE_AUDIT_EPOCH_FUNCTION
from app.db.market_scan_integrity import require_publication_market_scan_snapshot
from app.models.market_scan import MarketScanRun
from app.repositories.base import SQLiteRepository
from app.repositories.market_scan_mapping import run_from_row
from app.repositories.market_scan_results import required_run_row


@dataclass(frozen=True)
class MarketScanDeltaRow:
    symbol: str
    name: str
    market: str
    industry: str | None
    status: str
    rank: int | None
    raw_score: float | None
    quote_source: str | None
    kline_source: str | None
    metadata_source: str | None
    quote_fallback_used: bool
    kline_fallback_used: bool
    metadata_degraded: bool
    degradation_reasons: tuple[str, ...]


@dataclass(frozen=True)
class MarketScanDeltaSnapshot:
    current: MarketScanRun
    previous: MarketScanRun | None
    current_rows: tuple[MarketScanDeltaRow, ...]
    previous_rows: tuple[MarketScanDeltaRow, ...]


class MarketScanDeltaRepository(SQLiteRepository):
    """Read only the persisted run/result rows required by a delta."""

    def __init__(self, path: Path, lock: threading.RLock) -> None:
        super().__init__(path, lock)

    def comparison_snapshot(self, run_id: int) -> MarketScanDeltaSnapshot:
        """Return current plus its immediately previous published cohort member.

        The complete read occurs in one SQLite snapshot.  A run outside the
        published status set intentionally gets no predecessor; the service
        exposes that state as an explicit unavailable response.
        """

        with self._read_snapshot() as conn:
            current_row = required_run_row(conn, run_id)
            current = run_from_row(current_row)
            if current.status in {"success", "degraded"}:
                require_publication_market_scan_snapshot(conn, current.id)
            previous_row = _previous_cohort_row(conn, current)
            if previous_row is not None:
                require_publication_market_scan_snapshot(conn, int(previous_row["id"]))
            current_rows = _delta_rows(conn, run_id)
            previous_rows = (
                _delta_rows(conn, int(previous_row["id"]))
                if previous_row is not None
                else ()
            )
        return MarketScanDeltaSnapshot(
            current=current,
            previous=run_from_row(previous_row) if previous_row is not None else None,
            current_rows=current_rows,
            previous_rows=previous_rows,
        )


def _previous_cohort_row(
    conn: sqlite3.Connection,
    current: MarketScanRun,
) -> sqlite3.Row | None:
    if current.status not in {"success", "degraded"}:
        return None
    return conn.execute(
        f"""
        SELECT *
        FROM market_scan_run
        WHERE status IN ('success', 'degraded')
          AND mode = ? AND scope = ? AND rule_version = ?
          AND (
               data_date < ?
               OR (
                   data_date = ?
                   AND COALESCE({SQLITE_AUDIT_EPOCH_FUNCTION}(finished_at), -1)
                       < COALESCE({SQLITE_AUDIT_EPOCH_FUNCTION}(?), -1)
               )
               OR (
                   data_date = ?
                   AND COALESCE({SQLITE_AUDIT_EPOCH_FUNCTION}(finished_at), -1)
                       = COALESCE({SQLITE_AUDIT_EPOCH_FUNCTION}(?), -1)
                   AND id < ?
               )
          )
        ORDER BY data_date DESC,
                 COALESCE({SQLITE_AUDIT_EPOCH_FUNCTION}(finished_at), -1) DESC,
                 id DESC
        LIMIT 1
        """,
        (
            current.mode,
            current.scope,
            current.rule_version,
            current.data_date,
            current.data_date,
            current.finished_at,
            current.data_date,
            current.finished_at,
            current.id,
        ),
    ).fetchone()


def _delta_rows(conn: sqlite3.Connection, run_id: int) -> tuple[MarketScanDeltaRow, ...]:
    rows = conn.execute(
        """
        SELECT symbol, name, market, industry, status, rank, raw_score,
               quote_source, kline_source, metadata_source,
               quote_fallback_used, kline_fallback_used, metadata_degraded,
               degradation_reasons_json
        FROM market_scan_result
        WHERE run_id = ?
        ORDER BY symbol ASC
        """,
        (run_id,),
    ).fetchall()
    return tuple(
        MarketScanDeltaRow(
            symbol=str(row["symbol"]),
            name=str(row["name"]),
            market=str(row["market"]),
            industry=str(row["industry"]) if row["industry"] is not None else None,
            status=str(row["status"]),
            rank=int(row["rank"]) if row["rank"] is not None else None,
            raw_score=float(row["raw_score"]) if row["raw_score"] is not None else None,
            quote_source=_optional_text(row["quote_source"]),
            kline_source=_optional_text(row["kline_source"]),
            metadata_source=_optional_text(row["metadata_source"]),
            quote_fallback_used=bool(row["quote_fallback_used"]),
            kline_fallback_used=bool(row["kline_fallback_used"]),
            metadata_degraded=bool(row["metadata_degraded"]),
            degradation_reasons=_string_tuple(row["degradation_reasons_json"]),
        )
        for row in rows
    )


def _optional_text(value: object) -> str | None:
    return str(value) if value is not None and str(value).strip() else None


def _string_tuple(value: object) -> tuple[str, ...]:
    try:
        parsed = json.loads(str(value))
    except (TypeError, ValueError):
        return ()
    if not isinstance(parsed, list):
        return ()
    return tuple(sorted({str(item) for item in parsed if str(item).strip()}))


__all__ = [
    "MarketScanDeltaRepository",
    "MarketScanDeltaRow",
    "MarketScanDeltaSnapshot",
]
