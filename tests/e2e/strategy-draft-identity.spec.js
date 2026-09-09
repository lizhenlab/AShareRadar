import { expect, test } from "@playwright/test";
import { mockApi, selectPrimaryView } from "./frontend-flow-api-fixtures.mjs";

test("editing and saving a strategy keep execution version explicit and paper orders reviewable", async ({ page }) => {
  const state = { spec:null, saved:null, writes:[], release:null, execution:null };
  await mockApi(page, {api:(url,request)=>strategyApi(url,request,state)});
  await page.goto("/"); await selectPrimaryView(page,"market");
  await page.locator("#marketScanStrategyToggle").click();
  state.spec=await page.evaluate(async()=>{
    const {strategySpecFromEditor}=await import("/static/js/strategy-lab-contracts.js");
    return strategySpecFromEditor(document);
  });
  await page.locator("#strategyNaturalText").fill("全市场选20只，持有5个交易日");
  await page.locator("#strategyParse").click();
  await expect(page.locator("#strategySave")).toBeEnabled(); await page.locator("#strategySave").click();
  await expect(page.locator("#strategyDraftStatus")).toContainText("v1");
  await expect(page.locator("#strategyExecuteLatest")).toBeEnabled();
  await page.locator("#strategyStockCount").fill("10");
  await expect(page.locator("#strategyExecuteLatest")).toBeDisabled();
  await expect(page.locator("#strategyCreateSchedule")).toBeDisabled();
  await expect(page.locator("#strategyDraftStatus")).toContainText("未保存");
  await expect(page.locator("#strategySave")).toBeEnabled(); await page.locator("#strategySave").click();
  await expect.poll(()=>Boolean(state.release)).toBe(true);
  await page.locator("#strategyStockCount").fill("30"); state.release();
  await expect(page.locator("#strategyDraftStatus")).toContainText("v2");
  await expect(page.locator("#strategyStockCount")).toHaveValue("30");
  await expect(page.locator("#strategyExecuteLatest")).toBeDisabled();
  await expect(page.locator("#strategySave")).toBeEnabled(); await page.locator("#strategySave").click();
  await expect(page.locator("#strategyDraftStatus")).toHaveAttribute("data-state","saved");
  await expect(page.locator("#strategyDraftStatus")).toContainText("v3");
  await page.locator("#strategyExecuteLatest").click();
  await expect(page.locator("#strategyCreateSimulation")).toBeEnabled();
  await page.locator("#strategyCreateSimulation").click();
  await expect(page.locator("#strategyLifecycleContent")).toContainText("策略 #7 v3");
  await expect(page.locator("#strategyLifecycleContent")).toContainText("不会加入复盘模拟账户");
  await expect(page.locator("[data-strategy-order]")).toHaveCount(25);
  await expect(page.locator('[data-strategy-order="600024.SH"]')).toContainText("100 股");
  const savedWrites=state.writes.filter(item=>item.path.endsWith("/strategies/7"));
  expect(savedWrites.map(item=>[item.body.expected_revision,item.body.spec.portfolio_constraints.stock_count])).toEqual([[1,10],[2,30]]);
  expect(state.writes.find(item=>item.path.endsWith("/executions")).body.revision).toBe(3);
  expect(state.writes).toHaveLength(5);
  expect(await page.evaluate(()=>document.documentElement.scrollWidth-innerWidth)).toBeLessThanOrEqual(1);
  await page.locator("#strategyLifecycleContent").screenshot({path:test.info().outputPath("paper-order-draft.png")});
});

async function strategyApi(url,request,state) {
  const path=url.pathname; if(!path.startsWith("/api/strategy-lab/")) return null;
  if(path.endsWith("/parse")) return {payload:{original_text:request.postDataJSON().text,draft:state.spec,compile:compiled(state.spec),ambiguities:[],unsupported_clauses:[],applied_defaults:[]}};
  if(path.endsWith("/compile")) return {payload:compiled(request.postDataJSON().spec)};
  if(request.method()!=="GET") {
    const body=request.postDataJSON(); state.writes.push({path,body});
    if(path.endsWith("/strategies") || path.endsWith("/strategies/7")) {
      const revision=state.saved?state.saved.revision+1:1;
      if(revision===2) await new Promise(resolve=>{state.release=resolve;});
      state.saved={strategy_id:7,strategy_version:revision,revision,fingerprint:fp(body.spec),archived:false,spec:body.spec};
      return {payload:state.saved};
    }
    if(path.endsWith("/executions")) {state.execution=execution(state.saved);return {payload:state.execution};}
    if(path.endsWith("/simulation-plan")) return {payload:paperPlan(state.execution)};
    throw new Error(`Unexpected write ${path}`);
  }
  if(path.endsWith("/strategies")) return {payload:{items:state.saved?[state.saved]:[],total:state.saved?1:0,
    page:1,page_size:100,page_count:state.saved?1:0}};
  if(path.endsWith("/candidates")) return {payload:{items:[],page:1,page_count:0,total:0}};
  if (path.endsWith("/executions")) return { payload: { items: [], total: 0, page: 1, page_size: 100, page_count: 0 } };
  if (path.endsWith("/versions")) return { payload: { items: [], total: 0 } };
  return null;
}

function fp(spec) {return (spec.portfolio_constraints.stock_count===20?"a":spec.portfolio_constraints.stock_count===10?"b":"c").repeat(64);}
function compiled(spec) {return {normalized_spec:spec,fingerprint:fp(spec),warnings:[],execution_plan:{executable:true,will_start_scan:false,expressions:[],board_labels:[],blocked_reasons:[]}};}
function execution(saved) {return {context:{execution_id:9,strategy_id:7,strategy_version:saved.strategy_version,strategy_fingerprint:saved.fingerprint,execution_fingerprint:"d".repeat(64),
  market_scan_run_id:42,data_date:"2026-09-04",data_as_of:"2026-09-04T15:00:00+08:00",rule_version:"portfolio-v1",cost_rule_fingerprint:"e".repeat(64)},selected:[],
  summary:{status:"ready",no_trade:false,no_trade_reasons:[],selected_count:25,evaluated_count:100,evidence_verified_count:100,estimated_turnover:1,residual_cash_cny:1000}};}
function paperPlan(draft) {return {...draft.context,plan_id:3,plan_digest:"f".repeat(64),status:"draft",disclaimers:[],orders:Array.from({length:25},(_,index)=>({symbol:`${600000+index}.SH`,name:`样本股票${index+1}`,board_label:"沪市主板",
  research_side:"paper_buy",target_weight:.02,target_quantity:100,estimated_gross_amount_cny:1000,estimated_round_trip_cost_cny:5,earliest_exit_policy:"A股 T+1；计划持有5日",constraint_notes:["名义资金与容量约束"]}))};}
