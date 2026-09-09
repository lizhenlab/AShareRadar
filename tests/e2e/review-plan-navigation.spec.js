import { expect, test } from "@playwright/test";
import { mockApi, selectPrimaryView } from "./frontend-flow-api-fixtures.mjs";


test("global review selection reveals an old plan without shifting its normal list page", async ({ page }) => {
  const state = reviewState("600519.SH");
  await mockApi(page, { api: (url, request) => reviewApi(url, request, state) });
  await page.goto("/");
  await expect(page.locator("#stockName")).toHaveText("贵州茅台");
  await selectPrimaryView(page, "review");
  state.deferNextList = true;
  await page.locator('[data-review-open-plan="7"]').click();
  const target = page.locator('#reviewPlanList [data-review-plan="7"]');
  await expect(target).toBeVisible();
  await expect(target).toBeFocused();
  await releaseLateNormalList(page, state);
  await expect(target).toBeFocused();
  await expect(page.locator("#reviewPlanList .review-plan-item").first()).toHaveAttribute("data-review-plan", "7");
  await expect(page.locator("#reviewPlanFeedback")).toContainText("已定位计划 #7");
  await page.locator("#reviewPlanLoadMore").click();
  await expect.poll(() => state.offsets.includes(20)).toBeTruthy();
  await expect(target).toHaveCount(1);
  await expect(page.locator("#reviewPlanLoadMore")).toBeHidden();
  expect(state.writes).toEqual([]);
});


test("local frozen plan stays readable when its stock load fails and recovers through the same card", async ({ page }) => {
  const state = reviewState("000001.SZ");
  state.quoteUnavailable = true;
  await mockApi(page, { api: (url, request) => reviewApi(url, request, state) });
  await page.goto("/");
  await expect(page.locator("#stockName")).toHaveText("贵州茅台");
  await selectPrimaryView(page, "review");
  await page.locator('[data-review-open-plan="7"]').click();
  const target = page.locator('#reviewPlanList [data-review-plan="7"]');
  await expect(target).toBeVisible();
  await expect(page.locator("#stockName")).toHaveText("贵州茅台");
  await expect(page.locator("#reviewPlanList")).toContainText("定位计划 #7 · 000001.SZ");
  await expect(page.locator("#reviewPlanFeedback")).toContainText("行情暂不可用");
  await expect(target.locator('[data-review-edit="7"]')).toBeDisabled();
  await expect(target.locator('[data-review-evaluate="7"]')).toBeDisabled();
  await expect(target.locator('[data-review-delete="7"]')).toBeDisabled();
  await expect(target.locator('[data-paper-from-review="7"]')).toBeDisabled();
  await expect(page.locator("#reviewPlanSubmit")).toBeDisabled();
  expect(state.writes).toEqual([]);
  state.quoteUnavailable = false;
  await page.locator('[data-review-open-plan="7"]').click();
  await expect(page.locator("#stockName")).toHaveText("平安银行");
  await expect(target).toBeVisible();
  await expect(target.locator('[data-review-edit="7"]')).toBeEnabled();
  await expect(page.locator("#reviewPlanFeedback")).not.toContainText("行情暂不可用");
  expect(state.writes).toEqual([]);
});


test("late ordinary review rows preserve input focus after the user leaves the revealed card", async ({ page }) => {
  const state = reviewState("600519.SH");
  await mockApi(page, { api: (url, request) => reviewApi(url, request, state) });
  await page.goto("/");
  await expect(page.locator("#stockName")).toHaveText("贵州茅台");
  await selectPrimaryView(page, "review");
  state.deferNextList = true;
  await page.locator('[data-review-open-plan="7"]').click();
  await expect(page.locator('#reviewPlanList [data-review-plan="7"]')).toBeFocused();
  const input = page.locator("#reviewHypothesis");
  await input.fill("用户已经开始编辑，不要重新聚焦卡片");
  await expect(input).toBeFocused();
  await releaseLateNormalList(page, state);
  await expect(input).toBeFocused();
  await expect(input).toHaveValue("用户已经开始编辑，不要重新聚焦卡片");
  expect(state.writes).toEqual([]);
});


async function releaseLateNormalList(page, state) {
  await expect.poll(() => typeof state.releaseList).toBe("function");
  await page.evaluate(() => { globalThis.previousReviewCard = document.querySelector('#reviewPlanList [data-review-plan="7"]'); });
  state.releaseList();
  await expect.poll(() => page.evaluate(() => document.querySelector('#reviewPlanList [data-review-plan="7"]') !== globalThis.previousReviewCard)).toBe(true);
}


function reviewState(symbol) {
  const recent = Array.from({ length: 20 }, (_, index) => detail(101 + index, symbol));
  return { selected: detail(7, symbol), recent, quoteUnavailable: false, writes: [], offsets: [] };
}

function reviewApi(url, request, state) {
  if (url.pathname === "/api/stock/workbench" && url.searchParams.get("symbol") === state.selected.plan.symbol
    && state.quoteUnavailable) return { status: 503, payload: { detail: "目标股票行情暂不可用" } };
  if (!url.pathname.startsWith("/api/reviews")) return null;
  if (request.method() !== "GET") state.writes.push(`${request.method()} ${url.pathname}`);
  if (url.pathname === "/api/reviews/plans/7") return { payload: state.selected };
  if (url.pathname === "/api/reviews/due") return { payload: [] };
  if (url.pathname === "/api/reviews/summary") return { payload: {
    generated_at: "2026-07-17 15:00:00", total_plan_count: 21, pending_count: 21,
    evaluated_count: 0, insufficient_count: 0, favorable_count: 0, unfavorable_count: 0,
    ambiguous_count: 0, target_hit_count: 0, stop_hit_count: 0, favorable_rate_pct: null,
    average_return_pct: null, average_mfe_pct: null, average_mae_pct: null, conclusion_counts: { pending: 21 },
  } };
  if (url.pathname === "/api/reviews") {
    if (!url.searchParams.has("symbol")) return { payload: [...state.recent, state.selected] };
    if (url.searchParams.get("symbol") !== state.selected.plan.symbol) return { payload: [] };
    const offset = Number(url.searchParams.get("offset") || 0);
    state.offsets.push(offset);
    if (offset === 0 && state.deferNextList) {
      state.deferNextList = false;
      return new Promise(resolve => { state.releaseList = () => resolve({ payload: state.recent }); });
    }
    return { payload: offset === 0 ? state.recent : [state.selected] };
  }
  return { payload: [] };
}

function detail(id, symbol) {
  return { plan: {
    id, advice_id: id + 1000, symbol, revision: 2,
    snapshot_market_time: "2026-07-01 15:00:00", snapshot_price: 100,
    snapshot_adjustment_mode: "qfq", snapshot_anchor_date: "2026-06-30", snapshot_anchor_close: 100,
    snapshot_data_version: "frontend-qfq-v1", snapshot_contract_version: "daily-kline.v1",
    target_price: 110, stop_price: 95, horizon_days: 20, hypothesis: `计划 ${id} 的冻结研究假设`,
    trigger_condition: "站稳100", invalidation_condition: "跌破95",
    trigger_basis: "daily_high_gte_target_price", invalidation_basis: "daily_low_lte_stop_price",
    plan_payload_digest: "a".repeat(64), created_at: "2026-07-01 15:00:00", updated_at: "2026-07-01 15:00:00",
  }, latest_evaluation: null };
}
