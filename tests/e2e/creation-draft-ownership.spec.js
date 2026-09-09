import { expect, test } from "@playwright/test";
import { mockApi } from "./frontend-flow-api-fixtures.mjs";

for (const kind of ["alert", "note"]) {
  test(`${kind} creation receipt preserves a new type draft until its own submission`, async ({ page }) => {
    const isNote = kind === "note", writes = [];
    const path = isNote ? "/api/stock/notes" : "/api/alerts";
    const originalType = isNote ? "观察" : "price_above", nextType = isNote ? "风险" : "price_below";
    const value = isNote ? "仍需关注同一价位" : "12.5";
    let release;
    const pending = new Promise(resolve => { release = resolve; });
    await mockApi(page, { async api(url, request) {
      if (url.pathname !== path || request.method() !== "POST") return null;
      const body = request.postDataJSON();
      writes.push(body);
      if (writes.length === 1) await pending;
      return { status: 201, payload: creationReceipt(body, writes.length) };
    } });
    await page.goto("/");
    await expect(page.locator("#stockName")).toHaveText("贵州茅台");
    await page.locator("#workspace-tab-tools").click();
    const type = page.locator(isNote ? "#noteType" : "#alertType");
    const input = page.locator(isNote ? "#noteContent" : "#alertThreshold");
    const submit = page.locator(isNote ? "#noteForm button[type=submit]" : "#alertForm button[type=submit]");
    await type.selectOption(originalType);
    await input.fill(value);
    await submit.click();
    await expect.poll(() => writes.length).toBe(1);
    await expect(submit).toBeDisabled();
    await expect(type).toBeEnabled();
    await type.selectOption(nextType);
    release();
    await expect(submit).toBeEnabled();
    await expect(type).toHaveValue(nextType);
    await expect(input).toHaveValue(value);
    await submit.click();
    await expect.poll(() => writes.length).toBe(2);
    await expect(submit).toBeEnabled();
    await expect(input).toHaveValue("");
    expect(writes.map(item => item[isNote ? "note_type" : "condition_type"])).toEqual([originalType, nextType]);
    expect(writes.map(item => item[isNote ? "content" : "threshold"])).toEqual(isNote ? [value, value] : [12.5, 12.5]);
  });
}

function creationReceipt(body, id) {
  return { ...body, id, symbol: "600519.SH", code: "600519", market: "SH", name: "贵州茅台",
    visible: true, color: null, revision: String(id).repeat(64),
    created_at: "2026-09-10T02:00:00.000000Z", updated_at: "2026-09-10T02:00:00.000000Z" };
}
