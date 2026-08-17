import { expect, test } from "@playwright/test";
import { mockApi, selectPrimaryView } from "./frontend-flow-api-fixtures.mjs";


test("strategy templates load only a compiled draft on desktop and mobile Chromium", async ({ page }, testInfo) => {
  test.skip(!testInfo.project.name.includes("chromium"), "desktop/mobile Chromium are the product gates");
  if (testInfo.project.name === "mobile-chromium") await page.setViewportSize({ width: 390, height: 844 });
  const writes = [];
  await mockApi(page, {
    api(url, request) {
      if (url.pathname === "/api/market-scans/latest") return { payload: null };
      if (url.pathname === "/api/strategy-lab/strategies") return { payload: emptyStrategies() };
      if (url.pathname === "/api/strategy-lab/templates") return { payload: templateCatalog() };
      if (url.pathname === "/api/strategy-lab/compile") {
        writes.push({ path: url.pathname, body: request.postDataJSON() });
        return { payload: compiledDraft(request.postDataJSON().spec) };
      }
      if (request.method() !== "GET") writes.push({ path: url.pathname, body: request.postDataJSON() });
      return null;
    },
  });

  await page.goto("/");
  await selectPrimaryView(page, "market");
  await page.locator("#marketScanStrategyToggle").click();
  await expect(page.locator("#strategyTemplateCards .strategy-template-card")).toHaveCount(3);
  await expect(page.locator("#strategyTemplateCatalogStatus")).toContainText("3 个互斥策略镜头");
  await expect(page.locator("#strategyTemplateCatalog")).toContainText("模板身份固定绑定历史合同");
  await expect(page.locator('[data-template-load="shadow_route"]')).toBeDisabled();
  await expect(page.locator('[data-template-load="unavailable_route"]')).toBeDisabled();
  await expect(page.locator('input[value="shadow_route"]')).toBeDisabled();

  await page.locator('input[value="ready_template"]').check();
  await page.locator('[data-template-load="ready_template"]').click();
  await expect.poll(() => writes.map((item) => item.path)).toEqual(["/api/strategy-lab/compile"]);
  expect(writes[0].body).toEqual({ dry_run: true, spec: strategySpec() });
  await expect(page.locator("#strategyName")).toHaveValue("模板草案");
  await expect(page.locator("#strategyTemplateDraftStatus")).toContainText("尚未保存");
  await expect(page.locator("#strategyTemplateCatalogStatus")).toContainText("未保存、未扫描、未改变生产排名");

  await page.locator("#strategyStockCount").fill("10");
  await page.locator("#strategyStockCount").dispatchEvent("change");
  await expect.poll(() => writes.map((item) => item.path)).toEqual([
    "/api/strategy-lab/compile", "/api/strategy-lab/compile",
  ]);
  await expect(page.locator("#strategyTemplateDraftStatus")).toHaveText("基于“模板草案”修改 · 当前为自定义");
  const bounds = await page.locator("#strategyTemplateCatalog").evaluate((element) => {
    const rect = element.getBoundingClientRect();
    return { left: rect.left, right: rect.right, viewport: window.innerWidth, scroll: element.scrollWidth, client: element.clientWidth };
  });
  expect(bounds.left).toBeGreaterThanOrEqual(0);
  expect(bounds.right).toBeLessThanOrEqual(bounds.viewport + 1);
  expect(bounds.scroll).toBeLessThanOrEqual(bounds.client + 1);
});


function emptyStrategies() {
  return { items: [], total: 0, page: 1, page_size: 100, page_count: 0 };
}

function templateCatalog() {
  const readySpec = strategySpec();
  return {
    schema_version: "full-market-strategy-template-catalog-v1", as_of_date: "2026-08-12",
    selection_mode: "exclusive", production_rule_version: "full-market-score-v4",
    production_effect: "none", official_session_count: 2,
    templates: [
      template("ready_template", "available_for_draft", readySpec),
      template("shadow_route", "shadow_only", null),
      template("unavailable_route", "unavailable", null),
    ],
    catalog_digest: "d".repeat(64),
  };
}

function template(templateId, availability, strategySpec) {
  const unavailable = availability === "unavailable";
  const shadow = availability === "shadow_only";
  const name = availability === "available_for_draft" ? "模板草案" : `${templateId} 研究路线`;
  return {
    template_id: templateId, version: 1, name, family: "E2E策略",
    objective: "组织研究目标与约束，不表达上涨概率。",
    horizon: { formation_sessions: 61, holding_sessions: 5, rebalance_sessions: 5, label: "形成61日/持有5日/每5日调仓" },
    availability, strategy_spec: strategySpec,
    contract_status: unavailable ? "unavailable" : "verified",
    efficacy_status: unavailable ? "unavailable" : shadow ? "insufficient_data" : "not_generated",
    regime_evidence_status: "not_generated", required_fields: unavailable ? ["amount", "missing_pit"] : ["amount"],
    missing_fields: unavailable ? ["missing_pit"] : [],
    gate_reasons: [unavailable ? "缺少 PIT 字段，禁止代理。" : shadow ? "样本不足，仅可 Shadow。" : "合同已校验，有效性未生成。"],
    regime_hypotheses: ["当前市场环境匹配尚未生成。"], cost_notes: ["成本模型尚未使用真实盘口。"],
    risk_notes: ["研究分不是上涨概率。"], limitations: ["不是投资建议或已验证收益。"],
    template_digest: ({ ready_template: "a", shadow_route: "b", unavailable_route: "c" }[templateId]).repeat(64),
  };
}

function strategySpec() {
  return {
    name: "模板草案", description: "E2E", schema_version: 1,
    universe: { boards: ["sh_main", "sz_main"] },
    exclusions: { exclude_st: true, exclude_new: false, min_listing_days: 120, exclude_suspended: true, min_history_sessions: 61, min_data_quality_score: 70, min_amount_cny: 0 },
    hard_filters: [
      { field: "amount", operator: "gte", value: 50000000, period_sessions: null },
      { field: "alpha_20d", operator: "gte", value: 35, period_sessions: null },
    ],
    objectives: { alpha_1d: .1, alpha_5d: .25, alpha_20d: .3, confidence: .1, risk: .15, tradability: .1 },
    profile: "custom",
    portfolio_constraints: { stock_count: 20, weighting_method: "equal", max_stock_weight: .1, max_industry_positions: 3, max_industry_weight: .3, max_board_weight: .5, min_position_amount_cny: 5000, max_notional_share_of_daily_amount: .001, custom_weights: {} },
    rebalance_policy: { hold_sessions: 5, cadence: "manual", rebalance_every_sessions: 5, buy_utility_threshold: 0, hold_utility_threshold: 0 },
    execution_policy: { t_plus_one: true, respect_price_limits: true, respect_suspensions: true, cost_profile: "base", commission_rate: .0003, minimum_commission_cny: 5, sell_stamp_duty_rate: .0005, transfer_fee_rate: .00001, buy_slippage_bps: 5, sell_slippage_bps: 5 },
    evidence_policy: { minimum_quality_score: 70, maximum_market_data_age_days: 1, maximum_fundamental_data_age_days: 120, allowed_sources: [], blocked_sources: [], require_verified_point_in_time_evidence: true },
  };
}

function compiledDraft(spec) {
  return {
    normalized_spec: spec, fingerprint: "f".repeat(64), warnings: [],
    execution_plan: { dry_run: true, executable: true, blocked_reasons: [], board_labels: ["上海主板", "深圳主板"], expressions: [], required_fields: ["amount"], objective_order: ["alpha_20d"], portfolio_summary: [], execution_summary: [], estimated_universe: "E2E", estimated_work: "dry-run", will_start_scan: false },
  };
}
