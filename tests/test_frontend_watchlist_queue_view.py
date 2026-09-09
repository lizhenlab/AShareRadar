from __future__ import annotations

from pathlib import Path
import subprocess

import pytest


ROOT = Path(__file__).resolve().parents[1]


@pytest.mark.parametrize("query,expected", [("银行", "600000.SH,000001.SZ"), ("ｓｈ", "600000.SH,600519.SH"), ("复利", "600519.SH"), ("银行 股息", "600000.SH"), ("000001", "000001.SZ")])
def test_watchlist_search_uses_existing_queue_fields_without_reordering(query: str, expected: str) -> None:
    _run(f"const query = {query!r}; const expected = {expected!r};" + r'''
        const before = JSON.stringify(items);
        const result = view.selectWatchlistQueue(items, {query}, {today:"2026-09-07"});
        assert(result.map(item => item.symbol).join(",") === expected, "queue search omitted a supported field or reordered results");
        assert(JSON.stringify(items) === before, "filter modified the complete queue");
    ''')


def test_watchlist_due_status_and_unread_filters_combine_and_preserve_original_records() -> None:
    _run(r'''
        const result = view.selectWatchlistQueue(items, {status:"to_research",due:true,unread:true}, {today:"2026-09-07"});
        assert(result.length === 1 && result[0] === items[0], "combined status/due/unread filter did not preserve matching identity");
        assert(view.selectWatchlistQueue(items, {status:"holding_research"}, {today:"2026-09-07"}).length === 0, "empty filtered set was not represented");
        assert(view.selectWatchlistQueue(items, {status:"excluded"}, {today:"2026-09-07"})[0] === items[3], "excluded records became inaccessible");
    ''')


def test_watchlist_due_dates_follow_shanghai_midnight_and_reject_invalid_days() -> None:
    _run(r'''
        const before = new Date("2026-09-06T15:59:59Z");
        const after = new Date("2026-09-06T16:00:00Z");
        assert(view.watchlistMarketDate(before) === "2026-09-06", "date moved before Shanghai midnight");
        assert(view.watchlistMarketDate(after) === "2026-09-07", "date used the device timezone rather than Shanghai");
        const dates = ["2026-09-07","2026-09-06","2026-09-08",null,"2026-02-30","2026-13-01"];
        const samples = dates.map((next_review_date,index) => ({symbol:String(index),next_review_date}));
        assert(view.selectWatchlistQueue(samples, {due:true}, {now:after}).map(item => item.symbol).join(",") === "0,1", "due filter accepted invalid/future/missing dates or omitted today");
    ''')


def test_watchlist_filter_projection_preserves_editor_nodes_and_existing_failure_warning() -> None:
    _run(r'''
        const before = JSON.stringify(items);
        const draft = {value:"尚未保存的研究原因"};
        const rows = items.map(item => ({dataset:{symbol:item.symbol},hidden:false,draft}));
        element("watchList").querySelectorAll = () => rows;
        element("watchList").innerHTML = "已更新，列表同步降级";
        element("watchQueueSearch").value = "茅台";
        view.applyWatchlistQueueView(items, {sourceReady:true});
        assert(rows[0].hidden && !rows[2].hidden, "filter did not project the rendered rows");
        assert(element("watchQueueCount").textContent.includes("1 / 共 4"), "matching and complete counts were not shown");
        element("watchQueueSearch").value = "";
        view.applyWatchlistQueueView(items, {sourceReady:true});
        assert(rows.every(row => !row.hidden && row.draft === draft), "filter rebuilt edited DOM or lost a draft");
        assert(element("watchList").innerHTML === "已更新，列表同步降级", "filter removed a write/readback warning");
        assert(JSON.stringify(items) === before, "projection truncated the subscription source");
    ''')


def test_watchlist_filter_no_match_is_distinct_from_empty_queue() -> None:
    _run(r'''
        element("watchQueueSearch").value = "不存在";
        view.applyWatchlistQueueView(items, {sourceReady:true});
        assert(!element("watchQueueNoMatch").hidden, "no matching rows lacked recovery hint");
        assert(element("watchQueueCount").textContent === "显示 0 / 共 4 条", "filter incorrectly claimed an empty saved queue");
        view.applyWatchlistQueueView([], {sourceReady:true});
        assert(element("watchQueueNoMatch").hidden, "empty saved queue claimed filtering hid records");
        assert(element("watchQueueCount").textContent === "显示 0 / 共 0 条", "empty queue total was stale");
    ''')


def test_watchlist_queue_binding_resets_session_filters_without_requests_or_stale_listeners() -> None:
    _run(r'''
        globalThis.fetch = () => {throw new Error("a display filter made an API request");};
        view.applyWatchlistQueueView(items, {sourceReady:true});
        let reads = 0;
        view.bindWatchlistQueueFilters(() => {reads += 1; return items;});
        const dispose = view.bindWatchlistQueueFilters(() => {reads += 1; return items;});
        element("watchQueueSearch").value = "茅台";
        element("watchQueueSearch").emit("input");
        assert(reads === 1, "binding duplicated a live listener or fetched before a list was loaded");
        assert(element("watchQueueCount").textContent === "显示 1 / 共 4 条", "search event did not update the projection");
        element("watchQueueStatus").value = "to_research";
        element("watchQueueDue").checked = true;
        element("watchQueueUnread").checked = true;
        element("watchQueueReset").emit("click");
        assert(element("watchQueueSearch").value === "" && element("watchQueueStatus").value === "all", "reset left hidden query constraints");
        assert(!element("watchQueueDue").checked && !element("watchQueueUnread").checked, "reset left quick filters checked");
        assert(element("watchQueueCount").textContent === "显示 4 / 共 4 条", "reset did not restore the entire queue");
        dispose();
        const before = reads;
        element("watchQueueSearch").emit("input");
        assert(reads === before, "disposed queue still responded to events");
    ''')


def test_watchlist_real_renderer_reapplies_filters_after_readback_without_changing_subscription() -> None:
    _run(r'''
        const {renderWatchlist,watchlistSubscriptionKey} = await import("./static/js/watchlist.js");
        const key = watchlistSubscriptionKey(items);
        element("watchQueueStatus").value = "to_research";
        renderWatchlist(items, {today:"2026-09-07"});
        assert(element("watchQueueCount").textContent === "显示 2 / 共 4 条", "normal rendering omitted selected queue filters");
        const updated = items.map(item => item.symbol === "600000.SH" ? {...item,research_status:"watching"} : item);
        renderWatchlist(updated, {today:"2026-09-07"});
        assert(element("watchQueueCount").textContent === "显示 1 / 共 4 条", "write readback failed to reapply the current filter");
        assert(watchlistSubscriptionKey(updated) === key, "view filtering modified the active stock subscription");
        assert(element("watchList").innerHTML.includes("今日复核 · 2026-09-07"), "row date semantics diverged from filter");
    ''')


def test_watchlist_unknown_source_stays_unavailable_when_a_filter_changes() -> None:
    _run(r'''
        element("watchList").innerHTML = "自选股读取失败";
        view.bindWatchlistQueueFilters(() => []);
        element("watchQueueSearch").value = "银行";
        element("watchQueueSearch").emit("input");
        assert(element("watchQueueCount").textContent.includes("尚未读取成功"), "failed initial read claimed a known empty queue");
        assert(element("watchQueueNoMatch").hidden, "unknown list rendered a no-match empty state");
        assert(element("watchList").innerHTML === "自选股读取失败", "filter erased source failure");
        view.applyWatchlistQueueView(items, {sourceReady:true});
        view.applyWatchlistQueueView([], {sourceReady:false});
        element("watchQueueSearch").emit("input");
        assert(element("watchQueueCount").textContent.includes("尚未读取成功"), "failure replacing a previously known list retained visible row counts");
    ''')


def test_watchlist_visibility_resume_updates_due_filter_and_badge_without_rebuilding_draft() -> None:
    _run(r'''
        const listeners = new Map();
        document.addEventListener = (event,handler) => listeners.set(event,handler);
        document.removeEventListener = (event) => listeners.delete(event);
        const badge = {textContent:"复核 · 2026-09-07",className:"watch-badge watch-review review-upcoming"};
        const row = {dataset:{symbol:"600000.SH"},hidden:false,querySelector:() => badge};
        element("watchList").querySelectorAll = () => [row];
        const now = new Date("2026-09-06T15:59:59Z");
        const options = {now};
        element("watchQueueDue").checked = true;
        view.applyWatchlistQueueView(items, {...options,sourceReady:true});
        view.bindWatchlistQueueFilters(() => items, options);
        assert(row.hidden, "future Shanghai review date was initially due");
        now.setTime(new Date("2026-09-06T16:00:00Z").getTime());
        document.hidden = true;
        listeners.get("visibilitychange")();
        assert(row.hidden, "hidden document performed unnecessary view work");
        document.hidden = false;
        listeners.get("visibilitychange")();
        assert(!row.hidden, "returning after Shanghai midnight retained an obsolete due filter");
        assert(badge.textContent === "今日复核 · 2026-09-07" && badge.className.includes("review-due"), "visible badge contradicted the new due filter");
    ''')


def _run(script: str) -> None:
    result = subprocess.run(["node", "--input-type=module", "--eval", _HELPERS + script], cwd=ROOT, check=False, capture_output=True, text=True)
    assert result.returncode == 0, result.stdout + result.stderr


_HELPERS = r'''
const view = await import("./static/js/watchlist-queue-view.js");
const elements = new Map();
function element(id) {
  if (!elements.has(id)) elements.set(id, {
    value:"",checked:false,hidden:false,textContent:"",innerHTML:"",attributes:{},listeners:new Map(),
    setAttribute(name,value) {this.attributes[name]=value;},
    querySelectorAll() {return [];},
    addEventListener(event,handler) {const list=this.listeners.get(event)||[];list.push(handler);this.listeners.set(event,list);},
    removeEventListener(event,handler) {this.listeners.set(event,(this.listeners.get(event)||[]).filter(item=>item!==handler));},
    emit(event) {for(const handler of this.listeners.get(event)||[]) handler({target:this,preventDefault(){}});},
  });
  return elements.get(id);
}
globalThis.document = {getElementById:element};
function assert(value,message) {if(!value) throw new Error(message);}
const items = [
  {symbol:"600000.SH",code:"600000",name:"浦发银行",group_name:"银行",note:"股息观察",research_status:"to_research",next_review_date:"2026-09-07",unread_change_count:2},
  {symbol:"000001.SZ",code:"000001",name:"平安银行",group_name:"银行",note:"待核对",research_status:"watching",next_review_date:"2026-09-06",unread_change_count:0},
  {symbol:"600519.SH",code:"600519",name:"贵州茅台",group_name:"复利",note:"耐心研究",research_status:"to_research",next_review_date:"2026-09-08",unread_change_count:1},
  {symbol:"000002.SZ",code:"000002",name:"万科A",group_name:"地产",note:"暂排除",research_status:"excluded",next_review_date:null,unread_change_count:0},
];
'''
