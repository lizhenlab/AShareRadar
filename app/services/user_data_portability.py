"""Versioned export and transactional import of local user-owned data."""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
import hashlib
import json
import math
from pathlib import Path
import re
import sqlite3
from typing import Literal, cast
from datetime import datetime
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from pydantic import JsonValue

from app.db.paper_trading_schema import paper_run_output_digest
from app.models.local_data import (
    AuditTimestampMetadata,
    CORE_USER_DATA_TABLES,
    LOCAL_DATA_BUNDLE_KIND,
    LOCAL_DATA_BUNDLE_VERSION,
    LocalDataImportMode,
    LocalDataImportResult,
    LocalDataTableBundle,
    LocalDataTableImportPreview,
    OPTIONAL_RESEARCH_USER_DATA_TABLES,
    UserDataBundle,
)
from app.utils.audit_time import audit_now_text, normalize_audit_time_text


SQLITE_BUSY_TIMEOUT_MS = 15_000
CONFLICT_STRATEGY = "remap_surrogate_ids_source_wins_on_stable_keys"
_IDENTIFIER = re.compile(r"[A-Za-z_][A-Za-z0-9_]*\Z")
_SHA256 = re.compile(r"[0-9a-f]{64}\Z")
_UTC_FIXED_AUDIT_TIMESTAMP = re.compile(
    r"\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}\.\d{6}Z\Z"
)
_SURROGATE_PRIMARY_KEYS = {
    "alert_rule": "id",
    "alert_event": "id",
    "stock_note": "id",
    "advice_history": "id",
    "advice_review_plan": "id",
    "advice_review_result": "id",
    "watchlist_scan_history": "id",
    "discovery_preset": "id",
    "discovery_research_queue_source": "id",
    "paper_strategy": "id",
    "paper_trading_run": "id",
    "paper_strategy_result": "id",
    "paper_trade": "id",
    "paper_equity_snapshot": "id",
    "paper_trading_event": "id",
    "strategy_spec": "id",
    "strategy_spec_version": "id",
}
_SOURCE_WINS_KEYS = {
    "watchlist": ("symbol",),
    "advice_review_plan": ("advice_id",),
    "advice_review_plan_revision": ("plan_id", "revision"),
    "advice_review_result": (
        "plan_id",
        "plan_revision",
        "as_of",
        "rule_version",
        "attempt",
    ),
    "discovery_preset": ("name",),
    "discovery_research_queue_source": (
        "symbol",
        "source_run_id",
        "source_preset_id",
        "source_preset_revision",
    ),
    "paper_trading_account": ("id",),
    "paper_strategy": ("plan_id", "plan_revision"),
    "paper_strategy_result": ("run_id", "strategy_id"),
    "paper_trade": ("run_id", "strategy_id", "side"),
    "paper_equity_snapshot": ("run_id", "as_of_date"),
    "paper_trading_event": ("run_id", "sequence"),
    "strategy_spec": ("created_at",),
    "strategy_spec_version": ("strategy_id", "revision"),
}
_CASE_INSENSITIVE_STABLE_COLUMNS = {
    "discovery_preset": frozenset({"name"}),
}
_IMMUTABLE_LEDGER_TABLES = frozenset(
    {"advice_review_plan_revision", "advice_review_result"}
)
_REVIEW_RESULT_INPUT_FIELDS = (
    "plan_id",
    "plan_revision",
    "advice_id",
    "symbol",
    "snapshot_market_time",
    "as_of",
    "evaluated_at",
    "rule_version",
    "trigger_basis",
    "invalidation_basis",
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
    "entry_price",
    "target_price",
    "stop_price",
    "horizon_days",
    "visible_bar_count",
    "visible_start_date",
    "visible_end_date",
    "available_forward_days",
    "forward_start_date",
    "forward_end_date",
    "evidence_contract_version",
    "source_window_digest",
    "source_session_count",
    "expected_session_count",
    "observation_basis",
    "attempt",
)
_REVIEW_RESULT_V1_INPUT_FIELDS = tuple(
    field
    for field in _REVIEW_RESULT_INPUT_FIELDS
    if field not in {"evaluated_at", "attempt"}
)
_REVIEW_EVIDENCE_DIGEST_VERSIONS = frozenset(
    {"advice-review-evidence.v1", "advice-review-evidence.v2"}
)
_REVIEW_RESULT_OUTCOME_FIELDS = (
    "status",
    "conclusion",
    "return_pct",
    "max_favorable_excursion_pct",
    "max_adverse_excursion_pct",
    "target_hit",
    "target_hit_date",
    "stop_hit",
    "stop_hit_date",
)
_REVIEW_RESULT_REAL_FIELDS = frozenset(
    {
        "snapshot_anchor_close",
        "anchor_evaluation_close",
        "price_scale_factor",
        "normalized_entry_price",
        "normalized_target_price",
        "normalized_stop_price",
        "entry_price",
        "target_price",
        "stop_price",
        "return_pct",
        "max_favorable_excursion_pct",
        "max_adverse_excursion_pct",
    }
)
_REVIEW_PLAN_PAYLOAD_KEYS = frozenset(
    {
        "advice_id",
        "evidence_refs",
        "horizon_days",
        "hypothesis",
        "invalidation_basis",
        "invalidation_condition",
        "snapshot",
        "stop_price",
        "symbol",
        "target_price",
        "trigger_basis",
        "trigger_condition",
    }
)
_REVIEW_PLAN_SNAPSHOT_KEYS = frozenset(
    {
        "adjustment_mode",
        "anchor_close",
        "anchor_date",
        "contract_version",
        "data_version",
        "market_time",
        "price",
    }
)
_RELATIONSHIPS = {
    "alert_event": (("rule_id", "alert_rule"),),
    "advice_review_plan": (("advice_id", "advice_history"),),
    "advice_review_plan_revision": (("plan_id", "advice_review_plan"),),
    "advice_review_result": (
        ("plan_id", "advice_review_plan"),
        ("advice_id", "advice_history"),
    ),
    "paper_strategy": (
        ("plan_id", "advice_review_plan"),
        ("advice_id", "advice_history"),
    ),
    "paper_strategy_result": (
        ("run_id", "paper_trading_run"),
        ("strategy_id", "paper_strategy"),
    ),
    "paper_trade": (
        ("run_id", "paper_trading_run"),
        ("strategy_id", "paper_strategy"),
    ),
    "paper_equity_snapshot": (("run_id", "paper_trading_run"),),
    "paper_trading_event": (
        ("run_id", "paper_trading_run"),
        ("strategy_id", "paper_strategy"),
    ),
    "strategy_spec_version": (("strategy_id", "strategy_spec"),),
}
_USER_DATA_AUDIT_TIMESTAMP_COLUMNS = {
    "watchlist": frozenset({"created_at", "updated_at", "last_viewed_at"}),
    "advice_history": frozenset({"created_at", "updated_at"}),
    "alert_rule": frozenset(
        {"last_checked_at", "last_triggered_at", "created_at", "updated_at"}
    ),
    "alert_event": frozenset({"created_at"}),
    "stock_note": frozenset({"created_at", "updated_at"}),
    "advice_review_plan": frozenset({"created_at", "updated_at"}),
    "advice_review_plan_revision": frozenset({"created_at"}),
    "advice_review_result": frozenset({"evaluated_at"}),
    "watchlist_scan_history": frozenset({"created_at"}),
    "discovery_preset": frozenset({"created_at", "updated_at"}),
    "discovery_research_queue_source": frozenset({"enqueued_at"}),
    "paper_trading_account": frozenset({"created_at", "updated_at"}),
    "paper_strategy": frozenset({"created_at", "updated_at"}),
    "paper_trading_run": frozenset({"created_at"}),
    "paper_strategy_result": frozenset({"created_at"}),
    "paper_trade": frozenset({"created_at"}),
    "paper_equity_snapshot": frozenset({"created_at"}),
    "paper_trading_event": frozenset({"created_at"}),
    "strategy_spec": frozenset({"created_at", "updated_at"}),
    "strategy_spec_version": frozenset({"created_at"}),
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
        "trigger_basis": "daily_high_gte_target_price",
        "invalidation_basis": "daily_low_lte_stop_price",
        "plan_payload_digest": "legacy-unverified",
        "deleted_at": None,
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
        "trigger_basis": "daily_high_gte_target_price",
        "invalidation_basis": "daily_low_lte_stop_price",
        "attempt": 1,
        "plan_payload_digest": "legacy-unverified",
        "input_digest": "legacy-unverified",
        "result_digest": "legacy-unverified",
        "evidence_contract_version": "legacy-unverified",
        "source_window_digest": "legacy-unverified",
        "source_session_count": 0,
        "expected_session_count": 0,
        "observation_basis": "gross_close_and_barrier_observation",
    },
    "paper_trading_account": {
        "default_cost_profile": "base",
    },
    "paper_strategy": {
        "priority": 0,
        "entry_expiry_sessions": 5,
        "plan_payload_digest": "legacy-unverified",
    },
    "paper_trading_run": {
        "cost_profile_id": "legacy",
        "cost_profile_name": "legacy",
        "cost_profile_version": "legacy",
        "benchmark_symbol": None,
        "benchmark_status": "unavailable",
        "benchmark_message": None,
        "input_fingerprint": "",
        "output_digest": "legacy-unverified",
        "strategy_snapshot_hash": "",
        "market_data_hash": "",
        "data_start_date": None,
        "data_end_date": None,
        "configuration_json": "{}",
        "rule_profiles_json": "[]",
        "data_sources_json": "[]",
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
    table: str
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
        audit_timestamps=AuditTimestampMetadata(semantics="utc-fixed"),
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
    legacy_audit_timezone: str | None = None,
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
            table_names, normalized_tables = _validate_bundle_for_database(
                conn,
                bundle,
                mode=mode,
                legacy_audit_timezone=legacy_audit_timezone,
            )
            _validate_in_bundle_relationships(normalized_tables)
            _validate_bundled_paper_run_output_digests(
                normalized_tables,
                allow_legacy_unverified=_bundle_missing_paper_output_digest(bundle),
            )
            prepared = _prepare_bundle(conn, normalized_tables, table_names, mode)
            _reject_paper_history_rewrites(conn, prepared)
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


def _reject_paper_history_rewrites(
    conn: sqlite3.Connection,
    prepared: dict[str, _PreparedTable],
) -> None:
    account = prepared.get("paper_trading_account")
    changes_initial_cash = account is not None and any(
        row.operation == "update" and _changes_paper_initial_cash(conn, row.values)
        for row in account.rows
    )
    if changes_initial_cash:
        has_paper_history = conn.execute(
            "SELECT 1 FROM paper_strategy UNION ALL SELECT 1 FROM paper_trading_run LIMIT 1"
        ).fetchone()
        if has_paper_history is not None:
            raise ValueError("已有模拟策略或运行时不能通过导入修改模拟账户")
    strategies = prepared.get("paper_strategy")
    if strategies is None:
        return
    for row in strategies.rows:
        if row.operation != "update":
            continue
        strategy_id = _integer_value(row.values["id"])
        has_result = conn.execute(
            "SELECT 1 FROM paper_strategy_result WHERE strategy_id = ? LIMIT 1",
            (strategy_id,),
        ).fetchone()
        if has_result is not None:
            raise ValueError("已进入不可变运行的模拟策略不能通过导入改写")


def _changes_paper_initial_cash(
    conn: sqlite3.Connection,
    values: dict[str, object],
) -> bool:
    current = conn.execute(
        "SELECT initial_cash FROM paper_trading_account WHERE id = ?",
        (_integer_value(values["id"]),),
    ).fetchone()
    return current is not None and float(current["initial_cash"]) != _float_value(values["initial_cash"])


def available_user_tables(conn: sqlite3.Connection) -> tuple[str, ...]:
    existing = {str(row[0]) for row in conn.execute("SELECT name FROM sqlite_schema WHERE type = 'table'").fetchall()}
    core = [name for name in CORE_USER_DATA_TABLES if name in existing]
    optional = [
        name
        for name in OPTIONAL_RESEARCH_USER_DATA_TABLES
        if name in existing and name not in CORE_USER_DATA_TABLES
    ]
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
        rows=[
            {
                column: _export_value(row[column], table, column)
                for column in columns
            }
            for row in rows
        ],
    )


def _validate_bundle_for_database(
    conn: sqlite3.Connection,
    bundle: UserDataBundle,
    *,
    mode: LocalDataImportMode,
    legacy_audit_timezone: str | None,
) -> tuple[tuple[str, ...], dict[str, LocalDataTableBundle]]:
    resolved_legacy_timezone = _resolve_legacy_audit_timezone(
        bundle.audit_timestamps,
        legacy_audit_timezone,
    )
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
    normalized = {
        name: _validate_table_bundle(
            conn,
            name,
            bundle.tables[name],
            audit_metadata=bundle.audit_timestamps,
            legacy_audit_timezone=resolved_legacy_timezone,
        )
        for name in table_names
    }
    return table_names, normalized


def _validate_table_bundle(
    conn: sqlite3.Connection,
    table: str,
    bundle: LocalDataTableBundle,
    *,
    audit_metadata: AuditTimestampMetadata | None,
    legacy_audit_timezone: str | None,
) -> LocalDataTableBundle:
    columns, primary_key, column_info = _table_contract(conn, table)
    bundle = _with_v1_compat_columns(table, bundle, columns, column_info)
    target_types = _validated_target_types(table, bundle, columns, primary_key, column_info)
    normalized_rows = _normalized_bundle_rows(
        table,
        bundle,
        columns,
        primary_key,
        column_info,
        audit_metadata=audit_metadata,
        legacy_audit_timezone=legacy_audit_timezone,
    )
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
    *,
    audit_metadata: AuditTimestampMetadata | None,
    legacy_audit_timezone: str | None,
) -> list[dict[str, JsonValue]]:
    if bundle.rows and not primary_key:
        raise ValueError(f"{table} 没有可携带的主键，不能安全导入")
    normalized_rows = [
        _normalize_bundle_audit_timestamps(
            table,
            {column: row[column] for column in columns},
            audit_metadata=audit_metadata,
            legacy_audit_timezone=legacy_audit_timezone,
        )
        for row in bundle.rows
    ]
    _validate_normalized_rows(table, normalized_rows, primary_key, column_info)
    return normalized_rows


def _normalize_bundle_audit_timestamps(
    table: str,
    row: dict[str, JsonValue],
    *,
    audit_metadata: AuditTimestampMetadata | None,
    legacy_audit_timezone: str | None,
) -> dict[str, JsonValue]:
    for column in _USER_DATA_AUDIT_TIMESTAMP_COLUMNS.get(table, ()):
        value = row.get(column)
        if value is None or not isinstance(value, str):
            continue
        try:
            row[column] = _normalize_imported_audit_timestamp(
                value,
                audit_metadata=audit_metadata,
                legacy_audit_timezone=legacy_audit_timezone,
            )
        except (TypeError, ValueError):
            raise ValueError(
                f"{table}.{column} 不是与 bundle 语义一致的有效审计时间"
            ) from None
    return row


def _export_value(value: object, table: str, column: str) -> JsonValue:
    portable = _json_value(value, table, column)
    if column not in _USER_DATA_AUDIT_TIMESTAMP_COLUMNS.get(table, ()) or portable is None:
        return portable
    if not isinstance(portable, str) or _UTC_FIXED_AUDIT_TIMESTAMP.fullmatch(portable) is None:
        raise ValueError(f"{table}.{column} 必须先归一化为 UTC 固定格式再导出")
    return portable


def _resolve_legacy_audit_timezone(
    metadata: AuditTimestampMetadata | None,
    explicit_timezone: str | None,
) -> str | None:
    if metadata is not None:
        return metadata.legacy_timezone
    if explicit_timezone is None:
        return None
    try:
        ZoneInfo(explicit_timezone)
    except (ValueError, ZoneInfoNotFoundError):
        raise ValueError("legacy_audit_timezone 必须是有效的 IANA 时区") from None
    return explicit_timezone


def _normalize_imported_audit_timestamp(
    value: str,
    *,
    audit_metadata: AuditTimestampMetadata | None,
    legacy_audit_timezone: str | None,
) -> str:
    parsed = _parse_bundle_audit_timestamp(value)
    if audit_metadata is not None and audit_metadata.semantics == "utc-fixed":
        if parsed.tzinfo is None or parsed.utcoffset() is None:
            raise ValueError("utc-fixed audit timestamp must include UTC timezone")
        if _UTC_FIXED_AUDIT_TIMESTAMP.fullmatch(value) is None:
            raise ValueError("utc-fixed audit timestamp must use the fixed UTC format")
        return value
    if parsed.tzinfo is None or parsed.utcoffset() is None:
        if legacy_audit_timezone is None:
            raise ValueError("legacy naive audit timestamp requires an explicit timezone")
        return normalize_audit_time_text(
            value,
            legacy_timezone=legacy_audit_timezone,
        )
    return normalize_audit_time_text(value, legacy_timezone="UTC")


def _parse_bundle_audit_timestamp(value: str) -> datetime:
    text = value.strip()
    if not text:
        raise ValueError("audit timestamp is empty")
    return datetime.fromisoformat(text.replace("Z", "+00:00"))


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


def _validate_bundled_paper_run_output_digests(
    tables: dict[str, LocalDataTableBundle],
    *,
    allow_legacy_unverified: bool,
) -> None:
    runs = tables.get("paper_trading_run")
    if runs is None:
        return
    for row in runs.rows:
        digest = row["output_digest"]
        if not isinstance(digest, str):
            raise ValueError("paper_trading_run.output_digest 类型无效")
        if digest == "legacy-unverified":
            if allow_legacy_unverified:
                continue
            raise ValueError("paper_trading_run 缺少可验证输出摘要")
        if _SHA256.fullmatch(digest) is None:
            raise ValueError("paper_trading_run.output_digest 不是有效 SHA-256")
    # Complete paper bundles can be verified without trusting the target
    # database or any surrogate identifier. Partial bundles are still checked
    # after materialization against the target's canonical ledger projection.
    required = {
        "paper_strategy",
        "paper_strategy_result",
        "paper_trade",
        "paper_equity_snapshot",
        "paper_trading_event",
    }
    if not runs.rows:
        return
    if not required.issubset(tables):
        raise ValueError("paper_trading_run 必须与完整输出账本一同导入")
    _validate_complete_bundled_paper_output_digests(tables, runs)


def _bundle_missing_paper_output_digest(bundle: UserDataBundle) -> bool:
    runs = bundle.tables.get("paper_trading_run")
    return runs is not None and "output_digest" not in runs.columns


def _validate_complete_bundled_paper_output_digests(
    tables: dict[str, LocalDataTableBundle],
    runs: LocalDataTableBundle,
) -> None:
    connection = sqlite3.connect(":memory:")
    connection.row_factory = sqlite3.Row
    try:
        for name in (
            "paper_strategy",
            "paper_trading_run",
            "paper_strategy_result",
            "paper_trade",
            "paper_equity_snapshot",
            "paper_trading_event",
        ):
            _create_bundle_verification_table(connection, name, tables[name])
            _insert_bundle_verification_rows(connection, name, tables[name])
        for row in runs.rows:
            digest = str(row["output_digest"])
            if digest == "legacy-unverified":
                continue
            if paper_run_output_digest(connection, _integer_value(row["id"])) != digest:
                raise ValueError("paper_trading_run 输出摘要与携带账本不一致")
    finally:
        connection.close()


def _create_bundle_verification_table(
    conn: sqlite3.Connection,
    table: str,
    bundle: LocalDataTableBundle,
) -> None:
    declarations = bundle.column_types or {}
    columns = ", ".join(
        f"{_quote_identifier(column)} {declarations.get(column) or 'BLOB'}"
        for column in bundle.columns
    )
    conn.execute(f"CREATE TABLE {_quote_identifier(table)} ({columns})")


def _insert_bundle_verification_rows(
    conn: sqlite3.Connection,
    table: str,
    bundle: LocalDataTableBundle,
) -> None:
    if not bundle.rows:
        return
    columns = ", ".join(_quote_identifier(column) for column in bundle.columns)
    placeholders = ", ".join("?" for _ in bundle.columns)
    conn.executemany(
        f"INSERT INTO {_quote_identifier(table)} ({columns}) VALUES ({placeholders})",
        (tuple(row[column] for column in bundle.columns) for row in bundle.rows),
    )


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
        if any(
            row[child_column] is not None and row[child_column] not in parent_ids
            for row in child.rows
        ):
            raise ValueError(f"{child_table}.{child_column} 包含无效的外键约束")


def _validate_review_bundle_relationships(tables: dict[str, LocalDataTableBundle]) -> None:
    plans = tables.get("advice_review_plan")
    revisions = tables.get("advice_review_plan_revision")
    results = tables.get("advice_review_result")
    paper_strategies = tables.get("paper_strategy")
    if plans is None:
        return
    if plans.rows and revisions is None:
        raise ValueError("advice_review_plan 必须与不可变版本账本一同导入")
    plan_advice = {row["id"]: row["advice_id"] for row in plans.rows}
    if results is not None and any(
        plan_advice.get(row["plan_id"]) != row["advice_id"] for row in results.rows
    ):
        raise ValueError("advice_review_result 包含不一致的计划/建议外键约束")
    if revisions is not None:
        _validate_review_revision_bundle(plans, revisions, results, paper_strategies)


def _validate_review_revision_bundle(
    plans: LocalDataTableBundle,
    revisions: LocalDataTableBundle,
    results: LocalDataTableBundle | None,
    paper_strategies: LocalDataTableBundle | None,
) -> None:
    plans_by_id = {row["id"]: row for row in plans.rows}
    revision_payloads: dict[tuple[object, object], dict[str, object]] = {}
    revision_digests: dict[tuple[object, object], str] = {}
    revisions_by_plan: dict[object, set[int]] = {}
    for row in revisions.rows:
        payload, revision_digest = _validated_revision_payload(row)
        plan = plans_by_id.get(row["plan_id"])
        if plan is None or payload.get("advice_id") != plan["advice_id"]:
            raise ValueError("advice_review_plan_revision 与计划/advice snapshot 绑定不一致")
        revision = int(row["revision"])
        identity = (row["plan_id"], revision)
        revision_payloads[identity] = payload
        revision_digests[identity] = revision_digest
        revisions_by_plan.setdefault(row["plan_id"], set()).add(revision)
    for row in plans.rows:
        current_revision = int(row["revision"])
        identity = (row["id"], current_revision)
        current_digest = revision_digests.get(identity)
        expected_revisions = set(range(1, current_revision + 1))
        if revisions_by_plan.get(row["id"], set()) != expected_revisions:
            raise ValueError("advice_review_plan 缺少完整、连续的不可变版本账本")
        if (
            current_digest is None
            or row["plan_payload_digest"] != current_digest
            or revision_payloads[identity] != _review_plan_payload(row)
        ):
            raise ValueError("advice_review_plan 当前投影未绑定不可变版本账本")
    if results is not None:
        _validate_bundled_review_results(results, revision_payloads, revision_digests)
    if paper_strategies is not None:
        _validate_bundled_paper_strategies(
            paper_strategies,
            revision_payloads,
            revision_digests,
        )


def _validate_bundled_review_results(
    results: LocalDataTableBundle,
    revision_payloads: dict[tuple[object, object], dict[str, object]],
    revision_digests: dict[tuple[object, object], str],
) -> None:
    for row in results.rows:
        identity = (row["plan_id"], row["plan_revision"])
        result_digest = revision_digests.get(identity)
        if result_digest is None:
            raise ValueError("advice_review_result 引用了不存在的计划版本")
        if row["plan_payload_digest"] not in {result_digest, "legacy-unverified"}:
            raise ValueError("advice_review_result 计划摘要与版本账本不一致")
        _validate_result_plan_binding(row, revision_payloads[identity])
        _validate_review_result_digests(row)


def _validate_bundled_paper_strategies(
    strategies: LocalDataTableBundle,
    revision_payloads: dict[tuple[object, object], dict[str, object]],
    revision_digests: dict[tuple[object, object], str],
) -> None:
    for row in strategies.rows:
        identity = (row["plan_id"], row["plan_revision"])
        strategy_digest = revision_digests.get(identity)
        if strategy_digest is None:
            raise ValueError("paper_strategy 引用了不存在的计划版本")
        if row["plan_payload_digest"] not in {strategy_digest, "legacy-unverified"}:
            raise ValueError("paper_strategy 计划摘要与版本账本不一致")
        _validate_paper_strategy_plan_binding(row, revision_payloads[identity])


def _validated_revision_payload(
    row: dict[str, JsonValue],
) -> tuple[dict[str, object], str]:
    payload_json = row["payload_json"]
    digest = row["payload_digest"]
    if not isinstance(payload_json, str) or not isinstance(digest, str):
        raise ValueError("advice_review_plan_revision 载荷类型无效")
    try:
        payload = json.loads(payload_json)
    except (TypeError, ValueError, json.JSONDecodeError):
        raise ValueError("advice_review_plan_revision 载荷不是有效 JSON") from None
    if not isinstance(payload, dict):
        raise ValueError("advice_review_plan_revision 载荷必须是 JSON 对象")
    _validate_revision_payload_contract(payload)
    canonical = _canonical_json(payload)
    expected = hashlib.sha256(canonical.encode("utf-8")).hexdigest()
    if payload_json != canonical or _SHA256.fullmatch(digest) is None or digest != expected:
        raise ValueError("advice_review_plan_revision 载荷或摘要不规范")
    return payload, digest


def _validate_revision_payload_contract(payload: dict[str, object]) -> None:
    snapshot = payload.get("snapshot")
    if set(payload) != _REVIEW_PLAN_PAYLOAD_KEYS or not isinstance(snapshot, dict):
        raise ValueError("advice_review_plan_revision 载荷字段不完整")
    _validate_revision_snapshot_contract(snapshot)
    _validate_revision_identity_contract(payload)
    _validate_revision_text_contract(payload, snapshot)
    _validate_revision_price_contract(payload, snapshot)


def _validate_revision_snapshot_contract(snapshot: dict[str, object]) -> None:
    if set(snapshot) != _REVIEW_PLAN_SNAPSHOT_KEYS:
        raise ValueError("advice_review_plan_revision 快照字段不完整")


def _validate_revision_identity_contract(payload: dict[str, object]) -> None:
    advice_id = payload["advice_id"]
    horizon_days = payload["horizon_days"]
    evidence_refs = payload["evidence_refs"]
    if (
        isinstance(advice_id, bool)
        or not isinstance(advice_id, int)
        or advice_id <= 0
        or isinstance(horizon_days, bool)
        or not isinstance(horizon_days, int)
        or not 1 <= horizon_days <= 60
        or not isinstance(evidence_refs, list)
    ):
        raise ValueError("advice_review_plan_revision 载荷类型无效")


def _validate_revision_text_contract(
    payload: dict[str, object],
    snapshot: dict[str, object],
) -> None:
    text_fields = (
        "hypothesis",
        "invalidation_basis",
        "invalidation_condition",
        "symbol",
        "trigger_basis",
        "trigger_condition",
    )
    snapshot_text_fields = (
        "adjustment_mode",
        "contract_version",
        "data_version",
        "market_time",
    )
    if any(not _is_nonblank_text(payload[field]) for field in text_fields):
        raise ValueError("advice_review_plan_revision 文本载荷无效")
    if any(not _is_nonblank_text(snapshot[field]) for field in snapshot_text_fields):
        raise ValueError("advice_review_plan_revision 快照文本无效")
    anchor_date = snapshot["anchor_date"]
    if anchor_date is not None and not isinstance(anchor_date, str):
        raise ValueError("advice_review_plan_revision anchor_date 无效")


def _validate_revision_price_contract(
    payload: dict[str, object],
    snapshot: dict[str, object],
) -> None:
    anchor_close = snapshot["anchor_close"]
    if anchor_close is not None:
        _float_value(anchor_close)
    entry_price = _float_value(snapshot["price"])
    target_price = _float_value(payload["target_price"])
    stop_price = _float_value(payload["stop_price"])
    if not target_price > entry_price > stop_price > 0:
        raise ValueError("advice_review_plan_revision 价格关系无效")


def _is_nonblank_text(value: object) -> bool:
    return isinstance(value, str) and bool(value.strip())


def _review_plan_payload(row: dict[str, JsonValue] | dict[str, object] | sqlite3.Row) -> dict[str, object]:
    try:
        evidence_refs = json.loads(str(row["evidence_refs_json"]))
    except (TypeError, ValueError, json.JSONDecodeError):
        raise ValueError("advice_review_plan 证据引用不是有效 JSON") from None
    if not isinstance(evidence_refs, list):
        raise ValueError("advice_review_plan 证据引用必须是列表")
    return {
        "advice_id": _integer_value(row["advice_id"]),
        "evidence_refs": evidence_refs,
        "horizon_days": _integer_value(row["horizon_days"]),
        "hypothesis": str(row["hypothesis"]),
        "invalidation_basis": str(row["invalidation_basis"]),
        "invalidation_condition": str(row["invalidation_condition"]),
        "snapshot": {
            "adjustment_mode": str(row["snapshot_adjustment_mode"]),
            "anchor_close": _optional_float_value(row["snapshot_anchor_close"]),
            "anchor_date": row["snapshot_anchor_date"],
            "contract_version": str(row["snapshot_contract_version"]),
            "data_version": str(row["snapshot_data_version"]),
            "market_time": str(row["snapshot_market_time"]),
            "price": _float_value(row["snapshot_price"]),
        },
        "stop_price": _float_value(row["stop_price"]),
        "symbol": str(row["symbol"]),
        "target_price": _float_value(row["target_price"]),
        "trigger_basis": str(row["trigger_basis"]),
        "trigger_condition": str(row["trigger_condition"]),
    }


def _validate_result_plan_binding(
    row: dict[str, JsonValue] | dict[str, object],
    payload: dict[str, object],
) -> None:
    snapshot = _revision_snapshot(payload)
    expected = (
        payload["advice_id"],
        payload["symbol"],
        snapshot["market_time"],
        snapshot["price"],
        snapshot["adjustment_mode"],
        snapshot["anchor_date"],
        snapshot["anchor_close"],
        snapshot["data_version"],
        snapshot["contract_version"],
        payload["trigger_basis"],
        payload["invalidation_basis"],
        payload["target_price"],
        payload["stop_price"],
        payload["horizon_days"],
    )
    observed = (
        row["advice_id"],
        row["symbol"],
        row["snapshot_market_time"],
        row["entry_price"],
        row["snapshot_adjustment_mode"],
        row["snapshot_anchor_date"],
        row["snapshot_anchor_close"],
        row["snapshot_data_version"],
        row["snapshot_contract_version"],
        row["trigger_basis"],
        row["invalidation_basis"],
        row["target_price"],
        row["stop_price"],
        row["horizon_days"],
    )
    if observed != expected:
        raise ValueError("advice_review_result 与不可变计划版本绑定不一致")


def _validate_paper_strategy_plan_binding(
    row: dict[str, JsonValue] | dict[str, object],
    payload: dict[str, object],
) -> None:
    snapshot = _revision_snapshot(payload)
    expected = (
        payload["advice_id"],
        payload["symbol"],
        snapshot["market_time"],
        snapshot["price"],
        snapshot["adjustment_mode"],
        snapshot["anchor_date"],
        snapshot["anchor_close"],
        snapshot["data_version"],
        snapshot["contract_version"],
        payload["target_price"],
        payload["stop_price"],
        payload["horizon_days"],
    )
    observed = (
        row["advice_id"],
        row["symbol"],
        row["snapshot_market_time"],
        row["snapshot_price"],
        row["snapshot_adjustment_mode"],
        row["snapshot_anchor_date"],
        row["snapshot_anchor_close"],
        row["snapshot_data_version"],
        row["snapshot_contract_version"],
        row["target_price"],
        row["stop_price"],
        row["horizon_days"],
    )
    if observed != expected:
        raise ValueError("paper_strategy 与不可变计划版本绑定不一致")


def _revision_snapshot(payload: dict[str, object]) -> dict[str, object]:
    snapshot = payload.get("snapshot")
    if not isinstance(snapshot, dict):
        raise ValueError("advice_review_plan_revision 快照载荷无效")
    return snapshot


def _validate_review_result_digests(
    row: dict[str, JsonValue] | dict[str, object],
) -> None:
    input_digest = row["input_digest"]
    result_digest = row["result_digest"]
    if input_digest == result_digest == "legacy-unverified":
        return
    evidence_version = row["evidence_contract_version"]
    if (
        evidence_version not in _REVIEW_EVIDENCE_DIGEST_VERSIONS
        or not isinstance(input_digest, str)
        or not isinstance(result_digest, str)
        or _SHA256.fullmatch(input_digest) is None
        or _SHA256.fullmatch(result_digest) is None
        or input_digest != _review_result_input_digest(row)
        or result_digest != _review_result_outcome_digest(row)
    ):
        raise ValueError("advice_review_result 输入或结果摘要不一致")


def _review_result_input_digest(
    row: dict[str, JsonValue] | dict[str, object],
) -> str:
    fields = (
        _REVIEW_RESULT_V1_INPUT_FIELDS
        if row["evidence_contract_version"] == "advice-review-evidence.v1"
        else _REVIEW_RESULT_INPUT_FIELDS
    )
    return _payload_digest(
        {
            field: _review_result_digest_value(field, row[field])
            for field in fields
        }
    )


def _review_result_outcome_digest(
    row: dict[str, JsonValue] | dict[str, object],
) -> str:
    return _payload_digest(
        {
            field: _review_result_digest_value(field, row[field])
            for field in _REVIEW_RESULT_OUTCOME_FIELDS
        }
    )


def _review_result_digest_value(field: str, value: object) -> object:
    if field not in _REVIEW_RESULT_REAL_FIELDS or value is None:
        return value
    return _float_value(value)


def _payload_digest(value: object) -> str:
    return hashlib.sha256(_canonical_json(value).encode("utf-8")).hexdigest()


def _canonical_json(value: object) -> str:
    return json.dumps(
        value,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
        allow_nan=False,
    )


def _optional_float_value(value: object) -> float | None:
    return None if value is None else _float_value(value)


def _integer_value(value: object) -> int:
    if isinstance(value, bool) or not isinstance(value, int):
        raise ValueError("复盘计划整数载荷无效")
    return value


def _float_value(value: object) -> float:
    if isinstance(value, bool) or not isinstance(value, int | float):
        raise ValueError("复盘计划数值载荷无效")
    parsed = float(value)
    if not math.isfinite(parsed):
        raise ValueError("复盘计划数值载荷无效")
    return parsed


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
        if row[column] is None:
            continue
        parent_map = id_maps.get(parent_table)
        if parent_map is None or row[column] not in parent_map:
            raise ValueError(f"{table}.{column} 无法映射到导入数据中的父记录")
        row[column] = parent_map[row[column]]
    _rewrite_review_plan_digest_after_remap(table, source_row, row, tables, id_maps)
    return row


def _rewrite_review_plan_digest_after_remap(
    table: str,
    source_row: dict[str, JsonValue],
    row: dict[str, object],
    tables: dict[str, LocalDataTableBundle],
    id_maps: dict[str, dict[object, object]],
) -> None:
    identity = _review_plan_revision_identity(table, source_row)
    if identity is None:
        return
    source_plan_id, revision = identity
    payload_json, digest = _remapped_revision_payload(
        tables,
        id_maps,
        source_plan_id=source_plan_id,
        revision=revision,
    )
    if table == "advice_review_plan_revision":
        row["payload_json"] = payload_json
        row["payload_digest"] = digest
    elif table == "advice_review_plan":
        row["plan_payload_digest"] = digest
    else:
        row["plan_payload_digest"] = digest
        if table == "advice_review_result" and row.get("input_digest") != "legacy-unverified":
            row["input_digest"] = _review_result_input_digest(row)
            row["result_digest"] = _review_result_outcome_digest(row)


def _review_plan_revision_identity(
    table: str,
    row: dict[str, JsonValue],
) -> tuple[object, object] | None:
    if table == "advice_review_plan":
        return row.get("id"), row.get("revision")
    if table in {
        "advice_review_plan_revision",
        "advice_review_result",
        "paper_strategy",
    }:
        revision_key = "revision" if table == "advice_review_plan_revision" else "plan_revision"
        return row.get("plan_id"), row.get(revision_key)
    return None


def _remapped_revision_payload(
    tables: dict[str, LocalDataTableBundle],
    id_maps: dict[str, dict[object, object]],
    *,
    source_plan_id: object,
    revision: object,
) -> tuple[str, str]:
    revisions = tables.get("advice_review_plan_revision")
    if revisions is None:
        raise ValueError("复盘计划缺少不可变版本账本")
    matched = next(
        (
            item
            for item in revisions.rows
            if item["plan_id"] == source_plan_id and item["revision"] == revision
        ),
        None,
    )
    if matched is None:
        raise ValueError("复盘记录引用了不存在的计划版本")
    payload = json.loads(str(matched["payload_json"]))
    if not isinstance(payload, dict):
        raise ValueError("复盘计划版本载荷无效")
    advice_map = id_maps.get("advice_history")
    source_advice_id = payload.get("advice_id")
    if advice_map is None or source_advice_id not in advice_map:
        raise ValueError("复盘计划版本无法映射 advice snapshot")
    payload["advice_id"] = advice_map[source_advice_id]
    payload_json = json.dumps(payload, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    return payload_json, hashlib.sha256(payload_json.encode("utf-8")).hexdigest()


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
    existing = {
        _stable_row_key(table, row, key_columns): row
        for row in _existing_rows(conn, table, bundle.columns)
    }
    prepared_rows: list[_PreparedRow] = []
    for row in rows:
        stable_key = _stable_row_key(table, row, key_columns)
        current = existing.get(stable_key)
        operation: _RowOperation
        if current is None:
            operation = "insert"
        elif current == row:
            operation = "unchanged"
        elif table in _IMMUTABLE_LEDGER_TABLES:
            raise ValueError(f"{table} 不可变账本与目标数据库冲突")
        else:
            operation = "update"
        prepared_rows.append(_PreparedRow(operation=operation, values=row))
        existing[stable_key] = row
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
    state = _surrogate_merge_state(table, existing_rows, rows, primary_key, stable_columns)
    for source_row in rows:
        _append_surrogate_merge_row(state, source_row)
    plan = _PreparedTable(
        bundle=bundle,
        rows=tuple(state.prepared_rows),
        preview=_preview_for_rows(state.prepared_rows, remapped=state.remapped),
    )
    return plan, state.id_map


def _surrogate_merge_state(
    table: str,
    existing_rows: list[dict[str, object]],
    source_rows: tuple[dict[str, object], ...],
    primary_key: str,
    stable_columns: tuple[str, ...] | None,
) -> _SurrogateMergeState:
    existing_by_id = {row[primary_key]: row for row in existing_rows}
    used_ids = set(existing_by_id)
    source_ids = [row[primary_key] for row in source_rows]
    existing_by_stable = (
        {_stable_row_key(table, row, stable_columns): row for row in existing_rows}
        if stable_columns is not None
        else {}
    )
    return _SurrogateMergeState(
        table=table,
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
    stable_key = _optional_row_key(state.table, source_row, state.stable_columns)
    stable_match = state.existing_by_stable.get(stable_key) if stable_key is not None else None
    if stable_match is not None:
        if state.table == "advice_review_plan" and _row_revision(source_row) < _row_revision(stable_match):
            raise ValueError("advice_review_plan 不能用旧修订回退目标数据库")
        target_id = stable_match[state.primary_key]
        row = {**source_row, state.primary_key: target_id}
        if stable_match == row:
            operation: _RowOperation = "unchanged"
        elif state.table in _IMMUTABLE_LEDGER_TABLES:
            raise ValueError(f"{state.table} 不可变账本与目标数据库冲突")
        else:
            operation = "update"
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


def _row_revision(row: dict[str, object]) -> int:
    value = row.get("revision")
    if isinstance(value, bool) or not isinstance(value, int):
        raise ValueError("advice_review_plan revision 必须是整数")
    return value


def _available_surrogate_id(state: _SurrogateMergeState, source_id: object) -> object:
    if source_id not in state.used_ids:
        return source_id
    while state.next_id in state.used_ids:
        state.next_id += 1
    target_id = state.next_id
    state.next_id += 1
    return target_id


def _optional_row_key(
    table: str,
    row: dict[str, object],
    columns: tuple[str, ...] | None,
) -> tuple[object, ...] | None:
    return _stable_row_key(table, row, columns) if columns is not None else None


def _validate_unique_stable_keys(
    table: str,
    rows: tuple[dict[str, object], ...],
    columns: tuple[str, ...],
) -> None:
    seen: set[tuple[object, ...]] = set()
    for row in rows:
        key = _stable_row_key(table, row, columns)
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


def _stable_row_key(
    table: str,
    row: dict[str, object],
    columns: tuple[str, ...],
) -> tuple[object, ...]:
    case_insensitive = _CASE_INSENSITIVE_STABLE_COLUMNS.get(table, frozenset())
    return tuple(
        cast(str, row[column]).casefold()
        if column in case_insensitive and isinstance(row[column], str)
        else row[column]
        for column in columns
    )


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
    _refresh_imported_paper_run_output_digests(conn, prepared)


def _refresh_imported_paper_run_output_digests(
    conn: sqlite3.Connection,
    prepared: dict[str, _PreparedTable],
) -> None:
    run_ids = _affected_paper_run_ids(conn, prepared, include_all_imported_runs=False)
    for run_id in sorted(run_ids):
        digest = paper_run_output_digest(conn, run_id)
        cursor = conn.execute(
            "UPDATE paper_trading_run SET output_digest = ? WHERE id = ?",
            (digest, run_id),
        )
        if cursor.rowcount != 1:
            raise ValueError("paper_trading_run 导入后无法写入输出摘要")


def _affected_paper_run_ids(
    conn: sqlite3.Connection,
    prepared: dict[str, _PreparedTable],
    *,
    include_all_imported_runs: bool,
) -> set[int]:
    runs = prepared.get("paper_trading_run")
    run_ids = (
        {
            _integer_value(row.values["id"])
            for row in runs.rows
            if include_all_imported_runs or row.operation != "unchanged"
        }
        if runs is not None
        else set()
    )
    _add_changed_paper_child_run_ids(run_ids, prepared)
    _add_changed_strategy_run_ids(conn, run_ids, prepared)
    return run_ids


def _add_changed_paper_child_run_ids(
    run_ids: set[int],
    prepared: dict[str, _PreparedTable],
) -> None:
    for table in (
        "paper_strategy_result",
        "paper_trade",
        "paper_equity_snapshot",
        "paper_trading_event",
    ):
        child = prepared.get(table)
        if child is not None:
            run_ids.update(
                _integer_value(row.values["run_id"])
                for row in child.rows
                if row.operation != "unchanged"
            )


def _add_changed_strategy_run_ids(
    conn: sqlite3.Connection,
    run_ids: set[int],
    prepared: dict[str, _PreparedTable],
) -> None:
    strategies = prepared.get("paper_strategy")
    if strategies is None:
        return
    strategy_ids = [
        _integer_value(row.values["id"])
        for row in strategies.rows
        if row.operation != "unchanged"
    ]
    for offset in range(0, len(strategy_ids), 400):
        chunk = strategy_ids[offset : offset + 400]
        if not chunk:
            continue
        placeholders = ", ".join("?" for _ in chunk)
        run_ids.update(
            int(row[0])
            for row in conn.execute(
                "SELECT DISTINCT run_id FROM paper_strategy_result "
                f"WHERE strategy_id IN ({placeholders})",
                chunk,
            ).fetchall()
        )


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
    _validate_imported_review_rows(conn, prepared, parent_rows)
    _validate_imported_paper_run_output_digests(conn, prepared)


def _validate_imported_paper_run_output_digests(
    conn: sqlite3.Connection,
    prepared: dict[str, _PreparedTable],
) -> None:
    run_ids = _affected_paper_run_ids(conn, prepared, include_all_imported_runs=True)
    for run_id in sorted(run_ids):
        stored = conn.execute(
            "SELECT output_digest FROM paper_trading_run WHERE id = ?",
            (run_id,),
        ).fetchone()
        if stored is None:
            raise ValueError("paper_trading_run 导入后不存在")
        if str(stored["output_digest"]) != paper_run_output_digest(conn, run_id):
            raise ValueError("paper_trading_run 导入后输出摘要不一致")


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
            if row[child_column] is None:
                continue
            parent = parent_rows[parent_table].get(row[child_column])
            if parent is None:
                raise ValueError(f"{child_table}.{child_column} 导入后违反外键约束")
            child_symbol = _row_symbol(row)
            parent_symbol = _row_symbol(parent)
            if child_symbol is not None and parent_symbol is not None and child_symbol != parent_symbol:
                raise ValueError(f"{child_table}.{child_column} 导入后关联到错误的父记录")


def _row_symbol(row) -> object | None:
    return row["symbol"] if "symbol" in row.keys() else None


def _validate_imported_review_rows(
    conn: sqlite3.Connection,
    prepared: dict[str, _PreparedTable],
    parent_rows: dict[str, dict[object, sqlite3.Row]],
) -> None:
    _validate_imported_review_plan_rows(conn, prepared)
    results = prepared.get("advice_review_result")
    strategies = prepared.get("paper_strategy")
    if results is None and strategies is None:
        return
    plans = parent_rows.get("advice_review_plan")
    if plans is None:
        raise ValueError("复盘子记录导入后缺少计划父记录")
    if results is not None:
        _validate_imported_review_results(conn, results, plans)
    if strategies is not None:
        _validate_imported_paper_strategies(conn, strategies, plans)


def _validate_imported_review_results(
    conn: sqlite3.Connection,
    results: _PreparedTable,
    plans: dict[object, sqlite3.Row],
) -> None:
    for prepared_row in results.rows:
        row = prepared_row.values
        if plans[row["plan_id"]]["advice_id"] != row["advice_id"]:
            raise ValueError("advice_review_result 导入后计划与建议关联不一致")
        revision = _imported_review_revision(
            conn,
            plan_id=row["plan_id"],
            revision=row["plan_revision"],
            missing_message="advice_review_result 导入后引用了不存在的计划版本",
        )
        if row["plan_payload_digest"] != str(revision["payload_digest"]):
            raise ValueError("advice_review_result 导入后计划摘要不一致")
        payload = _loaded_revision_payload(revision)
        _validate_result_plan_binding(row, payload)
        _validate_review_result_digests(row)


def _validate_imported_paper_strategies(
    conn: sqlite3.Connection,
    strategies: _PreparedTable,
    plans: dict[object, sqlite3.Row],
) -> None:
    for prepared_row in strategies.rows:
        row = prepared_row.values
        if plans[row["plan_id"]]["advice_id"] != row["advice_id"]:
            raise ValueError("paper_strategy 导入后计划与建议关联不一致")
        revision = _imported_review_revision(
            conn,
            plan_id=row["plan_id"],
            revision=row["plan_revision"],
            missing_message="paper_strategy 导入后引用了不存在的计划版本",
        )
        if row["plan_payload_digest"] != str(revision["payload_digest"]):
            raise ValueError("paper_strategy 导入后计划摘要不一致")
        _validate_paper_strategy_plan_binding(row, _loaded_revision_payload(revision))


def _validate_imported_review_plan_rows(
    conn: sqlite3.Connection,
    prepared: dict[str, _PreparedTable],
) -> None:
    plans = prepared.get("advice_review_plan")
    if plans is None:
        return
    for prepared_row in plans.rows:
        plan_id = prepared_row.values["id"]
        projection = conn.execute(
            "SELECT * FROM advice_review_plan WHERE id = ?",
            (plan_id,),
        ).fetchone()
        if projection is None:
            raise ValueError("advice_review_plan 导入后投影不存在")
        revision = _imported_review_revision(
            conn,
            plan_id=plan_id,
            revision=projection["revision"],
            missing_message="advice_review_plan 导入后缺少当前不可变版本",
        )
        payload = _loaded_revision_payload(revision)
        if (
            str(projection["plan_payload_digest"]) != str(revision["payload_digest"])
            or payload != _review_plan_payload(projection)
        ):
            raise ValueError("advice_review_plan 导入后投影与不可变版本不一致")


def _imported_review_revision(
    conn: sqlite3.Connection,
    *,
    plan_id: object,
    revision: object,
    missing_message: str,
) -> sqlite3.Row:
    row = conn.execute(
        """
        SELECT payload_json, payload_digest FROM advice_review_plan_revision
        WHERE plan_id = ? AND revision = ?
        """,
        (plan_id, revision),
    ).fetchone()
    if row is None:
        raise ValueError(missing_message)
    return row


def _loaded_revision_payload(row: sqlite3.Row) -> dict[str, object]:
    try:
        payload = json.loads(str(row["payload_json"]))
    except (TypeError, ValueError, json.JSONDecodeError):
        raise ValueError("advice_review_plan_revision 导入后载荷损坏") from None
    if not isinstance(payload, dict):
        raise ValueError("advice_review_plan_revision 导入后载荷不是对象")
    if _payload_digest(payload) != str(row["payload_digest"]):
        raise ValueError("advice_review_plan_revision 导入后摘要不一致")
    return payload


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
    return audit_now_text()


__all__ = [
    "CONFLICT_STRATEGY",
    "ImportStateCallback",
    "available_user_tables",
    "export_user_data",
    "import_user_data",
    "user_data_state_digest",
]
