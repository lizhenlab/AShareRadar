import { expect, test } from "@playwright/test";
import { marketScanPollingIdentityPayload, mockApi, selectPrimaryView } from "./frontend-flow-api-fixtures.mjs";

test("collecting all filtered stocks keeps its pagination when the viewport becomes narrow", async ({ page }) => {
  await page.setViewportSize({ width: 1200, height: 900 });
  const applications = [];
  const writes = [];
  let releaseCollection;
  const pending = new Promise(resolve => { releaseCollection = resolve; });
  await mockApi(page, { async api(url, request) {
    const path = url.pathname;
    if (path === "/api/market-scans/polling-identity") return { payload: marketScanPollingIdentityPayload(run(), run(), "official") };
    if (path.startsWith("/api/market-scans/latest")) return { payload: run() };
    if (path === "/api/market-scans/42/results") return { payload: {
      run: run(), items: [], total: 0, page: 1, page_size: Number(url.searchParams.get("page_size")), page_count: 0,
    } };
    if (path === "/api/discovery/presets") return { payload: {
      items: [preset()], total: 1, page: 1, page_size: 100, page_count: 1,
    } };
    if (path.endsWith("/apply")) {
      const body = request.postDataJSON();
      applications.push(body);
      if (applications.length === 2) await pending;
      return { payload: leaderboard(body) };
    }
    if (path.endsWith("/rank-changes")) return { payload: {
      current_run_id: 42, previous_run_id: null, comparable: false, reason: "no_previous_run",
      current_rule_version: "bulk-test-v1", previous_rule_version: null,
      items: [], total: 0, page: 1, page_size: 200, page_count: 0,
    } };
    if (path.endsWith("/research-queue")) {
      const body = request.postDataJSON();
      writes.push(body);
      return { payload: { added_count: body.symbols.length, existing_count: 0,
        items: body.symbols.map(symbol => ({ symbol, source_run_id: body.run_id, source_preset_id: 7,
          source_preset_revision: body.expected_preset_revision, source_preset_name: preset().name,
          enqueued_at: "2026-09-10T10:00:00+08:00", added: true })) } };
    }
    return null;
  } });
  await page.goto("/");
  await selectPrimaryView(page, "market");
  await page.locator("#marketScanModeOfficial").check();
  await page.locator("#discoveryPresetSelect").selectOption("7");
  await page.locator("#discoveryPresetApply").click();
  await expect(page.locator("#discoveryPresetFeedback")).toContainText("已应用");
  expect(applications).toHaveLength(1);
  expect(applications[0].page_size).toBe(100);
  try {
    await page.locator("#discoveryEnqueueAll").click();
    await expect.poll(() => applications.length).toBe(2);
    await page.setViewportSize({ width: 700, height: 900 });
    expect(await page.evaluate(() => matchMedia("(max-width: 820px)").matches)).toBe(true);
  } finally {
    releaseCollection();
  }
  await expect(page.locator("#discoveryPresetFeedback")).toContainText("当前筛选结果处理完成：新增 205");
  expect(applications.slice(1).map(body => [body.page, body.page_size])).toEqual([[1, 100], [2, 100], [3, 100]]);
  expect(writes.map(body => body.symbols.length)).toEqual([100, 100, 5]);
  expect(writes.flatMap(body => body.symbols)).toEqual(Array.from({ length: 205 }, (_, i) => item(i + 1).symbol));
  expect(writes.every(body => body.run_id === 42 && body.expected_preset_revision === 3)).toBe(true);
});

function leaderboard(body) {
  const offset = (body.page - 1) * body.page_size;
  return { preset: preset(), run_id: 42, rule_version: "bulk-test-v1", total: 205,
    page: body.page, page_size: body.page_size, page_count: Math.ceil(205 / body.page_size),
    items: Array.from({ length: Math.max(0, Math.min(body.page_size, 205 - offset)) }, (_, i) => item(offset + i + 1)) };
}

function item(position) {
  const code = String(position).padStart(6, "0");
  return { position, source_rank: position, symbol: `${code}.SH`, code, market: "SH", name: `样本${code}`,
    industry: "半导体", is_st: false, is_new: false, quality: 90, trend: 85, change: 2, turnover: 3,
    amount: 100000000, score: 88, raw_score: 88.1 };
}

function preset() {
  return { id: 7, name: "全部205只", revision: 3, schema_version: 2, criteria: { market: ["SH"] },
    sort: [{ field: "score", order: "desc" }, { field: "symbol", order: "asc" }], column_view: "overview" };
}

function run() {
  return { id: 42, status: "success", trigger: "manual", mode: "official", rule_version: "bulk-test-v1",
    as_of: "2026-09-04 16:00:00", data_date: "2026-09-04", quote_date: "2026-09-04",
    scope: "沪市 + 深市 + 北交所当前上市A股", total_count: 205, excluded_count: 0, processed_count: 205,
    success_count: 205, missing_count: 0, skipped_count: 0, retry_count: 0, progress_pct: 100, coverage_pct: 100,
    created_at: "2026-09-04 16:00:00", updated_at: "2026-09-04 16:01:00", finished_at: "2026-09-04 16:01:00",
    snapshot_digest: "a".repeat(64), snapshot_seal_origin: "publication", snapshot_sealed_at: "2026-09-04 16:01:00" };
}
