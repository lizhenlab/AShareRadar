from __future__ import annotations

import json
from pathlib import Path
import subprocess

from app.models.strategy_lab import StrategySpecInput
from app.services.strategy_compiler import compile_strategy_spec
from tests.test_frontend_strategy_recovery import _HARNESS


ROOT = Path(__file__).resolve().parents[1]


def test_input_immediately_blocks_saved_execution_and_new_schedule() -> None:
    _node(r'''
      const controller=await ready();
      input("strategyStockCount","10");
      assert.equal(element("strategyExecuteLatest").disabled,true);
      assert.equal(element("strategyCreateSchedule").disabled,true);
      await controller.execute("latest_scan"); await click("strategyCreateSchedule");
      assert.equal(writes.length,0,"draft edits executed the previously saved version");
      assert.match(element("strategyDraftStatus").textContent,/未保存|编译/);
    ''')


def test_compiled_changes_require_save_before_the_new_version_can_execute() -> None:
    _node(r'''
      const controller=await ready(); await edit("strategyStockCount","10");
      assert.equal(element("strategySave").disabled,false);
      assert.equal(element("strategyExecuteLatest").disabled,true);
      await click("strategySave");
      assert.equal(controller.state.strategy.strategy_version,2);
      assert.equal(element("strategyExecuteLatest").disabled,false);
      await controller.execute("latest_scan");
      assert.equal(writes.at(-1).body.revision,2);
      assert.match(element("strategyDraftStatus").textContent,/v2/);
    ''')


def test_reverting_to_the_saved_normalized_spec_restores_execution() -> None:
    _node(r'''
      await ready(); await edit("strategyStockCount","10");
      assert.equal(element("strategyExecuteLatest").disabled,true);
      await edit("strategyStockCount","20");
      assert.equal(element("strategyExecuteLatest").disabled,false);
      assert.equal(writes.length,0);
    ''')


def test_blurring_an_already_compiled_input_does_not_cancel_the_save_click() -> None:
    _node(r'''
      const controller=await ready(); await edit("strategyStockCount","10");
      change("strategyStockCount");
      assert.equal(element("strategySave").disabled,false,"blur invalidated the unchanged compiled input");
      await click("strategySave"); assert.equal(controller.state.strategy.strategy_version,2);
    ''')


def test_late_compile_success_cannot_replace_the_latest_preview() -> None:
    _node(r'''
      const old=deferred();
      const controller=await ready((url,options)=>url.endsWith("/compile") && JSON.parse(options.body).spec.portfolio_constraints.stock_count===10 ? old.promise : undefined);
      input("strategyStockCount","10"); change("strategyStockCount"); await settle();
      await edit("strategyStockCount","30");
      old.resolve(compiled({...spec,portfolio_constraints:{...spec.portfolio_constraints,stock_count:10}}));
      await settle();
      assert.equal(controller.state.spec.portfolio_constraints.stock_count,30);
      assert.equal(element("strategySave").disabled,false);
    ''')


def test_late_compile_failure_cannot_disable_the_latest_valid_draft() -> None:
    _node(r'''
      let rejectOld; const old=new Promise((_resolve,reject)=>{rejectOld=reject;});
      await ready((url,options)=>url.endsWith("/compile") && JSON.parse(options.body).spec.portfolio_constraints.stock_count===10 ? old : undefined);
      input("strategyStockCount","10"); change("strategyStockCount"); await settle();
      await edit("strategyStockCount","30");
      rejectOld(new Error("旧编译失败")); await settle();
      assert.equal(element("strategySave").disabled,false);
      assert.doesNotMatch(status().textContent,/旧编译失败/);
    ''')


def test_compile_response_cannot_change_the_submitted_stock_count() -> None:
    _node(r'''
      await ready((url,options)=>url.endsWith("/compile") && JSON.parse(options.body).spec.portfolio_constraints.stock_count===10 ? compiled(spec) : undefined);
      await edit("strategyStockCount","10");
      assert.equal(element("strategyExecuteLatest").disabled,true,"mismatched compile response enabled the old saved version");
      assert.equal(element("strategySave").disabled,true);
      assert.match(status().textContent,/编译.*不一致/);
    ''')


def test_real_compiler_ordering_and_objective_normalization_are_accepted() -> None:
    source = StrategySpecInput.model_validate({
        "name": "归一化边界", "profile": "custom",
        "universe": {"boards": ["beijing", "sh_main"]},
        "hard_filters": [
            {"field": "risk", "operator": "lte", "value": 80.0},
            {"field": "amount", "operator": "gte", "value": 100_000_000.0},
        ],
        "objectives": {"alpha_1d": .1, "alpha_5d": .2, "alpha_20d": .3,
                       "confidence": .1, "risk": .1, "tradability": .1},
    })
    payload = compile_strategy_spec(source).model_dump(mode="json")
    _node(f'''
      const {{validateCompiledStrategy}}=await import("./static/js/strategy-draft-state.js");
      const actualCompiled={json.dumps(payload, ensure_ascii=False)};
      const original={json.dumps(source.model_dump(mode="json"), ensure_ascii=False)};
      assert.equal(validateCompiledStrategy(actualCompiled,original),actualCompiled);
      const changed=structuredClone(actualCompiled); changed.normalized_spec.hard_filters[0].value=1;
      assert.throws(()=>validateCompiledStrategy(changed,original),/编译.*不一致/);
    ''')


def test_save_acknowledgement_preserves_edits_made_while_the_write_is_pending() -> None:
    _node(r'''
      const pending=deferred(); let sent;
      const controller=await ready((url,options)=>{
        if(options.method==="PUT") {sent=JSON.parse(options.body); return pending.promise;} return undefined;
      });
      await edit("strategyStockCount","10"); const saving=click("strategySave"); await settle();
      input("strategyStockCount","30"); change("strategyStockCount");
      pending.resolve(saved(sent.spec,2)); await saving; await settle();
      assert.equal(controller.state.strategy.strategy_version,2);
      assert.equal(controller.state.strategy.spec.portfolio_constraints.stock_count,10);
      assert.equal(element("strategyStockCount").value,"30");
      assert.equal(element("strategyExecuteLatest").disabled,true);
      assert.match(element("strategyDraftStatus").textContent,/未保存/);
    ''')


def test_save_rejects_a_mismatched_confirmed_fingerprint() -> None:
    _node(r'''
      const controller=await ready((_url,options)=>options.method==="PUT" ? {...saved(JSON.parse(options.body).spec,2),fingerprint:"f".repeat(64)} : undefined);
      await edit("strategyStockCount","10");
      assert.equal(await click("strategySave"),null);
      assert.equal(controller.state.strategy.strategy_version,1);
      assert.equal(element("strategyExecuteLatest").disabled,true);
      assert.match(status().textContent,/回执|指纹/);
    ''')


def test_save_cannot_submit_an_input_that_has_not_finished_compiling() -> None:
    _node(r'''
      const controller=await ready(); input("strategyStockCount","10");
      await click("strategySave");
      assert.equal(writes.length,0);
      assert.equal(controller.state.strategy.strategy_version,1);
    ''')


def test_unknown_creation_requires_explicitly_loading_a_saved_strategy_before_retry() -> None:
    _node(r'''
      let posts=0;
      const controller=makeController(async(url,options={})=>{
        if(url.endsWith("/compile")) return compiled(JSON.parse(options.body).spec);
        if(options.method) {posts+=1; throw new Error("保存响应超时");}
        if(url.endsWith("/strategies/7")) return saved(spec);
        if(url.includes("/evidence?")) return null;
        return {items:url.includes("/strategies?")?[saved(spec)]:[],total:1};
      });
      controller.state.spec=structuredClone(spec); controller.state.compileExecutable=true;
      await click("strategySave"); await click("strategySave");
      assert.equal(posts,1,"unknown create was repeated without identifying the saved result");
      assert.match(element("strategyDraftStatus").textContent,/保存结果待核对/);
      await controller.loadStrategies(); assert.equal(element("strategySave").disabled,true,"list refresh implicitly claimed a strategy");
      element("strategySavedSelect").value="7"; await click("strategyLoad");
      assert.equal(controller.state.strategy.strategy_id,7);
      assert.equal(element("strategySave").disabled,false);
    ''')


def test_explicit_rejection_allows_retry_but_unknown_update_keeps_revision_for_review() -> None:
    _node(r'''
      let attempts=0;
      const controller=await ready((_url,options)=>{
        if(options.method!=="PUT") return undefined;
        attempts+=1; if(attempts===1) throw Object.assign(new Error("字段被拒绝"),{status:422});
        throw new Error("更新响应超时");
      });
      await edit("strategyStockCount","10"); await click("strategySave");
      assert.equal(element("strategySave").disabled,false,"confirmed rejection should allow corrections and retry");
      await click("strategySave"); await click("strategySave");
      assert.equal(attempts,2);
      assert.equal(controller.state.strategy.revision,1);
      assert.match(element("strategyDraftStatus").textContent,/保存结果待核对/);
    ''')


def test_editing_during_schedule_mutation_compiles_after_that_write_finishes() -> None:
    _node(r'''
      const pending=deferred(); let enabled=true;
      const task=()=>({schedule_id:1,strategy_id:7,strategy_version:1,strategy_fingerprint:"a".repeat(64),
        cadence:"daily_after_close",mode:"official",notional_cash_cny:100000,enabled,last_execution_id:null,last_market_scan_run_id:null});
      const controller=await ready((url,options)=>{
        if(options.method==="PATCH") return pending.promise;
        if(url.includes("/schedules?")) return {items:[task()],page:1,page_size:20,page_count:1,total:1};
        return undefined;
      });
      element("strategyScheduleManager").open=true; await controller.scheduleManager.refresh();
      const write=element("strategyScheduleRows").handlers.get("click")({target:{closest:()=>({dataset:{strategyScheduleToggle:"1"}})}});
      await edit("strategyStockCount","10");
      enabled=false; pending.resolve(task()); await write; await settle();
      assert.equal(element("strategySave").disabled,false,"pending draft was never compiled after the independent task write");
      assert.equal(element("strategyExecuteLatest").disabled,true);
    ''')


def test_paper_order_draft_displays_every_order_and_its_frozen_source() -> None:
    _node(r'''
      const {renderSimulationPlan}=await import("./static/js/strategy-lab-view.js");
      const {validateSimulationPlan}=await import("./static/js/strategy-lab-contracts.js");
      const plan=paperPlan(25); validateSimulationPlan(plan);
      renderSimulationPlan({strategyLifecycleContent:element("strategyLifecycleContent")},plan);
      const html=element("strategyLifecycleContent").innerHTML;
      assert.equal((html.match(/data-strategy-order=/g)||[]).length,25,"orders were omitted from the reviewable result");
      assert.match(html,/600024.SH/); assert.match(html,/策略 #7 v2/); assert.match(html,/执行 #9/);
      assert.match(html,/不会.*复盘模拟账户/); assert.match(html,/估算金额/); assert.match(html,/往返成本/);
      assert.match(html,/A股 T\+1/); assert.doesNotMatch(html,/<script>/);
    ''')


def test_paper_order_draft_rejects_an_unrelated_execution_context() -> None:
    _node(r'''
      const {validateSimulationPlan}=await import("./static/js/strategy-lab-contracts.js");
      assert.throws(()=>validateSimulationPlan(paperPlan(),{...paperPlan(),execution_id:99}),/来源|执行/);
    ''')


def test_paper_order_draft_rejects_invalid_quantity_and_duplicate_symbols() -> None:
    _node(r'''
      const {validateSimulationPlan}=await import("./static/js/strategy-lab-contracts.js");
      const bad=paperPlan(); bad.orders[0].target_quantity=-100;
      assert.throws(()=>validateSimulationPlan(bad),/委托|数量/);
      const repeated=paperPlan(); repeated.orders.push(repeated.orders[0]);
      assert.throws(()=>validateSimulationPlan(repeated),/重复/);
    ''')


def _node(body: str) -> None:
    harness = _HARNESS.replace(
        'if (selector.startsWith("[data-strategy-board]")) return boards;',
        'if (selector.startsWith("#strategyEditor ")) return [element("strategyStockCount"), element("strategyName")];\n'
        '    if (selector.startsWith("[data-strategy-board]")) return boards;',
    )
    result = subprocess.run(
        ["node", "--input-type=module", "-e", harness + _SETUP + body,
         json.dumps(StrategySpecInput(name="草案身份测试").model_dump(mode="json"), ensure_ascii=False)],
        cwd=ROOT, capture_output=True, text=True, check=False,
    )
    assert result.returncode == 0, result.stderr


_SETUP = r'''
const writes=[];
function fp(value) {return value.portfolio_constraints.stock_count===20?"a".repeat(64):"b".repeat(64);}
function compiled(value) {return {normalized_spec:structuredClone(value),fingerprint:fp(value),warnings:[],
  execution_plan:{executable:true,will_start_scan:false,expressions:[],board_labels:[],blocked_reasons:[]}};}
function saved(value,revision=1) {return {...strategy(7,revision),spec:structuredClone(value),fingerprint:fp(value)};}
async function ready(override=()=>undefined) {
  const controller=makeController(async(url,options={})=>{
    const response=override(url,options); if(response!==undefined) return response;
    if(url.endsWith("/compile")) return compiled(JSON.parse(options.body).spec);
    if(options.method) {const body=JSON.parse(options.body); writes.push({url,body});
      return url.endsWith("/executions")?draft():saved(body.spec,2);}
    if(url.endsWith("/strategies/7")) return saved(spec);
    if(url.includes("/candidates?")) return candidatePage(1);
    if(url.includes("/evidence?")) return null;
    return {items:[],total:0};
  });
  element("strategySavedSelect").value="7"; await click("strategyLoad"); return controller;
}
function input(id,value) {element(id).value=value; element(id).handlers.get("input")?.();}
function change(id) {element(id).handlers.get("change")?.();}
async function settle() {for(let i=0;i<15;i+=1) await new Promise(resolve=>setImmediate(resolve));}
async function edit(id,value) {input(id,value); change(id); await settle();}
function paperPlan(count=1) {return {plan_id:3,execution_id:9,strategy_id:7,strategy_version:2,
  strategy_fingerprint:"a".repeat(64),execution_fingerprint:"b".repeat(64),cost_rule_fingerprint:"c".repeat(64),
  plan_digest:"d".repeat(64),rule_version:"research-v1",data_as_of:"2026-09-04T15:00:00+08:00",status:count?"draft":"no_trade",disclaimers:[],
  orders:Array.from({length:count},(_,i)=>({symbol:`${600000+i}.SH`,name:i===0?"<script>注入</script>":`样本${i}`,board_label:"沪市主板",research_side:"paper_buy",
    target_weight:.02,target_quantity:100,estimated_gross_amount_cny:1000,estimated_round_trip_cost_cny:5,earliest_exit_policy:"A股 T+1；计划持有5日",constraint_notes:["资金约束"]}))};}
'''
