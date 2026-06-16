from __future__ import annotations

from app.db.mappers import row_to_alert_event, row_to_alert_rule
from app.models.schemas import AlertEventItem, AlertRuleInput, AlertRuleItem, AlertRuleUpdate, Quote
from app.repositories.base import SQLiteRepository
from app.utils.symbols import standard_symbol
from app.utils.time import now_text


class AlertRepository(SQLiteRepository):
    def create_rule(self, quote: Quote, payload: AlertRuleInput) -> AlertRuleItem:
        symbol = standard_symbol(f"{quote.market}{quote.code}")
        timestamp = now_text()
        name = (payload.name or _default_rule_name(payload.condition_type, payload.threshold)).strip()[:40]
        note = (payload.note.strip()[:160] or None) if payload.note else None
        with self._lock, self._connect() as conn:
            cursor = conn.execute(
                """
                INSERT INTO alert_rule (
                    symbol, code, market, stock_name, name, condition_type, threshold, note,
                    enabled, cooldown_seconds, last_state, created_at, updated_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    symbol,
                    quote.code,
                    quote.market,
                    quote.name,
                    name,
                    payload.condition_type,
                    payload.threshold,
                    note,
                    int(payload.enabled),
                    payload.cooldown_seconds,
                    "等待",
                    timestamp,
                    timestamp,
                ),
            )
            row_id = int(cursor.lastrowid)
        item = self.rule(row_id)
        if item is None:
            raise RuntimeError("预警规则保存失败")
        return item

    def rule(self, row_id: int) -> AlertRuleItem | None:
        with self._lock, self._connect() as conn:
            row = conn.execute("SELECT * FROM alert_rule WHERE id = ?", (row_id,)).fetchone()
        return row_to_alert_rule(row) if row else None

    def rules(self, symbol: str | None = None, include_disabled: bool = True, limit: int = 200) -> list[AlertRuleItem]:
        sql = "SELECT * FROM alert_rule"
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
        return [row_to_alert_rule(row) for row in rows]

    def delete_rule(self, row_id: int) -> bool:
        with self._lock, self._connect() as conn:
            cursor = conn.execute("DELETE FROM alert_rule WHERE id = ?", (row_id,))
            return cursor.rowcount > 0

    def update_rule(self, row_id: int, payload: AlertRuleUpdate) -> AlertRuleItem | None:
        updates = payload.model_dump(exclude_unset=True)
        if not updates:
            return self.rule(row_id)

        assignments: list[str] = []
        params: list[object] = []
        if "name" in updates:
            name = (payload.name or "").strip()[:40]
            if not name:
                current = self.rule(row_id)
                if current is None:
                    return None
                name = _default_rule_name(current.condition_type, current.threshold)
            assignments.append("name = ?")
            params.append(name)
        if "threshold" in updates:
            if payload.threshold is None:
                raise ValueError("预警阈值不能为空")
            assignments.append("threshold = ?")
            params.append(payload.threshold)
        if "note" in updates:
            note = (payload.note.strip()[:160] or None) if payload.note else None
            assignments.append("note = ?")
            params.append(note)
        if "enabled" in updates:
            if payload.enabled is None:
                raise ValueError("启用状态不能为空")
            assignments.append("enabled = ?")
            params.append(int(payload.enabled))
        if "cooldown_seconds" in updates:
            if payload.cooldown_seconds is None:
                raise ValueError("冷却时间不能为空")
            assignments.append("cooldown_seconds = ?")
            params.append(payload.cooldown_seconds)

        if not assignments:
            return self.rule(row_id)

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
    ) -> AlertEventItem | None:
        event_id: int | None = None
        resolved_event_type = event_type or ("触发" if triggered else "恢复")
        should_create_event = force_event or (triggered and rule.last_state != "触发")
        should_update_triggered_at = should_create_event and triggered
        trigger_increment = 1 if should_create_event and triggered else 0
        with self._lock, self._connect() as conn:
            conn.execute(
                """
                UPDATE alert_rule
                SET
                    last_checked_at = ?,
                    last_triggered_at = CASE WHEN ? THEN ? ELSE last_triggered_at END,
                    last_state = ?,
                    trigger_count = trigger_count + ?,
                    updated_at = ?
                WHERE id = ?
                """,
                (
                    checked_at,
                    int(should_update_triggered_at),
                    checked_at,
                    state,
                    trigger_increment,
                    checked_at,
                    rule.id,
                ),
            )
            if should_create_event:
                cursor = conn.execute(
                    """
                    INSERT INTO alert_event (
                        rule_id, symbol, code, market, stock_name, name, condition_type, event_type,
                        message, price, change_pct, threshold, created_at
                    ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                    """,
                    (
                        rule.id,
                        rule.symbol,
                        rule.code,
                        rule.market,
                        rule.stock_name or quote.name,
                        rule.name,
                        rule.condition_type,
                        resolved_event_type,
                        message,
                        quote.price,
                        quote.change_pct,
                        rule.threshold,
                        checked_at,
                    ),
                )
                event_id = int(cursor.lastrowid)
        return self.event(event_id) if event_id else None

    def event(self, row_id: int | None) -> AlertEventItem | None:
        if row_id is None:
            return None
        with self._lock, self._connect() as conn:
            row = conn.execute("SELECT * FROM alert_event WHERE id = ?", (row_id,)).fetchone()
        return row_to_alert_event(row) if row else None

    def events(self, symbol: str | None = None, limit: int = 100) -> list[AlertEventItem]:
        sql = "SELECT * FROM alert_event"
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
