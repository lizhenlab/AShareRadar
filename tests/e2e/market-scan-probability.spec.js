import { expect, test } from "@playwright/test";

test("full-market Shadow probability stays auditable, gated, and rank preserving", async ({ page }, testInfo) => {
  const resultQueries = [];
  await routeProbabilityFixture(page, resultQueries);
  await page.goto("/");
  await page.locator('button[data-primary-view="market"]').click();
  await page.locator("#marketScanModeOfficial").check({ force: true });

  await expect(page.locator("#marketScanProbabilityHorizon5d")).toBeChecked();
  await expect(page.locator("#marketScanProbabilitySemantics")).toContainText("上涨概率是样本外校准估计，不参与生产评分或排名");
  await expect(page.locator("#marketScanProbabilityTarget")).toHaveText("未来所选周期净超额收益为正");
  await expect(page.locator("#marketScanProbabilityStatus")).toHaveText("样本外已校准 · Shadow");
  await expect(page.locator("#marketScanProbabilityBaseRate")).toHaveText("51.4%");
  await expect(page.locator("#marketScanRows .market-scan-probability").first()).toContainText("5日 61.2%");
  await expect(page.locator("#marketScanRows .market-scan-probability").first()).toContainText("95% CI 56.0%–66.0%");
  await expect(page.locator("#marketScanRows tr.market-scan-result-row > td").first()).toHaveText("7");
  await expect(page.locator("#marketScanProbabilityMin")).toBeEnabled();
  expect((await page.locator("#marketScanSort option").allTextContents()).join(" ")).not.toContain("上涨概率");

  await page.locator("#marketScanFilterToggle").click();
  await page.locator("#marketScanAdvancedFilters summary").click();
  await page.locator("#marketScanProbabilityMin").fill("60");
  await page.locator('#marketScanFilters button[type="submit"]').click();
  await expect.poll(() => resultQueries.at(-1)).toMatchObject({ probability_horizon: "5", min_upside_probability: "0.6" });
  await expect(page.locator("#marketScanRows tr.market-scan-result-row > td").first()).toHaveText("7");

  await page.locator("#marketScanProbabilityHorizon1d").check({ force: true });
  await expect(page.locator("#marketScanProbabilityStatus")).toHaveText("证据不足");
  await expect(page.locator("#marketScanProbabilityMin")).toBeDisabled();
  await expect(page.locator("#marketScanProbabilityMin")).toHaveValue("");
  await expect(page.locator("#marketScanRows .market-scan-probability").first()).toContainText("1日 · 证据不足");
  await expect(page.locator("#marketScanRows .market-scan-probability").first()).toContainText("不上屏概率数值");
  await expect(page.locator("#marketScanRows .market-scan-probability").first()).not.toContainText(/0\.0%|50\.0%/);
  await expect.poll(() => resultQueries.at(-1)).not.toHaveProperty("min_upside_probability");

  await page.locator("#marketScanProbabilityHorizon20d").check({ force: true });
  await expect(page.locator("#marketScanProbabilityBaseRate")).toHaveText("55.0%");
  await expect(page.locator("#marketScanRows .market-scan-probability").first()).toContainText("20日 70.0%");
  await expect(page.locator("#marketScanProbabilityMin")).toBeEnabled();
  await page.locator("#marketScanProbabilityMin").fill("69");
  await page.locator('#marketScanFilters button[type="submit"]').click();
  await expect.poll(() => resultQueries.at(-1)).toMatchObject({ probability_horizon: "20", min_upside_probability: "0.69" });
  await expect(page.locator("#marketScanRows tr.market-scan-result-row > td").first()).toHaveText("7");

  await page.locator("#marketScanRows [data-market-scan-snapshot-target]").first().click();
  const snapshot = page.locator("#marketScanRows .market-scan-probability-snapshot").first();
  await expect(snapshot).toBeVisible();
  await expect(snapshot).toContainText("1 日");
  await expect(snapshot).toContainText("5 日");
  await expect(snapshot).toContainText("20 日");
  await expect(snapshot).toContainText("probability-model-v1 / probability-features-v1");
  await expect(snapshot).toContainText("2026-07-15");
  await expect(snapshot).toContainText("训练 120 日 · 校准 40 日 · 测试 60 日 · 180000 条");
  await expect(snapshot).toContainText("Brier 0.1964 · BSS 0.127 · ECE 0.034 · AUC 0.681 · 分箱单调 是");
  await expect(snapshot).toContainText("仅用于 Shadow 研究，不参与生产排名");

  if (testInfo.project.use.isMobile) {
    const layout = await page.locator("#marketScanProbabilityResearch").evaluate((element) => ({
      right: element.getBoundingClientRect().right,
      viewport: document.documentElement.clientWidth,
      documentWidth: document.documentElement.scrollWidth,
      horizonHeights: Array.from(element.querySelectorAll(".market-scan-probability-horizons span"), (node) => node.getBoundingClientRect().height),
    }));
    const snapshotRight = await snapshot.evaluate((element) => element.getBoundingClientRect().right);
    expect(layout.right).toBeLessThanOrEqual(layout.viewport + 1);
    expect(snapshotRight).toBeLessThanOrEqual(layout.viewport + 1);
    expect(layout.documentWidth).toBeLessThanOrEqual(layout.viewport + 1);
    expect(Math.min(...layout.horizonHeights)).toBeGreaterThanOrEqual(44);
  }
});

test("snapshot publication failure keeps a passed score-distribution gate visibly separate", async ({ page }, testInfo) => {
  const failedRun = failedGateRun();
  await routeGateMessageFixture(page, failedRun);
  await page.goto("/");
  await page.locator('button[data-primary-view="market"]').click();

  await expect(page.locator("#marketScanDetails")).toBeHidden();
  await expect(page.locator("#marketScanGateSummary")).toBeVisible();
  await expect(page.locator("#marketScanHeadline")).toContainText("快照跨度 1918 秒");
  await expect(page.locator("#marketScanHeadline")).not.toContainText("评分分布门禁");
  await expect(page.locator("#marketScanPublicationBlockers")).toContainText("全市场报价快照跨度 1918 秒超过 1200 秒门槛");
  await expect(page.locator("#marketScanPassedGates")).toContainText("评分分布 · raw-score-distribution-v2");
  await expect(page.locator("#marketScanSourceWarnings")).toContainText("tencent 未覆盖");
  await expect(page.locator("#marketScanPassedGates")).toHaveClass(/passed/);

  if (testInfo.project.use.isMobile) {
    const layout = await page.locator("#marketScanPassedGates").evaluate((element) => ({
      columns: getComputedStyle(element).gridTemplateColumns.trim().split(/\s+/).length,
      right: element.getBoundingClientRect().right,
      viewport: document.documentElement.clientWidth,
    }));
    expect(layout.columns).toBe(1);
    expect(layout.right).toBeLessThanOrEqual(layout.viewport + 1);
  }
});

async function routeProbabilityFixture(page, resultQueries) {
  await page.route("**/api/**", async (route) => {
    const request = route.request();
    const url = new URL(request.url());
    if (url.pathname === "/api/stream/quotes") return route.abort();
    if (url.pathname === "/api/market-scans/latest") return fulfillJson(route, scanRun());
    if (url.pathname === "/api/market-scans/latest-published") return fulfillJson(route, scanRun());
    if (url.pathname === "/api/market-scans" && request.method() === "GET") {
      return fulfillJson(route, { items: [scanRun()], total: 1, page: 1, page_size: 30, page_count: 1 });
    }
    if (url.pathname === "/api/market-scans/42/results") {
      const query = Object.fromEntries(url.searchParams);
      resultQueries.push(query);
      const item = scanItem();
      const horizon = query.probability_horizon || "5";
      const minimum = Number(query.min_upside_probability ?? 0);
      const record = item.upside_probabilities[horizon]?.net_excess_positive;
      const items = query.min_upside_probability && Number(record?.probability) < minimum ? [] : [item];
      return fulfillJson(route, resultPage(items, Number(query.page_size) || 100));
    }
    return fulfillJson(route, fallbackPayload(url));
  });
}

async function routeGateMessageFixture(page, run) {
  await page.route("**/api/**", async (route) => {
    const request = route.request();
    const url = new URL(request.url());
    if (url.pathname === "/api/stream/quotes") return route.abort();
    if (url.pathname === "/api/market-scans/latest") return fulfillJson(route, run);
    if (url.pathname === "/api/market-scans/latest-published") return fulfillJson(route, null);
    if (url.pathname === "/api/market-scans" && request.method() === "GET") {
      return fulfillJson(route, { items: [run], total: 1, page: 1, page_size: 30, page_count: 1 });
    }
    return fulfillJson(route, fallbackPayload(url));
  });
}

function resultPage(items, pageSize) {
  return {
    run: scanRun(), items, total: items.length, page: 1, page_size: pageSize,
    page_count: items.length ? 1 : 0, probability_research: probabilityResearch(),
  };
}

function probabilityResearch() {
  const common = {
    target: "net_excess_positive", model_version: "probability-model-v1",
    feature_version: "probability-features-v1", label_version: "probability-label-v1",
    cost_model_version: "probability-cost-v1", limitations: ["仅用于 Shadow 研究，不参与生产排名"],
  };
  return {
    schema_version: "market-scan-probability-artifact-v1", run_id: 42,
    status: "calibrated_shadow", default_horizon: 5, primary_target: "net_excess_positive",
    production_ranking_effect: "none", horizons: {
      "1": { net_excess_positive: { ...common, status: "insufficient_data", horizon: 1, base_rate: 0.49, training_cutoff: "2026-07-15" } },
      "5": { net_excess_positive: {
        ...common, status: "calibrated_shadow", horizon: 5, base_rate: 0.514, training_cutoff: "2026-07-15",
        counts: { training_session_count: 120, calibration_session_count: 40, test_session_count: 60, observation_count: 180000 },
        calibration_metrics: { calibrated: { brier_score: 0.1964, brier_skill_score: 0.127, ece: 0.034, auc: 0.681, bin_monotonic: true } },
      } },
      "20": { net_excess_positive: { ...common, status: "calibrated_shadow", horizon: 20, base_rate: 0.55, training_cutoff: "2026-07-11" } },
    },
  };
}

function scanItem() {
  const record = (status, probability, lower, upper, baseRate) => ({
    status, probability, confidence_interval: probability === null ? null : { lower, upper, level: 0.95 }, base_rate: baseRate,
  });
  return {
    run_id: 42, symbol: "600519.SH", code: "600519", market: "SH", name: "贵州茅台", industry: "白酒",
    list_date: "2001-08-27", metadata_source: "fixture", is_st: false, is_new: false, status: "success",
    rank: 7, score: 92, raw_score: 91.5, trend_score: 88, leader_score: 80, data_quality_score: 96,
    price: 1500, change_pct: 1.2, turnover_rate: 0.8, volume_ratio: 1.1, amount: 2000000000,
    tags: ["趋势向上"], metrics: {}, score_details: {}, reason: "冻结生产排名 7", error: null,
    data_date: "2026-07-29", quote_timestamp: "2026-07-29 15:00:00", quote_source: "fixture",
    kline_source: "fixture", adjustment_mode: "qfq", quote_fallback_used: false, kline_fallback_used: false,
    metadata_degraded: false, degradation_reasons: [], updated_at: "2026-07-29 16:00:00",
    upside_probabilities: {
      "1": { net_excess_positive: record("insufficient_data", null, null, null, 0.49) },
      "5": { net_excess_positive: record("calibrated_shadow", 0.612, 0.56, 0.66, 0.514) },
      "20": { net_excess_positive: record("calibrated_shadow", 0.70, 0.65, 0.75, 0.55) },
    },
  };
}

function scanRun() {
  return {
    id: 42, status: "success", trigger: "manual", mode: "official", rule_version: "full-market-score-v4",
    as_of: "2026-07-29 16:00:00", data_date: "2026-07-29", quote_date: "2026-07-29",
    scope: "SH/SZ/BJ listed A-shares", total_count: 1, excluded_count: 0, processed_count: 1,
    success_count: 1, missing_count: 0, skipped_count: 0, retry_count: 0, progress_pct: 100,
    coverage_pct: 100, created_at: "2026-07-29 16:00:00", updated_at: "2026-07-29 16:01:00",
    started_at: "2026-07-29 16:00:01", finished_at: "2026-07-29 16:01:00", duration_ms: 59000,
    message: "扫描完成", stock_pool_source: "fixture", task_run_id: null, retry_of_run_id: null,
  };
}

function failedGateRun() {
  const audit = "评分分布门禁 raw-score-distribution-v2：raw_score样本 5499/5499，distinct ratio 99.65%，最大并列组 2/5499（0.04%），0/100饱和 0/5499（0.00%），前100并列 0/100（0.00%），最大组 1";
  return {
    ...scanRun(), id: 67, status: "failed", processed_count: 5542, total_count: 5542,
    success_count: 5499, skipped_count: 43, coverage_pct: 99.22,
    message: `盘后正式扫描未达到发布可信度：发布阻断：全市场报价快照跨度 1918 秒超过 1200 秒门槛；已通过：${audit}`,
    last_error: "批量行情缺失 1 只：tencent 未覆盖；akshare 最近失败，短暂冷却中；全市场报价快照跨度 1918 秒超过 1200 秒门槛；逐股结果含缺失 0、跳过 43",
  };
}

function fallbackPayload(url) {
  if (url.pathname === "/api/discovery/presets") return { items: [], total: 0, page: 1, page_size: 100, page_count: 0 };
  if (url.pathname === "/api/market") return { indices: [] };
  if (url.pathname === "/api/strong-stocks") return { items: [] };
  if (url.pathname === "/api/data/status") return { providers: [], source_plan: {}, cache: {}, capabilities: [], capability_statuses: [] };
  if (url.pathname === "/api/tasks/status") return { enabled: false, running: false, tasks: [] };
  if (url.pathname === "/api/stock/workbench") return { analysis: { quote: { code: "600519", market: "SH", name: "贵州茅台", price: 1, change: 0, change_pct: 0, source: "fixture", timestamp: "2026-07-29 10:00:00" }, data_quality: { level: "优秀", score: 95 }, signal_snapshot: {}, action_advice: {}, review: {}, klines: [] }, insights: { overview: {} }, local_data_warnings: [], chart_marks: { marks: [], categories: [] } };
  if (["/api/tasks/runs", "/api/monitor/events", "/api/watchlist", "/api/advice/timeline", "/api/plates"].includes(url.pathname)) return [];
  return [];
}

async function fulfillJson(route, payload) {
  await route.fulfill({ status: 200, contentType: "application/json", body: JSON.stringify(payload) });
}
