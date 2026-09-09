from __future__ import annotations

from pathlib import Path
import subprocess

import pytest


ROOT = Path(__file__).resolve().parents[1]


def test_opening_watchlist_chart_does_not_mark_hidden_changes_read() -> None:
    _run(r'''
      assert(await navigateWatchlistResearch(context, "600519.SH"), "ordinary stock navigation failed");
      assert(state.workspaceView === "overview", "ordinary stock click did not open the chart");
      assert(state.loadOptions.waitForAdviceTimeline === false, "chart navigation unnecessarily waited for hidden changes");
      assert(writes.length === 0 && state.watchlist[0].unread_change_count === 3, "opening chart cleared unseen changes");
    ''')


def test_explicit_changes_navigation_reveals_timeline_then_clears_only_its_watermark() -> None:
    _run(r'''
      assert(await navigateWatchlistResearch(context, "600519.SH", {changes:true}), "explicit changes navigation failed");
      assert(state.workspaceView === "tools" && state.loadOptions.waitForAdviceTimeline, "changes click did not select and wait for its timeline");
      assert(writes.length === 1 && writes[0].url.includes("600519.SH/mark-viewed"), "wrong stock or duplicate mark was submitted");
      assert(writes[0].body.viewed_through_advice_id === 73, "marking did not use visible timeline watermark");
      assert(order.indexOf("focus") < order.indexOf("write"), "unread state changed before the timeline was revealed");
      assert(element("noteContent").value === "尚未提交的草稿", "navigation cleared the note draft");
    ''')


@pytest.mark.parametrize("invisible", ["background", "section", "different_view"])
def test_changes_that_never_become_visible_remain_unread(invisible: str) -> None:
    _run(f"const invisible = {invisible!r};" + r'''
      const load = context.loadAll;
      context.loadAll = async options => {
        await load(options);
        if (invisible === "background") document.hidden = true;
        if (invisible === "section") element("adviceTimeline").visible = false;
        if (invisible === "different_view") state.workspaceView = "overview";
        return true;
      };
      assert(await navigateWatchlistResearch(context, "600519.SH", {changes:true}) === false, "hidden timeline was treated as viewed");
      assert(writes.length === 0, "hidden timeline cleared unread changes");
      assert(element("watchlistFeedback").textContent.includes("未读状态保持"), "user received no explanation for preserved unread state");
    ''')


@pytest.mark.parametrize("failure", ["workbench", "timeline"])
def test_failed_changes_loading_keeps_the_existing_unread_count(failure: str) -> None:
    _run(f"const failure = {failure!r};" + r'''
      const load = context.loadAll;
      context.loadAll = async options => {
        await load(options);
        if (failure === "timeline") state.adviceTimelineWatermark = null;
        return failure !== "workbench";
      };
      assert(await navigateWatchlistResearch(context, "600519.SH", {changes:true}) === false, "failed context claimed changes were viewed");
      assert(writes.length === 0 && state.watchlist[0].unread_change_count === 3, "failed read cleared unread state");
    ''')


def test_fast_stock_switch_cannot_mark_the_old_timeline_read() -> None:
    _run(r'''
      const pending = deferred();
      const load = context.loadAll;
      context.loadAll = options => {load(options); return pending.promise;};
      const first = navigateWatchlistResearch(context, "600519.SH", {changes:true});
      context.loadAll = load;
      await navigateWatchlistResearch(context, "000001.SZ", {changes:true});
      pending.resolve(true);
      assert(await first === false, "late old stock completion retained view authority");
      assert(writes.length === 1 && writes[0].url.includes("000001.SZ/mark-viewed"), "fast switch marked the wrong stock");
    ''')


@pytest.mark.parametrize("invalid", ["missing", "stock", "stale"])
def test_visible_changes_require_a_matching_current_mutation_context(invalid: str) -> None:
    _run(f"const invalid = {invalid!r};" + r'''
      context.currentWatchlistMutationOptions = () => invalid === "missing" ? null
        : {symbol:invalid === "stock" ? "000001.SZ" : state.symbol,isCurrent:() => invalid !== "stale"};
      assert(await navigateWatchlistResearch(context, "600519.SH", {changes:true}) === false, "unusable mutation context granted read-mark authority");
      assert(writes.length === 0, "mismatched mutation context wrote unread state");
    ''')


def test_failed_mark_read_does_not_undo_successful_changes_navigation() -> None:
    _run(r'''
      failWrite = true;
      assert(await navigateWatchlistResearch(context, "600519.SH", {changes:true}) === false, "failed write claimed success");
      assert(state.workspaceView === "tools" && state.symbol === "600519.SH", "failed read mark undid successful navigation");
      assert(element("watchlistFeedback").textContent.includes("未读状态未清除"), "mark failure gave no recovery status");
      assert(state.watchlist[0].unread_change_count === 3, "failed mark changed the local unread count");
    ''')


def test_unread_changes_have_a_separate_accessible_action_outside_the_stock_button() -> None:
    _run(r'''
      const {renderWatchlist} = await import("./static/js/watchlist.js");
      renderWatchlist(state.watchlist);
      const html = element("watchList").innerHTML;
      const action = html.indexOf('data-action="changes"');
      assert(action > html.indexOf('</button>'), "changes action was nested in the stock button");
      assert(html.includes('aria-label="查看 贵州茅台 的 3 条新变化"'), "changes action lost the stock-specific accessible name");
      assert(html.includes('data-action="open"'), "explicit changes replaced ordinary chart navigation");
    ''')


def _run(script: str) -> None:
    result = subprocess.run(["node", "--input-type=module", "--eval", _HELPERS + script], cwd=ROOT, capture_output=True, text=True, check=False)
    assert result.returncode == 0, result.stdout + result.stderr


_HELPERS = r'''
import {navigateWatchlistResearch} from "./static/js/watchlist-navigation.js";
const elements=new Map(); const writes=[]; const order=[]; let failWrite=false;
function element(id) {
  if (!elements.has(id)) elements.set(id,{value:"",textContent:"",innerHTML:"",dataset:{},hidden:false,visible:true,
    setAttribute(){},querySelectorAll(){return [];},getClientRects(){return this.visible?[{}]:[];},
    focus(){order.push("focus");},scrollIntoView(){order.push("reveal");}});
  return elements.get(id);
}
globalThis.document={hidden:false,getElementById:element};
element("noteContent").value="尚未提交的草稿";
const state={symbol:"600519.SH",loadSeq:0,workspaceView:"overview",watchlist:[
  {symbol:"600519.SH",code:"600519",name:"贵州茅台",unread_change_count:3},
  {symbol:"000001.SZ",code:"000001",name:"平安银行",unread_change_count:2},
]};
const context={state,root:document,
  setActiveSymbol(symbol){state.symbol=symbol;},setWorkspaceView(view){state.workspaceView=view;},
  clearWatchlistFeedback(){element("watchlistFeedback").textContent="";},
  setWatchlistFeedback(message){element("watchlistFeedback").textContent=message;},setMutationStatus(){},
  loadAll(options){state.loadOptions=options;state.loadSeq+=1;state.adviceTimelineWatermark={symbol:state.symbol,loadSeq:state.loadSeq,adviceId:73};return Promise.resolve(true);},
  currentWatchlistMutationOptions(){const symbol=state.symbol;return {symbol,isCurrent:()=>state.symbol===symbol};},
};
globalThis.fetch=async (url,options={})=>{
  if(options.method==='POST'){
    writes.push({url:String(url),body:JSON.parse(options.body)});order.push("write");
    if(failWrite)return new Response(JSON.stringify({detail:"已读服务繁忙"}),{status:503});
    const symbol=decodeURIComponent(String(url).split('/').at(-2));
    const item=state.watchlist.find(row=>row.symbol===symbol);item.unread_change_count=0;
    return new Response(JSON.stringify(item),{status:200});
  }
  return new Response(JSON.stringify(state.watchlist),{status:200});
};
function assert(value,message){if(!value)throw new Error(message);}
function deferred(){let resolve;const promise=new Promise(done=>{resolve=done;});return {promise,resolve};}
'''
