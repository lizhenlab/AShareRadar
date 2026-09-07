from __future__ import annotations

import asyncio
from types import SimpleNamespace

import pytest

from app.models.analysis import DataQuality
from app.models.user_data import AlertRuleInput
from app.services.alerts import evaluate_alert_rules
from app.services.cache import SQLiteCache
from tests.factories import make_quote


def _hub_and_analysis(tmp_path, *, price=100):
    quote = make_quote(price=price)
    quality = DataQuality(score=90, level="高", source="测试", quote_time=quote.timestamp, kline_count=0)
    analysis = SimpleNamespace(
        quote=quote, data_quality=quality, support=110, resistance=90,
        support_available=False, resistance_available=False,
    )
    cache = SQLiteCache(tmp_path / "alerts.sqlite3")

    async def load_analysis(_hub, _symbol):
        return analysis

    async def load_quote(_symbol):
        return quote

    async def load_quality(_quote, **_kwargs):
        return quality

    hub = SimpleNamespace(cache=cache, quote=load_quote, assess_quote_quality=load_quality)
    return hub, analysis, load_analysis


def _seed_triggered(cache, quote, condition):
    rule = cache.create_alert_rule(quote, AlertRuleInput(symbol="600519.SH", condition_type=condition, threshold=0))
    cache.update_alert_rule_state(
        rule, checked_at="2026-05-13 09:59:00", state="触发", triggered=True,
        message="已有触发", quote=quote, event_type="触发", force_event=True,
    )
    return cache.alert_rule(rule.id)


@pytest.mark.parametrize("condition", ["break_support", "break_resistance"])
def test_missing_dynamic_level_preserves_triggered_state_and_events(tmp_path, condition):
    hub, analysis, loader = _hub_and_analysis(tmp_path)
    before = _seed_triggered(hub.cache, analysis.quote, condition)
    events_before = hub.cache.alert_events()

    result = asyncio.run(evaluate_alert_rules(hub, analysis_loader=loader))

    assert result.failed_count == 1
    assert result.new_event_count == result.triggered_count == 0
    assert result.items[0].status == "failed"
    assert result.items[0].current_value is None
    assert "不能评估" in result.items[0].message
    assert hub.cache.alert_rule(before.id) == before
    assert hub.cache.alert_events() == events_before


@pytest.mark.parametrize("condition,level", [("break_support", 90), ("break_resistance", 110)])
def test_available_level_after_unknown_emits_one_real_recovery(tmp_path, condition, level):
    hub, analysis, loader = _hub_and_analysis(tmp_path)
    before = _seed_triggered(hub.cache, analysis.quote, condition)
    unknown = asyncio.run(evaluate_alert_rules(hub, analysis_loader=loader))
    assert unknown.new_event_count == 0
    setattr(analysis, "support" if condition == "break_support" else "resistance", level)
    setattr(analysis, "support_available" if condition == "break_support" else "resistance_available", True)

    recovered = asyncio.run(evaluate_alert_rules(hub, analysis_loader=loader))
    repeated = asyncio.run(evaluate_alert_rules(hub, analysis_loader=loader))

    assert recovered.failed_count == repeated.failed_count == 0
    assert recovered.new_event_count == 1
    assert recovered.items[0].event.event_type == "恢复"
    assert repeated.new_event_count == 0
    current = hub.cache.alert_rule(before.id)
    assert current.last_state == "未触发"
    assert current.last_triggered_at == before.last_triggered_at
    assert current.trigger_count == before.trigger_count


@pytest.mark.parametrize("condition,threshold", [("break_support", 110), ("break_resistance", 90)])
def test_explicit_threshold_still_evaluates_without_dynamic_level(tmp_path, condition, threshold):
    hub, analysis, loader = _hub_and_analysis(tmp_path)
    hub.cache.create_alert_rule(
        analysis.quote, AlertRuleInput(symbol="600519.SH", condition_type=condition, threshold=threshold),
    )
    result = asyncio.run(evaluate_alert_rules(hub, analysis_loader=loader))
    assert result.failed_count == 0
    assert result.triggered_count == result.new_event_count == 1
    assert result.items[0].current_value == 100


def test_unknown_rule_does_not_prevent_other_rules_in_batch(tmp_path):
    hub, analysis, loader = _hub_and_analysis(tmp_path)
    before = _seed_triggered(hub.cache, analysis.quote, "break_support")
    valid = hub.cache.create_alert_rule(
        analysis.quote, AlertRuleInput(symbol="600519.SH", condition_type="price_above", threshold=90),
    )
    result = asyncio.run(evaluate_alert_rules(hub, analysis_loader=loader))
    by_id = {item.rule.id: item for item in result.items}
    assert result.checked_count == 2
    assert result.failed_count == result.triggered_count == result.new_event_count == 1
    assert by_id[before.id].status == "failed"
    assert by_id[valid.id].event.event_type == "触发"
    assert hub.cache.alert_rule(before.id) == before
