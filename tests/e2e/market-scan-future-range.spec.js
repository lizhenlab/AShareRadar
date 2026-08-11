import { expect, test } from "@playwright/test";

test("future-range research stays independent, auditable, paged, and mobile safe", async ({ page }, testInfo) => {
  const queries = [];
  await routeFutureRangeFixture(page, queries, "ready");
  await page.goto("/");
  await page.locator('button[data-primary-view="market"]').click();

  const panel = page.locator("#marketScanFutureRangeResearch");
  await expect(panel).not.toHaveAttribute("open", "");
  await panel.locator("summary").click();
  await expect(panel).toHaveAttribute("open", "");
  await expect(page.locator("#marketScanFutureRangeSummaryStatus")).toHaveText("冻结证据可用");
  await expect(page.locator("#marketScanFutureRangeEvidenceCount")).toHaveText("60 / 6000");
  await expect(page.locator("#marketScanFutureRangeContext")).toContainText("HLC3 典型价代理 · 非 VWAP");
  await expect(page.locator("#marketScanFutureRangeMetrics")).toContainText("典型价变化");
  await expect(page.locator("#marketScanFutureRangeMetrics")).toContainText("+1.30%");
  await expect(page.locator("#marketScanFutureRangeMetrics")).toContainText("A股 T+1 不可执行");
  await expect(page.locator("#marketScanFutureRangeProbability")).toContainText("不上屏 0 或 50% 占位值");
  await expect(page.locator("#marketScanFutureRangeDetails")).toContainText("贵州茅台 · 600519.SH");
  expect(queries.at(-1)).toMatchObject({ page: "1", page_size: "20", session_offset: "1", include_research: "true" });

  await page.locator('input[name="marketScanFutureRangePath"][value="cumulative_path"]').check({ force: true });
  await expect(page.locator("#marketScanFutureRangeMetrics")).toContainText("累计 MAE");
  await page.locator("#marketScanFutureRangeDetails details").first().click();
  await expect(page.locator("#marketScanFutureRangeDetails")).toContainText("终值收盘");
  await expect(page.locator("#marketScanFutureRangeDetails")).toContainText("D+1 仅区间诊断，不作为可实现收益");

  await page.locator('input[name="marketScanFutureRangeOffset"][value="2"]').check({ force: true });
  await expect.poll(() => queries.at(-1)).toMatchObject({ page: "1", session_offset: "2", include_research: "false" });
  await expect(page.locator("#marketScanFutureRangeDetailsHelp")).toContainText("目标 D+2");
  await expect(page.locator("#marketScanFutureRangeMetrics")).toContainText("可执行净收益");
  await expect(page.locator("#marketScanFutureRangeMetrics")).toContainText("+1.40%");
  await page.locator("#marketScanFutureRangeDetails details").first().click();
  await expect(page.locator("#marketScanFutureRangeDetails")).toContainText("净超额收益");
  await expect(page.locator("#marketScanFutureRangeDetails")).toContainText("base · future-range-cost-v1");
  await page.locator("#marketScanFutureRangeNext").click();
  await expect.poll(() => queries.at(-1)).toMatchObject({ page: "2", session_offset: "2" });
  await expect(page.locator("#marketScanFutureRangePageText")).toContainText("第 2/2 页");

  await page.locator('input[name="marketScanFutureRangeOffset"][value="3"]').check({ force: true });
  await expect.poll(() => queries.at(-1)).toMatchObject({ page: "1", session_offset: "3", include_research: "false" });
  await expect(page.locator("#marketScanFutureRangeDetailsHelp")).toContainText("目标 D+3");
  await expect(page.locator("#marketScanFutureRangeMetrics")).toContainText("可执行净超额");
  await page.locator("#marketScanFutureRangeDetails details").first().click();
  await expect(page.locator("#marketScanFutureRangeDetails")).toContainText("市场基准净收益");

  await page.locator("#marketScanFutureRangeGroup").selectOption("top20");
  await expect(page.locator("#marketScanFutureRangeEvidenceCount")).toHaveText("60 / 1200");
  await expect(page.locator("#marketScanFutureRangeGroups tr.selected")).toContainText("Top20");
  await expect(page.locator("#marketScanRows tr.market-scan-result-row > td").first()).toHaveText("1");

  if (testInfo.project.use.isMobile) {
    const layout = await panel.evaluate((element) => ({
      right: element.getBoundingClientRect().right,
      viewport: document.documentElement.clientWidth,
      documentWidth: document.documentElement.scrollWidth,
      controls: Array.from(element.querySelectorAll(".market-scan-future-range-controls fieldset span"), (node) => node.getBoundingClientRect().height),
    }));
    expect(layout.right).toBeLessThanOrEqual(layout.viewport + 1);
    expect(layout.documentWidth).toBeLessThanOrEqual(layout.viewport + 1);
    expect(Math.min(...layout.controls)).toBeGreaterThanOrEqual(44);
  }
});

test("legacy runs degrade to not-generated without fabricated evidence", async ({ page }) => {
  await routeFutureRangeFixture(page, [], "not_generated");
  await page.goto("/");
  await page.locator('button[data-primary-view="market"]').click();
  await page.locator("#marketScanFutureRangeResearch > summary").click();
  await expect(page.locator("#marketScanFutureRangeSummaryStatus")).toHaveText("尚未生成");
  await expect(page.locator("#marketScanFutureRangeState")).toContainText("不显示 0 或 50% 占位值");
  await expect(page.locator("#marketScanFutureRangeContent")).toBeHidden();
  await expect(page.locator("#marketScanFutureRangeState")).not.toContainText(/0\.0%|50\.0%/);
});

async function routeFutureRangeFixture(page, queries, status) {
  await page.route("**/api/**", async (route) => {
    const request = route.request();
    const url = new URL(request.url());
    if (url.pathname === "/api/stream/quotes") return route.abort();
    if (url.pathname === "/api/market-scans/latest") return fulfillJson(route, scanRun());
    if (url.pathname === "/api/market-scans/latest-published") return fulfillJson(route, scanRun());
    if (url.pathname === "/api/market-scans" && request.method() === "GET") {
      return fulfillJson(route, { items: [scanRun()], total: 1, page: 1, page_size: 30, page_count: 1 });
    }
    if (url.pathname === "/api/market-scans/29/results") return fulfillJson(route, resultPage());
    if (url.pathname === "/api/market-scans/29/future-range-research") {
      const query = Object.fromEntries(url.searchParams);
      queries.push(query);
      return fulfillJson(route, status === "ready" ? futureRangeResponse(query) : notGeneratedResponse(query));
    }
    return fulfillJson(route, fallbackPayload(url));
  });
}

function futureRangeResponse(query) {
  const offset = Number(query.session_offset || 1);
  const page = Number(query.page || 1);
  return {
    schema_version: "market-scan-future-range-api-v1", generation_status: "ready",
    artifact: { schema_version: "market-scan-future-range-artifact-v1", generated_at: "2026-08-11T10:00:00+08:00", integrity_digest: "a".repeat(64) },
    research: query.include_research === "false" ? null : futureRangeResearch(),
    record_page: { page, page_size: 20, total: 21, page_count: 2, session_offset: offset, symbol: null, items: [futureRecord(offset, page)] },
  };
}

function futureRangeResearch() {
  const group = (sessionOffset, groupType, groupValue, sampleSize, center) => ({
    cohort: { mode: "official", scope: "SH/SZ/BJ", rule_version: "full-market-score-v4" },
    group_type: groupType, group_value: groupValue, session_offset: sessionOffset, status: "ok",
    sample_size: sampleSize, independent_session_count: 60,
    metrics: {
      level_shift_low: metric(center - 0.01), level_shift_hlc3_proxy: metric(center), level_shift_high: metric(center + 0.01),
      mae: metric(-0.008), mfe: metric(0.02), terminal_close_return: metric(0.016),
      net_return: sessionOffset === 1 ? unavailableMetric() : metric(0.014),
      net_excess_return: sessionOffset === 1 ? unavailableMetric() : metric(0.003),
    },
  });
  return {
    report_contract_version: "market-scan-future-range-report-v1", status: "ok", generated_at: "2026-08-11T10:00:00+08:00",
    run: { run_id: 29, mode: "official", data_date: "2026-07-31" },
    config: { session_offsets: [1, 2, 3], center_proxy: "HLC3_proxy_not_VWAP" },
    source: { read_only: true, adjustment_mode: "qfq" }, record_count: 6000,
    groups: [1, 2, 3].flatMap((sessionOffset) => [
      group(sessionOffset, "top_n", "20", 1200, 0.018), group(sessionOffset, "top_n", "50", 3000, 0.015),
      group(sessionOffset, "top_n", "100", 6000, 0.013), group(sessionOffset, "all", null, 320000, 0.004),
      group(sessionOffset, "decile", "Q1", 32000, -0.004), group(sessionOffset, "decile", "Q10", 32000, 0.011),
    ]),
    rank_ic: [1, 2, 3].map((sessionOffset) => ({ session_offset: sessionOffset, metric: "level_shift_hlc3_proxy", status: "ok", independent_session_count: 60, mean_rank_ic: 0.042, ci95: [0.018, 0.066] })),
    monotonicity: [1, 2, 3].map((sessionOffset) => ({ session_offset: sessionOffset, metric: "level_shift_hlc3_proxy", status: "ok", independent_session_count: 60, spearman: 0.94, passed: true })),
    probability_context: { status: "not_available", source: "persisted_oos_calibrated_shadow_only", limitations: ["artifact_not_supplied"] },
    limitations: ["official_only", "high_low_not_executable_return"],
  };
}

function metric(value) {
  return { status: "ok", mean: value + 0.001, median: value, positive_rate: 0.58, ci95: [value - 0.003, value + 0.003] };
}

function unavailableMetric() { return { status: "insufficient_data", mean: null, median: null, positive_rate: null, ci95: null }; }

function futureRecord(offset, page) {
  return {
    run_id: 29, symbol: page === 1 ? "600519.SH" : "000001.SZ", name: page === 1 ? "贵州茅台" : "平安银行",
    rank: page, trend_score: 94 - page, d_bar: { date: "2026-07-31", hlc3_proxy: 1406.66 },
    probability: { status: "not_available", predictions: [] },
    offsets: [{ session_offset: offset, target_session_date: `2026-08-0${offset + 2}`, fixed_session_status: "available",
      level_shift: { low: 0.0079, hlc3_proxy: 0.0132, high: 0.014 },
      d1_open_reference: { entry_date: "2026-08-03", entry_price: 1412,
        specified_day: { low: -0.0078, hlc3_proxy: 0.0094, high: 0.0198, close: 0.0163 },
        cumulative_path: { mae: -0.0078, mfe: 0.0198, terminal_close_return: 0.0163 } },
      interval_structure: { normalized_width: 0.027, overlap_ratio: 0.487 },
      execution: offset === 1
        ? { status: "data_unavailable", reason: "A_share_T_plus_1_no_same_session_exit", gross_return: null, cost_drag: null, net_return: null, market_benchmark_net_return: null, net_excess_return: null }
        : { status: "modelled", reason: null, entry_date: "2026-08-03", exit_date: `2026-08-0${offset + 2}`, gross_return: 0.018, cost_drag: 0.002, net_return: 0.016, market_benchmark_net_return: 0.012, net_excess_return: 0.004, cost_profile_id: "base", cost_model_version: "future-range-cost-v1" } }],
  };
}

function notGeneratedResponse(query) {
  return {
    schema_version: "market-scan-future-range-api-v1", generation_status: "not_generated", artifact: null, research: null,
    record_page: { page: 1, page_size: 20, total: 0, page_count: 0, session_offset: Number(query.session_offset || 1), symbol: null, items: [] },
  };
}

function resultPage() {
  return { run: scanRun(), items: [scanItem()], total: 1, page: 1, page_size: 100, page_count: 1, probability_research: null };
}

function scanRun() {
  return {
    id: 29, status: "success", trigger: "manual", mode: "official", rule_version: "full-market-score-v4",
    as_of: "2026-07-31 16:00:00", data_date: "2026-07-31", quote_date: "2026-07-31", scope: "SH/SZ/BJ listed A-shares",
    total_count: 1, excluded_count: 0, processed_count: 1, success_count: 1, missing_count: 0, skipped_count: 0, retry_count: 0,
    progress_pct: 100, coverage_pct: 100, created_at: "2026-07-31 16:00:00", updated_at: "2026-07-31 16:01:00",
    started_at: "2026-07-31 16:00:01", finished_at: "2026-07-31 16:01:00", duration_ms: 59000,
    message: "扫描完成", stock_pool_source: "fixture", task_run_id: null, retry_of_run_id: null,
  };
}

function scanItem() {
  return {
    run_id: 29, symbol: "600519.SH", code: "600519", market: "SH", name: "贵州茅台", industry: "白酒", list_date: "2001-08-27",
    metadata_source: "fixture", is_st: false, is_new: false, status: "success", rank: 1, score: 92, raw_score: 91.5,
    trend_score: 88, leader_score: 80, data_quality_score: 96, price: 1500, change_pct: 1.2, turnover_rate: 0.8,
    volume_ratio: 1.1, amount: 2000000000, tags: ["趋势向上"], metrics: {}, score_details: {}, reason: "冻结生产排名 1", error: null,
    data_date: "2026-07-31", quote_timestamp: "2026-07-31 15:00:00", quote_source: "fixture", kline_source: "fixture",
    adjustment_mode: "qfq", quote_fallback_used: false, kline_fallback_used: false, metadata_degraded: false,
    degradation_reasons: [], updated_at: "2026-07-31 16:00:00", upside_probabilities: {},
  };
}

function fallbackPayload(url) {
  if (url.pathname === "/api/discovery/presets") return { items: [], total: 0, page: 1, page_size: 100, page_count: 0 };
  if (url.pathname === "/api/market") return { indices: [] };
  if (url.pathname === "/api/strong-stocks") return { items: [] };
  if (url.pathname === "/api/data/status") return { providers: [], source_plan: {}, cache: {}, capabilities: [], capability_statuses: [] };
  if (url.pathname === "/api/tasks/status") return { enabled: false, running: false, tasks: [] };
  if (url.pathname === "/api/stock/workbench") return { analysis: { quote: { code: "600519", market: "SH", name: "贵州茅台", price: 1, change: 0, change_pct: 0, source: "fixture", timestamp: "2026-07-31 10:00:00" }, data_quality: { level: "优秀", score: 95 }, signal_snapshot: {}, action_advice: {}, review: {}, klines: [] }, insights: { overview: {} }, local_data_warnings: [], chart_marks: { marks: [], categories: [] } };
  if (["/api/tasks/runs", "/api/monitor/events", "/api/watchlist", "/api/advice/timeline", "/api/plates"].includes(url.pathname)) return [];
  return [];
}

async function fulfillJson(route, payload) {
  await route.fulfill({ status: 200, contentType: "application/json", body: JSON.stringify(payload) });
}
