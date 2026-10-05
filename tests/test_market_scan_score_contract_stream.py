from __future__ import annotations

from collections.abc import Iterator
from contextlib import contextmanager
import json
from pathlib import Path
import sqlite3

import pytest

from app.artifacts.io import decode_json_bytes
from app.db import market_scan_action_source
from app.db.connection import SQLiteConnectionFactory
from app.db.market_scan_integrity import MarketScanSnapshotSealError, market_scan_snapshot_digest
from app.repositories.market_scan_score_diagnostics import (
    ProductionScoreContractCollector,
    read_production_score_contract,
)
from app.services.cache import SQLiteCache
from tests.test_market_scan_skip_contract import _valid_action_source_run
from tests.test_strategy_execution import _disable_market_scan_immutability, _environment


def _metrics(rule: object = "full-market-score-v5", score_hash: object = "a" * 64) -> dict[str, object]:
    return {"score_details": {"score_spec": {"rule_version": rule}, "score_spec_hash": score_hash}}


def _assert_sql_equivalent(rows: list[tuple[str, object]], expected_count: int) -> None:
    collector = ProductionScoreContractCollector()
    with sqlite3.connect(":memory:") as conn:
        conn.row_factory = sqlite3.Row
        conn.execute("CREATE TABLE market_scan_result(run_id INTEGER, status TEXT, metrics_json TEXT)")
        for status, metrics in rows:
            encoded = json.dumps(metrics, ensure_ascii=False)
            conn.execute("INSERT INTO market_scan_result VALUES (1, ?, ?)", (status, encoded))
            collector.observe({"status": status, "metrics_json": decode_json_bytes(encoded.encode("utf-8"))})
        expected = read_production_score_contract(conn, 1, expected_count=expected_count)
    assert collector.contract(expected_count=expected_count) == expected


@pytest.mark.parametrize("field", ["rule", "hash"])
@pytest.mark.parametrize("value", [None, True, False, 0, 1.5, [], {}, ["full-market-score-v5"], "", " a", "a ", "A" * 64])
def test_stream_contract_matches_sql_for_inner_field_types(field: str, value: object) -> None:
    metrics = _metrics(rule=value) if field == "rule" else _metrics(score_hash=value)
    _assert_sql_equivalent([("success", metrics)], 1)


@pytest.mark.parametrize(
    "metrics",
    [None, [], True, 12, "payload", {}, {"score_details": None}, {"score_details": []},
     {"score_details": {"score_spec": []}}, {"score_details.score_spec.rule_version": "full-market-score-v5"},
     {"score_details": {"score_spec": {"rule_version": "full-market-score-v5"}}},
     {"score_details": {"score_spec_hash": "a" * 64}},
     {"score_details": [{"score_spec": {"rule_version": "full-market-score-v5"}, "score_spec_hash": "a" * 64}]}],
)
def test_stream_contract_matches_sql_for_missing_or_nonobject_paths(metrics: object) -> None:
    _assert_sql_equivalent([("success", metrics)], 1)


@pytest.mark.parametrize("expected_count", [-1, 0, 1, 2, 3])
def test_stream_contract_matches_sql_for_count_and_complete_coverage(expected_count: int) -> None:
    _assert_sql_equivalent([("success", _metrics()), ("success", _metrics())], expected_count)
    _assert_sql_equivalent([("success", _metrics()), ("success", {})], expected_count)
    _assert_sql_equivalent([], expected_count)


@pytest.mark.parametrize(
    "first,second",
    [(_metrics(), _metrics()), (_metrics("旧评分规范"), _metrics("旧评分规范")),
     (_metrics("full-market-score-v4"), _metrics()), (_metrics(), _metrics(score_hash="b" * 64)),
     (_metrics(None), _metrics(score_hash=None)), (_metrics(score_hash="x" * 64), _metrics(score_hash="x" * 64))],
)
def test_stream_contract_matches_sql_for_independent_uniqueness(first: object, second: object) -> None:
    _assert_sql_equivalent([("success", first), ("success", second)], 2)


def test_stream_contract_ignores_other_status_rows() -> None:
    _assert_sql_equivalent(
        [("success", _metrics()), ("missing", {}), ("skipped", _metrics("other", "b" * 64)),
         ("pending", None), ("SUCCESS", _metrics("other", "c" * 64))],
        1,
    )


def test_verified_read_projects_contract_without_second_json_sql_scan(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    _repo, settings, run_id, _skip, _diagnostics = _valid_action_source_run(tmp_path)
    cache = SQLiteCache(settings=settings)
    with sqlite3.connect(cache.path) as conn:
        conn.row_factory = sqlite3.Row
        expected = read_production_score_contract(conn, run_id, expected_count=100)
    assert expected is not None
    original = SQLiteConnectionFactory.read_snapshot

    def forbid_json_rescan(action: int, _first: str | None, second: str | None, *_rest: object) -> int:
        assert not (action == sqlite3.SQLITE_FUNCTION and second in {"json_type", "json_extract"})
        return sqlite3.SQLITE_OK

    @contextmanager
    def guarded(factory: SQLiteConnectionFactory) -> Iterator[sqlite3.Connection]:
        with original(factory) as conn:
            conn.set_authorizer(forbid_json_rescan)
            yield conn

    monkeypatch.setattr(SQLiteConnectionFactory, "read_snapshot", guarded)
    with cache.verified_market_scan_read(run_id) as verified:
        assert verified.success_score_contract == expected
        assert verified.action_source_digest == verified.snapshot_digest
    with pytest.raises(RuntimeError, match="已关闭"):
        _ = verified.success_score_contract


def test_ineligible_inspection_does_not_publish_collected_contract(tmp_path: Path) -> None:
    _repo, settings, run_id, _skip, _diagnostics = _valid_action_source_run(tmp_path)
    with sqlite3.connect(settings.cache_path) as conn:
        conn.row_factory = sqlite3.Row
        conn.execute("DROP TRIGGER trg_market_scan_rule_contract_immutable_delete")
        conn.execute("DELETE FROM market_scan_rule_contract")
        inspection = market_scan_action_source.inspect_market_scan_action_source(conn, run_id)
    assert inspection.eligible is False
    assert inspection.success_score_contract is None


def test_legacy_backfill_session_does_not_publish_collected_contract(tmp_path: Path) -> None:
    cache, _service, _strategy_id, run_id = _environment(tmp_path)
    with sqlite3.connect(cache.path) as conn:
        conn.row_factory = sqlite3.Row
        _disable_market_scan_immutability(conn)
        conn.execute("UPDATE market_scan_run SET snapshot_seal_origin = 'legacy_backfill' WHERE id = ?", (run_id,))
        digest = market_scan_snapshot_digest(conn, run_id)
        conn.execute("UPDATE market_scan_run SET snapshot_digest = ? WHERE id = ?", (digest, run_id))
    with cache.verified_market_scan_read(run_id) as verified:
        assert verified.snapshot_digest == digest
        assert verified.action_source_digest is None
        assert verified.success_score_contract is None


@pytest.mark.parametrize("corruption", ["digest", "duplicate_json_key", "nonfinite_json"])
def test_last_row_corruption_cannot_publish_partial_contract(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, corruption: str,
) -> None:
    _repo, settings, run_id, _skip, _diagnostics = _valid_action_source_run(tmp_path)

    def unexpected_contract(*_args: object, **_kwargs: object) -> None:
        pytest.fail("a failed full snapshot must not publish partial collected evidence")

    monkeypatch.setattr(ProductionScoreContractCollector, "contract", unexpected_contract)
    with sqlite3.connect(settings.cache_path) as conn:
        conn.row_factory = sqlite3.Row
        _disable_market_scan_immutability(conn)
        last = conn.execute(
            "SELECT symbol FROM market_scan_result WHERE run_id = ? ORDER BY symbol DESC LIMIT 1", (run_id,),
        ).fetchone()[0]
        if corruption == "digest":
            conn.execute("UPDATE market_scan_result SET amount = amount + 1 WHERE run_id = ? AND symbol = ?", (run_id, last))
        else:
            raw = '{"score_details":{},"score_details":{}}' if corruption == "duplicate_json_key" else '{"score_details":NaN}'
            conn.execute("UPDATE market_scan_result SET metrics_json = ? WHERE run_id = ? AND symbol = ?", (raw, run_id, last))
        with pytest.raises(MarketScanSnapshotSealError):
            market_scan_action_source.inspect_market_scan_action_source(conn, run_id)


def test_collector_error_aborts_inspection_before_contract_publication(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    _repo, settings, run_id, _skip, _diagnostics = _valid_action_source_run(tmp_path)

    def broken_observer(*_args: object) -> None:
        raise RuntimeError("collector interrupted")

    monkeypatch.setattr(ProductionScoreContractCollector, "observe", broken_observer)
    with sqlite3.connect(settings.cache_path) as conn:
        conn.row_factory = sqlite3.Row
        with pytest.raises(RuntimeError, match="collector interrupted"):
            market_scan_action_source.inspect_market_scan_action_source(conn, run_id)
