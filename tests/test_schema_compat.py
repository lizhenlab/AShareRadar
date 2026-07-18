from __future__ import annotations

from pathlib import Path
import sqlite3
import subprocess
import sys
from tempfile import TemporaryDirectory
import unittest
from unittest.mock import patch

from app.db.schema import initialize_schema
from app.db.schema_migrations import QUOTE_HISTORY_CONTRACT_MIGRATION, QUOTE_HISTORY_UNIQUE_INDEX


class SchemaCompatibilityTests(unittest.TestCase):
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
                for row in conn.execute(
                    "SELECT id, price, quote_timestamp, trade_date FROM quote_history ORDER BY trade_date"
                )
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
        first_rows = [
            tuple(row)
            for row in conn.execute(
                "SELECT id, symbol, name, trade_date FROM quote_history ORDER BY id"
            )
        ]
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
        return conn.execute(
            "SELECT 1 FROM sqlite_master WHERE type = 'table' AND name = ?",
            (table,),
        ).fetchone() is not None


if __name__ == "__main__":
    unittest.main()
