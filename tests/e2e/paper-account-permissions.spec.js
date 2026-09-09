import { expect, test } from "@playwright/test";
import { mockApi, paperTradingDashboard } from "./frontend-flow-api-fixtures.mjs";


for (const emptyRun of [true, false]) {
  test(`historical ${emptyRun ? "empty" : "unfilled"} run keeps cash frozen while default costs remain editable`, async ({ page }) => {
    const dashboard = savedDashboard(emptyRun);
    const writes = [];
    await mockApi(page, { api: (url, request) => {
      if (url.pathname === "/api/paper-trading/account" && request.method() === "PATCH") {
        const payload = request.postDataJSON();
        writes.push(payload);
        if ("initial_cash" in payload) return {status: 400, payload: {detail: "已有运行不能修改初始资金"}};
        Object.assign(dashboard.account, payload);
        return {payload: dashboard.account};
      }
      if (url.pathname === "/api/paper-trading") return {payload: dashboard};
      return null;
    } });
    await page.goto("/");
    await expect(page.locator("#stockName")).toHaveText("贵州茅台");
    await page.locator('[data-primary-view="review"]').click();
    await page.locator("#workspace-tab-paper").click();
    await expect(page.locator("#paperInitialCash")).toBeDisabled();
    await expect(page.locator("#savePaperAccount")).toHaveText("保存默认成本");
    if (!emptyRun) {
      await expect(page.locator("#paperStrategyList")).toContainText("不可变历史运行，不能删除");
      await expect(page.locator("#paperStrategyList [data-paper-delete]")).toHaveCount(0);
    }
    await page.locator("#paperDefaultCostProfile").selectOption("stress");
    await page.locator("#savePaperAccount").click();
    await expect(page.locator("#paperTradingFeedback")).toContainText("默认成本已保存");
    await expect(page.locator("#paperDefaultCostProfile")).toHaveValue("stress");
    await expect(page.locator("#paperInitialCash")).toBeDisabled();
    expect(writes).toEqual([{default_cost_profile: "stress"}]);
  });
}


function savedDashboard(emptyRun) {
  const run = {
    id: 1, as_of: "2026-07-03T16:00:00+08:00", input_fingerprint: "a".repeat(64),
    output_digest: "b".repeat(64), strategy_count: emptyRun ? 0 : 1, execution_count: 0,
    closed_count: 0, data_unavailable_count: emptyRun ? 0 : 1,
  };
  const strategies = emptyRun ? [] : [{
    id: 7, plan_id: 10, plan_revision: 1, advice_id: 20, symbol: "600519.SH",
    plan_payload_digest: "a".repeat(64), allocation_pct: 25, status: "data_unavailable",
    allocation_order: null, target_price: 110, stop_price: 95,
  }];
  return paperTradingDashboard({runs: [run], latest_run: run, selected_run_id: 1, strategies});
}
