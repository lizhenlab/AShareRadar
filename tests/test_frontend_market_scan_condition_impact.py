"""Applied-query ownership and exact frozen condition impact UI behavior."""
from __future__ import annotations

import subprocess
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
SUPPORT = r'''
import assert from "node:assert/strict";
import fs from "node:fs";
import { createMarketScanScreeningController } from "./static/js/market-scan-screening-controller.js";
import { validateScreenEvaluation } from "./static/js/market-scan-screening-contracts.js";
import { conditionImpactContent } from "./static/js/market-scan-condition-impact.js";
import { screenSpecFromResultsQuery, screenSpecFromPreset, normalizedScreenSpecKey } from "./static/js/market-scan-screen-spec-source.js";
import { commitStandardScreenContext, commitDiscoveryScreenContext, clearAppliedScreenContext, readAppliedScreenContext, screenContextMatches, requireScreenEvidenceBinding } from "./static/js/market-scan-screen-context.js";
const BASE=JSON.parse(fs.readFileSync("tests/fixtures/market_scan_screen_evaluation_v2.json","utf8"));
const RUN={...BASE.evidence,id:BASE.evidence.run_id,updated_at:BASE.evidence.finished_at};
const QUERY=`/api/market-scans/${RUN.id}/results?status=success&sort=rank&order=asc&min_confidence=80&max_risk=50`;
function deferred(){let resolve,reject;const promise=new Promise((yes,no)=>{resolve=yes;reject=no;});return {promise,resolve,reject};}
async function flush(){for(let i=0;i<30;i++)await Promise.resolve();}
function harness(){
  const elements=new Map();
  const root={getElementById(id){
    if(!elements.has(id))elements.set(id,{ownerDocument:root,handlers:new Map(),dataset:{},textContent:"",innerHTML:"",hidden:false,open:true,value:"",disabled:false,
      setAttribute(key,value){this[key]=String(value);},removeAttribute(key){delete this[key];},
      addEventListener(type,handler){const list=this.handlers.get(type)||new Set();list.add(handler);this.handlers.set(type,list);},
      removeEventListener(type,handler){this.handlers.get(type)?.delete(handler);},dispatch(type){for(const handler of this.handlers.get(type)||[])handler({target:this});}});
    return elements.get(id);},querySelectorAll(){return [];}};
  const get=id=>root.getElementById(id), wrap=get("marketScanTableWrap");wrap.dataset.marketScanRunId=String(RUN.id);
  const commit=(run=RUN,query=QUERY,total=2,extra={})=>{wrap.dataset.marketScanRunId=String(run.id);commitStandardScreenContext(wrap,{run,total,items:[],...extra},query);};
  commit();return {root,get,wrap,commit,elements};
}
function responses(run=RUN){
  const evidence={...BASE.evidence,...Object.fromEntries(Object.keys(BASE.evidence).map(key=>[key,key==="run_id"?run.id:run[key]]))};
  const evaluation=structuredClone(BASE);evaluation.evidence=evidence;
  const breadth={schema_version:"market-scan-breadth-v1",evidence,canonical_digest:"a".repeat(64),population:{total:6,by_status:{success:6},by_market:{SH:6}},
    score:{present_count:6,missing_count:0,min:0,max:0,mean:0,percentiles:{p10:0,p25:0,p50:0,p75:0,p90:0},bins:Array.from({length:10},(_,i)=>({lower:i*10,upper:(i+1)*10,count:i===0?6:0}))},
    change:{advancing:0,flat:6,declining:0,missing:0},industries:[{industry:null,count:6,score_present_count:6,average_score:0}]};
  const delta={schema_version:"market-scan-delta-v1",status:"unavailable",unavailable_reason:"previous_same_cohort_not_found",current:{...evidence},previous:null,canonical_digest:"a".repeat(64),
    cohort:{mode:run.mode,scope:run.scope,rule_version:run.rule_version},summary:{previous_present_count:0,current_present_count:0,compared_symbol_count:0,evidence_detail_scope:"top100_union",evidence_change_reason_counts:[]},top_buckets:[],rank_score_changes:[],exposure_changes:[],evidence_changes:[]};
  delete delta.current.quote_date;
  return {breadth,evaluation,delta};
}
function responseFor(url, data=responses()){return structuredClone(url.endsWith("breadth")?data.breadth:url.endsWith("delta")?data.delta:data.evaluation);}
'''


def run_node(script: str) -> None:
    subprocess.run(["node", "--input-type=module", "-e", SUPPORT + script], cwd=ROOT, check=True, timeout=30)


def test_query_and_full_compatibility_preset_keep_zero_false_and_unrepresented_bounds() -> None:
    run_node(r'''
      const query=`/api/market-scans/${RUN.id}/results?status=all&market=SH&market=SZ&industry=银行&is_st=false&min_score=0&max_score=80&sort=risk&order=asc`;
      const spec=screenSpecFromResultsQuery(query,RUN.id);
      assert.equal(spec.status,null); assert.equal(spec.is_st,false); assert.equal(spec.ranges.score.min,0);
      assert.deepEqual(spec.markets,["SH","SZ"]); assert.deepEqual(spec.sort,[{field:"risk",order:"asc"}]);
      const preset={criteria:{market:["SH","SZ"],is_st:false,confidence:{max:80},risk:{min:0,max:50},tradability:{max:90}},sort:[{field:"quality",order:"desc"}]};
      const full=screenSpecFromPreset(preset);
      assert.equal(full.ranges.confidence.max,80);assert.equal(full.ranges.risk.min,0);assert.equal(full.ranges.tradability.max,90);
      assert.deepEqual(full.sort,[{field:"data_quality_score",order:"desc"}]);
      assert.equal(normalizedScreenSpecKey(full),normalizedScreenSpecKey({...full,ranges:{...full.ranges,score:null},keyword:null}));
      assert.throws(()=>screenSpecFromResultsQuery(`${QUERY}&unknown=1`,RUN.id),/不支持/);
      assert.throws(()=>screenSpecFromResultsQuery(QUERY,RUN.id+1),/批次/);
    ''')


def test_real_v2_fixture_independent_counts_missing_and_zero_render_without_extra_requests() -> None:
    run_node(r'''
      validateScreenEvaluation(BASE,RUN.id);
      const h=harness(), calls=[];
      const controller=createMarketScanScreeningController({root:h.root,fetcher:async(url,options)=>{calls.push({url,options});return responseFor(url);}});
      await controller.open();
      assert.equal(calls.length,3);assert.equal(controller.state.loaded,true);
      assert.equal(normalizedScreenSpecKey(JSON.parse(calls[1].options.body).spec),normalizedScreenSpecKey(BASE.spec));
      assert.match(h.get("marketScanScreeningSpec").innerHTML,/80/);
      assert.match(h.get("marketScanScreeningSpec").innerHTML,/50/);
      assert.match(h.get("marketScanScreeningEvaluation").innerHTML,/原值：0/);
      assert.match(h.get("marketScanScreeningEvaluation").innerHTML,/不可用（证据缺失）/);
      assert.match(h.get("marketScanScreeningContextLabel").textContent,/已应用/);
      h.get("marketScanConfidenceMin").value="99";
      h.get("marketScanFilters").dispatch("submit");
      await controller.open();assert.equal(calls.length,3,"Draft controls cannot silently change frozen explanation");
      assert.match(h.get("marketScanScreeningSpec").innerHTML,/80/);
      controller.dispose();
    ''')


@pytest.mark.parametrize("mutation", [
    "value.condition_impacts.pop()", "value.condition_impacts.reverse()",
    'value.condition_impacts[0].condition_code="unknown"',
    "value.condition_impacts[1].additional_count=-1", "value.condition_impacts[1].additional_count=1.5",
    "value.condition_impacts[1].matched_without_condition++", "value.condition_impacts[1].missing_additional_count=3",
    "value.condition_impacts[1].examples.pop()", "value.condition_impacts[1].examples[0].run_id++",
    'value.condition_impacts[1].examples[0].symbol="600000.SZ"',
    "value.condition_impacts[1].examples[0].observed_value=NaN",
    "value.condition_impacts[1].examples[0].observed_value=true",
    "value.condition_impacts[1].examples[0].observed_value=90",
    "value.condition_impacts[1].examples[0].missing=true",
    "value.condition_impacts[1].examples[1].observed_value=0",
    "value.condition_impacts[1].examples[1]=value.condition_impacts[1].examples[0]",
    "value.exclusion_reasons[0].count=1",
    "value.exclusion_reasons[0].missing_count=0",
])
def test_condition_impact_contract_rejects_inconsistent_totals_and_examples(mutation: str) -> None:
    run_node(f'''
      const value=structuredClone(BASE);{mutation};
      assert.throws(()=>validateScreenEvaluation(value,RUN.id));
    ''')


@pytest.mark.parametrize("mutation", [
    'value.evidence.snapshot_digest="f".repeat(64)', 'value.evidence.rule_version="wrong"',
    'value.spec.sort=[{field:"score",order:"desc"}]',
    'value.spec.ranges.confidence.min=79',
])
def test_controller_binds_returned_evidence_and_spec_to_applied_query(mutation: str) -> None:
    run_node(f'''
      const h=harness(); const controller=createMarketScanScreeningController({{root:h.root,fetcher:async(url)=>{{
        const value=responseFor(url);if(url.endsWith("evaluate")){{{mutation};}}return value;
      }}}});
      await controller.open();assert.equal(controller.state.loaded,false);
      assert.match(h.get("marketScanScreeningEvaluation").innerHTML,/不一致/);
      assert(!h.get("marketScanScreeningEvaluation").innerHTML.includes("data-condition-impact"));
      controller.dispose();
    ''')


@pytest.mark.parametrize("policy", ["probability", "active_v6", "ready_v6", "ranking_policy_v6"])
def test_unsupported_applied_query_has_visible_reason_and_never_evaluates(policy: str) -> None:
    options = {
        "probability": '(RUN,`${QUERY}&min_upside_probability=0.6`,0)',
        "active_v6": '(RUN,QUERY,0,{production_ranking:{status:"active",score_rule_version:"full-market-score-v6"}})',
        "ready_v6": '(RUN,QUERY,0,{production_ranking:{status:"ready"}})',
        "ranking_policy_v6": '(RUN,QUERY,0,{ranking_policy:"v6"})',
    }[policy]
    run_node(f'''
      const h=harness();h.commit{options};const calls=[];
      assert.equal(h.get("marketScanExplainEmpty").disabled,true);
      assert.equal(h.get("marketScanExplainUnavailable").hidden,false);
      assert.match(h.get("marketScanExplainUnavailable").textContent,/概率/);
      const controller=createMarketScanScreeningController({{root:h.root,fetcher:async(url)=>{{calls.push(url);return responseFor(url);}}}});
      await controller.open();assert.equal(calls.length,2);assert(!calls.some(url=>url.endsWith("evaluate")));
      assert.match(h.get("marketScanScreeningEvaluation").innerHTML,/概率/);controller.dispose();
    ''')


def test_context_same_batch_preset_definition_revision_and_failed_application_clear_explanation() -> None:
    run_node(r'''
      const h=harness(), preset={id:7,revision:1,name:"完整兼容方案",criteria:{confidence:{min:80,max:95},risk:{max:50}},sort:[{field:"rank",order:"asc"}]};
      const payload={run_id:RUN.id,rule_version:RUN.rule_version,total:2,preset};
      commitDiscoveryScreenContext(h.wrap,RUN,payload,preset);
      const previous=readAppliedScreenContext(h.wrap); assert.equal(previous.spec.ranges.confidence.max,95);
      assert.match(previous.source.label,/修订 1/);
      commitDiscoveryScreenContext(h.wrap,RUN,{...payload,preset:{...preset,revision:2}},{...preset,revision:2});
      assert.equal(screenContextMatches(h.wrap,previous),false);
      const altered={...preset,criteria:{confidence:{min:1}}};
      commitDiscoveryScreenContext(h.wrap,RUN,{...payload,preset:altered},preset);
      assert.equal(readAppliedScreenContext(h.wrap).spec,null);
      assert.match(readAppliedScreenContext(h.wrap).error,/提交的方案/);
      clearAppliedScreenContext(h.wrap);assert.equal(readAppliedScreenContext(h.wrap),null);
      assert.equal(h.get("marketScanExplainEmpty").hidden,true);
    ''')


def test_full_snapshot_identity_a_b_a_aborts_old_request_and_ignores_late_response() -> None:
    run_node(r'''
      const h=harness(), pending=deferred(), calls=[]; let held=false;
      const controller=createMarketScanScreeningController({root:h.root,fetcher:async(url,options)=>{
        calls.push({url,options});if(url.endsWith("evaluate")&&!held){held=true;return pending.promise;}return responseFor(url);
      }});
      const old=controller.open();await flush();assert.equal(calls.length,2);
      const original=readAppliedScreenContext(h.wrap);
      clearAppliedScreenContext(h.wrap);
      h.commit({...RUN,snapshot_digest:"f".repeat(64)});
      h.commit(RUN);
      assert.equal(calls[1].options.signal.aborted,true);
      assert.equal(screenContextMatches(h.wrap,original),false,"A-B-A must not recover an old generation");
      await controller.open();assert.equal(controller.state.loaded,true);
      pending.resolve(BASE);await old;
      assert.equal(controller.state.loaded,true);assert.match(h.get("marketScanScreeningEvaluation").innerHTML,/单条件独立影响/);
      controller.dispose();
    ''')


def test_dispose_unsubscribes_source_and_dom_events_and_hides_failed_pending_context() -> None:
    run_node(r'''
      const h=harness(), pending=deferred();let signal,calls=0;
      const controller=createMarketScanScreeningController({root:h.root,fetcher:async(_url,options)=>{signal=options.signal;calls++;return pending.promise;}});
      const reading=controller.open();controller.dispose();assert.equal(signal.aborted,true);
      for(const element of h.elements.values())for(const listeners of element.handlers.values())assert.equal(listeners.size,0);
      h.commit({...RUN,snapshot_digest:"f".repeat(64)});h.get("marketScanScreeningRefresh").dispatch("click");
      pending.resolve(responses().breadth);await reading;assert.equal(calls,1);
      clearAppliedScreenContext(h.wrap);
      const next=createMarketScanScreeningController({root:h.root,fetcher:async()=>{throw new Error("must not request absent context");}});
      await next.open();assert.match(h.get("marketScanScreeningFeedback").textContent,/未应用的表单草稿/);next.dispose();
    ''')


def test_impact_examples_escape_untrusted_names_and_preserve_missing_as_missing() -> None:
    run_node(r'''
      const impacts=structuredClone(BASE.condition_impacts);impacts[1].examples[0].name='<img src=x onerror="alert(1)">';
      const html=conditionImpactContent(impacts);assert(!html.includes("<img"));assert(html.includes("&lt;img"));
      assert(html.includes("原值：0"));assert(html.includes("不可用（证据缺失）"));
      const h=harness(),context=readAppliedScreenContext(h.wrap),delta={...BASE.evidence};delete delta.quote_date;
      requireScreenEvidenceBinding(delta,context,undefined,true);
      assert.throws(()=>requireScreenEvidenceBinding(delta,context),/完整冻结身份/);
    ''')


def test_screen_producers_keep_order_and_never_revive_a_b_a_or_a_cancelled_probe() -> None:
    run_node(r'''
      const {beginScreenProducer,ownsScreenProducer,screenProducer,ownsScreenProbe,releaseScreenProducer,suspendScreenProducer,presetOwnsScreen}=await import("./static/js/market-scan-screen-producer.js");
      const h=harness();let cancelled=0;
      const first=beginScreenProducer(h.wrap,"standard",RUN);
      const probe={producerBefore:screenProducer(h.wrap)};
      const preset=beginScreenProducer(h.wrap,"preset",RUN,()=>cancelled++);
      assert(!ownsScreenProducer(h.wrap,first));assert(!ownsScreenProbe(h.wrap,probe));
      assert(presetOwnsScreen(h.wrap,RUN));assert(!presetOwnsScreen(h.wrap,{...RUN,snapshot_digest:"f".repeat(64)}));
      const next=beginScreenProducer(h.wrap,"standard",RUN);
      assert.equal(cancelled,1);assert(ownsScreenProducer(h.wrap,next));
      assert(!ownsScreenProducer(h.wrap,first));assert(!ownsScreenProducer(h.wrap,preset));
      assert(!ownsScreenProbe(h.wrap,probe),"Returning to standard/run A cannot revive its earlier probe");
      releaseScreenProducer(h.wrap,"preset");assert(ownsScreenProducer(h.wrap,next));
      h.commit();suspendScreenProducer(h.wrap);assert(ownsScreenProducer(h.wrap,next));
      clearAppliedScreenContext(h.wrap);suspendScreenProducer(h.wrap);assert(!ownsScreenProducer(h.wrap,next));
      assert.equal(readAppliedScreenContext(h.wrap),null);
    ''')
