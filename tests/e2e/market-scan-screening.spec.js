import { expect, test } from "@playwright/test";
import { marketScanPollingIdentityPayload } from "./frontend-flow-api-fixtures.mjs";

test.beforeEach(async ({ page }) => {
  await page.clock.setFixedTime("2026-08-11T15:30:00Z");
});

test("trusted screening stays lazy, auditable, responsive, and column configurable", async ({ page }, testInfo) => {
  const calls = [];
  await routeScreeningFixture(page, calls);
  await page.goto("/");
  await page.locator('button[data-primary-view="market"]').click();

  await expect(page.locator("#marketScanScreeningWorkbench")).not.toHaveAttribute("open", "");
  await expect.poll(() => calls.filter((value) => /breadth|screen\/evaluate|delta/.test(value)).length).toBe(0);

  await page.locator("#marketScanScreeningWorkbench > summary").click();
  await expect(page.locator("#marketScanScreeningSummaryStatus")).toContainText("命中 1/2");
  await expect(page.locator("#marketScanScreeningEvidence")).toContainText("摘要 aaaaaaaaaaaa");
  await expect(page.locator("#marketScanScreeningBreadth")).toContainText("分数覆盖");
  await expect(page.locator("#marketScanScreeningBreadth")).toContainText("1/2");
  await expect(page.locator("#marketScanScreeningBreadth")).toContainText("--");
  await expect(page.locator("#marketScanScreeningEvaluation")).toContainText("当前页入选理由");
  await expect(page.locator("#marketScanScreeningEvaluation")).toContainText("证据缺失");
  await expect(page.locator("#marketScanScreeningDiff")).toContainText("Top100 新进入");
  await expect.poll(() => calls.filter((value) => /breadth|screen\/evaluate|delta/.test(value)).length).toBe(3);

  await page.locator("#marketScanTableWrap").evaluate((element) => { element.dataset.marketScanRunId = ""; });
  await expect(page.locator("#marketScanScreeningSummaryStatus")).toContainText("暂无冻结榜单");
  await expect(page.locator("#marketScanScreeningEvidence")).toContainText("尚未绑定冻结批次");
  await expect(page.locator("#marketScanScreeningSpec")).toBeEmpty();
  await page.locator("#marketScanTableWrap").evaluate((element) => { element.dataset.marketScanRunId = "42"; });
  await expect.poll(() => calls.filter((value) => /breadth|screen\/evaluate|delta/.test(value)).length).toBe(6);

  await page.locator('label:has(#marketScanColumnLiquidity)').click();
  await expect(page.locator("#marketScanTable")).toHaveAttribute("data-column-view", "liquidity");
  await expect(page.locator("#marketScanTableWrap")).toHaveAttribute("aria-label", /流动性列视图/);
  await expect(page.locator("#marketScanTable > thead > tr > th:nth-child(4)")).toBeHidden();
  await expect(page.locator("#marketScanTable > thead > tr > th:nth-child(7)")).toBeVisible();
  await page.locator("#marketScanColumnResearch").focus();
  await page.keyboard.press("Space");
  await expect(page.locator("#marketScanTable")).toHaveAttribute("data-column-view", "research");
  await expect(page.locator("#marketScanTable > thead > tr > th:nth-child(7)")).toBeHidden();

  if (testInfo.project.name.includes("mobile")) {
    const layout = await page.locator("#marketScanScreeningWorkbench").evaluate((element) => ({
      right: element.getBoundingClientRect().right,
      viewport: document.documentElement.clientWidth,
      documentWidth: document.documentElement.scrollWidth,
      controls: Array.from(document.querySelectorAll("#marketScanColumnViews span"), (node) => node.getBoundingClientRect().height),
    }));
    expect(layout.right).toBeLessThanOrEqual(layout.viewport + 1);
    expect(layout.documentWidth).toBeLessThanOrEqual(layout.viewport + 1);
    expect(Math.min(...layout.controls)).toBeGreaterThanOrEqual(44);
  }
});

async function routeScreeningFixture(page, calls) {
  await page.route("**/api/**", async (route) => {
    const request = route.request();
    const url = new URL(request.url());
    calls.push(`${request.method()} ${url.pathname}`);
    if (url.pathname === "/api/stream/quotes") return route.abort();
    if (url.pathname === "/api/market-scans/polling-identity") return fulfill(route, marketScanPollingIdentityPayload(scanRun(), scanRun()));
    if (["/api/market-scans/latest", "/api/market-scans/latest-published"].includes(url.pathname)) return fulfill(route, scanRun());
    if (url.pathname === "/api/market-scans" && request.method() === "GET") return fulfill(route, { items: [scanRun()], total: 1, page: 1, page_size: 30, page_count: 1 });
    if (url.pathname === "/api/market-scans/42/results") return fulfill(route, resultPage());
    if (url.pathname === "/api/market-scans/42/breadth") return fulfill(route, breadth());
    if (url.pathname === "/api/market-scans/42/screen/evaluate") return fulfill(route, evaluation());
    if (url.pathname === "/api/market-scans/42/delta") return fulfill(route, delta());
    return fulfill(route, fallback(url));
  });
}

function scanRun() {
  return {
    id: 42, status: "success", trigger: "manual", mode: "official", rule_version: "full-market-score-v4",
    as_of: "2026-08-11 16:00:00", data_date: "2026-08-11", quote_date: "2026-08-11",
    scope: "沪市 + 深市 + 北交所当前上市A股", total_count: 2, excluded_count: 0, processed_count: 2,
    success_count: 2, missing_count: 0, skipped_count: 0, retry_count: 0, progress_pct: 100,
    coverage_pct: 100, created_at: "2026-08-11 16:00:00", updated_at: "2026-08-11 16:01:00",
    started_at: "2026-08-11 16:00:01", finished_at: "2026-08-11 16:01:00", duration_ms: 59000,
    message: "扫描完成", stock_pool_source: "fixture", task_run_id: null, retry_of_run_id: null,
    snapshot_digest: "a".repeat(64), snapshot_seal_origin: "publication", snapshot_sealed_at: "2026-08-11 16:01:00",
  };
}

function item(symbol = "600519.SH", name = "贵州茅台", rank = 1, score = 92) {
  const [code, market] = symbol.split(".");
  return {
    run_id: 42, symbol, code, market, name, industry: "白酒", list_date: "2001-08-27", metadata_source: "fixture",
    is_st: false, is_new: false, status: "success", rank, score, raw_score: score, trend_score: 88, leader_score: 80,
    data_quality_score: 96, price: 1500, change_pct: 1.2, turnover_rate: 0.8, volume_ratio: 1.1, amount: 2000000000,
    tags: ["趋势向上"], metrics: {}, score_details: {}, reason: "冻结生产排名", error: null,
    data_date: "2026-08-11", quote_timestamp: "2026-08-11 15:00:00", quote_observed_at: "2026-08-11 15:00:01",
    quote_source: "fixture", kline_source: "fixture",
    adjustment_mode: "qfq", quote_fallback_used: false, kline_fallback_used: false, metadata_degraded: false,
    degradation_reasons: [], updated_at: "2026-08-11 16:00:00", upside_probabilities: {},
  };
}

function resultPage() {
  return { run: scanRun(), items: [item()], total: 1, page: 1, page_size: 100, page_count: 1, probability_research: null };
}

function evidence() {
  return { run_id: 42, status: "success", mode: "official", scope: "沪市 + 深市 + 北交所当前上市A股", data_date: "2026-08-11", quote_date: "2026-08-11", rule_version: "full-market-score-v4", finished_at: "2026-08-11 16:01:00", snapshot_digest: "a".repeat(64), snapshot_seal_origin: "publication", snapshot_sealed_at: "2026-08-11 16:01:00" };
}

function breadth() {
  return {
    schema_version: "market-scan-breadth-v1", evidence: evidence(),
    population: { total: 2, by_status: { success: 2 }, by_market: { SH: 2 } },
    score: { present_count: 1, missing_count: 1, min: 92, max: 92, mean: 92, percentiles: { p10: 92, p25: 92, p50: 92, p75: 92, p90: 92 }, bins: Array.from({ length: 10 }, (_, index) => ({ lower: index * 10, upper: (index + 1) * 10, count: index === 9 ? 1 : 0 })) },
    change: { advancing: 1, flat: 0, declining: 0, missing: 1 }, industries: [
      { industry: "白酒", count: 1, score_present_count: 1, average_score: 92 },
      { industry: null, count: 1, score_present_count: 0, average_score: null },
    ],
    canonical_digest: "a".repeat(64),
  };
}

function spec() {
  return { schema_version: "screen-spec-v2", status: "success", markets: [], industries: [], is_st: null, is_new: null, ranges: { score: { min: 80 } }, keyword: null, sort: [{ field: "rank", order: "asc" }] };
}

function evaluation() {
  return {
    schema_version: "market-scan-screen-evaluation-v1", evidence: evidence(), spec: spec(), spec_digest: "b".repeat(64),
    population_count: 2, matched_count: 1,
    funnel: [
      { index: 1, condition_code: "status", label: "结果状态", input_count: 2, matched_count: 2, excluded_count: 0, missing_count: 0 },
      { index: 2, condition_code: "range.score", label: "趋势强度 ≥ 80", input_count: 2, matched_count: 1, excluded_count: 1, missing_count: 1 },
    ],
    exclusion_reasons: [{ code: "range.score", label: "趋势强度缺失", count: 1, missing_count: 1 }],
    matched: { items: [item()], total: 1, page: 1, page_size: 100, page_count: 1 },
    matched_explanations: [{ symbol: "600519.SH", passed_conditions: ["status", "range.score"] }],
    near_misses: [{ item: item("600000.SH", "浦发银行", 2, null), failed_conditions: [{ code: "range.score.missing", label: "趋势强度缺失", missing: true }] }],
    canonical_digest: "c".repeat(64),
  };
}

function delta() {
  const entrant = { symbol: "600519.SH", name: "贵州茅台", market: "SH", industry: "白酒", previous_rank: 110, current_rank: 1, previous_raw_score: 70, current_raw_score: 92, reason_codes: ["crossed_into_top_n"] };
  const current = { run_id: 42, status: "success", mode: "official", scope: "沪市 + 深市 + 北交所当前上市A股", rule_version: "full-market-score-v4", data_date: "2026-08-11", finished_at: "2026-08-11 16:01:00", snapshot_digest: "a".repeat(64), snapshot_seal_origin: "publication", snapshot_sealed_at: "2026-08-11 16:01:00" };
  const previous = { ...current, run_id: 41, data_date: "2026-08-10", finished_at: "2026-08-10 16:01:00", snapshot_digest: "b".repeat(64), snapshot_sealed_at: "2026-08-10 16:01:00" };
  return {
    schema_version: "market-scan-delta-v1", status: "ready", unavailable_reason: null,
    current, previous, cohort: { mode: "official", scope: current.scope, rule_version: current.rule_version },
    summary: { previous_present_count: 120, current_present_count: 121, compared_symbol_count: 120, evidence_detail_scope: "top100_union", evidence_change_reason_counts: [] },
    top_buckets: [20, 50, 100].map((top_n) => ({ top_n, previous_count: top_n - 1, current_count: top_n, retained_count: top_n - 1, entrants: [entrant], exits: [], present_but_unrankable: [] })),
    rank_score_changes: [], exposure_changes: [], evidence_changes: [], canonical_digest: "d".repeat(64),
  };
}

function fallback(url) {
  if (url.pathname === "/api/discovery/presets") return { items: [], total: 0, page: 1, page_size: 100, page_count: 0 };
  if (url.pathname === "/api/market") return { indices: [] };
  if (url.pathname === "/api/strong-stocks") return { items: [] };
  if (url.pathname === "/api/data/status") return { providers: [], source_plan: {}, cache: {}, capabilities: [], capability_statuses: [] };
  if (url.pathname === "/api/tasks/status") return { enabled: false, running: false, tasks: [] };
  if (["/api/tasks/runs", "/api/monitor/events", "/api/watchlist", "/api/advice/timeline", "/api/plates"].includes(url.pathname)) return [];
  return [];
}

async function fulfill(route, payload) {
  await route.fulfill({ status: 200, contentType: "application/json", body: JSON.stringify(payload) });
}
