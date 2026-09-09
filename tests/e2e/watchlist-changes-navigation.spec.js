import { expect, test as base } from "@playwright/test";
import { mockApi, selectPrimaryView } from "./frontend-flow-api-fixtures.mjs";

// Long-lived WebKit workers can stop dispatching even the next document request
// after the preceding suites. Give these flows their own browser worker while
// keeping the normal navigation timeout, tracing and assertions unchanged.
const test = base.extend({
  watchlistNavigationBrowser: [async ({ browser }, use) => {
    await use(browser);
  }, { scope: "worker", auto: true }],
});

test("viewing a chart keeps changes unread while explicit changes reveal the owned timeline", async ({ page }, testInfo) => {
  const watchlist = queue();
  const marks = [];
  await mockApi(page, {watchlist,timeline,api(url,request) {
    if (url.pathname.endsWith("/mark-viewed")) marks.push({url:url.pathname,body:request.postDataJSON()});
    return null;
  }});
  await page.goto("/");
  await expect(page.locator("#stockName")).toHaveText("贵州茅台");
  await selectPrimaryView(page,"monitor");
  const row = page.locator('.watch-queue-row[data-symbol="000001.SZ"]');
  await row.locator('[data-action="open"]').click();
  await expect(page.locator("#stockName")).toHaveText("平安银行");
  await expect(page.locator("#adviceTimeline")).toContainText("000001.SZ 的结论变化");
  await expect(page.locator("#adviceTimeline")).toBeHidden();
  expect(marks).toHaveLength(0);
  expect(watchlist[1].unread_change_count).toBe(2);
  await page.locator("#noteContent").evaluate(input => {input.value="保留的记录草稿";});
  await selectPrimaryView(page,"monitor");
  await row.getByRole("button",{name:"查看 平安银行 的 2 条新变化"}).click();
  await expect(page.locator("#adviceTimeline")).toBeVisible();
  await expect(page.locator("#adviceTimeline")).toBeFocused();
  await expect(page.locator("body")).toHaveAttribute("data-primary-view","research");
  await expect.poll(() => marks.length).toBe(1);
  expect(marks[0]).toEqual({url:"/api/watchlist/000001.SZ/mark-viewed",body:{clear_unread:true,viewed_through_advice_id:801}});
  await expect(page.locator("#noteContent")).toHaveValue("保留的记录草稿");
  const positions = await page.evaluate(() => ({
    top:document.getElementById("adviceTimeline").getBoundingClientRect().top,
    navBottom:document.getElementById("primaryNavigation").getBoundingClientRect().bottom,
    overflow:document.documentElement.scrollWidth-innerWidth,
  }));
  expect(positions.top).toBeGreaterThanOrEqual(positions.navBottom);
  expect(positions.overflow).toBeLessThanOrEqual(1);
  await page.screenshot({path:testInfo.outputPath("visible-changes-timeline.png")});
  await selectPrimaryView(page,"monitor");
  await expect(row.locator('[data-action="changes"]')).toHaveCount(0);
});

test("failed change history keeps the count and a second explicit click can recover", async ({ page }) => {
  const watchlist = queue(); let fail = true; const marks=[];
  await mockApi(page, {watchlist,timeline,api(url) {
    if (url.pathname.endsWith("/mark-viewed")) marks.push(url.pathname);
    if (fail && url.pathname === "/api/advice/timeline" && url.searchParams.get("symbol") === "000001.SZ") {
      return {payload:{detail:"隔离测试时间线暂不可用"},status:503};
    }
    return null;
  }});
  await page.goto("/");
  await expect(page.locator("#stockName")).toHaveText("贵州茅台");
  await selectPrimaryView(page,"monitor");
  const changes = page.locator('.watch-queue-row[data-symbol="000001.SZ"] [data-action="changes"]');
  await changes.click();
  await expect(page.locator("#watchlistFeedback")).toContainText("未读状态保持");
  await expect(page.locator("#adviceTimeline")).toContainText("暂不可用");
  expect(marks).toHaveLength(0);
  expect(watchlist[1].unread_change_count).toBe(2);
  fail = false;
  await selectPrimaryView(page,"monitor");
  await changes.click();
  await expect(page.locator("#adviceTimeline")).toBeFocused();
  await expect.poll(() => marks.length).toBe(1);
});

test("leaving the changes page before its history arrives does not mark invisible records", async ({ page }) => {
  const watchlist=queue(); const marks=[]; let release; let pending=false;
  const delayed=new Promise(resolve=>{release=resolve;});
  await mockApi(page,{watchlist,timeline,api:async(url)=>{
    if(url.pathname.endsWith("/mark-viewed")) marks.push(url.pathname);
    if(url.pathname === "/api/advice/timeline" && url.searchParams.get("symbol") === "000001.SZ") {
      pending=true; await delayed; return {payload:timeline("000001.SZ")};
    }
    return null;
  }});
  await page.goto("/");
  await expect(page.locator("#stockName")).toHaveText("贵州茅台");
  await selectPrimaryView(page,"monitor");
  await page.locator('.watch-queue-row[data-symbol="000001.SZ"] [data-action="changes"]').click();
  await expect.poll(()=>pending).toBe(true);
  await selectPrimaryView(page,"system");
  release();
  await expect(page.locator("#watchlistFeedback")).toContainText("未读状态保持");
  expect(marks).toHaveLength(0);
  expect(watchlist[1].unread_change_count).toBe(2);
});

function queue() {
  return [{symbol:"600519.SH",code:"600519",name:"贵州茅台",unread_change_count:3},
    {symbol:"000001.SZ",code:"000001",name:"平安银行",unread_change_count:2}];
}
function timeline(symbol) {
  return [{id:symbol === "000001.SZ" ? 801 : 800,action:"观察",confidence:50,trend_score:50,risk_level:"中等",
    created_at:"2026-07-15 10:00:00",market_time:"2026-07-15 10:00:00",comparison_status:"comparable",has_changes:true,
    reason:`${symbol} 的结论变化`,changes:[{category:"action",field:"action",before:"等待",after:"观察"}]}];
}
