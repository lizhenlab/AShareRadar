from __future__ import annotations

import asyncio
import math
import sqlite3
import threading
import unittest
from pathlib import Path
from tempfile import TemporaryDirectory
from types import SimpleNamespace
from unittest.mock import patch

import pytest

from app.models.schemas import AlertRuleInput, AlertRuleItem, AlertRuleUpdate, DataQuality, FactorCalibration, StandardFactor, StockNoteItem
from app.services.cache import SQLiteCache
from app.services.alerts import (
    AlertRuleEvaluator,
    _evaluate_rule,
    _should_emit_event,
    decide_alert_transition,
    evaluate_alert_rules,
    validate_alert_condition,
)
from app.services.chart_marks import _note_marks
from app.services.research_factors import _factor_calibration_impact, _factor_score_impact, _factor_specs
from app.services.stock_insights import RULE_VERSION, rule_definitions
from tests.factories import make_quote as _quote


def test_alert_evaluator_accepts_explicit_analysis_loader() -> None:
    hub = SimpleNamespace()
    analysis = SimpleNamespace(quote=_quote())
    rule = _alert_rule(
        condition_type="trend_score_above",
        threshold=70,
        last_state="未触发",
        last_triggered_at=None,
        cooldown_seconds=300,
    )
    calls: list[tuple[object, str]] = []

    async def load_analysis(datahub, symbol: str):
        calls.append((datahub, symbol))
        return analysis

    async def load_twice():
        evaluator = AlertRuleEvaluator(hub, "2026-05-13 10:00:00", load_analysis)
        return await evaluator._analysis_for_rule(rule), await evaluator._analysis_for_rule(rule)

    first, second = asyncio.run(load_twice())

    assert first is analysis
    assert second is analysis
    assert calls == [(hub, rule.symbol)]


def test_alert_evaluator_default_loader_is_lazy_and_registration_free() -> None:
    hub = SimpleNamespace()
    analysis = SimpleNamespace(quote=_quote())
    rule = _alert_rule(
        condition_type="trend_score_above",
        threshold=70,
        last_state="未触发",
        last_triggered_at=None,
        cooldown_seconds=300,
    )
    calls: list[tuple[object, str, bool]] = []

    async def load_analysis(datahub, symbol: str, *, persist_history: bool):
        calls.append((datahub, symbol, persist_history))
        return analysis

    with patch("app.workflows.stock_analysis.analyze_individual_stock", new=load_analysis):
        loaded = asyncio.run(AlertRuleEvaluator(hub, "2026-05-13 10:00:00")._analysis_for_rule(rule))

    assert loaded is analysis
    assert calls == [(hub, rule.symbol, False)]


def test_live_quote_and_quality_caches_are_isolated_from_analysis_in_both_rule_orders() -> None:
    live_quote = _quote(price=110.0)
    analysis_quote = _quote(price=90.0)
    live_quality = _quality(score=40, anomaly="live quote low quality")
    analysis_quality = _quality(score=90)
    analysis = SimpleNamespace(
        quote=analysis_quote,
        data_quality=analysis_quality,
        trend_score=80,
        support=85.0,
        resistance=95.0,
    )
    price_rule = _alert_rule(
        rule_id=1,
        condition_type="price_above",
        threshold=100,
        last_state="未触发",
        last_triggered_at=None,
        cooldown_seconds=300,
    )
    analysis_rule = _alert_rule(
        rule_id=2,
        condition_type="trend_score_above",
        threshold=70,
        last_state="未触发",
        last_triggered_at=None,
        cooldown_seconds=300,
    )

    async def evaluate_in_order(rules: list[AlertRuleItem]):
        cache = _AlertCacheStub()
        hub = _AlertDataHubStub(cache, live_quote, live_quality)

        async def load_analysis(_datahub, _symbol: str):
            return analysis

        evaluator = AlertRuleEvaluator(hub, "2026-05-13 10:00:00", load_analysis)
        items = [await evaluator.evaluate(rule) for rule in rules]
        return evaluator, hub, cache, {item.rule.condition_type: item for item in items}

    analysis_first = asyncio.run(evaluate_in_order([analysis_rule, price_rule]))
    live_first = asyncio.run(evaluate_in_order([price_rule, analysis_rule]))

    for evaluator, hub, cache, items in (analysis_first, live_first):
        assert items["price_above"].triggered is True
        assert items["price_above"].current_value == 110.0
        assert "40 分" in items["price_above"].message
        assert items["trend_score_above"].triggered is True
        assert items["trend_score_above"].current_value == 80.0
        assert "低置信提醒" not in items["trend_score_above"].message
        assert cache.persisted_quotes["price_above"].price == 110.0
        assert cache.persisted_quotes["trend_score_above"].price == 90.0
        assert evaluator.live_quote_cache == {price_rule.symbol: live_quote}
        assert evaluator.live_quality_cache == {price_rule.symbol: live_quality}
        assert hub.quote_calls == [price_rule.symbol]
        assert hub.quality_calls == [live_quote]


def test_alert_cache_reads_and_state_write_run_off_event_loop_thread() -> None:
    rule = _alert_rule(
        condition_type="price_above",
        threshold=100,
        last_state="未触发",
        last_triggered_at=None,
        cooldown_seconds=300,
    )

    class ThreadTrackingCache:
        def __init__(self) -> None:
            self.io_threads: list[int] = []

        def alert_rules(self, **_kwargs):
            self.io_threads.append(threading.get_ident())
            return [rule]

        def update_alert_rule_state(self, *_args, **_kwargs):
            self.io_threads.append(threading.get_ident())
            return None

        def alert_rule(self, _rule_id: int):
            self.io_threads.append(threading.get_ident())
            return rule

    async def run_check():
        cache = ThreadTrackingCache()
        hub = _AlertDataHubStub(cache, _quote(price=110), _quality(score=90))
        event_loop_thread = threading.get_ident()
        summary = await evaluate_alert_rules(hub)
        return cache, summary, event_loop_thread

    cache, summary, event_loop_thread = asyncio.run(run_check())

    assert summary.checked_count == 1
    assert summary.failed_count == 0
    assert len(cache.io_threads) == 3
    assert all(thread_id != event_loop_thread for thread_id in cache.io_threads)


def test_alert_evaluation_loads_every_enabled_rule_without_default_limit() -> None:
    rules = [
        _alert_rule(
            rule_id=rule_id,
            condition_type="price_above",
            threshold=100,
            last_state="未触发",
            last_triggered_at=None,
            cooldown_seconds=300,
        )
        for rule_id in range(1, 202)
    ]

    class CompleteRuleCache:
        def __init__(self) -> None:
            self.rule_query: dict[str, object] | None = None

        def alert_rules(self, **kwargs):
            self.rule_query = kwargs
            return rules

        def update_alert_rule_state(self, *_args, **_kwargs):
            return None

        def alert_rule(self, rule_id: int):
            return rules[rule_id - 1]

    cache = CompleteRuleCache()
    hub = _AlertDataHubStub(cache, _quote(price=110), _quality(score=90))

    summary = asyncio.run(evaluate_alert_rules(hub))

    assert cache.rule_query == {"symbol": None, "include_disabled": False}
    assert summary.checked_count == 201
    assert len(summary.items) == 201
    assert summary.failed_count == 0


def test_alert_evaluation_marks_rule_failed_when_compare_and_swap_is_rejected() -> None:
    with TemporaryDirectory() as tmpdir:
        cache = SQLiteCache(Path(tmpdir) / "cache.sqlite3")
        quote = _quote(price=110)
        rule = cache.create_alert_rule(
            quote,
            AlertRuleInput(symbol="600519", condition_type="price_above", threshold=100),
        )

        class ConcurrentPatchHub:
            def __init__(self) -> None:
                self.cache = cache

            async def quote(self, _symbol: str):
                updated = cache.update_alert_rule(rule.id, AlertRuleUpdate(threshold=120))
                assert updated is not None
                return quote

            async def assess_quote_quality(self, _quote_value, **_kwargs):
                return _quality(score=90)

        summary = asyncio.run(evaluate_alert_rules(ConcurrentPatchHub()))
        current = cache.alert_rule(rule.id)
        events = cache.alert_events(symbol=rule.symbol)

    assert current is not None
    assert current.threshold == 120
    assert summary.checked_count == 1
    assert summary.triggered_count == 0
    assert summary.new_event_count == 0
    assert summary.failed_count == 1
    assert summary.items[0].status == "failed"
    assert summary.items[0].rule.threshold == 100
    assert "评估期间已修改或删除" in summary.items[0].message
    assert events == []


def test_alert_state_write_failure_keeps_per_rule_degradation() -> None:
    rule = _alert_rule(
        condition_type="price_above",
        threshold=100,
        last_state="未触发",
        last_triggered_at=None,
        cooldown_seconds=300,
    )

    class FailingStateCache:
        def alert_rules(self, **_kwargs):
            return [rule]

        def update_alert_rule_state(self, *_args, **_kwargs):
            raise RuntimeError("persist failed")

        def alert_rule(self, _rule_id: int):
            raise AssertionError("failed persistence must skip readback")

    hub = _AlertDataHubStub(FailingStateCache(), _quote(price=110), _quality(score=90))

    summary = asyncio.run(evaluate_alert_rules(hub))

    assert summary.checked_count == 1
    assert summary.failed_count == 1
    assert summary.items[0].status == "failed"
    assert "persist failed" in summary.items[0].message


def test_alert_batch_continues_after_sqlite_os_and_unknown_rule_failures() -> None:
    rules = [
        _alert_rule(
            rule_id=rule_id,
            condition_type="price_above",
            threshold=100,
            last_state="未触发",
            last_triggered_at=None,
            cooldown_seconds=300,
        )
        for rule_id in range(1, 5)
    ]

    class RecoverableAlertError(Exception):
        pass

    class BatchCache:
        def __init__(self) -> None:
            self.readback_ids: list[int] = []

        def alert_rules(self, **_kwargs):
            return rules

        def update_alert_rule_state(self, *_args, **_kwargs):
            return None

        def alert_rule(self, rule_id: int):
            self.readback_ids.append(rule_id)
            return rules[rule_id - 1]

    class BatchHub:
        def __init__(self) -> None:
            self.cache = BatchCache()
            self.quote_results = iter(
                [
                    sqlite3.OperationalError("database is locked\napi_key=super-secret"),
                    OSError("quote file unavailable\nretry later"),
                    RecoverableAlertError("x" * 180),
                    _quote(price=110),
                ]
            )
            self.quote_calls: list[str] = []

        async def quote(self, symbol: str):
            self.quote_calls.append(symbol)
            result = next(self.quote_results)
            if isinstance(result, Exception):
                raise result
            return result

        async def assess_quote_quality(self, _quote_value, **_kwargs):
            return _quality(score=90)

    hub = BatchHub()

    summary = asyncio.run(evaluate_alert_rules(hub))

    assert summary.checked_count == 4
    assert summary.failed_count == 3
    assert summary.triggered_count == 1
    assert [item.status for item in summary.items] == ["failed", "failed", "failed", "evaluated"]
    assert len(hub.quote_calls) == 4
    assert hub.cache.readback_ids == [4]
    assert "super-secret" not in summary.items[0].message
    assert "<redacted>" in summary.items[0].message
    assert "\n" not in summary.items[0].message
    assert "quote file unavailable retry later" in summary.items[1].message
    assert "x" * 121 not in summary.items[2].message


def test_alert_batch_initialization_failure_still_propagates() -> None:
    class FailingBatchCache:
        def alert_rules(self, **_kwargs):
            raise sqlite3.OperationalError("cannot initialize alert batch")

    with pytest.raises(sqlite3.OperationalError, match="cannot initialize alert batch"):
        asyncio.run(evaluate_alert_rules(SimpleNamespace(cache=FailingBatchCache())))


def test_alert_rule_evaluation_cancellation_propagates() -> None:
    rule = _alert_rule(
        condition_type="price_above",
        threshold=100,
        last_state="未触发",
        last_triggered_at=None,
        cooldown_seconds=300,
    )

    class CancellationCache:
        def alert_rules(self, **_kwargs):
            return [rule]

    class CancellationHub:
        cache = CancellationCache()

        async def quote(self, _symbol: str):
            raise asyncio.CancelledError

    with pytest.raises(asyncio.CancelledError):
        asyncio.run(evaluate_alert_rules(CancellationHub()))


def test_alert_rule_read_cancellation_propagates() -> None:
    started = threading.Event()
    release = threading.Event()

    class BlockingCache:
        def alert_rules(self, **_kwargs):
            started.set()
            release.wait(timeout=2)
            return []

    async def run_check() -> None:
        task = asyncio.create_task(evaluate_alert_rules(SimpleNamespace(cache=BlockingCache())))
        assert await asyncio.to_thread(started.wait, 1)
        task.cancel()
        try:
            with pytest.raises(asyncio.CancelledError):
                await task
        finally:
            release.set()

    asyncio.run(run_check())

class RuleDefinitionTests(unittest.TestCase):
    def test_rule_definitions_are_versioned_and_parameterized(self) -> None:
        rules = rule_definitions()
        self.assertGreaterEqual(len(rules), 6)
        for rule in rules:
            self.assertEqual(rule.version, RULE_VERSION)
            self.assertTrue(rule.parameters, rule.id)

class FactorCalibrationLogicTests(unittest.TestCase):
    def test_risk_pressure_uses_normalized_positive_direction(self) -> None:
        self.assertEqual(_factor_specs()["risk_pressure"].direction, "正向")

    def test_weak_calibration_never_adds_positive_impact(self) -> None:
        calibration = FactorCalibration(
            sample_count=12,
            win_rate=35,
            avg_forward_5d_return=-1.2,
            avg_forward_10d_return=-2.0,
            max_adverse_return=-4.5,
            stability_score=66,
            expected_level="风险",
            confidence_level="偏弱",
            note="test",
        )
        factor = StandardFactor(
            id="risk_pressure",
            name="风险压力",
            category="风控",
            value="测试",
            score=60,
            level="良好",
            direction="正向",
            weight=1,
            calibration=calibration,
        )
        base_impact = round((factor.score - 50) / 2)

        self.assertLessEqual(_factor_score_impact(factor), base_impact)
        self.assertLessEqual(_factor_calibration_impact(calibration), 0)

class AlertCooldownTests(unittest.TestCase):
    def test_trigger_emits_when_state_changes_or_cooldown_expires(self) -> None:
        rule = _alert_rule(last_state="未触发", last_triggered_at=None, cooldown_seconds=300)
        self.assertTrue(_should_emit_event(rule, True, "2026-05-13 10:00:00"))

        cooling = _alert_rule(last_state="触发", last_triggered_at="2026-05-13 09:59:00", cooldown_seconds=300)
        self.assertFalse(_should_emit_event(cooling, True, "2026-05-13 10:00:00"))

        expired = _alert_rule(last_state="触发", last_triggered_at="2026-05-13 09:54:30", cooldown_seconds=300)
        self.assertTrue(_should_emit_event(expired, True, "2026-05-13 10:00:00"))

        self.assertFalse(_should_emit_event(expired, False, "2026-05-13 10:01:00"))

    def test_alert_transition_decision_covers_trigger_recovery_and_quality_gate(self) -> None:
        first = _alert_rule(last_state="未触发", last_triggered_at=None, cooldown_seconds=300)
        first_decision = decide_alert_transition(first, True, "2026-05-13 10:00:00", quality_score=45)
        self.assertEqual(first_decision.event_type, "触发")
        self.assertTrue(first_decision.should_create_event)
        self.assertTrue(first_decision.should_update_triggered_at)
        self.assertEqual(first_decision.trigger_increment, 1)

        repeated_low_quality = _alert_rule(last_state="触发", last_triggered_at="2026-05-13 09:54:00", cooldown_seconds=300)
        repeated_decision = decide_alert_transition(repeated_low_quality, True, "2026-05-13 10:00:00", quality_score=45)
        self.assertFalse(repeated_decision.should_create_event)
        self.assertFalse(repeated_decision.should_update_triggered_at)
        self.assertEqual(repeated_decision.trigger_increment, 0)

        recovery = decide_alert_transition(repeated_low_quality, False, "2026-05-13 10:00:00", quality_score=90)
        self.assertEqual(recovery.event_type, "恢复")
        self.assertTrue(recovery.should_create_event)
        self.assertFalse(recovery.should_update_triggered_at)
        self.assertEqual(recovery.trigger_increment, 0)

    def test_alert_transition_recovers_from_bad_last_triggered_time(self) -> None:
        bad_time = _alert_rule(last_state="触发", last_triggered_at="bad-time", cooldown_seconds=300)

        decision = decide_alert_transition(bad_time, True, "2026-05-13 10:00:00", quality_score=90)

        self.assertTrue(decision.should_create_event)
        self.assertTrue(decision.should_update_triggered_at)

    def test_alert_transition_recovers_from_future_last_triggered_time(self) -> None:
        future_time = _alert_rule(last_state="触发", last_triggered_at="2026-05-13 10:10:00", cooldown_seconds=300)

        decision = decide_alert_transition(future_time, True, "2026-05-13 10:00:00", quality_score=45)

        self.assertTrue(decision.should_create_event)
        self.assertTrue(decision.should_update_triggered_at)

class AlertValidationTests(unittest.TestCase):
    def test_price_alert_rejects_zero_threshold_but_dynamic_support_allows_zero(self) -> None:
        with self.assertRaises(ValueError):
            validate_alert_condition("price_above", 0)
        with self.assertRaises(ValueError):
            validate_alert_condition("trend_score_above", 101)
        with self.assertRaises(ValueError):
            validate_alert_condition("change_pct_below", -120)

        validate_alert_condition("break_support", 0)
        validate_alert_condition("break_resistance", 0)

    def test_alert_condition_rejects_non_finite_thresholds(self) -> None:
        for threshold in (math.nan, math.inf, -math.inf):
            with self.subTest(threshold=threshold):
                with self.assertRaisesRegex(ValueError, "预警阈值必须是有效数字"):
                    validate_alert_condition("price_above", threshold)

class AlertRuleEvaluationTests(unittest.TestCase):
    def test_price_alert_uses_inclusive_threshold(self) -> None:
        quote = _quote(price=1200.0)
        rule = _alert_rule(
            condition_type="price_above",
            threshold=1200.0,
            last_state="未触发",
            last_triggered_at=None,
            cooldown_seconds=300,
        )

        triggered, current_value, message = _evaluate_rule(rule, quote, None)

        self.assertTrue(triggered)
        self.assertEqual(current_value, 1200.0)
        self.assertIn("目标高于 1200.00", message)

    def test_trend_alert_without_analysis_is_not_evaluated(self) -> None:
        quote = _quote()
        rule = _alert_rule(
            condition_type="trend_score_above",
            threshold=70,
            last_state="未触发",
            last_triggered_at=None,
            cooldown_seconds=300,
        )

        triggered, current_value, message = _evaluate_rule(rule, quote, None)

        self.assertFalse(triggered)
        self.assertIsNone(current_value)
        self.assertIn("暂不能评估", message)

    def test_break_support_uses_dynamic_support_when_threshold_is_zero(self) -> None:
        quote = _quote(price=99.0)
        analysis = SimpleNamespace(trend_score=50, support=100.0, resistance=120.0)
        rule = _alert_rule(
            condition_type="break_support",
            threshold=0,
            last_state="未触发",
            last_triggered_at=None,
            cooldown_seconds=300,
        )

        triggered, current_value, message = _evaluate_rule(rule, quote, analysis)

        self.assertTrue(triggered)
        self.assertEqual(current_value, 99.0)
        self.assertIn("支撑参考 100.00", message)

    def test_break_resistance_explicit_threshold_overrides_dynamic_resistance(self) -> None:
        quote = _quote(price=112.0)
        analysis = SimpleNamespace(trend_score=50, support=100.0, resistance=120.0)
        rule = _alert_rule(
            condition_type="break_resistance",
            threshold=110.0,
            last_state="未触发",
            last_triggered_at=None,
            cooldown_seconds=300,
        )

        triggered, current_value, message = _evaluate_rule(rule, quote, analysis)

        self.assertTrue(triggered)
        self.assertEqual(current_value, 112.0)
        self.assertIn("压力参考 110.00", message)

class ChartMarkTests(unittest.TestCase):
    def test_note_marks_preserve_visibility_and_kline_date(self) -> None:
        marks = _note_marks(
            [
                StockNoteItem(
                    id=1,
                    symbol="600519.SH",
                    code="600519",
                    market="SH",
                    name="贵州茅台",
                    note_type="复盘",
                    content="回踩20日线后观察承接。",
                    price=1688.0,
                    trade_date="2026-05-13 10:30:00",
                    color="#2563eb",
                    visible=False,
                    created_at="2026-05-13 10:31:00",
                    updated_at="2026-05-13 10:31:00",
                )
            ]
        )
        self.assertEqual(marks[0].kline_date, "2026-05-13")
        self.assertEqual(marks[0].anchor_price_type, "manual")
        self.assertFalse(marks[0].visible)

def _alert_rule(
    *,
    rule_id: int = 1,
    condition_type: str = "price_above",
    threshold: float = 1.0,
    last_state: str,
    last_triggered_at: str | None,
    cooldown_seconds: int,
) -> AlertRuleItem:
    return AlertRuleItem(
        id=rule_id,
        symbol="600519.SH",
        code="600519",
        market="SH",
        stock_name="贵州茅台",
        name="测试预警",
        condition_type=condition_type,
        condition_label=condition_type,
        threshold=threshold,
        enabled=True,
        last_checked_at=None,
        last_triggered_at=last_triggered_at,
        last_state=last_state,
        trigger_count=0,
        cooldown_seconds=cooldown_seconds,
        created_at="2026-05-13 09:00:00",
        updated_at="2026-05-13 09:00:00",
    )


def _quality(*, score: int, anomaly: str | None = None) -> DataQuality:
    return DataQuality(
        level="良好" if score >= 70 else "较弱",
        source="测试",
        quote_time="2026-05-13 10:00:00",
        kline_count=0,
        score=score,
        anomalies=[anomaly] if anomaly else [],
    )


class _AlertCacheStub:
    def __init__(self) -> None:
        self.persisted_quotes = {}

    def alert_rule(self, _rule_id: int):
        return None

    def update_alert_rule_state(self, rule: AlertRuleItem, *, quote, **_kwargs):
        self.persisted_quotes[rule.condition_type] = quote
        return None


class _AlertDataHubStub:
    def __init__(self, cache: _AlertCacheStub, quote, quality: DataQuality) -> None:
        self.cache = cache
        self._quote = quote
        self._quality = quality
        self.quote_calls: list[str] = []
        self.quality_calls = []

    async def quote(self, symbol: str):
        self.quote_calls.append(symbol)
        return self._quote

    async def assess_quote_quality(self, quote, **_kwargs):
        self.quality_calls.append(quote)
        return self._quality
