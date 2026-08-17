from __future__ import annotations

from collections.abc import Callable
import gzip
from pathlib import Path
import sqlite3
import hashlib
import json

import pytest

from app.artifacts.io import canonical_json_text, sha256_hex
from app.config import Settings
from app.db.market_scan_integrity import (
    MarketScanSnapshotSealError,
    create_market_scan_immutability_triggers,
    delete_verified_market_scan_snapshots,
    drop_market_scan_immutability_triggers,
    market_scan_immutability_triggers_present,
    seal_market_scan_snapshot,
)
import app.repositories.market_scan_retention as market_scan_retention
import app.repositories.runtime_research_artifact_retention as artifact_retention
from app.repositories.runtime_research_artifact_retention import (
    RuntimeCleanupIntegrityError,
    market_scan_artifact_protection,
)
import app.repositories.runtime_research_artifact_retention as artifact_retention_module
from app.services.cache import SQLiteCache
from app.services.market_scan_universe import FULL_MARKET_SCOPE
from tests.market_scan_test_support import action_pass_publication_diagnostics


_STAMP = "2026-08-01T16:31:00+08:00"


def test_published_retention_deletes_exact_overflow_and_restores_guards(
    tmp_path: Path,
) -> None:
    cache, run_ids = _published_cache(tmp_path, count=3, limit=1)

    preview = cache.preview_runtime_cleanup()
    removed = cache.cleanup_runtime_rows()

    assert preview["market_scan_run"] == 2
    assert removed["market_scan_run"] == 2
    with sqlite3.connect(cache.path) as conn:
        assert conn.execute("SELECT id FROM market_scan_run").fetchall() == [(run_ids[-1],)]
        assert conn.execute("SELECT COUNT(*) FROM market_scan_result").fetchone()[0] == 1
        assert len(market_scan_immutability_triggers_present(conn)) == 5
        with pytest.raises(sqlite3.IntegrityError, match="immutable"):
            conn.execute(
                "UPDATE market_scan_run SET message = 'blocked' WHERE id = ?",
                (run_ids[-1],),
            )


def test_tampered_published_overflow_rolls_back_all_cleanup(tmp_path: Path) -> None:
    cache, run_ids = _published_cache(tmp_path, count=3, limit=1)
    with sqlite3.connect(cache.path) as conn:
        drop_market_scan_immutability_triggers(conn)
        conn.execute(
            "UPDATE market_scan_result SET score = 1 WHERE run_id = ?",
            (run_ids[0],),
        )
        create_market_scan_immutability_triggers(conn)

    with pytest.raises(MarketScanSnapshotSealError, match="摘要不一致"):
        cache.cleanup_runtime_rows()

    with sqlite3.connect(cache.path) as conn:
        assert conn.execute("SELECT COUNT(*) FROM market_scan_run").fetchone()[0] == 3
        assert conn.execute("SELECT COUNT(*) FROM market_scan_result").fetchone()[0] == 3
        assert len(market_scan_immutability_triggers_present(conn)) == 5


def test_injected_delete_failure_rolls_back_and_keeps_guards(tmp_path: Path) -> None:
    cache, run_ids = _published_cache(tmp_path, count=3, limit=1)
    with sqlite3.connect(cache.path) as conn:
        conn.execute(
            f"""
            CREATE TRIGGER injected_market_scan_delete_failure
            BEFORE DELETE ON market_scan_run WHEN OLD.id = {run_ids[0]}
            BEGIN
                SELECT RAISE(ABORT, 'injected published cleanup failure');
            END
            """
        )

    with pytest.raises(sqlite3.IntegrityError, match="injected published cleanup failure"):
        cache.cleanup_runtime_rows()

    with sqlite3.connect(cache.path) as conn:
        assert conn.execute("SELECT COUNT(*) FROM market_scan_run").fetchone()[0] == 3
        assert conn.execute("SELECT COUNT(*) FROM market_scan_result").fetchone()[0] == 3
        assert len(market_scan_immutability_triggers_present(conn)) == 5


def test_artifact_published_during_delete_rolls_back_database_and_pins_next_pass(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    cache, run_ids = _published_cache(tmp_path, count=3, limit=1)
    original = market_scan_retention.delete_verified_market_scan_snapshots
    artifact_path: Path | None = None

    def publish_then_delete(conn: sqlite3.Connection, candidates: tuple[int, ...]) -> int:
        nonlocal artifact_path
        directory = tmp_path / "research" / "individual_probability"
        directory.mkdir(parents=True)
        artifact_path = _write_individual_assessment(directory, run_ids=(run_ids[0],))
        return original(conn, candidates)

    monkeypatch.setattr(
        market_scan_retention,
        "delete_verified_market_scan_snapshots",
        publish_then_delete,
    )

    with pytest.raises(RuntimeCleanupIntegrityError, match="事务期间发生变化"):
        cache.cleanup_runtime_rows()

    assert artifact_path is not None and artifact_path.is_file()
    with sqlite3.connect(cache.path) as conn:
        assert conn.execute("SELECT COUNT(*) FROM market_scan_run").fetchone()[0] == 3
        assert conn.execute("SELECT COUNT(*) FROM market_scan_result").fetchone()[0] == 3
        assert len(market_scan_immutability_triggers_present(conn)) == 5
    monkeypatch.setattr(
        market_scan_retention,
        "delete_verified_market_scan_snapshots",
        original,
    )
    assert cache.preview_runtime_cleanup()["market_scan_run"] == 1


def test_verified_snapshot_delete_rejects_missing_explicit_transaction(
    tmp_path: Path,
) -> None:
    cache, run_ids = _published_cache(tmp_path, count=1, limit=1)
    with sqlite3.connect(cache.path, isolation_level=None) as conn:
        assert not conn.in_transaction
        with pytest.raises(MarketScanSnapshotSealError, match="显式写事务"):
            delete_verified_market_scan_snapshots(conn, run_ids)
        assert len(market_scan_immutability_triggers_present(conn)) == 5
        assert conn.execute("SELECT COUNT(*) FROM market_scan_run").fetchone()[0] == 1


@pytest.mark.parametrize(
    "reference",
    [
        "probability_outbox",
        "discovery_queue",
        "screen_alert",
        "strategy_execution",
        "strategy_schedule",
        "strategy_schedule_run",
        "dynamic_foreign_key",
        "retry_graph",
    ],
)
def test_published_retention_protects_each_database_reference_family(
    tmp_path: Path,
    reference: str,
) -> None:
    cache, run_ids = _published_cache(tmp_path / reference, count=2, limit=1)
    with sqlite3.connect(cache.path) as conn:
        _attach_reference(conn, reference, run_ids[0], run_ids[1])

    preview = cache.preview_runtime_cleanup()
    removed = cache.cleanup_runtime_rows()

    assert preview["market_scan_run"] == 0
    assert removed["market_scan_run"] == 0
    with sqlite3.connect(cache.path) as conn:
        assert conn.execute("SELECT COUNT(*) FROM market_scan_run").fetchone()[0] == 2


def test_terminal_skipped_probability_outbox_does_not_pin_published_snapshot(
    tmp_path: Path,
) -> None:
    cache, run_ids = _published_cache(tmp_path, count=3, limit=1)
    with sqlite3.connect(cache.path) as conn:
        conn.execute(
            """
            INSERT INTO market_scan_probability_capture_outbox (
                run_id, status, attempt_count, next_attempt_at, completed_at,
                last_error, created_at, updated_at
            ) VALUES (?, 'skipped', 25, ?, ?, '评分分布门禁未通过', ?, ?)
            """,
            (run_ids[0], _STAMP, _STAMP, _STAMP, _STAMP),
        )

    assert cache.preview_runtime_cleanup()["market_scan_run"] == 2
    assert cache.cleanup_runtime_rows()["market_scan_run"] == 2
    with sqlite3.connect(cache.path) as conn:
        assert conn.execute("SELECT id FROM market_scan_run").fetchall() == [
            (run_ids[-1],)
        ]
        assert conn.execute(
            "SELECT COUNT(*) FROM market_scan_probability_capture_outbox"
        ).fetchone()[0] == 0


def test_task_run_owner_does_not_pin_published_snapshot(tmp_path: Path) -> None:
    cache, run_ids = _published_cache(tmp_path, count=3, limit=1)
    with sqlite3.connect(cache.path) as conn:
        for run_id in run_ids:
            _attach_task_before_reseal(conn, run_id)

    assert cache.preview_runtime_cleanup()["market_scan_run"] == 2
    assert cache.cleanup_runtime_rows()["market_scan_run"] == 2
    with sqlite3.connect(cache.path) as conn:
        assert conn.execute("SELECT id FROM market_scan_run").fetchall() == [(run_ids[-1],)]


@pytest.mark.parametrize("active_status", ["queued", "running", "cancelling"])
def test_active_retry_descendant_recursively_pins_every_ancestor(
    tmp_path: Path,
    active_status: str,
) -> None:
    cache, run_ids = _published_cache(tmp_path, count=1, limit=1)
    with sqlite3.connect(cache.path) as conn:
        first_failed = _insert_retry_run(conn, run_ids[0], status="failed")
        second_failed = _insert_retry_run(conn, first_failed, status="failed")
        active = _insert_retry_run(conn, second_failed, status=active_status)

    assert cache.preview_runtime_cleanup()["market_scan_run"] == 0
    assert cache.cleanup_runtime_rows()["market_scan_run"] == 0
    with sqlite3.connect(cache.path) as conn:
        assert conn.execute("SELECT id, retry_of_run_id FROM market_scan_run ORDER BY id").fetchall() == [
            (run_ids[0], None),
            (first_failed, run_ids[0]),
            (second_failed, first_failed),
            (active, second_failed),
        ]


@pytest.mark.parametrize("active_status", ["queued", "running", "cancelling"])
def test_retained_published_and_active_retry_chain_pins_candidate_ancestors(
    tmp_path: Path,
    active_status: str,
) -> None:
    path = tmp_path / "runtime.sqlite3"
    cache = SQLiteCache(path, settings=Settings(cache_path=path, max_market_scan_runs=1))
    with sqlite3.connect(path) as conn:
        oldest_published = _insert_published_snapshot(conn, 0)
        failed = _insert_retry_run(conn, oldest_published, status="failed")
        retained_published = _insert_published_snapshot(conn, 1)
        _attach_retry_before_reseal(conn, failed, retained_published)
        active = _insert_retry_run(conn, retained_published, status=active_status)

    assert cache.preview_runtime_cleanup()["market_scan_run"] == 0
    assert cache.cleanup_runtime_rows()["market_scan_run"] == 0
    with sqlite3.connect(cache.path) as conn:
        assert conn.execute("SELECT id, status, retry_of_run_id FROM market_scan_run ORDER BY id").fetchall() == [
            (oldest_published, "success", None),
            (failed, "failed", oldest_published),
            (retained_published, "success", failed),
            (active, active_status, retained_published),
        ]


def test_file_artifact_reference_pins_run_and_ambiguous_catalog_fails_closed(
    tmp_path: Path,
) -> None:
    cache, run_ids = _published_cache(tmp_path, count=2, limit=1)
    directory = tmp_path / "research" / "market_scan_future_range"
    directory.mkdir(parents=True)
    payload = {"run": {"run_id": run_ids[0]}}
    digest = sha256_hex(canonical_json_text(payload))
    protected = directory / f"market-scan-future-range-run-{run_ids[0]}-{digest}.json"
    protected.write_text(
        canonical_json_text(
            {
                "schema_version": "market-scan-future-range-artifact-v1",
                "payload": payload,
                "integrity": {
                    "algorithm": "sha256",
                    "scope": "payload",
                    "integrity_digest": digest,
                },
            }
        ),
        encoding="utf-8",
    )

    assert cache.preview_runtime_cleanup()["market_scan_run"] == 0
    assert cache.cleanup_runtime_rows()["market_scan_run"] == 0

    (directory / "unattributed.json").write_text("{}", encoding="utf-8")
    with pytest.raises(RuntimeCleanupIntegrityError, match="无法归属"):
        cache.preview_runtime_cleanup()
    with sqlite3.connect(cache.path) as conn:
        assert conn.execute("SELECT COUNT(*) FROM market_scan_run").fetchone()[0] == 2


def test_individual_probability_assessment_recursively_pins_source_run(
    tmp_path: Path,
) -> None:
    cache, run_ids = _published_cache(tmp_path, count=2, limit=1)
    directory = tmp_path / "research" / "individual_probability"
    directory.mkdir(parents=True)
    unsigned = {
        "schema_version": "individual-upside-probability-assessment-v1",
        "generated_at": _STAMP,
        "payload": {"official_pit": {"sources": [{"run_id": run_ids[0], "integrity_digest": "a" * 64}]}},
    }
    digest = sha256_hex(canonical_json_text(unsigned))
    artifact = {
        **unsigned,
        "integrity": {
            "algorithm": "sha256",
            "integrity_digest": digest,
            "notice": "test",
        },
    }
    (directory / f"individual-upside-probability-assessment-{digest}.json").write_text(
        canonical_json_text(artifact),
        encoding="utf-8",
    )

    assert cache.preview_runtime_cleanup()["market_scan_run"] == 0
    assert cache.cleanup_runtime_rows()["market_scan_run"] == 0


def test_individual_probability_docs_fallback_pins_when_primary_missing(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    project_root = tmp_path / "project"
    fallback = project_root / "docs" / "research" / "artifacts"
    fallback.mkdir(parents=True)
    _write_individual_assessment(fallback, run_ids=(71, 77))
    monkeypatch.setattr(artifact_retention, "_PROJECT_ROOT", project_root)

    protection = market_scan_artifact_protection(project_root / "data" / "ashare_radar.sqlite3")

    assert {71, 77}.issubset(protection.run_ids)


@pytest.mark.parametrize("scope", ["payload", "generated_at+payload"])
def test_stream_probability_manifest_verifies_semantic_digest(
    tmp_path: Path,
    scope: str,
) -> None:
    generated_at = "2026-08-11T07:09:44Z"
    payload = {
        "studies": [{"metadata": {"artifact_set_run_ids": [37, 62]}, "run_id": 37}],
        "records": [{"generated_at": generated_at, "run_id": 37}],
    }
    digest_payload = {"studies": payload["studies"], "records": [{"run_id": 37}]}
    identity: object = digest_payload
    if scope == "generated_at+payload":
        identity = {"generated_at": generated_at, "payload": digest_payload}
    digest = hashlib.sha256(json.dumps(identity, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode()).hexdigest()
    artifact = {
        "generated_at": generated_at,
        "integrity": {
            "algorithm": "sha256",
            "integrity_digest": digest,
            "notice": "integrity_digest_not_a_signature",
            "scope": scope,
        },
        "payload": payload,
        "schema_version": "market-scan-probability-artifact-v1",
    }
    path = tmp_path / f"market-scan-probability-run-37-{digest}.json"
    path.write_text(
        json.dumps(artifact, ensure_ascii=False, sort_keys=True, separators=(",", ":")),
        encoding="utf-8",
    )
    assert artifact_retention_module._stream_probability_run_ids(path, 37, digest, path.stat().st_size) == {37, 62}


def test_stream_probability_accepts_legacy_raw_payload_digest(tmp_path: Path) -> None:
    generated_at = "2026-08-11T07:09:44Z"
    payload = {"generated_at": generated_at, "run_id": 37}
    digest = hashlib.sha256(json.dumps(payload, sort_keys=True, separators=(",", ":")).encode()).hexdigest()
    artifact = {
        "generated_at": generated_at,
        "integrity": {
            "algorithm": "sha256",
            "integrity_digest": digest,
            "notice": "integrity_digest_not_a_signature",
            "scope": "payload",
        },
        "payload": payload,
        "schema_version": "market-scan-probability-artifact-v1",
    }
    path = tmp_path / f"market-scan-probability-run-37-{digest}.json"
    path.write_text(json.dumps(artifact, sort_keys=True, separators=(",", ":")), encoding="utf-8")

    assert artifact_retention_module._stream_probability_run_ids(path, 37, digest, path.stat().st_size) == {37}


def test_stream_probability_keeps_long_manifest_across_read_boundary(tmp_path: Path) -> None:
    generated_at = "2026-08-11T07:09:44Z"
    run_ids = list(range(1, 301))
    padding = "x" * (65 * 1024 * 1024 + 1_048_400)
    payload = {"padding": padding, "study": {"artifact_set_run_ids": run_ids, "run_id": 37}}
    encoded_payload = json.dumps(payload, sort_keys=True, separators=(",", ":")).encode()
    digest = hashlib.sha256(encoded_payload).hexdigest()
    artifact = {
        "generated_at": generated_at,
        "integrity": {
            "algorithm": "sha256",
            "integrity_digest": digest,
            "notice": "integrity_digest_not_a_signature",
            "scope": "payload",
        },
        "payload": payload,
        "schema_version": "market-scan-probability-artifact-v1",
    }
    path = tmp_path / f"market-scan-probability-run-37-{digest}.json"
    path.write_text(json.dumps(artifact, sort_keys=True, separators=(",", ":")), encoding="utf-8")

    assert artifact_retention_module._stream_probability_run_ids(path, 37, digest, path.stat().st_size) == set(run_ids)


@pytest.mark.parametrize(
    "old,new",
    [
        (b'"algorithm":"sha256"', b'"algorithm":"sha257"'),
        (b'"artifact_set_run_ids":[37,62]', b'"artifact_set_run_ids":[37,99]'),
        (b'"run_id":37', b'"run_id":99'),
        (b"2026-08-11T07:09:44Z", b"2026-08-11T07:09:45Z"),
    ],
)
def test_stream_probability_manifest_rejects_equal_length_tamper(
    tmp_path: Path,
    old: bytes,
    new: bytes,
) -> None:
    generated_at = "2026-08-11T07:09:44Z"
    payload = {"studies": [{"artifact_set_run_ids": [37, 62], "run_id": 37}], "generated_at": generated_at}
    digest_payload = {"studies": payload["studies"]}
    digest = hashlib.sha256(json.dumps(digest_payload, sort_keys=True, separators=(",", ":")).encode()).hexdigest()
    artifact = {
        "generated_at": generated_at,
        "integrity": {"algorithm": "sha256", "integrity_digest": digest, "notice": "integrity_digest_not_a_signature", "scope": "payload"},
        "payload": payload,
        "schema_version": "market-scan-probability-artifact-v1",
    }
    encoded = json.dumps(artifact, sort_keys=True, separators=(",", ":")).encode()
    encoded = encoded.replace(old, new, 1)
    path = tmp_path / f"market-scan-probability-run-37-{digest}.json"
    path.write_bytes(encoded)
    with pytest.raises(RuntimeCleanupIntegrityError):
        artifact_retention_module._stream_probability_run_ids(path, 37, digest, path.stat().st_size)


def test_individual_probability_primary_ignores_broken_docs_fallback(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    project_root = tmp_path / "project"
    fallback = project_root / "docs" / "research" / "artifacts"
    fallback.mkdir(parents=True)
    (fallback / "broken.json").write_text("broken", encoding="utf-8")
    primary = tmp_path / "research" / "individual_probability"
    primary.mkdir(parents=True)
    _write_individual_assessment(primary, run_ids=(10,))
    monkeypatch.setattr(artifact_retention, "_PROJECT_ROOT", project_root)

    protection = market_scan_artifact_protection(tmp_path / "runtime.sqlite3")

    assert 10 in protection.run_ids


def test_fit_members_pin_only_explicit_sparse_run_ids(tmp_path: Path) -> None:
    directory = tmp_path / "research" / "market_scan_probability_fit"
    directory.mkdir(parents=True)
    payload = {
        "through_run_id": 100,
        "members": [{"run_id": 10}, {"run_id": 100}],
    }
    digest = sha256_hex(canonical_json_text(payload))
    artifact = {
        "schema_version": "market-scan-probability-fit-assessment-v1",
        "generated_at": _STAMP,
        "payload": payload,
        "integrity": {
            "algorithm": "sha256",
            "scope": "payload",
            "integrity_digest": digest,
        },
    }
    encoded = gzip.compress(canonical_json_text(artifact).encode(), mtime=0)
    target = directory / f"market-scan-probability-fit-through-run-100-{digest}.json.gz"
    target.write_bytes(encoded)

    protection = market_scan_artifact_protection(tmp_path / "runtime.sqlite3")

    assert {10, 100}.issubset(protection.run_ids)
    assert 50 not in protection.run_ids


def test_future_range_summary_pins_every_declared_run_id(tmp_path: Path) -> None:
    cache, run_ids = _published_cache(tmp_path, count=2, limit=1)
    directory = tmp_path / "research"
    directory.mkdir(parents=True)
    (directory / "market-scan-future-range-summary.json").write_text(
        canonical_json_text(
            {
                "schema_version": "market-scan-future-range-evaluation-summary-v1",
                "artifact_count": 1,
                "artifacts": [
                    {
                        "run_id": run_ids[0],
                        "integrity_digest": "a" * 64,
                        "offline_replay_verified": True,
                    }
                ],
            }
        ),
        encoding="utf-8",
    )

    assert cache.preview_runtime_cleanup()["market_scan_run"] == 0
    assert cache.cleanup_runtime_rows()["market_scan_run"] == 0


@pytest.mark.parametrize(
    "summary",
    [
        {},
        {
            "schema_version": "market-scan-future-range-evaluation-summary-v1",
            "artifact_count": 2,
            "artifacts": [],
        },
        {
            "schema_version": "market-scan-future-range-evaluation-summary-v1",
            "artifact_count": 1,
            "artifacts": [
                {
                    "run_id": 1,
                    "integrity_digest": "bad",
                    "offline_replay_verified": False,
                }
            ],
        },
    ],
)
def test_future_range_summary_malformed_shape_fails_cleanup_closed(
    tmp_path: Path,
    summary: dict[str, object],
) -> None:
    cache, _run_ids = _published_cache(tmp_path, count=2, limit=1)
    directory = tmp_path / "research"
    directory.mkdir(parents=True)
    (directory / "market-scan-future-range-summary.json").write_text(
        canonical_json_text(summary),
        encoding="utf-8",
    )

    with pytest.raises(RuntimeCleanupIntegrityError, match="未来区间研究 summary"):
        cache.cleanup_runtime_rows()
    with sqlite3.connect(cache.path) as conn:
        assert conn.execute("SELECT COUNT(*) FROM market_scan_run").fetchone()[0] == 2


def _published_cache(
    tmp_path: Path,
    *,
    count: int,
    limit: int,
) -> tuple[SQLiteCache, tuple[int, ...]]:
    tmp_path.mkdir(parents=True, exist_ok=True)
    path = tmp_path / "runtime.sqlite3"
    cache = SQLiteCache(path, settings=Settings(cache_path=path, max_market_scan_runs=limit))
    with sqlite3.connect(path) as conn:
        run_ids = tuple(_insert_published_snapshot(conn, offset) for offset in range(count))
    return cache, run_ids


def _insert_published_snapshot(conn: sqlite3.Connection, offset: int) -> int:
    day = offset + 1
    run_updated = f"2026-08-{day:02d}T16:31:00+08:00"
    cursor = conn.execute(
        """
        INSERT INTO market_scan_run (
            status, trigger, mode, rule_version, as_of, data_date, quote_date,
            scope, total_count, processed_count, success_count, created_at,
            updated_at, started_at, finished_at, duration_ms,
            publication_diagnostics_json
        ) VALUES (
            'success', 'manual', 'official', 'retention-test-v1', ?, ?, ?,
            ?, 1, 1, 1, ?, ?, ?, ?, 60000, ?
        )
        """,
        (
            f"2026-08-{day:02d} 16:30:00",
            f"2026-08-{day:02d}",
            f"2026-08-{day:02d}",
            FULL_MARKET_SCOPE,
            f"2026-08-{day:02d}T16:00:00+08:00",
            run_updated,
            f"2026-08-{day:02d}T16:00:00+08:00",
            f"2026-08-{day:02d}T16:30:00+08:00",
            action_pass_publication_diagnostics().model_dump_json(),
        ),
    )
    run_id = _required_lastrowid(cursor)
    conn.execute(
        """
        INSERT INTO market_scan_result (
            run_id, symbol, code, market, name, status, rank, score, raw_score,
            trend_score, leader_score, data_quality_score, price, data_date,
            quote_timestamp, quote_observed_at, quote_source, kline_source,
            adjustment_mode, updated_at
        ) VALUES (?, ?, ?, 'SH', '测试股票', 'success', 1, 80, 80, 80, 80,
                  100, 10, ?, ?, ?, 'test', 'test', 'qfq', ?)
        """,
        (
            run_id,
            f"600{run_id:03d}.SH",
            f"600{run_id:03d}",
            f"2026-08-{day:02d}",
            f"2026-08-{day:02d} 15:00:00",
            f"2026-08-{day:02d}T15:00:01+08:00",
            f"2026-08-{day:02d}T16:29:00+08:00",
        ),
    )
    seal_market_scan_snapshot(
        conn,
        run_id,
        origin="publication",
        sealed_at=f"2026-08-{day:02d}T16:32:00+08:00",
    )
    return run_id


def _attach_reference(
    conn: sqlite3.Connection,
    reference: str,
    old_run_id: int,
    retained_run_id: int,
) -> None:
    handlers: dict[str, Callable[[], object]] = {
        "probability_outbox": lambda: _insert_probability_outbox(conn, old_run_id),
        "discovery_queue": lambda: _insert_discovery_queue(conn, old_run_id),
        "screen_alert": lambda: _insert_screen_alert(conn, old_run_id, retained_run_id),
        "strategy_execution": lambda: _insert_strategy_execution(conn, old_run_id),
        "strategy_schedule": lambda: _insert_strategy_schedule(conn, old_run_id),
        "strategy_schedule_run": lambda: _insert_strategy_schedule_run(conn, old_run_id),
        "dynamic_foreign_key": lambda: _insert_dynamic_foreign_key(conn, old_run_id),
        "retry_graph": lambda: _attach_retry_before_reseal(conn, old_run_id, retained_run_id),
    }
    handlers[reference]()


def _insert_probability_outbox(conn: sqlite3.Connection, run_id: int) -> None:
    conn.execute(
        """
        INSERT INTO market_scan_probability_capture_outbox (
            run_id, status, next_attempt_at, created_at, updated_at
        ) VALUES (?, 'pending', ?, ?, ?)
        """,
        (run_id, _STAMP, _STAMP, _STAMP),
    )


def _insert_discovery_queue(conn: sqlite3.Connection, run_id: int) -> None:
    conn.execute(
        """
        INSERT INTO watchlist (symbol, code, market, name, created_at, updated_at)
        VALUES ('600001.SH', '600001', 'SH', '测试', ?, ?)
        """,
        (_STAMP, _STAMP),
    )
    conn.execute(
        """
        INSERT INTO discovery_research_queue_source (
            symbol, source_run_id, source_preset_id, source_preset_revision,
            source_preset_name, preset_schema_version, preset_snapshot_json, enqueued_at
        ) VALUES ('600001.SH', ?, 1, 1, '测试', 2, '{}', ?)
        """,
        (run_id, _STAMP),
    )


def _insert_discovery_preset(conn: sqlite3.Connection) -> int:
    cursor = conn.execute(
        """
        INSERT INTO discovery_preset (
            name, schema_version, revision, criteria_json, sort_json, created_at, updated_at
        ) VALUES ('测试预设', 2, 1, '{}', '{}', ?, ?)
        """,
        (_STAMP, _STAMP),
    )
    return _required_lastrowid(cursor)


def _insert_screen_alert(conn: sqlite3.Connection, old: int, retained: int) -> None:
    preset_id = _insert_discovery_preset(conn)
    conn.execute(
        """
        INSERT INTO discovery_screen_alert_event (
            preset_id, current_run_id, previous_run_id, preset_revision,
            event_digest, entered_symbols_json, exited_symbols_json,
            suppressed_unrankable_symbols_json, created_at
        ) VALUES (?, ?, ?, 1, ?, '[]', '[]', '[]', ?)
        """,
        (preset_id, old, retained, "a" * 64, _STAMP),
    )


def _insert_strategy_base(conn: sqlite3.Connection) -> None:
    conn.execute(
        "INSERT INTO strategy_spec (id, current_revision, created_at, updated_at) VALUES (1, 1, ?, ?)",
        (_STAMP, _STAMP),
    )
    conn.execute(
        """
        INSERT INTO strategy_spec_version (
            strategy_id, revision, name, spec_json, fingerprint, created_at
        ) VALUES (1, 1, '测试策略', '{}', ?, ?)
        """,
        ("b" * 64, _STAMP),
    )


def _insert_strategy_execution(conn: sqlite3.Connection, run_id: int) -> int:
    _insert_strategy_base(conn)
    digest, origin = conn.execute(
        "SELECT snapshot_digest, snapshot_seal_origin FROM market_scan_run WHERE id = ?",
        (run_id,),
    ).fetchone()
    cursor = conn.execute(
        """
        INSERT INTO strategy_execution (
            strategy_id, strategy_revision, strategy_fingerprint,
            execution_fingerprint, kind, market_scan_run_id, source_snapshot_digest,
            source_snapshot_seal_origin, rule_version, data_as_of, data_date,
            cost_rule_fingerprint, status, summary_json, result_digest, created_at
        ) VALUES (1, 1, ?, ?, 'latest_scan', ?, ?, ?, 'retention-test-v1', ?,
                  '2026-08-01', ?, 'ready', '{}', ?, ?)
        """,
        ("b" * 64, "c" * 64, run_id, digest, origin, _STAMP, "d" * 64, "e" * 64, _STAMP),
    )
    return _required_lastrowid(cursor)


def _insert_strategy_schedule(conn: sqlite3.Connection, run_id: int) -> int:
    _insert_strategy_base(conn)
    cursor = conn.execute(
        """
        INSERT INTO strategy_schedule (
            strategy_id, strategy_revision, strategy_fingerprint, cadence, mode,
            notional_cash_cny, alert_conditions_json, enabled,
            last_market_scan_run_id, created_at, updated_at
        ) VALUES (1, 1, ?, 'daily_after_close', 'official', 100000, '{}', 1, ?, ?, ?)
        """,
        ("b" * 64, run_id, _STAMP, _STAMP),
    )
    return _required_lastrowid(cursor)


def _insert_strategy_schedule_run(conn: sqlite3.Connection, run_id: int) -> None:
    schedule_id = _insert_strategy_schedule(conn, run_id + 1000)
    conn.execute(
        """
        INSERT INTO strategy_schedule_run (
            schedule_id, market_scan_run_id, status, started_at, finished_at
        ) VALUES (?, ?, 'completed', ?, ?)
        """,
        (schedule_id, run_id, _STAMP, _STAMP),
    )


def _insert_dynamic_foreign_key(conn: sqlite3.Connection, run_id: int) -> None:
    conn.execute(
        """
        CREATE TABLE external_scan_audit (
            id INTEGER PRIMARY KEY,
            source_run_id INTEGER NOT NULL REFERENCES market_scan_run(id) ON DELETE RESTRICT
        )
        """
    )
    conn.execute("INSERT INTO external_scan_audit VALUES (1, ?)", (run_id,))


def _insert_retry_run(
    conn: sqlite3.Connection,
    parent_run_id: int,
    *,
    status: str,
) -> int:
    cursor = conn.execute(
        """
        INSERT INTO market_scan_run (
            retry_of_run_id, status, trigger, mode, rule_version, as_of,
            data_date, scope, created_at, updated_at, started_at, finished_at
        ) VALUES (?, ?, 'retry', 'official', 'retention-test-v1', ?,
                  '2026-08-10', 'test', ?, ?, ?, ?)
        """,
        (
            parent_run_id,
            status,
            _STAMP,
            _STAMP,
            _STAMP,
            _STAMP if status != "queued" else None,
            _STAMP if status == "failed" else None,
        ),
    )
    return _required_lastrowid(cursor)


def _attach_task_before_reseal(conn: sqlite3.Connection, run_id: int) -> None:
    cursor = conn.execute(
        "INSERT INTO task_run (task_name, status, started_at, finished_at) VALUES ('scan', 'success', ?, ?)",
        (_STAMP, _STAMP),
    )
    _mutate_and_reseal(conn, run_id, "task_run_id", _required_lastrowid(cursor))


def _attach_retry_before_reseal(
    conn: sqlite3.Connection,
    parent_run_id: int,
    child_run_id: int,
) -> None:
    _mutate_and_reseal(conn, child_run_id, "retry_of_run_id", parent_run_id)


def _mutate_and_reseal(
    conn: sqlite3.Connection,
    run_id: int,
    column: str,
    value: int,
) -> None:
    drop_market_scan_immutability_triggers(conn)
    conn.execute(
        f"""
        UPDATE market_scan_run
        SET snapshot_digest = NULL, snapshot_seal_origin = NULL,
            snapshot_sealed_at = NULL, {column} = ?
        WHERE id = ?
        """,
        (value, run_id),
    )
    seal_market_scan_snapshot(
        conn,
        run_id,
        origin="publication",
        sealed_at="2026-08-03T16:32:00+08:00",
    )
    create_market_scan_immutability_triggers(conn)


def _required_lastrowid(cursor: sqlite3.Cursor) -> int:
    assert cursor.lastrowid is not None
    return int(cursor.lastrowid)


def _write_individual_assessment(
    directory: Path,
    *,
    run_ids: tuple[int, ...],
) -> Path:
    unsigned = {
        "schema_version": "individual-upside-probability-assessment-v1",
        "generated_at": _STAMP,
        "payload": {"official_pit": {"sources": [{"run_id": run_id} for run_id in run_ids]}},
    }
    digest = sha256_hex(canonical_json_text(unsigned))
    artifact = {
        **unsigned,
        "integrity": {
            "algorithm": "sha256",
            "integrity_digest": digest,
            "notice": "test",
        },
    }
    target = directory / f"individual-upside-probability-assessment-{digest}.json"
    target.write_text(canonical_json_text(artifact), encoding="utf-8")
    return target
