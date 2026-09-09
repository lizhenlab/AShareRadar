import { expect, test } from "@playwright/test";
import { mockApi, selectPrimaryView } from "./frontend-flow-api-fixtures.mjs";


test("due queue reaches plan 201 and filters the full server queue without changing global scope", async ({ page }) => {
  const state = queueState();
  await openReview(page, state);
  expect(state.calls).toEqual([]);
  await page.locator("#reviewDashboardStatus").selectOption("due");
  await expect(page.locator("#reviewDuePageStatus")).toContainText("1 / 5 页 · 本筛选共 201");
  await expect(page.locator("#reviewDashboardQueue .review-dashboard-item")).toHaveCount(50);
  for (let number = 2; number <= 5; number += 1) {
    await page.locator("#reviewDueNext").click();
    await expect(page.locator("#reviewDuePageStatus")).toContainText(`${number} / 5 页`);
  }
  await expect(page.locator('[data-review-dashboard-plan="201"]')).toBeVisible();
  await expect(page.locator("#reviewDueNext")).toBeDisabled();
  await page.locator("#reviewDashboardSymbol").fill("  000001.sz  ");
  await expect(page.locator("#reviewDuePageStatus")).toContainText("1 / 1 页 · 本筛选共 1");
  await expect(page.locator("#reviewDashboardQueue .review-dashboard-item")).toHaveCount(1);
  await expect(page.locator('[data-review-dashboard-plan="201"]')).toContainText("000001.SZ");
  await expect(page.locator("#reviewDashboardSummary")).toContainText("201");
  await expect(page.locator("#evaluateDueReviews")).toHaveText("批量评估全局到期计划（最多100个）");
  await expect(page.locator("#reviewBatchScope")).toContainText("全局");
  expect(state.calls.slice(1, 5).every(query => query.snapshot_token === TOKEN && query.as_of === AS_OF)).toBe(true);
  expect(state.calls.at(-1)).toEqual({ page: "1", page_size: "50", symbol: "000001.SZ" });
  expect(state.writes).toEqual([]);
});


test("due failures retain the last successful page and a changed snapshot restarts explicitly", async ({ page }) => {
  const state = queueState();
  state.failNext = 503;
  await openReview(page, state);
  await page.locator("#reviewDashboardStatus").selectOption("due");
  await expect(page.locator("#reviewDueFeedback")).toContainText("暂不可用");
  await expect(page.locator("#reviewDashboardQueue")).not.toContainText("没有符合筛选");
  await page.locator("#reviewDueRetry").click();
  await expect(page.locator("#reviewDuePageStatus")).toContainText("1 / 5 页");
  state.failNext = 503;
  await page.locator("#reviewDueNext").click();
  await expect(page.locator("#reviewDueFeedback")).toContainText("重试");
  await expect(page.locator("#reviewDuePageStatus")).toContainText("1 / 5 页");
  await expect(page.locator('[data-review-dashboard-plan="1"]')).toBeVisible();
  await page.locator("#reviewDueRetry").click();
  await expect(page.locator("#reviewDuePageStatus")).toContainText("2 / 5 页");
  await expect(page.locator('[data-review-dashboard-plan="51"]')).toBeVisible();
  state.failNext = 409;
  await page.locator("#reviewDuePrev").click();
  await expect(page.locator("#reviewDueFeedback")).toContainText("队列已变化");
  await expect(page.locator("#reviewDueRetry")).toHaveText("从首页重新读取");
  await expect(page.locator("#reviewDuePageStatus")).toContainText("2 / 5 页");
  await expect(page.locator("#reviewDueNext")).toBeDisabled();
  state.token = "b".repeat(64);
  await page.locator("#reviewDueRetry").click();
  await expect(page.locator("#reviewDuePageStatus")).toContainText("1 / 5 页");
  expect(state.calls.map(query => query.page)).toEqual(["1", "1", "2", "2", "1", "1"]);
  expect(state.calls.at(-1).snapshot_token).toBeUndefined();
  expect(state.calls.at(-1).as_of).toBeUndefined();
  expect(state.writes).toEqual([]);
});


test("late due filter responses cannot replace newer filtering or the ordinary view", async ({ page }) => {
  const state = queueState();
  await openReview(page, state);
  await page.locator("#reviewDashboardStatus").selectOption("due");
  await expect(page.locator("#reviewDuePageStatus")).toContainText("201");
  state.holdSymbol = "600";
  await page.locator("#reviewDashboardSymbol").fill("600");
  await expect.poll(() => typeof state.release).toBe("function");
  const firstRelease = state.release;
  await page.locator("#reviewDashboardSymbol").fill("000001");
  await expect(page.locator("#reviewDuePageStatus")).toContainText("本筛选共 1");
  firstRelease();
  await expect(page.locator("#reviewDashboardQueue .review-dashboard-item")).toHaveCount(1);
  await expect(page.locator('[data-review-dashboard-plan="201"]')).toBeVisible();
  state.release = null;
  await page.locator("#reviewDashboardSymbol").fill("600");
  await expect.poll(() => typeof state.release).toBe("function");
  await page.locator("#reviewDashboardStatus").selectOption("all");
  state.release();
  await expect(page.locator("#reviewDueControls")).toBeHidden();
  await expect(page.locator("#reviewDashboardQueue .review-dashboard-item")).toHaveCount(200);
  await expect(page.locator("#reviewDashboardQueue")).not.toContainText("到期 2026");
  expect(state.writes).toEqual([]);
});


const AS_OF = "2026-09-08 15:15:00";
const TOKEN = "a".repeat(64);

function queueState() {
  return { rows: Array.from({ length: 201 }, (_, index) => dueDetail(index + 1)), calls: [], writes: [], token: TOKEN };
}

async function openReview(page, state) {
  await mockApi(page, { api: (url, request) => reviewApi(url, request, state) });
  await page.goto("/");
  await expect(page.locator("#stockName")).toHaveText("贵州茅台");
  await selectPrimaryView(page, "review");
  await expect(page.locator("#reviewDashboardSummary")).toContainText("201");
}

function reviewApi(url, request, state) {
  if (!url.pathname.startsWith("/api/reviews")) return null;
  if (request.method() !== "GET") state.writes.push(`${request.method()} ${url.pathname}`);
  if (url.pathname === "/api/reviews/summary") return { payload: summary() };
  if (url.pathname === "/api/reviews/due") return dueResponse(url, state);
  if (url.pathname === "/api/reviews") {
    const symbol = url.searchParams.get("symbol");
    const rows = symbol ? state.rows.filter(item => item.plan.symbol === symbol) : state.rows;
    const offset = Number(url.searchParams.get("offset") || 0);
    return { payload: rows.slice(offset, offset + Number(url.searchParams.get("limit"))) };
  }
  return { payload: [] };
}

function dueResponse(url, state) {
  const query = Object.fromEntries(url.searchParams);
  state.calls.push(query);
  if (state.failNext) {
    const status = state.failNext;
    state.failNext = null;
    return { status, payload: { detail: "synthetic unavailable or stale snapshot" } };
  }
  const rows = state.rows.filter(item => (!query.symbol || item.plan.symbol.includes(query.symbol))
    && (!query.from_date || item.plan.snapshot_market_time.slice(0, 10) >= query.from_date)
    && (!query.horizon_days || item.plan.horizon_days === Number(query.horizon_days)));
  const number = Number(query.page), size = Number(query.page_size);
  const response = { payload: { items: rows.slice((number - 1) * size, number * size), total: rows.length,
    page: number, page_size: size, page_count: Math.ceil(rows.length / size), as_of: AS_OF, snapshot_token: state.token } };
  if (state.holdSymbol && query.symbol === state.holdSymbol) return new Promise(resolve => { state.release = () => resolve(response); });
  return response;
}

function summary() {
  return { generated_at: AS_OF, total_plan_count: 201, pending_count: 201, evaluated_count: 0, insufficient_count: 0,
    favorable_count: 0, unfavorable_count: 0, ambiguous_count: 0, target_hit_count: 0, stop_hit_count: 0,
    favorable_rate_pct: null, average_return_pct: null, average_mfe_pct: null, average_mae_pct: null, conclusion_counts: { pending: 201 } };
}

function dueDetail(id) {
  return { plan: { id, advice_id: id + 1000, symbol: id === 201 ? "000001.SZ" : "600519.SH", revision: 1,
    snapshot_market_time: "2026-07-01 15:00:00", snapshot_price: 100,
    snapshot_adjustment_mode: "qfq", snapshot_anchor_date: "2026-06-30", snapshot_anchor_close: 100,
    snapshot_data_version: "frontend-qfq-v1", snapshot_contract_version: "daily-kline.v1",
    target_price: 110, stop_price: 95, horizon_days: 20, hypothesis: `队列计划 ${id}`,
    trigger_condition: "站稳100", invalidation_condition: "跌破95", trigger_basis: "daily_high_gte_target_price",
    invalidation_basis: "daily_low_lte_stop_price", plan_payload_digest: "a".repeat(64),
    created_at: "2026-07-01 15:00:00", updated_at: "2026-07-01 15:00:00" }, latest_evaluation: null,
    due_date: "2026-07-29", overdue_trading_days: 2 };
}
