import { expect, test } from "@playwright/test";
import { dailyKlines, minuteAnalysisPayload, minuteKlines, mockApi, workbenchPayload } from "./frontend-flow-api-fixtures.mjs";

for (const timezoneId of ["UTC", "America/Los_Angeles"]) {
  test.describe(`market chart clock with browser timezone ${timezoneId}`, () => {
    test.use({ timezoneId });
    test("axis uses Shanghai time while inspectors retain source timestamps and daily dates", async ({ page }, testInfo) => {
      const rows = minuteKlines("5m", 24).map((row, index) => ({ ...row,
        timestamp: new Date(Date.UTC(2026, 6, 15, 1, 30 + index * 5)).toISOString() }));
      rows[23].timestamp = "2026-07-15T12:25:00+09:00";
      await page.addInitScript(() => {
        window.__chartAxisText = {};
        const prototype = CanvasRenderingContext2D.prototype, fillText = prototype.fillText, clearRect = prototype.clearRect;
        prototype.clearRect = function (...args) {
          window.__chartAxisText[this.canvas.id] = [];
          return clearRect.apply(this, args);
        };
        prototype.fillText = function (text, ...args) {
          (window.__chartAxisText[this.canvas.id] ||= []).push(String(text));
          return fillText.call(this, text, ...args);
        };
      });
      await mockApi(page, { workbench(symbol) { return workbenchPayload(symbol, { withKlines: true }); },
        api(url) {
          if (url.pathname !== "/api/stock/minute-analysis") return null;
          return { payload: { ...minuteAnalysisPayload("5m"), klines: rows, updated_at: rows.at(-1).timestamp } };
        } });
      await page.goto("/");
      await expect(page.locator("#stockName")).toHaveText("贵州茅台");
      const daily = dailyKlines(240).slice(-60);
      await expect.poll(() => axisLabels(page, "klineCanvas")).toEqual([daily[0].date.slice(5), daily.at(-1).date.slice(5)]);
      const dailyCanvas = page.locator("#klineCanvas");
      await dailyCanvas.focus();
      await dailyCanvas.press("Home");
      await expect(page.locator("#dailyChartInspectorValues .chart-inspector-heading strong")).toHaveText(daily[0].date);
      if (testInfo.project.use.isMobile) await page.locator("#mobileChartMinute").click();
      const minuteCanvas = page.locator("#minuteKlineCanvas");
      await expect(minuteCanvas).toBeVisible();
      await expect.poll(() => axisLabels(page, "minuteKlineCanvas")).toEqual(["07-15 09:30", "07-15 11:25"]);
      await minuteCanvas.scrollIntoViewIfNeeded();
      const bounds = await minuteCanvas.boundingBox();
      const x = bounds.x + 46 + (bounds.width - 62) * 9.5 / rows.length;
      const y = bounds.y + 18 + (bounds.height - 46) / 2;
      if (testInfo.project.use.isMobile) await page.touchscreen.tap(x, y);
      else await page.mouse.move(x, y);
      const heading = page.locator("#minuteChartInspectorValues .chart-inspector-heading strong");
      await expect(heading).toHaveText("2026-07-15T02:15:00.000Z");
      await minuteCanvas.focus();
      await minuteCanvas.press("End");
      await expect(heading).toHaveText("2026-07-15T12:25:00+09:00");
    });
  });
}

async function axisLabels(page, id) {
  return page.evaluate(canvasId => (window.__chartAxisText[canvasId] || []).filter(text => /^\d{2}-\d{2}(?: |$)/.test(text)), id);
}
