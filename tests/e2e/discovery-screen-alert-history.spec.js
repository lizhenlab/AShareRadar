import { expect, test } from "@playwright/test";
import { mockApi, selectPrimaryView, marketScanPollingIdentityPayload } from "./frontend-flow-api-fixtures.mjs";

test("screen change acknowledgements stay readable through failed history and detail pagination", async ({ page }, testInfo) => {
  const state = fixtureState();
  await mockApi(page, { api: (url, request) => api(url, request, state) });
  await openPreset(page);
  expect(state.historyReads).toHaveLength(0);
  await page.locator("#discoveryPresetMore summary").click();
  await page.locator("#discoveryPresetScreenAlert").click();
  await expect(page.locator("#discoveryScreenAlertsDetailStatus")).toContainText("已确认");
  expect(state.writes).toBe(1);
  expect(state.historyReads).toHaveLength(0);
  await expect(page.locator("#discoveryScreenAlertsDetailRows [data-change]")).toHaveCount(50);
  await page.locator("#discoveryScreenAlertsDetailNext").click();
  await expect(page.locator("#discoveryScreenAlertsDetailPage")).toContainText("第 2 / 2 页");
  await page.locator("#discoveryScreenAlertsKind").selectOption("unrankable");
  await expect(page.locator("#discoveryScreenAlertsDetailRows")).toContainText("未计为退出");
  expect(state.detailReads).toHaveLength(0);
  state.failHistory = true;
  await page.locator("#discoveryScreenAlertsHistory summary").click();
  await expect(page.locator("#discoveryScreenAlertsHistoryStatus")).toContainText("读取失败");
  await expect(page.locator("#discoveryScreenAlertsDetailStatus")).toContainText("已确认");
  await page.locator("#discoveryScreenAlertsHistoryRefresh").click();
  await expect(page.locator("#discoveryScreenAlertsHistoryPage")).toContainText("共 21 条记录");
  await page.locator("#discoveryScreenAlertsHistoryNext").click();
  await page.getByRole("button", { name: "查看记录 #1", exact: true }).click();
  await expect(page.locator("#discoveryScreenAlertsDetailContext")).toContainText("历史记录 #1");
  await expect(page.locator("#discoveryScreenAlertsDetailContext")).toContainText("修订 v1");
  state.failDetail = true;
  await page.locator("#discoveryScreenAlertsDetailNext").click();
  await expect(page.locator("#discoveryScreenAlertsDetailStatus")).toContainText("读取失败");
  await expect(page.locator("#discoveryScreenAlertsDetailPage")).toContainText("第 1 / 2 页");
  await page.locator("#discoveryScreenAlertsDetailRefresh").click();
  await expect(page.locator("#discoveryScreenAlertsDetailPage")).toContainText("第 2 / 2 页");
  await page.locator("#discoveryScreenAlertsKind").selectOption("exited");
  await expect(page.locator("#discoveryScreenAlertsDetailRows")).toContainText("600001.SH");
  await expect(page.locator("#discoveryScreenAlertsDetailRows")).toContainText("前批次 #41");
  expect(state.writes).toBe(1);
  expect(await page.evaluate(() => document.documentElement.scrollWidth - innerWidth)).toBeLessThanOrEqual(1);
  await page.locator("#discoveryScreenAlertsDetail").screenshot({ path: testInfo.outputPath("screen-change-history-detail.png") });
});

test("history detail selection and leaving the market keep older responses from replacing owned content", async ({ page }) => {
  const state = fixtureState();
  let release; const pending = new Promise(resolve => { release = resolve; });
  state.pendingDetail = async () => { state.waiting = true; await pending; };
  await mockApi(page, { api: (url, request) => api(url, request, state) });
  await openPreset(page);
  await page.locator("#discoveryScreenAlertsHistory summary").click();
  await page.getByRole("button", { name: "查看记录 #21", exact: true }).click();
  await expect.poll(() => state.waiting).toBe(true);
  await page.getByRole("button", { name: "查看记录 #20", exact: true }).click();
  await expect(page.locator("#discoveryScreenAlertsDetailContext")).toContainText("历史记录 #20");
  release();
  await page.locator("#discoveryScreenAlertsKind").selectOption("exited");
  await expect(page.locator("#discoveryScreenAlertsDetailContext")).toContainText("历史记录 #20");
  let releaseHistory; const slowHistory = new Promise(resolve => { releaseHistory = resolve; });
  state.pendingHistory = async () => { state.waitingHistory = true; await slowHistory; };
  await page.locator("#discoveryScreenAlertsHistoryRefresh").click();
  await expect.poll(() => state.waitingHistory).toBe(true);
  await selectPrimaryView(page, "research");
  releaseHistory();
  await selectPrimaryView(page, "market");
  await expect(page.locator("#discoveryScreenAlertsHistoryStatus")).toContainText("读取已取消");
  await expect(page.locator("#discoveryScreenAlertsDetailContext")).toContainText("历史记录 #20");
  await page.locator("#discoveryPresetSelect").selectOption("8");
  await expect(page.locator("#discoveryScreenAlertsContext")).toContainText("另一方案 #8");
  await expect(page.locator("#discoveryScreenAlertsDetail")).toBeHidden();
  await expect(page.locator("#discoveryScreenAlertsHistoryRows")).toBeEmpty();
  expect(state.writes).toBe(0);
});

async function openPreset(page) {
  await page.goto("/");
  await selectPrimaryView(page, "market");
  await page.locator("#marketScanModeOfficial").check();
  await expect(page.locator("#discoveryPresetSelect option[value='7']")).toHaveCount(1);
  await page.locator("#discoveryPresetSelect").selectOption("7");
}

async function api(url, request, state) {
  const path = url.pathname;
  if (path === "/api/market-scans/polling-identity") return { payload: marketScanPollingIdentityPayload(run(), run(), "official") };
  if (path.startsWith("/api/market-scans/latest")) return { payload: run() };
  if (path === "/api/market-scans/42/results") return { payload: { run: run(), items: [], total: 0, page: 1,
    page_size: Number(url.searchParams.get("page_size")), page_count: 0 } };
  if (path === "/api/discovery/presets") return { payload: { items: [preset(7), preset(8)], total: 2, page: 1, page_size: 100, page_count: 1 } };
  if (path === "/api/discovery/presets/7/screen-alerts" && request.method() === "POST") {
    state.writes += 1; return { payload: acknowledgement() };
  }
  if (path === "/api/discovery/presets/7/screen-alerts") return historyResponse(url, state);
  if (path.startsWith("/api/discovery/presets/7/screen-alerts/")) return detailResponse(url, state);
  return null;
}

async function historyResponse(url, state) {
  state.historyReads.push(url.search);
  if (state.pendingHistory) await state.pendingHistory();
  if (state.failHistory) { state.failHistory = false; return { status: 503, payload: { detail: "历史暂不可用" } }; }
  const page = Number(url.searchParams.get("page"));
  return { payload: { preset_id: 7, items: Array.from({ length: 21 }, (_, i) => summary(21 - i)).slice((page - 1) * 20, page * 20),
    total: 21, page, page_size: 20, page_count: 2 } };
}

async function detailResponse(url, state) {
  const id = Number(url.pathname.split("/").at(-1));
  state.detailReads.push(url.search);
  if (id === 21 && state.pendingDetail) await state.pendingDetail();
  if (state.failDetail) { state.failDetail = false; return { status: 503, payload: { detail: "明细暂不可用" } }; }
  const kind = url.searchParams.get("kind"); const page = Number(url.searchParams.get("page"));
  const items = changes().filter(item => kind === "all" || item.change === kind);
  return { payload: { event: summary(id), items: items.slice((page - 1) * 50, page * 50), total: items.length,
    page, page_size: 50, page_count: Math.ceil(items.length / 50), kind } };
}

function fixtureState() { return { historyReads: [], detailReads: [], writes: 0 }; }
function preset(id) { return { id, name: id === 7 ? "质量方案" : "另一方案", revision: 2, schema_version: 2, criteria: {},
  sort: [{ field: "rank", order: "asc" }], column_view: "overview", created_at: "2026-09-07", updated_at: "2026-09-07" }; }
function summary(id) { return { id, preset_id: 7, preset_revision: 1, current_run_id: 42, previous_run_id: 41,
  event_digest: "a".repeat(64), created_at: "2026-09-07T10:00:00Z", entered_count: 55, exited_count: 1, suppressed_unrankable_count: 1 }; }
function changes() { return [...Array.from({ length: 55 }, (_, i) => ({ symbol: `${String(i + 1).padStart(6, "0")}.SZ`, change: "entered" })),
  { symbol: "600001.SH", change: "exited" }, { symbol: "600002.SH", change: "unrankable" }]; }
function acknowledgement() { return { schema_version: "market-scan-screen-alert-v1", status: "ready", unavailable_reason: null,
  preset: { preset_id: 7, preset_revision: 2, preset_name: "质量方案", spec_digest: "c".repeat(64) },
  current: { run_id: 42 }, previous: { run_id: 41 }, event_digest: "d".repeat(64), created: true,
  entered_symbols: changes().filter(item => item.change === "entered").map(item => item.symbol),
  exited_symbols: ["600001.SH"], suppressed_unrankable_symbols: ["600002.SH"] }; }
function run() { return { id: 42, status: "success", trigger: "manual", mode: "official", rule_version: "screen-test-v1",
  as_of: "2026-09-04 16:00:00", data_date: "2026-09-04", quote_date: "2026-09-04", scope: "沪市 + 深市 + 北交所当前上市A股",
  total_count: 1, excluded_count: 0, processed_count: 1, success_count: 1, missing_count: 0, skipped_count: 0,
  retry_count: 0, progress_pct: 100, coverage_pct: 100, created_at: "2026-09-04 16:00:00", updated_at: "2026-09-04 16:01:00",
  finished_at: "2026-09-04 16:01:00", snapshot_digest: "a".repeat(64), snapshot_seal_origin: "publication", snapshot_sealed_at: "2026-09-04 16:01:00" }; }
