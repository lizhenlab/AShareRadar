from __future__ import annotations

from app.models.schemas import AlertEvaluationItem, AlertEvaluationSummary, AlertRuleItem, AnalysisResult, DataQuality, Quote
from app.services.datahub import DataHub
from app.utils.time import now_text, parse_text_time


VALID_ALERT_CONDITIONS = {
    "price_above",
    "price_below",
    "change_pct_above",
    "change_pct_below",
    "trend_score_above",
    "trend_score_below",
    "break_support",
    "break_resistance",
}


async def evaluate_alert_rules(datahub: DataHub, symbol: str | None = None) -> AlertEvaluationSummary:
    checked_at = now_text()
    rules = datahub.cache.alert_rules(symbol=symbol, include_disabled=False, limit=200)
    results: list[AlertEvaluationItem] = []
    analysis_cache: dict[str, AnalysisResult] = {}
    quote_cache: dict[str, Quote] = {}
    quality_cache: dict[str, DataQuality] = {}
    new_events = 0
    for rule in rules:
        analysis = None
        quote = quote_cache.get(rule.symbol)
        if _needs_analysis(rule):
            analysis = analysis_cache.get(rule.symbol)
            if analysis is None:
                from app.workflows.individual import analyze_individual_stock

                analysis = await analyze_individual_stock(datahub, rule.symbol, persist_history=False)
                analysis_cache[rule.symbol] = analysis
                quote_cache[rule.symbol] = analysis.quote
            quote = analysis.quote
        if quote is None:
            quote = await datahub.quote(rule.symbol)
            quote_cache[rule.symbol] = quote
        triggered, current_value, message = _evaluate_rule(rule, quote, analysis)
        if analysis:
            quality = analysis.data_quality
            quality_cache[rule.symbol] = quality
        else:
            quality = quality_cache.get(rule.symbol)
            if quality is None:
                quality = await datahub.assess_quote_quality(quote, klines=[], require_kline=False)
                quality_cache[rule.symbol] = quality
        quality_score = quality.score
        if triggered and quality_score < 70:
            message = f"{message} 数据质量仅 {quality_score} 分，原因：{_quality_reason(quality)}；本次预警应作为低置信提醒。"
        force_event = _should_emit_event(rule, triggered, checked_at)
        if triggered and quality_score < 50 and rule.last_state == "触发":
            force_event = False
        event_type = "触发" if triggered else "恢复"
        if rule.last_state == "触发" and not triggered:
            force_event = True
        event = datahub.cache.update_alert_rule_state(
            rule,
            checked_at=checked_at,
            state="触发" if triggered else "未触发",
            triggered=triggered,
            message=message,
            quote=quote,
            event_type=event_type,
            force_event=force_event,
        )
        if event:
            new_events += 1
        rule_after = datahub.cache.alert_rule(rule.id) or rule
        results.append(
            AlertEvaluationItem(
                rule=rule_after,
                current_value=current_value,
                triggered=triggered,
                message=message,
                event=event,
            )
        )
    return AlertEvaluationSummary(
        checked_at=checked_at,
        checked_count=len(rules),
        triggered_count=sum(1 for item in results if item.triggered),
        new_event_count=new_events,
        items=results,
    )


def validate_alert_condition(condition_type: str, threshold: float | None = None) -> None:
    if condition_type not in VALID_ALERT_CONDITIONS:
        allowed = "、".join(sorted(VALID_ALERT_CONDITIONS))
        raise ValueError(f"不支持的预警条件：{condition_type}。可用条件：{allowed}")
    if threshold is None:
        return
    if condition_type in {"price_above", "price_below"} and threshold <= 0:
        raise ValueError("价格预警阈值必须大于0。")
    if condition_type in {"trend_score_above", "trend_score_below"} and not 0 <= threshold <= 100:
        raise ValueError("趋势评分预警阈值应在0到100之间。")
    if condition_type in {"change_pct_above", "change_pct_below"} and not -100 <= threshold <= 100:
        raise ValueError("涨跌幅预警阈值应在-100%到100%之间。")
    if condition_type in {"break_support", "break_resistance"} and threshold < 0:
        raise ValueError("支撑/压力预警阈值不能小于0；填0表示使用系统动态支撑/压力。")


def _needs_analysis(rule: AlertRuleItem) -> bool:
    return rule.condition_type in {"trend_score_above", "trend_score_below", "break_support", "break_resistance"}


def _should_emit_event(rule: AlertRuleItem, triggered: bool, checked_at: str) -> bool:
    if not triggered:
        return False
    if rule.last_state != "触发":
        return True
    if not rule.last_triggered_at:
        return True
    try:
        last_triggered = parse_text_time(rule.last_triggered_at)
        checked = parse_text_time(checked_at)
    except ValueError:
        return False
    return (checked - last_triggered).total_seconds() >= max(30, rule.cooldown_seconds)


def _evaluate_rule(
    rule: AlertRuleItem,
    quote: Quote,
    analysis: AnalysisResult | None,
) -> tuple[bool, float | None, str]:
    if rule.condition_type == "price_above":
        return quote.price >= rule.threshold, quote.price, f"{quote.name} 现价 {quote.price:.2f}，目标高于 {rule.threshold:.2f}。"
    if rule.condition_type == "price_below":
        return quote.price <= rule.threshold, quote.price, f"{quote.name} 现价 {quote.price:.2f}，目标低于 {rule.threshold:.2f}。"
    if rule.condition_type == "change_pct_above":
        return quote.change_pct >= rule.threshold, quote.change_pct, f"{quote.name} 涨跌幅 {quote.change_pct:.2f}%，目标高于 {rule.threshold:.2f}%。"
    if rule.condition_type == "change_pct_below":
        return quote.change_pct <= rule.threshold, quote.change_pct, f"{quote.name} 涨跌幅 {quote.change_pct:.2f}%，目标低于 {rule.threshold:.2f}%。"
    if analysis and rule.condition_type == "trend_score_above":
        return (
            analysis.trend_score >= rule.threshold,
            float(analysis.trend_score),
            f"{quote.name} 趋势评分 {analysis.trend_score}，目标高于 {rule.threshold:.0f}。",
        )
    if analysis and rule.condition_type == "trend_score_below":
        return (
            analysis.trend_score <= rule.threshold,
            float(analysis.trend_score),
            f"{quote.name} 趋势评分 {analysis.trend_score}，目标低于 {rule.threshold:.0f}。",
        )
    if analysis and rule.condition_type == "break_support":
        target = rule.threshold if rule.threshold > 0 else analysis.support
        return quote.price <= target, quote.price, f"{quote.name} 现价 {quote.price:.2f}，支撑参考 {target:.2f}。"
    if analysis and rule.condition_type == "break_resistance":
        target = rule.threshold if rule.threshold > 0 else analysis.resistance
        return quote.price >= target, quote.price, f"{quote.name} 现价 {quote.price:.2f}，压力参考 {target:.2f}。"
    return False, None, f"{quote.name} 当前条件暂不能评估。"


def _quality_reason(quality: DataQuality) -> str:
    if quality.anomalies:
        return "、".join(quality.anomalies[:3])
    if quality.notes:
        return "、".join(quality.notes[:2])
    return "行情可信度不足"
