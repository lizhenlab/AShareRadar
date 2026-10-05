import { expect, test } from "@playwright/test";
import { mockApi, workbenchPayload } from "./frontend-flow-api-fixtures.mjs";

function contextPayload(symbol, { missing = false } = {}) {
  const payload = workbenchPayload(symbol);
  const observation = (name, change_pct) => ({name, change_pct, symbol: name === "沪深300" ? "000300.SH" : "行业01",
    source: "本地行情 <img src=x>", event_at: "2026-07-14 10:00:00", observed_at: "2026-07-14 10:00:01"});
  payload.insights.overview = {...payload.insights.overview, total_score: missing ? 66 : 59, total_level: "观察",
    directional_evidence_score: 72.5, risk_penalty: 2.5, directional_score: 70, evidence_coverage_pct: 75,
    factors: [
      ...["技术面", "量价热度（衍生）", "基本面"].map(name => ({name, score: 80, level: "偏强",
        aggregation_role: "direction", score_available: true, participates_in_total_score: true,
        summary: "合成方向证据", evidence: []})),
      {name: "风险面", aggregation_role: "risk_constraint", score: 40, level: "偏弱",
      score_available: true, participates_in_total_score: true, summary: "已核验的风险", evidence: ["独立风险证据"]}],
    market_context_score: {base_score: 70, reliability_score: 80, score: missing ? 66 : 59,
      rule_version: "current-market-context.v2", relative_adjustment: missing ? 0 : -1,
      before_gates_score: missing ? 70 : 69, pre_reliability_score: missing ? 70 : 61.6375,
      market_multiplier: missing ? 1 : 0.7, industry_multiplier: missing ? 1 : 0.875,
      market: missing ? null : observation("沪深300", -3), industry: missing ? null : observation("银行", -4.25),
      stock_excess_pct: missing ? null : -2.5, industry_excess_pct: missing ? null : -1.25,
      unavailable_reasons: missing ? ["主行业与大盘数据过时，未参与修正"] : [],
      note: "当日相对强弱修正，参数尚未经样本外收益验证。"}};
  payload.factor_lab.factors = [{name: "龙头强度", aggregation_role: "composite", score: 71,
    participates_in_current_score: false, data_nature: "derived", weight: 0,
    value: "相对表现仍可观察", evidence: ["原有趋势与板块证据"],
    calibration: {sample_count: 999, win_rate: 99}, percentile: 99}];
  return payload;
}

function evidenceSupportPayload(symbol, variant) {
  const payload = workbenchPayload(symbol);
  const support = variant === "zero"
    ? {data_quality_score: 0, calibration_coverage_pct: 0, sample_support_pct: 0,
      required_factor_count: 0, calibrated_factor_count: 0, minimum_similar_samples: 0, full_support_sample_threshold: 30}
    : {data_quality_score: 90, calibration_coverage_pct: 50, sample_support_pct: 100 / 30,
      required_factor_count: 4, calibrated_factor_count: 2, minimum_similar_samples: 1, full_support_sample_threshold: 30};
  payload.factor_lab = {...payload.factor_lab, evidence_sufficiency: variant === "zero" ? 0 : 3,
    evidence_sufficiency_version: "factor-evidence-sufficiency.v2", evidence_support: support,
    evidence_sufficiency_note: '最低样本不足 <img src=x onerror="alert(1)">，不由看多方向加分。'};
  if (variant === "missing") delete support.sample_support_pct;
  if (variant === "legacy") delete payload.factor_lab.evidence_sufficiency_version;
  return payload;
}

function aggregationPayload(symbol, variant) {
  const payload = workbenchPayload(symbol);
  const zero = variant === "zero";
  const factor = (id, name, score, share, usage = "direction", available = true) => ({id, name, score: zero ? 0 : score,
    value: "可复核的当前证据", direction: "正向", weight: 1.25, score_share_pct: share, score_usage: usage,
    participates_in_current_score: available, data_nature: available ? "derived" : "unavailable",
    calibration: {sample_count: 0, participates_in_historical_aggregate: false}});
  payload.factor_lab = {...payload.factor_lab, total_score: zero ? 0 : 51, factors: [
    factor("trend_momentum", "趋势动量", 80, 100 / 6), factor("chip_position", "筹码位置", 50, 100 / 6),
    factor("volume_confirmation", "量价确认", 41, 100 / 6), factor("fund_flow_proxy", "量价连续性（衍生）", 50, 100 / 6, "direction", zero),
    factor("valuation_anchor", "估值锚", 50, 100 / 3, "direction", zero),
    {...factor("risk_pressure", "风险压力", 40, 0, "risk_constraint"), score: zero ? 50 : 40,
      calibration: {sample_count: 999, win_rate: 99}},
    factor("unknown", "未知因子 <img src=x>", 80, 0, "excluded"),
  ], score_aggregation: {
    version: "factor-aggregation.v3", directional_score: zero ? 0 : 53.5, risk_penalty: zero ? 0 : 2.5,
    total_score: zero ? 0 : 51, coverage_pct: zero ? 100 : 50,
    groups: ["趋势与价位", "量价", "估值"].map((name, i) => ({name, budget_pct: 100 / 3,
      contribution: zero ? -50 / 3 : [5, -1.5, 0][i], coverage_pct: zero ? 100 : [100, 50, 0][i]})),
    factor_shares: {trend_momentum: 100 / 6, chip_position: 100 / 6, volume_confirmation: 100 / 6,
      fund_flow_proxy: 100 / 6, valuation_anchor: 100 / 3, risk_pressure: 0},
    excluded_ids: zero ? ["unknown"] : ["fund_flow_proxy", "valuation_anchor", "unknown"],
  }};
  if (variant === "legacy") delete payload.factor_lab.score_aggregation;
  return payload;
}

test("current score shows bounded context chain and escaped source details", async ({ page }, testInfo) => {
  await mockApi(page, {workbench: symbol => contextPayload(symbol)});
  await page.goto("/");
  const panel = page.locator("[data-market-context-score]");
  const basis = page.locator("[data-directional-score-basis]");
  await expect(basis).toContainText("独立证据分 72.5 → 风险扣分 2.5 → 方向基分 70.0");
  await expect(basis).toContainText("方向证据覆盖 75.0%；缺失份额不转移。覆盖度不是上涨概率");
  await expect(panel).toBeVisible();
  await expect(panel).toContainText("方向基础 70.0 → 相对强弱修正 69.0 → 市场与行业约束 61.6 → 可靠性约束 59");
  await expect(panel).toContainText("沪深300 000300.SH · -3.00%");
  await expect(panel).toContainText("个股相对行业：-2.50 个百分点");
  await expect(panel).toContainText("行业相对大盘：-1.25 个百分点");
  await expect(panel).toContainText("相对抗跌不等于上涨");
  await panel.locator("summary").click();
  await expect(panel).toContainText("相对强弱实际调整 -1.00 分");
  await expect(panel).toContainText("本地行情 <img src=x>");
  await expect(panel.locator("img")).toHaveCount(0);
  await expect(panel).toContainText("参数尚未经样本外收益验证");
  await expect.poll(() => page.evaluate(() => document.documentElement.scrollWidth <= innerWidth + 1)).toBe(true);
  await page.screenshot({path: testInfo.outputPath("market-context-score.png"), fullPage: false});
});

test("missing context and composite observations never imply missing leadership or historical odds", async ({ page }) => {
  await mockApi(page, {workbench: symbol => contextPayload(symbol, {missing: true})});
  await page.goto("/");
  const panel = page.locator("[data-market-context-score]");
  await expect(panel).toContainText("大盘：未参与（缺少有效观测）");
  await expect(panel).toContainText("主行业：未参与（缺少有效观测）");
  await panel.locator("summary").click();
  await expect(panel).toContainText("主行业与大盘数据过时，未参与修正");
  await expect(panel).toContainText("相对强弱实际调整 0.00 分");
  const risk = page.locator("#factorList .factor-item").filter({hasText: "风险面"});
  await expect(risk).toContainText("风险约束：仅按风险扣分，不作为独立方向证据");
  await expect(risk).not.toContainText("不可用");
  const composite = page.locator("#factorLab .standard-factor.composite");
  await expect(composite).toContainText("71 · 复合观察，不重复计分");
  await expect(composite).toContainText("相对表现仍可观察");
  await expect(composite).toContainText("原有趋势与板块证据");
  await expect(composite).not.toContainText("不可用");
  await expect(composite).not.toContainText("胜率");
  await expect(composite).not.toContainText("999");
});

test("factor evidence basis explains zero and small support without inventing legacy evidence", async ({ page }, testInfo) => {
  let variant = "zero";
  await mockApi(page, {workbench: symbol => evidenceSupportPayload(symbol, variant)});
  await page.goto("/");
  await page.locator("#workspace-tab-strategy").click();
  const lab = page.locator("#factorLab");
  const support = lab.locator("[data-factor-evidence-support]");
  await expect(support).toBeVisible();
  await support.locator("summary").click();
  await expect(support).toContainText("数据质量 0.0分");
  await expect(support).toContainText("历史校准覆盖 0.0%");
  await expect(support).toContainText("样本支持 0.0%");
  await expect(support).toContainText("最低相似样本 0，充分支持阈值 30");
  await expect(support).toContainText("不是统计置信度或上涨概率");
  await expect(support).toContainText('最低样本不足 <img src=x onerror="alert(1)">');
  await expect(support.locator("img")).toHaveCount(0);
  variant = "small";
  await page.reload();
  await support.locator("summary").click();
  await expect(support).toContainText("样本支持 3.3%");
  await expect(support).toContainText("历史校准因子 2/4；最低相似样本 1");
  await support.scrollIntoViewIfNeeded();
  await expect.poll(() => page.evaluate(() => document.documentElement.scrollWidth <= innerWidth + 1)).toBe(true);
  await page.screenshot({path: testInfo.outputPath("factor-evidence-support.png"), fullPage: false});
  for (const state of ["missing", "legacy"]) {
    variant = state;
    await page.reload();
    await expect(lab).toContainText("证据充分度 3/100");
    await expect(support).toHaveCount(0);
    await expect(lab).not.toContainText("最低样本不足");
  }
});

test("factor aggregation v3 shows fixed budgets and risk constraints without overflow", async ({ page }, testInfo) => {
  let variant = "partial";
  await mockApi(page, {workbench: symbol => aggregationPayload(symbol, variant)});
  await page.goto("/");
  await page.locator("#workspace-tab-strategy").click();
  const lab = page.locator("#factorLab");
  const aggregation = lab.locator("[data-factor-score-aggregation]");
  await aggregation.locator("summary").click();
  await expect(aggregation).toContainText("方向分 53.5 → 风险扣分 2.5 → 总分 51");
  await expect(aggregation).toContainText("方向覆盖 50.0%；缺失份额不转移");
  await expect(aggregation.locator(".factor-lab-metrics > span")).toHaveCount(3);
  await expect(aggregation).toContainText("贡献 -1.50 分");
  await expect(aggregation).toContainText("画像不改变方向预算");
  await expect(aggregation).toContainText("综合分和覆盖度都不是上涨概率");
  await expect(lab).toContainText("固定份额 16.7%");
  await expect(lab).toContainText("固定份额 33.3%；缺失保留份额");
  const risk = lab.locator(".standard-factor").filter({hasText: "风险压力"});
  await expect(risk).toContainText("仅作风险约束，风险只扣分，不加看多");
  await expect(risk).not.toContainText("999");
  await expect(risk).not.toContainText("胜率");
  await expect(lab).toContainText("未知因子 <img src=x>");
  await expect(lab).toContainText("未注册，不计入综合分");
  await expect(lab.locator("img")).toHaveCount(0);
  await expect(lab).not.toContainText("权重 1.25");
  await aggregation.scrollIntoViewIfNeeded();
  await expect.poll(() => page.evaluate(() => document.documentElement.scrollWidth <= innerWidth + 1)).toBe(true);
  await aggregation.screenshot({path: testInfo.outputPath("factor-score-aggregation-v3.png")});
  variant = "zero";
  await page.reload();
  await aggregation.locator("summary").click();
  await expect(aggregation).toContainText("方向分 0.0 → 风险扣分 0.0 → 总分 0");
  await expect(lab).toContainText("0 分");
  variant = "legacy";
  await page.reload();
  await expect(aggregation).toHaveCount(0);
  await expect(lab).toContainText("80 · 权重 1.25");
  await expect(lab).not.toContainText("固定份额");
});
