"""Request-local, linearized reads of one verified market-scan snapshot."""

from __future__ import annotations

from collections.abc import Callable, Iterator, Mapping
from contextlib import contextmanager
from pathlib import Path
import sqlite3
import threading
from types import MappingProxyType
from typing import Protocol, cast

from app.db.market_scan_action_source import inspect_market_scan_action_source
from app.db.connection import SQLiteConnectionFactory
from app.db.market_scan_integrity import (
    MarketScanSnapshotSealError,
    verify_market_scan_snapshot,
)
from app.market_scan_repository_contracts import verify_persisted_market_scan_result
from app.market_scan_screening import screen_spec_from_market_scan_filters
from app.models.market_scan import (
    MarketScanFilterValues,
    MarketScanProductionScoreContract,
    MarketScanResultItem,
    MarketScanResultPage,
    MarketScanResultStatus,
    MarketScanRun,
    MarketScanSortOrderValues,
    MarketScanSortValues,
)
from app.models.market_scan_screening import ScreenSpecV2
from app.repositories.market_scan_filtering import market_scan_result_screen_sql
from app.repositories.market_scan_mapping import (
    page_count,
    result_from_row,
    run_from_row,
)
from app.repositories.market_scan_probability_capture import (
    read_probability_source_capture_state,
)
from app.repositories.market_scan_results import required_run_row
from app.repositories.market_scan_score_diagnostics import (
    read_production_score_contract,
)


MARKET_SCAN_VERIFIED_RESULT_READ_ARGUMENTS = (
    "status", "market", "industry", "is_st", "is_new", "min_score", "max_score",
    "min_trend_score", "max_trend_score", "min_change_pct", "max_change_pct",
    "min_turnover_rate", "max_turnover_rate", "min_amount", "max_amount",
    "min_data_quality_score", "max_data_quality_score", "min_confidence", "max_risk",
    "min_tradability", "keyword", "sort", "order", "symbols", "page", "page_size",
)
_SESSION_CLOSE_TOKEN = object()


class VerifiedMarketScanRead(Protocol):
    """Read-only capability issued only by ``verified_market_scan_read``."""

    @property
    def run(self) -> MarketScanRun: ...

    @property
    def snapshot_digest(self) -> str | None: ...

    @property
    def action_source_digest(self) -> str | None: ...

    @property
    def probability_source_capture_state(self) -> Mapping[str, object] | None: ...

    @property
    def success_score_contract(self) -> MarketScanProductionScoreContract | None: ...

    def results_page(self, **query: object) -> MarketScanResultPage: ...


class _VerifiedMarketScanReadSession:
    """Opaque read capability that is valid only inside its repository context."""

    __slots__ = (
        "_action_source_digest",
        "_active",
        "_capture_state",
        "_page_reader",
        "_page_read",
        "_release_guard",
        "_run",
        "_score_contract",
        "_snapshot_digest",
        "_state_validator",
        "_thread_id",
    )

    def __init__(
        self,
        run: MarketScanRun,
        *,
        snapshot_digest: str | None,
        action_source_digest: str | None,
        capture_state: Mapping[str, object] | None,
        score_contract: MarketScanProductionScoreContract | None,
        state_validator: Callable[[], None],
        page_reader: Callable[[Mapping[str, object]], MarketScanResultPage],
        release_guard: Callable[[], None],
    ) -> None:
        self._run = run
        self._snapshot_digest = snapshot_digest
        self._action_source_digest = action_source_digest
        self._capture_state = (
            MappingProxyType(dict(capture_state))
            if capture_state is not None
            else None
        )
        self._score_contract = score_contract
        self._thread_id = threading.get_ident()
        self._state_validator = state_validator
        self._page_reader = page_reader
        self._release_guard = release_guard
        self._active = True
        self._page_read = False

    @property
    def run(self) -> MarketScanRun:
        self._require_active()
        return self._run

    @property
    def snapshot_digest(self) -> str | None:
        self._require_active()
        return self._snapshot_digest

    @property
    def action_source_digest(self) -> str | None:
        self._require_active()
        return self._action_source_digest

    @property
    def probability_source_capture_state(self) -> Mapping[str, object] | None:
        self._require_active()
        return self._capture_state

    @property
    def success_score_contract(self) -> MarketScanProductionScoreContract | None:
        self._require_active()
        return self._score_contract

    def results_page(self, **query: object) -> MarketScanResultPage:
        self._require_active()
        if self._page_read:
            raise RuntimeError("同一已验证榜单读取上下文只能读取一次分页")
        self._page_read = True
        return self._page_reader(query)

    def _require_active(self) -> None:
        if not self._active:
            raise RuntimeError("已验证榜单读取上下文已关闭，不能跨请求复用")
        if threading.get_ident() != self._thread_id:
            raise RuntimeError("已验证榜单读取上下文不能跨线程复用")
        self._state_validator()

    def _close(self, token: object) -> None:
        if token is not _SESSION_CLOSE_TOKEN:
            raise RuntimeError("已验证榜单读取上下文只能由仓储关闭")
        self._active = False
        self._release_guard()


@contextmanager
def verified_market_scan_read(
    path: Path,
    run_id: int,
) -> Iterator[VerifiedMarketScanRead]:
    """Own one read connection and issue a non-reusable verified session."""
    connections = SQLiteConnectionFactory(path)
    with connections.read_snapshot() as conn:
        with _verified_market_scan_read_in_snapshot(conn, run_id) as session:
            yield session


@contextmanager
def _verified_market_scan_read_in_snapshot(
    conn: sqlite3.Connection,
    run_id: int,
) -> Iterator[VerifiedMarketScanRead]:
    _require_read_snapshot(conn)
    run_row = required_run_row(conn, run_id)
    snapshot_digest, action_source_digest = _verified_read_identity(conn, run_row)
    capture_state = None
    score_contract = None
    if action_source_digest is not None:
        capture_state = read_probability_source_capture_state(conn, run_id)
        score_contract = read_production_score_contract(
            conn,
            run_id,
            expected_count=int(run_row["success_count"] or 0),
        )
    state_validator, release_guard = _connection_state_guard(conn)

    def page_reader(query: Mapping[str, object]) -> MarketScanResultPage:
        return _verified_market_scan_result_page(conn, run_row, **query)

    session = _VerifiedMarketScanReadSession(
        run_from_row(run_row),
        snapshot_digest=snapshot_digest,
        action_source_digest=action_source_digest,
        capture_state=capture_state,
        score_contract=score_contract,
        state_validator=state_validator,
        page_reader=page_reader,
        release_guard=release_guard,
    )
    try:
        yield session
    finally:
        session._close(_SESSION_CLOSE_TOKEN)  # noqa: SLF001


def _require_read_snapshot(conn: sqlite3.Connection) -> None:
    query_only = conn.execute("PRAGMA query_only").fetchone()
    if not conn.in_transaction or query_only is None or int(query_only[0]) != 1:
        raise RuntimeError("已验证榜单读取必须运行在单一只读 SQLite snapshot 内")


def _schema_version(conn: sqlite3.Connection) -> int:
    row = conn.execute("PRAGMA schema_version").fetchone()
    if row is None:
        raise RuntimeError("无法读取 SQLite schema version")
    return int(row[0])


def _connection_state_guard(
    conn: sqlite3.Connection,
) -> tuple[Callable[[], None], Callable[[], None]]:
    total_changes = conn.total_changes
    schema_version = _schema_version(conn)
    transaction_invalidated = False

    def trace_statement(statement: str) -> None:
        nonlocal transaction_invalidated
        if _statement_invalidates_snapshot(statement):
            transaction_invalidated = True

    def validate() -> None:
        _validate_connection_state(
            conn,
            transaction_invalidated=transaction_invalidated,
            total_changes=total_changes,
            schema_version=schema_version,
        )

    def release() -> None:
        try:
            conn.set_trace_callback(None)
        except sqlite3.Error:
            pass

    conn.set_trace_callback(trace_statement)
    return validate, release


def _statement_invalidates_snapshot(statement: str) -> bool:
    normalized = " ".join(statement.upper().split())
    return normalized.startswith(("BEGIN", "COMMIT", "END", "ROLLBACK")) or (
        normalized.startswith("PRAGMA QUERY_ONLY") and "=" in normalized
    )


def _validate_connection_state(
    conn: sqlite3.Connection,
    *,
    transaction_invalidated: bool,
    total_changes: int,
    schema_version: int,
) -> None:
    try:
        query_only = conn.execute("PRAGMA query_only").fetchone()
        current_schema_version = _schema_version(conn)
    except sqlite3.Error as exc:
        raise RuntimeError("已验证榜单读取的 SQLite snapshot 已失效") from exc
    if (
        transaction_invalidated
        or not conn.in_transaction
        or query_only is None
        or int(query_only[0]) != 1
    ):
        raise RuntimeError("已验证榜单读取的 SQLite snapshot 已失效")
    if conn.total_changes != total_changes:
        raise RuntimeError("已验证榜单读取连接在验证后发生了写入")
    if current_schema_version != schema_version:
        raise RuntimeError("已验证榜单读取的数据库结构在验证后发生了变化")


def _verified_read_identity(
    conn: sqlite3.Connection,
    run_row: sqlite3.Row,
) -> tuple[str | None, str | None]:
    run_id = int(run_row["id"])
    if str(run_row["status"]) not in {"success", "degraded"}:
        return None, None
    if str(run_row["snapshot_seal_origin"] or "") != "publication":
        return verify_market_scan_snapshot(conn, run_id), None
    inspection = inspect_market_scan_action_source(conn, run_id)
    expected = str(run_row["snapshot_digest"] or "")
    if inspection.snapshot_digest != expected:
        raise MarketScanSnapshotSealError(
            f"run {run_id} 已验证快照摘要与同事务批次行不一致"
        )
    return (
        inspection.snapshot_digest,
        inspection.snapshot_digest if inspection.eligible else None,
    )


def _verified_market_scan_result_page(
    conn: sqlite3.Connection,
    run_row: sqlite3.Row,
    **query: object,
) -> MarketScanResultPage:
    run_id = int(run_row["id"])
    spec = _screen_spec(query)
    where, params, order_sql = market_scan_result_screen_sql(
        run_id,
        spec,
        symbols=cast(MarketScanFilterValues, query["symbols"]),
    )
    page = cast(int, query["page"])
    page_size = cast(int, query["page_size"])
    total = int(
        conn.execute(
            f"SELECT COUNT(*) FROM market_scan_result WHERE {where}",
            params,
        ).fetchone()[0]
    )
    rows = conn.execute(
        f"""
        SELECT * FROM market_scan_result
        WHERE {where}
        ORDER BY {order_sql}
        LIMIT ? OFFSET ?
        """,
        (*params, page_size, (page - 1) * page_size),
    ).fetchall()
    registered = conn.execute(
        """
        SELECT production_score_rule_version, production_score_spec_hash
        FROM market_scan_rule_contract
        WHERE rule_version = ?
        """,
        (run_row["rule_version"],),
    ).fetchone()
    run = run_from_row(run_row)
    items = _verified_result_items(rows, run=run, registered=registered)
    return MarketScanResultPage(
        run=run,
        items=items,
        total=total,
        page=page,
        page_size=page_size,
        page_count=page_count(total, page_size),
    )


def _screen_spec(query: Mapping[str, object]) -> ScreenSpecV2:
    return screen_spec_from_market_scan_filters(
        status=cast(MarketScanResultStatus | None, query["status"]),
        market=cast(MarketScanFilterValues, query["market"]),
        industry=cast(MarketScanFilterValues, query["industry"]),
        is_st=cast(bool | None, query["is_st"]),
        is_new=cast(bool | None, query["is_new"]),
        min_score=cast(int | None, query["min_score"]),
        max_score=cast(int | None, query["max_score"]),
        min_trend_score=cast(int | None, query["min_trend_score"]),
        max_trend_score=cast(int | None, query["max_trend_score"]),
        min_change_pct=cast(float | None, query["min_change_pct"]),
        max_change_pct=cast(float | None, query["max_change_pct"]),
        min_turnover_rate=cast(float | None, query["min_turnover_rate"]),
        max_turnover_rate=cast(float | None, query["max_turnover_rate"]),
        min_amount=cast(float | None, query["min_amount"]),
        max_amount=cast(float | None, query["max_amount"]),
        min_data_quality_score=cast(int | None, query["min_data_quality_score"]),
        max_data_quality_score=cast(int | None, query["max_data_quality_score"]),
        min_confidence=cast(float | None, query["min_confidence"]),
        max_risk=cast(float | None, query["max_risk"]),
        min_tradability=cast(float | None, query["min_tradability"]),
        keyword=cast(str | None, query["keyword"]),
        sort=cast(MarketScanSortValues, query["sort"]),
        order=cast(MarketScanSortOrderValues, query["order"]),
    )


def _verified_result_items(
    rows: list[sqlite3.Row],
    *,
    run: MarketScanRun,
    registered: sqlite3.Row | None,
) -> list[MarketScanResultItem]:
    items = [result_from_row(row) for row in rows]
    expected_rule = (
        registered["production_score_rule_version"]
        if registered is not None
        else None
    )
    expected_hash = (
        registered["production_score_spec_hash"]
        if registered is not None
        else None
    )
    for item in items:
        verify_persisted_market_scan_result(
            item,
            run,
            expected_score_rule_version=expected_rule,
            expected_score_spec_hash=expected_hash,
        )
    return items


__all__ = [
    "MARKET_SCAN_VERIFIED_RESULT_READ_ARGUMENTS",
    "VerifiedMarketScanRead",
    "verified_market_scan_read",
]
