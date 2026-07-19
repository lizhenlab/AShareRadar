"""Versioned export and transactional import of local user-owned data."""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from datetime import datetime, timezone
import hashlib
import json
import math
from pathlib import Path
import re
import sqlite3
from typing import Literal, cast

from pydantic import JsonValue

from app.models.local_data import (
    CORE_USER_DATA_TABLES,
    LOCAL_DATA_BUNDLE_KIND,
    LOCAL_DATA_BUNDLE_VERSION,
    LocalDataImportMode,
    LocalDataImportResult,
    LocalDataTableBundle,
    LocalDataTableImportPreview,
    USER_DATA_TABLE_ALLOWLIST,
    UserDataBundle,
)


SQLITE_BUSY_TIMEOUT_MS = 15_000
CONFLICT_STRATEGY = "remap_surrogate_ids_source_wins_on_stable_keys"
_IDENTIFIER = re.compile(r"[A-Za-z_][A-Za-z0-9_]*\Z")
_SURROGATE_PRIMARY_KEYS = {
    "alert_rule": "id",
    "alert_event": "id",
    "stock_note": "id",
    "advice_history": "id",
    "advice_review_plan": "id",
    "advice_review_result": "id",
}
_SOURCE_WINS_KEYS = {
    "watchlist": ("symbol",),
    "advice_review_plan": ("advice_id",),
    "advice_review_result": ("plan_id", "plan_revision", "as_of", "rule_version"),
}
_RELATIONSHIPS = {
    "alert_event": (("rule_id", "alert_rule"),),
    "advice_review_plan": (("advice_id", "advice_history"),),
    "advice_review_result": (
        ("plan_id", "advice_review_plan"),
        ("advice_id", "advice_history"),
    ),
}
_V1_COMPAT_COLUMN_DEFAULTS: dict[str, dict[str, JsonValue]] = {
    "advice_history": {
        "kline_adjustment_mode": "unknown",
        "kline_anchor_date": None,
        "kline_anchor_close": None,
        "kline_data_version": "unknown",
        "kline_contract_version": "unknown",
    },
    "advice_review_plan": {
        "snapshot_adjustment_mode": "unknown",
        "snapshot_anchor_date": None,
        "snapshot_anchor_close": None,
        "snapshot_data_version": "unknown",
        "snapshot_contract_version": "unknown",
    },
    "advice_review_result": {
        "snapshot_adjustment_mode": "unknown",
        "snapshot_anchor_date": None,
        "snapshot_anchor_close": None,
        "snapshot_data_version": "unknown",
        "snapshot_contract_version": "unknown",
        "evaluation_adjustment_mode": "unknown",
        "evaluation_data_version": "unknown",
        "evaluation_contract_version": "unknown",
        "anchor_evaluation_close": None,
        "price_scale_factor": None,
        "normalized_entry_price": None,
        "normalized_target_price": None,
        "normalized_stop_price": None,
    },
}
_RowOperation = Literal["insert", "update", "unchanged"]
ImportStateCallback = Callable[[str, LocalDataImportResult], None]


@dataclass(frozen=True)
class _PreparedRow:
    operation: _RowOperation
    values: dict[str, object]


@dataclass(frozen=True)
class _PreparedTable:
    bundle: LocalDataTableBundle
    rows: tuple[_PreparedRow, ...]
    preview: LocalDataTableImportPreview


@dataclass
class _SurrogateMergeState:
    primary_key: str
    stable_columns: tuple[str, ...] | None
    existing_by_id: dict[object, dict[str, object]]
    existing_by_stable: dict[tuple[object, ...], dict[str, object]]
    used_ids: set[object]
    next_id: int
    prepared_rows: list[_PreparedRow]
    id_map: dict[object, object]
    remapped: int = 0


def export_user_data(path: Path) -> UserDataBundle:
    database_path = _require_database(path)
    with _connect(database_path) as conn:
        conn.execute("BEGIN")
        bundle = _export_user_data_from_connection(conn)
        conn.rollback()
    return bundle


def _export_user_data_from_connection(conn: sqlite3.Connection) -> UserDataBundle:
    table_names = available_user_tables(conn)
    if not table_names:
        raise ValueError("本地数据库中没有可导出的用户数据表")
    tables = {name: _export_table(conn, name) for name in table_names}
    schema_version = int(conn.execute("PRAGMA schema_version").fetchone()[0])
    return UserDataBundle(
        kind=LOCAL_DATA_BUNDLE_KIND,
        version=LOCAL_DATA_BUNDLE_VERSION,
        exported_at=_utc_now_text(),
        source_schema_version=schema_version,
        tables=tables,
        row_counts={name: len(table.rows) for name, table in tables.items()},
    )


def user_data_state_digest(path: Path) -> str:
    database_path = _require_database(path)
    with _connect(database_path) as conn:
        conn.execute("BEGIN")
        digest = _user_data_state_digest_from_connection(conn)
        conn.rollback()
    return digest


def _user_data_state_digest_from_connection(conn: sqlite3.Connection) -> str:
    payload = _export_user_data_from_connection(conn).model_dump(mode="json")
    payload.pop("exported_at", None)
    encoded = json.dumps(payload, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()


def import_user_data(
    path: Path,
    bundle: UserDataBundle,
    *,
    mode: LocalDataImportMode = "merge",
    dry_run: bool = True,
    on_validated_state: ImportStateCallback | None = None,
) -> LocalDataImportResult:
    if mode not in {"merge", "replace"}:
        raise ValueError("导入模式必须是 merge 或 replace")
    database_path = _require_database(path)
    with _connect(database_path) as conn:
        try:
            conn.execute("BEGIN IMMEDIATE")
            conn.execute("PRAGMA defer_foreign_keys = ON")
            database_digest = _user_data_state_digest_from_connection(conn)
            table_names, normalized_tables = _validate_bundle_for_database(conn, bundle, mode=mode)
            _validate_in_bundle_relationships(normalized_tables)
            prepared = _prepare_bundle(conn, normalized_tables, table_names, mode)
            previews = {name: prepared[name].preview for name in table_names}
            result = LocalDataImportResult(
                bundle_version=bundle.version,
                mode=mode,
                dry_run=dry_run,
                committed=not dry_run,
                conflict_strategy=CONFLICT_STRATEGY,
                tables=previews,
                totals=_sum_previews(previews.values()),
            )
            if dry_run:
                _apply_prepared_bundle(conn, prepared, table_names, mode)
                _foreign_key_check(conn)
                _validate_imported_relationships(conn, prepared)
                if on_validated_state is not None:
                    on_validated_state(database_digest, result)
                conn.rollback()
            else:
                if on_validated_state is not None:
                    on_validated_state(database_digest, result)
                _apply_prepared_bundle(conn, prepared, table_names, mode)
                _foreign_key_check(conn)
                _validate_imported_relationships(conn, prepared)
                conn.commit()
        except BaseException:
            conn.rollback()
            raise
    return result


def available_user_tables(conn: sqlite3.Connection) -> tuple[str, ...]:
    existing = {str(row[0]) for row in conn.execute("SELECT name FROM sqlite_schema WHERE type = 'table'").fetchall()}
    core = [name for name in CORE_USER_DATA_TABLES if name in existing]
    optional = sorted((existing & USER_DATA_TABLE_ALLOWLIST) - set(CORE_USER_DATA_TABLES))
    return tuple((*core, *optional))


def _export_table(conn: sqlite3.Connection, table: str) -> LocalDataTableBundle:
    columns, primary_key, column_info = _table_contract(conn, table)
    order_columns = primary_key or columns
    order_sql = ", ".join(_quote_identifier(column) for column in order_columns)
    rows = conn.execute(f"SELECT * FROM {_quote_identifier(table)} ORDER BY {order_sql}").fetchall()
    return LocalDataTableBundle(
        columns=list(columns),
        column_types={column: str(column_info[column]["type"] or "") for column in columns},
        primary_key=list(primary_key),
        rows=[{column: _json_value(row[column], table, column) for column in columns} for row in rows],
    )


def _validate_bundle_for_database(
    conn: sqlite3.Connection,
    bundle: UserDataBundle,
    *,
    mode: LocalDataImportMode,
) -> tuple[tuple[str, ...], dict[str, LocalDataTableBundle]]:
    ordered_available = available_user_tables(conn)
    available = set(ordered_available)
    requested = set(bundle.tables)
    unavailable = sorted(requested - available)
    if unavailable:
        raise ValueError("目标数据库缺少用户数据表：" + "、".join(unavailable))
    if mode == "replace" and requested != available:
        missing = sorted(available - requested)
        raise ValueError("replace 模式必须包含全部用户数据表，缺少：" + "、".join(missing))
    table_names = tuple(name for name in ordered_available if name in requested)
    normalized = {name: _validate_table_bundle(conn, name, bundle.tables[name]) for name in table_names}
    return table_names, normalized


def _validate_table_bundle(
    conn: sqlite3.Connection,
    table: str,
    bundle: LocalDataTableBundle,
) -> LocalDataTableBundle:
    columns, primary_key, column_info = _table_contract(conn, table)
    bundle = _with_v1_compat_columns(table, bundle, columns, column_info)
    target_types = _validated_target_types(table, bundle, columns, primary_key, column_info)
    normalized_rows = _normalized_bundle_rows(table, bundle, columns, primary_key, column_info)
    return bundle.model_copy(
        update={
            "columns": list(columns),
            "column_types": target_types,
            "rows": normalized_rows,
        }
    )


def _with_v1_compat_columns(
    table: str,
    bundle: LocalDataTableBundle,
    target_columns: tuple[str, ...],
    column_info: dict[str, sqlite3.Row],
) -> LocalDataTableBundle:
    missing = set(target_columns) - set(bundle.columns)
    defaults = _V1_COMPAT_COLUMN_DEFAULTS.get(table, {})
    if not missing or not missing.issubset(defaults):
        return bundle
    rows = [{**row, **{column: defaults[column] for column in missing}} for row in bundle.rows]
    column_types = bundle.column_types
    if column_types is not None:
        column_types = {
            **column_types,
            **{column: str(column_info[column]["type"] or "") for column in missing},
        }
    return bundle.model_copy(
        update={
            "columns": [*bundle.columns, *sorted(missing)],
            "column_types": column_types,
            "rows": rows,
        }
    )


def _validated_target_types(
    table: str,
    bundle: LocalDataTableBundle,
    columns: tuple[str, ...],
    primary_key: tuple[str, ...],
    column_info: dict[str, sqlite3.Row],
) -> dict[str, str]:
    if set(bundle.columns) != set(columns):
        raise ValueError(f"{table} 列结构与目标数据库不一致")
    if tuple(bundle.primary_key) != primary_key:
        raise ValueError(f"{table} 主键结构与目标数据库不一致")
    target_types = {column: str(column_info[column]["type"] or "") for column in columns}
    if bundle.column_types is None:
        return target_types
    for column in columns:
        if _normalize_declared_type(bundle.column_types[column]) != _normalize_declared_type(target_types[column]):
            raise ValueError(f"{table}.{column} 列类型与目标数据库不一致")
    return target_types


def _normalized_bundle_rows(
    table: str,
    bundle: LocalDataTableBundle,
    columns: tuple[str, ...],
    primary_key: tuple[str, ...],
    column_info: dict[str, sqlite3.Row],
) -> list[dict[str, JsonValue]]:
    if bundle.rows and not primary_key:
        raise ValueError(f"{table} 没有可携带的主键，不能安全导入")
    normalized_rows = [{column: row[column] for column in columns} for row in bundle.rows]
    _validate_normalized_rows(table, normalized_rows, primary_key, column_info)
    return normalized_rows


def _validate_normalized_rows(
    table: str,
    rows: list[dict[str, JsonValue]],
    primary_key: tuple[str, ...],
    column_info: dict[str, sqlite3.Row],
) -> None:
    seen_keys: set[tuple[object, ...]] = set()
    for row in rows:
        _validate_row_types(table, row, column_info)
        key = tuple(row[column] for column in primary_key)
        if any(value is None for value in key) or key in seen_keys:
            raise ValueError(f"{table} 包含空主键或重复主键")
        seen_keys.add(key)


def _validate_row_types(table: str, row: dict[str, object], column_info: dict[str, sqlite3.Row]) -> None:
    for column, value in row.items():
        info = column_info[column]
        if value is None:
            if bool(info["notnull"]) or int(info["pk"]) > 0:
                raise ValueError(f"{table}.{column} 不允许为空")
            continue
        if not _matches_affinity(value, str(info["type"] or "")):
            raise ValueError(f"{table}.{column} 的值类型与目标数据库不一致")


def _validate_in_bundle_relationships(tables: dict[str, LocalDataTableBundle]) -> None:
    for child_table, relationships in _RELATIONSHIPS.items():
        child = tables.get(child_table)
        if child is not None:
            _validate_child_bundle_relationships(tables, child_table, child, relationships)
    _validate_review_bundle_relationships(tables)


def _validate_child_bundle_relationships(
    tables: dict[str, LocalDataTableBundle],
    child_table: str,
    child: LocalDataTableBundle,
    relationships: tuple[tuple[str, str], ...],
) -> None:
    for child_column, parent_table in relationships:
        parent = tables.get(parent_table)
        if parent is None:
            if child.rows:
                raise ValueError(f"{child_table}.{child_column} 外键约束要求导入包同时包含 {parent_table}")
            continue
        parent_key = _SURROGATE_PRIMARY_KEYS[parent_table]
        parent_ids = {row[parent_key] for row in parent.rows}
        if any(row[child_column] not in parent_ids for row in child.rows):
            raise ValueError(f"{child_table}.{child_column} 包含无效的外键约束")


def _validate_review_bundle_relationships(tables: dict[str, LocalDataTableBundle]) -> None:
    plans = tables.get("advice_review_plan")
    results = tables.get("advice_review_result")
    if plans is None or results is None:
        return
    plan_advice = {row["id"]: row["advice_id"] for row in plans.rows}
    if any(plan_advice.get(row["plan_id"]) != row["advice_id"] for row in results.rows):
        raise ValueError("advice_review_result 包含不一致的计划/建议外键约束")


def _prepare_bundle(
    conn: sqlite3.Connection,
    tables: dict[str, LocalDataTableBundle],
    table_names: tuple[str, ...],
    mode: LocalDataImportMode,
) -> dict[str, _PreparedTable]:
    prepared: dict[str, _PreparedTable] = {}
    id_maps: dict[str, dict[object, object]] = {}
    for table in table_names:
        bundle = tables[table]
        rows = tuple(_remap_foreign_keys(table, row, tables, id_maps) for row in bundle.rows)
        if mode == "replace":
            table_plan, id_map = _prepare_replace_table(conn, table, bundle, rows)
        else:
            table_plan, id_map = _prepare_merge_table(conn, table, bundle, rows)
        prepared[table] = table_plan
        if id_map is not None:
            id_maps[table] = id_map
    return prepared


def _remap_foreign_keys(
    table: str,
    source_row: dict[str, JsonValue],
    tables: dict[str, LocalDataTableBundle],
    id_maps: dict[str, dict[object, object]],
) -> dict[str, object]:
    row: dict[str, object] = dict(source_row)
    for column, parent_table in _RELATIONSHIPS.get(table, ()):
        if parent_table not in tables:
            continue
        parent_map = id_maps.get(parent_table)
        if parent_map is None or row[column] not in parent_map:
            raise ValueError(f"{table}.{column} 无法映射到导入数据中的父记录")
        row[column] = parent_map[row[column]]
    return row


def _prepare_replace_table(
    conn: sqlite3.Connection,
    table: str,
    bundle: LocalDataTableBundle,
    rows: tuple[dict[str, object], ...],
) -> tuple[_PreparedTable, dict[object, object] | None]:
    existing_count = int(conn.execute(f"SELECT COUNT(*) FROM {_quote_identifier(table)}").fetchone()[0])
    prepared_rows = tuple(_PreparedRow(operation="insert", values=row) for row in rows)
    preview = LocalDataTableImportPreview(
        incoming=len(rows),
        inserted=len(rows),
        updated=0,
        unchanged=0,
        deleted=existing_count,
        remapped=0,
    )
    primary_key = _SURROGATE_PRIMARY_KEYS.get(table)
    id_map = None if primary_key is None else {row[primary_key]: row[primary_key] for row in rows}
    return _PreparedTable(bundle=bundle, rows=prepared_rows, preview=preview), id_map


def _prepare_merge_table(
    conn: sqlite3.Connection,
    table: str,
    bundle: LocalDataTableBundle,
    rows: tuple[dict[str, object], ...],
) -> tuple[_PreparedTable, dict[object, object] | None]:
    primary_key = _SURROGATE_PRIMARY_KEYS.get(table)
    if primary_key is None:
        return _prepare_stable_merge_table(conn, table, bundle, rows), None
    return _prepare_surrogate_merge_table(conn, table, bundle, rows, primary_key)


def _prepare_stable_merge_table(
    conn: sqlite3.Connection,
    table: str,
    bundle: LocalDataTableBundle,
    rows: tuple[dict[str, object], ...],
) -> _PreparedTable:
    key_columns = _SOURCE_WINS_KEYS.get(table)
    if key_columns is None or tuple(bundle.primary_key) != key_columns:
        raise ValueError(f"{table} 没有受支持的稳定合并键")
    existing = {_row_key(row, key_columns): row for row in _existing_rows(conn, table, bundle.columns)}
    prepared_rows: list[_PreparedRow] = []
    for row in rows:
        current = existing.get(_row_key(row, key_columns))
        operation: _RowOperation
        if current is None:
            operation = "insert"
        elif current == row:
            operation = "unchanged"
        else:
            operation = "update"
        prepared_rows.append(_PreparedRow(operation=operation, values=row))
        existing[_row_key(row, key_columns)] = row
    return _PreparedTable(
        bundle=bundle,
        rows=tuple(prepared_rows),
        preview=_preview_for_rows(prepared_rows, remapped=0),
    )


def _prepare_surrogate_merge_table(
    conn: sqlite3.Connection,
    table: str,
    bundle: LocalDataTableBundle,
    rows: tuple[dict[str, object], ...],
    primary_key: str,
) -> tuple[_PreparedTable, dict[object, object]]:
    if tuple(bundle.primary_key) != (primary_key,):
        raise ValueError(f"{table} 的代理主键结构不受支持")
    existing_rows = _existing_rows(conn, table, bundle.columns)
    stable_columns = _SOURCE_WINS_KEYS.get(table)
    if stable_columns is not None:
        _validate_unique_stable_keys(table, rows, stable_columns)
    state = _surrogate_merge_state(existing_rows, rows, primary_key, stable_columns)
    for source_row in rows:
        _append_surrogate_merge_row(state, source_row)
    plan = _PreparedTable(
        bundle=bundle,
        rows=tuple(state.prepared_rows),
        preview=_preview_for_rows(state.prepared_rows, remapped=state.remapped),
    )
    return plan, state.id_map


def _surrogate_merge_state(
    existing_rows: list[dict[str, object]],
    source_rows: tuple[dict[str, object], ...],
    primary_key: str,
    stable_columns: tuple[str, ...] | None,
) -> _SurrogateMergeState:
    existing_by_id = {row[primary_key]: row for row in existing_rows}
    used_ids = set(existing_by_id)
    source_ids = [row[primary_key] for row in source_rows]
    existing_by_stable = {_row_key(row, stable_columns): row for row in existing_rows} if stable_columns is not None else {}
    return _SurrogateMergeState(
        primary_key=primary_key,
        stable_columns=stable_columns,
        existing_by_id=existing_by_id,
        existing_by_stable=existing_by_stable,
        used_ids=used_ids,
        next_id=max((cast(int, value) for value in (*used_ids, *source_ids)), default=0) + 1,
        prepared_rows=[],
        id_map={},
    )


def _append_surrogate_merge_row(
    state: _SurrogateMergeState,
    source_row: dict[str, object],
) -> None:
    source_id = source_row[state.primary_key]
    stable_key = _optional_row_key(source_row, state.stable_columns)
    stable_match = state.existing_by_stable.get(stable_key) if stable_key is not None else None
    if stable_match is not None:
        target_id = stable_match[state.primary_key]
        row = {**source_row, state.primary_key: target_id}
        operation: _RowOperation = "unchanged" if stable_match == row else "update"
    elif state.existing_by_id.get(source_id) == source_row:
        target_id, row, operation = source_id, source_row, "unchanged"
    else:
        target_id = _available_surrogate_id(state, source_id)
        row, operation = {**source_row, state.primary_key: target_id}, "insert"
    if target_id != source_id:
        state.remapped += 1
    state.id_map[source_id] = target_id
    state.used_ids.add(target_id)
    state.existing_by_id[target_id] = row
    if stable_key is not None:
        state.existing_by_stable[stable_key] = row
    state.prepared_rows.append(_PreparedRow(operation=operation, values=row))


def _available_surrogate_id(state: _SurrogateMergeState, source_id: object) -> object:
    if source_id not in state.used_ids:
        return source_id
    while state.next_id in state.used_ids:
        state.next_id += 1
    target_id = state.next_id
    state.next_id += 1
    return target_id


def _optional_row_key(
    row: dict[str, object],
    columns: tuple[str, ...] | None,
) -> tuple[object, ...] | None:
    return _row_key(row, columns) if columns is not None else None


def _validate_unique_stable_keys(
    table: str,
    rows: tuple[dict[str, object], ...],
    columns: tuple[str, ...],
) -> None:
    seen: set[tuple[object, ...]] = set()
    for row in rows:
        key = _row_key(row, columns)
        if key in seen:
            raise ValueError(f"{table} 包含重复的稳定合并键")
        seen.add(key)


def _preview_for_rows(
    rows: list[_PreparedRow],
    *,
    remapped: int,
) -> LocalDataTableImportPreview:
    return LocalDataTableImportPreview(
        incoming=len(rows),
        inserted=sum(row.operation == "insert" for row in rows),
        updated=sum(row.operation == "update" for row in rows),
        unchanged=sum(row.operation == "unchanged" for row in rows),
        deleted=0,
        remapped=remapped,
    )


def _existing_rows(
    conn: sqlite3.Connection,
    table: str,
    columns: list[str],
) -> list[dict[str, object]]:
    rows = conn.execute(f"SELECT * FROM {_quote_identifier(table)}").fetchall()
    return [{column: row[column] for column in columns} for row in rows]


def _row_key(row: dict[str, object], columns: tuple[str, ...]) -> tuple[object, ...]:
    return tuple(row[column] for column in columns)


def _apply_prepared_bundle(
    conn: sqlite3.Connection,
    prepared: dict[str, _PreparedTable],
    table_names: tuple[str, ...],
    mode: LocalDataImportMode,
) -> None:
    if mode == "replace":
        for name in reversed(table_names):
            conn.execute(f"DELETE FROM {_quote_identifier(name)}")
    for name in table_names:
        table = prepared[name]
        for row in table.rows:
            if row.operation == "insert":
                _insert_row(conn, name, table.bundle, row.values)
            elif row.operation == "update":
                _update_row(conn, name, table.bundle, row.values)


def _insert_row(
    conn: sqlite3.Connection,
    table: str,
    bundle: LocalDataTableBundle,
    row: dict[str, object],
) -> None:
    columns_sql = ", ".join(_quote_identifier(column) for column in bundle.columns)
    placeholders = ", ".join("?" for _ in bundle.columns)
    conn.execute(
        f"INSERT INTO {_quote_identifier(table)} ({columns_sql}) VALUES ({placeholders})",
        tuple(row[column] for column in bundle.columns),
    )


def _update_row(
    conn: sqlite3.Connection,
    table: str,
    bundle: LocalDataTableBundle,
    row: dict[str, object],
) -> None:
    updates = [column for column in bundle.columns if column not in bundle.primary_key]
    assignments = ", ".join(f"{_quote_identifier(column)} = ?" for column in updates)
    predicates = " AND ".join(f"{_quote_identifier(column)} = ?" for column in bundle.primary_key)
    params = tuple(row[column] for column in (*updates, *bundle.primary_key))
    cursor = conn.execute(
        f"UPDATE {_quote_identifier(table)} SET {assignments} WHERE {predicates}",
        params,
    )
    if cursor.rowcount != 1:
        raise ValueError(f"{table} 稳定键更新未命中目标记录")


def _foreign_key_check(conn: sqlite3.Connection) -> None:
    violations = conn.execute("PRAGMA foreign_key_check").fetchmany(5)
    if violations:
        details = ", ".join(f"{row[0]}:{row[1]}" for row in violations)
        raise ValueError(f"导入数据违反外键约束：{details}")


def _validate_imported_relationships(
    conn: sqlite3.Connection,
    prepared: dict[str, _PreparedTable],
) -> None:
    parent_rows = _imported_parent_rows(conn, prepared)
    for child_table, relationships in _RELATIONSHIPS.items():
        child = prepared.get(child_table)
        if child is not None:
            _validate_imported_child_rows(child_table, child, relationships, parent_rows)
    _validate_imported_review_rows(prepared.get("advice_review_result"), parent_rows)


def _imported_parent_rows(
    conn: sqlite3.Connection,
    prepared: dict[str, _PreparedTable],
) -> dict[str, dict[object, sqlite3.Row]]:
    parent_tables = {parent_table for child_table, relationships in _RELATIONSHIPS.items() if child_table in prepared for _, parent_table in relationships}
    result: dict[str, dict[object, sqlite3.Row]] = {}
    for parent_table in parent_tables:
        parent_key = _SURROGATE_PRIMARY_KEYS[parent_table]
        rows = conn.execute(f"SELECT * FROM {_quote_identifier(parent_table)}").fetchall()
        result[parent_table] = {row[parent_key]: row for row in rows}
    return result


def _validate_imported_child_rows(
    child_table: str,
    child: _PreparedTable,
    relationships: tuple[tuple[str, str], ...],
    parent_rows: dict[str, dict[object, sqlite3.Row]],
) -> None:
    for prepared_row in child.rows:
        row = prepared_row.values
        for child_column, parent_table in relationships:
            parent = parent_rows[parent_table].get(row[child_column])
            if parent is None:
                raise ValueError(f"{child_table}.{child_column} 导入后违反外键约束")
            if _row_symbol(row) != _row_symbol(parent):
                raise ValueError(f"{child_table}.{child_column} 导入后关联到错误的父记录")


def _row_symbol(row) -> object | None:
    return row["symbol"] if "symbol" in row.keys() else None


def _validate_imported_review_rows(
    results: _PreparedTable | None,
    parent_rows: dict[str, dict[object, sqlite3.Row]],
) -> None:
    if results is None:
        return
    plans = parent_rows["advice_review_plan"]
    for prepared_row in results.rows:
        row = prepared_row.values
        if plans[row["plan_id"]]["advice_id"] != row["advice_id"]:
            raise ValueError("advice_review_result 导入后计划与建议关联不一致")


def _table_contract(
    conn: sqlite3.Connection,
    table: str,
) -> tuple[tuple[str, ...], tuple[str, ...], dict[str, sqlite3.Row]]:
    rows = conn.execute(f"PRAGMA table_xinfo({_quote_identifier(table)})").fetchall()
    visible = [row for row in rows if int(row["hidden"]) == 0]
    if not visible:
        raise ValueError(f"用户数据表 {table} 不存在或没有可导出列")
    columns = tuple(str(row["name"]) for row in visible)
    primary_key = tuple(str(row["name"]) for row in sorted(visible, key=lambda row: int(row["pk"])) if int(row["pk"]) > 0)
    return columns, primary_key, {str(row["name"]): row for row in visible}


def _normalize_declared_type(value: str) -> str:
    return " ".join(value.upper().split())


def _matches_affinity(value: object, declared_type: str) -> bool:
    affinity = declared_type.upper()
    if "INT" in affinity:
        return isinstance(value, int) and not isinstance(value, bool)
    if any(token in affinity for token in ("CHAR", "CLOB", "TEXT")):
        return isinstance(value, str)
    if "BLOB" in affinity or not affinity:
        return False
    if any(token in affinity for token in ("REAL", "FLOA", "DOUB")):
        return isinstance(value, (int, float)) and not isinstance(value, bool) and math.isfinite(value)
    return isinstance(value, (int, float, str)) and not isinstance(value, bool)


def _json_value(value: object, table: str, column: str) -> JsonValue:
    if value is None or isinstance(value, (str, int)):
        return value
    if isinstance(value, float) and math.isfinite(value):
        return value
    raise ValueError(f"{table}.{column} 包含不可携带的 SQLite 值")


def _sum_previews(previews) -> LocalDataTableImportPreview:
    rows = list(previews)
    return LocalDataTableImportPreview(
        incoming=sum(item.incoming for item in rows),
        inserted=sum(item.inserted for item in rows),
        updated=sum(item.updated for item in rows),
        unchanged=sum(item.unchanged for item in rows),
        deleted=sum(item.deleted for item in rows),
        remapped=sum(item.remapped for item in rows),
    )


def _connect(path: Path) -> sqlite3.Connection:
    conn = sqlite3.connect(path, timeout=SQLITE_BUSY_TIMEOUT_MS / 1000)
    conn.row_factory = sqlite3.Row
    conn.execute(f"PRAGMA busy_timeout = {SQLITE_BUSY_TIMEOUT_MS}")
    conn.execute("PRAGMA foreign_keys = ON")
    return conn


def _require_database(path: Path) -> Path:
    resolved = Path(path).expanduser().resolve()
    if not resolved.is_file():
        raise ValueError(f"本地数据库不存在：{resolved}")
    return resolved


def _quote_identifier(value: str) -> str:
    if _IDENTIFIER.fullmatch(value) is None:
        raise ValueError("SQLite 标识符不合法")
    return f'"{value}"'


def _utc_now_text() -> str:
    return datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")


__all__ = [
    "CONFLICT_STRATEGY",
    "ImportStateCallback",
    "available_user_tables",
    "export_user_data",
    "import_user_data",
    "user_data_state_digest",
]
