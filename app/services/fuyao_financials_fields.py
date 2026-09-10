"""Only documented statement fields and explicitly understood indicator labels."""

from __future__ import annotations

STATEMENT_FIELDS: dict[str, dict[str, str]] = {
    "income": {
        "basic_eps": "基本每股收益", "operating_income": "营业收入", "operating_costs": "营业成本",
        "operating_expenses": "营业支出", "operating_profit": "营业利润", "profit_total": "利润总额",
        "net_profit": "净利润", "parent_holder_net_profit": "归母净利润", "income_tax_expense": "所得税费用",
        "interest_expenses": "利息支出", "manage_fee": "管理费用", "sales_fee": "销售费用",
        "research_and_development_expenses": "研发费用",
    },
    "balance": {
        "total_current_assets": "流动资产合计", "non_current_nets_total": "非流动资产净值合计",
        "assets_total": "资产总计", "total_debt": "负债合计", "holder_equity_total": "所有者权益合计",
        "cash": "货币资金", "accounts_receivable": "应收账款",
    },
    "cashflow": {
        "act_cash_flow_net": "经营活动现金流量净额", "invest_cash_flow_net": "投资活动现金流量净额",
        "financing_cash_flow_net": "筹资活动现金流量净额", "cash_equivalents_net_addition": "现金及现金等价物净增加额",
        "pay_dividends_profits_interest_cash": "分配股利利润或偿付利息支付的现金",
        "pay_fixed_assets_etc_cash": "购建固定资产等支付的现金",
    },
}

INDICATOR_LABELS = {"calculate_operating_income_yoy_growth_ratio": "营业收入同比增长率"}
INDICATOR_ABILITIES = frozenset({"growth", "profitability", "solvency", "operation", "cash-flow"})
FINANCIAL_WARNINGS = [
    "供应商报告日期尚未核实为首次披露时刻；本次获取不能证明历史当时已知。",
    "金额数值单位尚未由公开契约确认；币种不等于金额单位，不作万元或亿元换算。",
    "季度数据的单季或累计口径待确认，不计算 TTM，不生成财务评分。",
    "负债合计不等于有息负债；金融企业与普通企业不能直接套用同一偿债阈值。",
]
