import { expect, test } from "@playwright/test";
import { mockApi, selectPrimaryView } from "./frontend-flow-api-fixtures.mjs";

for (const nextPlan of [7, 8]) {
  test(`saving plan A preserves the newer draft for plan ${nextPlan} and its next revision`, async ({ page }) => {
    const api = reviewApi();
    await openReviews(page, api);
    await page.locator('[data-review-edit="7"]').click();
    await page.locator("#reviewHypothesis").fill("A的本次提交");
    await page.locator("#reviewPlanSubmit").click();
    await expect.poll(() => api.writes.length).toBe(1);
    expect(api.writes[0]).toMatchObject({ id: 7, body: { expected_revision: 2 } });
    if (nextPlan === 8) await page.locator('[data-review-edit="8"]').click();
    await page.locator("#reviewHypothesis").fill(`计划${nextPlan}的新草稿`);
    await page.locator("#reviewTarget").fill("123");
    await expect(page.locator("#reviewPlanSubmit")).toBeDisabled();
    api.release();
    await expect(page.locator("#reviewPlanFeedback")).toContainText("已更新");
    await expect(page.locator("#reviewPlanSubmit")).toBeEnabled();
    await expect(page.locator("#reviewPlanSubmit")).toHaveText("更新计划");
    await expect(page.locator("#reviewHypothesis")).toHaveValue(`计划${nextPlan}的新草稿`);
    await expect(page.locator("#reviewTarget")).toHaveValue("123");
    await expect(page.locator("#reviewPlanCancel")).toBeVisible();
    await page.locator("#reviewPlanSubmit").click();
    await expect.poll(() => api.writes.length).toBe(2);
    expect(api.writes[1]).toMatchObject({ id: nextPlan, body: {
      expected_revision: nextPlan === 7 ? 3 : 2,
      hypothesis: `计划${nextPlan}的新草稿`, target_price: 123,
    } });
    await expect(page.locator("#reviewPlanCancel")).toBeHidden();
    await expect(page.locator("#reviewPlanSubmit")).toHaveText("建立计划");
    expect(api.writes).toHaveLength(2);
  });
}

async function openReviews(page, api) {
  await mockApi(page, {
    api: api.handle,
    timeline() {
      return [{ id: 5, symbol: "600519.SH", price: 100, summary: "未保存快照",
        market_time: "2026-07-16 10:00:00", created_at: "2026-07-16 10:00:00",
        kline_adjustment_mode: "qfq", kline_anchor_date: "2026-07-15", kline_anchor_close: 100,
        kline_data_version: "browser-fixture.v1", kline_contract_version: "browser-fixture.v1",
        snapshot_contract_version: "browser-fixture.v1", rule_version: "browser-fixture.v1" }];
    },
  });
  await page.goto("/");
  await expect(page.locator("#stockName")).toHaveText("贵州茅台");
  await selectPrimaryView(page, "review");
  const panel = page.locator(".advice-review-panel");
  if (await panel.evaluate(element => element.classList.contains("layout-panel-collapsed"))) {
    await panel.locator(".layout-collapse-toggle").click();
  }
  await expect(page.locator("#reviewPlanList [data-review-edit]")).toHaveCount(2);
}

function reviewApi() {
  const plans = new Map([7, 8].map(id => [id, plan(id)]));
  const writes = [];
  let release;
  const gate = new Promise(resolve => { release = resolve; });
  const handle = async (url, request) => {
    if (url.pathname === "/api/reviews") {
      return { payload: [...plans.values()].map(value => ({ plan: value, latest_evaluation: null })) };
    }
    if (url.pathname === "/api/reviews/due") return { payload: [] };
    if (!url.pathname.startsWith("/api/reviews/plans/") || request.method() !== "PATCH") return null;
    const id = Number(url.pathname.split("/").at(-1));
    const body = request.postDataJSON();
    writes.push({ id, body });
    if (writes.length === 1) await gate;
    if (plans.get(id)?.revision !== body.expected_revision) {
      return { status: 409, payload: { detail: "计划修订冲突" } };
    }
    const { expected_revision, ...fields } = body;
    const saved = { ...plans.get(id), ...fields, revision: expected_revision + 1 };
    plans.set(id, saved);
    return { payload: saved };
  };
  return { writes, release, handle };
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
