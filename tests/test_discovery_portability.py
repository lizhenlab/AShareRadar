from __future__ import annotations

import sqlite3

from app.db.schema import initialize_schema
from app.models.local_data import USER_DATA_TABLE_ALLOWLIST
from app.services.user_data_portability import export_user_data, import_user_data


def test_discovery_presets_and_queue_provenance_round_trip_without_scan_history(tmp_path) -> None:
    source = tmp_path / "source.sqlite3"
    target = tmp_path / "target.sqlite3"
    for path in (source, target):
        with sqlite3.connect(path) as conn:
            initialize_schema(conn)

    assert "discovery_preset" in USER_DATA_TABLE_ALLOWLIST
    assert "discovery_research_queue_source" in USER_DATA_TABLE_ALLOWLIST

    timestamp = "2026-07-28T01:00:00.000000Z"
    with sqlite3.connect(source) as conn:
        conn.execute(
            """
            INSERT INTO watchlist (
                symbol, code, market, name, research_status, created_at, updated_at
            ) VALUES ('600519.SH', '600519', 'SH', '贵州茅台', 'to_research', ?, ?)
            """,
            (timestamp, timestamp),
        )
        preset_id = conn.execute(
            """
            INSERT INTO discovery_preset (
                name, schema_version, revision, criteria_json, sort_json,
                created_at, updated_at
            ) VALUES ('高质量白酒', 1, 2, '{"industry":["白酒"]}',
                      '[{"field":"score","order":"desc"}]', ?, ?)
            """,
            (timestamp, timestamp),
        ).lastrowid
        assert preset_id is not None
        conn.execute(
            """
            INSERT INTO discovery_research_queue_source (
                symbol, source_run_id, source_preset_id, source_preset_revision,
                source_preset_name, preset_schema_version, preset_snapshot_json,
                enqueued_at
            ) VALUES ('600519.SH', 424242, ?, 2, '高质量白酒', 1,
                      '{"criteria":{"industry":["白酒"]}}', ?)
            """,
            (preset_id, timestamp),
        )

    bundle = export_user_data(source)
    assert len(bundle.tables["discovery_preset"].rows) == 1
    assert len(bundle.tables["discovery_research_queue_source"].rows) == 1

    result = import_user_data(target, bundle, mode="replace", dry_run=False)

    assert result.committed is True
    with sqlite3.connect(target) as conn:
        preset = conn.execute(
            "SELECT name, revision FROM discovery_preset"
        ).fetchone()
        provenance = conn.execute(
            """
            SELECT symbol, source_run_id, source_preset_revision, source_preset_name
            FROM discovery_research_queue_source
            """
        ).fetchone()
    assert preset == ("高质量白酒", 2)
    assert provenance == ("600519.SH", 424242, 2, "高质量白酒")


def test_discovery_preset_merge_matches_case_insensitive_names(tmp_path) -> None:
    source = tmp_path / "source.sqlite3"
    target = tmp_path / "target.sqlite3"
    for path in (source, target):
        with sqlite3.connect(path) as conn:
            initialize_schema(conn)

    timestamp = "2026-07-28T01:00:00.000000Z"
    with sqlite3.connect(source) as conn:
        conn.execute(
            """
            INSERT INTO discovery_preset (
                name, criteria_json, sort_json, created_at, updated_at
            ) VALUES ('Alpha', '{"score":{"min":80}}',
                      '[{"field":"score","order":"desc"}]', ?, ?)
            """,
            (timestamp, timestamp),
        )
    with sqlite3.connect(target) as conn:
        conn.execute(
            """
            INSERT INTO discovery_preset (
                name, criteria_json, sort_json, created_at, updated_at
            ) VALUES ('alpha', '{"score":{"min":20}}',
                      '[{"field":"score","order":"desc"}]', ?, ?)
            """,
            (timestamp, timestamp),
        )

    result = import_user_data(
        target,
        export_user_data(source),
        mode="merge",
        dry_run=False,
    )

    assert result.tables["discovery_preset"].updated == 1
    with sqlite3.connect(target) as conn:
        rows = conn.execute(
            "SELECT name, criteria_json FROM discovery_preset"
        ).fetchall()
    assert rows == [("Alpha", '{"score":{"min":80}}')]
