import { expect, test } from "@playwright/test";
import { expectPrimaryView, mockApi, selectPrimaryView } from "./frontend-flow-api-fixtures.mjs";

test("area navigation retains each page and notes avoid unrelated review reads", async ({ page }, testInfo) => {
  const reads = [];
  const writes = [];
  await mockApi(page, {api(url, request) {
    if (request.method() !== "GET") writes.push(url.pathname);
    if (url.pathname.startsWith("/api/reviews") || url.pathname.startsWith("/api/paper-trading")) reads.push(url.pathname);
    return null;
  }});
  await page.goto("/");
  await expect(page.locator("#stockName")).toHaveText("贵州茅台");
  const initialReads = reads.length;
  await page.locator("#workspace-tab-tools").click();
  await page.locator("#noteContent").fill("导航期间保留的研究草稿");
  await expect(page.locator("#workspace-panel-tools")).toBeVisible();
  expect(reads.length).toBe(initialReads);
  await page.screenshot({path:testInfo.outputPath("functional-navigation.png"),fullPage:false});
  await selectPrimaryView(page, "review");
  await page.locator("#workspace-tab-paper").click();
  await expect(page.locator(".query-panel")).toBeHidden();
  await selectPrimaryView(page, "system");
  await expect(page.locator("#workspace-panel-diagnostics")).toBeVisible();
  await expect(page.locator(".query-panel")).toBeHidden();
  await page.screenshot({path:testInfo.outputPath("system-maintenance.png"),fullPage:false});
  await page.locator("#workspace-tab-data").click();
  await selectPrimaryView(page, "monitor");
  await expect(page.locator(".data-health")).toBeHidden();
  await selectPrimaryView(page, "research");
  await expect(page.locator("#workspace-panel-tools")).toBeVisible();
  await expect(page.locator("#noteContent")).toHaveValue("导航期间保留的研究草稿");
  await selectPrimaryView(page, "review");
  await expect(page.locator("#workspace-panel-paper")).toBeVisible();
  await selectPrimaryView(page, "system");
  await expect(page.locator("#workspace-panel-data")).toBeVisible();
  expect(writes).toEqual([]);
  await page.reload();
  await expect(page.locator("#workspace-panel-data")).toBeVisible();
  await selectPrimaryView(page, "research");
  await expect(page.locator("#workspace-panel-tools")).toBeVisible();
  await selectPrimaryView(page, "review");
  await expect(page.locator("#workspace-panel-paper")).toBeVisible();
  const overflow = await page.evaluate(() => document.documentElement.scrollWidth - document.documentElement.clientWidth);
  expect(overflow).toBeLessThanOrEqual(1);
});

test("existing browser preferences follow moved stock notes and system data pages", async ({ page }) => {
  await mockApi(page);
  await page.goto("/");
  for (const [workspace, owner] of [["tools", "research"], ["data", "system"]]) {
    await page.evaluate(({ workspace }) => {
      localStorage.setItem("ashare-radar.workspace-preferences", JSON.stringify({
        version: 1, preferences: {primaryView:"review",workspaceView:workspace,dailyChartRange:120},
      }));
    }, {workspace});
    await page.reload();
    await expect(page.locator("body")).toHaveAttribute("data-primary-view", owner);
    await expect(page.locator(`#workspace-panel-${workspace}`)).toBeVisible();
    await expect(page.locator(`#workspace-tab-${workspace}`)).toHaveAttribute("aria-selected", "true");
  }
});

test("primary navigation separates stock research, selection, replay, monitoring, and system maintenance", async ({ page }) => {
  await mockApi(page, {
    api(url) {
      if (url.pathname === "/api/market-scans/latest") return { payload: null };
      return null;
    },
  });
  await page.goto("/");
  await expect(page.locator("#stockName")).toHaveText("贵州茅台");
  const primaryButtons = page.locator("#primaryNavigation button[data-primary-view]");
  await expect(primaryButtons).toHaveCount(5);
  await expect(primaryButtons).toHaveText(["个股研究", "全市场选股", "复盘模拟", "自选监控", "系统维护"]);
  await expectPrimaryView(page, "research");
  await expect(page.locator("#stockWorkbench")).toBeVisible();
  await expect(page.locator(".query-panel")).toBeVisible();
  await expect(page.locator("#workspace-panel-overview")).toBeVisible();
  await expect(page.locator(".control-panel")).toBeHidden();
  await expect(page.locator(".side-column")).toBeHidden();
  await selectPrimaryView(page, "market");
  await expect(page.locator("#stockWorkbench")).toBeHidden();
  await expect(page.locator(".query-panel")).toBeHidden();
  await expect(page.locator("#workspace-panel-market-scan")).toBeVisible();
  await expect(page.locator("#workspace-panel-overview")).toBeHidden();
  await selectPrimaryView(page, "review");
  await expect(page.locator(".query-panel")).toBeVisible();
  await expect(page.locator("#stockWorkbench")).toBeHidden();
  await expect(page.locator("#workspace-panel-replay")).toBeVisible();
  await expect(page.locator("#workspace-tab-replay")).toBeVisible();
  await expect(page.locator("#workspace-tab-paper")).toBeVisible();
  await expect(page.locator("#workspace-tab-tools")).toBeHidden();
  await expect(page.locator("#workspace-tab-data")).toBeHidden();
  await selectPrimaryView(page, "research");
  await page.locator("#workspace-tab-tools").click();
  await expect(page.locator("#workspace-panel-tools")).toBeVisible();

  await selectPrimaryView(page, "monitor");
  await expect(page.locator(".query-panel")).toBeHidden();
  await expect(page.locator(".workspace")).toBeHidden();
  await expect(page.locator(".control-panel")).toBeVisible();
  await expect(page.locator(".side-column")).toBeVisible();

  await selectPrimaryView(page, "research");
  await expect(page.locator("#stockWorkbench")).toBeHidden();
  await expect(page.locator(".query-panel")).toBeVisible();
  await expect(page.locator("#workspace-panel-tools")).toBeVisible();
  await selectPrimaryView(page, "system");
  await expect(page.locator("#workspace-panel-diagnostics")).toBeVisible();
  await expect(page.locator(".query-panel")).toBeHidden();
  await page.locator("#workspace-tab-data").click();
  await expect(page.locator("#workspace-panel-data")).toBeVisible();
});
