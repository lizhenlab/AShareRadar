"""SQLite persistence boundary for idempotent saved-screen change events."""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
import sqlite3
import threading

from pydantic import TypeAdapter

from app.artifacts.io import canonical_json_text, sha256_hex
from app.db.connection import SQLITE_AUDIT_EPOCH_FUNCTION
from app.db.market_scan_action_source import require_market_scan_action_source
from app.db.market_scan_integrity import require_publication_market_scan_snapshot
from app.market_scan_screening import screen_spec_digest, screen_spec_from_discovery
from app.models.discovery import DiscoveryCriteria, DiscoverySort
from app.models.market_scan import MARKET_SCAN_FULL_MARKET_SCOPE, MarketScanRun
from app.models.market_scan_screen_alert import MARKET_SCAN_SCREEN_ALERT_SCHEMA_VERSION
from app.models.market_scan_screening import ScreenSpecV2
from app.repositories.base import SQLiteRepository
from app.repositories.market_scan_mapping import run_from_row
from app.repositories.market_scan_results import required_run_row
from app.repositories.market_scan_screening_sql import screen_spec_filter_sql
from app.utils.errors import NotFoundError


_SORT_ADAPTER = TypeAdapter(list[DiscoverySort])
_PUBLISHED = frozenset({"success", "degraded"})


class MarketScanScreenAlertPresetRevisionError(ValueError):
    """The saved preset changed between compilation and persistence."""


@dataclass(frozen=True)
class MarketScanScreenAlertPresetSnapshot:
    preset_id: int
    revision: int
    name: str
    criteria: DiscoveryCriteria
    sort: tuple[DiscoverySort, ...]


@dataclass(frozen=True)
class MarketScanScreenAlertComparisonSnapshot:
    current: MarketScanRun
    previous: MarketScanRun | None
    current_matches: tuple[str, ...]
    previous_matches: tuple[str, ...]
    current_status_by_symbol: dict[str, str]


class MarketScanScreenAlertRepository(SQLiteRepository):
    def __init__(self, path: Path, lock: threading.RLock | None = None) -> None:
        super().__init__(Path(path), lock or threading.RLock())

    def preset_snapshot(self, preset_id: int) -> MarketScanScreenAlertPresetSnapshot:
        with self._read_snapshot() as conn:
            row = _preset_row(conn, preset_id)
        return _preset_snapshot(row)

    def comparison_snapshot(
        self,
        *,
        preset_id: int,
        preset_revision: int,
        current_run_id: int,
        spec: ScreenSpecV2,
    ) -> MarketScanScreenAlertComparisonSnapshot:
        with self._read_snapshot() as conn:
            preset = _preset_snapshot(_preset_row(conn, preset_id))
            _require_revision(preset, preset_revision)
            current = run_from_row(required_run_row(conn, current_run_id))
            if current.status in _PUBLISHED:
                require_publication_market_scan_snapshot(conn, current.id)
            previous_row = _previous_cohort_row(conn, current)
            previous = run_from_row(previous_row) if previous_row is not None else None
            if previous is not None:
                require_publication_market_scan_snapshot(conn, previous.id)
            current_matches, previous_matches = _matching_symbols(
                conn,
                spec,
                current_run_id=current.id,
                previous_run_id=previous.id if previous is not None else None,
            )
            current_statuses = _result_statuses(conn, current.id)
        return MarketScanScreenAlertComparisonSnapshot(
            current=current,
            previous=previous,
            current_matches=current_matches,
            previous_matches=previous_matches,
            current_status_by_symbol=current_statuses,
        )

    def insert_event(
        self,
        *,
        preset_id: int,
        preset_revision: int,
        current_run_id: int,
        previous_run_id: int,
        event_digest: str,
        entered_symbols: tuple[str, ...],
        exited_symbols: tuple[str, ...],
        suppressed_unrankable_symbols: tuple[str, ...],
        created_at: str,
    ) -> bool:
        with self._lock, self._connect() as conn:
            conn.execute("BEGIN IMMEDIATE")
            preset = _preset_snapshot(_preset_row(conn, preset_id))
            _require_revision(preset, preset_revision)
            _validate_event_evidence(
                conn,
                preset=preset,
                current_run_id=current_run_id,
                previous_run_id=previous_run_id,
                event_digest=event_digest,
                entered_symbols=entered_symbols,
                exited_symbols=exited_symbols,
                suppressed_unrankable_symbols=suppressed_unrankable_symbols,
            )
            cursor = conn.execute(
                """
                INSERT OR IGNORE INTO discovery_screen_alert_event (
                    preset_id, current_run_id, previous_run_id, preset_revision,
                    event_digest, entered_symbols_json, exited_symbols_json,
                    suppressed_unrankable_symbols_json, created_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    preset_id,
                    current_run_id,
                    previous_run_id,
                    preset_revision,
                    event_digest,
                    canonical_json_text(list(entered_symbols)),
                    canonical_json_text(list(exited_symbols)),
                    canonical_json_text(list(suppressed_unrankable_symbols)),
                    created_at,
                ),
            )
            if cursor.rowcount == 1:
                return True
            if not _event_exists(
                conn,
                preset_id=preset_id,
                current_run_id=current_run_id,
                previous_run_id=previous_run_id,
                preset_revision=preset_revision,
                event_digest=event_digest,
            ):
                raise RuntimeError("筛选变化事件未写入且不存在幂等记录")
        return False


def _validate_event_evidence(
    conn: sqlite3.Connection,
    *,
    preset: MarketScanScreenAlertPresetSnapshot,
    current_run_id: int,
    previous_run_id: int,
    event_digest: str,
    entered_symbols: tuple[str, ...],
    exited_symbols: tuple[str, ...],
    suppressed_unrankable_symbols: tuple[str, ...],
) -> None:
    _require_event_source_snapshots(conn, current_run_id, previous_run_id)
    spec = screen_spec_from_discovery(preset.criteria, list(preset.sort))
    replayed = _replayed_event_membership(
        conn,
        spec=spec,
        current_run_id=current_run_id,
        previous_run_id=previous_run_id,
    )
    submitted = (entered_symbols, exited_symbols, suppressed_unrankable_symbols)
    if submitted != replayed:
        raise ValueError("筛选变化事件成员集合无法由冻结批次重放")
    expected_digest = _event_evidence_digest(
        preset=preset,
        spec=spec,
        current_run_id=current_run_id,
        previous_run_id=previous_run_id,
        membership=replayed,
    )
    if event_digest != expected_digest:
        raise ValueError("筛选变化事件摘要无法由冻结证据重放")


def _require_event_source_snapshots(
    conn: sqlite3.Connection,
    current_run_id: int,
    previous_run_id: int,
) -> None:
    current = run_from_row(required_run_row(conn, current_run_id))
    if current.status not in _PUBLISHED:
        raise ValueError("筛选变化事件 current run 尚未发布")
    if current.scope != MARKET_SCAN_FULL_MARKET_SCOPE:
        raise ValueError("筛选变化事件只接受完整全市场批次")
    previous_row = _previous_cohort_row(conn, current)
    if previous_row is None or int(previous_row["id"]) != previous_run_id:
        raise ValueError("筛选变化事件 previous run 不是紧邻同 cohort 已发布批次")
    require_market_scan_action_source(conn, current.id)
    require_market_scan_action_source(conn, previous_run_id)


def _replayed_event_membership(
    conn: sqlite3.Connection,
    *,
    spec: ScreenSpecV2,
    current_run_id: int,
    previous_run_id: int,
) -> tuple[tuple[str, ...], tuple[str, ...], tuple[str, ...]]:
    where_sql, parameters = screen_spec_filter_sql(spec, alias="r")
    current_matches = set(
        _matching_symbols_for_run(conn, current_run_id, where_sql, parameters)
    )
    previous_matches = set(
        _matching_symbols_for_run(conn, previous_run_id, where_sql, parameters)
    )
    statuses = _result_statuses(conn, current_run_id)
    expected_entered = tuple(sorted(current_matches - previous_matches))
    exit_candidates = previous_matches - current_matches
    expected_suppressed = tuple(
        sorted(
            symbol
            for symbol in exit_candidates
            if statuses.get(symbol) in {"pending", "missing", "skipped"}
        )
    )
    expected_exited = tuple(sorted(exit_candidates - set(expected_suppressed)))
    return expected_entered, expected_exited, expected_suppressed


def _event_evidence_digest(
    *,
    preset: MarketScanScreenAlertPresetSnapshot,
    spec: ScreenSpecV2,
    current_run_id: int,
    previous_run_id: int,
    membership: tuple[tuple[str, ...], tuple[str, ...], tuple[str, ...]],
) -> str:
    entered, exited, suppressed = membership
    return sha256_hex(
        canonical_json_text(
            {
                "schema_version": MARKET_SCAN_SCREEN_ALERT_SCHEMA_VERSION,
                "preset_id": preset.preset_id,
                "preset_revision": preset.revision,
                "spec_digest": screen_spec_digest(spec),
                "current_run_id": current_run_id,
                "previous_run_id": previous_run_id,
                "entered_symbols": list(entered),
                "exited_symbols": list(exited),
                "suppressed_unrankable_symbols": list(suppressed),
            }
        )
    )


def _preset_row(conn: sqlite3.Connection, preset_id: int) -> sqlite3.Row:
    row = conn.execute(
        """
        SELECT id, revision, name, criteria_json, sort_json
        FROM discovery_preset WHERE id = ?
        """,
        (preset_id,),
    ).fetchone()
    if row is None:
        raise NotFoundError(f"筛选方案不存在：{preset_id}")
    return row


def _preset_snapshot(row: sqlite3.Row) -> MarketScanScreenAlertPresetSnapshot:
    return MarketScanScreenAlertPresetSnapshot(
        preset_id=int(row["id"]),
        revision=int(row["revision"]),
        name=str(row["name"]),
        criteria=DiscoveryCriteria.model_validate_json(str(row["criteria_json"])),
        sort=tuple(_SORT_ADAPTER.validate_json(str(row["sort_json"]))),
    )


def _require_revision(
    preset: MarketScanScreenAlertPresetSnapshot,
    expected: int,
) -> None:
    if preset.revision != expected:
        raise MarketScanScreenAlertPresetRevisionError(
            f"筛选方案修订冲突：期望 {expected}，当前 {preset.revision}"
        )


def _previous_cohort_row(
    conn: sqlite3.Connection,
    current: MarketScanRun,
) -> sqlite3.Row | None:
    if current.status not in _PUBLISHED:
        return None
    return conn.execute(
        f"""
        SELECT * FROM market_scan_run
        WHERE status IN ('success', 'degraded')
          AND mode = ? AND scope = ? AND rule_version = ?
          AND (
              data_date < ?
              OR (data_date = ? AND COALESCE({SQLITE_AUDIT_EPOCH_FUNCTION}(finished_at), -1)
                  < COALESCE({SQLITE_AUDIT_EPOCH_FUNCTION}(?), -1))
              OR (data_date = ? AND COALESCE({SQLITE_AUDIT_EPOCH_FUNCTION}(finished_at), -1)
                  = COALESCE({SQLITE_AUDIT_EPOCH_FUNCTION}(?), -1) AND id < ?)
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


def _matching_symbols(
    conn: sqlite3.Connection,
    spec: ScreenSpecV2,
    *,
    current_run_id: int,
    previous_run_id: int | None,
) -> tuple[tuple[str, ...], tuple[str, ...]]:
    if previous_run_id is None:
        return (), ()
    where_sql, parameters = screen_spec_filter_sql(spec, alias="r")
    current = _matching_symbols_for_run(conn, current_run_id, where_sql, parameters)
    previous = _matching_symbols_for_run(conn, previous_run_id, where_sql, parameters)
    return current, previous


def _matching_symbols_for_run(
    conn: sqlite3.Connection,
    run_id: int,
    where_sql: str,
    parameters: list[object],
) -> tuple[str, ...]:
    rows = conn.execute(
        f"""
        SELECT r.symbol FROM market_scan_result AS r
        WHERE r.run_id = ? AND {where_sql}
        ORDER BY r.symbol ASC
        """,
        (run_id, *parameters),
    ).fetchall()
    return tuple(str(row["symbol"]) for row in rows)


def _result_statuses(conn: sqlite3.Connection, run_id: int) -> dict[str, str]:
    rows = conn.execute(
        "SELECT symbol, status FROM market_scan_result WHERE run_id = ?",
        (run_id,),
    ).fetchall()
    return {str(row["symbol"]): str(row["status"]) for row in rows}


def _event_exists(
    conn: sqlite3.Connection,
    *,
    preset_id: int,
    current_run_id: int,
    previous_run_id: int,
    preset_revision: int,
    event_digest: str,
) -> bool:
    row = conn.execute(
        """
        SELECT 1 FROM discovery_screen_alert_event
        WHERE preset_id = ? AND current_run_id = ? AND previous_run_id = ?
          AND preset_revision = ? AND event_digest = ?
        """,
        (preset_id, current_run_id, previous_run_id, preset_revision, event_digest),
    ).fetchone()
    return row is not None


__all__ = [
    "MarketScanScreenAlertComparisonSnapshot",
    "MarketScanScreenAlertPresetRevisionError",
    "MarketScanScreenAlertPresetSnapshot",
    "MarketScanScreenAlertRepository",
]
