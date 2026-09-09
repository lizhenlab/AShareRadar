import { expect, test } from "@playwright/test";
import { mockApi, selectPrimaryView } from "./frontend-flow-api-fixtures.mjs";

test("repeated feature binding retains one write and disabled styling without the legacy CSS entry", async ({ page }) => {
  const writes = [];
  let release;
  const pendingWrite = new Promise(resolve => { release = resolve; });
  await mockApi(page, {
    async api(url, request) {
      if (url.pathname === "/api/stock/notes" && request.method() === "POST") {
        writes.push(request.postDataJSON());
        await pendingWrite;
        return { payload: { detail: "隔离测试拒绝写入" }, status: 503 };
      }
      return null;
    },
  });
  await page.goto("/");
  await expect(page.locator("#stockName")).toHaveText("贵州茅台");
  await selectPrimaryView(page, "research");
  await page.locator("#workspace-tab-tools").click();
  await page.evaluate(async () => {
    const { bindAdviceReviewEvents } = await import("/static/js/advice-review-events.js");
    const { bindStockNoteAlertEvents } = await import("/static/js/stock-note-alert-events.js");
    bindAdviceReviewEvents({ root: document });
    bindStockNoteAlertEvents({ root: document });
    bindStockNoteAlertEvents({ root: document });
  });
  await page.locator("#noteContent").fill("重复绑定后仍只有一次写入");
  const button = page.locator("#noteForm button[type=submit]");
  await button.click();
  await expect.poll(() => writes.length).toBe(1);
  await expect(button).toBeDisabled();
  await expect(button).toHaveCSS("cursor", "wait");
  await expect(button).toHaveCSS("opacity", "0.72");
  await expect(page.locator('link[href*="/static/styles.css"]')).toHaveCount(0);
  release();
  await expect(page.locator("#noteFormFeedback")).toContainText("隔离测试拒绝写入");
  await expect(button).toBeEnabled();
  await expect(page.locator("#noteContent")).toHaveValue("重复绑定后仍只有一次写入");
  expect(writes).toHaveLength(1);
});
