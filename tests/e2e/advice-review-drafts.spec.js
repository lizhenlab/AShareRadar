import { expect, test } from "@playwright/test";
import { mockApi, selectPrimaryView } from "./frontend-flow-api-fixtures.mjs";

test("review pagination preserves text and prices entered while the response is pending", async ({ page }) => {
  const gate = deferred();
  let requested = false;
  await openReviews(page, {
    async api(url) {
      if (url.pathname === "/api/reviews" && url.searchParams.get("offset") === "20") {
        requested = true;
        await gate.promise;
        return { payload: [] };
      }
      return null;
    },
  });
  await fillDraft(page, "分页前草稿");
  await page.locator("#reviewPlanLoadMore").click();
  await expect.poll(() => requested).toBe(true);
  await fillDraft(page, "分页等待期间继续输入");
  gate.resolve();
  await expect(page.locator("#reviewPlanLoadMore")).toBeHidden();
  await expectDraft(page, "分页等待期间继续输入");
  await expect(page.locator("#reviewAdviceId")).toHaveValue("3");
});

test("review snapshot refresh preserves the selected draft including intentionally blank input", async ({ page }) => {
  const gate = deferred();
  let refresh = false;
  let requested = false;
  await openReviews(page, {
    async api(url) {
      if (refresh && url.pathname === "/api/advice/timeline" && url.searchParams.get("limit") === "200") {
        requested = true;
        await gate.promise;
        return { payload: [snapshot(5), snapshot(3), snapshot(4, "600519.SH", 200)] };
      }
      return null;
    },
  });
  await fillDraft(page, "刷新前草稿");
  await selectPrimaryView(page, "research");
  refresh = true;
  await selectPrimaryView(page, "review");
  await expect.poll(() => requested).toBe(true);
  await fillDraft(page, "刷新等待期间继续输入");
  await page.locator("#reviewHypothesis").fill("");
  gate.resolve();
  await expect(page.locator('#reviewAdviceId option[value="5"]')).toHaveCount(1);
  await expect(page.locator("#reviewAdviceId")).toHaveValue("3");
  await expect(page.locator("#reviewHypothesis")).toHaveValue("");
  await expect(page.locator("#reviewTrigger")).toHaveValue("刷新等待期间继续输入");
  await expect(page.locator("#reviewTarget")).toHaveValue("120");
  await expect(page.locator("#reviewStop")).toHaveValue("90");
});

test("explicit snapshot selection, cancelling an edit and successful save reset the form", async ({ page }) => {
  let created = false;
  const writes = [];
  await openReviews(page, {
    api(url, request) {
      if (url.pathname === "/api/reviews/plans" && request.method() === "POST") {
        const body = request.postDataJSON();
        writes.push(body);
        created = true;
        return { payload: plan(99, body.advice_id, body), status: 201 };
      }
      if (created && url.pathname === "/api/reviews" && url.searchParams.has("symbol")) {
        return { payload: [{ plan: plan(99, 3), latest_evaluation: null }] };
      }
      return null;
    },
  });
  await fillDraft(page, "旧快照草稿");
  await page.locator("#reviewAdviceId").selectOption("4");
  await expect(page.locator("#reviewHypothesis")).toHaveValue("默认假设4");
  await expect(page.locator("#reviewTarget")).toHaveValue("210");
  await page.locator('[data-review-edit="10"]').click();
  await page.locator("#reviewHypothesis").fill("正在编辑已有计划");
  await page.locator("#reviewPlanCancel").click();
  await expect(page.locator("#reviewHypothesis")).toHaveValue("默认假设3");
  await fillDraft(page, "待保存草稿");
  await page.locator("#reviewPlanSubmit").click();
  await expect.poll(() => writes.length).toBe(1);
  expect(writes[0].hypothesis).toBe("待保存草稿");
  expect(writes[0].advice_id).toBe(3);
  await expect(page.locator("#reviewAdviceId")).toHaveValue("4");
  await expect(page.locator("#reviewHypothesis")).toHaveValue("默认假设4");
  await expect(page.locator("#reviewTarget")).toHaveValue("210");
});

test("changing stocks cannot attach the previous stock's draft to the new snapshot", async ({ page }) => {
  await openReviews(page);
  await fillDraft(page, "茅台草稿");
  if (!(await page.locator("#symbolInput").isVisible())) await page.locator("#queryPanelToggle").click();
  await page.locator("#symbolInput").fill("000001");
  await page.locator("#searchForm button[type=submit]").click();
  await expect(page.locator("#stockCode")).toHaveText("SZ000001");
  await expect(page.locator("#reviewHypothesis")).toHaveValue("平安银行快照");
  await expect(page.locator("#reviewTarget")).toHaveValue("21");
});

async function openReviews(page, options = {}) {
  await mockApi(page, {
    async api(url, request) {
      const custom = options.api ? await options.api(url, request) : null;
      if (custom) return custom;
      if (url.pathname === "/api/reviews") {
        const details = (url.searchParams.get("symbol") || "").includes("600519")
          ? Array.from({ length: 20 }, (_, index) => ({ plan: plan(index + 10, index + 100), latest_evaluation: null }))
          : [];
        return { payload: details };
      }
      if (url.pathname === "/api/reviews/due") return { payload: [] };
      return null;
    },
    timeline(symbol) {
      return symbol.includes("600519") ? [snapshot(3), snapshot(4, "600519.SH", 200)]
        : [snapshot(30, "000001.SZ", 20, "平安银行快照")];
    },
  });
  await page.goto("/");
  await expect(page.locator("#stockName")).toHaveText("贵州茅台");
  await selectPrimaryView(page, "review");
  const panel = page.locator(".advice-review-panel");
  if (await panel.evaluate(element => element.classList.contains("layout-panel-collapsed"))) {
    await panel.locator(".layout-collapse-toggle").click();
  }
  await expect(page.locator("#reviewAdviceId")).toHaveValue("3");
  await expect(page.locator("#reviewPlanList [data-review-edit]")).toHaveCount(20);
  await expect(page.locator("#reviewHypothesis")).toHaveValue("默认假设3");
}

async function fillDraft(page, text) {
  for (const id of ["reviewHypothesis", "reviewTrigger", "reviewInvalidation"]) await page.locator(`#${id}`).fill(text);
  await page.locator("#reviewTarget").fill("120");
  await page.locator("#reviewStop").fill("90");
  await page.locator("#reviewHorizon").fill("35");
}

async function expectDraft(page, text) {
  for (const id of ["reviewHypothesis", "reviewTrigger", "reviewInvalidation"]) await expect(page.locator(`#${id}`)).toHaveValue(text);
  await expect(page.locator("#reviewTarget")).toHaveValue("120");
  await expect(page.locator("#reviewStop")).toHaveValue("90");
  await expect(page.locator("#reviewHorizon")).toHaveValue("35");
}

function snapshot(id, symbol = "600519.SH", price = 100, summary = `默认假设${id}`) {
  return { id, symbol, price, summary, market_time: "2026-07-16 10:00:00", created_at: "2026-07-16 10:00:00",
    kline_adjustment_mode: "qfq", kline_anchor_date: "2026-07-15", kline_anchor_close: price,
    kline_data_version: "browser-fixture.v1", kline_contract_version: "browser-fixture.v1",
    snapshot_contract_version: "browser-fixture.v1", rule_version: "browser-fixture.v1" };
}

function plan(id, adviceId, overrides = {}) {
  return { id, advice_id: adviceId, symbol: "600519.SH", revision: 1,
    snapshot_market_time: "2026-07-16 10:00:00", snapshot_price: 100,
    snapshot_adjustment_mode: "qfq", snapshot_anchor_date: "2026-07-15", snapshot_anchor_close: 100,
    snapshot_data_version: "browser-fixture.v1", snapshot_contract_version: "browser-fixture.v1",
    target_price: 110, stop_price: 95, horizon_days: 20,
    hypothesis: "已有计划", trigger_condition: "触发说明", invalidation_condition: "失效说明",
    trigger_basis: "daily_high_gte_target_price", invalidation_basis: "daily_low_lte_stop_price",
    plan_payload_digest: "a".repeat(64), created_at: "2026-07-16 10:00:00", updated_at: "2026-07-16 10:00:00",
    ...overrides };
}

function deferred() {
  let resolve;
  const promise = new Promise(complete => { resolve = complete; });
  return { promise, resolve };
}
