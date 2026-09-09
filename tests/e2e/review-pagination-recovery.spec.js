import { expect, test } from "@playwright/test";
import { mockApi, selectPrimaryView } from "./frontend-flow-api-fixtures.mjs";

test("a failed older review page keeps visible plans and editing draft for same-page retry", async ({ page }) => {
  const api = reviewPages();
  await mockApi(page, { api: api.handle });
  await page.goto("/");
  await expect(page.locator("#stockName")).toHaveText("贵州茅台");
  await selectPrimaryView(page, "review");
  const panel = page.locator(".advice-review-panel");
  if (await panel.evaluate(element => element.classList.contains("layout-panel-collapsed"))) {
    await panel.locator(".layout-collapse-toggle").click();
  }
  await expect(page.locator("#reviewPlanList [data-review-edit]")).toHaveCount(20);
  await page.locator('[data-review-edit="101"]').click();
  await page.locator("#reviewHypothesis").fill("分页失败也应保留的草稿");
  await page.locator("#reviewTarget").fill("123");
  await page.locator("#reviewPlanLoadMore").click();
  await expect.poll(() => api.offsets.length).toBe(1);
  await expect(page.locator("#reviewPlanLoadMore")).toBeDisabled();
  api.releaseFailure();
  await expect(page.locator("#reviewPlanPageFeedback")).toContainText("已保留当前列表");
  await expect(page.locator("#reviewPlanPageFeedback")).toContainText("重试");
  await expect(page.locator("#reviewPlanList [data-review-edit]")).toHaveCount(20);
  await expect(page.locator('[data-review-edit="101"]')).toBeAttached();
  await expect(page.locator("#reviewHypothesis")).toHaveValue("分页失败也应保留的草稿");
  await expect(page.locator("#reviewTarget")).toHaveValue("123");
  await expect(page.locator("#reviewPlanSubmit")).toHaveText("更新计划");
  await expect(page.locator("#reviewPlanLoadMore")).toBeEnabled();
  await page.locator("#reviewPlanLoadMore").click();
  await expect(page.locator("#reviewPlanList [data-review-edit]")).toHaveCount(21);
  await expect(page.locator('[data-review-edit="101"]')).toHaveCount(1);
  await expect(page.locator('[data-review-edit="121"]')).toHaveCount(1);
  await expect(page.locator("#reviewPlanPageFeedback")).toBeHidden();
  await expect(page.locator("#reviewPlanLoadMore")).toBeHidden();
  await expect(page.locator("#reviewHypothesis")).toHaveValue("分页失败也应保留的草稿");
  await expect(page.locator("#reviewTarget")).toHaveValue("123");
  expect(api.offsets).toEqual([20, 20]);
});

function reviewPages() {
  const plans = Array.from({ length: 21 }, (_, index) => ({ plan: plan(101 + index), latest_evaluation: null }));
  const offsets = [];
  let releaseFailure;
  const failure = new Promise(resolve => { releaseFailure = resolve; });
  const handle = async (url) => {
    if (url.pathname === "/api/reviews/due") return { payload: [] };
    if (url.pathname !== "/api/reviews") return null;
    const offset = Number(url.searchParams.get("offset") || 0);
    const limit = Number(url.searchParams.get("limit") || 20);
    if (url.searchParams.has("symbol") && offset === 20) {
      offsets.push(offset);
      if (offsets.length === 1) {
        await failure;
        return { status: 503, payload: { detail: "后续页面暂不可用" } };
      }
    }
    return { payload: plans.slice(offset, offset + limit) };
  };
  return { offsets, releaseFailure, handle };
}

function plan(id) {
  return { id, advice_id: id + 100, symbol: "600519.SH", revision: 2,
    snapshot_market_time: "2026-07-16 10:00:00", snapshot_price: 100,
    snapshot_adjustment_mode: "qfq", snapshot_anchor_date: "2026-07-15", snapshot_anchor_close: 100,
    snapshot_data_version: "browser-fixture.v1", snapshot_contract_version: "browser-fixture.v1",
    target_price: 110, stop_price: 95, horizon_days: 20,
    hypothesis: `计划${id}`, trigger_condition: "触发说明", invalidation_condition: "失效说明",
    trigger_basis: "daily_high_gte_target_price", invalidation_basis: "daily_low_lte_stop_price",
    plan_payload_digest: "a".repeat(64), created_at: "2026-07-16 10:00:00", updated_at: "2026-07-16 10:00:00" };
}
