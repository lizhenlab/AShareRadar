import { expect, test } from "@playwright/test";
import { mockApi, selectPrimaryView } from "./frontend-flow-api-fixtures.mjs";

test("workspace maintenance shows history and checks capacity only on demand", async ({ page, isMobile }) => {
  let storageCalls = 0;
  await mockApi(page, { api(url) {
    if (url.pathname === "/api/system/workspace-maintenance") return { payload: {
      schema_version: "workspace-maintenance-status-v1", status: "configured", checked_at: "2026-10-16T03:00:00Z",
      scope_digest: "a".repeat(64), storage_root_count: 1,
      backups: [{ name: "研究备份", status: "manifest_checked", verification_record_status: "recorded",
        last_verified_at: "2026-10-16T02:00:00Z", root_names: ["研究资料"], size_bytes: 1048576, file_count: 2, excluded_count: 1 }],
      runners: [{ name: "前瞻采集", plan_id: "future-study", status: "fault", phase: "waiting", fault_codes: ["heartbeat_expired"] }],
    } };
    if (url.pathname === "/api/system/workspace-maintenance/storage") {
      storageCalls += 1;
      expect(url.searchParams.get("scope_digest")).toBe("a".repeat(64));
      return { payload: { status: "partial", roots: [], totals: { unique_bytes: 1048576, unique_file_count: 2 }, checked_at: "2026-10-16T03:00:00Z" } };
    }
    return null;
  } });
  await page.goto("/");
  await selectPrimaryView(page, "system");
  await page.locator("#workspace-tab-diagnostics").click();
  await expect(page.locator("#workspaceBackupRecords")).toContainText("研究备份");
  await expect(page.locator("#workspaceBackupRecords")).toContainText("历史验证记录不代表当前文件");
  await expect(page.locator("#workspaceRunnerRecords")).toContainText("巡检已超时");
  if (isMobile) {
    await expect.poll(() => page.evaluate(() => document.documentElement.scrollWidth <= innerWidth)).toBe(true);
    await page.locator("#workspaceMaintenanceTitle").scrollIntoViewIfNeeded();
    await page.locator('[aria-labelledby="workspaceMaintenanceTitle"]').screenshot({ path: "/tmp/ashare-four-improvements-20261016/maintenance-mobile.png" });
  }
  expect(storageCalls).toBe(0);
  await page.locator("#workspaceStorageInspect").click();
  await expect(page.locator("#workspaceStorageStatus")).toContainText("部分结果");
  expect(storageCalls).toBe(1);
});

test("unconfigured maintenance remains explicit and does not offer a scan", async ({ page }) => {
  await mockApi(page, { api(url) {
    if (url.pathname === "/api/system/workspace-maintenance") return { payload: {
      schema_version: "workspace-maintenance-status-v1", status: "not_configured", checked_at: "2026-10-16T03:00:00Z",
      scope_digest: null, storage_root_count: 0, backups: [], runners: [],
    } };
    return null;
  } });
  await page.goto("/");
  await selectPrimaryView(page, "system");
  await page.locator("#workspace-tab-diagnostics").click();
  await expect(page.locator("#workspaceMaintenanceStatus")).toContainText("尚未配置");
  await expect(page.locator("#workspaceStorageInspect")).toBeDisabled();
});
