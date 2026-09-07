from __future__ import annotations

from pathlib import Path
import sqlite3

import pytest

from app.db.market_scan_integrity import create_market_scan_immutability_triggers, delete_verified_market_scan_snapshots
from app.services.market_scan_research_inputs import read_research_session, readonly_research_connection
from tests.test_market_scan_research_inputs import _initialize, _seed_run


def _retained_snapshot(tmp_path: Path) -> tuple[Path, int]:
    path = tmp_path / "research.sqlite3"
    _initialize(path)
    run_id = _seed_run(path, mode="official", rule_version="v5", quote_date="2026-08-03", ranks=("600001.SH",))
    with sqlite3.connect(path) as conn:
        conn.execute("PRAGMA journal_mode=WAL")
        create_market_scan_immutability_triggers(conn)
    return path, run_id


def _clean_before_member_export(reader: sqlite3.Connection, path: Path, run_id: int) -> list[int]:
    deleted: list[int] = []

    def trace(statement: str) -> None:
        if "ORDER BY rank IS NULL, rank, symbol" in statement and not deleted:
            with sqlite3.connect(path) as writer:
                writer.execute("BEGIN IMMEDIATE")
                deleted.append(delete_verified_market_scan_snapshots(writer, (run_id,)))

    reader.set_trace_callback(trace)
    return deleted


def test_export_rejects_autocommit_before_retention_can_split_snapshot(tmp_path: Path) -> None:
    path, run_id = _retained_snapshot(tmp_path)
    with sqlite3.connect(path) as reader:
        reader.row_factory = sqlite3.Row
        reader.execute("PRAGMA query_only=ON")
        deleted = _clean_before_member_export(reader, path, run_id)
        with pytest.raises(ValueError, match="explicit read transaction"):
            read_research_session(reader, run_id)
        assert deleted == []
        assert reader.in_transaction is False


def test_explicit_transaction_keeps_verified_members_during_retention(tmp_path: Path) -> None:
    path, run_id = _retained_snapshot(tmp_path)
    with readonly_research_connection(path) as reader:
        deleted = _clean_before_member_export(reader, path, run_id)
        snapshot = read_research_session(reader, run_id)
        assert deleted == [1], "retention must commit after run export and before member export"
        assert snapshot["run"]["total_count"] == 1
        assert [item["symbol"] for item in snapshot["items"]] == ["600001.SH"]
        assert reader.in_transaction is True
        assert reader.execute("SELECT COUNT(*) FROM market_scan_run").fetchone()[0] == 1
    with sqlite3.connect(path) as current:
        assert current.execute("SELECT COUNT(*) FROM market_scan_run").fetchone()[0] == 0
