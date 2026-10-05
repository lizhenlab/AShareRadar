"""Frozen candidate comparison contracts, ownership and local export behavior."""
from __future__ import annotations

import subprocess
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
SUPPORT = r'''
import assert from "node:assert/strict";
import fs from "node:fs";
import { createMarketScanComparisonController } from "./static/js/market-scan-comparison-controller.js";
import { comparisonRunKey, validateMarketScanComparison } from "./static/js/market-scan-comparison-contracts.js";
import { comparisonTable, createMarketScanComparisonView } from "./static/js/market-scan-comparison-view.js";
const fixture = JSON.parse(fs.readFileSync("tests/fixtures/market_scan_comparison_v1.json", "utf8"));
const originalRun = {...fixture.evidence, id: fixture.evidence.run_id, updated_at: fixture.evidence.finished_at};
function reply() { let resolve, reject; const promise = new Promise((yes,no) => { resolve=yes; reject=no; }); return {promise,resolve,reject}; }
function harness(request = async () => structuredClone(fixture)) {
  let run = structuredClone(originalRun), active = true, latest, handlers;
  const saved = [], requests = [];
  const view = {bind(value) {handlers=value; return () => {};}, render(value) { latest=structuredClone(value); }, renderRows() {}, save(value) { saved.push(value); }};
  const controller=createMarketScanComparisonController({view,getRun:()=>run,isActive:()=>active,request:async (...args)=>{ requests.push(args); return request(...args); }});
  return {controller, saved, requests, get latest(){return latest;}, get handlers(){return handlers;}, set run(value){run=value;}, set active(value){active=value;}, select(){fixture.requested_symbols.forEach(symbol=>controller.toggle(symbol,run.id));}};
}
'''


def run_node(script: str) -> None:
    subprocess.run(["node", "--input-type=module", "-e", SUPPORT + script], cwd=ROOT, check=True, timeout=30)


def test_comparison_bounded_request_selection_export_and_immutable_response() -> None:
    run_node(r'''
      const response=structuredClone(fixture), h=harness(async()=>response);
      assert.equal(h.controller.export(),false);
      assert.equal(h.controller.toggle("bad",originalRun.id),false);
      assert.equal(h.controller.toggle(fixture.requested_symbols[0],originalRun.id+1),false);
      h.select(); h.handlers.rowsChanged(); h.handlers.rowsChanged();
      assert.equal(h.requests.length,0);
      await h.controller.compare();
      assert.deepEqual(JSON.parse(h.requests[0][1].body),{symbols:fixture.requested_symbols,expected_snapshot_digest:fixture.evidence.snapshot_digest});
      assert.equal(h.requests[0][0],`/api/market-scans/${originalRun.id}/compare`);
      assert.equal(h.requests[0][1].method,"POST");
      assert.equal(h.controller.export(),true);
      assert.deepEqual(h.saved,[fixture]);
      response.items[0].name="later mutation";
      h.controller.export(); assert.deepEqual(h.saved[1],fixture);
      h.controller.toggle("600998.SH",originalRun.id); h.controller.toggle("600999.SH",originalRun.id);
      assert.equal(h.controller.toggle("600997.SH",originalRun.id),false);
      assert.equal(h.latest.symbols.length,4);
      assert.equal(h.controller.export(),false); assert.equal(h.requests.length,1);
      h.controller.clear(); assert.equal(h.latest.symbols.length,0);
      assert.equal(h.latest.payload,null);
    ''')


@pytest.mark.parametrize("mutation", [
    'value.items.pop()', 'value.items.reverse()', 'value.requested_symbols.reverse()',
    'value.items[0].run_id++', 'value.items[0].symbol="600998.SH"',
    'value.items[0].code="999999"', 'value.items[0].market="BJ"',
    'value.schema_version="future"', 'value.audit_only=false', 'value.ranking_basis="current"',
    'value.evidence.snapshot_digest="b".repeat(64)', 'value.evidence.run_id++',
    'value.evidence.rule_version="other"', 'value.evidence.quote_date="2000-01-01"',
    'value.evidence.finished_at="2000-01-01 00:00:00"', 'value.evidence.mode="intraday"',
    'value.evidence.scope="partial"', 'value.evidence.status="failed"',
    'value.evidence.snapshot_sealed_at="2000-01-01 00:00:00"',
    'value.canonical_digest="ABC"', 'value.action_source_eligible=null',
    'delete value.items[0].quote_source', 'value.items[0].confidence="0"',
    'value.items[0].risk=NaN', 'value.items[0].raw_score=Infinity',
    'value.items[0].score=1.5', 'value.items[0].score=101',
    'value.items[0].price=0', 'value.items[0].turnover_rate=-1',
    'value.items[0].degradation_reasons=[null]', 'value.items[0].quote_fallback_used=0',
    'value.items[0].status="missing"', 'value.items[0].extra="unsupported"',
    'value.extra="unsupported"', 'value.items[0].rank=null', 'value.items[0].score=null',
    'value.items[0].raw_score=null', 'value.items[0].price=null',
    'value.items[0].quote_source=""', 'value.items[0].kline_source=null',
    'value.items[0].quote_timestamp=null', 'value.items[0].quote_observed_at=null',
    'value.items[0].data_date="2000-01-01"', 'value.items[0].error="failure"',
    'value.items[0].adjustment_mode="hfq"',
])
def test_comparison_rejects_malformed_or_unbound_responses(mutation: str) -> None:
    run_node(f'''
      const value=structuredClone(fixture); {mutation};
      assert.throws(()=>validateMarketScanComparison(value,originalRun,fixture.requested_symbols),/候选对比校验失败/);
    ''')


def test_comparison_batch_identity_same_id_new_digest_mode_and_same_batch_refresh() -> None:
    run_node(r'''
      const h=harness(); h.select(); await h.controller.compare();
      h.run=structuredClone(originalRun); h.controller.sync();
      assert.equal(h.latest.symbols.length,2); assert.equal(h.controller.export(),true);
      for (const key of ["snapshot_digest","mode","rule_version","finished_at","snapshot_sealed_at","updated_at"]) {
        h.run=structuredClone(originalRun); h.controller.sync(); h.select();
        h.run={...originalRun,[key]:key==="snapshot_digest"?"f".repeat(64):key==="mode"?"intraday":"changed"};
        h.controller.sync(); assert.equal(h.latest.symbols.length,0,key); assert.equal(h.controller.export(),false,key);
      }
      h.run=null; h.controller.sync(); assert.equal(h.latest.run,null);
      assert.equal(comparisonRunKey({...originalRun,snapshot_digest:null}),null);
    ''')


def test_old_response_and_old_failure_cannot_replace_new_a_b_a_result() -> None:
    run_node(r'''
      for (const fail of [false,true]) {
        const old=reply(), fresh=reply(); let count=0;
        const h=harness(()=>++count===1?old.promise:fresh.promise); h.select();
        const oldRead=h.controller.compare();
        h.run={...originalRun,id:originalRun.id+1}; h.controller.sync();
        h.run=structuredClone(originalRun); h.controller.sync(); h.select();
        const newRead=h.controller.compare();
        assert.equal(h.requests[0][1].signal.aborted,true);
        assert.equal(h.controller.export(),false);
        const current=structuredClone(fixture); current.items[0].name="new response";
        fresh.resolve(current); await newRead;
        if(fail) old.reject(new Error("stale failure")); else old.resolve(fixture);
        await oldRead;
        assert.equal(h.latest.payload.items[0].name,"new response");
        assert.equal(h.latest.busy,false); assert.equal(h.controller.export(),true);
        assert.equal(h.saved[0].items[0].name,"new response");
      }
    ''')


@pytest.mark.parametrize("invalidate", ["clear", "selection", "hidden", "dispose"])
def test_pending_comparison_is_aborted_by_its_owner(invalidate: str) -> None:
    action = {
        "clear": "h.controller.clear()",
        "selection": 'h.controller.toggle("600999.SH",originalRun.id)',
        "hidden": 'h.active=false; h.controller.abort()',
        "dispose": "h.controller.dispose()",
    }[invalidate]
    run_node(f'''
      const pending=reply(), h=harness(()=>pending.promise); h.select();
      const read=h.controller.compare(); {action};
      assert.equal(h.requests[0][1].signal.aborted,true);
      pending.resolve(fixture); await read;
      assert.equal(h.latest.payload,null); assert.equal(h.latest.busy,false);
      assert.equal(h.controller.export(),false); assert.equal(h.saved.length,0);
    ''')


def test_new_failure_clears_previous_valid_result_and_does_not_allow_export() -> None:
    run_node(r'''
      let calls=0;
      const h=harness(async()=>{if(++calls===1)return structuredClone(fixture); throw new Error("snapshot mismatch");});
      h.select(); await h.controller.compare(); assert.equal(h.controller.export(),true);
      await h.controller.compare();
      assert.equal(h.latest.payload,null); assert.equal(h.latest.busy,false);
      assert.equal(h.controller.export(),false); assert.match(h.latest.message,/snapshot mismatch/);
      assert.equal(h.saved.length,1);
    ''')


def test_comparison_differences_missing_zero_and_xss_keep_original_semantics() -> None:
    run_node(r'''
      const value=structuredClone(fixture);
      value.items[0].name='<img src=x onerror="alert(1)">'; value.items[0].reason='<script>alert(1)</script>';
      value.items[0].confidence=null; value.items[1].confidence=0;
      value.items[0].quote_source=null; value.items[1].quote_source="public";
      value.items[0].industry=value.items[1].industry="same industry";
      value.items[0].risk=value.items[1].risk=0;
      const html=comparisonTable(value,true);
      assert(!html.includes("<img")); assert(!html.includes("<script")); assert(html.includes("&lt;img"));
      assert(html.includes('data-comparison-field="confidence"')); assert(html.includes("不可用</td><td>0"));
      assert(html.includes('data-comparison-field="quote_source"'));
      assert(!html.includes('data-comparison-field="industry"')); assert(!html.includes('data-comparison-field="risk"'));
      assert(comparisonTable(value,false).includes("风险（越高风险越大）"));
    ''')


def test_legacy_and_missing_rows_are_visible_for_audit_without_fabricated_scores() -> None:
    run_node(r'''
      const value=structuredClone(fixture), run={...originalRun,snapshot_seal_origin:"legacy_backfill"};
      value.evidence.snapshot_seal_origin="legacy_backfill"; value.action_source_eligible=false;
      const item=value.items[0]; item.status="missing"; item.reason="缺少资料";
      for(const key of ["rank","score","raw_score","trend_score","leader_score","data_quality_score","risk","confidence","tradability"])item[key]=null;
      validateMarketScanComparison(value,run,value.requested_symbols);
      const html=comparisonTable(value); assert(html.includes("仅供历史审计")); assert(html.includes("缺少资料"));
      assert(html.includes("不可用")); value.action_source_eligible=true;
      assert.throws(()=>validateMarketScanComparison(value,run,value.requested_symbols),/来源资格/);
    ''')


def test_local_json_export_releases_object_url_even_if_browser_download_fails() -> None:
    run_node(r'''
      const elements=new Map(), calls=[], blobs=[];
      const root={getElementById(id){if(!elements.has(id))elements.set(id,{}); return elements.get(id);},
        defaultView:{URL:{createObjectURL(blob){blobs.push(blob); return "blob:local";},revokeObjectURL(url){calls.push(url);}}},
        createElement(){return {click(){throw new Error("download failed");}}}};
      const view=createMarketScanComparisonView(root);
      assert.throws(()=>view.save(fixture),/download failed/);
      assert.deepEqual(calls,["blob:local"]); assert.deepEqual(JSON.parse(await blobs[0].text()),fixture);
    ''')


def test_comparison_view_dispose_releases_listeners_and_observer_before_rebind() -> None:
    run_node(r'''
      const elements=new Map(); let disconnected=0, invoked=0;
      class Element {
        handlers=new Map(); dataset={};
        addEventListener(name,handler){const list=this.handlers.get(name)||[]; this.handlers.set(name,[...list,handler]);}
        removeEventListener(name,handler){this.handlers.set(name,(this.handlers.get(name)||[]).filter(value=>value!==handler));}
        click(){for(const handler of this.handlers.get("click")||[])handler({target:{closest:()=>null}});}
      }
      const root={getElementById(id){if(!elements.has(id))elements.set(id,new Element()); return elements.get(id);},
        defaultView:{MutationObserver:class { observe(){} disconnect(){disconnected++;}}}};
      const handlers={compare:()=>invoked++,clear(){},export(){},differences(){},toggle(){},remove(){},rowsChanged(){}};
      const first=createMarketScanComparisonView(root).bind(handlers);
      elements.get("marketScanCompareRun").click(); assert.equal(invoked,1);
      first(); elements.get("marketScanCompareRun").click(); assert.equal(invoked,1);
      const second=createMarketScanComparisonView(root).bind(handlers);
      elements.get("marketScanCompareRun").click(); assert.equal(invoked,2);
      second(); assert.equal(disconnected,2);
      for(const element of elements.values())for(const values of element.handlers.values())assert.equal(values.length,0);
    ''')


def test_equal_missing_comparison_fields_do_not_claim_equal_risk() -> None:
    run_node(r'''
      const value=structuredClone(fixture);
      value.items[1]={...value.items[0],symbol:value.items[1].symbol};
      assert.match(comparisonTable(value,true),/相同或同样缺失不证明风险相同/);
    ''')


@pytest.mark.parametrize("mutation", [
    'value.evidence.snapshot_digest="f".repeat(64)',
    'value.items[0].rank=null',
    'value.items.pop()',
])
def test_invalid_new_response_cannot_reuse_or_mix_previous_valid_result(mutation: str) -> None:
    run_node(f'''
      let calls=0;
      const h=harness(async()=>{{
        const value=structuredClone(fixture); if (++calls===2) {{ {mutation}; }} return value;
      }});
      h.select(); await h.controller.compare(); assert.equal(h.controller.export(),true);
      await h.controller.compare();
      assert.equal(h.latest.payload,null); assert.equal(h.latest.busy,false);
      assert.equal(h.controller.export(),false); assert.equal(h.saved.length,1);
      assert.match(h.latest.message,/校验失败/);
    ''')
