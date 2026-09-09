import { expect, test } from "@playwright/test";
import { mockApi, selectPrimaryView } from "./frontend-flow-api-fixtures.mjs";

test.use({ timezoneId: "America/Los_Angeles" });

test("queue filters preserve complete subscriptions, editing drafts and Shanghai due dates", async ({ page }, testInfo) => {
  await page.clock.setFixedTime(new Date("2026-09-06T16:00:00Z"));
  const calls = [];
  const watchlist = queueItems();
  page.on("request", request => {
    if (new URL(request.url()).pathname.startsWith("/api/watchlist") || request.url().includes("/api/stream/quotes")) {
      calls.push({url:request.url(),method:request.method()});
    }
  });
  await mockApi(page, {watchlist});
  await page.goto("/");
  await expect(page.locator("#stockName")).toHaveText("贵州茅台");
  await selectPrimaryView(page, "monitor");
  await expect(page.locator("#watchQueueCount")).toHaveText("显示 4 / 共 4 条");
  const first = page.locator('.watch-queue-row[data-symbol="600000.SH"]');
  await expect(first).toContainText("今日复核 · 2026-09-07");
  const before = calls.length;
  await page.locator("#watchQueueDue").check();
  await expect(page.locator("#watchQueueCount")).toHaveText("显示 3 / 共 4 条");
  await page.locator("#watchQueueStatus").selectOption("to_research");
  await page.locator("#watchQueueUnread").check();
  await expect(page.locator(".watch-queue-row:visible")).toHaveCount(1);
  await expect(first).toBeVisible();
  await page.locator("#watchQueueReset").click();
  await first.getByRole("button", {name:"编辑 浦发银行"}).click();
  const draft = first.locator('[name="note"]');
  await draft.fill("筛选后仍保留的草稿");
  await page.locator("#watchQueueSearch").fill("茅台");
  await expect(first).toBeHidden();
  await expect(page.locator("#watchQueueCount")).toHaveText("显示 1 / 共 4 条");
  await page.locator("#watchQueueReset").click();
  await expect(draft).toHaveValue("筛选后仍保留的草稿");
  await expect(draft).toBeVisible();
  expect(calls).toHaveLength(before);
  await page.locator("#watchQueueStatus").selectOption("to_research");
  await first.locator('[name="research_status"]').selectOption("watching");
  await first.getByRole("button", {name:"保存",exact:true}).click();
  await expect(page.locator("#watchQueueCount")).toHaveText("显示 1 / 共 4 条");
  await expect(first).toBeHidden();
  expect(watchlist.find(item => item.symbol === "600000.SH").note).toBe("筛选后仍保留的草稿");
  const complete = await page.evaluate(async () => {
    const script = document.querySelector('script[type="module"][src^="/static/app.js"]');
    return (await import(script.src)).__appTest.state.watchlist;
  });
  expect(complete).toHaveLength(4);
  expect(complete.find(item => item.symbol === "600000.SH").unread_change_count).toBe(2);
  expect(calls.filter(call => call.method !== "GET").map(call => call.method)).toEqual(["PATCH"]);
  expect(await page.evaluate(() => document.documentElement.scrollWidth - window.innerWidth)).toBeLessThanOrEqual(1);
  await page.locator(".watchlist-box").screenshot({path:testInfo.outputPath("watchlist-queue-view.png")});
});

test("queue filter distinguishes unknown source and no match while preserving cached read errors", async ({ page }) => {
  let fail = true;
  await mockApi(page, {watchlist:queueItems(),api(url, request) {
    return fail && url.pathname === "/api/watchlist" && request.method() === "GET"
      ? {payload:{detail:"隔离测试队列暂不可用"},status:503} : null;
  }});
  await page.goto("/");
  await expect(page.locator("#stockName")).toHaveText("贵州茅台");
  await selectPrimaryView(page, "monitor");
  await expect(page.locator("#watchList")).toContainText("自选股读取失败");
  await page.locator("#watchQueueSearch").fill("银行");
  await expect(page.locator("#watchQueueCount")).toContainText("尚未读取成功");
  await expect(page.locator("#watchQueueNoMatch")).toBeHidden();
  fail = false;
  await refreshQueue(page);
  await expect(page.locator("#watchQueueCount")).toHaveText("显示 2 / 共 4 条");
  fail = true;
  await refreshQueue(page);
  await expect(page.locator("#watchList")).toContainText("自选股同步降级，显示上次结果");
  await page.locator("#watchQueueSearch").fill("不存在");
  await expect(page.locator("#watchQueueCount")).toHaveText("显示 0 / 共 4 条");
  await expect(page.locator("#watchQueueNoMatch")).toBeVisible();
  await expect(page.locator("#watchList")).toContainText("自选股同步降级，显示上次结果");
  await page.locator("#watchQueueReset").click();
  await expect(page.locator("#watchQueueCount")).toHaveText("显示 4 / 共 4 条");
});

async function refreshQueue(page) {
  await page.evaluate(async () => {
    const script = document.querySelector('script[type="module"][src^="/static/app.js"]');
    await (await import(script.src)).__appTest.refreshWatchlist({force:true});
  });
}

function queueItems() {
  return [
    {symbol:"600000.SH",code:"600000",name:"浦发银行",group_name:"银行",note:"股息观察",research_status:"to_research",next_review_date:"2026-09-07",unread_change_count:2},
    {symbol:"000001.SZ",code:"000001",name:"平安银行",group_name:"银行",note:"待核对",research_status:"watching",next_review_date:"2026-09-06",unread_change_count:0},
    {symbol:"600519.SH",code:"600519",name:"贵州茅台",group_name:"复利",note:"耐心研究",research_status:"to_research",next_review_date:"2026-09-08",unread_change_count:1},
    {symbol:"000002.SZ",code:"000002",name:"万科A",group_name:"地产",note:"暂排除",research_status:"excluded",next_review_date:"2026-09-05",unread_change_count:0},
  ];
}
