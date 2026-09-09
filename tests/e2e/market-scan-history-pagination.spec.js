import { expect, test } from "@playwright/test";
import { mockApi, selectPrimaryView, marketScanPollingIdentityPayload } from "./frontend-flow-api-fixtures.mjs";

test("history pagination reaches older same-day batches and retries without changing the selected snapshot", async ({ page }, testInfo) => {
  let failSecondPage = true;
  let mode = "official";
  const queries = [], resultIds = [];
  await mockApi(page, { api(url) {
    if (url.pathname === "/api/market-scans/polling-identity") {
      mode = url.searchParams.get("mode");
      return { payload: marketScanPollingIdentityPayload(run(201, mode), run(201, mode), mode) };
    }
    if (url.pathname.startsWith("/api/market-scans/latest")) return { payload: run(201, url.searchParams.get("mode") || mode) };
    if (url.pathname === "/api/market-scans") {
      const current = Number(url.searchParams.get("page"));
      queries.push(current);
      if (current === 2 && failSecondPage) {
        failSecondPage = false;
        return { status: 503, payload: { detail: "历史暂不可用" } };
      }
      const ids = Array.from({ length: 201 }, (_, index) => 201 - index).slice((current - 1) * 100, current * 100);
      return { payload: { items: ids.map(id => run(id, url.searchParams.get("mode"))), total: 201, page: current, page_size: 100, page_count: 3 } };
    }
    const match = /^\/api\/market-scans\/(\d+)(\/results)?$/.exec(url.pathname);
    if (match) {
      const item = run(Number(match[1]));
      if (!match[2]) return { payload: item };
      resultIds.push(item.id);
      return { payload: { run: item, items: [], total: 0, page: 1, page_size: Number(url.searchParams.get("page_size")), page_count: 0 } };
    }
    return null;
  }});
  await page.goto("/");
  await selectPrimaryView(page, "market");
  await page.locator("#marketScanModeOfficial").check();
  await page.locator("#marketScanHistoryToggle").click();
  await expect(page.locator("#marketScanHistoryPageInfo")).toHaveText("历史第 1 / 3 页");
  const resultCount = resultIds.length;
  await page.locator("#marketScanHistoryNext").click();
  await expect(page.locator("#marketScanHistoryFeedback")).toContainText("读取失败");
  await expect(page.locator("#marketScanHistoryPageInfo")).toHaveText("历史第 1 / 3 页");
  await page.locator("#marketScanHistoryNext").click();
  await expect(page.locator("#marketScanHistoryPageInfo")).toHaveText("历史第 2 / 3 页");
  await page.locator("#marketScanHistoryNext").click();
  await expect(page.locator("#marketScanHistoryPageInfo")).toHaveText("历史第 3 / 3 页");
  await expect(page.locator("#marketScanHistoryNext")).toBeDisabled();
  expect(resultIds.length).toBe(resultCount);
  await page.locator("#marketScanHistoryRun").selectOption("1");
  await expect(page.locator("#marketScanBrowseContext")).toContainText("历史批次 #1");
  await expect.poll(() => resultIds.at(-1)).toBe(1);
  await page.locator("#marketScanHistoryPrev").click();
  await expect(page.locator("#marketScanHistoryPageInfo")).toHaveText("历史第 2 / 3 页");
  await expect(page.locator("#marketScanHistoryRun")).toHaveValue("1");
  await expect(page.locator('#marketScanHistoryRun option[value="1"]')).toContainText("当前浏览（不在本次查询中）");
  expect(queries.slice(-4)).toEqual([2, 2, 3, 2]);
  const overflow = await page.evaluate(() => document.documentElement.scrollWidth - document.documentElement.clientWidth);
  expect(overflow).toBeLessThanOrEqual(1);
  await page.screenshot({ path: testInfo.outputPath("history-pagination.png") });
});

function run(id, mode = "official") {
  const day = "2026-08-14";
  return {
    id, status: "success", trigger: "manual", mode, rule_version: "full-market-score-v5",
    as_of: `${day} 16:30:00`, data_date: day, quote_date: day, scope: "沪市 + 深市 + 北交所当前上市A股",
    total_count: 1, excluded_count: 0, processed_count: 1, success_count: 1, missing_count: 0,
    skipped_count: 0, retry_count: 0, progress_pct: 100, coverage_pct: 100,
    created_at: `${day} 16:30:00`, updated_at: `${day} 16:31:00`, finished_at: `${day} 16:31:00`,
    snapshot_digest: String(id % 10).repeat(64), snapshot_seal_origin: "publication", snapshot_sealed_at: `${day} 16:31:00`,
  };
}
