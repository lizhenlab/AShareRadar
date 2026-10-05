import { createHash } from "node:crypto";
import { expect, test } from "@playwright/test";
import { mockApi, selectPrimaryView } from "./frontend-flow-api-fixtures.mjs";

test("saved holding and rebalance periods stay independent through edits", async ({ page }) => {
  const state = await loadSavedStrategy(page);
  await expect(page.locator("#strategyHoldSessions")).toHaveValue("10");
  await expect(page.locator("#strategyRebalanceSessions")).toHaveValue("5");
  expect(periods(state.writes.at(-1))).toEqual([10, 5]);
  await editAndCompile(page, state, "strategyStockCount", "10");
  expect(periods(state.writes.at(-1))).toEqual([10, 5]);
  await editAndCompile(page, state, "strategyHoldSessions", "15");
  expect(periods(state.writes.at(-1))).toEqual([15, 5]);
  await editAndCompile(page, state, "strategyRebalanceSessions", "8");
  expect(periods(state.writes.at(-1))).toEqual([15, 8]);
  await expect(page.locator("#strategyRebalanceHelp")).toContainText("按交易日填写");
  await expect(page.locator("#strategyRebalanceHelp")).toContainText("不会按此间隔自动调仓");
  expect(state.writes.every(item => item.path.endsWith("/compile"))).toBe(true);
  expect(await page.evaluate(() => document.documentElement.scrollWidth - innerWidth)).toBeLessThanOrEqual(1);
  await page.locator("#strategyEditor").screenshot({ path: test.info().outputPath("independent-rebalance.png") });
});

test("empty or fractional rebalance interval blocks compilation and execution", async ({ page }) => {
  const state = await loadSavedStrategy(page);
  const compiledCount = state.writes.length;
  for (const value of ["", "2.5"]) {
    await page.locator("#strategyRebalanceSessions").fill(value);
    await page.locator("#strategyRebalanceSessions").dispatchEvent("change");
    await expect(page.locator("#strategySave")).toBeDisabled();
    await expect(page.locator("#strategyExecuteLatest")).toBeDisabled();
    await expect(page.locator("#strategyCreateSchedule")).toBeDisabled();
    await expect(page.locator("#strategyLabStatus")).toContainText("strategyRebalanceSessions");
    expect(state.writes).toHaveLength(compiledCount);
  }
  await editAndCompile(page, state, "strategyRebalanceSessions", "6");
  expect(periods(state.writes.at(-1))).toEqual([10, 6]);
  await expect(page.locator("#strategySave")).toBeEnabled();
});

async function loadSavedStrategy(page) {
  const state = { saved: null, writes: [] };
  await mockApi(page, { api: (url, request) => strategyApi(url, request, state) });
  await page.goto("/");
  await selectPrimaryView(page, "market");
  await page.locator("#marketScanStrategyToggle").click();
  const spec = await page.evaluate(async () => {
    const { strategySpecFromEditor } = await import("/static/js/strategy-lab-contracts.js");
    return strategySpecFromEditor(document);
  });
  spec.rebalance_policy.hold_sessions = 10;
  spec.rebalance_policy.rebalance_every_sessions = 5;
  state.saved = { strategy_id: 7, strategy_version: 1, revision: 1,
    fingerprint: fingerprint(spec), archived: false, spec };
  await page.locator("#strategyListRefresh").click();
  await expect(page.locator("#strategyLoad")).toBeEnabled();
  await page.locator("#strategySavedSelect").selectOption("7");
  await page.locator("#strategyLoad").click();
  await expect(page.locator("#strategyDraftStatus")).toContainText("v1");
  await expect(page.locator("#strategyExecuteLatest")).toBeEnabled();
  return state;
}

async function editAndCompile(page, state, id, value) {
  const before = state.writes.length;
  await page.locator(`#${id}`).fill(value);
  await page.locator(`#${id}`).dispatchEvent("change");
  await expect.poll(() => state.writes.length).toBe(before + 1);
  await expect(page.locator("#strategySave")).toBeEnabled();
}

function strategyApi(url, request, state) {
  const path = url.pathname;
  if (path === "/api/market-scans/latest") return { payload: null };
  if (!path.startsWith("/api/strategy-lab/")) return null;
  if (request.method() !== "GET") {
    const body = request.postDataJSON();
    state.writes.push({ path, body });
    if (path.endsWith("/compile")) return { payload: { normalized_spec: body.spec,
      fingerprint: fingerprint(body.spec), warnings: [], execution_plan: { executable: true,
        will_start_scan: false, expressions: [], board_labels: [], blocked_reasons: [] } } };
    throw new Error(`Unexpected write ${path}`);
  }
  if (path.endsWith("/strategies/7")) return { payload: state.saved };
  if (path.endsWith("/strategies")) return { payload: { items: state.saved ? [state.saved] : [],
    total: state.saved ? 1 : 0, page: 1, page_size: 100, page_count: state.saved ? 1 : 0 } };
  if (path.endsWith("/evidence")) return { payload: null };
  if (path.endsWith("/executions")) return { payload: { items: [], total: 0, page: 1, page_size: 100, page_count: 0 } };
  return { payload: { items: [], total: 0 } };
}

function periods(write) {
  const policy = write.body.spec.rebalance_policy;
  return [policy.hold_sessions, policy.rebalance_every_sessions];
}

function fingerprint(spec) {
  return createHash("sha256").update(JSON.stringify(spec)).digest("hex");
}
