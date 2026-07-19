from __future__ import annotations

from pathlib import Path
import sqlite3
import subprocess
import sys
from tempfile import TemporaryDirectory
import unittest
from unittest.mock import patch

from app.db.schema import initialize_schema
from app.db.schema_migrations import (
    QUOTE_HISTORY_CONTRACT_MIGRATION,
    QUOTE_HISTORY_UNIQUE_INDEX,
    apply_compat_migrations,
)


class SchemaCompatibilityTests(unittest.TestCase):
    def test_initialize_schema_adds_market_scan_tables_indexes_and_foreign_keys_idempotently(self) -> None:
        conn = sqlite3.connect(":memory:")
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA foreign_keys = ON")
        self.addCleanup(conn.close)
        conn.execute("CREATE TABLE legacy_marker (value TEXT)")

        initialize_schema(conn)
        initialize_schema(conn)

        tables = {row["name"] for row in conn.execute("SELECT name FROM sqlite_master WHERE type = 'table'")}
        self.assertTrue({"market_scan_run", "market_scan_result"}.issubset(tables))
        result_columns = self._column_names(conn, "market_scan_result")
        self.assertIn("metadata_source", result_columns)
        self.assertTrue(
            {
                "quote_fallback_used",
                "kline_fallback_used",
                "metadata_degraded",
                "degradation_reasons_json",
            }.issubset(result_columns)
        )
        self.assertIn("retry_of_run_id", self._column_names(conn, "market_scan_run"))
        self.assertIn("stock_pool_source", self._column_names(conn, "market_scan_run"))
        run_indexes = self._index_names(conn, "market_scan_run")
        result_indexes = self._index_names(conn, "market_scan_result")
        self.assertIn("uq_market_scan_single_active", run_indexes)
        self.assertIn("idx_market_scan_result_rank", result_indexes)
        self.assertIn("idx_market_scan_result_filters", result_indexes)
        active_index_sql = conn.execute(
            "SELECT sql FROM sqlite_master WHERE type = 'index' AND name = ?",
            ("uq_market_scan_single_active",),
        ).fetchone()[0]
        self.assertIn("WHERE status IN", active_index_sql)

        task_fk = conn.execute("PRAGMA foreign_key_list(market_scan_run)").fetchall()
        result_fk = conn.execute("PRAGMA foreign_key_list(market_scan_result)").fetchall()
        self.assertTrue(any(row["table"] == "task_run" and row["on_delete"] == "SET NULL" for row in task_fk))
        self.assertTrue(any(row["table"] == "market_scan_run" and row["on_delete"] == "SET NULL" for row in task_fk))
        self.assertTrue(any(row["table"] == "market_scan_run" and row["on_delete"] == "CASCADE" for row in result_fk))

        run_id = conn.execute(
            """
            INSERT INTO market_scan_run (
                status, trigger, rule_version, as_of, data_date, scope, created_at, updated_at
            ) VALUES ('queued', 'manual', 'v1', '2026-07-17 16:30:00', '2026-07-17', 'test', 'now', 'now')
            """
        ).lastrowid
        conn.execute(
            """
            INSERT INTO market_scan_result (
                run_id, symbol, code, market, name, status, updated_at
            ) VALUES (?, '920066.BJ', '920066', 'BJ', '北交样本', 'pending', 'now')
            """,
            (run_id,),
        )
        conn.execute("DELETE FROM market_scan_run WHERE id = ?", (run_id,))
        self.assertEqual(conn.execute("SELECT COUNT(*) FROM market_scan_result").fetchone()[0], 0)

    def test_legacy_market_scan_tags_are_backfilled_once_into_structured_provenance(self) -> None:
        conn = sqlite3.connect(":memory:")
        conn.row_factory = sqlite3.Row
        self.addCleanup(conn.close)
        initialize_schema(conn)
        run_id = conn.execute(
            """
            INSERT INTO market_scan_run (
                status, trigger, rule_version, as_of, data_date, scope, created_at, updated_at
            ) VALUES ('degraded', 'manual', 'v1', '2026-07-17 16:30:00', '2026-07-17', 'test', 'now', 'now')
            """
        ).lastrowid
        conn.execute(
            """
            INSERT INTO market_scan_result (
                run_id, symbol, code, market, name, status, tags_json, list_date, updated_at
            ) VALUES (?, '600519.SH', '600519', 'SH', '贵州茅台', 'success',
                      '["兜底行情","兜底K线","上市日期未知"]', NULL, 'now')
            """,
            (run_id,),
        )
        conn.execute(
            "DELETE FROM schema_migration WHERE name = '20260719_market_scan_structured_degradation'"
        )

        apply_compat_migrations(conn)
        row = conn.execute(
            """
            SELECT quote_fallback_used, kline_fallback_used, metadata_degraded
            FROM market_scan_result WHERE run_id = ?
            """,
            (run_id,),
        ).fetchone()

        self.assertEqual(tuple(row), (1, 1, 1))
        self.assertEqual(
            conn.execute(
                "SELECT COUNT(*) FROM schema_migration WHERE name = '20260719_market_scan_structured_degradation'"
            ).fetchone()[0],
            1,
        )

    def test_initialize_schema_adds_fallback_provenance_to_legacy_market_data(self) -> None:
        conn = sqlite3.connect(":memory:")
        conn.row_factory = sqlite3.Row
        self.addCleanup(conn.close)
        conn.executescript(
            """
            CREATE TABLE quote_snapshot (
                symbol TEXT PRIMARY KEY, code TEXT NOT NULL, market TEXT NOT NULL,
                name TEXT NOT NULL, price REAL NOT NULL, prev_close REAL NOT NULL,
                open REAL NOT NULL, high REAL NOT NULL, low REAL NOT NULL,
                volume REAL NOT NULL, amount REAL NOT NULL, change REAL NOT NULL,
                change_pct REAL NOT NULL, turnover_rate REAL, pe REAL, pb REAL,
                market_cap REAL, quote_timestamp TEXT NOT NULL, source TEXT NOT NULL,
                fetched_at TEXT NOT NULL
            );
            INSERT INTO quote_snapshot VALUES (
                '600519.SH', '600519', 'SH', 'legacy', 100, 99, 99, 101, 98,
                1000, 100000, 1, 1.01, 2, 20, 5, 1000000,
                '2026-07-18 15:00:00', 'legacy', '2026-07-18 15:00:01'
            );

            CREATE TABLE quote_history (
                id INTEGER PRIMARY KEY AUTOINCREMENT, symbol TEXT NOT NULL,
                code TEXT NOT NULL, market TEXT NOT NULL, name TEXT NOT NULL,
                price REAL NOT NULL, change_pct REAL NOT NULL, pe REAL, pb REAL,
                market_cap REAL, source TEXT NOT NULL, quote_timestamp TEXT NOT NULL,
                trade_date TEXT NOT NULL CHECK (length(trim(trade_date)) > 0),
                fetched_at TEXT NOT NULL
            );
            INSERT INTO quote_history (
                symbol, code, market, name, price, change_pct, pe, pb, market_cap,
                source, quote_timestamp, trade_date, fetched_at
            ) VALUES (
                '600519.SH', '600519', 'SH', 'legacy', 100, 1.01, 20, 5,
                1000000, 'legacy', '2026-07-18 15:00:00', '2026-07-18',
                '2026-07-18 15:00:01'
            );

            CREATE TABLE kline_daily (
                symbol TEXT NOT NULL,
                adjustment_mode TEXT NOT NULL DEFAULT 'unknown'
                    CHECK (adjustment_mode IN ('qfq', 'hfq', 'none', 'unknown')),
                date TEXT NOT NULL, open REAL NOT NULL, close REAL NOT NULL,
                high REAL NOT NULL, low REAL NOT NULL, volume REAL NOT NULL,
                as_of TEXT, data_version TEXT NOT NULL DEFAULT 'legacy',
                contract_version TEXT NOT NULL DEFAULT 'legacy', source TEXT NOT NULL,
                fetched_at TEXT NOT NULL,
                PRIMARY KEY (symbol, adjustment_mode, date)
            );
            INSERT INTO kline_daily VALUES (
                '600519.SH', 'qfq', '2026-07-18', 99, 100, 101, 98, 1000,
                '2026-07-18', 'daily-version-1', 'daily-kline.v1', 'legacy',
                '2026-07-18 15:00:01'
            );

            CREATE TABLE kline_minute (
                symbol TEXT NOT NULL, interval TEXT NOT NULL, timestamp TEXT NOT NULL,
                open REAL NOT NULL, close REAL NOT NULL, high REAL NOT NULL,
                low REAL NOT NULL, volume REAL NOT NULL, amount REAL,
                turnover_rate REAL, source TEXT NOT NULL, fetched_at TEXT NOT NULL,
                PRIMARY KEY (symbol, interval, timestamp)
            );
            INSERT INTO kline_minute VALUES (
                '600519.SH', '5m', '2026-07-18 14:55:00', 99, 100, 101, 98,
                1000, 100000, 1.2, 'legacy', '2026-07-18 15:00:01'
            );
            """
        )

        initialize_schema(conn)
        initialize_schema(conn)

        for table in ("quote_snapshot", "quote_history", "kline_daily", "kline_minute"):
            with self.subTest(table=table):
                self.assertIn("fallback_used", self._column_names(conn, table))
                self.assertTrue(self._column_not_null(conn, table, "fallback_used"))
                self.assertEqual(self._column_default(conn, table, "fallback_used"), "0")
                self.assertIn("check (fallback_used in (0, 1))", self._table_sql(conn, table).lower())
                self.assertEqual(conn.execute(f"SELECT fallback_used FROM {table}").fetchone()[0], 0)
                with self.assertRaises(sqlite3.IntegrityError):
                    conn.execute(f"UPDATE {table} SET fallback_used = 2")

    def test_initialize_schema_upgrades_legacy_quote_history_idempotently(self) -> None:
        conn = sqlite3.connect(":memory:")
        conn.row_factory = sqlite3.Row
        self.addCleanup(conn.close)
        conn.executescript(
            """
            CREATE TABLE quote_history (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                symbol TEXT NOT NULL,
                code TEXT NOT NULL,
                market TEXT NOT NULL,
                name TEXT NOT NULL,
                price REAL NOT NULL,
                change_pct REAL NOT NULL,
                pe REAL,
                pb REAL,
                market_cap REAL,
                source TEXT NOT NULL,
                quote_timestamp TEXT NOT NULL,
                fetched_at TEXT NOT NULL
            );

            INSERT INTO quote_history (
                symbol, code, market, name, price, change_pct, pe, pb,
                market_cap, source, quote_timestamp, fetched_at
            ) VALUES
                (
                    '600519.SH', '600519', 'SH', 'Kweichow Moutai', 1418.88, 1.2,
                    22.5, 7.1, 1780000000000, 'legacy',
                    '2026-07-09 15:00:00', '2026-07-09 15:00:01'
                ),
                (
                    '600519.SH', '600519', 'SH', 'Kweichow Moutai', 1400.00, 0.2,
                    21.5, 7.0, 1770000000000, 'legacy-late-fetch',
                    '2026-07-09 14:00:00', '2026-07-09 16:00:00'
                ),
                (
                    '600519.SH', '600519', 'SH', 'Kweichow Moutai', 1420.00, 1.3,
                    22.6, 7.2, 1790000000000, 'legacy-latest-id',
                    '2026-07-09 15:00:00', '2026-07-09 15:00:02'
                ),
                (
                    '600519.SH', '600519', 'SH', 'Kweichow Moutai', 1425.00, 1.5,
                    22.8, 7.3, 1800000000000, 'legacy-next-day',
                    '2026/07/10 10:00:00', '2026/07/10 10:00:01'
                ),
                (
                    '600519.SH', '600519', 'SH', 'Kweichow Moutai', 1405.00, 0.4,
                    21.8, 7.0, 1775000000000, 'legacy-slash-stale',
                    '2026/07/09 14:30:00', '2026/07/09 16:30:00'
                );
            """
        )

        initialize_schema(conn)
        first_columns = self._column_names(conn, "quote_history")
        first_indexes = self._index_names(conn, "quote_history")

        initialize_schema(conn)
        second_columns = self._column_names(conn, "quote_history")
        second_indexes = self._index_names(conn, "quote_history")

        self.assertIn("trade_date", first_columns)
        self.assertTrue(self._column_not_null(conn, "quote_history", "trade_date"))
        self.assertIsNone(self._column_default(conn, "quote_history", "trade_date"))
        self.assertEqual(second_columns, first_columns)
        self.assertEqual(first_indexes, {QUOTE_HISTORY_UNIQUE_INDEX})
        self.assertTrue(
            {
                "idx_quote_history_symbol_time",
                "idx_quote_history_symbol_trade_date_time",
                "idx_quote_history_symbol_trade_latest",
            }.isdisjoint(first_indexes)
        )
        self.assertEqual(second_indexes, first_indexes)
        self.assertEqual(
            self._index_columns(conn, QUOTE_HISTORY_UNIQUE_INDEX),
            ["symbol", "trade_date"],
        )
        self.assertTrue(self._index_is_unique(conn, "quote_history", QUOTE_HISTORY_UNIQUE_INDEX))
        self.assertEqual(
            [
                (row["id"], row["price"], row["quote_timestamp"], row["trade_date"])
                for row in conn.execute("SELECT id, price, quote_timestamp, trade_date FROM quote_history ORDER BY trade_date")
            ],
            [
                (3, 1420.0, "2026-07-09 15:00:00", "2026-07-09"),
                (4, 1425.0, "2026/07/10 10:00:00", "2026-07-10"),
            ],
        )
        self.assertEqual(conn.execute("SELECT COUNT(*) FROM quote_history").fetchone()[0], 2)
        self.assertEqual(
            conn.execute(
                "SELECT COUNT(*) FROM schema_migration WHERE name = ?",
                ("20260612_quote_history_trade_date",),
            ).fetchone()[0],
            1,
        )
        self.assertEqual(
            conn.execute(
                "SELECT COUNT(*) FROM schema_migration WHERE name = ?",
                ("20260714_quote_history_daily_snapshot",),
            ).fetchone()[0],
            1,
        )

        with self.assertRaises(sqlite3.IntegrityError):
            conn.execute(
                """
                INSERT INTO quote_history (
                    symbol, code, market, name, price, change_pct, pe, pb,
                    market_cap, source, quote_timestamp, trade_date, fetched_at
                ) VALUES (
                    '600519.SH', '600519', 'SH', 'duplicate', 1430, 1.8, 23, 7.4,
                    1810000000000, 'duplicate', '2026-07-09 15:01:00', '2026-07-09',
                    '2026-07-09 15:01:01'
                )
                """
            )

        with self.assertRaises(sqlite3.IntegrityError):
            conn.execute(
                """
                INSERT INTO quote_history (
                    symbol, code, market, name, price, change_pct, pe, pb,
                    market_cap, source, quote_timestamp, trade_date, fetched_at
                ) VALUES (
                    '000001.SZ', '000001', 'SZ', 'null-date', 10, 0.1, NULL, NULL,
                    NULL, 'invalid', '2026-07-11 15:00:00', NULL,
                    '2026-07-11 15:00:01'
                )
                """
            )

        with self.assertRaises(sqlite3.IntegrityError):
            conn.execute(
                """
                INSERT INTO quote_history (
                    symbol, code, market, name, price, change_pct, pe, pb,
                    market_cap, source, quote_timestamp, trade_date, fetched_at
                ) VALUES (
                    '000001.SZ', '000001', 'SZ', 'empty-date', 10, 0.1, NULL, NULL,
                    NULL, 'invalid', '2026-07-11 15:00:00', '',
                    '2026-07-11 15:00:01'
                )
                """
            )

    def test_nullable_trade_dates_are_rebuilt_cleaned_and_deduplicated(self) -> None:
        conn = sqlite3.connect(":memory:")
        conn.row_factory = sqlite3.Row
        self.addCleanup(conn.close)
        conn.executescript(
            f"""
            CREATE TABLE quote_history (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                symbol TEXT NOT NULL,
                code TEXT NOT NULL,
                market TEXT NOT NULL,
                name TEXT NOT NULL,
                price REAL NOT NULL,
                change_pct REAL NOT NULL,
                pe REAL,
                pb REAL,
                market_cap REAL,
                source TEXT NOT NULL,
                quote_timestamp TEXT NOT NULL,
                trade_date TEXT,
                fetched_at TEXT NOT NULL
            );
            CREATE UNIQUE INDEX {QUOTE_HISTORY_UNIQUE_INDEX}
                ON quote_history(symbol, trade_date);
            CREATE TABLE schema_migration (
                name TEXT PRIMARY KEY,
                applied_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
            );
            INSERT INTO schema_migration (name) VALUES
                ('20260612_quote_history_trade_date'),
                ('20260714_quote_history_normalize_trade_date'),
                ('20260714_quote_history_daily_snapshot');
            """
        )
        conn.executemany(
            """
            INSERT INTO quote_history (
                symbol, code, market, name, price, change_pct, pe, pb,
                market_cap, source, quote_timestamp, trade_date, fetched_at
            ) VALUES (?, ?, ?, ?, ?, 0.1, NULL, NULL, NULL, ?, ?, ?, ?)
            """,
            [
                (
                    "600519.SH",
                    "600519",
                    "SH",
                    "older-null",
                    1400,
                    "legacy",
                    "2026/07/11 14:00:00",
                    None,
                    "2026/07/11 14:00:01",
                ),
                (
                    "600519.SH",
                    "600519",
                    "SH",
                    "newer-empty",
                    1410,
                    "legacy",
                    "2026-07-11 15:00:00",
                    "",
                    "2026-07-11 15:00:01",
                ),
                (
                    "600519.SH",
                    "600519",
                    "SH",
                    "valid-slash-date",
                    1420,
                    "legacy",
                    "2026-07-12 15:00:00",
                    "2026/07/12",
                    "2026-07-12 15:00:01",
                ),
                (
                    "000001.SZ",
                    "000001",
                    "SZ",
                    "fetched-fallback",
                    10,
                    "legacy",
                    "",
                    None,
                    "2026/07/13 15:00:01",
                ),
                ("000002.SZ", "000002", "SZ", "unrecoverable-empty", 11, "legacy", "", None, ""),
                ("000003.SZ", "000003", "SZ", "unrecoverable-invalid", 12, "legacy", "bad", "bad", "bad"),
                (
                    "000004.SZ",
                    "000004",
                    "SZ",
                    "timestamp-fallback",
                    13,
                    "legacy",
                    "2026/07/14 15:00:00",
                    "not-a-date",
                    "",
                ),
            ],
        )

        initialize_schema(conn)
        first_rows = [tuple(row) for row in conn.execute("SELECT id, symbol, name, trade_date FROM quote_history ORDER BY id")]
        first_schema_version = conn.execute("PRAGMA schema_version").fetchone()[0]

        initialize_schema(conn)

        self.assertEqual(
            first_rows,
            [
                (2, "600519.SH", "newer-empty", "2026-07-11"),
                (3, "600519.SH", "valid-slash-date", "2026-07-12"),
                (4, "000001.SZ", "fetched-fallback", "2026-07-13"),
                (7, "000004.SZ", "timestamp-fallback", "2026-07-14"),
            ],
        )
        self.assertEqual(
            [tuple(row) for row in conn.execute("SELECT id, symbol, name, trade_date FROM quote_history ORDER BY id")],
            first_rows,
        )
        self.assertEqual(conn.execute("PRAGMA schema_version").fetchone()[0], first_schema_version)
        self.assertTrue(self._column_not_null(conn, "quote_history", "trade_date"))
        self.assertIsNone(self._column_default(conn, "quote_history", "trade_date"))
        self.assertIn("check (length(trim(trade_date)) > 0)", self._table_sql(conn, "quote_history").lower())
        self.assertEqual(
            conn.execute(
                "SELECT COUNT(*) FROM schema_migration WHERE name = ?",
                (QUOTE_HISTORY_CONTRACT_MIGRATION,),
            ).fetchone()[0],
            1,
        )
        self.assertTrue(self._index_is_unique(conn, "quote_history", QUOTE_HISTORY_UNIQUE_INDEX))
        self.assertFalse(self._table_exists(conn, "quote_history__compat_rebuild"))

    def test_compat_migration_rolls_back_as_one_transaction(self) -> None:
        conn = sqlite3.connect(":memory:")
        conn.row_factory = sqlite3.Row
        self.addCleanup(conn.close)
        conn.executescript(
            """
            CREATE TABLE quote_history (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                symbol TEXT NOT NULL,
                quote_timestamp TEXT NOT NULL,
                fetched_at TEXT NOT NULL
            );
            INSERT INTO quote_history (symbol, quote_timestamp, fetched_at)
            VALUES ('600519.SH', '2026-07-09 15:00:00', '2026-07-09 15:00:01');
            """
        )

        with patch("app.db.schema_migrations._ensure_unique_index", side_effect=RuntimeError("index failed")):
            with self.assertRaisesRegex(RuntimeError, "index failed"):
                initialize_schema(conn)

        self.assertNotIn("trade_date", self._column_names(conn, "quote_history"))
        self.assertEqual(conn.execute("SELECT COUNT(*) FROM schema_migration").fetchone()[0], 0)

    def test_quote_history_rebuild_rolls_back_if_unique_index_creation_fails(self) -> None:
        conn = sqlite3.connect(":memory:")
        conn.row_factory = sqlite3.Row
        self.addCleanup(conn.close)
        conn.executescript(
            f"""
            CREATE TABLE quote_history (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                symbol TEXT NOT NULL,
                code TEXT NOT NULL,
                market TEXT NOT NULL,
                name TEXT NOT NULL,
                price REAL NOT NULL,
                change_pct REAL NOT NULL,
                pe REAL,
                pb REAL,
                market_cap REAL,
                source TEXT NOT NULL,
                quote_timestamp TEXT NOT NULL,
                trade_date TEXT,
                fetched_at TEXT NOT NULL
            );
            CREATE UNIQUE INDEX {QUOTE_HISTORY_UNIQUE_INDEX}
                ON quote_history(symbol, trade_date);
            INSERT INTO quote_history (
                symbol, code, market, name, price, change_pct, pe, pb,
                market_cap, source, quote_timestamp, trade_date, fetched_at
            ) VALUES (
                '600519.SH', '600519', 'SH', 'legacy', 1400, 0.1, NULL, NULL,
                NULL, 'legacy', '2026-07-09 15:00:00', NULL, '2026-07-09 15:00:01'
            );
            """
        )
        original_table_sql = self._table_sql(conn, "quote_history")

        with patch("app.db.schema_migrations._ensure_unique_index", side_effect=RuntimeError("index failed")):
            with self.assertRaisesRegex(RuntimeError, "index failed"):
                initialize_schema(conn)

        self.assertEqual(self._table_sql(conn, "quote_history"), original_table_sql)
        self.assertFalse(self._column_not_null(conn, "quote_history", "trade_date"))
        self.assertIsNone(conn.execute("SELECT trade_date FROM quote_history").fetchone()["trade_date"])
        self.assertTrue(self._index_is_unique(conn, "quote_history", QUOTE_HISTORY_UNIQUE_INDEX))
        self.assertFalse(self._table_exists(conn, "quote_history__compat_rebuild"))
        self.assertEqual(conn.execute("SELECT COUNT(*) FROM schema_migration").fetchone()[0], 0)

    def test_legacy_advice_history_rows_keep_legacy_contract_defaults(self) -> None:
        conn = sqlite3.connect(":memory:")
        conn.row_factory = sqlite3.Row
        self.addCleanup(conn.close)
        conn.executescript(
            """
            CREATE TABLE advice_history (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                symbol TEXT NOT NULL,
                code TEXT NOT NULL,
                market TEXT NOT NULL,
                name TEXT NOT NULL,
                action TEXT NOT NULL,
                confidence INTEGER NOT NULL,
                trend_score INTEGER NOT NULL,
                trend_label TEXT NOT NULL,
                risk_level TEXT NOT NULL,
                price REAL NOT NULL,
                change_pct REAL NOT NULL,
                support REAL NOT NULL,
                resistance REAL NOT NULL,
                data_quality_score INTEGER NOT NULL,
                data_quality_level TEXT NOT NULL,
                reason TEXT NOT NULL,
                summary TEXT NOT NULL,
                created_at TEXT NOT NULL
            );
            INSERT INTO advice_history (
                symbol, code, market, name, action, confidence, trend_score,
                trend_label, risk_level, price, change_pct, support, resistance,
                data_quality_score, data_quality_level, reason, summary, created_at
            ) VALUES (
                '600519.SH', '600519', 'SH', 'legacy', '观察', 60, 65,
                '偏强', '中等', 1418.88, 1.2, 1400.01, 1450.01,
                90, '良好', 'legacy reason', 'legacy summary', '2026-07-15 10:00:00'
            );
            """
        )

        initialize_schema(conn)
        initialize_schema(conn)

        columns = self._column_names(conn, "advice_history")
        row = conn.execute(
            """
            SELECT snapshot_contract_version, conclusion_basis, rule_version,
                   model_version, market_time, data_quality_source
            FROM advice_history
            """
        ).fetchone()
        self.assertTrue(
            {
                "snapshot_contract_version",
                "conclusion_basis",
                "rule_version",
                "model_version",
                "market_time",
                "data_quality_source",
            }.issubset(columns)
        )
        self.assertEqual(
            tuple(row),
            ("legacy", "legacy_unknown", "unknown", "unknown", None, None),
        )

    def test_concurrent_processes_apply_quote_history_migration_once(self) -> None:
        with TemporaryDirectory() as tmpdir:
            path = Path(tmpdir) / "legacy.sqlite3"
            conn = sqlite3.connect(path)
            conn.executescript(
                """
                CREATE TABLE quote_history (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    symbol TEXT NOT NULL,
                    code TEXT NOT NULL,
                    market TEXT NOT NULL,
                    name TEXT NOT NULL,
                    price REAL NOT NULL,
                    change_pct REAL NOT NULL,
                    source TEXT NOT NULL,
                    quote_timestamp TEXT NOT NULL,
                    fetched_at TEXT NOT NULL
                );
                """
            )
            conn.executemany(
                """
                INSERT INTO quote_history (
                    symbol, code, market, name, price, change_pct, source, quote_timestamp, fetched_at
                ) VALUES (?, '600519', 'SH', 'Moutai', ?, 1.0, 'legacy', ?, ?)
                """,
                [
                    (
                        "600519.SH",
                        float(index),
                        "2026-07-09 15:00:00",
                        f"2026-07-09 15:{index % 60:02d}:01",
                    )
                    for index in range(5_000)
                ],
            )
            conn.commit()
            conn.close()

            script = """
import sqlite3
import sys
from app.db.schema import initialize_schema

conn = sqlite3.connect(sys.argv[1], timeout=15)
conn.row_factory = sqlite3.Row
conn.execute('PRAGMA busy_timeout = 15000')
initialize_schema(conn)
conn.close()
"""
            processes = [
                subprocess.Popen(
                    [sys.executable, "-c", script, str(path)],
                    cwd=Path(__file__).resolve().parents[1],
                    stdout=subprocess.PIPE,
                    stderr=subprocess.PIPE,
                    text=True,
                )
                for _ in range(2)
            ]
            results = [process.communicate(timeout=30) for process in processes]

            for process, (_stdout, stderr) in zip(processes, results, strict=True):
                self.assertEqual(process.returncode, 0, stderr)

            conn = sqlite3.connect(path)
            conn.row_factory = sqlite3.Row
            self.addCleanup(conn.close)
            row = conn.execute("SELECT id, price FROM quote_history").fetchone()
            migration_count = conn.execute(
                "SELECT COUNT(*) FROM schema_migration WHERE name = ?",
                ("20260714_quote_history_daily_snapshot",),
            ).fetchone()[0]

            self.assertEqual((row["id"], row["price"]), (5_000, 4_999.0))
            self.assertEqual(migration_count, 1)
            self.assertTrue(self._index_is_unique(conn, "quote_history", QUOTE_HISTORY_UNIQUE_INDEX))

    @staticmethod
    def _column_names(conn: sqlite3.Connection, table: str) -> set[str]:
        return {row["name"] for row in conn.execute(f"PRAGMA table_info({table})")}

    @staticmethod
    def _column_not_null(conn: sqlite3.Connection, table: str, column: str) -> bool:
        return bool(next(row["notnull"] for row in conn.execute(f"PRAGMA table_info({table})") if row["name"] == column))

    @staticmethod
    def _column_default(conn: sqlite3.Connection, table: str, column: str):
        return next(row["dflt_value"] for row in conn.execute(f"PRAGMA table_info({table})") if row["name"] == column)

    @staticmethod
    def _index_names(conn: sqlite3.Connection, table: str) -> set[str]:
        return {row["name"] for row in conn.execute(f"PRAGMA index_list({table})")}

    @staticmethod
    def _index_columns(conn: sqlite3.Connection, index: str) -> list[str]:
        return [row["name"] for row in conn.execute(f"PRAGMA index_info({index})")]

    @staticmethod
    def _index_is_unique(conn: sqlite3.Connection, table: str, index: str) -> bool:
        return any(row["name"] == index and bool(row["unique"]) for row in conn.execute(f"PRAGMA index_list({table})"))

    @staticmethod
    def _table_sql(conn: sqlite3.Connection, table: str) -> str:
        return conn.execute(
            "SELECT sql FROM sqlite_master WHERE type = 'table' AND name = ?",
            (table,),
        ).fetchone()["sql"]

    @staticmethod
    def _table_exists(conn: sqlite3.Connection, table: str) -> bool:
        return (
            conn.execute(
                "SELECT 1 FROM sqlite_master WHERE type = 'table' AND name = ?",
                (table,),
            ).fetchone()
            is not None
        )


if __name__ == "__main__":
    unittest.main()
