from __future__ import annotations

import sqlite3
import unittest

from app.db.schema import initialize_schema


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
            ) VALUES (
                '600519.SH', '600519', 'SH', 'Kweichow Moutai', 1418.88, 1.2,
                22.5, 7.1, 1780000000000, 'legacy',
                '2026-07-09 15:00:00', '2026-07-09 15:00:01'
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
        self.assertEqual(second_columns, first_columns)
        self.assertTrue(
            {
                "idx_quote_history_symbol_time",
                "idx_quote_history_symbol_trade_date_time",
                "idx_quote_history_symbol_trade_latest",
            }.issubset(first_indexes)
        )
        self.assertEqual(second_indexes, first_indexes)
        self.assertEqual(
            self._index_columns(conn, "idx_quote_history_symbol_trade_latest"),
            ["symbol", "trade_date", "fetched_at", "id"],
        )
        self.assertEqual(
            conn.execute("SELECT trade_date FROM quote_history").fetchone()["trade_date"],
            "2026-07-09",
        )
        self.assertEqual(conn.execute("SELECT COUNT(*) FROM quote_history").fetchone()[0], 1)
        self.assertEqual(
            conn.execute(
                "SELECT COUNT(*) FROM schema_migration WHERE name = ?",
                ("20260612_quote_history_trade_date",),
            ).fetchone()[0],
            1,
        )

    @staticmethod
    def _column_names(conn: sqlite3.Connection, table: str) -> set[str]:
        return {row["name"] for row in conn.execute(f"PRAGMA table_info({table})")}

    @staticmethod
    def _index_names(conn: sqlite3.Connection, table: str) -> set[str]:
        return {row["name"] for row in conn.execute(f"PRAGMA index_list({table})")}

    @staticmethod
    def _index_columns(conn: sqlite3.Connection, index: str) -> list[str]:
        return [row["name"] for row in conn.execute(f"PRAGMA index_info({index})")]


if __name__ == "__main__":
    unittest.main()
