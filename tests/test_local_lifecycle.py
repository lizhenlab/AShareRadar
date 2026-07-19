from __future__ import annotations

import asyncio
from concurrent.futures import ThreadPoolExecutor, TimeoutError as FutureTimeoutError
from datetime import datetime, timedelta
import math
import sqlite3
from threading import Barrier, Event
from types import SimpleNamespace
import unittest
from unittest.mock import patch
from pathlib import Path
from tempfile import TemporaryDirectory

from app.db.schema_migrations import apply_compat_schema, ensure_column, run_once, table_exists
from app.models.schemas import AlertRuleInput, AlertRuleUpdate, MinuteKline, Quote, StockConceptItem, StockNoteInput, StockNoteItem, StockNoteUpdate
from app.services import research
from app.services.cache import SQLiteCache
from app.services.chart_marks import _note_marks
from app.services.analysis import build_analysis
from app.services.data_quality import build_data_quality
from app.services.datahub import DataHub
from app.services.market_sampling import unique_standard_symbols
from app.repositories.alerts import AlertStateDecision
from app.services.research import (
    build_feature_snapshot,
    build_theme_context_report,
)
from app.services.scheduler import LocalDataScheduler
from app.services.stock_insights import build_stock_insight_bundle
from app.services.workbench_context import WorkbenchContextCache
from app.config import Settings
from app.utils.time import now_text
from app.workflows import individual
from tests.factories import (
    make_kline as _kline,
    make_plate_item as _plate_item,
    make_quote as _quote,
    make_stock_info as _stock_info,
)


def _analysis_for_advice():
    quote = _quote()
    klines = [_kline(date=f"2026-05-{index + 1:02d}") for index in range(30)]
    return build_analysis(quote, klines, data_quality=build_data_quality(quote, klines))


class LocalLifecycleTests(unittest.TestCase):
    def test_watchlist_readd_preserves_existing_optional_fields_until_overridden(self) -> None:
        with TemporaryDirectory() as tmpdir:
            cache = SQLiteCache(Path(tmpdir) / "cache.sqlite3")
            quote = _quote()

            first = cache.save_watchlist_item(quote, note="核心观察", group_name="白酒", pinned=True)
            second = cache.save_watchlist_item(quote.model_copy(update={"name": "贵州茅台A"}))
            third = cache.save_watchlist_item(quote, note="", group_name="", pinned=False)

        self.assertEqual(first.note, "核心观察")
        self.assertEqual(second.name, "贵州茅台A")
        self.assertEqual(second.note, "核心观察")
        self.assertEqual(second.group_name, "白酒")
        self.assertTrue(second.pinned)
        self.assertIsNone(third.note)
        self.assertEqual(third.group_name, "默认")
        self.assertFalse(third.pinned)

    def test_watchlist_latest_quote_respects_cache_ttl_future_and_dirty_values(self) -> None:
        with TemporaryDirectory() as tmpdir:
            path = Path(tmpdir) / "cache.sqlite3"
            settings = Settings(cache_path=path, quote_cache_seconds=60)
            cache = SQLiteCache(settings=settings)
            quote = _quote()

            cache.save_watchlist_item(quote)
            cache.save_quotes([quote])
            self.assertEqual(cache.watchlist_item("600519.SH").latest_price, quote.price)

            stale = (datetime.now() - timedelta(seconds=120)).strftime("%Y-%m-%d %H:%M:%S")
            with cache._connect() as conn:
                conn.execute("UPDATE quote_snapshot SET fetched_at = ?", (stale,))
            self.assertIsNone(cache.watchlist_item("600519.SH").latest_price)

            future = (datetime.now() + timedelta(seconds=120)).strftime("%Y-%m-%d %H:%M:%S")
            with cache._connect() as conn:
                conn.execute("UPDATE quote_snapshot SET fetched_at = ?", (future,))
            self.assertIsNone(cache.watchlist_item("600519.SH").latest_price)

            current = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
            with cache._connect() as conn:
                conn.execute("UPDATE quote_snapshot SET fetched_at = ?, price = ?", (current, math.inf))
            dirty = cache.watchlist_item("600519.SH")

        self.assertIsNotNone(dirty)
        assert dirty is not None
        self.assertIsNone(dirty.latest_price)
        self.assertIsNone(dirty.latest_change_pct)
        self.assertIsNone(dirty.latest_source)

    def test_alert_rule_can_be_updated_without_losing_identity(self) -> None:
        with TemporaryDirectory() as tmpdir:
            cache = SQLiteCache(Path(tmpdir) / "cache.sqlite3")
            quote = _quote()
            created = cache.create_alert_rule(
                quote,
                AlertRuleInput(
                    symbol="600519",
                    condition_type="price_above",
                    threshold=1300.0,
                    note="初始提醒",
                ),
            )

            updated = cache.update_alert_rule(
                created.id,
                AlertRuleUpdate(name="关键压力观察", threshold=1350.5, note=None, enabled=False, cooldown_seconds=900),
            )

            self.assertIsNotNone(updated)
            assert updated is not None
            self.assertEqual(updated.id, created.id)
            self.assertEqual(updated.name, "关键压力观察")
            self.assertEqual(updated.threshold, 1350.5)
            self.assertIsNone(updated.note)
            self.assertFalse(updated.enabled)
            self.assertEqual(updated.cooldown_seconds, 900)

    def test_alert_rule_empty_name_uses_effective_updated_threshold(self) -> None:
        with TemporaryDirectory() as tmpdir:
            cache = SQLiteCache(Path(tmpdir) / "cache.sqlite3")
            created = cache.create_alert_rule(
                _quote(),
                AlertRuleInput(symbol="600519", condition_type="price_above", threshold=1300.0, name="旧提醒"),
            )

            updated = cache.update_alert_rule(created.id, AlertRuleUpdate(name="", threshold=1400.0))

            self.assertIsNotNone(updated)
            assert updated is not None
            self.assertEqual(updated.name, "价格上穿 1400")
            self.assertEqual(updated.threshold, 1400.0)

    def test_alert_rule_semantic_update_resets_stale_evaluation_state(self) -> None:
        with TemporaryDirectory() as tmpdir:
            cache = SQLiteCache(Path(tmpdir) / "cache.sqlite3")
            created = cache.create_alert_rule(
                _quote(),
                AlertRuleInput(symbol="600519", condition_type="price_above", threshold=1400.0),
            )
            with sqlite3.connect(cache.path) as conn:
                conn.execute(
                    """
                    UPDATE alert_rule
                    SET last_checked_at = '2026-05-13 10:00:00',
                        last_triggered_at = '2026-05-13 10:00:00',
                        last_state = '触发'
                    WHERE id = ?
                    """,
                    (created.id,),
                )

            updated = cache.update_alert_rule(
                created.id,
                AlertRuleUpdate(condition_type="price_below", threshold=1200.0),
            )

            self.assertIsNotNone(updated)
            assert updated is not None
            self.assertEqual(updated.condition_type, "price_below")
            self.assertEqual(updated.last_state, "等待")
            self.assertIsNone(updated.last_checked_at)
            self.assertIsNone(updated.last_triggered_at)

    def test_alert_rule_create_blank_name_uses_default_name(self) -> None:
        with TemporaryDirectory() as tmpdir:
            cache = SQLiteCache(Path(tmpdir) / "cache.sqlite3")

            created = cache.create_alert_rule(
                _quote(),
                AlertRuleInput(symbol="600519", condition_type="price_below", threshold=1200.0, name="   "),
            )

            self.assertEqual(created.name, "价格下破 1200")

    def test_alert_rule_repository_rejects_non_finite_thresholds(self) -> None:
        with TemporaryDirectory() as tmpdir:
            cache = SQLiteCache(Path(tmpdir) / "cache.sqlite3")
            quote = _quote()

            with self.assertRaisesRegex(ValueError, "预警阈值必须是有效数字"):
                cache.create_alert_rule(
                    quote,
                    AlertRuleInput.model_construct(
                        symbol="600519",
                        condition_type="price_above",
                        threshold=math.inf,
                        enabled=True,
                        cooldown_seconds=300,
                    ),
                )

            created = cache.create_alert_rule(
                quote,
                AlertRuleInput(symbol="600519", condition_type="price_above", threshold=1300.0),
            )
            with self.assertRaisesRegex(ValueError, "预警阈值必须是有效数字"):
                cache.update_alert_rule(created.id, AlertRuleUpdate.model_construct(threshold=math.nan))

    def test_alert_rule_mapper_sanitizes_legacy_dirty_rows(self) -> None:
        with TemporaryDirectory() as tmpdir:
            cache = SQLiteCache(Path(tmpdir) / "cache.sqlite3")
            created = cache.create_alert_rule(
                _quote(),
                AlertRuleInput(symbol="600519", condition_type="price_above", threshold=1300.0),
            )
            bad_threshold_rule = cache.create_alert_rule(
                _quote(),
                AlertRuleInput(symbol="600519", condition_type="price_above", threshold=1400.0),
            )

            with cache._connect() as conn:
                conn.execute(
                    """
                    UPDATE alert_rule
                    SET
                        condition_type = ?,
                        threshold = ?,
                        trigger_count = ?,
                        cooldown_seconds = ?,
                        name = ?,
                        note = ?,
                        last_checked_at = ?,
                        last_triggered_at = ?
                    WHERE id = ?
                    """,
                    ("unknown_condition", 1300.0, -7, -1, "   ", "   ", "   ", "   ", created.id),
                )
                conn.execute(
                    "UPDATE alert_rule SET threshold = ?, enabled = ? WHERE id = ?",
                    (math.inf, 1, bad_threshold_rule.id),
                )

            dirty = cache.alert_rule(created.id)
            dirty_threshold = cache.alert_rule(bad_threshold_rule.id)
            enabled_rules = cache.alert_rules(include_disabled=False)

        self.assertIsNotNone(dirty)
        assert dirty is not None
        self.assertFalse(dirty.enabled)
        self.assertEqual(dirty.condition_type, "unknown_condition")
        self.assertEqual(dirty.threshold, 1300.0)
        self.assertEqual(dirty.trigger_count, 0)
        self.assertEqual(dirty.cooldown_seconds, 300)
        self.assertTrue(dirty.name)
        self.assertIsNone(dirty.note)
        self.assertIsNone(dirty.last_checked_at)
        self.assertIsNone(dirty.last_triggered_at)
        self.assertIsNotNone(dirty_threshold)
        assert dirty_threshold is not None
        self.assertFalse(dirty_threshold.enabled)
        self.assertEqual(dirty_threshold.threshold, 0.0)
        self.assertEqual(enabled_rules, [])

    def test_alert_rule_state_update_normalizes_dirty_trigger_count_and_event_values(self) -> None:
        with TemporaryDirectory() as tmpdir:
            cache = SQLiteCache(Path(tmpdir) / "cache.sqlite3")
            quote = _quote()
            created = cache.create_alert_rule(
                quote,
                AlertRuleInput(symbol="600519", condition_type="price_above", threshold=1300.0),
            )
            with cache._connect() as conn:
                conn.execute("UPDATE alert_rule SET trigger_count = ? WHERE id = ?", (-5, created.id))

            dirty_rule = created.model_copy(update={"threshold": math.inf})
            dirty_quote_data = quote.model_dump()
            dirty_quote_data.update({"price": math.inf, "change_pct": -math.inf})
            dirty_quote = Quote.model_construct(**dirty_quote_data)
            event = cache.update_alert_rule_state(
                dirty_rule,
                checked_at="2026-05-13 10:00:00",
                state="触发",
                triggered=True,
                message="触发",
                quote=dirty_quote,
                force_event=True,
            )
            updated = cache.alert_rule(created.id)

        self.assertIsNotNone(updated)
        assert updated is not None
        self.assertEqual(updated.trigger_count, 1)
        self.assertIsNotNone(event)
        assert event is not None
        self.assertEqual(event.price, 0.0)
        self.assertEqual(event.change_pct, 0.0)
        self.assertEqual(event.threshold, 0.0)

    def test_alert_rule_state_update_skips_disabled_or_deleted_stale_rules(self) -> None:
        with TemporaryDirectory() as tmpdir:
            cache = SQLiteCache(Path(tmpdir) / "cache.sqlite3")
            quote = _quote()
            disabled_rule = cache.create_alert_rule(
                quote,
                AlertRuleInput(symbol="600519", condition_type="price_above", threshold=1200.0),
            )
            cache.update_alert_rule(disabled_rule.id, AlertRuleUpdate(enabled=False))

            disabled_event = cache.update_alert_rule_state(
                disabled_rule,
                checked_at="2026-05-13 10:00:00",
                state="触发",
                triggered=True,
                message="禁用旧对象不应产生事件",
                quote=quote,
                force_event=True,
            )
            disabled_after = cache.alert_rule(disabled_rule.id)

            deleted_rule = cache.create_alert_rule(
                quote,
                AlertRuleInput(symbol="600519", condition_type="price_above", threshold=1250.0),
            )
            cache.delete_alert_rule(deleted_rule.id)
            deleted_event = cache.update_alert_rule_state(
                deleted_rule,
                checked_at="2026-05-13 10:01:00",
                state="触发",
                triggered=True,
                message="删除旧对象不应产生事件",
                quote=quote,
                force_event=True,
            )
            events = cache.alert_events(limit=10)

        self.assertIsNone(disabled_event)
        self.assertIsNone(deleted_event)
        self.assertEqual(events, [])
        self.assertIsNotNone(disabled_after)
        assert disabled_after is not None
        self.assertIsNone(disabled_after.last_checked_at)
        self.assertEqual(disabled_after.trigger_count, 0)

    def test_alert_rule_state_compare_and_swap_blocks_duplicate_concurrent_event(self) -> None:
        with TemporaryDirectory() as tmpdir:
            cache = SQLiteCache(Path(tmpdir) / "cache.sqlite3")
            quote = _quote()
            snapshot = cache.create_alert_rule(
                quote,
                AlertRuleInput(symbol="600519", condition_type="price_above", threshold=1200.0),
            )
            decision = AlertStateDecision(
                event_type="触发",
                should_create_event=True,
                should_update_triggered_at=True,
                trigger_increment=1,
            )

            first = cache.update_alert_rule_state(
                snapshot,
                checked_at="2026-05-13 10:00:00",
                state="触发",
                triggered=True,
                message="第一次并发触发",
                quote=quote,
                decision=decision,
            )
            second = cache.update_alert_rule_state(
                snapshot,
                checked_at="2026-05-13 10:00:00",
                state="触发",
                triggered=True,
                message="第二次旧快照触发",
                quote=quote,
                decision=decision,
            )
            current = cache.alert_rule(snapshot.id)
            events = cache.alert_events(symbol=snapshot.symbol)

        self.assertIsNotNone(first)
        self.assertIsNone(second)
        assert current is not None
        self.assertEqual(current.trigger_count, 1)
        self.assertEqual(len(events), 1)
        self.assertEqual(events[0].message, "第一次并发触发")

    def test_alert_rule_state_compare_and_swap_rejects_changed_threshold_snapshot(self) -> None:
        with TemporaryDirectory() as tmpdir:
            cache = SQLiteCache(Path(tmpdir) / "cache.sqlite3")
            quote = _quote()
            snapshot = cache.create_alert_rule(
                quote,
                AlertRuleInput(symbol="600519", condition_type="price_above", threshold=1200.0),
            )
            cache.update_alert_rule(snapshot.id, AlertRuleUpdate(threshold=1300.0))
            stale_event = cache.update_alert_rule_state(
                snapshot,
                checked_at="2026-05-13 10:00:00",
                state="触发",
                triggered=True,
                message="旧阈值不应触发",
                quote=quote,
                decision=AlertStateDecision("触发", True, True, 1),
            )
            current = cache.alert_rule(snapshot.id)
            events = cache.alert_events(symbol=snapshot.symbol)

        self.assertIsNone(stale_event)
        assert current is not None
        self.assertEqual(current.threshold, 1300.0)
        self.assertEqual(current.last_state, "等待")
        self.assertEqual(current.trigger_count, 0)
        self.assertEqual(events, [])

    def test_stock_note_can_toggle_visibility_and_clear_anchor(self) -> None:
        with TemporaryDirectory() as tmpdir:
            cache = SQLiteCache(Path(tmpdir) / "cache.sqlite3")
            quote = _quote()
            created = cache.create_stock_note(
                quote,
                StockNoteInput(
                    symbol="600519",
                    content="回踩后观察承接。",
                    note_type="观察",
                    price=1288.0,
                    trade_date="2026-05-13 10:00:00",
                    visible=True,
                ),
            )

            updated = cache.update_stock_note(
                created.id,
                StockNoteUpdate(content="改为只保留复盘，不上图。", note_type="复盘", price=None, visible=False),
            )

            self.assertIsNotNone(updated)
            assert updated is not None
            self.assertEqual(updated.id, created.id)
            self.assertEqual(updated.content, "改为只保留复盘，不上图。")
            self.assertEqual(updated.note_type, "复盘")
            self.assertIsNone(updated.price)
            self.assertFalse(updated.visible)
            self.assertEqual(cache.stock_notes("600519", visible_only=True), [])

    def test_stock_note_update_can_clear_trade_date_and_preserves_stable_order(self) -> None:
        with TemporaryDirectory() as tmpdir:
            cache = SQLiteCache(Path(tmpdir) / "cache.sqlite3")
            quote = _quote()
            first = cache.create_stock_note(
                quote,
                StockNoteInput(symbol="600519", content="第一条", trade_date="2026-05-13 10:00:00"),
            )
            second = cache.create_stock_note(
                quote,
                StockNoteInput(symbol="600519", content="第二条", trade_date="2026-05-13 10:00:00"),
            )

            ordered_before_clear = cache.stock_notes("600519", limit=10)
            cleared = cache.update_stock_note(first.id, StockNoteUpdate(trade_date="   "))

        self.assertIsNotNone(cleared)
        assert cleared is not None
        self.assertIsNone(cleared.trade_date)
        self.assertEqual([item.id for item in ordered_before_clear], [second.id, first.id])

    def test_stock_note_create_rejects_blank_content_and_non_positive_price(self) -> None:
        with TemporaryDirectory() as tmpdir:
            cache = SQLiteCache(Path(tmpdir) / "cache.sqlite3")
            quote = _quote()

            with self.assertRaisesRegex(ValueError, "笔记内容不能为空"):
                cache.create_stock_note(quote, StockNoteInput(symbol="600519", content="   "))

            with self.assertRaisesRegex(ValueError, "笔记价格必须大于0"):
                cache.create_stock_note(quote, StockNoteInput(symbol="600519", content="观察", price=0))

    def test_stock_note_create_rejects_non_finite_price(self) -> None:
        with TemporaryDirectory() as tmpdir:
            cache = SQLiteCache(Path(tmpdir) / "cache.sqlite3")

            with self.assertRaisesRegex(ValueError, "笔记价格必须是有效数字"):
                cache.create_stock_note(
                    _quote(),
                    StockNoteInput.model_construct(symbol="600519", content="观察", price=math.nan),
                )

    def test_stock_note_update_rejects_non_positive_price(self) -> None:
        with TemporaryDirectory() as tmpdir:
            cache = SQLiteCache(Path(tmpdir) / "cache.sqlite3")
            created = cache.create_stock_note(_quote(), StockNoteInput(symbol="600519", content="观察", price=1288.0))

            with self.assertRaisesRegex(ValueError, "笔记价格必须大于0"):
                cache.update_stock_note(created.id, StockNoteUpdate(price=-1))

            with self.assertRaisesRegex(ValueError, "笔记价格必须是有效数字"):
                cache.update_stock_note(created.id, StockNoteUpdate.model_construct(price=math.inf))

    def test_dirty_legacy_stock_note_row_remains_displayable(self) -> None:
        with TemporaryDirectory() as tmpdir:
            path = Path(tmpdir) / "cache.sqlite3"
            cache = SQLiteCache(path)
            created = cache.create_stock_note(_quote(), StockNoteInput(symbol="600519", content="观察", price=1288.0))
            with sqlite3.connect(path) as conn:
                conn.execute(
                    """
                    UPDATE stock_note
                    SET name = '', note_type = '', content = '', price = 'bad', visible = 'bad',
                        trade_date = '  ', color = '  ', created_at = '', updated_at = ''
                    WHERE id = ?
                    """,
                    (created.id,),
                )

            notes = cache.stock_notes("600519", limit=5)

        self.assertEqual(len(notes), 1)
        note = notes[0]
        self.assertEqual(note.name, "未知股票")
        self.assertEqual(note.note_type, "观察")
        self.assertEqual(note.content, "历史笔记字段异常，已使用兜底展示。")
        self.assertIsNone(note.price)
        self.assertIsNone(note.trade_date)
        self.assertIsNone(note.color)
        self.assertFalse(note.visible)
        self.assertEqual(note.created_at, "")
        self.assertEqual(note.updated_at, "")

    def test_runtime_event_log_write_failure_is_best_effort(self) -> None:
        with TemporaryDirectory() as tmpdir:
            cache = SQLiteCache(Path(tmpdir) / "cache.sqlite3")
            cache.runtime_event_repo = _FailingRuntimeEventRepo()  # type: ignore[assignment]

            cache.log_event("fallback", "事件库暂不可写")

    def test_legacy_invalid_stock_note_trade_date_does_not_align_to_kline(self) -> None:
        marks = _note_marks(
            [
                StockNoteItem(
                    id=1,
                    symbol="600519.SH",
                    code="600519",
                    market="SH",
                    name="贵州茅台",
                    note_type="观察",
                    content="历史坏日期不应贴到K线。",
                    price=1288.0,
                    trade_date="2026-05-13坏值",
                    color="#2563eb",
                    visible=True,
                    created_at="2026-05-13 10:00:00",
                    updated_at="2026-05-13 10:00:00",
                )
            ]
        )

        self.assertEqual(marks[0].date, "2026-05-13坏值")
        self.assertIsNone(marks[0].kline_date)

    def test_runtime_cleanup_handles_minute_kline_table_without_id(self) -> None:
        with TemporaryDirectory() as tmpdir:
            path = Path(tmpdir) / "cache.sqlite3"
            settings = Settings(cache_path=path, max_minute_kline_rows=3)
            cache = SQLiteCache(settings=settings)
            rows = [
                MinuteKline(
                    timestamp=f"2026-05-15 10:{index:02d}:00",
                    open=100 + index,
                    close=100 + index,
                    high=101 + index,
                    low=99 + index,
                    volume=1000 + index,
                    interval="5m",
                    source="测试分钟线",
                )
                for index in range(6)
            ]
            cache.save_minute_klines("600519.SH", "5m", rows, "测试分钟线")
            cache.save_minute_klines("000001.SZ", "5m", rows[:1], "测试分钟线")
            removed = cache.cleanup_runtime_rows()
            with cache._connect() as conn:
                counts_by_symbol = {
                    row["symbol"]: row["count"]
                    for row in conn.execute(
                        """
                        SELECT symbol, COUNT(*) AS count
                        FROM kline_minute
                        GROUP BY symbol
                        """
                    ).fetchall()
                }

        self.assertIn("kline_minute", removed)
        self.assertEqual(counts_by_symbol["600519.SH"], 3)
        self.assertEqual(counts_by_symbol["000001.SZ"], 1)

    def test_runtime_cleanup_keeps_quote_history_for_cold_symbols(self) -> None:
        with TemporaryDirectory() as tmpdir:
            path = Path(tmpdir) / "cache.sqlite3"
            settings = Settings(cache_path=path, max_quote_history_rows=120)
            cache = SQLiteCache(settings=settings)
            first_day = datetime(2025, 1, 1)
            cache.save_quotes(
                [
                    _quote(
                        timestamp=f"{(first_day + timedelta(days=offset)):%Y-%m-%d} 10:00:00",
                        price=1300 + offset,
                        high=1305 + offset,
                    )
                    for offset in range(122)
                ]
            )
            cache.save_quotes(
                [
                    _quote(timestamp="2026-05-10 10:00:00", price=12.0).model_copy(
                        update={
                            "code": "000001",
                            "market": "SZ",
                            "name": "平安银行",
                            "prev_close": 11.8,
                            "open": 11.9,
                            "high": 12.1,
                            "low": 11.7,
                            "change": 0.2,
                        }
                    )
                ]
            )

            removed = cache.cleanup_runtime_rows()
            with cache._connect() as conn:
                counts_by_symbol = {
                    row["symbol"]: row["count"]
                    for row in conn.execute(
                        """
                        SELECT symbol, COUNT(*) AS count
                        FROM quote_history
                        GROUP BY symbol
                        """
                    ).fetchall()
                }

        self.assertEqual(removed["quote_history"], 2)
        self.assertEqual(counts_by_symbol["600519.SH"], 120)
        self.assertEqual(counts_by_symbol["000001.SZ"], 1)

    def test_runtime_cleanup_trims_cache_and_alert_events(self) -> None:
        with TemporaryDirectory() as tmpdir:
            path = Path(tmpdir) / "cache.sqlite3"
            settings = Settings(cache_path=path, max_cache_event_rows=2, max_alert_event_rows=2)
            cache = SQLiteCache(settings=settings)
            quote = _quote()
            rule = cache.create_alert_rule(
                quote,
                AlertRuleInput(symbol="600519", condition_type="price_above", threshold=1200.0),
            )
            for index in range(4):
                cache.log_event("quote", f"cache event {index}")
                cache.update_alert_rule_state(
                    rule,
                    checked_at=f"2026-05-13 10:0{index}:00",
                    state="触发",
                    triggered=True,
                    message=f"测试触发 {index}",
                    quote=quote,
                    force_event=True,
                )

            removed = cache.cleanup_runtime_rows()
            counts = cache.table_counts()

        self.assertEqual(removed["cache_event"], 2)
        self.assertEqual(removed["alert_event"], 2)
        self.assertEqual(counts["cache_event"], 2)
        self.assertEqual(counts["alert_event"], 2)

    def test_alert_event_cleanup_keeps_latest_insertions_for_id_cursor(self) -> None:
        with TemporaryDirectory() as tmpdir:
            path = Path(tmpdir) / "cache.sqlite3"
            settings = Settings(cache_path=path, max_alert_event_rows=2)
            cache = SQLiteCache(settings=settings)
            quote = _quote()
            rule = cache.create_alert_rule(
                quote,
                AlertRuleInput(symbol="600519", condition_type="price_above", threshold=1200.0),
            )
            events = [
                cache.update_alert_rule_state(
                    rule,
                    checked_at=checked_at,
                    state="触发",
                    triggered=True,
                    message=f"测试触发 {index}",
                    quote=quote,
                    force_event=True,
                )
                for index, checked_at in enumerate(
                    (
                        "2026-05-13 10:00:00",
                        "2026-05-13 10:01:00",
                        "2026-05-13 09:00:00",
                    )
                )
            ]
            assert all(event is not None for event in events)
            event_ids = [event.id for event in events if event is not None]

            cache.cleanup_runtime_rows()
            remaining = cache.alert_events(limit=10)
            after_second = cache.alert_events(after_id=event_ids[1], limit=10)

        self.assertEqual([item.id for item in remaining], [event_ids[2], event_ids[1]])
        self.assertEqual([item.id for item in after_second], [event_ids[2]])

    def test_scheduler_cleanup_preserves_user_history(self) -> None:
        with TemporaryDirectory() as tmpdir:
            path = Path(tmpdir) / "cache.sqlite3"
            settings = Settings(
                cache_path=path,
                max_cache_event_rows=1,
                max_alert_event_rows=1,
                max_advice_history_rows=1,
            )
            cache = SQLiteCache(settings=settings)
            quote = _quote()
            rule = cache.create_alert_rule(
                quote,
                AlertRuleInput(symbol="600519", condition_type="price_above", threshold=1200.0),
            )
            for index in range(3):
                cache.log_event("quote", f"regenerable event {index}")
                cache.update_alert_rule_state(
                    rule,
                    checked_at=f"2026-05-13 10:0{index}:00",
                    state="触发",
                    triggered=True,
                    message=f"用户预警 {index}",
                    quote=quote,
                    force_event=True,
                )
            analysis = _analysis_for_advice()
            for index in range(3):
                cache.save_advice_snapshot(analysis.model_copy(update={"support": analysis.support + index * 0.01}))
            before = cache.table_counts()
            scheduler = LocalDataScheduler(SimpleNamespace(settings=settings, cache=cache))

            asyncio.run(scheduler._check_data_health())
            after = cache.table_counts()

        self.assertEqual(before["cache_event"], 3)
        self.assertEqual(before["alert_event"], 3)
        self.assertEqual(before["advice_history"], 3)
        self.assertEqual(after["cache_event"], 1)
        self.assertEqual(after["alert_event"], before["alert_event"])
        self.assertEqual(after["advice_history"], before["advice_history"])

    def test_provider_runtime_updates_preserve_manual_enabled_state(self) -> None:
        with TemporaryDirectory() as tmpdir:
            cache = SQLiteCache(Path(tmpdir) / "cache.sqlite3")
            cache.ensure_provider("akshare", 1, enabled=False)
            cache.update_provider_success("akshare", 1, 12.0)
            cache.update_provider_failure("akshare", 1, "provider down")
            direct_status = next(item for item in cache.provider_statuses() if item.name == "akshare")

            cache.ensure_provider_capability("tencent", "quote", 1, enabled=True)
            cache.ensure_provider("tencent", 1, enabled=False)
            cache.update_provider_capability_success("tencent", "quote", 1, 8.0)
            aggregate_status = next(item for item in cache.provider_statuses() if item.name == "tencent")
            capability_status = next(item for item in cache.provider_capability_statuses() if item.name == "tencent" and item.kind == "quote")

        self.assertFalse(direct_status.enabled)
        self.assertEqual(direct_status.success_count, 1)
        self.assertEqual(direct_status.failure_count, 1)
        self.assertFalse(aggregate_status.enabled)
        self.assertTrue(capability_status.enabled)
        self.assertEqual(capability_status.success_count, 1)

    def test_monitor_events_merge_repeated_messages_after_old_window(self) -> None:
        with TemporaryDirectory() as tmpdir:
            cache = SQLiteCache(Path(tmpdir) / "cache.sqlite3")
            cache.save_monitor_event("info", "quote", "刷新行情完成：10 只")
            with cache._connect() as conn:
                conn.execute(
                    "UPDATE monitor_event SET created_at = ?, last_seen_at = ? WHERE category = ?",
                    ("2026-05-13 09:00:00", "2026-05-13 09:00:00", "quote"),
                )
            cache.save_monitor_event("info", "quote", "刷新行情完成：10 只")
            events = cache.recent_monitor_events(limit=10)

        self.assertEqual(len(events), 1)
        self.assertEqual(events[0].created_at, "2026-05-13 09:00:00")
        self.assertNotEqual(events[0].last_seen_at, "2026-05-13 09:00:00")
        self.assertEqual(events[0].repeat_count, 2)

    def test_monitor_event_cleanup_keeps_recent_last_seen_event(self) -> None:
        with TemporaryDirectory() as tmpdir:
            path = Path(tmpdir) / "cache.sqlite3"
            settings = Settings(cache_path=path, max_monitor_event_rows=1)
            cache = SQLiteCache(settings=settings)
            cache.save_monitor_event("info", "quote", "较早创建但最近重复")
            cache.save_monitor_event("info", "kline", "较晚创建但不活跃")
            with cache._connect() as conn:
                conn.execute(
                    "UPDATE monitor_event SET created_at = ?, last_seen_at = ? WHERE category = ?",
                    ("2026-05-13 09:00:00", "2026-05-13 12:00:00", "quote"),
                )
                conn.execute(
                    "UPDATE monitor_event SET created_at = ?, last_seen_at = ? WHERE category = ?",
                    ("2026-05-13 10:00:00", "2026-05-13 10:00:00", "kline"),
                )
            cache.cleanup_runtime_rows()
            events = cache.recent_monitor_events(limit=10)

        self.assertEqual(len(events), 1)
        self.assertEqual(events[0].category, "quote")

    def test_user_and_runtime_lists_reject_non_positive_limits(self) -> None:
        with TemporaryDirectory() as tmpdir:
            cache = SQLiteCache(Path(tmpdir) / "cache.sqlite3")
            quote = _quote()
            cache.create_stock_note(quote, StockNoteInput(symbol="600519", content="观察"))
            rule = cache.create_alert_rule(
                quote,
                AlertRuleInput(symbol="600519", condition_type="price_above", threshold=1200.0),
            )
            cache.update_alert_rule_state(
                rule,
                checked_at="2026-05-13 10:00:00",
                state="触发",
                triggered=True,
                message="测试触发",
                quote=quote,
                force_event=True,
            )
            run_id = cache.start_task_run("test-task")
            cache.finish_task_run(run_id, "success", "done")
            cache.save_monitor_event("info", "quote", "刷新行情完成：10 只")
            analysis = build_analysis(
                quote,
                [_kline(date=f"2026-05-{index + 1:02d}") for index in range(30)],
                data_quality=build_data_quality(quote, [_kline(date=f"2026-05-{index + 1:02d}") for index in range(30)]),
            )
            cache.save_advice_snapshot(analysis)

            self.assertGreater(len(cache.stock_notes("600519", limit=10)), 0)
            self.assertGreater(len(cache.alert_rules(limit=10)), 0)
            self.assertGreater(len(cache.alert_events(limit=10)), 0)
            self.assertGreater(len(cache.recent_task_runs(limit=10)), 0)
            self.assertGreater(len(cache.recent_monitor_events(limit=10)), 0)
            self.assertGreater(len(cache.advice_history("600519", limit=10)), 0)

            for limit in (0, -1):
                self.assertEqual(cache.stock_notes("600519", limit=limit), [])
                self.assertEqual(cache.alert_rules(limit=limit), [])
                self.assertEqual(cache.alert_events(limit=limit), [])
                self.assertEqual(cache.recent_task_runs(limit=limit), [])
                self.assertEqual(cache.recent_monitor_events(limit=limit), [])
                self.assertEqual(cache.advice_history("600519", limit=limit), [])

    def test_cancelled_task_run_rejects_late_success_or_failure_updates(self) -> None:
        with TemporaryDirectory() as tmpdir:
            cache = SQLiteCache(Path(tmpdir) / "cache.sqlite3")
            run_id = cache.start_task_run("race-test")

            cache.finish_task_run(run_id, "cancelled", "cancelled first")
            cache.finish_task_run(run_id, "success", "late success")
            cache.finish_task_run(run_id, "failed", "late failure")

            run = cache.recent_task_runs(limit=1)[0]

        self.assertEqual(run.status, "cancelled")
        self.assertEqual(run.message, "cancelled first")

    def test_cancellation_can_override_success_persisted_during_cancellation_race(self) -> None:
        with TemporaryDirectory() as tmpdir:
            cache = SQLiteCache(Path(tmpdir) / "cache.sqlite3")
            run_id = cache.start_task_run("race-test")

            cache.finish_task_run(run_id, "success", "handler completed")
            cache.finish_task_run(run_id, "cancelled", "caller cancelled")

            run = cache.recent_task_runs(limit=1)[0]

        self.assertEqual(run.status, "cancelled")
        self.assertEqual(run.message, "caller cancelled")

    def test_advice_history_sanitizes_dirty_legacy_numeric_rows(self) -> None:
        with TemporaryDirectory() as tmpdir:
            cache = SQLiteCache(Path(tmpdir) / "cache.sqlite3")
            created = cache.save_advice_snapshot(_analysis_for_advice())
            with cache._connect() as conn:
                conn.execute(
                    """
                    UPDATE advice_history
                    SET confidence = ?, trend_score = ?, price = ?, change_pct = ?,
                        support = ?, resistance = ?, data_quality_score = ?, repeat_count = ?,
                        name = ?, action = ?, trend_label = ?, risk_level = ?,
                        data_quality_level = ?, reason = ?, summary = ?, updated_at = ?
                    WHERE id = ?
                    """,
                    (
                        "inf",
                        -10,
                        "inf",
                        "nan",
                        "bad",
                        "-inf",
                        "nan",
                        -3,
                        "   ",
                        "   ",
                        "   ",
                        "   ",
                        "   ",
                        "   ",
                        "   ",
                        "   ",
                        created.id,
                    ),
                )

            item = cache.advice_history("600519.SH", limit=1)[0]

        self.assertEqual(item.confidence, 0)
        self.assertEqual(item.trend_score, 0)
        self.assertEqual(item.price, 0)
        self.assertEqual(item.change_pct, 0)
        self.assertEqual(item.support, 0)
        self.assertEqual(item.resistance, 0)
        self.assertEqual(item.data_quality_score, 0)
        self.assertEqual(item.repeat_count, 1)
        self.assertEqual(item.name, "未知股票")
        self.assertEqual(item.action, "控制风险")
        self.assertEqual(item.trend_label, "未知")
        self.assertEqual(item.risk_level, "未知")
        self.assertEqual(item.data_quality_level, "未知")
        self.assertTrue(item.reason)
        self.assertTrue(item.summary)
        self.assertIsNone(item.updated_at)

    def test_dirty_advice_snapshot_numbers_do_not_absorb_new_history(self) -> None:
        with TemporaryDirectory() as tmpdir:
            cache = SQLiteCache(Path(tmpdir) / "cache.sqlite3")
            first = cache.save_advice_snapshot(_analysis_for_advice())
            with cache._connect() as conn:
                conn.execute(
                    "UPDATE advice_history SET trend_score = ?, support = ? WHERE id = ?",
                    (math.inf, "bad", first.id),
                )
            second = cache.save_advice_snapshot(_analysis_for_advice())
            rows = cache.advice_history("600519.SH", limit=5)

        self.assertNotEqual(first.id, second.id)
        self.assertEqual(len(rows), 2)
        self.assertEqual(rows[0].repeat_count, 1)

    def test_advice_history_uses_exact_conclusion_identity_and_keeps_a_b_a(self) -> None:
        with TemporaryDirectory() as tmpdir:
            cache = SQLiteCache(Path(tmpdir) / "cache.sqlite3")
            base = _analysis_for_advice()
            confidence = base.action_advice.confidence + (1 if base.action_advice.confidence < 100 else -1)
            trend_score = base.trend_score + (1 if base.trend_score < 100 else -1)
            quality_score = base.data_quality.score + (1 if base.data_quality.score < 100 else -1)
            variants = (
                base.model_copy(update={"action_advice": base.action_advice.model_copy(update={"action": f"{base.action_advice.action}A"})}),
                base.model_copy(update={"action_advice": base.action_advice.model_copy(update={"confidence": confidence})}),
                base.model_copy(update={"trend_score": trend_score}),
                base.model_copy(update={"trend_label": f"{base.trend_label}A"}),
                base.model_copy(update={"risk_level": f"{base.risk_level}A"}),
                base.model_copy(update={"support": base.support + 0.01}),
                base.model_copy(update={"resistance": base.resistance + 0.01}),
                base.model_copy(update={"data_quality": base.data_quality.model_copy(update={"score": quality_score})}),
                base.model_copy(update={"data_quality": base.data_quality.model_copy(update={"level": f"{base.data_quality.level}A"})}),
                base.model_copy(update={"data_quality": base.data_quality.model_copy(update={"source": "身份变化测试源"})}),
            )

            initial = cache.save_advice_snapshot(base)
            saved_ids = [initial.id]
            for variant in variants:
                changed = cache.save_advice_snapshot(variant)
                restored = cache.save_advice_snapshot(base)
                self.assertNotEqual(changed.id, restored.id)
                saved_ids.extend((changed.id, restored.id))
            rows = cache.advice_history("600519.SH", limit=100)

        self.assertEqual(len(rows), 1 + 2 * len(variants))
        self.assertEqual(len(set(saved_ids)), len(saved_ids))
        self.assertTrue(all(row.repeat_count == 1 for row in rows))

    def test_advice_identity_uses_price_cents_but_not_quote_or_market_time(self) -> None:
        with TemporaryDirectory() as tmpdir:
            cache = SQLiteCache(Path(tmpdir) / "cache.sqlite3")
            base = _analysis_for_advice()
            first = cache.save_advice_snapshot(base)
            same_cent = base.model_copy(
                update={
                    "support": base.support + 0.004,
                    "quote": base.quote.model_copy(
                        update={
                            "price": base.quote.price + 1,
                            "change_pct": base.quote.change_pct + 1,
                            "timestamp": "2026-07-15 14:59:59",
                        }
                    ),
                }
            )
            merged = cache.save_advice_snapshot(same_cent)
            changed_cent = cache.save_advice_snapshot(base.model_copy(update={"support": base.support + 0.01}))

            rows = cache.advice_history("600519.SH", limit=10)

        self.assertEqual(merged.id, first.id)
        self.assertEqual(changed_cent.id, first.id + 1)
        self.assertEqual(len(rows), 2)
        self.assertEqual(rows[1].repeat_count, 2)

    def test_new_advice_snapshot_writes_contract_provenance_and_never_merges_legacy(self) -> None:
        with TemporaryDirectory() as tmpdir:
            cache = SQLiteCache(Path(tmpdir) / "cache.sqlite3")
            analysis = _analysis_for_advice()
            first = cache.save_advice_snapshot(analysis)
            with cache._connect() as conn:
                new_contract = conn.execute(
                    """
                    SELECT snapshot_contract_version, conclusion_basis, rule_version,
                           model_version, market_time, data_quality_source
                    FROM advice_history WHERE id = ?
                    """,
                    (first.id,),
                ).fetchone()
                conn.execute(
                    """
                    UPDATE advice_history
                    SET snapshot_contract_version = 'legacy',
                        conclusion_basis = 'legacy_unknown',
                        rule_version = 'unknown', model_version = 'unknown',
                        market_time = NULL, data_quality_source = NULL
                    WHERE id = ?
                    """,
                    (first.id,),
                )

            second = cache.save_advice_snapshot(analysis)
            timeline = cache.advice_timeline("600519.SH", limit=2)

        self.assertEqual(
            tuple(new_contract),
            (
                "conclusion.v1",
                "analysis_action_advice",
                "rules.v2",
                "none",
                analysis.quote.timestamp,
                analysis.data_quality.source,
            ),
        )
        self.assertNotEqual(first.id, second.id)
        self.assertEqual(len(timeline), 2)
        self.assertEqual(timeline[0].comparison_status, "legacy")
        self.assertEqual(timeline[0].previous_id, first.id)

    def test_watchlist_unread_count_increments_only_for_comparable_conclusion_change(self) -> None:
        with TemporaryDirectory() as tmpdir:
            cache = SQLiteCache(Path(tmpdir) / "cache.sqlite3")
            analysis = _analysis_for_advice()
            cache.save_watchlist_item(analysis.quote)

            cache.save_advice_snapshot(analysis)
            after_insert = cache.watchlist_item("600519.SH")
            cache.save_advice_snapshot(analysis)
            after_merge = cache.watchlist_item("600519.SH")
            changed_confidence = analysis.action_advice.confidence + (1 if analysis.action_advice.confidence < 100 else -1)
            cache.save_advice_snapshot(
                analysis.model_copy(update={"action_advice": analysis.action_advice.model_copy(update={"confidence": changed_confidence})})
            )
            after_change = cache.watchlist_item("600519.SH")

        self.assertEqual(after_insert.unread_change_count, 0)
        self.assertEqual(after_merge.unread_change_count, 0)
        self.assertEqual(after_change.unread_change_count, 1)

    def test_watchlist_unread_count_ignores_identical_insert_and_incomparable_version(self) -> None:
        with TemporaryDirectory() as tmpdir:
            path = Path(tmpdir) / "cache.sqlite3"
            settings = Settings(cache_path=path, advice_history_dedupe_seconds=0)
            cache = SQLiteCache(settings=settings)
            analysis = _analysis_for_advice()
            cache.save_watchlist_item(analysis.quote)

            first = cache.save_advice_snapshot(analysis)
            duplicate = cache.save_advice_snapshot(analysis)
            with cache._connect() as conn:
                conn.execute(
                    "UPDATE advice_history SET rule_version = 'rules.incompatible' WHERE id = ?",
                    (duplicate.id,),
                )
            changed_confidence = analysis.action_advice.confidence + (1 if analysis.action_advice.confidence < 100 else -1)
            version_changed = cache.save_advice_snapshot(
                analysis.model_copy(update={"action_advice": analysis.action_advice.model_copy(update={"confidence": changed_confidence})})
            )
            item = cache.watchlist_item(analysis.quote.code)

        self.assertNotEqual(first.id, duplicate.id)
        self.assertNotEqual(duplicate.id, version_changed.id)
        self.assertIsNotNone(item)
        self.assertEqual(item.unread_change_count, 0)

    def test_mark_viewed_watermark_keeps_committed_later_changes_and_is_idempotent(self) -> None:
        with TemporaryDirectory() as tmpdir:
            path = Path(tmpdir) / "cache.sqlite3"
            settings = Settings(cache_path=path, advice_history_dedupe_seconds=0)
            cache = SQLiteCache(settings=settings)
            analysis = _analysis_for_advice()
            cache.save_watchlist_item(analysis.quote)
            baseline = cache.save_advice_snapshot(analysis)
            step = -1 if analysis.action_advice.confidence >= 99 else 1
            first_change = cache.save_advice_snapshot(
                analysis.model_copy(
                    update={"action_advice": analysis.action_advice.model_copy(update={"confidence": analysis.action_advice.confidence + step})}
                )
            )
            second_change = cache.save_advice_snapshot(
                analysis.model_copy(
                    update={"action_advice": analysis.action_advice.model_copy(update={"confidence": analysis.action_advice.confidence + (2 * step)})}
                )
            )

            marked = cache.mark_watchlist_viewed(
                analysis.quote.code,
                viewed_through_advice_id=first_change.id,
            )
            marked_again = cache.mark_watchlist_viewed(
                analysis.quote.code,
                viewed_through_advice_id=first_change.id,
            )
            fully_marked = cache.mark_watchlist_viewed(
                analysis.quote.code,
                viewed_through_advice_id=second_change.id,
            )

        self.assertLess(baseline.id, first_change.id)
        self.assertLess(first_change.id, second_change.id)
        self.assertIsNotNone(marked)
        self.assertEqual(marked.unread_change_count, 1)
        self.assertIsNotNone(marked_again)
        self.assertEqual(marked_again.unread_change_count, 1)
        self.assertIsNotNone(fully_marked)
        self.assertEqual(fully_marked.unread_change_count, 0)

    def test_mark_viewed_rejects_missing_or_foreign_watermark_without_clearing(self) -> None:
        with TemporaryDirectory() as tmpdir:
            path = Path(tmpdir) / "cache.sqlite3"
            settings = Settings(cache_path=path, advice_history_dedupe_seconds=0)
            cache = SQLiteCache(settings=settings)
            analysis = _analysis_for_advice()
            cache.save_watchlist_item(analysis.quote)
            cache.save_advice_snapshot(analysis)
            changed_confidence = analysis.action_advice.confidence + (1 if analysis.action_advice.confidence < 100 else -1)
            cache.save_advice_snapshot(
                analysis.model_copy(update={"action_advice": analysis.action_advice.model_copy(update={"confidence": changed_confidence})})
            )
            foreign = analysis.model_copy(update={"quote": analysis.quote.model_copy(update={"code": "000001", "market": "SZ", "name": "平安银行"})})
            foreign_advice = cache.save_advice_snapshot(foreign)

            with self.assertRaisesRegex(ValueError, "不存在或不属于"):
                cache.mark_watchlist_viewed(
                    analysis.quote.code,
                    viewed_through_advice_id=foreign_advice.id,
                )
            with self.assertRaisesRegex(ValueError, "不存在或不属于"):
                cache.mark_watchlist_viewed(
                    analysis.quote.code,
                    viewed_through_advice_id=foreign_advice.id + 10_000,
                )
            item = cache.watchlist_item(analysis.quote.code)

        self.assertIsNotNone(item)
        self.assertEqual(item.unread_change_count, 1)
        self.assertIsNone(item.last_viewed_at)

    def test_advice_snapshot_and_unread_increment_roll_back_together(self) -> None:
        with TemporaryDirectory() as tmpdir:
            cache = SQLiteCache(Path(tmpdir) / "cache.sqlite3")
            analysis = _analysis_for_advice()
            cache.save_watchlist_item(analysis.quote)
            baseline = cache.save_advice_snapshot(analysis)
            cache.increment_watchlist_unread_count(analysis.quote.code, 2)
            changed_confidence = analysis.action_advice.confidence + (1 if analysis.action_advice.confidence < 100 else -1)
            changed = analysis.model_copy(update={"action_advice": analysis.action_advice.model_copy(update={"confidence": changed_confidence})})

            from app.repositories.watchlist import increment_watchlist_unread_change_count

            def increment_then_fail(*args, **kwargs):
                increment_watchlist_unread_change_count(*args, **kwargs)
                raise sqlite3.DatabaseError("watchlist readonly")

            with (
                patch(
                    "app.repositories.advice.increment_watchlist_unread_change_count",
                    side_effect=increment_then_fail,
                ),
                self.assertRaisesRegex(sqlite3.DatabaseError, "watchlist readonly"),
            ):
                cache.save_advice_snapshot(changed)
            rows = cache.advice_history("600519.SH", limit=5)
            watchlist_item = cache.watchlist_item("600519.SH")

        self.assertEqual([row.id for row in rows], [baseline.id])
        self.assertIsNotNone(watchlist_item)
        self.assertEqual(watchlist_item.unread_change_count, 2)

    def test_advice_snapshot_insert_succeeds_without_watchlist_row(self) -> None:
        with TemporaryDirectory() as tmpdir:
            cache = SQLiteCache(Path(tmpdir) / "cache.sqlite3")

            saved = cache.save_advice_snapshot(_analysis_for_advice())

            self.assertEqual([row.id for row in cache.advice_history("600519.SH", limit=5)], [saved.id])
            self.assertIsNone(cache.watchlist_item("600519.SH"))

    def test_advice_snapshot_increment_normalizes_dirty_unread_count(self) -> None:
        with TemporaryDirectory() as tmpdir:
            cache = SQLiteCache(Path(tmpdir) / "cache.sqlite3")
            analysis = _analysis_for_advice()
            cache.save_watchlist_item(analysis.quote)
            cache.save_advice_snapshot(analysis)
            with cache._connect() as conn:
                conn.execute(
                    "UPDATE watchlist SET unread_change_count = ? WHERE symbol = ?",
                    ("dirty", "600519.SH"),
                )

            changed_confidence = analysis.action_advice.confidence + (1 if analysis.action_advice.confidence < 100 else -1)
            cache.save_advice_snapshot(
                analysis.model_copy(update={"action_advice": analysis.action_advice.model_copy(update={"confidence": changed_confidence})})
            )

            watchlist_item = cache.watchlist_item("600519.SH")
            self.assertIsNotNone(watchlist_item)
            self.assertEqual(watchlist_item.unread_change_count, 1)

    def test_mark_viewed_watermark_does_not_clear_concurrent_later_change(self) -> None:
        with TemporaryDirectory() as tmpdir:
            path = Path(tmpdir) / "cache.sqlite3"
            settings = Settings(cache_path=path, advice_history_dedupe_seconds=0)
            saving_cache = SQLiteCache(settings=settings)
            viewing_cache = SQLiteCache(settings=settings)
            analysis = _analysis_for_advice()
            saving_cache.save_watchlist_item(analysis.quote)
            saving_cache.save_advice_snapshot(analysis)
            step = -1 if analysis.action_advice.confidence >= 99 else 1
            first_change = analysis.model_copy(
                update={"action_advice": analysis.action_advice.model_copy(update={"confidence": analysis.action_advice.confidence + step})}
            )
            displayed = saving_cache.save_advice_snapshot(first_change)
            second_change = analysis.model_copy(
                update={"action_advice": analysis.action_advice.model_copy(update={"confidence": analysis.action_advice.confidence + (2 * step)})}
            )
            watermark_entered = Event()
            allow_mark = Event()

            from app.repositories.watchlist import _unread_change_count_after_watermark

            def paused_watermark_count(*args, **kwargs):
                remaining = _unread_change_count_after_watermark(*args, **kwargs)
                watermark_entered.set()
                if not allow_mark.wait(timeout=5):
                    raise TimeoutError("test did not release mark-viewed transaction")
                return remaining

            def mark_viewed():
                return viewing_cache.mark_watchlist_viewed(
                    "600519.SH",
                    viewed_through_advice_id=displayed.id,
                )

            with patch(
                "app.repositories.watchlist._unread_change_count_after_watermark",
                side_effect=paused_watermark_count,
            ):
                with ThreadPoolExecutor(max_workers=2) as executor:
                    mark_future = executor.submit(mark_viewed)
                    self.assertTrue(watermark_entered.wait(timeout=2))
                    save_future = executor.submit(saving_cache.save_advice_snapshot, second_change)
                    try:
                        with self.assertRaises(FutureTimeoutError):
                            save_future.result(timeout=0.1)
                    finally:
                        allow_mark.set()
                    marked = mark_future.result(timeout=2)
                    saved = save_future.result(timeout=2)

            final_item = saving_cache.watchlist_item("600519.SH")

        self.assertEqual(saved.symbol, "600519.SH")
        self.assertIsNotNone(marked)
        self.assertIsNotNone(final_item)
        self.assertEqual(final_item.unread_change_count, 1)

    def test_advice_timeline_reads_one_extra_baseline_row(self) -> None:
        with TemporaryDirectory() as tmpdir:
            cache = SQLiteCache(Path(tmpdir) / "cache.sqlite3")
            with patch.object(cache.advice_repo, "timeline_items", return_value=[]) as load:
                self.assertEqual(cache.advice_timeline("600519.SH", limit=8), [])

        load.assert_called_once_with("600519.SH", limit=9)

    def test_advice_history_respects_injected_dedupe_settings(self) -> None:
        with TemporaryDirectory() as tmpdir:
            settings = Settings(
                cache_path=Path(tmpdir) / "cache.sqlite3",
                advice_history_dedupe_seconds=0,
            )
            cache = SQLiteCache(settings=settings)

            first = cache.save_advice_snapshot(_analysis_for_advice())
            second = cache.save_advice_snapshot(_analysis_for_advice())
            rows = cache.advice_history("600519.SH", limit=5)

        self.assertNotEqual(first.id, second.id)
        self.assertEqual(len(rows), 2)
        self.assertTrue(all(row.repeat_count == 1 for row in rows))

    def test_advice_history_deduplicates_atomically_across_cache_instances(self) -> None:
        with TemporaryDirectory() as tmpdir:
            path = Path(tmpdir) / "cache.sqlite3"
            settings = Settings(cache_path=path, advice_history_dedupe_seconds=600)
            first_cache = SQLiteCache(settings=settings)
            second_cache = SQLiteCache(settings=settings)
            analysis = _analysis_for_advice()
            first_cache.save_watchlist_item(analysis.quote)
            saved_ids: list[int] = []

            def save_after_barrier(cache: SQLiteCache, barrier: Barrier) -> int:
                barrier.wait()
                return cache.save_advice_snapshot(analysis).id

            with ThreadPoolExecutor(max_workers=2) as executor:
                for _ in range(5):
                    barrier = Barrier(2)
                    futures = (
                        executor.submit(save_after_barrier, first_cache, barrier),
                        executor.submit(save_after_barrier, second_cache, barrier),
                    )
                    saved_ids.extend(future.result() for future in futures)

            rows = first_cache.advice_history("600519.SH", limit=5)
            watchlist_item = first_cache.watchlist_item("600519.SH")

        self.assertEqual(len(set(saved_ids)), 1)
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0].repeat_count, 10)
        self.assertIsNotNone(watchlist_item)
        self.assertEqual(watchlist_item.unread_change_count, 0)


class ThemeContextTests(unittest.TestCase):
    def test_compat_schema_skips_missing_tables_and_creates_migration_table(self) -> None:
        conn = sqlite3.connect(":memory:")
        conn.row_factory = sqlite3.Row
        apply_compat_schema(conn)
        tables = {row["name"] for row in conn.execute("SELECT name FROM sqlite_master WHERE type = 'table'").fetchall()}
        conn.close()

        self.assertEqual(tables, {"schema_migration"})

    def test_ensure_column_ignores_missing_table(self) -> None:
        conn = sqlite3.connect(":memory:")
        ensure_column(conn, "missing_table", "new_column", "TEXT")

        self.assertFalse(table_exists(conn, "missing_table"))
        conn.close()

    def test_run_once_creates_migration_table_and_is_idempotent(self) -> None:
        conn = sqlite3.connect(":memory:")
        conn.row_factory = sqlite3.Row
        conn.execute("CREATE TABLE counter (value INTEGER NOT NULL)")
        conn.execute("INSERT INTO counter (value) VALUES (0)")

        run_once(conn, "increment_counter", "UPDATE counter SET value = value + 1")
        run_once(conn, "increment_counter", "UPDATE counter SET value = value + 1")

        value = conn.execute("SELECT value FROM counter").fetchone()["value"]
        migration = conn.execute("SELECT name FROM schema_migration WHERE name = 'increment_counter'").fetchone()
        conn.close()

        self.assertEqual(value, 1)
        self.assertIsNotNone(migration)

    def test_stock_concept_schema_backfills_match_reason_for_old_cache(self) -> None:
        from app.db.schema import initialize_schema

        with TemporaryDirectory() as tmpdir:
            path = Path(tmpdir) / "legacy.sqlite3"

            conn = sqlite3.connect(path)
            conn.row_factory = sqlite3.Row
            conn.execute(
                """
                CREATE TABLE stock_concept (
                    symbol TEXT NOT NULL,
                    rank INTEGER NOT NULL,
                    name TEXT NOT NULL,
                    change_pct REAL NOT NULL DEFAULT 0,
                    amount REAL,
                    turnover_rate REAL,
                    leading_stock TEXT,
                    leading_stock_change_pct REAL,
                    source TEXT NOT NULL,
                    updated_at TEXT NOT NULL,
                    PRIMARY KEY (symbol, name)
                )
                """
            )
            initialize_schema(conn)
            columns = {row["name"] for row in conn.execute("PRAGMA table_info(stock_concept)").fetchall()}
            conn.close()

        self.assertIn("match_reason", columns)

    def test_stock_concept_cache_roundtrip_and_cleanup(self) -> None:
        with TemporaryDirectory() as tmpdir:
            path = Path(tmpdir) / "cache.sqlite3"
            settings = Settings(cache_path=path, max_stock_concept_rows=2)
            cache = SQLiteCache(settings=settings)
            cache.save_stock_concepts(
                "600519.SH",
                [
                    StockConceptItem(
                        symbol="600519.SH",
                        rank=1,
                        name="白酒概念",
                        change_pct=1.8,
                        amount=2_000_000_000,
                        turnover_rate=2.1,
                        leading_stock="贵州茅台",
                        leading_stock_change_pct=2.3,
                        match_reason="测试概念成分匹配",
                        source="测试概念源",
                        updated_at=now_text(),
                    ),
                    StockConceptItem(
                        symbol="600519.SH",
                        rank=2,
                        name="消费概念",
                        change_pct=0.8,
                        match_reason="测试概念成分匹配",
                        source="测试概念源",
                        updated_at=now_text(),
                    ),
                    StockConceptItem(
                        symbol="600519.SH",
                        rank=3,
                        name="高股息概念",
                        change_pct=0.3,
                        match_reason="测试概念成分匹配",
                        source="测试概念源",
                        updated_at=now_text(),
                    ),
                ],
            )
            cache.save_stock_concepts(
                "000001.SZ",
                [
                    StockConceptItem(
                        symbol="000001.SZ",
                        rank=1,
                        name="银行概念",
                        change_pct=0.5,
                        match_reason="测试概念成分匹配",
                        source="测试概念源",
                        updated_at=now_text(),
                    )
                ],
            )
            rows = cache.get_stock_concepts("600519", max_age_seconds=60 * 60 * 24, limit=5)
            removed = cache.cleanup_runtime_rows()
            with cache._connect() as conn:
                counts_by_symbol = {
                    row["symbol"]: row["count"]
                    for row in conn.execute(
                        """
                        SELECT symbol, COUNT(*) AS count
                        FROM stock_concept
                        GROUP BY symbol
                        """
                    ).fetchall()
                }

        self.assertEqual(len(rows), 3)
        self.assertEqual(rows[0].name, "白酒概念")
        self.assertIn("stock_concept", removed)
        self.assertEqual(counts_by_symbol["600519.SH"], 2)
        self.assertEqual(counts_by_symbol["000001.SZ"], 1)

    def test_theme_context_explains_relative_strength(self) -> None:
        analysis = build_analysis(
            _quote(change_pct=3.2),
            [_kline(date=f"2026-05-{index + 1:02d}", close=100 + index, high=101 + index, low=99 + index, volume=2000) for index in range(40)],
            stock_profile=_stock_info(),
            industry_context=_plate_item(change_pct=1.1),
            data_quality=build_data_quality(_quote(change_pct=3.2), [_kline() for _ in range(40)]),
        )
        insights = build_stock_insight_bundle(analysis)
        feature = build_feature_snapshot(analysis, insights)
        report = build_theme_context_report(
            analysis,
            feature,
            [
                StockConceptItem(
                    symbol="600519.SH",
                    rank=1,
                    name="白酒概念",
                    change_pct=1.6,
                    leading_stock="贵州茅台",
                    source="测试概念源",
                    updated_at="2026-05-15 10:00:00",
                )
            ],
        )

        self.assertIn(report.level, {"主题顺风", "主题配合"})
        self.assertIn("个股", report.style)
        self.assertIn(report.relative_strength, {"显著强于背景", "强于背景", "与背景同步"})
        self.assertTrue(any("相对" in item for item in report.evidence))

    def test_theme_context_handles_missing_concepts_conservatively(self) -> None:
        analysis = build_analysis(
            _quote(change_pct=-0.8),
            [_kline(date=f"2026-05-{index + 1:02d}", close=100 - index * 0.2, high=101, low=98, volume=1000) for index in range(40)],
            stock_profile=_stock_info(),
            industry_context=None,
            data_quality=build_data_quality(_quote(change_pct=-0.8), [_kline() for _ in range(40)]),
        )
        insights = build_stock_insight_bundle(analysis)
        feature = build_feature_snapshot(analysis, insights)
        report = build_theme_context_report(analysis, feature, [])

        self.assertEqual(report.level, "主题待确认")
        self.assertEqual(report.relative_strength, "强弱待确认")
        self.assertIn("概念归属成分", report.missing_data)
        self.assertTrue(any("保守" in item or "暂未" in item for item in report.risks + report.opportunities))

    def test_event_digest_exposes_external_data_checklist(self) -> None:
        analysis = build_analysis(
            _quote(change_pct=7.2, turnover_rate=13.5),
            [
                _kline(date=f"2026-05-{index + 1:02d}", close=100 + index * 0.4, high=102 + index * 0.4, low=99 + index * 0.4, volume=1000 + index * 120)
                for index in range(40)
            ],
            data_quality=build_data_quality(_quote(change_pct=7.2, turnover_rate=13.5), [_kline() for _ in range(40)]),
        )
        insights = build_stock_insight_bundle(analysis)

        self.assertTrue(insights.lhb.action_items)
        self.assertIn("龙虎榜席位", insights.events.missing_sources)
        self.assertTrue(any(item.category in {"龙虎榜", "公告", "融资融券"} for item in insights.events.events))
        self.assertTrue(any(item.action_hint for item in insights.events.events))


class WorkbenchCacheTests(unittest.TestCase):
    def test_context_cache_trims_oldest_entries(self) -> None:
        cache = WorkbenchContextCache(max_size=32)
        for index in range(cache.max_size + 4):
            cache.entries[f"{index:06d}.SH"] = (float(index), None)  # type: ignore[assignment]

        cache.trim()

        self.assertEqual(len(cache.entries), cache.max_size)
        self.assertNotIn("000000.SH", cache.entries)
        self.assertIn(f"{cache.max_size + 3:06d}.SH", cache.entries)

    def test_stock_workbench_does_not_repeat_advice_snapshot_for_cached_context(self) -> None:
        async def run_check(path: Path):
            hub = DataHub(cache=SQLiteCache(path))
            quote = _quote(pe=26.8, pb=2.95, market_cap=1_000_000_000)
            klines = [_kline(date=f"2026-05-{index + 1:02d}", close=100 + index, high=101 + index, low=99 + index, volume=2000) for index in range(40)]
            quality = build_data_quality(quote, klines)
            pool = [_stock_info(code="600519", market="SH")] + [_stock_info(code=f"600{index:03d}", market="SH") for index in range(20)]

            async def quotes_for(symbols, use_cache: bool = True):
                rows = []
                for idx, symbol in enumerate(symbols):
                    code, market = symbol.split(".")
                    rows.append(
                        quote.model_copy(
                            update={
                                "code": code,
                                "market": market,
                                "name": f"测试{code}",
                                "price": quote.price + idx,
                                "source": "测试行情",
                            }
                        )
                    )
                return rows

            hub.workbench_contexts = WorkbenchContextCache()
            with (
                patch.object(hub, "quote", return_value=quote),
                patch.object(
                    hub,
                    "kline",
                    return_value=klines,
                ),
                patch.object(
                    hub,
                    "plate_rank",
                    return_value=[_plate_item()],
                ),
                patch.object(
                    hub,
                    "assess_quote_quality",
                    return_value=quality,
                ),
                patch.object(
                    hub,
                    "stock_profile",
                    return_value=_stock_info(code="600519", market="SH"),
                ),
                patch.object(
                    hub,
                    "stock_pool",
                    return_value=pool,
                ),
                patch.object(
                    hub,
                    "quotes",
                    side_effect=quotes_for,
                ),
                patch.object(
                    hub,
                    "stock_concepts",
                    return_value=[],
                ),
            ):
                await individual.stock_workbench(hub, "600519")
                await individual.stock_workbench(hub, "600519")
            return hub.cache.advice_history("600519.SH", limit=5)

        with TemporaryDirectory() as tmpdir:
            history = asyncio.run(run_check(Path(tmpdir) / "cache.sqlite3"))

        self.assertEqual(len(history), 1)
        self.assertEqual(history[0].repeat_count, 1)

    def test_strong_stock_symbol_sampling_deduplicates_normalized_symbols(self) -> None:
        rows = unique_standard_symbols(["000333", "000333.SZ", "SZ000333", "600036", "600036.SH"])
        self.assertEqual(rows, ["000333.SZ", "600036.SH"])

    def test_analyze_individual_stock_loads_peer_quotes(self) -> None:
        async def run_check(path: Path):
            hub = DataHub(cache=SQLiteCache(path))
            hub.cache.save_stock_pool([_stock_info(code="600519", market="SH")] + [_stock_info(code=f"600{index:03d}", market="SH") for index in range(20)])
            hub.cache.save_plate_rank([])
            peer_pool = [_stock_info(code="600519", market="SH")] + [_stock_info(code=f"600{index:03d}", market="SH") for index in range(20)]
            with (
                patch.object(hub, "quote", return_value=_quote(pe=26.8, pb=2.95, market_cap=1_000_000_000)),
                patch.object(
                    hub,
                    "stock_profile",
                    return_value=_stock_info(code="600519", market="SH"),
                ),
                patch.object(
                    hub,
                    "stock_pool",
                    return_value=peer_pool,
                ),
                patch.object(
                    hub,
                    "kline",
                    return_value=[
                        _kline(date=f"2026-05-{index + 1:02d}", close=100 + index, high=101 + index, low=99 + index, volume=2000) for index in range(40)
                    ],
                ),
                patch.object(hub, "assess_quote_quality", return_value=build_data_quality(_quote(), [_kline() for _ in range(40)])),
                patch.object(
                    hub,
                    "quotes",
                    side_effect=lambda symbols, use_cache=True: [
                        Quote(
                            code=item.split(".")[0],
                            name=f"同行{idx}",
                            market=item.split(".")[1],
                            price=10 + idx,
                            prev_close=9.8 + idx,
                            open=9.9 + idx,
                            high=10.2 + idx,
                            low=9.7 + idx,
                            volume=100000,
                            amount=1_000_000,
                            change=0.2,
                            change_pct=2.0,
                            turnover_rate=1.5,
                            pe=18 + idx,
                            pb=2.0 + idx * 0.08,
                            market_cap=1_000_000_000,
                            timestamp="2026-05-13 10:00:00",
                            source="测试行情",
                        )
                        for idx, item in enumerate(symbols)
                    ],
                ),
            ):
                return await individual.analyze_individual_stock(hub, "600519", persist_history=False)

        with TemporaryDirectory() as tmpdir:
            analysis = __import__("asyncio").run(run_check(Path(tmpdir) / "cache.sqlite3"))

        self.assertGreaterEqual(len(analysis.peer_quotes), 5)
        self.assertEqual(analysis.peer_sample.status, "available")
        self.assertEqual(analysis.peer_sample.requested_count, len(analysis.peer_quotes))
        self.assertIsNone(analysis.peer_sample.warning)

    def test_analyze_individual_stock_degrades_when_plate_rank_fails(self) -> None:
        async def run_check(path: Path):
            hub = DataHub(cache=SQLiteCache(path))
            events: list[tuple[str, str]] = []
            with (
                patch.object(hub, "quote", return_value=_quote()),
                patch.object(
                    hub,
                    "stock_profile",
                    return_value=_stock_info(code="600519", market="SH"),
                ),
                patch.object(
                    hub,
                    "stock_pool",
                    return_value=[],
                ),
                patch.object(
                    hub,
                    "kline",
                    return_value=[_kline(date=f"2026-05-{index + 1:02d}", close=100 + index) for index in range(40)],
                ),
                patch.object(
                    hub,
                    "plate_rank",
                    side_effect=RuntimeError("板块源不可用"),
                ),
                patch.object(
                    hub,
                    "assess_quote_quality",
                    return_value=build_data_quality(_quote(), [_kline() for _ in range(40)]),
                ),
                patch.object(
                    hub.cache,
                    "log_event",
                    side_effect=lambda category, message: events.append((category, message)),
                ),
            ):
                analysis = await individual.analyze_individual_stock(hub, "600519", persist_history=False)
            return analysis, events

        with TemporaryDirectory() as tmpdir:
            analysis, events = __import__("asyncio").run(run_check(Path(tmpdir) / "cache.sqlite3"))

        self.assertIsNone(analysis.industry_context)
        self.assertIn(("fallback", "个股行业背景暂不可用：600519.SH；板块源不可用"), events)

    def test_analyze_individual_stock_degrades_when_quality_check_is_slow(self) -> None:
        async def slow_quality(*args, **kwargs):
            await asyncio.sleep(1)
            return build_data_quality(_quote(), [_kline() for _ in range(40)])

        async def run_check(path: Path):
            hub = DataHub(cache=SQLiteCache(path))
            hub.settings.workbench_optional_timeout_seconds = 0.01
            events: list[tuple[str, str]] = []
            with (
                patch.object(hub, "quote", return_value=_quote()),
                patch.object(
                    hub,
                    "stock_profile",
                    return_value=_stock_info(code="600519", market="SH"),
                ),
                patch.object(
                    hub,
                    "stock_pool",
                    return_value=[],
                ),
                patch.object(
                    hub,
                    "kline",
                    return_value=[_kline(date=f"2026-05-{index + 1:02d}", close=100 + index) for index in range(40)],
                ),
                patch.object(
                    hub,
                    "plate_rank",
                    return_value=[],
                ),
                patch.object(
                    hub,
                    "assess_quote_quality",
                    side_effect=slow_quality,
                ),
                patch.object(
                    hub.cache,
                    "log_event",
                    side_effect=lambda category, message: events.append((category, message)),
                ),
            ):
                analysis = await individual.analyze_individual_stock(hub, "600519", persist_history=False)
            return analysis, events

        with TemporaryDirectory() as tmpdir:
            analysis, events = asyncio.run(run_check(Path(tmpdir) / "cache.sqlite3"))

        self.assertEqual(analysis.data_quality.consistency_level, "未校验")
        self.assertTrue(any("数据质量校验暂不可用：600519.SH；TimeoutError" in note for note in analysis.data_quality.notes))
        self.assertIn(("fallback", "数据质量校验暂不可用：600519.SH；TimeoutError"), events)

    def test_analyze_individual_stock_degrades_when_quote_history_read_fails(self) -> None:
        async def run_check(path: Path):
            hub = DataHub(cache=SQLiteCache(path))
            events: list[tuple[str, str]] = []
            with (
                patch.object(hub, "quote", return_value=_quote()),
                patch.object(
                    hub,
                    "stock_profile",
                    return_value=_stock_info(code="600519", market="SH"),
                ),
                patch.object(
                    hub,
                    "stock_pool",
                    return_value=[],
                ),
                patch.object(
                    hub,
                    "kline",
                    return_value=[_kline(date=f"2026-05-{index + 1:02d}", close=100 + index) for index in range(40)],
                ),
                patch.object(
                    hub,
                    "plate_rank",
                    return_value=[],
                ),
                patch.object(
                    hub,
                    "assess_quote_quality",
                    return_value=build_data_quality(_quote(), [_kline() for _ in range(40)]),
                ),
                patch.object(
                    hub.cache,
                    "quote_history",
                    side_effect=sqlite3.DatabaseError("quote history readonly"),
                ),
                patch.object(
                    hub.cache,
                    "log_event",
                    side_effect=lambda category, message: events.append((category, message)),
                ),
            ):
                analysis = await individual.analyze_individual_stock(hub, "600519", persist_history=False)
            return analysis, events

        with TemporaryDirectory() as tmpdir:
            analysis, events = __import__("asyncio").run(run_check(Path(tmpdir) / "cache.sqlite3"))

        self.assertEqual(analysis.quote_history, [])
        self.assertIn(("fallback", "个股历史报价暂不可用：600519.SH；quote history readonly"), events)

    def test_analyze_individual_stock_degrades_when_advice_snapshot_write_fails(self) -> None:
        async def run_check(path: Path):
            hub = DataHub(cache=SQLiteCache(path))
            events: list[tuple[str, str]] = []
            with (
                patch.object(hub, "quote", return_value=_quote()),
                patch.object(
                    hub,
                    "stock_profile",
                    return_value=_stock_info(code="600519", market="SH"),
                ),
                patch.object(
                    hub,
                    "stock_pool",
                    return_value=[],
                ),
                patch.object(
                    hub,
                    "kline",
                    return_value=[_kline(date=f"2026-05-{index + 1:02d}", close=100 + index) for index in range(40)],
                ),
                patch.object(
                    hub,
                    "plate_rank",
                    return_value=[],
                ),
                patch.object(
                    hub,
                    "assess_quote_quality",
                    return_value=build_data_quality(_quote(), [_kline() for _ in range(40)]),
                ),
                patch.object(
                    hub.cache,
                    "save_advice_snapshot",
                    side_effect=sqlite3.DatabaseError("advice readonly"),
                ),
                patch.object(
                    hub.cache,
                    "log_event",
                    side_effect=lambda category, message: events.append((category, message)),
                ),
            ):
                analysis = await individual.analyze_individual_stock(hub, "600519", persist_history=True)
            return analysis, events

        with TemporaryDirectory() as tmpdir:
            analysis, events = __import__("asyncio").run(run_check(Path(tmpdir) / "cache.sqlite3"))

        self.assertEqual(analysis.quote.code, "600519")
        self.assertIn(("fallback", "分析建议快照暂不可写：600519.SH；advice readonly"), events)


class _FailingRuntimeEventRepo:
    def log_event(self, category: str, message: str) -> None:
        raise sqlite3.DatabaseError("event log readonly")


class ReplayConfidenceTests(unittest.TestCase):
    def test_replay_pattern_note_warns_when_sample_is_small(self) -> None:
        note = research._replay_pattern_note("放量突破", 3, 80.0, 3.2)
        self.assertIn("样本只有 3 次", note)
        self.assertIn("不宜提高权重", note)
