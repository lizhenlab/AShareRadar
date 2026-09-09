from __future__ import annotations

from pathlib import Path
import subprocess

import pytest


ROOT = Path(__file__).resolve().parents[1]


@pytest.mark.parametrize("panel", ["status", "events", "providers"])
def test_cached_tools_results_explain_failed_refresh_and_clear_warning_on_recovery(panel: str) -> None:
    _run(f"const panel = {panel!r};" + r'''
        const diagnostics = await import("./static/js/diagnostics.js");
        const state = {};
        let fail = false;
        const failingUrl = {status: "/api/tasks/status", events: "/api/monitor/events?limit=8", providers: "/api/data/status"}[panel];
        globalThis.fetch = async url => fail && url === failingUrl
          ? response({detail: "<数据库忙>"}, 503) : response(statusPayload(url));
        const load = () => panel === "providers" ? diagnostics.loadDataStatus(state, {force:true}) : diagnostics.loadMonitoring(state, {force:true});
        assert(await load(), "initial load failed");
        fail = true;
        assert(await load() === false, "failed refresh claimed success");
        const target = element({status:"taskCards",events:"monitorEvents",providers:"providerStatus"}[panel]);
        assert(target.innerHTML.includes("刷新失败") && target.innerHTML.includes("上次成功"), "cached content hid refresh failure");
        assert(target.innerHTML.includes("&lt;数据库忙&gt;"), "error was not escaped");
        if (panel === "status") assert(element("schedulerState").textContent.includes("上次"), "cached scheduler claimed current status");
        if (panel === "providers") assert(!target.innerHTML.includes("当前正常"), "cached provider claimed current health");
        fail = false;
        assert(await load(), "recovery load failed");
        assert(!target.innerHTML.includes("刷新失败"), "recovered panel retained stale warning");
        if (panel === "providers") assert(target.innerHTML.includes("当前正常"), "recovered current health missing");
    ''')


def test_legacy_import_preview_and_commit_bind_explicit_source_timezone_without_editing_bundle() -> None:
    _run(r'''
        const local = await import("./static/js/local-data.js");
        const state = importState();
        const before = JSON.stringify(state.localDataImportBundle);
        element("localDataLegacyTimezone").value = "America/Los_Angeles";
        const calls = [];
        globalThis.fetch = async (url, options) => {calls.push({url, options}); return response(previewPayload(url));};
        await local.previewLocalDataImport(state);
        await local.commitLocalDataImport(state);
        assert(calls.length === 2, "preview and commit were not both requested");
        for (const call of calls) {
          assert(new URL(call.url, "http://localhost").searchParams.get("legacy_audit_timezone") === "America/Los_Angeles", "request omitted frozen source timezone");
          assert(call.options.body === before, "timezone choice rewrote imported JSON");
        }
    ''')


def test_changed_legacy_timezone_cannot_reuse_a_preview_token() -> None:
    _run(r'''
        const local = await import("./static/js/local-data.js");
        const state = importState();
        element("localDataLegacyTimezone").value = "Asia/Shanghai";
        const calls = [];
        globalThis.fetch = async url => {calls.push(url); return response(previewPayload(url));};
        await local.previewLocalDataImport(state);
        element("localDataLegacyTimezone").value = "America/Los_Angeles";
        let rejected = false;
        try {await local.commitLocalDataImport(state);} catch (error) {rejected = error.message.includes("预览");}
        assert(rejected && calls.length === 1, "changed timezone reused old preview authority");
    ''')


def test_inflight_legacy_preview_cannot_overwrite_new_timezone_selection() -> None:
    _run(r'''
        const local = await import("./static/js/local-data.js");
        const state = importState();
        element("localDataLegacyTimezone").value = "Asia/Shanghai";
        let resolve;
        globalThis.fetch = () => new Promise(done => {resolve = done;});
        const request = local.previewLocalDataImport(state);
        element("localDataLegacyTimezone").value = "America/Los_Angeles";
        resolve(response(previewPayload("dry_run=true")));
        assert(await request === null, "old timezone response installed preview token");
        assert(!state.localDataImportPreview, "stale timezone preview survived");
    ''')


def test_modern_bundle_uses_its_own_timezone_metadata_without_a_user_override() -> None:
    _run(r'''
        const local = await import("./static/js/local-data.js");
        const state = importState();
        state.localDataImportBundle.audit_timestamps = {semantics:"utc-fixed",legacy_timezone:null};
        element("localDataLegacyTimezone").value = "not-an-IANA-zone";
        globalThis.fetch = async url => {
          assert(!String(url).includes("legacy_audit_timezone"), "modern explicit metadata received legacy override");
          return response(previewPayload(url));
        };
        assert(await local.previewLocalDataImport(state), "modern bundle required timezone selection");
    ''')


def test_missing_legacy_timezone_error_identifies_the_available_recovery_control() -> None:
    _run(r'''
        const local = await import("./static/js/local-data.js");
        globalThis.fetch = async () => response({detail:"legacy naive audit timestamp requires an explicit timezone"}, 400);
        let message = "";
        try {await local.previewLocalDataImport(importState());} catch (error) {message = error.message;}
        assert(message.includes("旧文件来源时区") && message.includes("预览"), "legacy error gave no actionable UI recovery");
    ''')


def _run(script: str) -> None:
    result = subprocess.run(["node", "--input-type=module", "--eval", _HELPERS + script], cwd=ROOT, check=False, capture_output=True, text=True)
    assert result.returncode == 0, result.stdout + result.stderr


_HELPERS = r'''
const elements = new Map();
function element(id) {
  if (!elements.has(id)) elements.set(id, {innerHTML:"",textContent:"",dataset:{},hidden:false,disabled:false,value:""});
  return elements.get(id);
}
globalThis.document = {hidden:true,getElementById:element,querySelectorAll:()=>[]};
element("localDataImportMode").value = "merge";
function assert(value,message) {if(!value) throw new Error(message);}
function response(payload,status=200) {return new Response(JSON.stringify(payload),{status,headers:{"Content-Type":"application/json"}});}
function statusPayload(url) {
  if(url === "/api/tasks/status") return {enabled:true,running:true,tasks:[{name:"quotes",display_name:"刷新报价",last_status:"success",last_message:"已完成"}]};
  if(url === "/api/tasks/runs?limit=8") return [];
  if(url === "/api/monitor/events?limit=8") return [{level:"info",category:"health",message:"上次事件",created_at:"2026-09-07T09:00:00Z"}];
  return {source_plan:{},providers:[{name:"test-provider",enabled:true,healthy:true,success_count:1,failure_count:0}],cache:{},capabilities:[],capability_statuses:[]};
}
function importState() {return {localDataImportBundle:{kind:"ashare-radar-user-data",version:1,tables:{}},localDataImportFileKey:"legacy.json:1:1"};}
function previewPayload(url) {const dry=String(url).includes("dry_run=true");return {dry_run:dry,committed:!dry,totals:{inserted:1},preview_token:dry?"preview-token-with-at-least-thirty-two-characters":null};}
'''
