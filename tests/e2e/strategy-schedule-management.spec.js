import { expect, test } from "@playwright/test";
import { mockApi, selectPrimaryView } from "./frontend-flow-api-fixtures.mjs";

test("saved strategy schedules can be reopened, paged, stopped and resumed without execution", async ({ page }) => {
  const state = { spec: null, saved: null, reads: [], writes: [], stopped: false, failPage: true, release: null };
  await mockApi(page, { api: (url, request) => scheduleApi(url, request, state) });
  await openLab(page);
  state.spec = await page.evaluate(async () => {
    const { strategySpecFromEditor } = await import("/static/js/strategy-lab-contracts.js");
    return strategySpecFromEditor(document);
  });
  await page.locator("#strategyNaturalText").fill("全市场选20只，持有5个交易日");
  await page.locator("#strategyParse").click();
  await expect(page.locator("#strategySave")).toBeEnabled();
  await page.locator("#strategySave").click();
  await expect(page.locator("#strategyLabStatus")).toContainText("已保存");
  expect(state.reads).toEqual([]);
  await page.locator("#strategyScheduleManager > summary").click();
  await expect(page.locator("#strategySchedulePage")).toContainText("共 101 个任务");
  await page.getByRole("button", { name: "停用任务 #1", exact: true }).click();
  await expect.poll(() => Boolean(state.release)).toBe(true);
  await expect(page.locator("#strategyArchive")).toBeDisabled();
  await expect(page.locator("#strategyExecuteLatest")).toBeDisabled();
  state.release();
  await expect(page.locator("#strategyScheduleStatus")).toContainText("任务 #1 已停用");
  await page.locator("#strategyScheduleNext").click();
  await expect(page.locator("#strategyScheduleStatus")).toContainText("显示上次成功");
  await expect(page.locator("#strategySchedulePage")).toContainText("第 1 / 6 页");
  await page.locator("#strategyScheduleNext").click();
  await expect(page.locator("#strategySchedulePage")).toContainText("第 2 / 6 页");
  expect(state.reads).toEqual([1, 1, 2, 2]);
  await page.reload();
  await selectPrimaryView(page, "market");
  await page.locator("#marketScanStrategyToggle").click();
  await page.locator("#strategyLoad").click();
  await expect(page.locator("#strategyLabStatus")).toContainText("已载入策略");
  await page.locator("#strategyScheduleManager > summary").click();
  await page.getByRole("button", { name: "恢复任务 #1", exact: true }).click();
  await expect(page.locator("#strategyScheduleStatus")).toContainText("任务 #1 已恢复");
  expect(state.writes).toEqual(["POST /api/strategy-lab/strategies", "PATCH /api/strategy-lab/schedules/1", "PATCH /api/strategy-lab/schedules/1"]);
  expect(await page.evaluate(() => document.documentElement.scrollWidth - innerWidth)).toBeLessThanOrEqual(1);
  await page.locator("#strategyScheduleManager").screenshot({ path: test.info().outputPath("strategy-schedule-manager.png") });
});

async function openLab(page) {
  await page.goto("/");
  await selectPrimaryView(page, "market");
  await page.locator("#marketScanStrategyToggle").click();
}

async function scheduleApi(url, request, state) {
  const path = url.pathname;
  if (!path.startsWith("/api/strategy-lab/")) return null;
  if (path.endsWith("/parse")) return { payload: { original_text: request.postDataJSON().text,
    draft: state.spec, compile: compiled(state.spec), ambiguities: [], unsupported_clauses: [], applied_defaults: [] } };
  if (path.endsWith("/compile")) return { payload: compiled(request.postDataJSON().spec) };
  if (path.endsWith("/schedules") && request.method() === "GET") {
    expect(url.searchParams.get("strategy_id")).toBe("7");
    expect(url.searchParams.get("include_disabled")).toBe("true");
    const number = Number(url.searchParams.get("page")); state.reads.push(number);
    if (number === 2 && state.failPage) { state.failPage = false; return { status:503, payload:{detail:"暂时无法读取任务"} }; }
    const items = Array.from({length:number === 6 ? 1 : 20}, (_,index) => schedule((number-1)*20+index+1,state));
    return { payload:{items,total:101,page:number,page_size:20,page_count:6} };
  }
  if (request.method() === "PATCH") {
    state.writes.push(`${request.method()} ${path}`);
    if (!state.stopped) await new Promise(resolve => { state.release = resolve; });
    state.stopped = !request.postDataJSON().enabled;
    return { payload:schedule(1,state) };
  }
  if (request.method() === "POST") {
    state.writes.push(`${request.method()} ${path}`);
    if (path.endsWith("/strategies")) {
      state.saved = {strategy_id:7,strategy_version:1,revision:1,fingerprint:"a".repeat(64),archived:false,spec:request.postDataJSON().spec};
      return {payload:state.saved};
    }
    throw new Error(`Unexpected write: ${path}`);
  }
  if (path.endsWith("/strategies")) return {payload:{items:state.saved?[state.saved]:[],total:state.saved?1:0,
    page:1,page_size:100,page_count:state.saved?1:0}};
  if (path.endsWith("/strategies/7")) return {payload:state.saved};
  if (path.endsWith("/executions")) return { payload: { items: [], total: 0, page: 1, page_size: 100, page_count: 0 } };
  if (path.endsWith("/versions")) return { payload: { items: [], total: 0 } };
  if (path.endsWith("/evidence")) return {payload:null};
  return null;
}

function schedule(id, state) {
  return {schedule_id:id,strategy_id:7,strategy_version:1,strategy_fingerprint:"a".repeat(64),
    cadence:"daily_after_close",mode:"official",notional_cash_cny:100000,enabled:id === 1 ? !state.stopped : true,
    last_execution_id:id === 1 ? 20 : null,last_market_scan_run_id:id === 1 ? 42 : null,
    alert_conditions:[],created_at:"2026-09-07T00:00:00Z",updated_at:"2026-09-07T00:00:00Z"};
}

function compiled(spec) {
  return {normalized_spec:spec,fingerprint:"a".repeat(64),warnings:[],execution_plan:{executable:true,expressions:[],board_labels:[],objective_order:[],blocked_reasons:[],will_start_scan:false}};
}
