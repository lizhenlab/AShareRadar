from __future__ import annotations

import subprocess
from pathlib import Path

import pytest


ROOT = Path(__file__).resolve().parents[1]


def test_screen_alert_history_is_lazy_and_acknowledgement_is_locally_pageable() -> None:
    _node(r'''
      const calls = [];
      const manager = create(async url => { calls.push(url); return history(); });
      manager.selectionChanged(preset());
      assert.equal(calls.length, 0);
      manager.recorded(ack(55));
      assert.equal(calls.length, 0, "a confirmed write must not require a history GET");
      assert.match(el("DetailContext").textContent, /批次 #41 → #42/);
      assert.match(el("DetailStatus").textContent, /已确认/);
      assert.equal(manager.state.detail.items.length, 50);
      await click("DetailNext");
      assert.equal(manager.state.detail.page, 2);
      assert.equal(manager.state.detail.items.length, 7);
      el("Kind").value = "exited"; await change("Kind");
      assert.equal(manager.state.detail.page, 1);
      assert.deepEqual(manager.state.detail.items, [{symbol:"600001.SH", change:"exited"}]);
      assert.equal(calls.length, 0);
    ''')


def test_history_read_failure_preserves_confirmed_ack_and_known_history_page() -> None:
    _node(r'''
      let reads=0;
      const manager=create(async()=>{ if (++reads>1) throw new Error("网络中断"); return history(); });
      manager.selectionChanged(preset()); manager.recorded(ack()); await manager.open();
      const previousDetail=el("DetailRows").innerHTML;
      await click("HistoryNext");
      assert.equal(manager.state.history.page, 1);
      assert.equal(el("DetailRows").innerHTML, previousDetail);
      assert.match(el("HistoryStatus").textContent, /第 2 页.*读取失败.*保留.*第 1 页/);
      assert.match(el("DetailStatus").textContent, /已确认/);
    ''')


def test_detail_selection_commits_only_after_success_and_retry_keeps_target() -> None:
    _node(r'''
      const reads=[]; let fail=true;
      const manager=create(async url=>{
        reads.push(url);
        if (!url.includes("/screen-alerts/")) return history();
        const id=Number(url.split("/screen-alerts/")[1].split("?")[0]);
        if (id===29 && fail) throw new Error("暂不可用");
        return detail(id);
      });
      manager.selectionChanged(preset()); await manager.open(); await select(30);
      const old=el("DetailRows").innerHTML;
      await select(29);
      assert.equal(manager.state.detail.event.id, 30);
      assert.equal(el("DetailRows").innerHTML, old);
      assert.match(el("DetailStatus").textContent, /记录 #29.*失败.*记录 #30/);
      fail=false; await click("DetailRefresh");
      assert.equal(manager.state.detail.event.id, 29);
      assert.equal(reads.at(-1), "/api/discovery/presets/7/screen-alerts/29?page=1&page_size=50&kind=all");
    ''')


@pytest.mark.parametrize("boundary", ["preset", "hidden", "workspace", "closed", "dispose"])
def test_stale_history_read_cannot_commit_after_leaving_its_owner(boundary: str) -> None:
    _node(r'''
      const pending=deferred(); let signal;
      const manager=create(async(_url,options)=>{ signal=options.signal; return pending.promise; });
      manager.selectionChanged(preset()); const read=manager.open();
      if (BOUNDARY==="preset") manager.selectionChanged(preset(8));
      if (BOUNDARY==="hidden") { root.hidden=true; await root.handlers.get("visibilitychange")(); }
      if (BOUNDARY==="workspace") manager.setSurfaceActive(false);
      if (BOUNDARY==="closed") { el("History").open=false; await el("History").handlers.get("toggle")(); }
      if (BOUNDARY==="dispose") manager.dispose();
      assert.equal(signal.aborted, true);
      pending.resolve(history()); await read;
      assert.equal(manager.state.history, null);
      assert.equal(el("HistoryRows").innerHTML, "");
    '''.replace("BOUNDARY", repr(boundary)))


def test_late_detail_cannot_override_newer_selection_or_ack() -> None:
    _node(r'''
      const pending=deferred();
      const manager=create(async url=>url.includes("/screen-alerts/30")?pending.promise:history());
      manager.selectionChanged(preset()); await manager.open(); const old=select(30);
      manager.recorded(ack());
      pending.resolve(detail(30)); await old;
      assert.equal(manager.state.detail.event.id, null);
      assert.match(el("DetailStatus").textContent, /已确认/);
    ''')


def test_confirmed_ack_is_preserved_while_market_is_hidden_without_starting_reads() -> None:
    _node(r'''
      let reads=0;
      const manager=create(async()=>{reads+=1;return history();});
      manager.selectionChanged(preset()); manager.setSurfaceActive(false);
      assert.equal(manager.recorded(ack()),true);
      await manager.open();
      assert.equal(reads,0);
      manager.setSurfaceActive(true);
      assert.equal(manager.state.detail.event.preset_id,7);
      assert.match(el("DetailStatus").textContent,/已确认/);
      const old=el("DetailRows").innerHTML;
      const other=ack();other.preset.preset_id=8;
      assert.equal(manager.recorded(other),false);
      assert.equal(el("DetailRows").innerHTML,old);
    ''')


def test_failed_kind_change_keeps_committed_classification_and_retries_the_failed_target() -> None:
    _node(r'''
      let fail=true;
      const manager=create(async url=>{
        if (!url.includes("/screen-alerts/")) return history();
        if (url.endsWith("kind=exited")) {
          if (fail) throw new Error("暂不可用");
          return {...detail(),items:[{symbol:"600001.SH",change:"exited"}],total:1,kind:"exited"};
        }
        return detail();
      });
      manager.selectionChanged(preset()); await manager.open(); await select(30);
      const old=el("DetailRows").innerHTML;
      el("Kind").value="exited"; await change("Kind");
      assert.equal(el("Kind").value,"all");
      assert.equal(el("DetailRows").innerHTML,old);
      fail=false; await click("DetailRefresh");
      assert.equal(el("Kind").value,"exited");
      assert.equal(manager.state.detail.total,1);
    ''')


@pytest.mark.parametrize("mutation", [
    'value.preset_id=8', 'value.items[0].preset_id=8', 'value.page=2',
    'value.items.pop()', 'value.items[0].entered_count=-1',
    'value.items[0].created_at="2026-02-30T01:00:00Z"',
    'value.items[1].id=value.items[0].id', 'value.items.reverse()',
])
def test_invalid_history_responses_never_replace_verified_content(mutation: str) -> None:
    _node(r'''
      let reads=0;
      const manager=create(async()=>{ const value=history(); if (++reads>1) { MUTATION; } return value; });
      manager.selectionChanged(preset()); await manager.open();
      const old=el("HistoryRows").innerHTML;
      await click("HistoryRefresh");
      assert.equal(el("HistoryRows").innerHTML, old);
      assert.match(el("HistoryStatus").textContent, /读取失败/);
    '''.replace("MUTATION", mutation))


@pytest.mark.parametrize("mutation", [
    'value.event.id=29', 'value.event.event_digest="b".repeat(64)',
    'value.items[0].symbol="000000.SZ"', 'value.items[0].symbol="600001"',
    'value.items[1].symbol=value.items[0].symbol',
    'value.items[0].change="exit"', 'value.items.reverse()',
    'value.total=2', 'value.kind="exited"',
])
def test_invalid_detail_response_keeps_acknowledged_content(mutation: str) -> None:
    _node(r'''
      const manager=create(async url=>{
        if (!url.includes("/screen-alerts/")) return history();
        const value=detail(30); MUTATION; return value;
      });
      manager.selectionChanged(preset()); manager.recorded(ack()); await manager.open();
      const old=el("DetailRows").innerHTML; await select(30);
      assert.equal(el("DetailRows").innerHTML, old);
      assert.match(el("DetailStatus").textContent, /读取失败.*已确认/);
    '''.replace("MUTATION", mutation))


def test_old_revision_history_remains_readable_and_zero_change_events_are_explicit() -> None:
    _node(r'''
      const manager=create(async url=>url.includes("/screen-alerts/")?detail(30, 0):history(0));
      manager.selectionChanged({...preset(), revision:9}); await manager.open(); await select(30);
      assert.equal(manager.state.detail.event.preset_revision, 2);
      assert.equal(manager.state.detail.total, 0);
      assert.match(el("DetailRows").innerHTML, /没有股票变化/);
      assert.match(el("DetailContext").textContent, /修订 v2/);
    ''')


def _node(script: str) -> None:
    completed = subprocess.run(
        ["node", "--input-type=module", "--eval", HARNESS + script], cwd=ROOT,
        capture_output=True, text=True, timeout=20, check=False,
    )
    assert completed.returncode == 0, completed.stderr


HARNESS = r'''
import assert from "node:assert/strict";
import {createDiscoveryScreenAlertsController} from "./static/js/discovery-screen-alerts.js";
const nodes=new Map();
function el(key) {
  if (!nodes.has(key)) nodes.set(key,{value:key==="Kind"?"all":"",innerHTML:"",textContent:"",hidden:false,open:false,
    disabled:false,dataset:{},attributes:{},handlers:new Map(),
    setAttribute(name,value){this.attributes[name]=value;},
    addEventListener(name,fn){this.handlers.set(name,fn);},removeEventListener(name){this.handlers.delete(name);},
    getClientRects(){return [{}];},querySelectorAll(){return [];}});
  return nodes.get(key);
}
const root={hidden:false,body:{dataset:{primaryView:"market"}},handlers:new Map(),
 getElementById(id){return el(id.replace("discoveryScreenAlerts",""));},
 addEventListener(name,fn){this.handlers.set(name,fn);},removeEventListener(name){this.handlers.delete(name);}};
function create(fetcher){return createDiscoveryScreenAlertsController({root,fetcher,surfaceActive:true});}
function preset(id=7){return {id,revision:2,name:"质量方案"};}
function summary(id=30,total=3){return {id,preset_id:7,preset_revision:2,current_run_id:42,previous_run_id:41,
 event_digest:"a".repeat(64),created_at:"2026-09-07T10:00:00Z",entered_count:total?total-2:0,
 exited_count:total?1:0,suppressed_unrankable_count:total?1:0};}
function history(total=3){return {preset_id:7,items:Array.from({length:20},(_,i)=>summary(30-i,total)),total:30,page:1,page_size:20,page_count:2};}
function detail(id=30,total=3){return {event:summary(id,total),items:total?[
 {symbol:"000001.SZ",change:"entered"},{symbol:"600001.SH",change:"exited"},{symbol:"600002.SH",change:"unrankable"}]:[],
 total,page:1,page_size:50,page_count:total?1:0,kind:"all"};}
function ack(count=1){return {schema_version:"market-scan-screen-alert-v1",status:"ready",unavailable_reason:null,
 preset:{preset_id:7,preset_revision:2,preset_name:"质量方案",spec_digest:"c".repeat(64)},
 current:{run_id:42},previous:{run_id:41},event_digest:"d".repeat(64),created:true,
 entered_symbols:Array.from({length:count},(_,i)=>String(i+1).padStart(6,"0")+".SZ"),
 exited_symbols:["600001.SH"],suppressed_unrankable_symbols:["600002.SH"]};}
function deferred(){let resolve;const promise=new Promise(done=>{resolve=done;});return {promise,resolve};}
async function click(key){return el(key).handlers.get("click")?.({target:el(key)});}
async function change(key){return el(key).handlers.get("change")?.({target:el(key)});}
async function select(id){return el("HistoryRows").handlers.get("click")({target:{closest(){return {dataset:{screenAlertEvent:String(id)}};}}});}
'''
