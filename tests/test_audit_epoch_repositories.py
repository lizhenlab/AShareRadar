from __future__ import annotations

from pathlib import Path
import sqlite3
from tempfile import TemporaryDirectory
from unittest.mock import patch

from app.services.cache import SQLiteCache
from tests.factories import make_quote


def test_older_audit_microsecond_does_not_overwrite_newer_quote_fetch() -> None:
    with TemporaryDirectory() as tmpdir:
        path = Path(tmpdir) / "cache.sqlite3"
        cache = SQLiteCache(path)
        quote_timestamp = "2026-07-24 14:00:00"

        with patch(
            "app.repositories.market_quotes.now_text",
            side_effect=[
                "2026-07-24T06:00:00.900000Z",
                "2026-07-24T06:00:00.100000Z",
            ],
        ):
            cache.save_quotes([make_quote(timestamp=quote_timestamp, price=1300.0)])
            cache.save_quotes([make_quote(timestamp=quote_timestamp, price=1299.0)])

        with sqlite3.connect(path) as conn:
            snapshot = conn.execute(
                "SELECT price, fetched_at FROM quote_snapshot WHERE symbol = ?",
                ("600519.SH",),
            ).fetchone()
            history = conn.execute(
                "SELECT price, fetched_at FROM quote_history WHERE symbol = ?",
                ("600519.SH",),
            ).fetchone()

    assert snapshot == (1300.0, "2026-07-24T06:00:00.900000Z")
    assert history == (1300.0, "2026-07-24T06:00:00.900000Z")
