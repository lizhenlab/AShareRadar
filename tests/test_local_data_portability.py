from __future__ import annotations

from copy import deepcopy
from pathlib import Path
import sqlite3

import pytest

from app.models.local_data import USER_DATA_TABLE_ALLOWLIST, UserDataBundle
from app.services.cache import SQLiteCache
from app.services.user_data_portability import export_user_data, import_user_data


def test_export_contains_only_exact_user_data_allowlist(tmp_path: Path) -> None:
    path = tmp_path / "source.sqlite3"
    SQLiteCache(path)
    _insert_watchlist(path, "600519.SH", note="source")

    bundle = export_user_data(path)

    assert set(bundle.tables) == USER_DATA_TABLE_ALLOWLIST
    assert bundle.row_counts["watchlist"] == 1
    assert "quote_snapshot" not in bundle.tables
    assert "provider_status" not in bundle.tables
    assert "schema_migration" not in bundle.tables
    assert bundle.tables["watchlist"].column_types is not None
    assert bundle.tables["watchlist"].column_types["symbol"] == "TEXT"


def test_merge_dry_run_reports_changes_without_writing_and_commit_is_source_wins(tmp_path: Path) -> None:
    source = tmp_path / "source.sqlite3"
    target = tmp_path / "target.sqlite3"
    SQLiteCache(source)
    SQLiteCache(target)
    _insert_watchlist(source, "600519.SH", note="source")
    _insert_watchlist(target, "600519.SH", note="target")
    bundle = export_user_data(source)

    preview = import_user_data(target, bundle, mode="merge", dry_run=True)

    assert preview.committed is False
    assert preview.tables["watchlist"].updated == 1
    assert _watchlist_note(target, "600519.SH") == "target"

    result = import_user_data(target, bundle, mode="merge", dry_run=False)

    assert result.committed is True
    assert result.conflict_strategy == "remap_surrogate_ids_source_wins_on_stable_keys"
    assert result.tables["watchlist"].remapped == 0
    assert _watchlist_note(target, "600519.SH") == "source"


def test_replace_requires_complete_snapshot_and_removes_rows_absent_from_source(tmp_path: Path) -> None:
    source = tmp_path / "source.sqlite3"
    target = tmp_path / "target.sqlite3"
    SQLiteCache(source)
    SQLiteCache(target)
    _insert_watchlist(source, "600519.SH", note="kept")
    _insert_watchlist(target, "000001.SZ", note="removed")
    bundle = export_user_data(source)
    incomplete_payload = bundle.model_dump(mode="json")
    incomplete_payload["tables"].pop("stock_note")
    incomplete_payload["row_counts"].pop("stock_note")
    incomplete = UserDataBundle.model_validate(incomplete_payload)

    with pytest.raises(ValueError, match="replace 模式必须包含全部用户数据表"):
        import_user_data(target, incomplete, mode="replace", dry_run=False)

    import_user_data(target, bundle, mode="replace", dry_run=False)

    assert _watchlist_symbols(target) == ["600519.SH"]


def test_import_rejects_column_type_and_primary_key_drift_before_writing(
    tmp_path: Path,
) -> None:
    source = tmp_path / "source.sqlite3"
    target = tmp_path / "target.sqlite3"
    SQLiteCache(source)
    SQLiteCache(target)
    _insert_watchlist(source, "600519.SH", note="source")
    payload = export_user_data(source).model_dump(mode="json")
    payload["tables"]["watchlist"]["rows"] = []
    payload["row_counts"]["watchlist"] = 0
    column_drift = deepcopy(payload)
    note_index = column_drift["tables"]["watchlist"]["columns"].index("note")
    column_drift["tables"]["watchlist"]["columns"][note_index] = "other_note"
    column_drift["tables"]["watchlist"]["column_types"]["other_note"] = column_drift["tables"]["watchlist"]["column_types"].pop("note")
    type_drift = deepcopy(payload)
    type_drift["tables"]["watchlist"]["column_types"]["note"] = "INTEGER"
    primary_key_drift = deepcopy(payload)
    primary_key_drift["tables"]["watchlist"]["primary_key"] = ["code"]

    for drifted_payload, message in (
        (column_drift, "列结构"),
        (type_drift, "列类型"),
        (primary_key_drift, "主键结构"),
    ):
        bundle = UserDataBundle.model_validate(drifted_payload)
        with pytest.raises(ValueError, match=message):
            import_user_data(target, bundle, mode="merge", dry_run=False)

    assert _watchlist_symbols(target) == []


def test_migrated_export_imports_into_fresh_database_with_different_column_order(
    tmp_path: Path,
) -> None:
    source = tmp_path / "migrated.sqlite3"
    target = tmp_path / "fresh.sqlite3"
    _create_legacy_watchlist_database(source)
    SQLiteCache(source)
    SQLiteCache(target)
    _insert_watchlist(source, "600519.SH", note="migrated")

    source_columns = _table_columns(source, "watchlist")
    target_columns = _table_columns(target, "watchlist")
    assert source_columns != target_columns
    assert set(source_columns) == set(target_columns)

    bundle = export_user_data(source)
    preview = import_user_data(target, bundle, mode="merge", dry_run=True)

    assert preview.tables["watchlist"].inserted == 1
    assert _watchlist_symbols(target) == []

    import_user_data(target, bundle, mode="merge", dry_run=False)

    assert _watchlist_note(target, "600519.SH") == "migrated"


def test_merge_remaps_colliding_surrogate_ids_and_dependent_relationships(
    tmp_path: Path,
) -> None:
    source = tmp_path / "source.sqlite3"
    target = tmp_path / "target.sqlite3"
    SQLiteCache(source)
    SQLiteCache(target)
    _insert_alert_chain(source, "600519.SH", marker="source")
    _insert_alert_chain(target, "000001.SZ", marker="target")
    _insert_stock_note(source, "600519.SH", marker="source")
    _insert_stock_note(target, "000001.SZ", marker="target")
    source_advice = _insert_advice(source, "600519.SH", marker="source")
    target_advice = _insert_advice(target, "000001.SZ", marker="target")
    source_plan = _insert_review_plan(source, source_advice, "600519.SH", marker="source")
    target_plan = _insert_review_plan(target, target_advice, "000001.SZ", marker="target")
    _insert_review_result(source, source_plan, source_advice, "600519.SH", marker="source")
    _insert_review_result(target, target_plan, target_advice, "000001.SZ", marker="target")
    bundle = export_user_data(source)

    preview = import_user_data(target, bundle, mode="merge", dry_run=True)

    remapped_tables = (
        "alert_rule",
        "alert_event",
        "stock_note",
        "advice_history",
        "advice_review_plan",
        "advice_review_result",
    )
    for table in remapped_tables:
        assert preview.tables[table].inserted == 1
        assert preview.tables[table].updated == 0
        assert preview.tables[table].remapped == 1
    assert preview.totals.remapped == len(remapped_tables)
    assert _table_count(target, "advice_history") == 1
    assert _table_count(target, "advice_review_result") == 1

    result = import_user_data(target, bundle, mode="merge", dry_run=False)

    assert result.tables == preview.tables
    assert result.totals == preview.totals
    assert _joined_alert_markers(target) == {("source", "source"), ("target", "target")}
    assert _joined_review_markers(target) == {
        ("source", "source", "source"),
        ("target", "target", "target"),
    }
    assert _stock_note_markers(target) == {"source", "target"}


def test_repeated_merge_is_idempotent_for_surrogate_rows(tmp_path: Path) -> None:
    source = tmp_path / "source.sqlite3"
    target = tmp_path / "target.sqlite3"
    SQLiteCache(source)
    SQLiteCache(target)
    _insert_watchlist(source, "600519.SH", note="source")
    _insert_alert_chain(source, "600519.SH", marker="source")
    _insert_stock_note(source, "600519.SH", marker="source")
    advice_id = _insert_advice(source, "600519.SH", marker="source")
    plan_id = _insert_review_plan(source, advice_id, "600519.SH", marker="source")
    _insert_review_result(source, plan_id, advice_id, "600519.SH", marker="source")
    bundle = export_user_data(source)

    import_user_data(target, bundle, mode="merge", dry_run=False)
    preview = import_user_data(target, bundle, mode="merge", dry_run=True)

    for table in USER_DATA_TABLE_ALLOWLIST:
        assert preview.tables[table].inserted == 0
        assert preview.tables[table].updated == 0
        assert preview.tables[table].remapped == 0
        assert preview.tables[table].unchanged == 1
        assert _table_count(target, table) == 1


def test_merge_rejects_child_rows_without_bundled_surrogate_parent(
    tmp_path: Path,
) -> None:
    source = tmp_path / "source.sqlite3"
    target = tmp_path / "target.sqlite3"
    SQLiteCache(source)
    SQLiteCache(target)
    _insert_alert_chain(source, "600519.SH", marker="source")
    _insert_alert_chain(target, "000001.SZ", marker="target")
    payload = export_user_data(source).model_dump(mode="json")
    payload["tables"].pop("alert_rule")
    payload["row_counts"].pop("alert_rule")
    child_only_bundle = UserDataBundle.model_validate(payload)

    with pytest.raises(ValueError, match="外键约束要求导入包同时包含 alert_rule"):
        import_user_data(target, child_only_bundle, mode="merge", dry_run=False)

    assert _joined_alert_markers(target) == {("target", "target")}


def test_v1_bundle_without_later_price_provenance_columns_is_upgraded(
    tmp_path: Path,
) -> None:
    source = tmp_path / "source.sqlite3"
    target = tmp_path / "target.sqlite3"
    SQLiteCache(source)
    SQLiteCache(target)
    advice_id = _insert_advice(source, "600519.SH", marker="legacy")
    plan_id = _insert_review_plan(source, advice_id, "600519.SH", marker="legacy")
    _insert_review_result(source, plan_id, advice_id, "600519.SH", marker="legacy")
    payload = export_user_data(source).model_dump(mode="json")
    _remove_bundle_columns(
        payload,
        "advice_history",
        {
            "kline_adjustment_mode",
            "kline_anchor_date",
            "kline_anchor_close",
            "kline_data_version",
            "kline_contract_version",
        },
    )
    _remove_bundle_columns(
        payload,
        "advice_review_plan",
        {
            "snapshot_adjustment_mode",
            "snapshot_anchor_date",
            "snapshot_anchor_close",
            "snapshot_data_version",
            "snapshot_contract_version",
        },
    )
    _remove_bundle_columns(
        payload,
        "advice_review_result",
        {
            "snapshot_adjustment_mode",
            "snapshot_anchor_date",
            "snapshot_anchor_close",
            "snapshot_data_version",
            "snapshot_contract_version",
            "evaluation_adjustment_mode",
            "evaluation_data_version",
            "evaluation_contract_version",
            "anchor_evaluation_close",
            "price_scale_factor",
            "normalized_entry_price",
            "normalized_target_price",
            "normalized_stop_price",
        },
    )

    import_user_data(
        target,
        UserDataBundle.model_validate(payload),
        mode="merge",
        dry_run=False,
    )

    with sqlite3.connect(target) as conn:
        advice = conn.execute("SELECT kline_adjustment_mode, kline_anchor_close FROM advice_history").fetchone()
        plan = conn.execute("SELECT snapshot_adjustment_mode, snapshot_anchor_close FROM advice_review_plan").fetchone()
        result = conn.execute(
            """
            SELECT snapshot_adjustment_mode, evaluation_adjustment_mode,
                   price_scale_factor, normalized_entry_price
            FROM advice_review_result
            """
        ).fetchone()
    assert advice == ("unknown", None)
    assert plan == ("unknown", None)
    assert result == ("unknown", "unknown", None, None)


def test_foreign_key_failure_rolls_back_every_table(tmp_path: Path) -> None:
    source = tmp_path / "source.sqlite3"
    target = tmp_path / "target.sqlite3"
    SQLiteCache(source)
    SQLiteCache(target)
    _insert_watchlist(source, "600519.SH", note="must rollback")
    advice_id = _insert_advice(source, "600519.SH", marker="broken")
    _insert_review_plan(source, advice_id, "600519.SH", marker="broken")
    payload = export_user_data(source).model_dump(mode="json")
    payload["tables"].pop("advice_history")
    payload["row_counts"].pop("advice_history")
    broken_bundle = UserDataBundle.model_validate(payload)

    with pytest.raises(ValueError, match="外键约束"):
        import_user_data(target, broken_bundle, mode="merge", dry_run=False)

    assert _watchlist_symbols(target) == []
    with sqlite3.connect(target) as conn:
        assert conn.execute("SELECT COUNT(*) FROM advice_review_plan").fetchone()[0] == 0


def _insert_watchlist(path: Path, symbol: str, *, note: str) -> None:
    code, market = symbol.split(".")
    with sqlite3.connect(path) as conn:
        conn.execute(
            """
            INSERT INTO watchlist (
                symbol, code, market, name, note, group_name, pinned,
                research_status, priority, unread_change_count, created_at, updated_at
            ) VALUES (?, ?, ?, ?, ?, '默认', 0, 'watching', 'medium', 0, ?, ?)
            """,
            (symbol, code, market, f"测试{code}", note, "2026-07-16 10:00:00", "2026-07-16 10:00:00"),
        )


def _insert_alert_chain(path: Path, symbol: str, *, marker: str) -> tuple[int, int]:
    code, market = symbol.split(".")
    with sqlite3.connect(path) as conn:
        rule = conn.execute(
            """
            INSERT INTO alert_rule (
                symbol, code, market, stock_name, name, condition_type, threshold,
                note, enabled, last_state, trigger_count, cooldown_seconds, created_at, updated_at
            ) VALUES (?, ?, ?, ?, ?, 'price_above', 101, ?, 1, '等待', 0, 300, ?, ?)
            """,
            (symbol, code, market, marker, marker, marker, "2026-07-16 10:00:00", "2026-07-16 10:00:00"),
        )
        event = conn.execute(
            """
            INSERT INTO alert_event (
                rule_id, symbol, code, market, stock_name, name, condition_type,
                event_type, message, price, change_pct, threshold, created_at
            ) VALUES (?, ?, ?, ?, ?, ?, 'price_above', '触发', ?, 102, 1, 101, ?)
            """,
            (int(rule.lastrowid), symbol, code, market, marker, marker, marker, "2026-07-16 10:01:00"),
        )
        return int(rule.lastrowid), int(event.lastrowid)


def _insert_stock_note(path: Path, symbol: str, *, marker: str) -> int:
    code, market = symbol.split(".")
    with sqlite3.connect(path) as conn:
        cursor = conn.execute(
            """
            INSERT INTO stock_note (
                symbol, code, market, name, note_type, content, visible, created_at, updated_at
            ) VALUES (?, ?, ?, ?, 'research', ?, 1, ?, ?)
            """,
            (symbol, code, market, marker, marker, "2026-07-16 10:00:00", "2026-07-16 10:00:00"),
        )
        return int(cursor.lastrowid)


def _insert_advice(path: Path, symbol: str, *, marker: str) -> int:
    code, market = symbol.split(".")
    with sqlite3.connect(path) as conn:
        cursor = conn.execute(
            """
            INSERT INTO advice_history (
                symbol, code, market, name, action, confidence, trend_score,
                trend_label, risk_level, price, change_pct, support, resistance,
                data_quality_score, data_quality_level, reason, summary, created_at,
                market_time
            ) VALUES (?, ?, ?, ?, '等待信号', 60, 55, '中性观察', '可控风险',
                      100, 0, 95, 110, 90, '优秀', ?, ?, ?, ?)
            """,
            (symbol, code, market, marker, marker, marker, "2026-07-16 10:00:00", "2026-07-16 09:59:00"),
        )
        return int(cursor.lastrowid)


def _insert_review_plan(
    path: Path,
    advice_id: int,
    symbol: str,
    *,
    marker: str,
) -> int:
    with sqlite3.connect(path) as conn:
        cursor = conn.execute(
            """
            INSERT INTO advice_review_plan (
                advice_id, symbol, snapshot_market_time, snapshot_price,
                hypothesis, trigger_condition, invalidation_condition,
                target_price, stop_price, horizon_days, evidence_refs_json,
                revision, created_at, updated_at
            ) VALUES (?, ?, '2026-07-16 09:59:00', 100, ?, ?, ?, 110, 95, 5,
                      '[]', 1, '2026-07-16 10:00:00', '2026-07-16 10:00:00')
            """,
            (advice_id, symbol, marker, marker, marker),
        )
        return int(cursor.lastrowid)


def _insert_review_result(
    path: Path,
    plan_id: int,
    advice_id: int,
    symbol: str,
    *,
    marker: str,
) -> int:
    with sqlite3.connect(path) as conn:
        cursor = conn.execute(
            """
            INSERT INTO advice_review_result (
                plan_id, plan_revision, advice_id, symbol, snapshot_market_time,
                as_of, evaluated_at, status, conclusion, rule_version,
                entry_price, target_price, stop_price, horizon_days,
                visible_bar_count, available_forward_days, target_hit, stop_hit
            ) VALUES (
                ?, 1, ?, ?, '2026-07-16 09:59:00', '2026-07-17',
                '2026-07-17 16:00:00', 'evaluated', 'horizon_gain', ?,
                100, 110, 95, 5, 1, 1, 0, 0
            )
            """,
            (plan_id, advice_id, symbol, marker),
        )
        return int(cursor.lastrowid)


def _create_legacy_watchlist_database(path: Path) -> None:
    with sqlite3.connect(path) as conn:
        conn.execute(
            """
            CREATE TABLE watchlist (
                symbol TEXT PRIMARY KEY,
                code TEXT NOT NULL,
                market TEXT NOT NULL,
                name TEXT NOT NULL,
                note TEXT,
                group_name TEXT NOT NULL DEFAULT '默认',
                pinned INTEGER NOT NULL DEFAULT 0,
                created_at TEXT NOT NULL,
                updated_at TEXT NOT NULL
            )
            """
        )


def _remove_bundle_columns(payload: dict, table: str, columns: set[str]) -> None:
    table_payload = payload["tables"][table]
    table_payload["columns"] = [column for column in table_payload["columns"] if column not in columns]
    for column in columns:
        table_payload["column_types"].pop(column)
    for row in table_payload["rows"]:
        for column in columns:
            row.pop(column)


def _watchlist_note(path: Path, symbol: str) -> str | None:
    with sqlite3.connect(path) as conn:
        row = conn.execute("SELECT note FROM watchlist WHERE symbol = ?", (symbol,)).fetchone()
    return row[0] if row else None


def _watchlist_symbols(path: Path) -> list[str]:
    with sqlite3.connect(path) as conn:
        return [row[0] for row in conn.execute("SELECT symbol FROM watchlist ORDER BY symbol")]


def _table_columns(path: Path, table: str) -> list[str]:
    with sqlite3.connect(path) as conn:
        return [str(row[1]) for row in conn.execute(f"PRAGMA table_info({table})")]


def _table_count(path: Path, table: str) -> int:
    with sqlite3.connect(path) as conn:
        return int(conn.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0])


def _joined_alert_markers(path: Path) -> set[tuple[str, str]]:
    with sqlite3.connect(path) as conn:
        return {
            (str(row[0]), str(row[1]))
            for row in conn.execute(
                """
                SELECT alert_rule.name, alert_event.message
                FROM alert_event
                JOIN alert_rule ON alert_rule.id = alert_event.rule_id
                """
            )
        }


def _joined_review_markers(path: Path) -> set[tuple[str, str, str]]:
    with sqlite3.connect(path) as conn:
        return {
            (str(row[0]), str(row[1]), str(row[2]))
            for row in conn.execute(
                """
                SELECT advice_history.summary, advice_review_plan.hypothesis,
                       advice_review_result.rule_version
                FROM advice_review_result
                JOIN advice_review_plan ON advice_review_plan.id = advice_review_result.plan_id
                JOIN advice_history
                  ON advice_history.id = advice_review_plan.advice_id
                 AND advice_history.id = advice_review_result.advice_id
                """
            )
        }


def _stock_note_markers(path: Path) -> set[str]:
    with sqlite3.connect(path) as conn:
        return {str(row[0]) for row in conn.execute("SELECT content FROM stock_note")}
