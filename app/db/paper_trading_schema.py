"""SQLite schema and idempotent v1-to-v2 migration for paper trading."""

from __future__ import annotations

import hashlib
import json
import sqlite3

from app.models.paper_trading_config import (
    DEFAULT_PAPER_ACCOUNT_NAME,
    DEFAULT_PAPER_INITIAL_CASH,
    MODELLED_ONE_WAY_FRICTION_PCT,
)
from app.utils.audit_time import audit_now_text


PAPER_TRADING_SCHEMA_VERSION = "20260730_paper_trading_v2"
PAPER_TRADING_OUTPUT_DIGEST_SCHEMA_VERSION = "20260813_paper_trading_output_digest_v3"

PAPER_TRADING_SCHEMA_SQL = """
CREATE TABLE IF NOT EXISTS paper_trading_account (
    id INTEGER PRIMARY KEY CHECK (id = 1),
    name TEXT NOT NULL CHECK (length(trim(name)) BETWEEN 1 AND 80),
    initial_cash REAL NOT NULL CHECK (initial_cash BETWEEN 10000 AND 1000000000),
    modelled_one_way_friction_pct REAL NOT NULL CHECK (modelled_one_way_friction_pct >= 0),
    default_cost_profile TEXT NOT NULL DEFAULT 'base'
        CHECK (default_cost_profile IN ('base', 'conservative', 'stress')),
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS paper_strategy (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    plan_id INTEGER NOT NULL CHECK (plan_id > 0),
    plan_revision INTEGER NOT NULL CHECK (plan_revision > 0),
    plan_payload_digest TEXT NOT NULL CHECK (
        length(plan_payload_digest) = 64
        AND plan_payload_digest NOT GLOB '*[^0-9a-f]*'
    ),
    advice_id INTEGER NOT NULL CHECK (advice_id > 0),
    symbol TEXT NOT NULL,
    activation_market_time TEXT NOT NULL,
    allocation_pct REAL NOT NULL CHECK (allocation_pct BETWEEN 1 AND 100),
    priority INTEGER NOT NULL DEFAULT 0 CHECK (priority BETWEEN -1000 AND 1000),
    entry_expiry_sessions INTEGER NOT NULL DEFAULT 5
        CHECK (entry_expiry_sessions BETWEEN 1 AND 60),
    snapshot_market_time TEXT NOT NULL,
    snapshot_price REAL NOT NULL CHECK (snapshot_price > 0),
    snapshot_adjustment_mode TEXT NOT NULL,
    snapshot_anchor_date TEXT,
    snapshot_anchor_close REAL,
    snapshot_data_version TEXT NOT NULL,
    snapshot_contract_version TEXT NOT NULL,
    target_price REAL NOT NULL CHECK (target_price > 0),
    stop_price REAL NOT NULL CHECK (stop_price > 0),
    horizon_days INTEGER NOT NULL CHECK (horizon_days BETWEEN 1 AND 60),
    status TEXT NOT NULL DEFAULT 'pending',
    normalized_target_price REAL,
    normalized_stop_price REAL,
    entry_date TEXT,
    entry_price REAL,
    quantity INTEGER NOT NULL DEFAULT 0 CHECK (quantity >= 0),
    buy_friction REAL NOT NULL DEFAULT 0 CHECK (buy_friction >= 0),
    held_sessions INTEGER NOT NULL DEFAULT 0 CHECK (held_sessions >= 0),
    last_price REAL,
    exit_date TEXT,
    exit_price REAL,
    exit_reason TEXT,
    sell_friction REAL NOT NULL DEFAULT 0 CHECK (sell_friction >= 0),
    realized_pnl REAL,
    return_pct REAL,
    error_message TEXT,
    last_processed_date TEXT,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL,
    UNIQUE(plan_id, plan_revision)
);

CREATE TABLE IF NOT EXISTS paper_trading_run (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    as_of TEXT NOT NULL,
    rule_version TEXT NOT NULL,
    modelled_one_way_friction_pct REAL NOT NULL DEFAULT 0,
    cost_profile_id TEXT NOT NULL DEFAULT 'legacy',
    cost_profile_name TEXT NOT NULL DEFAULT 'legacy',
    cost_profile_version TEXT NOT NULL DEFAULT 'legacy',
    benchmark_symbol TEXT,
    benchmark_status TEXT NOT NULL DEFAULT 'unavailable',
    benchmark_message TEXT,
    strategy_count INTEGER NOT NULL CHECK (strategy_count >= 0),
    execution_count INTEGER NOT NULL CHECK (execution_count >= 0),
    closed_count INTEGER NOT NULL CHECK (closed_count >= 0),
    data_unavailable_count INTEGER NOT NULL CHECK (data_unavailable_count >= 0),
    input_fingerprint TEXT NOT NULL DEFAULT '',
    output_digest TEXT NOT NULL DEFAULT 'legacy-unverified' CHECK (
        output_digest = 'legacy-unverified'
        OR (
            length(output_digest) = 64
            AND output_digest NOT GLOB '*[^0-9a-f]*'
        )
    ),
    strategy_snapshot_hash TEXT NOT NULL DEFAULT '',
    market_data_hash TEXT NOT NULL DEFAULT '',
    data_start_date TEXT,
    data_end_date TEXT,
    configuration_json TEXT NOT NULL DEFAULT '{}',
    rule_profiles_json TEXT NOT NULL DEFAULT '[]',
    data_sources_json TEXT NOT NULL DEFAULT '[]',
    message TEXT NOT NULL,
    created_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS paper_strategy_result (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    run_id INTEGER NOT NULL,
    strategy_id INTEGER NOT NULL,
    status TEXT NOT NULL,
    allocation_order INTEGER,
    normalized_target_price REAL,
    normalized_stop_price REAL,
    entry_wait_sessions INTEGER NOT NULL DEFAULT 0,
    entry_date TEXT,
    entry_price REAL,
    quantity INTEGER NOT NULL DEFAULT 0,
    buy_friction REAL NOT NULL DEFAULT 0,
    held_sessions INTEGER NOT NULL DEFAULT 0,
    last_price REAL,
    pending_exit_reason TEXT,
    pending_exit_date TEXT,
    exit_date TEXT,
    exit_price REAL,
    exit_reason TEXT,
    sell_friction REAL NOT NULL DEFAULT 0,
    gross_realized_pnl REAL,
    realized_pnl REAL,
    return_pct REAL,
    rule_profile_id TEXT,
    rule_data_degraded INTEGER NOT NULL DEFAULT 0 CHECK (rule_data_degraded IN (0, 1)),
    error_message TEXT,
    last_processed_date TEXT,
    created_at TEXT NOT NULL,
    FOREIGN KEY(run_id) REFERENCES paper_trading_run(id) ON DELETE CASCADE,
    FOREIGN KEY(strategy_id) REFERENCES paper_strategy(id) ON DELETE RESTRICT,
    UNIQUE(run_id, strategy_id)
);

CREATE TABLE IF NOT EXISTS paper_trade (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    run_id INTEGER NOT NULL,
    strategy_id INTEGER NOT NULL,
    symbol TEXT NOT NULL,
    side TEXT NOT NULL CHECK (side IN ('buy', 'sell')),
    trade_date TEXT NOT NULL,
    price REAL NOT NULL CHECK (price > 0),
    quantity INTEGER NOT NULL CHECK (quantity > 0),
    gross_amount REAL NOT NULL CHECK (gross_amount > 0),
    commission_amount REAL NOT NULL DEFAULT 0 CHECK (commission_amount >= 0),
    stamp_duty_amount REAL NOT NULL DEFAULT 0 CHECK (stamp_duty_amount >= 0),
    transfer_fee_amount REAL NOT NULL DEFAULT 0 CHECK (transfer_fee_amount >= 0),
    slippage_amount REAL NOT NULL DEFAULT 0 CHECK (slippage_amount >= 0),
    friction_amount REAL NOT NULL CHECK (friction_amount >= 0),
    reason TEXT NOT NULL,
    created_at TEXT NOT NULL,
    FOREIGN KEY(run_id) REFERENCES paper_trading_run(id) ON DELETE CASCADE,
    FOREIGN KEY(strategy_id) REFERENCES paper_strategy(id) ON DELETE RESTRICT,
    UNIQUE(run_id, strategy_id, side)
);

CREATE TABLE IF NOT EXISTS paper_equity_snapshot (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    run_id INTEGER NOT NULL,
    as_of_date TEXT NOT NULL,
    cash_balance REAL NOT NULL,
    market_value REAL NOT NULL,
    estimated_exit_friction REAL NOT NULL,
    total_equity REAL NOT NULL,
    gross_equity REAL NOT NULL,
    cumulative_cost REAL NOT NULL,
    realized_pnl REAL NOT NULL,
    unrealized_pnl REAL NOT NULL,
    return_pct REAL NOT NULL,
    gross_return_pct REAL NOT NULL,
    benchmark_equity REAL,
    benchmark_return_pct REAL,
    excess_return_pct REAL,
    exposure_pct REAL NOT NULL,
    drawdown_pct REAL NOT NULL,
    created_at TEXT NOT NULL,
    FOREIGN KEY(run_id) REFERENCES paper_trading_run(id) ON DELETE CASCADE,
    UNIQUE(run_id, as_of_date)
);

CREATE TABLE IF NOT EXISTS paper_trading_event (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    run_id INTEGER NOT NULL,
    sequence INTEGER NOT NULL CHECK (sequence > 0),
    strategy_id INTEGER,
    symbol TEXT,
    event_date TEXT NOT NULL,
    event_code TEXT NOT NULL,
    category TEXT NOT NULL,
    severity TEXT NOT NULL,
    message TEXT NOT NULL,
    details_json TEXT NOT NULL DEFAULT '{}',
    created_at TEXT NOT NULL,
    FOREIGN KEY(run_id) REFERENCES paper_trading_run(id) ON DELETE CASCADE,
    FOREIGN KEY(strategy_id) REFERENCES paper_strategy(id) ON DELETE RESTRICT,
    UNIQUE(run_id, sequence)
);
"""

_RUN_COMPAT_COLUMNS = {
    "cost_profile_id": "TEXT NOT NULL DEFAULT 'legacy'",
    "cost_profile_name": "TEXT NOT NULL DEFAULT 'legacy'",
    "cost_profile_version": "TEXT NOT NULL DEFAULT 'legacy'",
    "benchmark_symbol": "TEXT",
    "benchmark_status": "TEXT NOT NULL DEFAULT 'unavailable'",
    "benchmark_message": "TEXT",
    "input_fingerprint": "TEXT NOT NULL DEFAULT ''",
    "output_digest": "TEXT NOT NULL DEFAULT 'legacy-unverified'",
    "strategy_snapshot_hash": "TEXT NOT NULL DEFAULT ''",
    "market_data_hash": "TEXT NOT NULL DEFAULT ''",
    "data_start_date": "TEXT",
    "data_end_date": "TEXT",
    "configuration_json": "TEXT NOT NULL DEFAULT '{}'",
    "rule_profiles_json": "TEXT NOT NULL DEFAULT '[]'",
    "data_sources_json": "TEXT NOT NULL DEFAULT '[]'",
}


def apply_paper_trading_schema(conn: sqlite3.Connection) -> None:
    conn.executescript(PAPER_TRADING_SCHEMA_SQL)
    _ensure_column(conn, "paper_trading_account", "default_cost_profile", "TEXT NOT NULL DEFAULT 'base'")
    _ensure_default_account(conn)
    _ensure_column(conn, "paper_strategy", "priority", "INTEGER NOT NULL DEFAULT 0")
    _ensure_column(conn, "paper_strategy", "entry_expiry_sessions", "INTEGER NOT NULL DEFAULT 5")
    _ensure_column(conn, "paper_strategy", "plan_payload_digest", "TEXT NOT NULL DEFAULT 'legacy-unverified'")
    for name, declaration in _RUN_COMPAT_COLUMNS.items():
        _ensure_column(conn, "paper_trading_run", name, declaration)
    _migrate_trade_table(conn)
    _migrate_equity_table(conn)
    conn.executescript(PAPER_TRADING_SCHEMA_SQL)
    _backfill_legacy_strategy_results(conn)
    _backfill_paper_run_output_digests(conn)
    _create_indexes(conn)
    conn.execute(
        "INSERT OR IGNORE INTO schema_migration (name) VALUES (?)",
        (PAPER_TRADING_SCHEMA_VERSION,),
    )
    conn.execute(
        "INSERT OR IGNORE INTO schema_migration (name) VALUES (?)",
        (PAPER_TRADING_OUTPUT_DIGEST_SCHEMA_VERSION,),
    )


def _ensure_default_account(conn: sqlite3.Connection) -> None:
    timestamp = audit_now_text()
    conn.execute(
        """
        INSERT OR IGNORE INTO paper_trading_account (
            id, name, initial_cash, modelled_one_way_friction_pct,
            default_cost_profile, created_at, updated_at
        ) VALUES (1, ?, ?, ?, 'base', ?, ?)
        """,
        (
            DEFAULT_PAPER_ACCOUNT_NAME,
            DEFAULT_PAPER_INITIAL_CASH,
            MODELLED_ONE_WAY_FRICTION_PCT,
            timestamp,
            timestamp,
        ),
    )


def _migrate_trade_table(conn: sqlite3.Connection) -> None:
    if "run_id" in _column_names(conn, "paper_trade"):
        return
    run_id = _legacy_run_id(conn, needed=_row_count(conn, "paper_trade") > 0)
    conn.execute("ALTER TABLE paper_trade RENAME TO paper_trade_v1")
    conn.executescript(_table_sql("paper_trade"))
    if run_id is not None:
        conn.execute(
            """
            INSERT INTO paper_trade (
                run_id, strategy_id, symbol, side, trade_date, price, quantity,
                gross_amount, commission_amount, stamp_duty_amount,
                transfer_fee_amount, slippage_amount, friction_amount, reason, created_at
            )
            SELECT ?, strategy_id, symbol, side, trade_date, price, quantity,
                   gross_amount, friction_amount, 0, 0, 0, friction_amount, reason, created_at
            FROM paper_trade_v1
            """,
            (run_id,),
        )
    conn.execute("DROP TABLE paper_trade_v1")


def _migrate_equity_table(conn: sqlite3.Connection) -> None:
    if "run_id" in _column_names(conn, "paper_equity_snapshot"):
        return
    run_id = _legacy_run_id(conn, needed=_row_count(conn, "paper_equity_snapshot") > 0)
    conn.execute("ALTER TABLE paper_equity_snapshot RENAME TO paper_equity_snapshot_v1")
    conn.executescript(_table_sql("paper_equity_snapshot"))
    if run_id is not None:
        conn.execute(
            """
            INSERT INTO paper_equity_snapshot (
                run_id, as_of_date, cash_balance, market_value,
                estimated_exit_friction, total_equity, gross_equity,
                cumulative_cost, realized_pnl, unrealized_pnl, return_pct,
                gross_return_pct, benchmark_equity, benchmark_return_pct,
                excess_return_pct, exposure_pct, drawdown_pct, created_at
            )
            SELECT ?, as_of_date, cash_balance, market_value,
                   estimated_exit_friction, total_equity,
                   total_equity + estimated_exit_friction,
                   estimated_exit_friction, realized_pnl, unrealized_pnl,
                   return_pct, return_pct, NULL, NULL, NULL,
                   CASE WHEN total_equity > 0 THEN market_value / total_equity * 100 ELSE 0 END,
                   drawdown_pct, created_at
            FROM paper_equity_snapshot_v1
            """,
            (run_id,),
        )
    conn.execute("DROP TABLE paper_equity_snapshot_v1")


def _legacy_run_id(conn: sqlite3.Connection, *, needed: bool) -> int | None:
    row = conn.execute("SELECT id FROM paper_trading_run ORDER BY id DESC LIMIT 1").fetchone()
    if row is not None:
        return int(row[0])
    if not needed:
        return None
    account = conn.execute("SELECT created_at FROM paper_trading_account WHERE id = 1").fetchone()
    created_at = str(account[0]) if account is not None else "1970-01-01T00:00:00.000000Z"
    cursor = conn.execute(
        """
        INSERT INTO paper_trading_run (
            as_of, rule_version, modelled_one_way_friction_pct,
            strategy_count, execution_count, closed_count,
            data_unavailable_count, message, created_at
        ) VALUES (
            '1970-01-01 00:00:00', 'paper-review-plan-v1-legacy', 0.05,
            (SELECT COUNT(*) FROM paper_strategy),
            (SELECT COUNT(*) FROM paper_trade),
            (SELECT COUNT(*) FROM paper_strategy WHERE status = 'closed'),
            (SELECT COUNT(*) FROM paper_strategy WHERE status = 'data_unavailable'),
            '由 v1 模拟账本迁移，历史成本拆分不可恢复', ?
        )
        """,
        (created_at,),
    )
    run_id = cursor.lastrowid
    if run_id is None:
        raise RuntimeError("旧模拟交易批次迁移失败")
    return int(run_id)


def _backfill_legacy_strategy_results(conn: sqlite3.Connection) -> None:
    run_row = conn.execute("SELECT id, created_at FROM paper_trading_run ORDER BY id DESC LIMIT 1").fetchone()
    if run_row is None:
        return
    run_id = int(run_row[0])
    if conn.execute("SELECT COUNT(*) FROM paper_strategy_result WHERE run_id = ?", (run_id,)).fetchone()[0]:
        return
    conn.execute(
        """
        INSERT OR IGNORE INTO paper_strategy_result (
            run_id, strategy_id, status, normalized_target_price,
            normalized_stop_price, entry_date, entry_price, quantity,
            buy_friction, held_sessions, last_price, exit_date, exit_price,
            exit_reason, sell_friction, realized_pnl, return_pct,
            error_message, last_processed_date, created_at
        )
        SELECT ?, id, status, normalized_target_price, normalized_stop_price,
               entry_date, entry_price, quantity, buy_friction, held_sessions,
               last_price, exit_date, exit_price, exit_reason, sell_friction,
               realized_pnl, return_pct, error_message, last_processed_date, ?
        FROM paper_strategy
        """,
        (run_id, str(run_row[1])),
    )


def paper_run_output_digest(conn: sqlite3.Connection, run_id: int) -> str:
    """Hash the complete persisted run projection used by dashboards/exports."""

    payload = _paper_run_output_payload(conn, run_id)
    try:
        encoded = json.dumps(
            payload,
            ensure_ascii=False,
            allow_nan=False,
            sort_keys=True,
            separators=(",", ":"),
        ).encode("utf-8")
    except (TypeError, ValueError) as exc:
        raise ValueError("模拟运行包含无法规范化的输出数据") from exc
    return hashlib.sha256(encoded).hexdigest()


def _paper_run_output_payload(
    conn: sqlite3.Connection,
    run_id: int,
) -> dict[str, object]:
    run_rows = _query_dicts(conn, "SELECT * FROM paper_trading_run WHERE id = ?", (run_id,))
    if not run_rows:
        raise ValueError("模拟运行不存在，不能校验输出摘要")
    run = _without_keys(run_rows[0], "id", "output_digest", "created_at")
    strategy_rows, strategy_identity = _paper_strategy_output_rows(conn, run_id)
    result_rows, trade_rows = _paper_run_strategy_children(conn, run_id)
    _validate_run_output_counts(run_rows[0], result_rows, trade_rows)
    payload: dict[str, object] = {
        "run": run,
        "strategies": [
            _without_keys(row, "id", "plan_id", "advice_id", "created_at", "updated_at")
            for row in strategy_rows
        ],
        "strategy_results": _canonical_run_children(result_rows, strategy_identity),
        "trades": _canonical_run_children(trade_rows, strategy_identity),
        "equity": _paper_equity_output_rows(conn, run_id),
        "events": _canonical_run_children(
            _query_dicts(
                conn,
                "SELECT * FROM paper_trading_event WHERE run_id = ? ORDER BY sequence, id",
                (run_id,),
            ),
            strategy_identity,
        ),
    }
    return payload


def _paper_run_strategy_children(
    conn: sqlite3.Connection,
    run_id: int,
) -> tuple[list[dict[str, object]], list[dict[str, object]]]:
    results = _query_dicts(
        conn,
        """
        SELECT result.*
        FROM paper_strategy_result AS result
        JOIN paper_strategy AS strategy ON strategy.id = result.strategy_id
        WHERE result.run_id = ?
        ORDER BY result.allocation_order, strategy.plan_revision,
                 strategy.plan_payload_digest, strategy.symbol
        """,
        (run_id,),
    )
    trades = _query_dicts(
        conn,
        """
        SELECT trade.*
        FROM paper_trade AS trade
        JOIN paper_strategy AS strategy ON strategy.id = trade.strategy_id
        LEFT JOIN paper_strategy_result AS result
          ON result.run_id = trade.run_id AND result.strategy_id = trade.strategy_id
        WHERE trade.run_id = ?
        ORDER BY trade.trade_date, trade.side, result.allocation_order,
                 strategy.plan_revision, strategy.plan_payload_digest, strategy.symbol
        """,
        (run_id,),
    )
    return results, trades


def _paper_strategy_output_rows(
    conn: sqlite3.Connection,
    run_id: int,
) -> tuple[list[dict[str, object]], dict[int, tuple[int, str, str]]]:
    strategy_rows = _query_dicts(
        conn,
        """
        SELECT strategy.*
        FROM paper_strategy AS strategy
        JOIN paper_strategy_result AS result ON result.strategy_id = strategy.id
        WHERE result.run_id = ?
        ORDER BY result.allocation_order, strategy.plan_revision,
                 strategy.plan_payload_digest, strategy.symbol, strategy.id
        """,
        (run_id,),
    )
    strategy_identity = {
        _database_int(row["id"]): (
            _database_int(row["plan_revision"]),
            str(row["plan_payload_digest"]),
            str(row["symbol"]),
        )
        for row in strategy_rows
    }
    return strategy_rows, strategy_identity


def _paper_equity_output_rows(
    conn: sqlite3.Connection,
    run_id: int,
) -> list[dict[str, object]]:
    return [
        _without_keys(row, "id", "run_id", "created_at")
        for row in _query_dicts(
            conn,
            "SELECT * FROM paper_equity_snapshot WHERE run_id = ? ORDER BY as_of_date, id",
            (run_id,),
        )
    ]


def _canonical_run_children(
    rows: list[dict[str, object]],
    strategy_identity: dict[int, tuple[int, str, str]],
) -> list[dict[str, object]]:
    canonical: list[dict[str, object]] = []
    for row in rows:
        strategy_id = row.get("strategy_id")
        clean = _without_keys(row, "id", "run_id", "strategy_id", "created_at")
        identity = strategy_identity.get(_database_int(strategy_id)) if strategy_id is not None else None
        if strategy_id is not None and identity is None:
            raise ValueError("模拟运行子记录未绑定同一运行的策略结果")
        if identity is not None and row.get("symbol") is not None and str(row["symbol"]) != identity[2]:
            raise ValueError("模拟运行子记录股票身份与策略不一致")
        clean["strategy_identity"] = identity
        canonical.append(clean)
    return canonical


def _validate_run_output_counts(
    run: dict[str, object],
    results: list[dict[str, object]],
    trades: list[dict[str, object]],
) -> None:
    observed = (
        len(results),
        len(trades),
        sum(str(row.get("status")) == "closed" for row in results),
        sum(str(row.get("status")) == "data_unavailable" for row in results),
    )
    declared = tuple(
        _database_int(run[field])
        for field in ("strategy_count", "execution_count", "closed_count", "data_unavailable_count")
    )
    if observed != declared:
        raise ValueError("模拟运行声明计数与输出账本不一致")


def _without_keys(row: dict[str, object], *keys: str) -> dict[str, object]:
    excluded = set(keys)
    return {key: value for key, value in row.items() if key not in excluded}


def _database_int(value: object) -> int:
    if isinstance(value, bool) or not isinstance(value, int | str):
        raise ValueError("模拟运行包含无效数据库标识")
    return int(value)


def _backfill_paper_run_output_digests(conn: sqlite3.Connection) -> None:
    rows = conn.execute(
        "SELECT id, output_digest FROM paper_trading_run ORDER BY id"
    ).fetchall()
    for row in rows:
        run_id = int(row[0])
        digest = str(row[1] or "")
        if len(digest) == 64 and all(character in "0123456789abcdef" for character in digest):
            continue
        conn.execute(
            "UPDATE paper_trading_run SET output_digest = ? WHERE id = ?",
            (paper_run_output_digest(conn, run_id), run_id),
        )


def _query_dicts(
    conn: sqlite3.Connection,
    statement: str,
    parameters: tuple[object, ...],
) -> list[dict[str, object]]:
    cursor = conn.execute(statement, parameters)
    columns = [str(item[0]) for item in cursor.description or ()]
    return [dict(zip(columns, tuple(row), strict=True)) for row in cursor.fetchall()]


def _create_indexes(conn: sqlite3.Connection) -> None:
    conn.executescript(
        """
        CREATE INDEX IF NOT EXISTS idx_paper_strategy_priority
            ON paper_strategy(activation_market_time, priority DESC, plan_id, id);
        CREATE INDEX IF NOT EXISTS idx_paper_strategy_result_run
            ON paper_strategy_result(run_id, allocation_order, strategy_id);
        CREATE INDEX IF NOT EXISTS idx_paper_trade_run_date
            ON paper_trade(run_id, trade_date DESC, id DESC);
        CREATE INDEX IF NOT EXISTS idx_paper_equity_run_date
            ON paper_equity_snapshot(run_id, as_of_date ASC);
        CREATE INDEX IF NOT EXISTS idx_paper_event_run_sequence
            ON paper_trading_event(run_id, sequence ASC);
        CREATE INDEX IF NOT EXISTS idx_paper_run_time
            ON paper_trading_run(created_at DESC, id DESC);
        """
    )


def _ensure_column(conn: sqlite3.Connection, table: str, column: str, declaration: str) -> None:
    if column not in _column_names(conn, table):
        conn.execute(f"ALTER TABLE {table} ADD COLUMN {column} {declaration}")


def _column_names(conn: sqlite3.Connection, table: str) -> set[str]:
    return {str(row[1]) for row in conn.execute(f"PRAGMA table_info({table})")}


def _row_count(conn: sqlite3.Connection, table: str) -> int:
    return int(conn.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0])


def _table_sql(table: str) -> str:
    start = PAPER_TRADING_SCHEMA_SQL.index(f"CREATE TABLE IF NOT EXISTS {table} (")
    end = PAPER_TRADING_SCHEMA_SQL.index("\n);", start) + 3
    return PAPER_TRADING_SCHEMA_SQL[start:end]


__all__ = [
    "PAPER_TRADING_OUTPUT_DIGEST_SCHEMA_VERSION",
    "PAPER_TRADING_SCHEMA_SQL",
    "PAPER_TRADING_SCHEMA_VERSION",
    "apply_paper_trading_schema",
    "paper_run_output_digest",
]
