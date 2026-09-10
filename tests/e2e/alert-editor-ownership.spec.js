import { expect, test } from "@playwright/test";
import { mockApi, workbenchPayload } from "./frontend-flow-api-fixtures.mjs";

for (const nextId of [1, 2]) {
  test(`delayed alert save retains rule ${nextId} draft through readback failure and an explicit retry`, async ({ page }) => {
    const rules = [rule(1), rule(2)], writes = [];
    let release, failReadback = false;
    const pending = new Promise(resolve => { release = resolve; });
    await mockApi(page, { workbench(symbol) { return { ...workbenchPayload(symbol), alert_rules: rules }; },
      async api(url, request) {
        if (url.pathname === "/api/alerts/events") return { payload: [] };
        if (url.pathname === "/api/alerts") return failReadback
          ? { status: 503, payload: { detail: "隔离测试列表暂不可用" } } : { payload: rules };
        if (!/^\/api\/alerts\/[0-9]+$/.test(url.pathname) || request.method() !== "PATCH") return null;
        const id = Number(url.pathname.split("/").at(-1)), body = request.postDataJSON();
        writes.push({ id, body });
        if (writes.length === 1) await pending;
        if (writes.length === 2) return { status: 503, payload: { detail: "隔离测试保存暂不可用" } };
        rules[id - 1] = { ...rules[id - 1], ...body };
        return { payload: rules[id - 1] };
      } });
    try {
      await page.goto("/");
      await expect(page.locator("#stockName")).toHaveText("贵州茅台");
      await page.locator("#workspace-tab-tools").click();
      await page.locator('[data-alert-edit="1"]').click();
      const submitted = page.locator('[data-alert-edit-form][data-alert-id="1"]');
      await submitted.locator('[name="name"]').fill("已提交的规则名称");
      await submitted.locator('button[type="submit"]').click();
      await expect.poll(() => writes.length).toBe(1);
      if (nextId !== 1) await page.locator(`[data-alert-edit="${nextId}"]`).click();
      const form = page.locator(`[data-alert-edit-form][data-alert-id="${nextId}"]`);
      await form.locator('[name="name"]').fill("下一次才提交的规则名称");
      await form.locator('[name="condition_type"]').selectOption("price_below");
      await form.locator('[name="threshold"]').fill("25");
      await form.locator('[name="note"]').fill("等待期间补充的研究备注");
      failReadback = true;
      release();
      await expect(page.locator("#alertList")).toContainText("预警已更新，列表同步降级");
      await expect(page.locator('[data-alert-row="1"] .editable-row-summary')).toContainText("已提交的规则名称");
      await expect(form).toBeVisible();
      await expect(form.locator('[name="name"]')).toHaveValue("下一次才提交的规则名称");
      await expect(form.locator('[name="note"]')).toHaveValue("等待期间补充的研究备注");
      await expect(form.locator('[name="condition_type"]')).toHaveValue("price_below");
      await expect(form.locator('[name="threshold"]')).toHaveValue("25");
      await expect(form.locator('button[type="submit"]')).toBeEnabled();
      failReadback = false;
      await form.locator('button[type="submit"]').click();
      await expect(form.locator(".inline-edit-feedback")).toContainText("保存暂不可用");
      await expect(form.locator('[name="note"]')).toHaveValue("等待期间补充的研究备注");
      await expect(form.locator('button[type="submit"]')).toBeEnabled();
      await form.locator('button[type="submit"]').click();
      await expect(form).toBeHidden();
      expect(writes).toHaveLength(3);
      expect(writes[1]).toEqual(writes[2]);
      expect(writes[2]).toEqual({ id: nextId, body: { name: "下一次才提交的规则名称", condition_type: "price_below",
        threshold: 25, cooldown_seconds: 300, note: "等待期间补充的研究备注" } });
    } finally { release(); }
  });
}

function rule(id) {
  return { id, symbol: "600519.SH", code: "600519", market: "SH", stock_name: "贵州茅台", name: `规则${id}`,
    condition_type: "price_above", condition_label: "价格高于", threshold: id * 10, note: "原规则备注", enabled: true,
    trigger_count: 0, cooldown_seconds: 300, last_state: "等待", last_checked_at: null, last_triggered_at: null,
    created_at: "2026-09-09T04:00:00Z", updated_at: "2026-09-09T04:00:00Z" };
}
