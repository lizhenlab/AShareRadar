"""Narrow financial fact lookup; never relax general document/forecast admission."""

from __future__ import annotations

from datetime import date
import re

from app.models.fuyao import FinancialFact, FinancialPeriodRecord, FinancialReportBundle
from app.models.research import StockQuestionAnswer
from app.services.fuyao_financials_views import financial_fact_text, financial_period_label
from app.utils.clock import market_now
from app.utils.symbols import standard_a_share_stock_symbol

_FACT_PATTERNS = (
    (r"归母净利润|归属.{0,8}净利润", "parent_holder_net_profit"),
    (r"营收.{0,4}(?:同比|增长)|营业收入.{0,4}(?:同比|增长)", "calculate_operating_income_yoy_growth_ratio"),
    (r"营业收入|营收", "operating_income"), (r"净利润", "net_profit"),
    (r"营业利润", "operating_profit"), (r"利润总额", "profit_total"),
    (r"经营.{0,8}现金流|经营现金流", "act_cash_flow_net"),
    (r"投资.{0,8}现金流", "invest_cash_flow_net"), (r"筹资.{0,8}现金流", "financing_cash_flow_net"),
    (r"资产总计|总资产", "assets_total"), (r"负债合计|总负债", "total_debt"),
    (r"所有者权益|股东权益", "holder_equity_total"), (r"应收账款", "accounts_receivable"),
    (r"货币资金", "cash"), (r"基本每股收益|每股收益|\bEPS\b", "basic_eps"),
    (r"研发费用", "research_and_development_expenses"),
)
_FINANCIAL = re.compile(r"财报|年报|季报|财务|营业收入|营收|利润|现金流|负债|资产|权益|每股收益|EPS|ROE|研发费用|应收账款|货币资金", re.I)
_UNSUPPORTED = re.compile(r"公告|原文|董事长|总经理|实控人|新闻|研报|预测|预期|预告|预计|未来|明年|明天|下周|下月|股价|涨停|跌停|买入|卖出|值得买", re.I)
_DERIVED = re.compile(r"TTM|有息负债|资产负债率|ROE|净资产收益率|环比|净利润.{0,4}(?:同比|增长)|利润率", re.I)
_JUDGMENT = re.compile(r"造假|风险|原因|为什么|分析|影响|好不好|合理|怎么样|倍数|比较|高于|低于|均值|同行|排名|占比")
_QUARTERS = ((r"一季|第一季|第1季|Q1|03-31", 1), (r"二季|第二季|第2季|中报|上半年|半年报|Q2|06-30", 2),
             (r"三季|第三季|第3季|Q3|09-30", 3), (r"四季|第四季|第4季|年报|全年|Q4|12-31", 4))


def financial_fact_answer(question: str, bundle: FinancialReportBundle) -> StockQuestionAnswer | None:
    if not _FINANCIAL.search(question):
        return None
    if _UNSUPPORTED.search(question):
        return _unavailable(question, bundle, "当前接口只支持结构化财务数值，不提供公告原文、人物事实或投资预测。", out_of_scope=True)
    if _DERIVED.search(question):
        return _unavailable(question, bundle, "当前单位、口径或所需字段尚未核实，不能计算所问的派生指标。")
    if _JUDGMENT.search(question):
        return _unavailable(question, bundle, "当前只核对原始财务字段，不能由单个数值回答原因、比较或财务判断。")
    try:
        _requested_identity(question, bundle.symbol)
        key = _requested_field(question)
        period = _requested_period(question, bundle)
    except ValueError as exc:
        return _unavailable(question, bundle, str(exc))
    fact = next((item for item in period.metrics if item.key == key), None)
    if fact is None or fact.raw_value is None or fact.value is None:
        return _unavailable(question, bundle, f"{financial_period_label(period)} 未返回可识别的所问数值。")
    if fact.source_kind == "indicators" and fact.unit is None:
        return _unavailable(question, bundle, f"{financial_period_label(period)} 的指标单位未确认，不能把原字符串解释为百分比。")
    return _fact_answer(question, bundle, period, fact)


def _requested_identity(question: str, symbol: str) -> None:
    codes = re.findall(r"(?<!\d)[0-9]{6}(?:\.(?:SH|SZ|BJ))?(?!\d)", question, re.I)
    if any(standard_a_share_stock_symbol(code) != symbol for code in codes):
        raise ValueError("所问证券与当前财务记录不一致，请先切换到对应个股。")


def _requested_field(question: str) -> str:
    covered: list[tuple[int, int]] = []
    keys: set[str] = set()
    for pattern, key in _FACT_PATTERNS:
        for match in re.finditer(pattern, question, re.I):
            if any(left <= match.start() and match.end() <= right for left, right in covered):
                continue
            covered.append(match.span())
            keys.add(key)
    if len(keys) != 1:
        raise ValueError("当前一次核对一个财务字段，请指定营收、净利润或经营现金流等单项字段。")
    return next(iter(keys))


def _requested_period(question: str, bundle: FinancialReportBundle) -> FinancialPeriodRecord:
    years = _requested_years(question)
    quarter = _requested_quarter(question)
    if not years:
        return _latest_requested_period(question, quarter, bundle)
    year, selected = int(next(iter(years))), quarter or 4
    month, day = ((3, 31), (6, 30), (9, 30), (12, 31))[selected - 1]
    end = date(year, month, day).isoformat()
    explicit_q4 = re.search(r"四季|4季|Q4", question, re.I)
    kind = "annual" if selected == 4 and not explicit_q4 else "quarterly"
    _validate_requested_date(question, end)
    period = next((item for item in bundle.periods if item.period_end == end and item.period_type == kind), None)
    if period is None:
        raise ValueError(f"当前缓存没有 {end} 的{'年报' if kind == 'annual' else '季度报告'}，不能用其他报告期代答。")
    return period


def _latest_requested_period(question: str, quarter: int | None, bundle: FinancialReportBundle) -> FinancialPeriodRecord:
    if quarter == 4 and "年报" in question:
        annual = next((item for item in bundle.periods if item.period_type == "annual"), None)
        if annual is None:
            raise ValueError("当前缓存没有年报记录。")
        return annual
    if quarter is not None:
        raise ValueError("请同时指定年份；不能把不同年份的同一季度混用。")
    if not bundle.periods:
        raise ValueError("当前缓存中没有可核对的财务报告期。")
    return bundle.periods[0]


def _requested_years(question: str) -> set[str]:
    current_year = market_now().year
    years = set(re.findall(r"(?<!\d)((?:19|20)\d{2})(?!\d)", question))
    if len(years) > 1 or re.search(r"最近\s*[两二三四五六七八九十\d]+\s*年|历年|过去|上季度|本季度|上期|前年", question):
        raise ValueError("当前一次核对一个明确报告期，请指定年份和报告期。")
    if "去年" in question:
        years.add(str(current_year - 1))
    if re.search(r"今年|本年", question):
        years.add(str(current_year))
    if len(years) > 1:
        raise ValueError("所问报告期相互冲突，请指定一个明确报告期。")
    return years


def _validate_requested_date(question: str, end: str) -> None:
    dates = re.findall(r"(?:19|20)\d{2}-\d{2}-\d{2}", question)
    if dates and set(dates) != {end}:
        raise ValueError("请指定有效的年报或季报期末日期，不能用任意日期代替报告期。")


def _requested_quarter(question: str) -> int | None:
    quarters = {quarter for pattern, quarter in _QUARTERS if re.search(pattern, question, re.I)}
    report_ids = re.findall(r"(?:19|20)\d{2}-([1-4])(?!\d|-)", question)
    quarters.update(int(value) for value in report_ids)
    if len(quarters) > 1:
        raise ValueError("当前一次核对一个报告期，所问季度不唯一。")
    return next(iter(quarters), None)


def _fact_answer(
    question: str, bundle: FinancialReportBundle, period: FinancialPeriodRecord, fact: FinancialFact,
) -> StockQuestionAnswer:
    report_dates = "、".join(period.supplier_report_dates) or "未返回"
    evidence = [f"股票：{bundle.symbol}；报告期：{financial_period_label(period)}；字段：{fact.key}；来源表：{fact.source_kind}。",
                f"来源：{bundle.source}；获取时间：{bundle.fetched_at}；币种：{period.currency or '未返回'}。",
                f"供应商报告日期：{report_dates}；该字段不等同于已核实的首次披露时刻。"]
    answer = f"{financial_period_label(period)} 的{fact.label}原值为 {financial_fact_text(fact)}。" + " ".join(evidence[1:])
    return StockQuestionAnswer(symbol=bundle.symbol, updated_at=bundle.fetched_at, question=question,
                              topic="财务事实", conclusion=f"{fact.label}：{financial_fact_text(fact)}", answer=answer,
                              confidence=0, confidence_note="结构化字段逐项引用，不使用启发式分数表示事实真伪；0为兼容值。",
                              answerability="answerable", answer_source=f"{bundle.source}结构化财务记录", evidence=evidence)


def _unavailable(
    question: str, bundle: FinancialReportBundle, reason: str, *, out_of_scope: bool = False,
) -> StockQuestionAnswer:
    return StockQuestionAnswer(
        symbol=bundle.symbol, updated_at=bundle.fetched_at, question=question, topic="财务事实",
        conclusion="当前财务记录无法回答这个问题", answer=reason + f" 来源：{bundle.source}；获取时间：{bundle.fetched_at}。",
        confidence=0, confidence_note="没有可回答的完整证据，不把缺失解释为零值。",
        answerability="out_of_scope" if out_of_scope else "insufficient_evidence", missing_evidence=[reason],
        answer_source=f"{bundle.source}结构化财务记录",
    )
