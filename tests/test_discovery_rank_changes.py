from __future__ import annotations

import sqlite3

from app.db.schema import initialize_schema
from app.repositories.discovery import DiscoveryRepository
from app.services.discovery import DiscoveryService


def test_adjacent_same_rule_runs_report_rank_delta_new_and_exit(tmp_path) -> None:
    service, path = _service(tmp_path)
    previous_id = _seed_run(
        path,
        "rule-v1",
        [("600001.SH", 1), ("600002.SH", 2), ("600003.SH", 3)],
        data_date="2026-07-27",
        as_of="2026-07-27 15:00:00",
    )
    current_id = _seed_run(
        path,
        "rule-v1",
        [("600002.SH", 1), ("600004.SH", 2), ("600001.SH", 3)],
        data_date="2026-07-28",
        as_of="2026-07-28 15:00:00",
    )

    first_page = service.rank_changes(current_id, page=1, page_size=2)
    second_page = service.rank_changes(current_id, page=2, page_size=2)

    assert first_page.comparable is True
    assert first_page.reason is None
    assert first_page.previous_run_id == previous_id
    assert first_page.current_rule_version == "rule-v1"
    assert first_page.previous_rule_version == "rule-v1"
    assert first_page.total == 4
    assert first_page.page_count == 2
    assert [item.symbol for item in first_page.items] == ["600002.SH", "600004.SH"]
    assert first_page.items[0].movement == "up"
    assert first_page.items[0].previous_rank == 2
    assert first_page.items[0].current_rank == 1
    assert first_page.items[0].rank_delta == 1
    assert first_page.items[1].movement == "new"
    assert first_page.items[1].previous_rank is None
    assert first_page.items[1].rank_delta is None

    assert [item.symbol for item in second_page.items] == ["600001.SH", "600003.SH"]
    assert second_page.items[0].movement == "down"
    assert second_page.items[0].rank_delta == -2
    assert second_page.items[1].movement == "exit"
    assert second_page.items[1].current_rank is None
    assert second_page.items[1].previous_rank == 3


def test_skipped_result_is_unavailable_instead_of_false_exit(tmp_path) -> None:
    service, path = _service(tmp_path)
    _seed_run(
        path,
        "rule-v1",
        [("600001.SH", 1)],
        data_date="2026-07-27",
        as_of="2026-07-27 15:00:00",
    )
    current_id = _seed_run(
        path,
        "rule-v1",
        [("600001.SH", 1)],
        data_date="2026-07-28",
        as_of="2026-07-28 15:00:00",
    )
    with sqlite3.connect(path) as conn:
        conn.execute(
            """
            UPDATE market_scan_result
            SET status = 'skipped', rank = NULL, score = NULL,
                trend_score = NULL, data_quality_score = NULL
            WHERE run_id = ? AND symbol = '600001.SH'
            """,
            (current_id,),
        )

    result = service.rank_changes(current_id, page=1, page_size=50)

    assert result.comparable is True
    assert result.total == 1
    assert result.items[0].symbol == "600001.SH"
    assert result.items[0].movement == "unavailable"
    assert result.items[0].previous_rank == 1
    assert result.items[0].current_rank is None
    assert result.items[0].rank_delta is None


def test_adjacent_different_rule_versions_are_explicitly_incomparable(tmp_path) -> None:
    service, path = _service(tmp_path)
    previous_id = _seed_run(
        path,
        "rule-v1",
        [("600001.SH", 1)],
        data_date="2026-07-27",
        as_of="2026-07-27 15:00:00",
    )
    current_id = _seed_run(
        path,
        "rule-v2",
        [("600001.SH", 2)],
        data_date="2026-07-28",
        as_of="2026-07-28 15:00:00",
    )

    result = service.rank_changes(current_id, page=1, page_size=50)

    assert result.comparable is False
    assert result.reason == "rule_version_mismatch"
    assert result.previous_run_id == previous_id
    assert result.current_rule_version == "rule-v2"
    assert result.previous_rule_version == "rule-v1"
    assert result.items == []
    assert result.total == 0
    assert result.page_count == 0


def test_rule_comparison_does_not_skip_an_intervening_incompatible_run(tmp_path) -> None:
    service, path = _service(tmp_path)
    _seed_run(
        path,
        "rule-v1",
        [("600001.SH", 3)],
        data_date="2026-07-15",
        as_of="2026-07-15 15:00:00",
    )
    incompatible_id = _seed_run(
        path,
        "rule-v2",
        [("600001.SH", 2)],
        data_date="2026-07-16",
        as_of="2026-07-16 15:00:00",
    )
    current_id = _seed_run(
        path,
        "rule-v1",
        [("600001.SH", 1)],
        data_date="2026-07-17",
        as_of="2026-07-17 15:00:00",
    )

    result = service.rank_changes(current_id, page=1, page_size=50)

    assert result.comparable is False
    assert result.reason == "rule_version_mismatch"
    assert result.previous_run_id == incompatible_id
    assert result.previous_rule_version == "rule-v2"
    assert result.items == []


def test_first_completed_run_has_no_previous_comparison(tmp_path) -> None:
    service, path = _service(tmp_path)
    current_id = _seed_run(
        path,
        "rule-v1",
        [("600001.SH", 1)],
        data_date="2026-07-28",
        as_of="2026-07-28 15:00:00",
    )

    result = service.rank_changes(current_id, page=1, page_size=50)

    assert result.comparable is False
    assert result.reason == "no_previous_run"
    assert result.previous_run_id is None
    assert result.previous_rule_version is None


def test_historical_backfill_is_selected_by_market_time_even_with_a_later_id(tmp_path) -> None:
    service, path = _service(tmp_path)
    current_id = _seed_run(
        path,
        "rule-v1",
        [("600001.SH", 2)],
        data_date="2026-07-28",
        as_of="2026-07-28 15:00:00",
    )
    backfill_id = _seed_run(
        path,
        "rule-v1",
        [("600001.SH", 1)],
        data_date="2026-07-27",
        as_of="2026-07-27 15:00:00",
    )

    result = service.rank_changes(current_id, page=1, page_size=50)

    assert backfill_id > current_id
    assert result.comparable is True
    assert result.previous_run_id == backfill_id
    assert result.items[0].rank_delta == -1


def test_different_scope_is_not_a_previous_period(tmp_path) -> None:
    service, path = _service(tmp_path)
    _seed_run(
        path,
        "rule-v1",
        [("600001.SH", 1)],
        data_date="2026-07-27",
        as_of="2026-07-27 15:00:00",
        scope="SZ-only",
    )
    current_id = _seed_run(
        path,
        "rule-v1",
        [("600001.SH", 2)],
        data_date="2026-07-28",
        as_of="2026-07-28 15:00:00",
        scope="ALL",
    )

    result = service.rank_changes(current_id, page=1, page_size=50)

    assert result.comparable is False
    assert result.reason == "no_previous_run"
    assert result.previous_run_id is None


def test_same_day_reruns_and_future_batches_are_not_previous_periods(tmp_path) -> None:
    service, path = _service(tmp_path)
    _seed_run(
        path,
        "rule-v1",
        [("600001.SH", 1)],
        data_date="2026-07-28",
        as_of="2026-07-28 09:00:00",
    )
    current_id = _seed_run(
        path,
        "rule-v1",
        [("600001.SH", 2)],
        data_date="2026-07-28",
        as_of="2026-07-28 15:00:00",
    )
    _seed_run(
        path,
        "rule-v1",
        [("600001.SH", 3)],
        data_date="2026-07-29",
        as_of="2026-07-29 15:00:00",
    )

    result = service.rank_changes(current_id, page=1, page_size=50)

    assert result.comparable is False
    assert result.reason == "no_previous_run"
    assert result.previous_run_id is None


def _service(tmp_path) -> tuple[DiscoveryService, object]:
    path = tmp_path / "runtime.sqlite3"
    with sqlite3.connect(path) as conn:
        initialize_schema(conn)
    return DiscoveryService(DiscoveryRepository(path)), path


def _seed_run(
    path,
    rule_version: str,
    rows: list[tuple[str, int]],
    *,
    data_date: str,
    as_of: str,
    scope: str = "ALL",
) -> int:
    timestamp = "2026-07-28T01:00:00.000000Z"
    with sqlite3.connect(path) as conn:
        run_id = conn.execute(
            """
            INSERT INTO market_scan_run (
                status, trigger, rule_version, as_of, data_date, scope,
                created_at, updated_at, finished_at
            ) VALUES ('success', 'manual', ?, ?, ?, ?, ?, ?, ?)
            """,
            (rule_version, as_of, data_date, scope, timestamp, timestamp, timestamp),
        ).lastrowid
        assert run_id is not None
        for symbol, rank in rows:
            code, market = symbol.split(".")
            conn.execute(
                """
                INSERT INTO market_scan_result (
                    run_id, symbol, code, market, name, status, rank, score,
                    trend_score, data_quality_score, updated_at
                ) VALUES (?, ?, ?, ?, ?, 'success', ?, 80, 80, 80, ?)
                """,
                (run_id, symbol, code, market, f"样本{code}", rank, timestamp),
            )
    return int(run_id)
