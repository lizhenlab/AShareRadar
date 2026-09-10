import { expect, test } from "@playwright/test";
import { mockApi, paperTradingDashboard } from "./frontend-flow-api-fixtures.mjs";

for (const oldFails of [false, true]) {
  test(`older comparison ${oldFails ? "failure" : "success"} cannot overwrite newer history or restored comparison selections`, async ({ page }) => {
    const pendingHistoryComparison = deferred();
    const pendingSelectionComparison = deferred();
    let comparisons = 0;
    let released = 0;
    await mockApi(page, { async api(url) {
      if (url.pathname === "/api/paper-trading/runs/compare") {
        comparisons += 1;
        const count = comparisons;
        if (count === 1) await pendingHistoryComparison.promise;
        if (count === 2) await pendingSelectionComparison.promise;
        released += 1;
        if (count <= 2 && oldFails) return { status: 503, payload: { detail: "过期比较错误" } };
        return { payload: comparison(Number(url.searchParams.get("left_run_id")), Number(url.searchParams.get("right_run_id"))) };
      }
      if (url.pathname !== "/api/paper-trading") return null;
      if (url.searchParams.has("run_id") && !oldFails) return { status: 503, payload: { detail: "新的历史读取失败" } };
      return { payload: dashboard(Number(url.searchParams.get("run_id") || 1)) };
    } });
    try {
      await openPaper(page);
      await page.locator("#paperCompareLeft").selectOption("1");
      await page.locator("#paperCompareRight").selectOption("2");
      await page.locator("#comparePaperRuns").click();
      await expect.poll(() => comparisons).toBe(1);
      await page.locator("#paperRunHistory").selectOption("3");
      await page.locator("#loadPaperRun").click();
      const feedback = page.locator("#paperTradingFeedback");
      await expect(feedback).toContainText(oldFails ? "已切换到运行 #3" : "模拟交易暂不可用");
      await expect(page.locator("#comparePaperRuns")).toBeEnabled();
      const latestFeedback = await feedback.textContent();
      pendingHistoryComparison.resolve();
      await expect.poll(() => released).toBe(1);
      await expect(feedback).toHaveText(latestFeedback);
      await expect(feedback).toHaveAttribute("data-tone", oldFails ? "ok" : "error");
      await page.locator("#comparePaperRuns").click();
      await expect.poll(() => comparisons).toBe(2);
      await page.locator("#paperCompareLeft").selectOption("3");
      await page.locator("#paperCompareLeft").selectOption("1");
      await expect(page.locator("#comparePaperRuns")).toBeEnabled();
      await page.locator("#paperCompareRight").selectOption("3");
      await page.locator("#comparePaperRuns").click();
      await expect(feedback).toHaveText("已比较运行 #1 与 #3");
      pendingSelectionComparison.resolve();
      await expect.poll(() => released).toBe(3);
      await expect(feedback).toHaveText("已比较运行 #1 与 #3");
      await expect(page.locator("#paperRunComparison thead")).toContainText("#3");
      await expect(page.locator("#paperExportJson")).toHaveAttribute("href", `/api/paper-trading/runs/${oldFails ? 3 : 1}/export.json`);
    } finally {
      pendingHistoryComparison.resolve();
      pendingSelectionComparison.resolve();
    }
  });
}

async function openPaper(page) {
  await page.goto("/");
  await expect(page.locator("#stockName")).toHaveText("贵州茅台");
  await page.locator('[data-primary-view="review"]').click();
  await page.locator("#workspace-tab-paper").click();
  await expect(page.locator("#paperRunHistory")).toHaveValue("1");
  const toggle = page.locator(".paper-run-panel .layout-collapse-toggle");
  if (await toggle.getAttribute("aria-expanded") === "false") await toggle.click();
  await expect(page.locator("#paperCompareLeft")).toBeVisible();
}

function run(id) {
  return { id, as_of: "2026-07-03T16:00:00+08:00", input_fingerprint: "a".repeat(64), output_digest: "b".repeat(64),
    strategy_count: 0, execution_count: 0, closed_count: 0, data_unavailable_count: 0 };
}

function dashboard(selected) {
  return paperTradingDashboard({ runs: [run(3), run(2), run(1)], latest_run: run(3), selected_run_id: selected, strategies: [] });
}

function comparison(left, right) {
  return { left_run: run(left), right_run: run(right), left_performance: {}, right_performance: {}, deltas: {} };
}

function deferred() {
  let resolve;
  const promise = new Promise(done => { resolve = done; });
  return { promise, resolve };
}
