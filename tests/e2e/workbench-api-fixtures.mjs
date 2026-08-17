export function workbenchPayload(symbol, { degraded = false, chartMarks = false, withKlines = false } = {}) {
  const stock = stockDetails(symbol);
  const canonicalSymbol = `${stock.code}.${stock.market}`;
  const klines = withKlines ? dailyKlines(240) : [];
  const signalDate = "2026-07-14";
  const owned = () => ({ symbol: canonicalSymbol });
  const researchOwned = () => ({ symbol: canonicalSymbol, updated_at: "2026-07-14 10:00:00" });
  return {
    schema_version: "stock-workbench-v2",
    symbol: canonicalSymbol,
    generated_at: "2026-07-14T02:00:01Z",
    context_generated_at: "2026-07-14T02:00:00Z",
    research_mode: "interactive_shadow",
    production_effect: "none",
    diagnosis_production_effect: "none",
    research_cohort: {
      requested_symbol: canonicalSymbol,
      observed_symbol: canonicalSymbol,
      mode: "interactive_shadow",
      decision_time: "2026-07-14T02:00:00Z",
      quote_event_time: "2026-07-14 10:00:00",
      signal_date: signalDate,
      daily_bar_cutoff: klines.at(-1)?.date || signalDate,
      production_effect: "none",
      advice_persistence: "disabled",
    },
    analysis: {
      quote: {
        code: stock.code, market: stock.market, name: stock.name, price: 100,
        change: 1, change_pct: 1, source: "E2E行情", timestamp: "2026-07-14 10:00:00",
      },
      data_quality: { level: "优秀", score: 95 },
      signal_snapshot: { label: "观察", summary: "E2E" },
      action_advice: { action: "观察", confidence: 60 },
      review: owned(),
      klines,
    },
    insights: {
      overview: researchOwned(), fund_flow: researchOwned(), order_pressure: researchOwned(), events: researchOwned(),
      financial_health: researchOwned(), valuation: researchOwned(), lhb: researchOwned(), abnormal_events: researchOwned(),
      rule_matches: researchOwned(), strategy_cards: [researchOwned()],
    },
    feature_snapshot: researchOwned(), factor_lab: researchOwned(), market_regime: researchOwned(),
    signal_validation: researchOwned(), risk_reward: researchOwned(), timeframe_alignment: researchOwned(),
    alpha_evidence: researchOwned(), diagnosis: researchOwned(), evidence_chain: researchOwned(),
    qa_report: researchOwned(), event_digest: researchOwned(), peer_comparison: researchOwned(),
    t_strategy: researchOwned(), risk_radar: researchOwned(), chip_analysis: researchOwned(),
    leadership: researchOwned(), theme_context: researchOwned(), replay: researchOwned(),
    local_data_warnings: degraded ? [{ component: "notes", message: "本地笔记暂不可用" }] : [],
    chart_marks: chartMarks
      ? { symbol: canonicalSymbol, marks: [{ category: "买点", price: 100, trade_date: signalDate }], categories: ["买点"] }
      : { symbol: canonicalSymbol, marks: [], categories: [] },
    alert_rules: [], alert_events: [], notes: [],
  };
}

export function dailyKlines(count) {
  const start = Date.UTC(2025, 10, 17);
  return Array.from({ length: count }, (_, index) => {
    const date = new Date(start + index * 86400000).toISOString().slice(0, 10);
    const open = 90 + index * 0.06 + Math.sin(index / 7) * 1.5;
    const close = open + Math.sin(index / 3) * 0.7;
    return {
      date, open, close,
      high: Math.max(open, close) + 0.8,
      low: Math.min(open, close) - 0.8,
      volume: 1000000 + index * 1000,
    };
  });
}

function stockDetails(symbol) {
  const rows = {
    "000001.SZ": { code: "000001", market: "SZ", name: "平安银行" },
    "300750.SZ": { code: "300750", market: "SZ", name: "宁德时代" },
    "920066.BJ": { code: "920066", market: "BJ", name: "北交样本" },
  };
  return rows[symbol] || { code: "600519", market: "SH", name: "贵州茅台" };
}
