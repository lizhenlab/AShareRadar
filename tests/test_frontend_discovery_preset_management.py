from __future__ import annotations

from pathlib import Path
import subprocess

import pytest


ROOT = Path(__file__).resolve().parents[1]


def test_save_selected_preset_creates_copy_and_explicit_update_uses_revision() -> None:
    _node(r'''
      const c = await ready(); select(1); input("discoveryPresetName", "新副本");
      input("marketScanScoreMin", "85");
      const saved = await c.savePreset();
      assert.equal(writes[0].method, "POST"); assert.equal(saved.id, 2);
      assert.equal(records[0].criteria.score.min, 80); assert.equal(records[0].name, "方案1");
      input("marketScanScoreMin", "90");
      const updated = await c.updatePreset();
      assert.equal(writes[1].method, "PUT"); assert.equal(writes[1].body.expected_revision, 1);
      assert.equal(updated.id, 2); assert.equal(updated.revision, 2);
      assert.equal(c.selectedPreset().id, 2);
    ''')


def test_compatible_null_defaults_in_real_response_are_accepted() -> None:
    _node(r'''
      const c = await ready(); input("discoveryPresetName", "新副本");
      const saved = await c.savePreset();
      assert.equal(saved.revision, 1); assert.equal(saved.criteria.is_st, null);
      assert.match(feedback(), /已保存/);
    ''')


def test_compatibility_preset_cannot_be_overwritten_from_partial_form() -> None:
    _node(r'''
      records[0].criteria = {confidence:{min:60,max:80}};
      const c = await ready(); select(1);
      assert.equal(el("discoveryPresetUpdate").disabled, true);
      await c.updatePreset(); assert.equal(writes.length, 0);
      assert.match(feedback(), /兼容|完整/);
      assert.equal(el("discoveryPresetSave").disabled, true);
      input("discoveryPresetName", "兼容副本"); await c.savePreset();
      assert.equal(writes.length, 0); assert.match(feedback(), /取消.*选择|完整/);
      select(""); input("discoveryPresetName", "明确自建方案");
      assert.equal(el("discoveryPresetSave").disabled, false);
      await c.savePreset(); assert.equal(writes.length,1);
    ''')


@pytest.mark.parametrize("mismatch", ["id", "revision", "definition"])
def test_update_rejects_unrelated_or_stale_acknowledgement(mismatch: str) -> None:
    _node(r'''
      const c = await ready((url, options) => {
        if (options.method !== "PUT") return;
        const body = JSON.parse(options.body);
        const ack = {...records[0], ...body, revision:2}; delete ack.expected_revision;
        if (MISMATCH === "id") ack.id = 99;
        if (MISMATCH === "revision") ack.revision = 1;
        if (MISMATCH === "definition") ack.criteria = {score:{min:10}};
        return ack;
      });
      select(1); input("marketScanScoreMin", "85");
      assert.equal(await c.updatePreset(), null);
      assert.equal(c.selectedPreset().revision, 1);
      assert.match(feedback(), /回执.*不一致|回执.*核对/);
    '''.replace("MISMATCH", repr(mismatch)))


def test_copy_rejects_an_acknowledgement_claiming_the_original_id() -> None:
    _node(r'''
      const c = await ready((url, options) => options.method === "POST"
        ? {...records[0], ...JSON.parse(options.body)} : undefined);
      select(1); input("discoveryPresetName", "新副本");
      assert.equal(await c.savePreset(), null); assert.equal(c.selectedPreset().id, 1);
      assert.match(feedback(), /回执.*不一致|回执.*核对/);
    ''')


@pytest.mark.parametrize("operation", ["savePreset", "updatePreset"])
def test_pending_write_keeps_newer_editor_input(operation: str) -> None:
    _node(r'''
      const pending = deferred(); let submitted;
      const c = await ready((url, options) => {
        if (!options.method) return;
        submitted = JSON.parse(options.body); return pending.promise;
      });
      select(1); input("discoveryPresetName", "已发出的名称");
      const action = c.OPERATION(); await settle();
      input("discoveryPresetName", "尚未保存的新名称"); input("marketScanScoreMin", "95");
      const ack = {...preset(OPERATION === "savePreset" ? 2 : 1), ...submitted,
        revision: OPERATION === "savePreset" ? 1 : 2}; delete ack.expected_revision;
      pending.resolve(ack); await action;
      assert.equal(el("discoveryPresetName").value, "尚未保存的新名称");
      assert.equal(el("marketScanScoreMin").value, "95");
      assert.match(feedback(), /输入.*保留|保留.*输入/);
    '''.replace("c.OPERATION()", f"c.{operation}()").replace("OPERATION", repr(operation)))


def test_pagination_reaches_older_presets_and_keeps_selected_identity() -> None:
    _node(r'''
      records = Array.from({length:101}, (_, i) => preset(i+1));
      const c = await ready(); select(101);
      await c.loadPresets(2);
      assert.deepEqual(c.state.presets.map(item => item.id), [1]);
      assert.equal(c.selectedPreset().id, 101); assert.equal(el("discoveryPresetSelect").value, "101");
      assert.match(el("discoveryPresetSelectionInfo").textContent, /101/);
      assert.match(el("discoveryPresetSelectionInfo").textContent, /跨页|不在当前页/);
      assert.match(el("discoveryPresetPageInfo").textContent, /2.*2.*101/);
      assert.equal(el("discoveryPresetNext").disabled, true);
      select(1); await c.loadPresets(1); assert.equal(c.selectedPreset().id, 1);
      assert.equal(reads.length, 3, "pagination must remain on demand");
    ''')


def test_failed_page_and_wrong_page_response_preserve_previous_view_and_selection() -> None:
    _node(r'''
      records = Array.from({length:101}, (_, i) => preset(i+1)); let fail = true;
      const c = await ready((url) => {
        if (!url.includes("page=2")) return;
        if (fail) throw new Error("offline");
        return page(1);
      });
      select(101); await c.loadPresets(2); fail=false; await c.loadPresets(2);
      assert.equal(c.state.presets.length,100); assert.equal(c.selectedPreset().id,101);
      assert.match(el("discoveryPresetPageInfo").textContent,/1.*2/);
      assert.match(feedback(),/分页|当前页|请求页/);
    ''')


def test_update_refreshes_server_order_before_pagination_and_keeps_ack_on_refresh_error() -> None:
    _node(r'''
      records=Array.from({length:201},(_,i)=>preset(i+1)); let failRead=false;
      const c=await ready((url,options)=>{
        if (!options.method && failRead) throw new Error("同步失败");
      });
      await c.loadPresets(2); select(2); input("marketScanScoreMin","81");
      await c.updatePreset();
      assert.equal(c.state.presets[0].id,102,"updated preset moved to page1; boundary item must appear on page2");
      failRead=true; input("marketScanScoreMin","82");
      await c.updatePreset();
      assert.equal(c.selectedPreset().revision,3);
      assert.equal(el("discoveryPresetNext").disabled,true);
      assert.match(feedback(),/已更新.*同步/);
      failRead=false; await c.loadPresets(); assert.equal(el("discoveryPresetNext").disabled,false);
    ''')


def test_unknown_write_locks_order_until_explicit_refresh_without_retrying_write() -> None:
    _node(r'''
      records=Array.from({length:101},(_,i)=>preset(i+1));
      const c=await ready((_url,options)=>{
        if(options.method === "PUT") throw new Error("响应超时");
      });
      select(101); input("marketScanScoreMin","81"); await c.updatePreset();
      assert.equal(el("discoveryPresetNext").disabled,true);
      const before=reads.length; await c.loadPresets(2); assert.equal(reads.length,before);
      await c.loadPresets(); assert.equal(el("discoveryPresetNext").disabled,false);
      assert.equal(writes.length,1);
    ''')


def test_unknown_copy_requires_explicit_identity_read_and_survives_editor_changes() -> None:
    _node(r'''
      let failWrite=true; let failRead=true;
      const c=await ready((url,options)=>{
        if(options.method==="POST" && failWrite)throw new Error("响应超时");
        if(/presets\/\d+$/.test(url) && failRead)throw new Error("核对失败");
      });
      select(1); input("discoveryPresetName","新副本"); await c.savePreset();
      assert.equal(c.state.writeUnconfirmed,true); assert.doesNotMatch(feedback(),/保存失败/);
      input("discoveryPresetName","又一个副本"); await c.loadPresets();
      await c.savePreset(); await c.updatePreset(); assert.equal(writes.length,1);
      assert.equal(el("discoveryPresetApply").disabled,true);
      assert.equal(el("discoveryPresetScreenAlert").disabled,true);
      assert.equal(el("discoveryPresetExport").disabled,true);
      const beforeReads=reads.length;
      await c.applyPreset(); await c.recordScreenAlert(); await c.exportPreset();
      assert.equal(writes.length,1); assert.equal(reads.length,beforeReads);
      select(1); await settle(); assert.equal(c.state.writeUnconfirmed,true);
      failRead=false; select(1); await settle();
      assert.equal(c.state.writeUnconfirmed,false); assert.equal(c.selectedPreset().id,1);
      failWrite=false; input("discoveryPresetName","已核对后新建"); await c.savePreset();
      assert.equal(writes.length,2); assert.equal(c.selectedPreset().id,2);
    ''')


def test_explicit_server_rejection_does_not_mark_save_as_unknown() -> None:
    _node(r'''
      const c=await ready((_url,options)=>{
        if(options.method==="POST")throw Object.assign(new Error("名称已存在"),{status:409});
      });
      select(1); await c.savePreset();
      assert.equal(c.state.writeUnconfirmed,false); assert.equal(el("discoveryPresetSave").disabled,false);
      assert.match(feedback(),/名称已存在/);
    ''')


def test_page_refresh_does_not_adopt_external_revision_or_replace_editor_draft() -> None:
    _node(r'''
      const c=await ready(); select(1); input("marketScanScoreMin","85");
      records=[{...records[0],revision:2,criteria:{score:{min:95}}}];
      await c.loadPresets();
      assert.equal(c.selectedPreset().revision,1); assert.equal(el("marketScanScoreMin").value,"85");
      assert.match(el("discoveryPresetSelectionInfo").textContent,/修订 1/);
    ''')


@pytest.mark.parametrize("mismatch", ["id", "revision", "definition"])
def test_rename_uses_the_same_identity_revision_and_definition_receipt_check(mismatch: str) -> None:
    _node(r'''
      const c=await ready((_url,options)=>{
        if(options.method!=="PATCH")return;
        const ack={...records[0],name:JSON.parse(options.body).name,revision:2};
        if(MISMATCH === "id")ack.id=99;
        if(MISMATCH === "revision")ack.revision=1;
        if(MISMATCH === "definition")ack.criteria={score:{min:10}};
        return ack;
      });
      select(1); input("discoveryPresetName","重命名"); await c.renamePreset();
      assert.equal(c.selectedPreset().name,"方案1"); assert.equal(c.selectedPreset().revision,1);
      assert.equal(c.state.writeUnconfirmed,true); assert.match(feedback(),/回执.*不一致/);
    '''.replace("MISMATCH", repr(mismatch)))


def test_deleting_last_page_item_clamps_to_remaining_page_without_selected_ghost() -> None:
    _node(r'''
      records=Array.from({length:101},(_,i)=>preset(i+1));
      globalThis.confirm=()=>true;
      const c=await ready((url,options)=>{
        if(options.method!=="DELETE")return;
        const id=Number(new URL(url,"http://localhost").pathname.split("/").at(-1));
        records=records.filter(item=>item.id!==id); return {deleted:true,preset_id:id};
      });
      await c.loadPresets(2); select(1); await c.deletePreset();
      assert.equal(c.selectedPreset(),null); assert.equal(c.state.page,1);
      assert.equal(c.state.presets.length,100); assert.equal(el("discoveryPresetSelect").value,"");
      assert.equal(el("discoveryPresetNext").disabled,true); assert.match(feedback(),/已删除/);
    ''')


def test_import_unknown_result_uses_the_same_explicit_confirmation_boundary() -> None:
    _node(r'''
      const c=await ready((url,options)=>{
        if(url.endsWith("/import"))throw new Error("导入响应超时");
      });
      const file={text:async()=>JSON.stringify({preset:{...preset(2),name:"导入新方案"}})};
      await c.importPreset(file); assert.equal(c.state.writeUnconfirmed,true);
      await c.importPreset(file); await c.savePreset(); assert.equal(writes.length,1);
      assert.match(feedback(),/待核对/); assert.doesNotMatch(feedback(),/保存失败/);
    ''')


def test_import_parse_failure_does_not_mark_an_unsent_write_unknown() -> None:
    _node(r'''
      const c=await ready(); await c.importPreset({text:async()=>"{bad json"});
      assert.equal(writes.length,0); assert.equal(c.state.writeUnconfirmed,false);
      assert.equal(el("discoveryPresetImport").disabled,false);
    ''')


@pytest.mark.parametrize("receipt", ["{deleted:true,preset_id:99}", "{deleted:false,preset_id:1}"])
def test_delete_rejects_wrong_identity_and_never_clears_the_selected_preset(receipt: str) -> None:
    _node(r'''
      globalThis.confirm=()=>true;
      const c=await ready((_url,options)=>options.method==="DELETE" ? RECEIPT : undefined);
      select(1); const result=await c.deletePreset();
      assert.equal(result,null); assert.equal(c.selectedPreset().id,1);
      assert.equal(c.state.presets.length,1); assert.equal(c.state.writeUnconfirmed,true);
      assert.match(feedback(),/删除回执.*核对|删除回执.*不一致/);
    '''.replace("RECEIPT", receipt))


def test_selection_and_screen_alert_hooks_receive_confirmed_identity() -> None:
    _node(r'''
      const selections=[]; const alerts=[];
      const c=await ready((url,options)=>url.endsWith("screen-alerts") ? {
        schema_version:"market-scan-screen-alert-v1",status:"ready",unavailable_reason:null,
        preset:{preset_id:1,preset_revision:1,preset_name:"方案1",spec_digest:"a".repeat(64)},
        current:{run_id:42},previous:{run_id:41},entered_symbols:[],exited_symbols:[],
        suppressed_unrankable_symbols:[],event_digest:"b".repeat(64),created:true,
      } : undefined,{onPresetChange:p=>selections.push(p),onScreenAlertRecorded:p=>alerts.push(p)});
      select(1); await c.recordScreenAlert();
      assert.equal(selections.at(-1).id,1); assert.equal(alerts.length,1);
      assert.equal(alerts[0].preset.preset_revision,1);
    ''')


_HARNESS = r'''
  import assert from "node:assert/strict";
  import {installAppDom} from "./tests/frontend_app_flow_helpers.mjs";
  import {createDiscoveryController} from "./static/js/discovery.js";
  const {element:el}=installAppDom({canvasContext:null});
  const writes=[]; const reads=[];
  let records=[preset(1)]; let controller;
  function preset(id) {return {id,name:`方案${id}`,revision:1,schema_version:2,
    criteria:{score:{min:80,max:null},is_st:null},sort:[{field:"rank",order:"asc"}],
    column_view:"overview",created_at:"2026-09-07",updated_at:String(id).padStart(8,"0")};}
  function page(number) {
    const sorted=[...records].sort((a,b)=>b.updated_at.localeCompare(a.updated_at)||b.id-a.id);
    return {items:sorted.slice((number-1)*100,number*100),total:records.length,
      page:number,page_size:100,page_count:Math.ceil(records.length/100)};
  }
  async function ready(override=()=>undefined, extra={}) {
    el("marketScanStatus").value="success"; el("marketScanSort").value="rank"; el("marketScanOrder").value="asc";
    controller=createDiscoveryController({root:document,getRun:()=>({id:42,status:"success"}),...extra,
      async fetcher(url,options={}) {
        const body=options.body ? JSON.parse(options.body) : null;
        (options.method ? writes : reads).push({url,method:options.method,body});
        const value=await override(String(url),options); if(value!==undefined)return value;
        if(!options.method && /presets\/\d+$/.test(url))return records.find(r=>r.id===Number(url.split("/").at(-1)));
        if(!options.method)return page(Number(new URL(url,"http://localhost").searchParams.get("page"))||1);
        const id=options.method==="POST" ? Math.max(0,...records.map(r=>r.id))+1 : Number(url.split("/").at(-1));
        const previous=records.find(r=>r.id===id); const record={...preset(id),...body,
          criteria:{is_st:null,...body.criteria},revision:previous?previous.revision+1:1,updated_at:"99999999"};
        delete record.expected_revision; records=records.filter(r=>r.id!==id).concat(record); return record;
      }});
    await controller.activate(); return controller;
  }
  function input(id,value){el(id).value=value;el(id).listeners?.input?.({target:el(id)});}
  function select(id){el("discoveryPresetSelect").value=String(id);el("discoveryPresetSelect").listeners.change();}
  function feedback(){return el("discoveryPresetFeedback").textContent;}
  function deferred(){let resolve;const promise=new Promise(done=>{resolve=done;});return {promise,resolve};}
  async function settle(){for(let i=0;i<20;i++)await Promise.resolve();}
'''


def _node(source: str) -> None:
    result = subprocess.run(
        ["node", "--input-type=module", "--eval", _HARNESS + source],
        cwd=ROOT, capture_output=True, text=True, check=False,
    )
    assert result.returncode == 0, result.stdout + result.stderr
