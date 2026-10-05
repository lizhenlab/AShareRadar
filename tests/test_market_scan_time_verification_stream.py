"""Publication time checks share the mandatory canonical snapshot traversal."""

from __future__ import annotations

from collections.abc import Mapping

import pytest

from app.db.market_scan_integrity import (
    MarketScanSnapshotSealError,
    market_scan_snapshot_digest,
    require_publication_market_scan_snapshot,
    seal_market_scan_snapshot,
    verify_market_scan_snapshot,
)
from tests.test_market_scan_snapshot_integrity import _materialized_v2_snapshot_digest
from tests.test_strategy_execution import _disable_market_scan_immutability, _environment


def _reseal(conn, run_id: int) -> str:
    digest = market_scan_snapshot_digest(conn, run_id)
    conn.execute(
        "UPDATE market_scan_run SET snapshot_digest = ? WHERE id = ?",
        (digest, run_id),
    )
    return digest


def _is_time_scan(sql: str) -> bool:
    return "select symbol, updated_at from market_scan_result" in " ".join(sql.lower().split())


def test_verification_observes_each_canonical_row_once_without_separate_time_scan(tmp_path):
    cache, _, _, run_id = _environment(tmp_path)
    with cache._connect() as conn:  # noqa: SLF001
        expected = _materialized_v2_snapshot_digest(conn, run_id)
        original = conn.execute(
            "SELECT symbol, updated_at FROM market_scan_result WHERE run_id = ? ORDER BY symbol",
            (run_id,),
        ).fetchall()
        statements: list[str] = []
        observed: list[tuple[object, object]] = []
        conn.set_trace_callback(statements.append)
        actual = verify_market_scan_snapshot(
            conn, run_id,
            result_observer=lambda row: observed.append((row["symbol"], row["updated_at"])),
        )
        conn.set_trace_callback(None)
    assert actual == expected
    assert observed == [(row[0], row[1]) for row in original]
    assert not any(_is_time_scan(sql) for sql in statements)
    assert sum("SELECT * FROM market_scan_result" in sql for sql in statements) == 1


@pytest.mark.parametrize("status", ["success", "missing", "skipped"])
@pytest.mark.parametrize("value", ["2099-01-01T00:00:00Z", "invalid-time", ""])
def test_invalid_last_result_time_is_rejected_before_observer_even_with_matching_digest(
    tmp_path, status: str, value: str,
):
    cache, _, _, run_id = _environment(tmp_path)
    with cache._connect() as conn:  # noqa: SLF001
        _disable_market_scan_immutability(conn)
        symbol = conn.execute(
            "SELECT MAX(symbol) FROM market_scan_result WHERE run_id = ?", (run_id,),
        ).fetchone()[0]
        conn.execute(
            "UPDATE market_scan_result SET status = ?, updated_at = ? WHERE run_id = ? AND symbol = ?",
            (status, value, run_id, symbol),
        )
        _reseal(conn, run_id)
        observed: list[object] = []

        def observe(row: Mapping[str, object]) -> None:
            observed.append(row["symbol"])
            # A consumer mutating its canonical dictionary must not bypass time admission.
            if isinstance(row, dict):
                row["updated_at"] = "2000-01-01T00:00:00Z"

        with pytest.raises(MarketScanSnapshotSealError, match="审计时间|晚于批次更新时间"):
            verify_market_scan_snapshot(conn, run_id, result_observer=observe)
        assert len(observed) == 3
        assert symbol not in observed


@pytest.mark.parametrize("value", [
    "2026-08-11T08:00:00.123456Z", "2026-08-11T16:00:00.123456+08:00",
    "2026-08-11 16:00:00.123456", "2026-08-11T08:00:00.123455Z",
])
def test_streamed_time_comparison_preserves_timezone_and_microsecond_contract(tmp_path, value):
    cache, _, _, run_id = _environment(tmp_path)
    with cache._connect() as conn:  # noqa: SLF001
        _disable_market_scan_immutability(conn)
        conn.execute(
            "UPDATE market_scan_run SET finished_at = ?, updated_at = ?, snapshot_sealed_at = ? WHERE id = ?",
            ("2026-08-11T08:00:00Z", "2026-08-11T08:00:00.123456Z", "2026-08-11T08:00:00.123457Z", run_id),
        )
        conn.execute("UPDATE market_scan_result SET updated_at = ? WHERE run_id = ?", (value, run_id))
        expected = _reseal(conn, run_id)
        assert verify_market_scan_snapshot(conn, run_id) == expected
        assert seal_market_scan_snapshot(conn, run_id) == expected


@pytest.mark.parametrize("already_sealed", [False, True])
def test_sealing_retains_early_time_precheck_and_does_not_change_provenance(tmp_path, already_sealed):
    cache, _, _, run_id = _environment(tmp_path)
    with cache._connect() as conn:  # noqa: SLF001
        _disable_market_scan_immutability(conn)
        conn.execute(
            "UPDATE market_scan_result SET updated_at = '2099-01-01T00:00:00Z' WHERE run_id = ?",
            (run_id,),
        )
        if not already_sealed:
            conn.execute(
                "UPDATE market_scan_run SET snapshot_digest = NULL, snapshot_sealed_at = NULL, snapshot_seal_origin = NULL WHERE id = ?",
                (run_id,),
            )
        before = tuple(conn.execute("SELECT * FROM market_scan_run WHERE id = ?", (run_id,)).fetchone())
        statements: list[str] = []
        conn.set_trace_callback(statements.append)
        with pytest.raises(MarketScanSnapshotSealError, match="晚于批次更新时间"):
            seal_market_scan_snapshot(conn, run_id)
        conn.set_trace_callback(None)
        after = tuple(conn.execute("SELECT * FROM market_scan_run WHERE id = ?", (run_id,)).fetchone())
        assert before == after
        assert any(_is_time_scan(sql) for sql in statements)


def test_legacy_backfill_keeps_audit_only_semantics_without_new_time_admission(tmp_path):
    cache, _, _, run_id = _environment(tmp_path)
    with cache._connect() as conn:  # noqa: SLF001
        _disable_market_scan_immutability(conn)
        conn.execute(
            "UPDATE market_scan_run SET snapshot_seal_origin = 'legacy_backfill' WHERE id = ?", (run_id,),
        )
        conn.execute("UPDATE market_scan_result SET updated_at = 'unknown' WHERE run_id = ?", (run_id,))
        expected = _reseal(conn, run_id)
        observed: list[object] = []
        assert verify_market_scan_snapshot(
            conn, run_id, result_observer=lambda row: observed.append(row["updated_at"]),
        ) == expected
        assert observed == ["unknown"] * 4
        with pytest.raises(MarketScanSnapshotSealError, match="原发布快照"):
            require_publication_market_scan_snapshot(conn, run_id)
