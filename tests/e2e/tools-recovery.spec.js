import { expect, test } from "@playwright/test";
import { mockApi, selectPrimaryView } from "./frontend-flow-api-fixtures.mjs";

test("legacy backup import can recover with an explicit timezone and cannot reuse changed preview", async ({ page }) => {
  const calls = [];
  await mockApi(page, {api(url, request) {
    if (url.pathname !== "/api/local-data/import") return null;
    calls.push({timezone:url.searchParams.get("legacy_audit_timezone"), dry:url.searchParams.get("dry_run"), body:request.postDataJSON()});
    if (!calls.at(-1).timezone && !calls.at(-1).body.audit_timestamps) {
      return {payload:{detail:"legacy naive audit timestamp requires an explicit timezone"},status:400};
    }
    const dry = url.searchParams.get("dry_run") === "true";
    return {payload:{dry_run:dry,committed:!dry,totals:{inserted:1},preview_token:dry?`browser-preview-token-with-thirty-two-characters-${calls.length}`:null}};
  }});
  await page.goto("/");
  await expect(page.locator("#stockName")).toHaveText("贵州茅台");
  await selectPrimaryView(page, "system");
  await page.locator("#workspace-tab-data").click();
  const bundle = {kind:"ashare-radar-user-data",version:1,tables:{}};
  await page.locator("#localDataImportFile").setInputFiles({name:"legacy.json",mimeType:"application/json",buffer:Buffer.from(JSON.stringify(bundle))});
  await page.locator("#previewLocalDataImport").click();
  await expect(page.locator("#localDataFeedback")).toContainText("旧文件来源时区");
  await expect(page.locator("#commitLocalDataImport")).toBeDisabled();
  await page.getByRole("combobox", {name:"旧文件来源时区", exact:true}).fill("America/Los_Angeles");
  await page.locator("#previewLocalDataImport").click();
  await expect(page.locator("#commitLocalDataImport")).toBeEnabled();
  await page.getByRole("combobox", {name:"旧文件来源时区", exact:true}).fill("Asia/Shanghai");
  await expect(page.locator("#commitLocalDataImport")).toBeDisabled();
  await expect(page.locator("#localDataImportPreview")).toBeEmpty();
  await page.locator("#previewLocalDataImport").click();
  await expect(page.locator("#commitLocalDataImport")).toBeEnabled();
  await page.locator("#commitLocalDataImport").click();
  await expect.poll(() => calls.filter(call => call.dry === "false").length).toBe(1);
  expect(calls.at(-1).timezone).toBe("Asia/Shanghai");
  expect(calls.at(-1).body).toEqual(bundle);
  expect(calls.map(call => call.timezone)).toEqual([null,"America/Los_Angeles","Asia/Shanghai","Asia/Shanghai"]);
  await expect(page.locator("#localDataFeedback")).toContainText("导入已提交");
});

test("cached monitoring results show failed refresh and recover without losing content", async ({ page }) => {
  let failing = false;
  await mockApi(page, {api(url) {
    const paths = ["/api/data/status","/api/tasks/status","/api/monitor/events"];
    if (!paths.includes(url.pathname)) return null;
    if (failing) return {payload:{detail:"隔离测试暂不可用"},status:503};
    if (url.pathname === "/api/data/status") return {payload:{source_plan:{},providers:[{name:"隔离数据源",enabled:true,healthy:true,success_count:1,failure_count:0}],cache:{},capabilities:[],capability_statuses:[]}};
    if (url.pathname === "/api/tasks/status") return {payload:{enabled:true,running:true,tasks:[{name:"quotes",display_name:"隔离刷新任务",last_status:"success",last_message:"已完成"}]}};
    return {payload:[{category:"health",message:"保留的监控事件",level:"info",created_at:"2026-09-07T09:00:00Z"}]};
  }});
  await page.goto("/");
  await expect(page.locator("#stockName")).toHaveText("贵州茅台");
  await selectPrimaryView(page, "system");
  await expect(page.locator("#providerStatus")).toContainText("当前正常");
  failing = true;
  await refreshTools(page);
  await expect(page.locator("#providerStatus")).toContainText("刷新失败，显示上次成功结果");
  await expect(page.locator("#providerStatus")).toContainText("上次正常");
  await expect(page.locator("#taskCards")).toContainText("刷新失败，显示上次成功结果");
  await expect(page.locator("#monitorEvents")).toContainText("保留的监控事件");
  await expect(page.locator("#monitorEvents")).toContainText("刷新失败");
  await expect(page.locator("#schedulerState")).toContainText("上次");
  failing = false;
  await refreshTools(page);
  await expect(page.locator("[data-refresh-warning]")).toHaveCount(0);
  await expect(page.locator("#providerStatus")).toContainText("当前正常");
  await expect(page.locator("#schedulerState")).toHaveText("运行中");
});

async function refreshTools(page) {
  await page.evaluate(async () => {
    const script = document.querySelector('script[type="module"][src^="/static/app.js"]');
    const { __appTest } = await import(script.src);
    await Promise.all([__appTest.refreshDataStatus({force:true}), __appTest.refreshMonitoring({force:true})]);
  });
}
