import { expect, test } from "@playwright/test";
import { mockApi } from "./frontend-flow-api-fixtures.mjs";


test("executable-candidate Shadow is explicit, honest, responsive, and locally degradable", async ({ page }, testInfo) => {
  test.skip(!testInfo.project.name.includes("chromium"), "desktop/mobile Chromium are the product gates");
  if (testInfo.project.name === "mobile-chromium") await page.setViewportSize({ width: 390, height: 844 });
  let shadowCalls = 0;
  let failLocally = false;
  await mockApi(page, {
    api(url) {
      if (url.pathname === "/api/market-scans/latest") return { payload: null };
      if (url.pathname === "/api/strategy-lab/strategies") {
        return { payload: { items: [], total: 0, page: 1, page_size: 100, page_count: 0 } };
      }
      if (url.pathname === "/api/strategy-lab/executable-candidate-shadow") {
        shadowCalls += 1;
        if (failLocally) {
          return { response: { status: 503, contentType: "application/json", body: JSON.stringify({ detail: "Shadow fixture unavailable" }) } };
        }
        return { payload: shadowReport() };
      }
      return null;
    },
  });

  await page.goto("/");
  await page.locator('[data-primary-view="market"]').click();
  await expect(page.locator("#strategyLab")).toBeHidden();
  expect(shadowCalls).toBe(0);
  await page.locator("#marketScanStrategyToggle").click();
  await expect(page.locator("#strategyExecutableShadow")).toBeVisible();
  expect(shadowCalls).toBe(0);

  await page.locator("#executableShadowRunId").fill("77");
  await page.locator("#executableShadowGenerate").click();
  await expect(page.locator("#executableShadowResult")).toHaveAttribute("data-state", "ready");
  expect(shadowCalls).toBe(1);

  const panel = page.locator("#strategyExecutableShadow");
  await expect(panel).toContainText("research_shadow · 仅影子研究");
  await expect(panel).toContainText("not_generated · 尚未生成");
  await expect(panel).toContainText("来源批次生产评分与排名保持不变");
  await expect(panel).toContainText("ADV unavailable");
  await expect(panel).toContainText("冻结当日成交额参与率代理");
  await expect(panel).toContainText("未验证 Alpha");
  await expect(panel).toContainText("生产原排名");
  await expect(panel).toContainText("Shadow 顺序");
  await expect(panel).toContainText("行业暴露");
  await expect(panel).toContainText("板块暴露");
  await expect(panel).toContainText("预计换手");
  await expect(panel).toContainText("日线成交代理");
  await expect(panel.locator(".executable-shadow-table-wrap tbody tr")).toHaveCount(2);

  const layout = await panel.evaluate((element) => {
    const box = element.getBoundingClientRect();
    const table = element.querySelector(".executable-shadow-table-wrap");
    return {
      left: box.left,
      right: box.right,
      viewport: window.innerWidth,
      tableScrollable: table.scrollWidth > table.clientWidth,
      overflowX: getComputedStyle(table).overflowX,
    };
  });
  expect(layout.left).toBeGreaterThanOrEqual(0);
  expect(layout.right).toBeLessThanOrEqual(layout.viewport + 1);
  expect(layout.overflowX).toBe("auto");
  if (testInfo.project.name === "mobile-chromium") expect(layout.tableScrollable).toBeTruthy();
  if (testInfo.project.name === "mobile-chromium") {
    const controls = await panel.evaluate((element) => ({
      inputFontSize: Number.parseFloat(getComputedStyle(element.querySelector("input")).fontSize),
      buttonHeight: element.querySelector("button[type=submit]").getBoundingClientRect().height,
    }));
    expect(controls.inputFontSize).toBeGreaterThanOrEqual(16);
    expect(controls.buttonHeight).toBeGreaterThanOrEqual(44);
  }

  failLocally = true;
  await page.locator("#executableShadowGenerate").click();
  await expect(page.locator("#executableShadowResult")).toHaveAttribute("data-state", "error");
  await expect(panel.locator(".executable-shadow-local-error")).toContainText("本面板不可用");
  await expect(page.locator("#marketScanTitle")).toBeVisible();
});


function shadowReport() {
  const first = candidate("600519.SH", "贵州茅台", "sh_main", "白酒", 12, 1, 31.5, .03, 100, 150000, 312.5);
  const second = candidate("300750.SZ", "宁德时代", "chinext", "电池", 20, 2, 38, .02, 100, 120000, 250);
  return {
    schema_version: "market-scan-executable-candidate-shadow-v2",
    status: "research_shadow", efficacy_status: "not_generated", production_effect: "none",
    production_ranking_mutated: false, database_write_performed: false,
    evidence: {
      run_id: 77, status: "success", mode: "official", scope: "沪市 + 深市 + 北交所当前上市A股",
      data_date: "2026-08-12", quote_date: "2026-08-12", scan_rule_version: "full-market-scan-v2",
      production_score_rule_version: "full-market-score-v4", production_score_spec_hash: "a".repeat(64),
      result_count: 3, successful_result_count: 3, verified_point_in_time_count: 2,
    },
    strategy_contract_version: "executable-candidate-shadow-spec-v2", strategy_fingerprint: "b".repeat(64),
    strategy_spec: strategySpec(), gate_policy: gatePolicy(),
    summary: {
      status: "ready", no_trade: false, no_trade_reasons: [], evaluated_count: 3, eligible_count: 2,
      selected_count: 2, rejected_count: 1, adjusted_count: 0, unfilled_count: 0,
      target_invested_weight: .05, estimated_turnover: .05, estimated_round_trip_cost_cny: 562.5,
      residual_cash_cny: 730000, evidence_verified_count: 2, replacement_attempt_count: 1,
      pool_exhausted: true, underinvested_reason: "候选池在约束后耗尽，仅入选 2/30 只",
      notes: ["只读冻结截面投影"],
    },
    selected: [first, second], candidate_preview: [first, second, rejectedCandidate()], candidate_total: 3,
    exposure_audit: {
      selected_count: 2, selected_weight: .05, top10_weight: .05,
      industry_weights: { "白酒": .03, "电池": .02 }, board_weights: { sh_main: .03, chinext: .02 },
      average_risk_score: 34.1, average_tradability_score: 81.4,
      estimated_round_trip_cost_cny: 562.5, estimated_turnover: .05,
    },
    draft_result_digest: "c".repeat(64),
    limitations: ["收益有效性尚未生成。", "当前没有可信历史ADV。", "日线代理不能证明盘口排队成交。"],
    canonical_digest: "d".repeat(64),
  };
}

function strategySpec() {
  return {
    name: "全市场可执行候选榜 Shadow v1", description: "只读投影", schema_version: 1,
    universe: { boards: ["sh_main", "star", "sz_main", "chinext", "beijing"] },
    exclusions: { exclude_st: true, exclude_new: true, min_listing_days: 120, exclude_suspended: true, min_history_sessions: 61, min_data_quality_score: 80, min_amount_cny: 100000000 },
    hard_filters: [
      { field: "risk", operator: "lte", value: 55, period_sessions: null },
      { field: "tradability", operator: "gte", value: 55, period_sessions: null },
    ],
    objectives: { alpha_1d: .05, alpha_5d: .2, alpha_20d: .25, confidence: .05, risk: .25, tradability: .2 },
    profile: "custom",
    portfolio_constraints: { stock_count: 30, weighting_method: "risk_adjusted", max_stock_weight: .05, max_industry_positions: 3, max_industry_weight: .2, max_board_weight: .5, min_position_amount_cny: 10000, max_notional_share_of_daily_amount: .001, custom_weights: {} },
    rebalance_policy: { hold_sessions: 5, cadence: "manual", rebalance_every_sessions: 5, buy_utility_threshold: 0, hold_utility_threshold: 0 },
    execution_policy: { t_plus_one: true, respect_price_limits: true, respect_suspensions: true, cost_profile: "conservative", commission_rate: .0003, minimum_commission_cny: 5, sell_stamp_duty_rate: .0005, transfer_fee_rate: .00001, buy_slippage_bps: 10, sell_slippage_bps: 10 },
    evidence_policy: { minimum_quality_score: 80, maximum_market_data_age_days: 1, maximum_fundamental_data_age_days: 120, allowed_sources: [], blocked_sources: [], require_verified_point_in_time_evidence: true },
  };
}

function gatePolicy() {
  return {
    exclude_st: true, exclude_new: true, suspension_evidence: "frozen_daily_amount_and_reason_proxy",
    price_limit_evidence: "frozen_daily_single_price_proxy", minimum_listing_days: 120,
    minimum_history_sessions: 61, minimum_amount_cny: 100000000, minimum_tradability_score: 55,
    maximum_risk_score: 55, adv_evidence_status: "unavailable",
    capacity_basis: "frozen_session_amount_participation_proxy", maximum_notional_share_of_session_amount: .001,
  };
}

function candidate(symbol, name, board, industry, originalRank, utilityRank, risk, weight, quantity, amount, cost) {
  return {
    symbol, code: symbol.slice(0, 6), name, board, industry, original_rank: originalRank,
    utility_rank: utilityRank, utility_score: 82, alpha_1d: 62, alpha_5d: 72, alpha_20d: 78,
    confidence: 80, risk, tradability: 82, status: "selected", target_weight: weight,
    target_quantity: quantity, estimated_gross_amount_cny: amount, estimated_round_trip_cost_cny: cost,
    evidence_verified: true, hard_filter_failures: [], reasons: ["冻结时点证据通过"],
    rank_change_reason: "保留生产原始排名，独立计算 Shadow 顺序",
  };
}

function rejectedCandidate() {
  return {
    ...candidate("000001.SZ", "平安银行", "sz_main", "银行", 5, 3, 72, 0, 0, 0, 0),
    status: "rejected", hard_filter_failures: ["risk <= 55.0"],
  };
}
