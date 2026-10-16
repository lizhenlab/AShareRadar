import { expect, test } from "@playwright/test";
import { expectHistoryPage, noteHistoryApi, noteIds, openRecentNotes } from "./note-history-fixtures.mjs";
import { editNote, noteForm, saveNote, switchStock } from "./note-write-api-fixtures.mjs";

test("recent fifty notes load once, and saving a note keeps the selected history page", async ({ page }, testInfo) => {
  const api = noteHistoryApi();
  await openRecentNotes(page, api);
  expect(api.historyReads).toHaveLength(0);
  expect(api.recentReads).toHaveLength(0);
  expect(await noteIds(page)).toEqual(api.notesFor("600519.SH").slice(0, 50).map(item => item.id));
  await page.locator("#noteHistoryOpen").click();
  await expectHistoryPage(page, 1);
  const snapshot = api.historyReads[0].snapshot;
  await expect(page.locator("#noteHistoryPrevious")).toBeDisabled();
  await page.locator("#noteHistoryNext").click();
  await expectHistoryPage(page, 2);
  const range = api.notesFor("600519.SH").slice(50, 100).map(item => item.id);
  expect(await noteIds(page)).toEqual(range);
  expect(api.historyReads[1]).toMatchObject({ symbol: "600519.SH", page: 2, pageSize: 50, expected: snapshot });
  await editNote(page, range[0], "第二页笔记的新判断");
  await saveNote(page, range[0]);
  await expect(noteForm(page, range[0])).toBeHidden();
  await expect.poll(() => api.historyReads.length).toBe(3);
  await expectHistoryPage(page, 2);
  await expect(page.locator(`[data-note-row="${range[0]}"] .editable-row-summary`)).toContainText("第二页笔记的新判断");
  expect(api.writes).toHaveLength(1);
  expect(api.historyReads[2]).toMatchObject({ page: 2, pageSize: 50, expected: null });
  expect(api.historyReads[2].snapshot).not.toBe(snapshot);
  expect(await noteIds(page)).toEqual(range);
  expect(api.recentReads).toHaveLength(0);
  await expect(page.locator("#noteHistoryPrevious")).toBeEnabled();
  await expect(page.locator("#noteHistoryNext")).toBeEnabled();
  await page.locator("#noteHistoryControls").scrollIntoViewIfNeeded();
  const layout = await page.locator("#noteHistoryControls").evaluate(controls => ({
    viewport: document.documentElement.clientWidth,
    scroll: document.documentElement.scrollWidth,
    buttons: [...controls.querySelectorAll("button")].map(button => {
      const { left, right } = button.getBoundingClientRect();
      return { left, right };
    }),
  }));
  expect(layout.scroll).toBeLessThanOrEqual(layout.viewport + 1);
  for (const button of layout.buttons) {
    expect(button.left).toBeGreaterThanOrEqual(0);
    expect(button.right).toBeLessThanOrEqual(layout.viewport + 1);
  }
  if (testInfo.project.name === "mobile-chromium") {
    await page.screenshot({ path: testInfo.outputPath("note-history-mobile.png") });
  }
});

test("a history conflict retains its page and failed refresh cannot reopen stale navigation", async ({ page }) => {
  const api = noteHistoryApi();
  await openRecentNotes(page, api);
  await page.locator("#noteHistoryOpen").click();
  await expectHistoryPage(page, 1);
  await page.locator("#noteHistoryNext").click();
  await expectHistoryPage(page, 2);
  const previousIds = await noteIds(page), oldSnapshot = api.historyReads.at(-1).snapshot;
  api.update(123, "另一窗口修改了第一页的笔记");
  await page.locator("#noteHistoryNext").click();
  await expect(page.locator("#noteHistoryStatus")).toContainText("历史已变化，当前保留旧页");
  await expectHistoryPage(page, 2);
  expect(await noteIds(page)).toEqual(previousIds);
  expect(api.historyReads.at(-1)).toMatchObject({ page: 3, expected: oldSnapshot, status: 409 });
  await expect(page.locator("#noteHistoryPrevious")).toBeDisabled();
  await expect(page.locator("#noteHistoryNext")).toBeDisabled();
  api.failNextHistory = true;
  await page.locator("#noteHistoryRefresh").click();
  await expect(page.locator("#noteHistoryStatus")).toContainText("历史读取失败");
  await expect(page.locator("#noteHistoryStatus")).toContainText("请刷新后继续翻页");
  await expectHistoryPage(page, 2);
  expect(await noteIds(page)).toEqual(previousIds);
  expect(api.historyReads.at(-1)).toMatchObject({ page: 2, expected: null, status: 503 });
  await expect(page.locator("#noteHistoryPrevious")).toBeDisabled();
  await expect(page.locator("#noteHistoryNext")).toBeDisabled();
  await expect(page.locator("#noteHistoryRefresh")).toBeEnabled();
  await page.locator("#noteHistoryRefresh").click();
  await expect(page.locator("#noteHistoryStatus")).not.toContainText("历史读取失败");
  await expectHistoryPage(page, 2);
  const newSnapshot = api.historyReads.at(-1).snapshot;
  expect(newSnapshot).not.toBe(oldSnapshot);
  expect(await noteIds(page)).toEqual(previousIds);
  await expect(page.locator("#noteHistoryPrevious")).toBeEnabled();
  await expect(page.locator("#noteHistoryNext")).toBeEnabled();
  await page.locator("#noteHistoryNext").click();
  await expectHistoryPage(page, 3, 23);
  expect(api.historyReads.at(-1)).toMatchObject({ page: 3, expected: newSnapshot, status: 200 });
  expect(await noteIds(page)).toEqual(api.notesFor("600519.SH").slice(100).map(item => item.id));
  await expect(page.locator("#noteHistoryNext")).toBeDisabled();
});

test("a delayed history response cannot replace another stock's notes or history controls", async ({ page }) => {
  // Model a transport that has already received the request and does not honor cancellation.
  await page.addInitScript(() => {
    const nativeFetch = window.fetch.bind(window);
    window.fetch = (input, options) => String(input).includes("/api/stock/notes/page")
      ? nativeFetch(input, { ...options, signal: undefined }) : nativeFetch(input, options);
  });
  const api = noteHistoryApi();
  await openRecentNotes(page, api);
  await page.locator("#noteHistoryOpen").click();
  await expectHistoryPage(page, 1);
  const release = api.pauseNextHistory();
  await page.locator("#noteHistoryNext").click();
  await expect.poll(() => api.historyReads.length).toBe(2);
  await expect(page.locator("#noteHistoryControls")).toHaveAttribute("aria-busy", "true");
  await switchStock(page, "000001", "平安银行");
  await expect(page.locator("#noteList [data-note-row]")).toHaveCount(1);
  await expect(page.locator("#noteHistoryStatus")).toContainText("当前显示最近 1 条");
  const response = page.waitForResponse(reply => new URL(reply.url()).pathname === "/api/stock/notes/page"
    && new URL(reply.url()).searchParams.get("page") === "2");
  release();
  await (await response).finished();
  await page.evaluate(() => new Promise(resolve => requestAnimationFrame(() => requestAnimationFrame(resolve))));
  expect(await noteIds(page)).toEqual([124]);
  await expect(page.locator("#noteHistoryControls")).toHaveAttribute("aria-busy", "false");
  await expect(page.locator("#noteHistoryOpen")).toBeEnabled();
  await expect(page.locator("#noteHistoryPrevious")).toBeDisabled();
  await expect(page.locator("#noteHistoryNext")).toBeDisabled();
  await expect(page.locator("#noteHistoryStatus")).toContainText("当前显示最近 1 条");
  await page.locator("#noteHistoryOpen").click();
  await expect(page.locator("#noteHistoryStatus")).toHaveText("共 1 条 · 第 1 / 1 页 · 每页 50 条");
  expect(api.historyReads.at(-1)).toMatchObject({ symbol: "000001.SZ", page: 1, expected: null });
  expect(await noteIds(page)).toEqual([124]);
});
