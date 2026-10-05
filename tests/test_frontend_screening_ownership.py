"""Integration regressions for the shared standard/preset screening surface."""
from __future__ import annotations

from pathlib import Path
import subprocess

import pytest

ROOT = Path(__file__).resolve().parents[1]

SUPPORT = r'''
import assert from "node:assert/strict";
import { installAppDom, marketScanPollingIdentity } from "./tests/frontend_app_flow_helpers.mjs";
import { impactRun, impactRows } from "./tests/e2e/market-scan-condition-impact-fixtures.mjs";
import { createMarketScanController } from "./static/js/market-scan.js";
import { createDiscoveryController } from "./static/js/discovery.js";
import { readAppliedScreenContext } from "./static/js/market-scan-screen-context.js";
const { element } = installAppDom({canvasContext:null});
const timers = new Map(); let nextTimer = 0;
globalThis.setTimeout = (callback, delay = 0) => { const id = ++nextTimer; timers.set(id, {callback,delay}); return id; };
globalThis.clearTimeout = id => timers.delete(id);
const run = impactRun(), calls = [];
const preset = {id:7,revision:1,name:"方案B",criteria:{score:{min:80},confidence:{min:80}},sort:[{field:"score",order:"desc"}]};
let heldStandard = null, heldPreset = null, probabilityPending = false;
const standardItem = {...impactRows(run)[0], name:"普通列表A"};
function standardPage() {
  return {run,items:[standardItem],total:1,page:1,page_size:100,page_count:1,
    probability_research:probabilityPending?{run_id:run.id,status:"not_generated",availability:"source_capture_pending",pipeline_stage:"source_capture_pending"}:null};
}
function presetPage() {
  const item = {...impactRows(run)[4],name:"方案列表B"};
  return {preset,run_id:run.id,rule_version:run.rule_version,total:1,page:1,page_size:100,page_count:1,
    items:[{...item,position:1,source_rank:item.rank,quality:item.data_quality_score,trend:item.trend_score,change:item.change_pct,turnover:item.turnover_rate}]};
}
async function fetcher(url, options={}) {
  const target=String(url); calls.push({url:target,options});
  if(target.includes("/polling-identity?"))return marketScanPollingIdentity(run,run);
  if(target==="/api/market-scans/latest"||target.startsWith("/api/market-scans/latest-published?"))return run;
  if(target.startsWith("/api/market-scans?"))return {items:[run],total:1,page:1,page_size:100,page_count:1};
  if(target.includes("/results?")) { const held=heldStandard;heldStandard=null;return held?held.promise:standardPage(); }
  if(target.startsWith("/api/discovery/presets?page="))return {items:[preset],total:1,page:1,page_size:100,page_count:1};
  if(target==="/api/discovery/presets/7/apply") {const held=heldPreset;heldPreset=null;return held?held.promise:presetPage();}
  if(target.includes("/rank-changes?"))return {current_run_id:run.id,previous_run_id:null,current_rule_version:run.rule_version,previous_rule_version:null,
    comparable:false,reason:"no_previous_run",items:[],total:0,page:1,page_size:200,page_count:0};
  throw new Error(`unexpected fixture read: ${target}`);
}
const standard=createMarketScanController({root:document,now:new Date(2026,7,11,17),fetcher,idlePollIntervalMs:30000,resultRetryIntervalMs:5000});
let discovery;
async function initialize() {
  await standard.activate();
  assert.equal(readAppliedScreenContext(element("marketScanTableWrap"))?.source.kind,"standard");
  discovery=createDiscoveryController({root:document,fetcher,getRun:()=>standard.state.run,loadStandardResults:()=>standard.loadResults()});
  await discovery.activate();
}
async function applyPreset() {
  element("discoveryPresetSelect").value=String(preset.id);
  element("discoveryPresetSelect").listeners.change();
  await discovery.applyPreset();
  assertPresetRemains();
}
function assertPresetRemains() {
  assert.equal(discovery.state.applied?.preset.id,preset.id);
  assert.match(element("marketScanRows").innerHTML,/方案列表B/);
  assert.doesNotMatch(element("marketScanRows").innerHTML,/普通列表A/);
  const applied=readAppliedScreenContext(element("marketScanTableWrap"));
  assert.equal(applied?.source.kind,"preset");
  assert.equal(applied.source.id,preset.id);
  assert.equal(applied.spec.ranges.confidence.min,80);
}
function deferred(){let resolve;const promise=new Promise(done=>{resolve=done;});return {promise,resolve};}
async function flush(){for(let i=0;i<200;i++)await Promise.resolve();}
async function fireTimer(id){const timer=timers.get(id);if(timer){timers.delete(id);timer.callback();await flush();}}
'''


def run_node(script: str) -> None:
    result = subprocess.run(["node", "--input-type=module", "-e", SUPPORT + script], cwd=ROOT, capture_output=True, text=True, timeout=20)
    assert result.returncode == 0, result.stderr or result.stdout


def test_unchanged_identity_poll_preserves_applied_query_and_displayed_rows() -> None:
    run_node(r'''
      await initialize();
      const wrap=element("marketScanTableWrap"), before=readAppliedScreenContext(wrap);
      const rows=element("marketScanRows").innerHTML;
      const offset=calls.length;
      await fireTimer(standard.state.pollTimer);
      assert.deepEqual(calls.slice(offset).map(x=>x.url),["/api/market-scans/polling-identity?mode=official"]);
      assert.deepEqual(readAppliedScreenContext(wrap),before,"A healthy unchanged identity poll must retain verified applied context");
      assert.equal(element("marketScanRows").innerHTML,rows);
      standard.deactivate();
    ''')


def test_older_standard_read_cannot_overwrite_newer_applied_preset_rows_or_context() -> None:
    run_node(r'''
      await initialize();
      const held=deferred();heldStandard=held;
      const old=standard.loadResults();await flush();
      assert.equal(heldStandard,null,"The old standard request must already be in flight");
      await applyPreset();
      held.resolve(standardPage());await old;await flush();
      assertPresetRemains();
      standard.deactivate();
    ''')


def test_probability_maintenance_timer_cannot_take_over_applied_preset() -> None:
    run_node(r'''
      probabilityPending=true;
      await initialize();
      const maintenanceTimer=standard.state.pollTimer;
      assert.equal(timers.get(maintenanceTimer)?.delay,5000);
      await applyPreset();
      const before=calls.filter(x=>x.url.includes("/results?")).length;
      await fireTimer(maintenanceTimer);
      assert.equal(calls.filter(x=>x.url.includes("/results?")).length,before,"Automatic standard maintenance must not replace an applied preset");
      assertPresetRemains();
      standard.deactivate();
    ''')


def test_visibility_resume_restores_applied_context_when_identity_is_unchanged() -> None:
    run_node(r'''
      await initialize();
      const wrap=element("marketScanTableWrap"), before=readAppliedScreenContext(wrap);
      const rows=element("marketScanRows").innerHTML;
      standard.setVisible(false);await flush();
      standard.setVisible(true);await flush();
      const after=readAppliedScreenContext(wrap);
      assert(after,"A healthy resume must restore the displayed page's applied context");
      assert.deepEqual(after.run,before.run);
      assert.deepEqual(after.spec,before.spec);
      assert.deepEqual(after.source,before.source);
      assert.equal(after.total,before.total);
      assert.equal(element("marketScanRows").innerHTML,rows);
      standard.deactivate();
    ''')


@pytest.mark.parametrize("hide", ["setVisible", "setSurfaceActive"])
def test_pending_preset_cannot_commit_after_surface_is_hidden(hide: str) -> None:
    run_node(r'''
      await initialize();
      const held=deferred();heldPreset=held;
      element("discoveryPresetSelect").value=String(preset.id);
      element("discoveryPresetSelect").listeners.change();
      const pending=discovery.applyPreset();await flush();
      assert.equal(heldPreset,null,"The preset response must already be in flight");
      standard.HIDE(false);await flush();
      held.resolve(presetPage());await pending;await flush();
      assert.equal(discovery.state.applied,null);
      assert.equal(readAppliedScreenContext(element("marketScanTableWrap")),null);
      assert.doesNotMatch(element("marketScanRows").innerHTML,/方案列表B/);
      standard.deactivate();
    '''.replace("HIDE", hide))


def test_completed_preset_remains_bound_across_unchanged_visibility_resume() -> None:
    run_node(r'''
      await initialize();await applyPreset();
      const before=readAppliedScreenContext(element("marketScanTableWrap"));
      const rows=element("marketScanRows").innerHTML;
      const previousApplied=discovery.state.applied;
      const resultCalls=calls.filter(x=>x.url.includes("/results?")).length;
      standard.setVisible(false);await flush();
      standard.setVisible(true);await flush();
      assertPresetRemains();
      const after=readAppliedScreenContext(element("marketScanTableWrap"));
      assert.deepEqual(after.run,before.run);
      assert.deepEqual(after.spec,before.spec);
      assert.deepEqual(after.source,before.source);
      assert.equal(discovery.state.applied,previousApplied);
      assert.equal(element("marketScanRows").innerHTML,rows);
      assert.equal(calls.filter(x=>x.url.includes("/results?")).length,resultCalls);
      standard.deactivate();
    ''')
