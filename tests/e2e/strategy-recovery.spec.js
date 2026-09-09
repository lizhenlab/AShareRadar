import { expect, test } from "@playwright/test";
import { mockApi, selectPrimaryView } from "./frontend-flow-api-fixtures.mjs";


test("confirmed strategy writes and candidate pagination recover without repeating or skipping", async ({ page }) => {
  const state = { spec: null, saved: null, readbackUnavailable: false, failedPage: false, writes: [], pages: [] };
  await mockApi(page, { api: (url, request) => strategyApi(url, request, state) });
  await page.goto("/");
  await selectPrimaryView(page, "market");
  await page.locator("#marketScanStrategyToggle").click();
  await page.locator("#strategyName").fill("失败恢复策略");
  state.spec = await page.evaluate(async () => {
    const { strategySpecFromEditor } = await import("/static/js/strategy-lab-contracts.js");
    return strategySpecFromEditor(document);
  });
  await page.locator("#strategyNaturalText").fill("全市场选20只，持有5个交易日");
  await page.locator("#strategyParse").click();
  await expect(page.locator("#strategySave")).toBeEnabled();
  await page.locator("#strategySave").click();
  await expect(page.locator("#strategyLabStatus")).toContainText("已保存");
  await expect(page.locator("#strategyLabStatus")).toContainText("同步未完成");
  await expect(page.locator("#strategySavedSelect")).toHaveValue("7");
  state.readbackUnavailable = false;
  await page.locator("#strategyListRefresh").click();
  await expect(page.locator("#strategyLabStatus")).toContainText("已读取");
  await page.locator("#strategyHistoryRefresh").click();
  await expect(page.locator("#strategyLab")).toHaveAttribute("aria-busy", "false");
  expect(state.writes).toEqual(["POST /api/strategy-lab/strategies"]);
  await page.locator("#strategyExecuteLatest").click();
  await expect(page.locator("#strategyCandidatePage")).toContainText("第 1 / 3 页");
  await page.locator("#strategyCandidateNext").click();
  await expect(page.locator("#strategyLabStatus")).toContainText("暂时无法读取候选");
  await expect(page.locator("#strategyCandidatePage")).toContainText("第 1 / 3 页");
  await page.locator("#strategyCandidateNext").click();
  await expect(page.locator("#strategyCandidatePage")).toContainText("第 2 / 3 页");
  expect(state.pages).toEqual([1, 2, 2]);
  expect(state.writes).toEqual(["POST /api/strategy-lab/strategies", "POST /api/strategy-lab/executions"]);
});


function strategyApi(url, request, state) {
  const path = url.pathname;
  if (!path.startsWith("/api/strategy-lab/")) return null;
  if (path.endsWith("/parse")) {
    return { payload: { original_text: request.postDataJSON().text, draft: state.spec,
      compile: compiled(state.spec), ambiguities: [], unsupported_clauses: [], applied_defaults: [] } };
  }
  if (path.endsWith("/compile")) return { payload: compiled(request.postDataJSON().spec) };
  if (request.method() === "POST") {
    state.writes.push(`${request.method()} ${path}`);
    if (path.endsWith("/executions")) return { payload: execution() };
    state.saved = { strategy_id: 7, strategy_version: 1, revision: 1,
      fingerprint: "a".repeat(64), archived: false, spec: request.postDataJSON().spec };
    state.readbackUnavailable = true;
    return { payload: state.saved };
  }
  if (state.readbackUnavailable) return { status: 503, payload: { detail: "列表暂时不可用" } };
  if (path.endsWith("/strategies")) {
    const items = state.saved ? [state.saved] : [];
    return { payload: { items, total: items.length, page: 1, page_size: 100, page_count: items.length } };
  }
  if (path.endsWith("/candidates")) {
    const page = Number(url.searchParams.get("page"));
    state.pages.push(page);
    if (page === 2 && !state.failedPage) {
      state.failedPage = true;
      return { status: 503, payload: { detail: "暂时无法读取候选" } };
    }
    return { payload: { items: [], page, page_count: 3, total: 125 } };
  }
  if (path.endsWith("/executions")) return { payload: { items: [], total: 0, page: 1, page_size: 100, page_count: 0 } };
  if (path.endsWith("/versions")) return { payload: { items: [], total: 0 } };
  return null;
}

function compiled(spec) {
  return { normalized_spec: spec, fingerprint: "a".repeat(64), warnings: [], execution_plan: { executable: true, expressions: [],
    board_labels: [], objective_order: [], blocked_reasons: [], will_start_scan: false } };
}

function execution() {
  return { context: { execution_id: 9, strategy_id: 7, strategy_version: 1, market_scan_run_id: 3,
    data_date: "2026-09-04", strategy_fingerprint: "a".repeat(64), execution_fingerprint: "b".repeat(64) },
    selected: [], summary: { status: "no_trade", no_trade: true, no_trade_reasons: [], selected_count: 0 } };
}
