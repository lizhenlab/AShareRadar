"""Display observed statements without turning unvalidated facts into a score."""

from __future__ import annotations

from app.models.analysis import FinancialHealth, FinancialMetric
from app.models.fuyao import FinancialFact, FinancialPeriodRecord, FinancialReportBundle


def financial_fact_text(fact: FinancialFact) -> str:
    if fact.raw_value is None:
        return "未返回"
    if fact.unit == "%" and fact.value is not None:
        return f"{fact.value:g}%"
    return f"{fact.raw_value}（单位待核实）"


def financial_period_label(period: FinancialPeriodRecord) -> str:
    kind = "年报" if period.period_type == "annual" else "季度报告（单季/累计待核实）"
    return f"{period.period_end} {kind}"


def financial_health_from_bundle(
    bundle: FinancialReportBundle, fallback: FinancialHealth | None = None,
) -> FinancialHealth:
    if fallback is not None and fallback.symbol != bundle.symbol:
        raise ValueError("financial fallback belongs to a different stock")
    period = bundle.periods[0] if bundle.periods else None
    metrics = _display_metrics(period, bundle.source) if period is not None else []
    if fallback is not None:
        metrics.extend(item for item in fallback.metrics if item.category != "formal_financial")
    return FinancialHealth(
        symbol=bundle.symbol, updated_at=bundle.fetched_at, score=None, score_available=False,
        formal_minimum_complete=False, report_period=period.period_end if period else None,
        metric_scope="formal_financial_health", level="不可用",
        summary=(f"已获取 {financial_period_label(period)} 的财务原始数值，当前只展示事实，不生成财务体检分。"
                 if period else "本次成功响应中没有财务记录，尚不能形成财务体检结论。"),
        metrics=metrics, highlights=[f"来源：{bundle.source}；获取时间：{bundle.fetched_at}"],
        risk_notes=list(bundle.warnings), missing_data=_missing_financial_evidence(period), source=bundle.source,
    )


def _display_metrics(period: FinancialPeriodRecord, source: str) -> list[FinancialMetric]:
    currency = period.currency or "未返回"
    return [FinancialMetric(
        name=item.label, value=financial_fact_text(item), level="观察" if item.raw_value is not None else "不可用",
        summary=f"{financial_period_label(period)}；币种：{currency}；来源表：{item.source_kind}。",
        source=source, category="formal_financial",
    ) for item in period.metrics]


def _missing_financial_evidence(period: FinancialPeriodRecord | None) -> list[str]:
    missing = ["经核实的金额/指标单位", "首次披露时刻与修订历史", "经过验证的财务评分规则"]
    if period is None:
        return ["报告期及正式财务记录", *missing]
    available = {item.source_kind for item in period.statements}
    labels = {"income": "同报告期利润表", "balance": "同报告期资产负债表", "cashflow": "同报告期现金流量表"}
    missing.extend(label for key, label in labels.items() if key not in available)
    missing.extend(item.label for item in period.metrics if item.raw_value is None)
    if period.period_type == "quarterly":
        missing.append("季度报告的单季或累计口径")
    return missing
