import { expect, test } from "@playwright/test";
import { mockApi, selectPrimaryView } from "./frontend-flow-api-fixtures.mjs";

const task = "refresh_watch_quotes";
const storageKey = "ashare-monitor-task-request-v1";

async function openMonitoring(page) {
  await selectPrimaryView(page, "system");
  await page.locator("#workspace-tab-diagnostics").click();
  const toggle = page.locator(".data-monitor .layout-collapse-toggle");
  if (await toggle.getAttribute("aria-expanded") === "false") await toggle.click();
  await expect(page.locator(`.monitor-actions [data-task="${task}"]`)).toBeVisible();
}

function receipt(id, status, runStatus) {
  return { request_id: id, requested_task: task, status, created_at: "2026-10-16T03:00:00Z",
    finished_at: status === "running" ? null : "2026-10-16T03:01:00Z", messages: [],
    runs: [{ id: 27, task_name: task, status: runStatus, started_at: "2026-10-16T03:00:00Z" }] };
}

test("accepted task survives navigation and reload, with receipt reads and a single POST", async ({ page }) => {
  const posts = [], reads = [];
  let status = "running";
  await mockApi(page, { api(url, request) {
    if (url.pathname === "/api/tasks/run-once") {
      expect(request.method()).toBe("POST");
      posts.push(url.searchParams.get("request_id"));
      return { status: 202, payload: { ok: true, receipt: receipt(posts[0], "accepted", "running") } };
    }
    if (url.pathname.startsWith("/api/tasks/requests/")) {
      reads.push(url.pathname.split("/").at(-1));
      return { payload: receipt(posts[0], status, status === "completed" ? "success" : "running") };
    }
    return null;
  } });
  await page.goto("/");
  await openMonitoring(page);
  await page.locator(`.monitor-actions [data-task="${task}"]`).click();
  await expect(page.locator("#schedulerState")).toContainText("任务正在后台执行");
  expect(posts).toHaveLength(1);
  await selectPrimaryView(page, "research");
  const beforeReturn = reads.length;
  await openMonitoring(page);
  await expect.poll(() => reads.length).toBeGreaterThan(beforeReturn);
  await expect(page.locator("#schedulerState")).not.toContainText("已取消");
  expect(posts).toHaveLength(1);
  const saved = await page.evaluate(key => JSON.parse(sessionStorage.getItem(key)), storageKey);
  expect(saved).toEqual({ requestId: posts[0], task });
  status = "completed";
  await page.reload();
  await openMonitoring(page);
  await expect(page.locator("#schedulerState")).toContainText("本次任务执行已完成");
  expect(posts).toHaveLength(1);
  expect(reads.every(id => id === posts[0])).toBe(true);
  expect(await page.evaluate(key => sessionStorage.getItem(key), storageKey)).toBeNull();
});

test("unconfirmed response retries the same UUID and displays the actual failed outcome", async ({ page }) => {
  const posts = [];
  await mockApi(page, { api(url) {
    if (url.pathname === "/api/tasks/run-once") {
      posts.push(url.searchParams.get("request_id"));
      if (posts.length === 1) return { status: 503, payload: { detail: "响应未确认" } };
      return { status: 202, payload: { ok: true, receipt: receipt(posts[0], "failed", "failed") } };
    }
    if (url.pathname.startsWith("/api/tasks/requests/")) return { status: 503, payload: { detail: "暂不可读" } };
    return null;
  } });
  await page.goto("/");
  await openMonitoring(page);
  const button = page.locator(`.monitor-actions [data-task="${task}"]`);
  await button.click();
  await expect(page.locator("#schedulerState")).toContainText("请求结果未确认");
  await expect(button).toBeEnabled();
  await expect(page.locator('.monitor-actions [data-task="refresh_key_klines"]')).toBeDisabled();
  await button.click();
  await expect(page.locator("#schedulerState")).toContainText("任务执行失败");
  expect(posts).toHaveLength(2);
  expect(posts[1]).toBe(posts[0]);
  expect(posts[0]).toMatch(/^[0-9a-f-]{36}$/);
  await expect(page.locator('.monitor-actions [data-task="refresh_key_klines"]')).toBeEnabled();
  expect(await page.evaluate(key => sessionStorage.getItem(key), storageKey)).toBeNull();
});
