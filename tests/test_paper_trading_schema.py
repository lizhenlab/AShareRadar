from __future__ import annotations

from pathlib import Path
import sqlite3

from app.db.paper_trading_schema import (
    PAPER_TRADING_SCHEMA_VERSION,
    apply_paper_trading_schema,
)


def test_v1_paper_ledger_migrates_in_place_and_is_idempotent(tmp_path: Path) -> None:
    path = tmp_path / "paper-v1.sqlite3"
    with sqlite3.connect(path) as conn:
        conn.execute("PRAGMA foreign_keys = ON")
        _create_v1_schema(conn)
        _insert_v1_ledger(conn)

        apply_paper_trading_schema(conn)
        first_counts = _v2_counts(conn)
        apply_paper_trading_schema(conn)

        assert _v2_counts(conn) == first_counts == (1, 1, 2, 1)
        assert conn.execute(
            "SELECT COUNT(*) FROM schema_migration WHERE name = ?",
            (PAPER_TRADING_SCHEMA_VERSION,),
        ).fetchone()[0] == 1
        strategy = conn.execute(
            "SELECT priority, entry_expiry_sessions FROM paper_strategy"
        ).fetchone()
        assert strategy == (0, 5)
        trade = conn.execute(
            """
            SELECT run_id, commission_amount, stamp_duty_amount,
                   transfer_fee_amount, slippage_amount, friction_amount
            FROM paper_trade WHERE side = 'buy'
            """
        ).fetchone()
        assert trade == (1, 5.0, 0.0, 0.0, 0.0, 5.0)
        equity = conn.execute(
            """
            SELECT run_id, gross_equity, cumulative_cost,
                   benchmark_equity, exposure_pct
            FROM paper_equity_snapshot
            """
        ).fetchone()
        assert equity == (1, 1000005.0, 5.0, None, 10.0)
        result = conn.execute(
            "SELECT run_id, strategy_id, status, realized_pnl FROM paper_strategy_result"
        ).fetchone()
        assert result == (1, 1, "closed", 900.0)
        assert conn.execute("PRAGMA foreign_key_check").fetchall() == []


def _v2_counts(conn: sqlite3.Connection) -> tuple[int, int, int, int]:
    return tuple(
        int(conn.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0])
        for table in (
            "paper_trading_run",
            "paper_strategy_result",
            "paper_trade",
            "paper_equity_snapshot",
        )
    )


def _create_v1_schema(conn: sqlite3.Connection) -> None:
    conn.executescript(
        """
        CREATE TABLE schema_migration (
            name TEXT PRIMARY KEY,
            applied_at TEXT NOT NULL DEFAULT '2026-07-01T00:00:00.000000Z'
        );
        CREATE TABLE paper_trading_account (
            id INTEGER PRIMARY KEY,
            name TEXT NOT NULL,
            initial_cash REAL NOT NULL,
            modelled_one_way_friction_pct REAL NOT NULL,
            created_at TEXT NOT NULL,
            updated_at TEXT NOT NULL
        );
        CREATE TABLE paper_strategy (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            plan_id INTEGER NOT NULL,
            plan_revision INTEGER NOT NULL,
            advice_id INTEGER NOT NULL,
            symbol TEXT NOT NULL,
            activation_market_time TEXT NOT NULL,
            allocation_pct REAL NOT NULL,
            snapshot_market_time TEXT NOT NULL,
            snapshot_price REAL NOT NULL,
            snapshot_adjustment_mode TEXT NOT NULL,
            snapshot_anchor_date TEXT,
            snapshot_anchor_close REAL,
            snapshot_data_version TEXT NOT NULL,
            snapshot_contract_version TEXT NOT NULL,
            target_price REAL NOT NULL,
            stop_price REAL NOT NULL,
            horizon_days INTEGER NOT NULL,
            status TEXT NOT NULL,
            normalized_target_price REAL,
            normalized_stop_price REAL,
            entry_date TEXT,
            entry_price REAL,
            quantity INTEGER NOT NULL,
            buy_friction REAL NOT NULL,
            held_sessions INTEGER NOT NULL,
            last_price REAL,
            exit_date TEXT,
            exit_price REAL,
            exit_reason TEXT,
            sell_friction REAL NOT NULL,
            realized_pnl REAL,
            return_pct REAL,
            error_message TEXT,
            last_processed_date TEXT,
            created_at TEXT NOT NULL,
            updated_at TEXT NOT NULL
        );
        CREATE TABLE paper_trading_run (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            as_of TEXT NOT NULL,
            rule_version TEXT NOT NULL,
            modelled_one_way_friction_pct REAL NOT NULL,
            strategy_count INTEGER NOT NULL,
            execution_count INTEGER NOT NULL,
            closed_count INTEGER NOT NULL,
            data_unavailable_count INTEGER NOT NULL,
            message TEXT NOT NULL,
            created_at TEXT NOT NULL
        );
        CREATE TABLE paper_trade (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            strategy_id INTEGER NOT NULL,
            symbol TEXT NOT NULL,
            side TEXT NOT NULL,
            trade_date TEXT NOT NULL,
            price REAL NOT NULL,
            quantity INTEGER NOT NULL,
            gross_amount REAL NOT NULL,
            friction_amount REAL NOT NULL,
            reason TEXT NOT NULL,
            created_at TEXT NOT NULL
        );
        CREATE TABLE paper_equity_snapshot (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            as_of_date TEXT NOT NULL UNIQUE,
            cash_balance REAL NOT NULL,
            market_value REAL NOT NULL,
            estimated_exit_friction REAL NOT NULL,
            total_equity REAL NOT NULL,
            realized_pnl REAL NOT NULL,
            unrealized_pnl REAL NOT NULL,
            return_pct REAL NOT NULL,
            drawdown_pct REAL NOT NULL,
            created_at TEXT NOT NULL
        );
        """
    )


def _insert_v1_ledger(conn: sqlite3.Connection) -> None:
    timestamp = "2026-07-01T00:00:00.000000Z"
    conn.execute(
        """
        INSERT INTO paper_trading_account
        VALUES (1, '旧模拟账户', 1000000, 0.05, ?, ?)
        """,
        (timestamp, timestamp),
    )
    conn.execute(
        """
        INSERT INTO paper_strategy (
            plan_id, plan_revision, advice_id, symbol,
            activation_market_time, allocation_pct, snapshot_market_time,
            snapshot_price, snapshot_adjustment_mode, snapshot_anchor_date,
            snapshot_anchor_close, snapshot_data_version,
            snapshot_contract_version, target_price, stop_price, horizon_days,
            status, normalized_target_price, normalized_stop_price,
            entry_date, entry_price, quantity, buy_friction, held_sessions,
            last_price, exit_date, exit_price, exit_reason, sell_friction,
            realized_pnl, return_pct, error_message, last_processed_date,
            created_at, updated_at
        ) VALUES (
            10, 1, 20, '600519', '2026-07-01 10:00:00', 10,
            '2026-07-01 09:45:00', 100, 'qfq', '2026-07-01', 100,
            'v1', 'daily-v1', 110, 90, 20, 'closed', 110, 90,
            '2026-07-02', 100, 1000, 5, 1, 101, '2026-07-03',
            101, 'target_hit', 5, 900, 0.9, NULL, '2026-07-03', ?, ?
        )
        """,
        (timestamp, timestamp),
    )
    conn.executemany(
        """
        INSERT INTO paper_trade (
            strategy_id, symbol, side, trade_date, price, quantity,
            gross_amount, friction_amount, reason, created_at
        ) VALUES (1, '600519', ?, ?, ?, 1000, ?, 5, ?, ?)
        """,
        (
            ("buy", "2026-07-02", 100, 100000, "strategy_entry", timestamp),
            ("sell", "2026-07-03", 101, 101000, "target_hit", timestamp),
        ),
    )
    conn.execute(
        """
        INSERT INTO paper_equity_snapshot VALUES (
            1, '2026-07-03', 900000, 100000, 5, 1000000,
            900, 100, 0, 0, ?
        )
        """,
        (timestamp,),
    )
