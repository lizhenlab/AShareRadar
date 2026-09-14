"""Period-bound sign observations; no ratios from unconfirmed statement units."""

from __future__ import annotations

from datetime import date
import math
import re

from app.models.fuyao import FinancialFact, FinancialPeriodRecord, FinancialReportBundle
from app.models.value_research import ValueFinancialPeriod, ValueResearchCheck
from app.services.fuyao_financials_views import financial_period_provenance
from app.services.fuyao_financials_parsing import financial_time
from app.utils.clock import ASHARE_TIMEZONE


FINANCIAL_VALUE_FIELDS = (
    ("net_profit", "合并净利润", "income", "核实非经常性损益、减值及经营盈利的持续性。"),
    ("act_cash_flow_net", "经营活动现金流量净额", "cashflow", "核实应收、存货、预收预付及季节性资金占用。"),
    ("holder_equity_total", "所有者权益合计", "balance", "核实权益构成、亏损侵蚀与持续经营披露；不等同归母净资产或破产判断。"),
)


def financial_value_periods(bundle: FinancialReportBundle, evaluated_at: str) -> list[ValueFinancialPeriod]:
    keys = [(period.period_end, period.period_type) for period in bundle.periods]
    return [_financial_period(bundle, period, evaluated_at, keys.count((period.period_end, period.period_type)) != 1)
            for period in bundle.periods]


def _financial_period(
    bundle: FinancialReportBundle, period: FinancialPeriodRecord, evaluated_at: str, duplicated: bool,
) -> ValueFinancialPeriod:
    source, fetched = financial_period_provenance(bundle, period)
    rejection = "报告期存在重复观察，须核实后再解释" if duplicated else _period_rejection(period, fetched, evaluated_at)
    checks = [_sign_check(period, field, rejection) for field in FINANCIAL_VALUE_FIELDS]
    checks.append(_profit_cashflow_check(period, checks, rejection))
    return ValueFinancialPeriod(
        period_end=period.period_end, period_type=period.period_type, source=source, fetched_at=fetched,
        observation_available=rejection is None, checks=checks,
        summary=rejection or "只描述本报告期已观察数值的正负；金额单位未核实，不据此生成财务健康结论。",
    )


def _period_rejection(period: FinancialPeriodRecord, fetched_at: str, evaluated_at: str) -> str | None:
    try:
        fetched, cutoff = financial_time(fetched_at), financial_time(evaluated_at)
        end = date.fromisoformat(period.period_end)
        expected = {(12, 31)} if period.period_type == "annual" else {(3, 31), (6, 30), (9, 30), (12, 31)}
        if (end.month, end.day) not in expected or end > fetched.astimezone(ASHARE_TIMEZONE).date():
            return "财报报告期与获取时点不一致，未形成风险观察"
        if fetched > cutoff:
            return "财报获取时间晚于本次研究时刻，未形成风险观察"
    except (ValueError, TypeError, OverflowError):
        return "财报观察时间无法核验，未形成风险观察"
    return None


def _unique_fact(period: FinancialPeriodRecord, key: str, kind: str) -> FinancialFact | None:
    facts = [item for item in period.metrics if item.key == key]
    statements = [item for item in period.statements if item.source_kind == kind]
    if len(facts) != 1 or len(statements) != 1 or facts[0].source_kind != kind:
        return None
    return facts[0] if _consistent_fact_value(facts[0]) else None


def _consistent_fact_value(fact: FinancialFact) -> bool:
    if fact.value is None or fact.raw_value is None or len(fact.raw_value) > 512:
        return False
    raw = fact.raw_value.strip()
    if not re.fullmatch(r"[+-]?(?:[0-9]+(?:\.[0-9]*)?|\.[0-9]+)(?:[eE][+-]?[0-9]+)?", raw):
        return False
    number = float(raw)
    return math.isfinite(number) and number == fact.value


def _sign_check(
    period: FinancialPeriodRecord, field: tuple[str, str, str, str], rejection: str | None,
) -> ValueResearchCheck:
    key, label, kind, action = field
    fact = _unique_fact(period, key, kind)
    if rejection or fact is None:
        return ValueResearchCheck(key=key, label=label, status="unavailable",
                                  summary=rejection or "本期字段缺失、来源表未齐或记录不唯一，不能判定正负。",
                                  action=f"核实本报告期的{label}及对应原始报表。")
    value = fact.value
    assert value is not None
    sign = "为负" if value < 0 else "为零" if value == 0 else "为正"
    return ValueResearchCheck(key=key, label=label, status="attention" if value <= 0 else "observed",
                              value=value, unit=fact.unit,
                              summary=f"所选报告期{label}{sign}；仅作符号观察，不推断金额大小或财务质量。", action=action)


def _profit_cashflow_check(
    period: FinancialPeriodRecord, checks: list[ValueResearchCheck], rejection: str | None,
) -> ValueResearchCheck:
    action = "核对同一年度合并口径净利润、经营现金流量净额及现金流量表补充资料。"
    base = {"key": "profit_cashflow_alignment", "label": "年度利润与经营现金流方向", "action": action}
    kinds = [item.source_kind for item in period.statements]
    if rejection or period.period_type != "annual" or period.alignment != "complete" or sorted(kinds) != ["balance", "cashflow", "income"]:
        return ValueResearchCheck(**base, status="unavailable",
                                  summary=rejection or "仅在同一年度三张合并报表完整对齐后检查；季度单季/累计口径未确认。")
    profit, cashflow = checks[0].value, checks[1].value
    if profit is None or cashflow is None:
        return ValueResearchCheck(**base, status="unavailable", summary="同一期合并净利润或经营现金流缺失，不能检查方向分歧。")
    if profit > 0 and cashflow < 0:
        return ValueResearchCheck(**base, status="attention",
                                  summary="本年报合并净利润为正、经营现金流为负；需核实营运资本变动和非现金损益，不据此断言财务造假。")
    return ValueResearchCheck(**base, status="observed",
                              summary="本年报未出现“合并净利润为正且经营现金流为负”的组合；不代表现金转换质量合格。")
