from __future__ import annotations

import math
import unittest
from types import SimpleNamespace

from app.models.schemas import AlertRuleItem, FactorCalibration, StandardFactor, StockNoteItem
from app.services.alerts import _evaluate_rule, _should_emit_event, decide_alert_transition, validate_alert_condition
from app.services.chart_marks import _note_marks
from app.services.research_factors import _factor_calibration_impact, _factor_score_impact, _factor_specs
from app.services.stock_insights import RULE_VERSION, rule_definitions
from tests.factories import make_quote as _quote

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
    condition_type: str = "price_above",
    threshold: float = 1.0,
    last_state: str,
    last_triggered_at: str | None,
    cooldown_seconds: int,
) -> AlertRuleItem:
    return AlertRuleItem(
        id=1,
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
