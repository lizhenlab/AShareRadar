import {expect, test} from "@playwright/test";
import {mockApi, selectPrimaryView, marketScanPollingIdentityPayload} from "./frontend-flow-api-fixtures.mjs";

test("saved screens remain reachable across pages and copying preserves the original multi-market definition", async ({page}, testInfo) => {
  const state={records:Array.from({length:101},(_,i)=>preset(i+1)),writes:[],failNext:true};
  await mockApi(page,{api:(url,request)=>presetApi(url,request,state)});
  await page.goto("/"); await selectPrimaryView(page,"market");
  await page.locator("#marketScanModeOfficial").check();
  await expect(page.locator("#discoveryPresetPageInfo")).toContainText("共 101 项");
  await page.locator("#discoveryPresetSelect").selectOption("101");
  await page.locator("#discoveryPresetNext").click();
  await expect(page.locator("#discoveryPresetFeedback")).toContainText("读取失败");
  await expect(page.locator("#discoveryPresetSelect")).toHaveValue("101");
  await expect(page.locator("#discoveryPresetPageInfo")).toContainText("1 / 2");
  await page.locator("#discoveryPresetNext").click();
  await expect(page.locator("#discoveryPresetPageInfo")).toContainText("2 / 2");
  await expect(page.locator("#discoveryPresetSelectionInfo")).toContainText("跨页保留");
  await page.locator("#discoveryPresetSelect").selectOption("1");
  await page.locator("#marketScanFilterToggle").click();
  await page.locator("#marketScanMarket").selectOption(["SH","SZ"]);
  await page.locator("#discoveryPresetName").fill("沪深独立副本");
  await page.locator("#discoveryPresetSave").click();
  await expect(page.locator("#discoveryPresetFeedback")).toContainText("已保存新");
  await expect(page.locator("#discoveryPresetSelect")).toHaveValue("102");
  expect(state.writes[0].method).toBe("POST");
  expect(state.records.find(item=>item.id===1).name).toBe("方案1");
  expect(state.records.find(item=>item.id===1).criteria.market).toEqual(["SH"]);
  await page.locator("#discoveryPresetApply").click();
  await expect(page.locator("#discoveryPresetFeedback")).toContainText("已应用");
  expect(await selectedMarkets(page)).toEqual(["SH","SZ"]);
  await page.locator("#marketScanScoreMin").fill("88");
  await page.locator("#discoveryPresetMore > summary").click();
  await page.locator("#discoveryPresetUpdate").click();
  await expect(page.locator("#discoveryPresetFeedback")).toContainText("已更新");
  const update=state.writes.find(item=>item.method==="PUT");
  expect(update.body.expected_revision).toBe(1); expect(update.body.criteria.score.min).toBe(88);
  expect(state.records.find(item=>item.id===1).criteria.score.min).toBe(80);
  await expect(page.locator("#discoveryPresetSelectionInfo")).toContainText("修订 2");
  await page.locator("#discoveryPresetPrev").click();
  await page.locator("#discoveryPresetSelect").selectOption("13");
  await expect(page.locator("#discoveryPresetSave")).toBeDisabled();
  await expect(page.locator("#discoveryPresetUpdate")).toBeDisabled();
  await expect(page.locator("#discoveryPresetSelectionInfo")).toContainText("兼容方案");
  expect(await page.evaluate(()=>document.documentElement.scrollWidth-innerWidth)).toBeLessThanOrEqual(1);
  await page.locator("#discoveryPresetControls").screenshot({path:testInfo.outputPath("saved-screen-management.png")});
});

async function selectedMarkets(page) {
  return page.locator("#marketScanMarket").evaluate(node=>Array.from(node.selectedOptions,item=>item.value));
}

function presetApi(url,request,state) {
  const path=url.pathname; const method=request.method();
  if(path==="/api/market-scans/polling-identity")return {payload:marketScanPollingIdentityPayload(run(),run(),"official")};
  if(path.startsWith("/api/market-scans/latest"))return {payload:run()};
  if(path==="/api/market-scans/42/results")return {payload:{run:run(),items:[],total:0,page:1,page_size:Number(url.searchParams.get("page_size")),page_count:0}};
  if(path==="/api/discovery/presets" && method==="GET") {
    const page=Number(url.searchParams.get("page"));
    if(page===2 && state.failNext) {state.failNext=false;return {status:503,payload:{detail:"读取失败，请重试"}};}
    const sorted=[...state.records].sort((a,b)=>b.updated_at.localeCompare(a.updated_at)||b.id-a.id);
    return {payload:{items:sorted.slice((page-1)*100,page*100),total:sorted.length,page,page_size:100,page_count:Math.ceil(sorted.length/100)}};
  }
  if(path.endsWith("/apply")) {
    const item=state.records.find(row=>row.id===Number(path.split("/").at(-2)));
    return {payload:{preset:item,run_id:42,rule_version:"screen-test-v1",items:[],total:0,page:1,page_size:request.postDataJSON().page_size,page_count:0}};
  }
  if(path==="/api/discovery/runs/42/rank-changes")return {payload:{current_run_id:42,previous_run_id:null,current_rule_version:"screen-test-v1",
    previous_rule_version:null,comparable:false,reason:"no_previous_run",items:[],total:0,page:1,page_size:200,page_count:0}};
  if(path.startsWith("/api/discovery/presets") && ["POST","PUT"].includes(method)) {
    const body=request.postDataJSON(); state.writes.push({method,body});
    const id=method==="POST"?102:Number(path.split("/").at(-1));
    const previous=state.records.find(item=>item.id===id);
    const item={...preset(id),...body,revision:previous?previous.revision+1:1,updated_at:"99999999"};delete item.expected_revision;
    state.records=state.records.filter(row=>row.id!==id).concat(item); return {payload:item,status:method==="POST"?201:200};
  }
  return null;
}

function preset(id) {return {id,name:`方案${id}`,revision:1,schema_version:2,
  criteria:{market:["SH"],score:{min:80},...(id===13?{confidence:{max:80}}:{})},
  sort:[{field:"rank",order:"asc"}],column_view:"overview",created_at:"2026-09-07",updated_at:String(id).padStart(8,"0")};}
function run() {return {id:42,status:"success",trigger:"manual",mode:"official",rule_version:"screen-test-v1",as_of:"2026-09-04 16:00:00",
  data_date:"2026-09-04",quote_date:"2026-09-04",scope:"沪市 + 深市 + 北交所当前上市A股",total_count:1,excluded_count:0,
  processed_count:1,success_count:1,missing_count:0,skipped_count:0,retry_count:0,progress_pct:100,coverage_pct:100,
  created_at:"2026-09-04 16:00:00",updated_at:"2026-09-04 16:01:00",finished_at:"2026-09-04 16:01:00",
  snapshot_digest:"a".repeat(64),snapshot_seal_origin:"publication",snapshot_sealed_at:"2026-09-04 16:01:00"};}
