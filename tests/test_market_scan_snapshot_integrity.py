from __future__ import annotations

import asyncio
from collections.abc import Callable, Iterator, Mapping
from contextlib import contextmanager
from dataclasses import replace
from datetime import datetime, timedelta
import json
import sqlite3
import threading
from time import perf_counter, process_time

import pytest
from pydantic import ValidationError

from app.artifacts.io import canonical_json_text, sha256_hex
from app.db import market_scan_action_source, market_scan_integrity
from app.db.connection import SQLiteConnectionFactory
from app.db.market_scan_integrity import (
    MarketScanSnapshotSealError,
    create_market_scan_immutability_triggers,
    market_scan_snapshot_digest,
    require_publication_market_scan_snapshot,
    seal_market_scan_snapshot,
    verify_market_scan_snapshot,
)
from app.models.market_scan import (
    MARKET_SCAN_FULL_MARKET_SCOPE,
    MarketScanPublicationDiagnostic,
    MarketScanPublicationDiagnostics,
    MarketScanResultPage,
    MarketScanResultWrite,
    MarketScanRun,
    MarketScanScoreDistribution,
    MarketScanScoreDistributionObservation,
    MarketScanScoreDistributionPolicy,
    MarketScanSeed,
)
from app.models.market_scan_snapshot import (
    FrozenFullMarketSnapshotIntegrityError,
    validate_frozen_full_market_snapshot,
)
from app.models.strategy_execution import StrategyExecutionRequest
from app.repositories import market_scan_verified_read as verified_read_module
from app.repositories.strategy_execution import FrozenMarketScan
from app.repositories.market_scan_verified_read import verified_market_scan_read
from app.services.cache import SQLiteCache
from app.services.market_scan_executable_shadow import MarketScanExecutableShadowService
from app.services.market_scan_manager import market_scan_rule_contract
from app.services.market_scan_query_service import MarketScanQueryService
from app.services.market_scan_research_stores import MarketScanResearchStores
from app.services.market_scan_scoring import FULL_MARKET_SCORE_RULE_VERSION
from tests.test_strategy_execution import _disable_market_scan_immutability, _environment
from tests.test_market_scan_skip_contract import (
    DATA_DATE,
    QUOTE_OBSERVED_AT,
    _score,
    _settings_and_rule,
    _varying_success_case,
)
from tests.market_scan_test_support import (
    _MarketScanHub,
    _scanner,
    action_pass_publication_diagnostics,
)


def _capture_verified_connection(
    monkeypatch: pytest.MonkeyPatch,
) -> list[sqlite3.Connection]:
    captured: list[sqlite3.Connection] = []
    real_read_snapshot = SQLiteConnectionFactory.read_snapshot

    @contextmanager
    def tracked_read_snapshot(
        factory: SQLiteConnectionFactory,
    ) -> Iterator[sqlite3.Connection]:
        with real_read_snapshot(factory) as conn:
            captured.append(conn)
            yield conn

    monkeypatch.setattr(
        verified_read_module.SQLiteConnectionFactory,
        "read_snapshot",
        tracked_read_snapshot,
    )
    return captured


def test_frozen_full_market_snapshot_reconstructs_the_complete_contract(tmp_path) -> None:
    cache, _service, _strategy_id, run_id = _environment(tmp_path)

    frozen = cache.strategy_execution_repo.frozen_scan(
        run_id=run_id,
        data_date=None,
        mode="official",
    )
    integrity = validate_frozen_full_market_snapshot(frozen.run, frozen.items)

    assert integrity.run_id == run_id
    assert integrity.result_count == frozen.run.total_count == 4
    assert integrity.success_count == frozen.run.success_count == 4
    assert integrity.missing_count == integrity.skipped_count == 0
    assert integrity.production_score_rule_version == FULL_MARKET_SCORE_RULE_VERSION
    assert len(integrity.production_score_spec_hash) == 64


def test_sealed_published_snapshot_rejects_every_direct_sql_mutation(tmp_path) -> None:
    cache, _service, _strategy_id, run_id = _environment(tmp_path)
    attacks = (
        ("UPDATE market_scan_run SET message = 'tampered' WHERE id = ?", (run_id,)),
        ("DELETE FROM market_scan_run WHERE id = ?", (run_id,)),
        (
            "UPDATE market_scan_result SET amount = amount + 1 WHERE run_id = ?",
            (run_id,),
        ),
        ("DELETE FROM market_scan_result WHERE run_id = ?", (run_id,)),
        (
            """
            INSERT INTO market_scan_result (
                run_id, symbol, code, market, name, status, updated_at
            ) VALUES (?, '999999.SH', '999999', 'SH', '伪造', 'pending', '2026-08-11')
            """,
            (run_id,),
        ),
    )

    with cache._connect() as conn:  # noqa: SLF001 - raw SQL attack boundary
        for sql, parameters in attacks:
            with pytest.raises(sqlite3.IntegrityError, match="published market_scan"):
                conn.execute(sql, parameters)

        active_id = conn.execute(
            """
            INSERT INTO market_scan_run (
                status, trigger, mode, rule_version, as_of, data_date, quote_date,
                scope, created_at, updated_at
            ) VALUES (
                'queued', 'manual', 'official', 'unpublished-test',
                '2026-08-12 16:00:00', '2026-08-12', '2026-08-12', 'test',
                '2026-08-12', '2026-08-12'
            )
            """
        ).lastrowid
        conn.execute(
            "UPDATE market_scan_run SET message = 'still mutable' WHERE id = ?",
            (active_id,),
        )
        conn.execute("DELETE FROM market_scan_run WHERE id = ?", (active_id,))


@pytest.mark.parametrize(
    "mutation",
    ("truncated", "header_counts", "rank", "score_spec", "scan_rule", "quote_date"),
)
def test_repository_rejects_each_resealed_or_incomplete_snapshot_counterexample(
    tmp_path,
    mutation: str,
) -> None:
    cache, _service, _strategy_id, run_id = _environment(tmp_path)
    with cache._connect() as conn:  # noqa: SLF001 - deterministic integrity mutation
        _disable_market_scan_immutability(conn)
        if mutation == "truncated":
            conn.execute(
                "DELETE FROM market_scan_result WHERE run_id = ? AND rank = 4",
                (run_id,),
            )
        elif mutation == "header_counts":
            conn.execute(
                "UPDATE market_scan_run SET success_count = 3, skipped_count = 1 WHERE id = ?",
                (run_id,),
            )
        elif mutation == "rank":
            conn.execute(
                "UPDATE market_scan_result SET rank = 2 WHERE run_id = ? AND rank = 1",
                (run_id,),
            )
        elif mutation == "score_spec":
            row = conn.execute(
                "SELECT symbol, metrics_json FROM market_scan_result WHERE run_id = ? AND rank = 1",
                (run_id,),
            ).fetchone()
            payload = json.loads(str(row["metrics_json"]))
            payload["score_details"]["score_spec"]["rule_version"] = "tampered-score-v9"
            conn.execute(
                "UPDATE market_scan_result SET metrics_json = ? WHERE run_id = ? AND symbol = ?",
                (json.dumps(payload, ensure_ascii=False), run_id, str(row["symbol"])),
            )
        elif mutation == "scan_rule":
            conn.execute(
                "UPDATE market_scan_run SET rule_version = 'tampered-scan-rule' WHERE id = ?",
                (run_id,),
            )
        else:
            conn.execute(
                "UPDATE market_scan_run SET quote_date = '2026-07-16' WHERE id = ?",
                (run_id,),
            )

    with pytest.raises(FrozenFullMarketSnapshotIntegrityError):
        cache.strategy_execution_repo.frozen_scan(
            run_id=run_id,
            data_date=None,
            mode="official",
        )


def test_strategy_service_revalidates_untrusted_repository_before_build_or_save(
    tmp_path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    cache, service, strategy_id, run_id = _environment(tmp_path)
    frozen = cache.strategy_execution_repo.frozen_scan(
        run_id=run_id,
        data_date=None,
        mode="official",
    )
    corrupt = FrozenMarketScan(run=frozen.run, items=frozen.items[:-1])
    monkeypatch.setattr(service.repository, "frozen_scan", lambda **_kwargs: corrupt)

    def forbidden_build(*_args: object, **_kwargs: object) -> None:
        pytest.fail("portfolio build must not run for an invalid frozen snapshot")

    monkeypatch.setattr("app.services.strategy_execution.build_portfolio_draft", forbidden_build)

    with pytest.raises(FrozenFullMarketSnapshotIntegrityError):
        service.execute(StrategyExecutionRequest(strategy_id=strategy_id))
    with cache._connect() as conn:  # noqa: SLF001 - zero-write integrity assertion
        assert conn.execute("SELECT COUNT(*) FROM strategy_execution").fetchone()[0] == 0


@pytest.mark.parametrize("read_kind", ("detail", "latest", "published", "page"))
def test_public_run_header_reads_fail_closed_after_result_tamper(
    tmp_path,
    read_kind: str,
) -> None:
    cache, _service, _strategy_id, run_id = _environment(tmp_path)
    with cache._connect() as conn:  # noqa: SLF001 - adversarial raw DB mutation
        _disable_market_scan_immutability(conn)
        conn.execute(
            """
            UPDATE market_scan_result SET amount = amount + 1
            WHERE run_id = ? AND rank = 1
            """,
            (run_id,),
        )

    reads = {
        "detail": lambda: cache.market_scan_run(run_id),
        "latest": lambda: cache.latest_market_scan_run(mode="official"),
        "published": lambda: cache.latest_published_market_scan_run(mode="official"),
        "page": lambda: cache.market_scan_runs(
            page=1,
            page_size=20,
            mode="official",
            status="published",
        ),
    }
    with pytest.raises(MarketScanSnapshotSealError):
        reads[read_kind]()


def test_probability_facing_queries_verify_one_full_snapshot_per_request(
    tmp_path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    cache, _service, _strategy_id, run_id = _environment(tmp_path)
    query = MarketScanQueryService(cache, _empty_research_stores())
    real_verify = market_scan_integrity.verify_market_scan_snapshot
    calls: list[int] = []

    def tracked_verify(
        conn: sqlite3.Connection,
        candidate_run_id: int,
        *,
        result_observer: Callable[[Mapping[str, object]], None] | None = None,
    ) -> str:
        calls.append(candidate_run_id)
        return real_verify(
            conn,
            candidate_run_id,
            result_observer=result_observer,
        )

    monkeypatch.setattr(market_scan_integrity, "verify_market_scan_snapshot", tracked_verify)
    monkeypatch.setattr(
        "app.repositories.market_scan_verified_read.verify_market_scan_snapshot",
        lambda *_args, **_kwargs: pytest.fail(
            "publication session must not run a second repository verifier"
        ),
    )
    monkeypatch.setattr(
        "app.db.market_scan_action_source.require_market_scan_action_source",
        lambda *_args, **_kwargs: pytest.fail(
            "request read must consume canonical inspection, not re-run action verifier"
        ),
    )

    assert _query_results(query, run_id).run.id == run_id
    assert calls == [run_id]
    assert query.probability_research(run_id)["status"] == "not_generated"
    assert calls == [run_id, run_id]
    research, probabilities = query.probability_projection(run_id)
    assert research["status"] == "not_generated"
    assert probabilities == {}
    assert calls == [run_id, run_id, run_id]


def test_verified_query_rejects_drop_tamper_recreate_receipt_bypass(tmp_path) -> None:
    cache, _service, _strategy_id, run_id = _environment(tmp_path)
    query = MarketScanQueryService(cache, _empty_research_stores())
    with cache._connect() as conn:  # noqa: SLF001 - adversarial DB mutation
        _disable_market_scan_immutability(conn)
        conn.execute(
            "UPDATE market_scan_result SET amount = amount + 1 WHERE run_id = ? AND rank = 1",
            (run_id,),
        )
        create_market_scan_immutability_triggers(conn)

    with pytest.raises(MarketScanSnapshotSealError, match="摘要不一致"):
        _query_results(query, run_id)


def test_verified_read_session_is_request_local_and_closes_on_error(tmp_path) -> None:
    cache, _service, _strategy_id, run_id = _environment(tmp_path)
    captured = None
    with pytest.raises(RuntimeError, match="fixture failure"):
        with cache.verified_market_scan_read(run_id) as verified:
            captured = verified
            assert not hasattr(verified, "_conn")
            assert verified.run.id == run_id
            cross_thread_errors: list[BaseException] = []

            def cross_thread_read() -> None:
                try:
                    _ = verified.run
                except BaseException as exc:  # noqa: BLE001 - assertion boundary
                    cross_thread_errors.append(exc)

            thread = threading.Thread(target=cross_thread_read)
            thread.start()
            thread.join()
            assert len(cross_thread_errors) == 1
            assert "跨线程" in str(cross_thread_errors[0])
            raise RuntimeError("fixture failure")

    assert captured is not None
    with pytest.raises(RuntimeError, match="已关闭"):
        _ = captured.run
    with pytest.raises(RuntimeError, match="已关闭"):
        captured.results_page(**_result_query())

    with cache.verified_market_scan_read(run_id) as next_request:
        assert next_request is not captured
        assert next_request.run.id == run_id


def test_verified_read_public_issuer_does_not_accept_caller_owned_connections(
    tmp_path,
) -> None:
    cache, _service, _strategy_id, run_id = _environment(tmp_path)
    with cache._connect() as conn:  # noqa: SLF001 - issuer misuse counterexample
        with pytest.raises(TypeError):
            with verified_market_scan_read(conn, run_id):
                pytest.fail("caller-owned connection must not enter the public issuer")

    with verified_market_scan_read(cache.path, run_id) as verified:
        assert verified.run.id == run_id


def test_verified_read_rejects_same_connection_dml_after_verification(
    tmp_path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    cache, _service, _strategy_id, run_id = _environment(tmp_path)
    connections = _capture_verified_connection(monkeypatch)
    with cache.verified_market_scan_read(run_id) as verified:
        assert not hasattr(verified, "_conn")
        conn = connections[-1]
        conn.set_trace_callback(None)
        conn.execute("PRAGMA query_only = OFF")
        conn.execute(
            """
            UPDATE market_scan_probability_capture_outbox
            SET updated_at = ?
            WHERE run_id = ?
            """,
            ("2026-07-17T17:02:00+08:00", run_id),
        )
        conn.execute("PRAGMA query_only = ON")

        with pytest.raises(RuntimeError, match="验证后发生了写入"):
            _ = verified.run


def test_verified_read_rejects_same_connection_schema_change_after_verification(
    tmp_path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    cache, _service, _strategy_id, run_id = _environment(tmp_path)
    connections = _capture_verified_connection(monkeypatch)
    with cache.verified_market_scan_read(run_id) as verified:
        assert not hasattr(verified, "_conn")
        conn = connections[-1]
        conn.set_trace_callback(None)
        conn.execute("PRAGMA query_only = OFF")
        _disable_market_scan_immutability(conn)
        create_market_scan_immutability_triggers(conn)
        conn.execute("PRAGMA query_only = ON")

        with pytest.raises(RuntimeError, match="数据库结构.*发生了变化"):
            verified.results_page(**_result_query())


@pytest.mark.parametrize("transaction_end", ["commit", "rollback"])
def test_verified_read_rejects_restarted_transaction_after_verification(
    tmp_path,
    transaction_end: str,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    cache, _service, _strategy_id, run_id = _environment(tmp_path)
    connections = _capture_verified_connection(monkeypatch)
    with cache.verified_market_scan_read(run_id) as verified:
        assert not hasattr(verified, "_conn")
        conn = connections[-1]
        getattr(conn, transaction_end)()
        conn.execute("BEGIN")

        with pytest.raises(RuntimeError, match="SQLite snapshot 已失效"):
            _ = verified.action_source_digest


def test_verified_read_closed_internal_connection_does_not_mask_domain_error(
    tmp_path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    cache, _service, _strategy_id, run_id = _environment(tmp_path)
    connections = _capture_verified_connection(monkeypatch)

    with pytest.raises(RuntimeError, match="fixture domain failure"):
        with cache.verified_market_scan_read(run_id) as verified:
            assert not hasattr(verified, "_conn")
            connections[-1].close()
            with pytest.raises(RuntimeError, match="SQLite snapshot 已失效"):
                _ = verified.run
            raise RuntimeError("fixture domain failure")

    with cache.verified_market_scan_read(run_id) as verified:
        connections[-1].close()
        with pytest.raises(RuntimeError, match="SQLite snapshot 已失效"):
            _ = verified.run


def test_verified_read_linearizes_outbox_state_until_the_next_request(tmp_path) -> None:
    cache, _service, _strategy_id, run_id = _environment(tmp_path)
    with sqlite3.connect(cache.path) as conn:
        assert str(conn.execute("PRAGMA journal_mode=WAL").fetchone()[0]).lower() == "wal"

    with cache.verified_market_scan_read(run_id) as pending:
        assert pending.probability_source_capture_state is not None
        assert pending.probability_source_capture_state["status"] == "pending"
        with sqlite3.connect(cache.path, timeout=5) as writer:
            writer.execute(
                """
                UPDATE market_scan_probability_capture_outbox
                SET status = 'succeeded', completed_at = ?, archive_digest = ?,
                    last_error = NULL, updated_at = ?
                WHERE run_id = ?
                """,
                (
                    "2026-07-17T17:00:00+08:00",
                    "d" * 64,
                    "2026-07-17T17:00:00+08:00",
                    run_id,
                ),
            )
        assert pending.probability_source_capture_state["status"] == "pending"

    with cache.verified_market_scan_read(run_id) as succeeded:
        assert succeeded.probability_source_capture_state is not None
        assert succeeded.probability_source_capture_state["status"] == "succeeded"
        with sqlite3.connect(cache.path, timeout=5) as writer:
            writer.execute(
                """
                UPDATE market_scan_probability_capture_outbox
                SET status = 'skipped', archive_digest = NULL,
                    last_error = 'fixture invalidated', updated_at = ?
                WHERE run_id = ?
                """,
                ("2026-07-17T17:01:00+08:00", run_id),
            )
        assert succeeded.probability_source_capture_state["status"] == "succeeded"

    with cache.verified_market_scan_read(run_id) as skipped:
        assert skipped.probability_source_capture_state is not None
        assert skipped.probability_source_capture_state["status"] == "skipped"


def test_5382_row_results_request_stays_under_api_timeout_with_one_full_hash(
    tmp_path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    cache, run_id = _large_current_action_publication(
        tmp_path,
        result_count=5_382,
    )
    query = MarketScanQueryService(cache, _empty_research_stores())
    real_verify = market_scan_integrity.verify_market_scan_snapshot
    real_replay = (
        market_scan_action_source.replay_current_action_gate_receipt_from_verified_observations
    )
    calls: list[int] = []
    replay_calls: list[int] = []

    def tracked_verify(
        conn: sqlite3.Connection,
        candidate_run_id: int,
        *,
        result_observer: Callable[[Mapping[str, object]], None] | None = None,
    ) -> str:
        calls.append(candidate_run_id)
        return real_verify(
            conn,
            candidate_run_id,
            result_observer=result_observer,
        )

    def tracked_replay(
        conn: sqlite3.Connection,
        run: sqlite3.Row,
        diagnostics: MarketScanPublicationDiagnostics,
        observations: tuple[MarketScanScoreDistributionObservation, ...],
    ) -> MarketScanPublicationDiagnostic | None:
        replay_calls.append(int(run["id"]))
        return real_replay(conn, run, diagnostics, observations)

    monkeypatch.setattr(market_scan_integrity, "verify_market_scan_snapshot", tracked_verify)
    monkeypatch.setattr(
        market_scan_action_source,
        "replay_current_action_gate_receipt_from_verified_observations",
        tracked_replay,
    )
    monkeypatch.setattr(
        "app.repositories.market_scan_action_gate_replay.read_success_score_observations",
        lambda *_args, **_kwargs: pytest.fail(
            "verified action inspection must reuse fused score observations"
        ),
    )
    wall_started = perf_counter()
    cpu_started = process_time()
    page = _query_results(query, run_id)
    cpu_elapsed = process_time() - cpu_started
    wall_elapsed = perf_counter() - wall_started

    assert page.total == 5_382
    assert len(page.items) == 100
    assert calls == [run_id]
    assert replay_calls == [run_id]
    assert cpu_elapsed < 5.0
    assert wall_elapsed < 12.0


def test_legacy_backfill_results_use_one_full_verification(
    tmp_path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    cache, run_id = _large_missing_publication(
        tmp_path,
        result_count=12,
        seal_origin="legacy_backfill",
    )
    query = MarketScanQueryService(cache, _empty_research_stores())
    real_verify = market_scan_integrity.verify_market_scan_snapshot
    calls: list[int] = []

    def tracked_verify(
        conn: sqlite3.Connection,
        candidate_run_id: int,
        *,
        result_observer: Callable[[Mapping[str, object]], None] | None = None,
    ) -> str:
        calls.append(candidate_run_id)
        return real_verify(
            conn,
            candidate_run_id,
            result_observer=result_observer,
        )

    monkeypatch.setattr(
        "app.repositories.market_scan_verified_read.verify_market_scan_snapshot",
        tracked_verify,
    )

    page = _query_results(query, run_id)

    assert page.run.snapshot_seal_origin == "legacy_backfill"
    assert page.probability_research["availability"] == "ineligible_run_contract"
    assert calls == [run_id]


def test_scheduler_no_action_hint_never_replaces_periodic_or_api_seal_verification(
    tmp_path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    cache, _service, _strategy_id, run_id = _environment(tmp_path)
    with cache._connect() as conn:  # noqa: SLF001 - adversarial mutation setup
        _disable_market_scan_immutability(conn)
    hub = _MarketScanHub(tmp_path / "scheduler")
    hub.cache = cache
    hub.settings = hub.settings.model_copy(
        update={
            "market_scan_auto_enabled": True,
            "market_scan_schedule_hour": 16,
            "market_scan_schedule_minute": 30,
        }
    )
    scanner = _scanner(hub)
    from app.repositories import market_scan_queries

    real_verify = market_scan_queries.verify_market_scan_snapshot
    verifier_calls: list[int] = []

    def tracked_verify(conn, candidate_run_id):
        verifier_calls.append(int(candidate_run_id))
        return real_verify(conn, candidate_run_id)

    monkeypatch.setattr(market_scan_queries, "verify_market_scan_snapshot", tracked_verify)

    async def skip_probability_worker() -> None:
        return None

    monkeypatch.setattr(
        scanner,
        "_activate_probability_capture_leader",
        skip_probability_worker,
    )

    async def scenario() -> None:
        await scanner.start()
        verifier_calls.clear()
        start = datetime(2026, 7, 17, 16, 30)
        for second in range(100):
            cache.save_monitor_event("info", "test", f"unrelated-{second}")
            assert await scanner.scheduled_tick(start + timedelta(seconds=second)) is None
        assert verifier_calls == [run_id]
        with cache._connect() as conn:  # noqa: SLF001 - adversarial mutation
            conn.execute(
                "UPDATE market_scan_result SET amount = amount + 1 WHERE run_id = ? AND rank = 1",
                (run_id,),
            )
        assert await scanner.scheduled_tick(start + timedelta(seconds=101)) is None
        assert verifier_calls == [run_id]
        with pytest.raises(MarketScanSnapshotSealError):
            cache.market_scan_run(run_id)
        with pytest.raises(MarketScanSnapshotSealError):
            await scanner.scheduled_tick(start + timedelta(minutes=5))
        with cache._connect() as conn:  # noqa: SLF001 - zero-action assertion
            assert conn.execute("SELECT COUNT(*) FROM market_scan_run").fetchone()[0] == 1
        await scanner.stop()

    asyncio.run(scenario())


def test_streamed_snapshot_digest_is_byte_exact_with_materialized_v2_contract(
    tmp_path,
) -> None:
    cache, _service, _strategy_id, run_id = _environment(tmp_path)

    with cache._connect() as conn:  # noqa: SLF001 - exact legacy algorithm oracle
        sample = conn.execute(
            "SELECT name, metrics_json FROM market_scan_result WHERE run_id = ? ORDER BY symbol LIMIT 1",
            (run_id,),
        ).fetchone()
        expected = _materialized_v2_snapshot_digest(conn, run_id)
        observed = market_scan_snapshot_digest(conn, run_id)
        stored = conn.execute(
            "SELECT snapshot_digest FROM market_scan_run WHERE id = ?",
            (run_id,),
        ).fetchone()[0]

    assert sample is not None
    assert any("\u4e00" <= char <= "\u9fff" for char in str(sample["name"]))
    assert '"components"' in str(sample["metrics_json"])
    assert observed == expected == stored


def test_streamed_snapshot_digest_preserves_empty_result_array_bytes(tmp_path) -> None:
    cache, _service, _strategy_id, _run_id = _environment(tmp_path)
    timestamp = "2026-08-13T12:00:00.000000Z"

    with cache._connect() as conn:  # noqa: SLF001 - empty published snapshot fixture
        cursor = conn.execute(
            """
            INSERT INTO market_scan_run (
                status, trigger, mode, rule_version, as_of, data_date, quote_date,
                scope, created_at, updated_at, finished_at,
                snapshot_seal_origin, snapshot_sealed_at
            ) VALUES (
                'success', 'manual', 'official', 'empty-v2-test', ?, ?, ?,
                'empty-test', ?, ?, ?, 'publication', ?
            )
            """,
            (timestamp, "2026-08-13", "2026-08-13", timestamp, timestamp, timestamp, timestamp),
        )
        run_id = int(cursor.lastrowid or 0)
        expected = _materialized_v2_snapshot_digest(conn, run_id)
        observed = market_scan_snapshot_digest(conn, run_id)

    assert run_id > 0
    assert observed == expected


def test_streamed_snapshot_digest_preserves_float_bytes_and_symbol_order(
    tmp_path,
) -> None:
    cache, _service, _strategy_id, run_id = _environment(tmp_path)

    with cache._connect() as conn:  # noqa: SLF001 - canonical-order fixture
        _disable_market_scan_immutability(conn)
        rows = conn.execute(
            "SELECT symbol FROM market_scan_result WHERE run_id = ? ORDER BY symbol DESC",
            (run_id,),
        ).fetchall()
        conn.execute("PRAGMA reverse_unordered_selects = ON")
        conn.execute(
            "UPDATE market_scan_result SET raw_score = ? WHERE run_id = ? AND symbol = ?",
            (82.98129600000001, run_id, str(rows[0][0])),
        )
        expected = _materialized_v2_snapshot_digest(conn, run_id)
        first = market_scan_snapshot_digest(conn, run_id)
        conn.execute("PRAGMA reverse_unordered_selects = OFF")
        second = market_scan_snapshot_digest(conn, run_id)

    assert first == second == expected


def test_snapshot_verifier_never_fetches_all_result_rows(tmp_path) -> None:
    cache, _service, _strategy_id, run_id = _environment(tmp_path)

    class ResultCursor:
        def __init__(self, cursor: sqlite3.Cursor) -> None:
            self._cursor = cursor

        @property
        def description(self):
            return self._cursor.description

        def __iter__(self):
            return iter(self._cursor)

        def fetchall(self):
            pytest.fail("snapshot digest must stream result rows")

    class StreamingConnection:
        def __init__(self, conn: sqlite3.Connection) -> None:
            self._conn = conn

        def execute(self, sql: str, parameters: tuple[object, ...] = ()):
            cursor = self._conn.execute(sql, parameters)
            if "FROM market_scan_result WHERE run_id = ?" in sql:
                return ResultCursor(cursor)
            return cursor

    with cache._connect() as conn:  # noqa: SLF001 - cursor behavior boundary
        expected = conn.execute(
            "SELECT snapshot_digest FROM market_scan_run WHERE id = ?",
            (run_id,),
        ).fetchone()[0]
        observed = verify_market_scan_snapshot(StreamingConnection(conn), run_id)  # type: ignore[arg-type]

    assert observed == expected


@pytest.mark.parametrize("invalid_json", ('{"duplicate":1,"duplicate":2}', '{"value":NaN}'))
def test_streamed_snapshot_digest_keeps_strict_json_fail_closed(
    tmp_path,
    invalid_json: str,
) -> None:
    cache, _service, _strategy_id, run_id = _environment(tmp_path)

    with cache._connect() as conn:  # noqa: SLF001 - malformed persistence fixture
        _disable_market_scan_immutability(conn)
        conn.execute(
            "UPDATE market_scan_result SET metrics_json = ? WHERE run_id = ? AND rank = 1",
            (invalid_json, run_id),
        )
        with pytest.raises(MarketScanSnapshotSealError, match="不是严格有限 JSON"):
            market_scan_snapshot_digest(conn, run_id)


def _materialized_v2_snapshot_digest(conn: sqlite3.Connection, run_id: int) -> str:
    run = conn.execute(
        "SELECT * FROM market_scan_run WHERE id = ?",
        (run_id,),
    ).fetchone()
    results = conn.execute(
        "SELECT * FROM market_scan_result WHERE run_id = ? ORDER BY symbol ASC",
        (run_id,),
    ).fetchall()
    payload = {
        "contract_version": market_scan_integrity.MARKET_SCAN_SNAPSHOT_DIGEST_CONTRACT,
        "run": market_scan_integrity._canonical_row(  # noqa: SLF001
            run,
            market_scan_integrity._RUN_FIELDS,  # noqa: SLF001
            market_scan_integrity._RUN_JSON_FIELDS,  # noqa: SLF001
            path="run",
        ),
        "results": [
            market_scan_integrity._canonical_row(  # noqa: SLF001
                row,
                market_scan_integrity._RESULT_FIELDS,  # noqa: SLF001
                market_scan_integrity._RESULT_JSON_FIELDS,  # noqa: SLF001
                path=f"result[{row['symbol']}]",
            )
            for row in results
        ],
    }
    return sha256_hex(canonical_json_text(payload))


def test_legacy_backfill_seal_is_audit_only_and_cannot_authorize_strategy_source(
    tmp_path,
) -> None:
    cache, _service, _strategy_id, run_id = _environment(tmp_path)
    with cache._connect() as conn:  # noqa: SLF001 - provenance boundary fixture
        _disable_market_scan_immutability(conn)
        conn.execute(
            """
            UPDATE market_scan_run
            SET snapshot_digest = NULL, snapshot_seal_origin = 'legacy_backfill'
            WHERE id = ?
            """,
            (run_id,),
        )
        conn.execute(
            "UPDATE market_scan_run SET snapshot_digest = ? WHERE id = ?",
            (market_scan_snapshot_digest(conn, run_id), run_id),
        )
        assert verify_market_scan_snapshot(conn, run_id)
        with pytest.raises(MarketScanSnapshotSealError, match="原发布快照"):
            require_publication_market_scan_snapshot(conn, run_id)

    with pytest.raises(FrozenFullMarketSnapshotIntegrityError, match="原发布快照"):
        cache.strategy_execution_repo.frozen_scan(
            run_id=run_id,
            data_date=None,
            mode="official",
        )


def test_executable_shadow_revalidates_before_portfolio_build(
    tmp_path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    cache, _service, _strategy_id, run_id = _environment(tmp_path)
    frozen = cache.strategy_execution_repo.frozen_scan(
        run_id=run_id,
        data_date=None,
        mode="official",
    )

    class CorruptRepository:
        def frozen_scan(self, **_kwargs: object) -> FrozenMarketScan:
            return FrozenMarketScan(run=frozen.run, items=frozen.items[:-1])

    def forbidden_build(*_args: object, **_kwargs: object) -> None:
        pytest.fail("shadow build must not run for an invalid frozen snapshot")

    monkeypatch.setattr(
        "app.services.market_scan_executable_shadow.build_portfolio_draft",
        forbidden_build,
    )

    shadow = MarketScanExecutableShadowService(CorruptRepository())
    with pytest.raises(FrozenFullMarketSnapshotIntegrityError):
        shadow.project(run_id)


@pytest.mark.parametrize("mutation", ("run_update", "run_delete", "result_update", "result_delete", "result_insert"))
def test_published_snapshot_is_append_only_at_the_database_boundary(tmp_path, mutation: str) -> None:
    cache, _service, _strategy_id, run_id = _environment(tmp_path)
    statements = {
        "run_update": ("UPDATE market_scan_run SET message = 'tampered' WHERE id = ?", (run_id,)),
        "run_delete": ("DELETE FROM market_scan_run WHERE id = ?", (run_id,)),
        "result_update": (
            "UPDATE market_scan_result SET amount = amount + 1 WHERE run_id = ? AND rank = 1",
            (run_id,),
        ),
        "result_delete": ("DELETE FROM market_scan_result WHERE run_id = ? AND rank = 1", (run_id,)),
        "result_insert": (
            "INSERT INTO market_scan_result (run_id, symbol, code, market, name, status, updated_at) VALUES (?, '999999.SH', '999999', 'SH', '伪造股票', 'pending', '2026-07-17 16:00:00')",
            (run_id,),
        ),
    }
    with cache._connect() as conn:  # noqa: SLF001 - deterministic raw SQL attack
        with pytest.raises(sqlite3.IntegrityError, match="published market_scan"):
            conn.execute(*statements[mutation])

    with cache._connect() as conn:  # noqa: SLF001 - assert rollback preserved the seal
        assert verify_market_scan_snapshot(conn, run_id)


@pytest.mark.parametrize(
    "mutation",
    ("seal_before_finished", "updated_before_finished", "updated_after_seal", "future_result"),
)
def test_publication_seal_rejects_impossible_audit_time_order(
    tmp_path,
    mutation: str,
) -> None:
    cache, _service, _strategy_id, run_id = _environment(tmp_path)
    with cache._connect() as conn:  # noqa: SLF001 - privileged corruption fixture
        _disable_market_scan_immutability(conn)
        conn.execute(
            """
            UPDATE market_scan_run
            SET snapshot_digest = NULL,
                snapshot_seal_origin = NULL,
                snapshot_sealed_at = NULL
            WHERE id = ?
            """,
            (run_id,),
        )
        sealed_at = "2099-01-01T00:00:00Z"
        if mutation == "seal_before_finished":
            sealed_at = "2000-01-01T00:00:00Z"
        elif mutation == "updated_before_finished":
            conn.execute(
                "UPDATE market_scan_run SET updated_at = '2000-01-01T00:00:00Z' WHERE id = ?",
                (run_id,),
            )
        elif mutation == "updated_after_seal":
            conn.execute(
                "UPDATE market_scan_run SET updated_at = '2099-01-02T00:00:00Z' WHERE id = ?",
                (run_id,),
            )
            sealed_at = "2099-01-01T00:00:00Z"
        else:
            conn.execute(
                "UPDATE market_scan_result SET updated_at = '2099-01-02T00:00:00Z' WHERE run_id = ?",
                (run_id,),
            )

        with pytest.raises(MarketScanSnapshotSealError):
            seal_market_scan_snapshot(conn, run_id, sealed_at=sealed_at)


@pytest.mark.parametrize(
    ("field", "value"),
    (
        ("snapshot_sealed_at", "2000-01-01T00:00:00Z"),
        ("finished_at", "2099-01-02T00:00:00Z"),
        ("updated_at", "2099-01-02T00:00:00Z"),
    ),
)
def test_public_run_model_rejects_impossible_audit_time_order(
    tmp_path,
    field: str,
    value: str,
) -> None:
    cache, _service, _strategy_id, run_id = _environment(tmp_path)
    run = cache.market_scan_run(run_id)
    payload = run.model_dump(mode="python")
    payload[field] = value

    with pytest.raises(ValidationError):
        MarketScanRun.model_validate(payload)


def test_public_result_page_rejects_result_updated_after_run(tmp_path) -> None:
    cache, _service, _strategy_id, run_id = _environment(tmp_path)
    frozen = cache.strategy_execution_repo.frozen_scan(
        run_id=run_id,
        data_date=None,
        mode="official",
    )
    item = frozen.items[0].model_copy(
        update={"updated_at": "2099-01-02T00:00:00Z"}
    )

    with pytest.raises(ValidationError, match="不能晚于"):
        MarketScanResultPage(
            run=frozen.run,
            items=[item],
            total=1,
            page=1,
            page_size=1,
            page_count=1,
        )


def _query_results(
    service: MarketScanQueryService,
    run_id: int,
) -> MarketScanResultPage:
    return service.results(run_id, **_result_query())


def _empty_research_stores() -> MarketScanResearchStores:
    return MarketScanResearchStores(
        probability=None,
        probability_source=None,
        future_range=None,
    )


def _result_query() -> dict[str, object]:
    return {
        "page": 1,
        "page_size": 100,
        "status": None,
        "market": None,
        "industry": None,
        "is_st": None,
        "is_new": None,
        "min_data_quality_score": None,
        "keyword": None,
        "sort": ("rank",),
        "order": ("asc",),
    }


def _large_missing_publication(
    tmp_path,
    *,
    result_count: int,
    seal_origin: str = "publication",
) -> tuple[SQLiteCache, int]:
    cache = SQLiteCache(tmp_path / "large-verified-read.sqlite3")
    stamp = "2026-08-13T16:10:00+08:00"
    with cache._connect() as conn:  # noqa: SLF001 - compact performance fixture
        cursor = conn.execute(
            """
            INSERT INTO market_scan_run (
                status, trigger, mode, rule_version, as_of, data_date, quote_date,
                scope, total_count, processed_count, success_count, missing_count,
                skipped_count, created_at, updated_at, finished_at, message,
                publication_diagnostics_json
            ) VALUES (
                'success', 'manual', 'official', 'large-read-performance-v1',
                ?, '2026-08-13', '2026-08-13', ?, ?, ?, 0, ?, 0,
                ?, ?, ?, 'large verified read fixture', ?
            )
            """,
            (
                stamp,
                MARKET_SCAN_FULL_MARKET_SCOPE,
                result_count,
                result_count,
                result_count,
                stamp,
                stamp,
                stamp,
                action_pass_publication_diagnostics().model_dump_json(),
            ),
        )
        run_id = int(cursor.lastrowid or 0)
        rows = []
        markets = ("SH", "SZ", "BJ")
        for index in range(result_count):
            code = f"{100_000 + index:06d}"
            market = markets[index % len(markets)]
            rows.append(
                (
                    run_id,
                    f"{code}.{market}",
                    code,
                    market,
                    f"样本{code}",
                    "missing",
                    "fixture missing quote",
                    "2026-08-13",
                    stamp,
                )
            )
        conn.executemany(
            """
            INSERT INTO market_scan_result (
                run_id, symbol, code, market, name, status, error, data_date,
                updated_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            rows,
        )
        seal_market_scan_snapshot(
            conn,
            run_id,
            origin=seal_origin,
            sealed_at=stamp,
        )
    return cache, run_id


def _large_current_action_publication(
    tmp_path,
    *,
    result_count: int,
) -> tuple[SQLiteCache, int]:
    settings, rule_version = _settings_and_rule(
        cache_path=tmp_path / "large-current-action.sqlite3"
    )
    cache = SQLiteCache(settings=settings)
    repo = cache.market_scan_repo
    contract = market_scan_rule_contract(settings)
    market_counts = _representative_market_counts(result_count)
    seeds = [
        MarketScanSeed(
            symbol=f"{code}.{market}",
            code=code,
            market=market,
            name=f"{market}{index}",
            list_date="2000-01-01",
            metadata_source="akshare",
        )
        for market, count in market_counts.items()
        for index, code in enumerate(_market_codes(market, count))
    ]
    run = repo.create_run(
        trigger="manual",
        mode="official",
        rule_version=rule_version,
        as_of="2026-07-17 16:30:00",
        data_date=DATA_DATE.isoformat(),
        scope=MARKET_SCAN_FULL_MARKET_SCOPE,
        rule_contract=contract,
    )
    repo.start_run(run.id)
    repo.record_stock_pool_source(run.id, "provider-full-pool")
    repo.seed_results(run.id, seeds, excluded_count=0)
    repo.begin_quote_capture(run.id, "2026-07-17T08:29:59Z")
    repo.seal_quote_capture(
        run.id,
        finished_at="2026-07-17T08:30:02Z",
        decision_as_of="2026-07-17 16:30:00",
        duration_ms=3_000,
        count=result_count,
    )
    pending = {item.symbol: item for item in repo.pending_items(run.id)}
    writes: list[MarketScanResultWrite] = []
    for index, seed in enumerate(seeds):
        quote, rows = _varying_success_case(
            pending[seed.symbol],
            index=index * 199.0 / (result_count - 1),
        )
        writes.append(
            replace(
                _score(
                    pending[seed.symbol],
                    quote,
                    rows,
                    settings=settings,
                    rule_version=rule_version,
                ),
                quote_observed_at=QUOTE_OBSERVED_AT,
            )
        )
    repo.save_result_batch(run.id, writes)
    policy = MarketScanScoreDistributionPolicy()
    distribution = MarketScanScoreDistribution.from_score_observations(
        repo.success_score_observations(run.id),
        expected_count=result_count,
        policy=policy,
    )
    assert policy.assess(distribution).status == "pass"
    diagnostics = action_pass_publication_diagnostics()
    diagnostics = diagnostics.model_copy(
        update={
            "passed_gates": [
                diagnostics.passed_gates[0].model_copy(
                    update={
                        "detail": distribution.audit_text().removeprefix(
                            "评分分布门禁 "
                        )
                    }
                )
            ]
        }
    )
    published = repo.finish_run(
        run.id,
        "degraded",
        message="5382 current action source performance fixture",
        publication_diagnostics=diagnostics,
    )
    assert published.success_count == result_count
    return cache, run.id


def _representative_market_counts(result_count: int) -> dict[str, int]:
    if result_count != 5_382:
        raise ValueError("current action performance fixture requires 5382 results")
    return {"SH": 2_265, "SZ": 2_806, "BJ": 311}


def _market_codes(market: str, count: int) -> list[str]:
    starts = {"SH": 600_000, "SZ": 1, "BJ": 920_000}
    return [f"{starts[market] + index:06d}" for index in range(count)]
