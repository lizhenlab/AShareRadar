from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
import math

from app.models.schemas import AlertEvaluationItem, AlertEvaluationSummary, AlertEventItem, AlertRuleItem, AnalysisResult, DataQuality, Quote
from app.repositories.alerts import AlertStateDecision
from app.services.datahub import DataHub
from app.utils.time import now_text, parse_text_time


AlertEvaluationResult = tuple[bool, float | None, str]
AlertEvaluator = Callable[[AlertRuleItem, Quote, AnalysisResult | None], AlertEvaluationResult]
ThresholdValidator = Callable[[float], None]


@dataclass(frozen=True)
class AlertConditionSpec:
    evaluator: AlertEvaluator
    validator: ThresholdValidator | None = None
    needs_analysis: bool = False


async def evaluate_alert_rules(datahub: DataHub, symbol: str | None = None) -> AlertEvaluationSummary:
    checked_at = now_text()
    rules = datahub.cache.alert_rules(symbol=symbol, include_disabled=False, limit=200)
    evaluator = AlertRuleEvaluator(datahub, checked_at)
    results = []
    for rule in rules:
        try:
            results.append(await evaluator.evaluate(rule))
        except (RuntimeError, TimeoutError, ValueError) as exc:
            results.append(_failed_evaluation(rule, exc))
    return AlertEvaluationSummary(
        checked_at=checked_at,
        checked_count=len(rules),
        triggered_count=sum(1 for item in results if item.triggered),
        new_event_count=evaluator.new_event_count,
        failed_count=sum(1 for item in results if item.status == "failed"),
        items=results,
    )


class AlertRuleEvaluator:
    def __init__(self, datahub: DataHub, checked_at: str) -> None:
        self.datahub = datahub
        self.checked_at = checked_at
        self.analysis_cache: dict[str, AnalysisResult] = {}
        self.quote_cache: dict[str, Quote] = {}
        self.quality_cache: dict[str, DataQuality] = {}
        self.new_event_count = 0

    async def evaluate(self, rule: AlertRuleItem) -> AlertEvaluationItem:
        analysis = await self._analysis_for_rule(rule)
        quote = await self._quote_for_rule(rule, analysis)
        triggered, current_value, message = _evaluate_rule(rule, quote, analysis)
        quality = await self._quality_for_rule(rule, quote, analysis)
        message = _message_with_quality_gate(message, triggered, quality)
        event = self._persist_state(rule, quote, triggered, message, quality.score)
        rule_after = self.datahub.cache.alert_rule(rule.id) or rule
        return AlertEvaluationItem(
            rule=rule_after,
            current_value=current_value,
            triggered=triggered,
            message=message,
            event=event,
        )

    async def _analysis_for_rule(self, rule: AlertRuleItem) -> AnalysisResult | None:
        if not _needs_analysis(rule):
            return None
        analysis = self.analysis_cache.get(rule.symbol)
        if analysis is None:
            from app.workflows.individual import analyze_individual_stock

            analysis = await analyze_individual_stock(self.datahub, rule.symbol, persist_history=False)
            self.analysis_cache[rule.symbol] = analysis
            self.quote_cache[rule.symbol] = analysis.quote
        return analysis

    async def _quote_for_rule(self, rule: AlertRuleItem, analysis: AnalysisResult | None) -> Quote:
        if analysis:
            return analysis.quote
        quote = self.quote_cache.get(rule.symbol)
        if quote is None:
            quote = await self.datahub.quote(rule.symbol)
            self.quote_cache[rule.symbol] = quote
        return quote

    async def _quality_for_rule(self, rule: AlertRuleItem, quote: Quote, analysis: AnalysisResult | None) -> DataQuality:
        if analysis:
            self.quality_cache[rule.symbol] = analysis.data_quality
            return analysis.data_quality
        quality = self.quality_cache.get(rule.symbol)
        if quality is None:
            quality = await self.datahub.assess_quote_quality(quote, klines=[], require_kline=False)
            self.quality_cache[rule.symbol] = quality
        return quality

    def _persist_state(
        self,
        rule: AlertRuleItem,
        quote: Quote,
        triggered: bool,
        message: str,
        quality_score: int,
    ) -> AlertEventItem | None:
        decision = decide_alert_transition(rule, triggered, self.checked_at, quality_score)
        event = self.datahub.cache.update_alert_rule_state(
            rule,
            checked_at=self.checked_at,
            state="触发" if triggered else "未触发",
            triggered=triggered,
            message=message,
            quote=quote,
            event_type=decision.event_type,
            force_event=decision.should_create_event,
            decision=decision,
        )
        if event:
            self.new_event_count += 1
        return event


def _failed_evaluation(rule: AlertRuleItem, exc: Exception) -> AlertEvaluationItem:
    detail = " ".join(str(exc).split()).strip() or exc.__class__.__name__
    return AlertEvaluationItem(
        rule=rule,
        triggered=False,
        message=f"{rule.stock_name} 检查失败：{detail[:120]}",
        status="failed",
    )


def validate_alert_condition(condition_type: str, threshold: float | None = None) -> None:
    spec = ALERT_CONDITION_SPECS.get(condition_type)
    if spec is None:
        allowed = "、".join(sorted(ALERT_CONDITION_SPECS))
        raise ValueError(f"不支持的预警条件：{condition_type}。可用条件：{allowed}")
    if threshold is not None:
        if not math.isfinite(threshold):
            raise ValueError("预警阈值必须是有效数字")
        if spec.validator:
            spec.validator(threshold)


def _needs_analysis(rule: AlertRuleItem) -> bool:
    spec = ALERT_CONDITION_SPECS.get(rule.condition_type)
    return bool(spec and spec.needs_analysis)


def _should_emit_event(rule: AlertRuleItem, triggered: bool, checked_at: str) -> bool:
    if not triggered:
        return False
    return _should_emit_trigger_event(rule, checked_at, suppress_repeated_low_quality=False)


def _force_alert_event(rule: AlertRuleItem, triggered: bool, checked_at: str, quality_score: int) -> bool:
    return decide_alert_transition(rule, triggered, checked_at, quality_score).should_create_event


def decide_alert_transition(
    rule: AlertRuleItem,
    triggered: bool,
    checked_at: str,
    quality_score: int,
) -> AlertStateDecision:
    should_create_event = _alert_transition_creates_event(rule, triggered, checked_at, quality_score)
    return AlertStateDecision(
        event_type="触发" if triggered else "恢复",
        should_create_event=should_create_event,
        should_update_triggered_at=should_create_event and triggered,
        trigger_increment=1 if should_create_event and triggered else 0,
    )


def _alert_transition_creates_event(
    rule: AlertRuleItem,
    triggered: bool,
    checked_at: str,
    quality_score: int,
) -> bool:
    if not triggered:
        return rule.last_state == "触发"
    return _should_emit_trigger_event(rule, checked_at, suppress_repeated_low_quality=quality_score < 50)


def _should_emit_trigger_event(
    rule: AlertRuleItem,
    checked_at: str,
    *,
    suppress_repeated_low_quality: bool,
) -> bool:
    if rule.last_state != "触发":
        return True
    if not rule.last_triggered_at:
        return True
    try:
        last_triggered = parse_text_time(rule.last_triggered_at)
        checked = parse_text_time(checked_at)
    except ValueError:
        return True
    if last_triggered > checked:
        return True
    if suppress_repeated_low_quality:
        return False
    return (checked - last_triggered).total_seconds() >= max(30, rule.cooldown_seconds)


def _message_with_quality_gate(message: str, triggered: bool, quality: DataQuality) -> str:
    if not triggered or quality.score >= 70:
        return message
    return f"{message} 数据质量仅 {quality.score} 分，原因：{_quality_reason(quality)}；本次预警应作为低置信提醒。"


def _evaluate_rule(
    rule: AlertRuleItem,
    quote: Quote,
    analysis: AnalysisResult | None,
) -> AlertEvaluationResult:
    spec = ALERT_CONDITION_SPECS.get(rule.condition_type)
    if spec is not None:
        return spec.evaluator(rule, quote, analysis)
    return False, None, f"{quote.name} 当前条件暂不能评估。"


def _eval_price_above(rule: AlertRuleItem, quote: Quote, analysis: AnalysisResult | None) -> AlertEvaluationResult:
    return quote.price >= rule.threshold, quote.price, f"{quote.name} 现价 {quote.price:.2f}，目标高于 {rule.threshold:.2f}。"


def _eval_price_below(rule: AlertRuleItem, quote: Quote, analysis: AnalysisResult | None) -> AlertEvaluationResult:
    return quote.price <= rule.threshold, quote.price, f"{quote.name} 现价 {quote.price:.2f}，目标低于 {rule.threshold:.2f}。"


def _eval_change_pct_above(rule: AlertRuleItem, quote: Quote, analysis: AnalysisResult | None) -> AlertEvaluationResult:
    return quote.change_pct >= rule.threshold, quote.change_pct, f"{quote.name} 涨跌幅 {quote.change_pct:.2f}%，目标高于 {rule.threshold:.2f}%。"


def _eval_change_pct_below(rule: AlertRuleItem, quote: Quote, analysis: AnalysisResult | None) -> AlertEvaluationResult:
    return quote.change_pct <= rule.threshold, quote.change_pct, f"{quote.name} 涨跌幅 {quote.change_pct:.2f}%，目标低于 {rule.threshold:.2f}%。"


def _eval_trend_score_above(rule: AlertRuleItem, quote: Quote, analysis: AnalysisResult | None) -> AlertEvaluationResult:
    if analysis is None:
        return _analysis_unavailable_result(quote)
    return (
        analysis.trend_score >= rule.threshold,
        float(analysis.trend_score),
        f"{quote.name} 趋势评分 {analysis.trend_score}，目标高于 {rule.threshold:.0f}。",
    )


def _eval_trend_score_below(rule: AlertRuleItem, quote: Quote, analysis: AnalysisResult | None) -> AlertEvaluationResult:
    if analysis is None:
        return _analysis_unavailable_result(quote)
    return (
        analysis.trend_score <= rule.threshold,
        float(analysis.trend_score),
        f"{quote.name} 趋势评分 {analysis.trend_score}，目标低于 {rule.threshold:.0f}。",
    )


def _eval_break_support(rule: AlertRuleItem, quote: Quote, analysis: AnalysisResult | None) -> AlertEvaluationResult:
    if analysis is None:
        return _analysis_unavailable_result(quote)
    target = _dynamic_level_target(rule.threshold, analysis.support)
    return quote.price <= target, quote.price, f"{quote.name} 现价 {quote.price:.2f}，支撑参考 {target:.2f}。"


def _eval_break_resistance(rule: AlertRuleItem, quote: Quote, analysis: AnalysisResult | None) -> AlertEvaluationResult:
    if analysis is None:
        return _analysis_unavailable_result(quote)
    target = _dynamic_level_target(rule.threshold, analysis.resistance)
    return quote.price >= target, quote.price, f"{quote.name} 现价 {quote.price:.2f}，压力参考 {target:.2f}。"


def _dynamic_level_target(threshold: float, fallback: float) -> float:
    return threshold if threshold > 0 else fallback


def _analysis_unavailable_result(quote: Quote) -> AlertEvaluationResult:
    return False, None, f"{quote.name} 当前条件暂不能评估。"


def _validate_positive_price(threshold: float) -> None:
    if threshold <= 0:
        raise ValueError("价格预警阈值必须大于0。")


def _validate_trend_score(threshold: float) -> None:
    if not 0 <= threshold <= 100:
        raise ValueError("趋势评分预警阈值应在0到100之间。")


def _validate_change_pct(threshold: float) -> None:
    if not -100 <= threshold <= 100:
        raise ValueError("涨跌幅预警阈值应在-100%到100%之间。")


def _validate_dynamic_level(threshold: float) -> None:
    if threshold < 0:
        raise ValueError("支撑/压力预警阈值不能小于0；填0表示使用系统动态支撑/压力。")


ALERT_CONDITION_SPECS = {
    "price_above": AlertConditionSpec(_eval_price_above, _validate_positive_price),
    "price_below": AlertConditionSpec(_eval_price_below, _validate_positive_price),
    "change_pct_above": AlertConditionSpec(_eval_change_pct_above, _validate_change_pct),
    "change_pct_below": AlertConditionSpec(_eval_change_pct_below, _validate_change_pct),
    "trend_score_above": AlertConditionSpec(_eval_trend_score_above, _validate_trend_score, needs_analysis=True),
    "trend_score_below": AlertConditionSpec(_eval_trend_score_below, _validate_trend_score, needs_analysis=True),
    "break_support": AlertConditionSpec(_eval_break_support, _validate_dynamic_level, needs_analysis=True),
    "break_resistance": AlertConditionSpec(_eval_break_resistance, _validate_dynamic_level, needs_analysis=True),
}


def _quality_reason(quality: DataQuality) -> str:
    if quality.anomalies:
        return "、".join(quality.anomalies[:3])
    if quality.notes:
        return "、".join(quality.notes[:2])
    return "行情可信度不足"
