from __future__ import annotations

from dataclasses import dataclass
import math
import sqlite3

from app.db.user_mappers import row_to_alert_event, row_to_alert_rule
from app.models.schemas import AlertEventItem, AlertRuleInput, AlertRuleItem, AlertRuleUpdate, Quote
from app.repositories.base import SQLiteRepository
from app.repositories.update_fields import FieldUpdate, present_updates, update_sql_parts
from app.utils.symbols import standard_symbol
from app.utils.time import now_text


def _columns_sql(columns: tuple[str, ...]) -> str:
    return ", ".join(columns)


def _named_placeholders_sql(columns: tuple[str, ...]) -> str:
    return ", ".join(f":{column}" for column in columns)


_ALERT_RULE_COLUMNS = (
    "id",
    "symbol",
    "code",
    "market",
    "stock_name",
    "name",
    "condition_type",
    "threshold",
    "note",
    "enabled",
    "last_checked_at",
    "last_triggered_at",
    "last_state",
    "trigger_count",
    "cooldown_seconds",
    "created_at",
    "updated_at",
)

_ALERT_RULE_INSERT_COLUMNS = (
    "symbol",
    "code",
    "market",
    "stock_name",
    "name",
    "condition_type",
    "threshold",
    "note",
    "enabled",
    "cooldown_seconds",
    "last_state",
    "created_at",
    "updated_at",
)

_ALERT_EVENT_COLUMNS = (
    "id",
    "rule_id",
    "symbol",
    "code",
    "market",
    "stock_name",
    "name",
    "condition_type",
    "event_type",
    "message",
    "price",
    "change_pct",
    "threshold",
    "created_at",
)

_ALERT_EVENT_INSERT_COLUMNS = _ALERT_EVENT_COLUMNS[1:]

_ALERT_RULE_SELECT_SQL = _columns_sql(_ALERT_RULE_COLUMNS)
_ALERT_EVENT_SELECT_SQL = _columns_sql(_ALERT_EVENT_COLUMNS)

_ALERT_RULE_INSERT_SQL = f"""
    INSERT INTO alert_rule (
        {_columns_sql(_ALERT_RULE_INSERT_COLUMNS)}
    ) VALUES ({_named_placeholders_sql(_ALERT_RULE_INSERT_COLUMNS)})
"""

_ALERT_RULE_STATE_SQL = """
    UPDATE alert_rule
    SET
        last_checked_at = ?,
        last_triggered_at = CASE WHEN ? THEN ? ELSE last_triggered_at END,
        last_state = ?,
        trigger_count = MAX(CAST(COALESCE(trigger_count, 0) AS INTEGER), 0) + ?,
        updated_at = ?
    WHERE id = ? AND enabled = 1
"""

_ALERT_EVENT_INSERT_SQL = f"""
    INSERT INTO alert_event (
        {_columns_sql(_ALERT_EVENT_INSERT_COLUMNS)}
    ) VALUES ({_named_placeholders_sql(_ALERT_EVENT_INSERT_COLUMNS)})
"""


@dataclass(frozen=True)
class AlertStateDecision:
    event_type: str
    should_create_event: bool
    should_update_triggered_at: bool
    trigger_increment: int


class AlertRepository(SQLiteRepository):
    def create_rule(self, quote: Quote, payload: AlertRuleInput) -> AlertRuleItem:
        timestamp = now_text()
        params = _alert_rule_insert_values(quote, payload, timestamp)
        with self._lock, self._connect() as conn:
            cursor = conn.execute(_ALERT_RULE_INSERT_SQL, params)
            row_id = int(cursor.lastrowid)
        item = self.rule(row_id)
        if item is None:
            raise RuntimeError("预警规则保存失败")
        return item

    def rule(self, row_id: int) -> AlertRuleItem | None:
        with self._lock, self._connect() as conn:
            row = conn.execute(f"SELECT {_ALERT_RULE_SELECT_SQL} FROM alert_rule WHERE id = ?", (row_id,)).fetchone()
        return row_to_alert_rule(row) if row else None

    def rules(self, symbol: str | None = None, include_disabled: bool = True, limit: int = 200) -> list[AlertRuleItem]:
        if limit <= 0:
            return []
        sql = f"SELECT {_ALERT_RULE_SELECT_SQL} FROM alert_rule"
        params: list[object] = []
        clauses = []
        if symbol:
            clauses.append("symbol = ?")
            params.append(standard_symbol(symbol))
        if not include_disabled:
            clauses.append("enabled = 1")
        if clauses:
            sql += " WHERE " + " AND ".join(clauses)
        sql += " ORDER BY enabled DESC, updated_at DESC, id DESC LIMIT ?"
        params.append(limit)
        with self._lock, self._connect() as conn:
            rows = conn.execute(sql, params).fetchall()
        items = [row_to_alert_rule(row) for row in rows]
        if not include_disabled:
            return [item for item in items if item.enabled]
        return items

    def delete_rule(self, row_id: int) -> bool:
        with self._lock, self._connect() as conn:
            cursor = conn.execute("DELETE FROM alert_rule WHERE id = ?", (row_id,))
            return cursor.rowcount > 0

    def update_rule(self, row_id: int, payload: AlertRuleUpdate) -> AlertRuleItem | None:
        updates = self._normalized_rule_updates(row_id, payload)
        if updates is None:
            return None
        if not updates:
            return self.rule(row_id)
        return self._apply_rule_updates(row_id, updates)

    def _normalized_rule_updates(self, row_id: int, payload: AlertRuleUpdate) -> list[FieldUpdate] | None:
        raw_updates = payload.model_dump(exclude_unset=True)
        updates = _alert_rule_updates(payload)
        if _empty_name_update_requested(raw_updates, updates):
            current = self.rule(row_id)
            if current is None:
                return None
            return _with_default_rule_name(updates, current)
        return updates

    def _apply_rule_updates(self, row_id: int, updates: list[FieldUpdate]) -> AlertRuleItem | None:
        assignments, params = update_sql_parts(updates)
        assignments.append("updated_at = ?")
        params.append(now_text())
        params.append(row_id)
        with self._lock, self._connect() as conn:
            cursor = conn.execute(
                f"UPDATE alert_rule SET {', '.join(assignments)} WHERE id = ?",
                params,
            )
            if cursor.rowcount <= 0:
                return None
        return self.rule(row_id)

    def update_rule_state(
        self,
        rule: AlertRuleItem,
        *,
        checked_at: str,
        state: str,
        triggered: bool,
        message: str,
        quote: Quote,
        event_type: str | None = None,
        force_event: bool = False,
        decision: AlertStateDecision | None = None,
    ) -> AlertEventItem | None:
        if not rule.enabled:
            return None
        event_id: int | None = None
        decision = decision or _alert_state_decision(rule, triggered, event_type, force_event)
        with self._lock, self._connect() as conn:
            if not _update_alert_rule_state_row(conn, rule, checked_at, state, decision):
                return None
            if decision.should_create_event:
                event_id = _insert_alert_event_row(conn, rule, quote, checked_at, message, decision.event_type)
        return self.event(event_id) if event_id else None

    def event(self, row_id: int | None) -> AlertEventItem | None:
        if row_id is None:
            return None
        with self._lock, self._connect() as conn:
            row = conn.execute(f"SELECT {_ALERT_EVENT_SELECT_SQL} FROM alert_event WHERE id = ?", (row_id,)).fetchone()
        return row_to_alert_event(row) if row else None

    def events(self, symbol: str | None = None, limit: int = 100) -> list[AlertEventItem]:
        if limit <= 0:
            return []
        sql = f"SELECT {_ALERT_EVENT_SELECT_SQL} FROM alert_event"
        params: list[object] = []
        if symbol:
            sql += " WHERE symbol = ?"
            params.append(standard_symbol(symbol))
        sql += " ORDER BY created_at DESC, id DESC LIMIT ?"
        params.append(limit)
        with self._lock, self._connect() as conn:
            rows = conn.execute(sql, params).fetchall()
        return [row_to_alert_event(row) for row in rows]


def _default_rule_name(condition_type: str, threshold: float) -> str:
    labels = {
        "price_above": "价格上穿",
        "price_below": "价格下破",
        "change_pct_above": "涨幅提醒",
        "change_pct_below": "跌幅提醒",
        "trend_score_above": "趋势转强",
        "trend_score_below": "趋势转弱",
        "break_support": "跌破支撑",
        "break_resistance": "突破压力",
    }
    return f"{labels.get(condition_type, '个股提醒')} {threshold:g}"


def _alert_rule_insert_values(quote: Quote, payload: AlertRuleInput, timestamp: str) -> dict[str, object | None]:
    threshold = _clean_alert_threshold(payload.threshold)
    return {
        "symbol": standard_symbol(f"{quote.market}{quote.code}"),
        "code": quote.code,
        "market": quote.market,
        "stock_name": quote.name,
        "name": _clean_alert_rule_name(payload.name, payload.condition_type, threshold),
        "condition_type": payload.condition_type,
        "threshold": threshold,
        "note": _clean_alert_rule_note(payload.note),
        "enabled": _clean_alert_enabled(payload.enabled),
        "cooldown_seconds": _clean_alert_cooldown_seconds(payload.cooldown_seconds),
        "last_state": "等待",
        "created_at": timestamp,
        "updated_at": timestamp,
    }


def _clean_alert_rule_name(name: str | None, condition_type: str, threshold: float) -> str:
    cleaned = (name or "").strip()[:40]
    return cleaned or _default_rule_name(condition_type, threshold)


def _clean_alert_rule_note(note: str | None) -> str | None:
    return (note.strip()[:160] or None) if note else None


def _clean_alert_threshold(value: float | None) -> float:
    threshold = _required_value(value, "预警阈值不能为空")
    if isinstance(threshold, bool):
        raise ValueError("预警阈值必须是有效数字")
    try:
        cleaned = float(threshold)
    except (TypeError, ValueError) as exc:
        raise ValueError("预警阈值必须是有效数字") from exc
    if not math.isfinite(cleaned):
        raise ValueError("预警阈值必须是有效数字")
    return cleaned


def _clean_alert_enabled(value: bool | None) -> int:
    return int(_required_value(value, "启用状态不能为空"))


def _clean_alert_cooldown_seconds(value: int | None) -> int:
    seconds = _required_value(value, "冷却时间不能为空")
    if isinstance(seconds, bool) or not isinstance(seconds, int):
        raise ValueError("冷却时间必须是有效整数")
    if seconds < 30 or seconds > 86400:
        raise ValueError("冷却时间必须在30到86400秒之间")
    return seconds


def _alert_rule_updates(payload: AlertRuleUpdate) -> list[FieldUpdate]:
    return present_updates(
        payload,
        {
            "name": lambda value: (value or "").strip()[:40],
            "threshold": _clean_alert_threshold,
            "note": _clean_alert_rule_note,
            "enabled": _clean_alert_enabled,
            "cooldown_seconds": _clean_alert_cooldown_seconds,
        },
    )


def _required_value(value, message: str):
    if value is None:
        raise ValueError(message)
    return value


def _field_value(updates: list[FieldUpdate], column: str):
    for item in updates:
        if item.column == column:
            return item.value
    return None


def _empty_name_update_requested(raw_updates: dict, updates: list[FieldUpdate]) -> bool:
    return "name" in raw_updates and _field_value(updates, "name") == ""


def _with_default_rule_name(updates: list[FieldUpdate], current: AlertRuleItem) -> list[FieldUpdate]:
    threshold = _effective_alert_threshold(updates, current)
    return _replace_field(updates, FieldUpdate("name", _default_rule_name(current.condition_type, threshold)))


def _effective_alert_threshold(updates: list[FieldUpdate], current: AlertRuleItem) -> float:
    updated_threshold = _field_value(updates, "threshold")
    return current.threshold if updated_threshold is None else updated_threshold


def _replace_field(updates: list[FieldUpdate], replacement: FieldUpdate) -> list[FieldUpdate]:
    return [replacement if item.column == replacement.column else item for item in updates]


def _alert_state_decision(
    rule: AlertRuleItem,
    triggered: bool,
    event_type: str | None,
    force_event: bool,
) -> AlertStateDecision:
    should_create_event = _fallback_should_create_alert_event(rule, triggered, force_event)
    return AlertStateDecision(
        event_type=event_type or _fallback_alert_event_type(triggered),
        should_create_event=should_create_event,
        should_update_triggered_at=_should_update_triggered_at(should_create_event, triggered),
        trigger_increment=_trigger_increment(should_create_event, triggered),
    )


def _fallback_should_create_alert_event(rule: AlertRuleItem, triggered: bool, force_event: bool) -> bool:
    if force_event:
        return True
    return _fallback_trigger_state_changed(rule, triggered)


def _fallback_trigger_state_changed(rule: AlertRuleItem, triggered: bool) -> bool:
    return triggered and rule.last_state != "触发"


def _fallback_alert_event_type(triggered: bool) -> str:
    return "触发" if triggered else "恢复"


def _should_update_triggered_at(should_create_event: bool, triggered: bool) -> bool:
    return should_create_event and triggered


def _trigger_increment(should_create_event: bool, triggered: bool) -> int:
    return 1 if should_create_event and triggered else 0


def _update_alert_rule_state_row(
    conn: sqlite3.Connection,
    rule: AlertRuleItem,
    checked_at: str,
    state: str,
    decision: AlertStateDecision,
) -> bool:
    cursor = conn.execute(
        _ALERT_RULE_STATE_SQL,
        (
            checked_at,
            int(decision.should_update_triggered_at),
            checked_at,
            state,
            decision.trigger_increment,
            checked_at,
            rule.id,
        ),
    )
    return cursor.rowcount > 0


def _insert_alert_event_row(
    conn: sqlite3.Connection,
    rule: AlertRuleItem,
    quote: Quote,
    checked_at: str,
    message: str,
    event_type: str,
) -> int:
    cursor = conn.execute(
        _ALERT_EVENT_INSERT_SQL,
        {
            "rule_id": rule.id,
            "symbol": rule.symbol,
            "code": rule.code,
            "market": rule.market,
            "stock_name": rule.stock_name or quote.name,
            "name": rule.name,
            "condition_type": rule.condition_type,
            "event_type": event_type,
            "message": message,
            "price": _finite_float_or_default(quote.price),
            "change_pct": _finite_float_or_default(quote.change_pct),
            "threshold": _finite_float_or_default(rule.threshold),
            "created_at": checked_at,
        },
    )
    return int(cursor.lastrowid)


def _finite_float_or_default(value: object, default: float = 0.0) -> float:
    try:
        parsed = float(value)
    except (TypeError, ValueError):
        return default
    return parsed if math.isfinite(parsed) else default
