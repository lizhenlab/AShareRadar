import { expect, test } from "@playwright/test";
import { mockApi, selectPrimaryView } from "./frontend-flow-api-fixtures.mjs";

const STREAM = "a".repeat(32);
const RESTORED = "b".repeat(32);

async function prepare(page, events, { failSubject = false } = {}) {
  const requests = [];
  await page.addInitScript(({ streamId }) => {
    localStorage.setItem("ashare-radar.alert-notifications-enabled.v1", "1");
    localStorage.setItem("ashare-radar.alert-notification-cursor.v2", JSON.stringify({ streamId, id: 0 }));
    globalThis.capturedNotifications = [];
    globalThis.Notification = class {
      static permission = "default";
      static async requestPermission() { this.permission = "granted"; return "granted"; }
      constructor(title, options) { this.title = title; this.options = options; globalThis.capturedNotifications.push(this); }
      close() { this.closed = true; }
    };
  }, { streamId: STREAM });
  await mockApi(page, {
    api(url, request) {
      requests.push({ pathname: url.pathname, method: request.method(), symbol: url.searchParams.get("symbol") });
      if (url.pathname === "/api/alerts/notification-events") {
        return { payload: { stream_id: STREAM, baseline_id: 0, cursor_id: events.at(-1).id, reset: false, has_more: false, events } };
      }
      if (failSubject && url.pathname === "/api/stock/workbench" && url.searchParams.get("symbol") === "000001.SZ") {
        return { status: 503, payload: { detail: "合成行情不可用" } };
      }
      // The current history has no captured event, as after deletion or a restored history.
      if (url.pathname === "/api/alerts/events" || url.pathname === "/api/alerts") return { payload: [] };
      return null;
    },
  });
  await page.goto("/");
  await expect(page.locator("#stockName")).toHaveText("贵州茅台");
  await page.locator("#workspace-tab-tools").click();
  await page.locator("#enableAlertNotifications").click();
  await expect.poll(() => page.evaluate(() => globalThis.capturedNotifications.length)).toBe(1);
  return requests;
}

function event(id) {
  return {
    id, rule_id: 2, symbol: "000001.SZ", code: "000001", market: "SZ", stock_name: "平安银行",
    name: "原触发规则", event_type: "触发", message: `原始通知记录${id}`, price: 123.45,
    change_pct: 2.5, threshold: 120, created_at: "2026-09-09T02:00:00.000Z",
  };
}

async function clickCaptured(page) {
  await page.evaluate((streamId) => {
    localStorage.setItem("ashare-radar.alert-notification-cursor.v2", JSON.stringify({ streamId, id: 1 }));
    globalThis.capturedNotifications[0].onclick();
  }, RESTORED);
}

test("desktop reminder opens its captured stock and original event after current history changes", async ({ page }) => {
  const requests = await prepare(page, [event(1)]);
  await selectPrimaryView(page, "monitor");
  await clickCaptured(page);
  await expect(page.locator("#workspace-panel-tools")).toBeVisible();
  await expect(page.locator("#stockName")).toHaveText("平安银行");
  const detail = page.locator("#notificationDetail");
  await expect(detail).toBeVisible();
  await expect(detail).toHaveAttribute("data-stream-id", STREAM);
  await expect(detail).toContainText("原始通知记录1");
  await expect(detail).toContainText("触发时价格 123.45");
  await expect(detail).toContainText("不是当前行情");
  expect(await page.evaluate(() => JSON.parse(localStorage.getItem("ashare-radar.alert-notification-cursor.v2")))).toEqual({ streamId: RESTORED, id: 1 });
  expect(requests.some((request) => request.method !== "GET")).toBe(false);
  expect(requests.some((request) => /^\/api\/alerts\/events\/\d+$/.test(request.pathname))).toBe(false);
  await selectPrimaryView(page, "monitor");
  await expect(detail).toBeHidden();
});

test("failed current quote retains the original notification while the prior stock remains explicit", async ({ page }) => {
  const requests = await prepare(page, [event(1)], { failSubject: true });
  await clickCaptured(page);
  const detail = page.locator("#notificationDetail");
  await expect(detail).toBeVisible();
  await expect(detail).toContainText("当前行情未载入");
  await expect(detail).toContainText("上次成功分析");
  await expect(detail).toContainText("平安银行 (000001.SZ)");
  await expect(detail).toContainText("原始通知记录1");
  await expect(page.locator("#stockName")).toHaveText("贵州茅台");
  await expect(page.locator("#sourceLine")).toContainText("当前仍显示 贵州茅台");
  expect(requests.some((request) => request.method !== "GET")).toBe(false);
});

test("summary notification opens a labelled batch without inventing a stock event", async ({ page }) => {
  const requests = await prepare(page, [1, 2, 3, 4].map(event));
  const previousLoads = requests.filter((request) => request.pathname === "/api/stock/workbench").length;
  await selectPrimaryView(page, "monitor");
  await clickCaptured(page);
  const detail = page.locator("#notificationDetail");
  await expect(detail).toBeVisible();
  await expect(page.locator("#workspace-panel-tools")).toBeVisible();
  await expect(detail).toContainText("4 条新预警");
  await expect(detail).toContainText("未指向单条记录");
  await expect(detail).not.toContainText("触发时价格");
  await expect(page.locator("#stockName")).toHaveText("贵州茅台");
  expect(requests.filter((request) => request.pathname === "/api/stock/workbench")).toHaveLength(previousLoads);
  expect(requests.some((request) => request.method !== "GET")).toBe(false);
});
