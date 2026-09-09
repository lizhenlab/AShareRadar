import { expect, test } from "@playwright/test";
import { mockApi, selectPrimaryView, workbenchPayload } from "./frontend-flow-api-fixtures.mjs";

test("stock name composition keys do not select or dismiss suggestions", async ({ page }) => {
  const requestedSymbols = [];
  await mockApi(page, { workbench(symbol) {
    requestedSymbols.push(symbol);
    return workbenchPayload(symbol);
  } });
  await page.goto("/");
  await expect(page.locator("#stockName")).toHaveText("贵州茅台");
  const input = page.locator("#symbolInput");
  await input.fill("平安");
  await expect(page.locator("#symbolSuggestions")).toContainText("平安银行");
  for (const legacy of [false, true]) {
    for (const key of ["ArrowDown", "ArrowUp", "Enter", "Escape"]) {
      const consumed = await input.evaluate((element, options) => {
        const event = new KeyboardEvent("keydown", { key: options.key, isComposing: !options.legacy, bubbles: true, cancelable: true });
        if (options.legacy) Object.defineProperty(event, "keyCode", { value: 229 });
        element.dispatchEvent(event);
        return event.defaultPrevented;
      }, { key, legacy });
      expect(consumed).toBe(false);
      await expect(input).toHaveValue("平安");
      await expect(page.locator("#symbolSuggestions")).toBeVisible();
      await expect(page.locator('#symbolSuggestions [aria-selected="true"]')).toHaveCount(0);
    }
  }
  expect(requestedSymbols).toEqual(["600519"]);
  await input.press("ArrowDown");
  await input.press("Enter");
  await expect(page.locator("#stockName")).toHaveText("平安银行");
  expect(requestedSymbols).toEqual(["600519", "000001.SZ"]);
});

for (const succeeds of [false, true]) {
  test(`import ${succeeds ? "success" : "failure"} leaves consumed preview disabled after the click handler finishes`, async ({ page }) => {
    await trackImportClicks(page);
    await mockApi(page, { api(url) {
      if (url.pathname !== "/api/local-data/import") return null;
      if (url.searchParams.get("dry_run") === "true") return { payload: preview() };
      return succeeds ? { payload: { committed: true, dry_run: false, totals: { inserted: 1 } } }
        : { status: 409, payload: { detail: "隔离提交失败" } };
    } });
    await openImport(page);
    await selectAndPreview(page, "first.json");
    const button = page.locator("#commitLocalDataImport");
    await expect(button).toBeEnabled();
    await button.click();
    await expect.poll(() => page.evaluate(() => window.__importClickCompletions)).toBe(1);
    await expect(button).toBeDisabled();
    if (!succeeds) await expect(page.locator("#localDataFeedback")).toContainText("隔离提交失败");
    await selectPrimaryView(page, "system");
    await page.locator("#workspace-tab-data").click();
    await page.locator("#previewLocalDataImport").click();
    await expect(button).toBeEnabled();
  });
}

test("a new import preview waits for the older commit and remains available after its failure", async ({ page }) => {
  await trackImportClicks(page);
  let releaseCommit;
  const pending = new Promise(resolve => { releaseCommit = resolve; });
  let commits = 0;
  await mockApi(page, { async api(url) {
    if (url.pathname !== "/api/local-data/import") return null;
    if (url.searchParams.get("dry_run") === "true") return { payload: preview() };
    commits += 1;
    if (commits === 1) { await pending; return { status: 409, payload: { detail: "旧文件提交失败" } }; }
    return { payload: { committed: true, dry_run: false, totals: { inserted: 1 } } };
  } });
  await openImport(page);
  await selectAndPreview(page, "old.json");
  const button = page.locator("#commitLocalDataImport");
  try {
    await button.click();
    await expect.poll(() => commits).toBe(1);
    await selectAndPreview(page, "new.json");
    await expect(button).toBeDisabled();
    const bounds = await button.boundingBox();
    await page.mouse.click(bounds.x + bounds.width / 2, bounds.y + bounds.height / 2);
    expect(commits).toBe(1);
  } finally {
    releaseCommit();
  }
  await expect.poll(() => page.evaluate(() => window.__importClickCompletions)).toBe(1);
  await expect(page.locator("#localDataFeedback")).toContainText("先前选择");
  await expect(button).toBeEnabled();
  await button.click();
  await expect.poll(() => commits).toBe(2);
  await expect.poll(() => page.evaluate(() => window.__importClickCompletions)).toBe(2);
  await expect(button).toBeDisabled();
});

function preview() {
  return { dry_run: true, preview_token: "browser-preview-token-with-at-least-thirty-two-characters", totals: { inserted: 1 } };
}

async function openImport(page) {
  await page.goto("/");
  await expect(page.locator("#stockName")).toHaveText("贵州茅台");
  await selectPrimaryView(page, "system");
  await page.locator("#workspace-tab-data").click();
}

async function selectAndPreview(page, name) {
  const bundle = { kind: "ashare-radar-user-data", version: 1, tables: {} };
  await page.locator("#localDataImportFile").setInputFiles({ name, mimeType: "application/json", buffer: Buffer.from(JSON.stringify(bundle)) });
  await page.locator("#previewLocalDataImport").click();
  await expect(page.locator("#localDataImportPreview")).toContainText("导入预览");
}

async function trackImportClicks(page) {
  await page.addInitScript(() => {
    window.__importClickCompletions = 0;
    const original = HTMLButtonElement.prototype.addEventListener;
    HTMLButtonElement.prototype.addEventListener = function(type, listener, options) {
      if (this.id !== "commitLocalDataImport" || type !== "click") return original.call(this, type, listener, options);
      return original.call(this, type, async function(event) {
        try { return await listener.call(this, event); }
        finally { window.__importClickCompletions += 1; }
      }, options);
    };
  });
}
