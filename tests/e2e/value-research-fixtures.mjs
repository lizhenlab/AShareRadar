import { fuyaoStock } from "./fuyao-api-fixtures.mjs";

export function valueResearch(symbol = "600519.SH") {
  return { schema_version: "value-research-v1", symbol, evaluated_at: "2026-09-12T12:00:00+08:00",
    metric_scope: "current_value_research", ranking_effect: "none", point_in_time: false,
    valuation: { available_inputs: 2, required_inputs: 2, coverage: "complete", source: "合成估值观察",
      fetched_at: "2026-09-11T15:30:00+08:00", earnings_yield_pct: 5, earnings_yield_reason: "正 PE TTM 20 的倒数。",
      book_to_price_pct: 50, book_to_price_reason: "正 PB MRQ 2 的倒数。",
      checks: [valueCheck("pe_ttm", "PE TTM", 20, "observed", "已准入当前估值评分输入。", "核实利润中非经常性项目。", "倍"),
        valueCheck("pb_mrq", "PB MRQ", 2, "observed", "已准入当前估值评分输入。", "核实资产减值和权益构成。", "倍")] },
    periods: [valuePeriod("2026-06-30", "quarterly", "合成中报来源", "2026-09-10T09:00:00+08:00"),
      valuePeriod("2025-12-31", "annual", "合成年报来源", "2026-04-01T09:00:00+08:00")],
    limitations: ["当前本地事实不能证明历史时点可知，不参与任何排名。", "金额单位未核实，不能据此计算跨期增长或现金转化率。"] };
}

export function valueStock(symbol = "600519.SH") {
  const record = fuyaoStock(symbol);
  record.value_research = valueResearch(record.symbol);
  record.financials.periods = record.value_research.periods.map(period => ({ ...record.financials.periods[0],
    period_end: period.period_end, period_type: period.period_type, source: period.source, fetched_at: period.fetched_at,
    alignment: "complete", statements: ["income", "cashflow", "balance"].map(sourceKind => ({ source_kind: sourceKind })),
    metrics: period.checks.slice(0, 3).map((check, index) => ({ key: check.key, label: check.label,
      value: check.value, raw_value: String(check.value), unit: check.unit, source_kind: ["income", "cashflow", "balance"][index] })) }));
  return record;
}

export function valueCheck(key, label, value, status, summary, action, unit = null) {
  return { key, label, value, status, summary, action, unit };
}

function valuePeriod(periodEnd, periodType, source, fetchedAt) {
  const quarterly = periodType === "quarterly";
  return { period_end: periodEnd, period_type: periodType, source, fetched_at: fetchedAt,
    observation_available: true, summary: quarterly ? "季度事实仅观察正负，不进行年度现金流比较。" : "同一年报的利润与经营现金流方向存在分歧。",
    checks: [valueCheck("net_profit", "合并净利润", 120, "observed", "合并净利润为正。", "核实利润的持续性及构成。"),
      valueCheck("act_cash_flow_net", "经营活动现金流量净额", -20, "attention", "经营现金流为负。", "核实应收款及营运资本变动。"),
      valueCheck("holder_equity_total", "所有者权益合计", quarterly ? 0 : -10, "attention",
        quarterly ? "所有者权益合计为零。" : "所有者权益合计为负。", "核实权益变动与公司公告。"),
      valueCheck("profit_cashflow_alignment", "年度利润与现金流核对", null, quarterly ? "unavailable" : "attention",
        quarterly ? "单季和累计口径未确认，暂不比较。" : "年度净利润为正且经营现金流为负。", "核对同年度现金流量表附注。")],
  };
}
