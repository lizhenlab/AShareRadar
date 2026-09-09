import { expect, test } from "@playwright/test";
import { mockApi, selectPrimaryView } from "./frontend-flow-api-fixtures.mjs";


test("saved strategy pagination reaches archived records and retries a failed page", async ({ page }) => {
  const state = { spec: null, enabled: false, total: 101, failSecond: true, requests: [] };
  await prepare(page, state);
  await page.locator("#strategyListRefresh").click();
  await expect(page.locator("#strategyListPage")).toHaveText("第 1 / 2 页 · 共 101 条");
  await page.locator("#strategyListNext").click();
  await expect(page.locator("#strategyLabStatus")).toContainText("第二页暂不可读");
  await expect(page.locator("#strategyListPage")).toHaveText("第 1 / 2 页 · 共 101 条");
  await page.locator("#strategyListNext").click();
  await expect(page.locator("#strategyListPage")).toHaveText("第 2 / 2 页 · 共 101 条");
  await expect(page.locator("#strategySavedSelect")).toHaveValue("1");
  await expect(page.locator("#strategySavedSelect option:checked")).toContainText("已归档");
  await page.locator("#strategyLoad").click();
  await expect(page.locator("#strategyLabStatus")).toContainText("已载入策略 #1");
  await expect(page.locator("#strategyExecuteLatest")).toBeDisabled();
  await page.locator("#strategyExecutionLoad").click();
  await expect(page.locator("#strategyLabStatus")).toContainText("已读取执行 #9");
  await expect(page.locator("#strategyCreateSimulation")).toBeDisabled();
  expect(state.requests.filter(item => item.path.endsWith("/strategies") && item.page === "2")).toHaveLength(2);
  expect(state.requests.filter(item => item.method === "POST").map(item => item.path)).toEqual(["/api/strategy-lab/compile"]);
});


test("reopening retained execution and paper plan preserves a draft across page reload", async ({ page }) => {
  const state = { spec: null, enabled: false, total: 1, failSecond: false, requests: [] };
  await prepare(page, state);
  await page.reload();
  await selectPrimaryView(page, "market");
  await page.locator("#marketScanStrategyToggle").click();
  await expect(page.locator("#strategySavedSelect")).toHaveValue("7");
  await page.locator("#strategyLoad").click();
  await expect(page.locator("#strategyLabStatus")).toContainText("已载入策略 #7");
  await expect(page.locator("#strategyExecutionSelect")).toHaveValue("9");
  await expect(page.locator("#strategyCompare")).toBeDisabled();
  await page.locator("#strategyName").fill("尚未保存的下一版");
  await page.locator("#strategyExecutionLoad").click();
  await expect(page.locator("#strategyLabStatus")).toContainText("已读取执行 #9");
  await expect(page.locator("#strategyLifecycleContent")).toContainText("纸面委托草案 #4");
  await expect(page.locator("#strategyLifecycleContent")).toContainText("不会加入复盘模拟账户");
  await expect(page.locator("#strategyName")).toHaveValue("尚未保存的下一版");
  await expect(page.locator("#strategyCreateSimulation")).toBeDisabled();
  expect(state.requests.some(item => item.path === "/api/strategy-lab/executions/9" && item.method === "GET")).toBe(true);
  expect(state.requests.some(item => item.path.endsWith("/simulation-plan") && item.method === "GET")).toBe(true);
  expect(state.requests.filter(item => item.method !== "GET" && !item.path.endsWith("/compile"))).toEqual([]);
});


async function prepare(page, state) {
  await mockApi(page, { api: (url, request) => historyApi(url, request, state) });
  await page.goto("/");
  await selectPrimaryView(page, "market");
  await page.locator("#marketScanStrategyToggle").click();
  state.spec = await page.evaluate(async () => {
    const { strategySpecFromEditor } = await import("/static/js/strategy-lab-contracts.js");
    return strategySpecFromEditor(document);
  });
  state.enabled = true;
}


function historyApi(url, request, state) {
  const path = url.pathname;
  if (!path.startsWith("/api/strategy-lab/")) return null;
  state.requests.push({ path, method: request.method(), page: url.searchParams.get("page") });
  if (path.endsWith("/templates")) return null;
  if (path.endsWith("/compile")) return { payload: { normalized_spec: request.postDataJSON().spec,
    fingerprint: "a".repeat(64), execution_plan: { executable: true } } };
  if (path.endsWith("/strategies")) return savedStrategies(url, state);
  if (/\/strategies\/\d+$/.test(path)) return { payload: savedStrategy(Number(path.split("/").at(-1)), state) };
  if (path.endsWith("/versions")) return { payload: { items: [{ revision: 1, name: "已保存", fingerprint: "a".repeat(64) }], total: 1 } };
  if (path.endsWith("/evidence")) return { payload: null };
  const strategyId = state.total === 1 ? 7 : 1;
  const context = executionContext(strategyId);
  if (path.endsWith("/executions")) return { payload: { items: [context], total: 1, page: 1, page_size: 100, page_count: 1 } };
  if (path.endsWith("/executions/9")) return { payload: { context, selected: [],
    summary: { status: "no_trade", no_trade: true, no_trade_reasons: ["无可交易候选"], selected_count: 0 } } };
  if (path.endsWith("/candidates")) return { payload: { items: [], execution_id: 9, page: 1, page_size: 50, page_count: 0, total: 0 } };
  if (path.endsWith("/simulation-plan")) return { payload: { ...context, plan_id: 4, plan_digest: "e".repeat(64),
    orders: [], disclaimers: ["仅供纸面研究"], status: "no_trade" } };
  return null;
}


function savedStrategies(url, state) {
  const page = Number(url.searchParams.get("page"));
  if (page === 2 && state.failSecond) {
    state.failSecond = false;
    return { status: 503, payload: { detail: "第二页暂不可读" } };
  }
  const total = state.enabled ? state.total : 0;
  const ids = total === 1 ? [7] : (page === 2 ? [1] : Array.from({ length: 100 }, (_, index) => index + 2));
  return { payload: { items: total ? ids.map(id => savedStrategy(id, state)) : [],
    total, page, page_size: 100, page_count: Math.ceil(total / 100) } };
}


function savedStrategy(id, state) {
  return { strategy_id: id, strategy_version: 1, revision: 1, fingerprint: "a".repeat(64),
    archived: id === 1, spec: { ...state.spec, name: `已保存策略 ${id}` } };
}


function executionContext(strategyId) {
  return { execution_id: 9, strategy_id: strategyId, strategy_version: 1, strategy_fingerprint: "a".repeat(64),
    execution_fingerprint: "b".repeat(64), market_scan_run_id: 3, source_snapshot_digest: "c".repeat(64),
    source_snapshot_seal_origin: "publication", cost_rule_fingerprint: "d".repeat(64), rule_version: "full-market-score-v5",
    data_as_of: "2026-09-04T15:00:00+08:00", data_date: "2026-09-04", kind: "latest_scan", status: "no_trade",
    point_in_time: true, created_at: "2026-09-04T16:00:00+08:00" };
}
