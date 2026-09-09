from __future__ import annotations

import pytest

from tests.test_frontend_notifications import _run_node


PRELUDE = r'''
const { installAppDom } = await import("./tests/frontend_app_flow_helpers.mjs");
const dom = installAppDom();
const notifications = await import("./static/js/notifications.js");
const A = "a".repeat(32), B = "b".repeat(32);
const values = new Map();
const storage = {getItem:key=>values.get(key)??null, setItem:(key,value)=>values.set(key,value), removeItem:key=>values.delete(key)};
const state = {symbol:"000001.SZ", primaryView:"research", workspaceView:"overview", loadSeq:0};
storage.setItem(notifications.ALERT_NOTIFICATION_CURSOR_KEY, JSON.stringify({streamId:A,id:0}));
const issued = []; let focused = 0, closed = 0;
class NotificationApi {
  static permission = "granted";
  constructor(title, options) { this.title=title; this.options=options; issued.push(this); }
  close() { closed += 1; }
}
globalThis.focus = () => { focused += 1; };
const event = id => ({id, rule_id:9, symbol:"600519.SH", code:"600519", market:"SH", stock_name:"原股票", name:"原规则",
  event_type:"触发", message:"原触发内容", price:123.45, change_pct:2.5, threshold:120, created_at:"2026-09-09T02:00:00.000Z"});
const batch = events => ({requestedCursor:{streamId:A,id:0},streamId:A,baselineId:0,cursorId:events.at(-1).id,events});
const cursor = () => JSON.parse(storage.getItem(notifications.ALERT_NOTIFICATION_CURSOR_KEY));
function assert(value, message) { if(!value) throw new Error(message); }
'''


@pytest.mark.parametrize("summary", [False, True])
def test_notification_delivery_click_retains_original_target_without_id_lookup(summary: bool) -> None:
    _run_node(PRELUDE + f"const summary = {str(summary).lower()};" + r'''
        let captured = null, reads = 0;
        globalThis.fetch = () => { reads += 1; throw new Error("no lookup allowed"); };
        const events = summary ? [1,2,3,4].map(event) : [event(1)];
        const delivered = notifications.deliverAlertNotifications(state, batch(events), {
          storage, NotificationApi, onNotificationClick: target => { captured=target; },
        });
        events[0].message="later replacement"; events[0].symbol="000001.SZ";
        storage.setItem(notifications.ALERT_NOTIFICATION_CURSOR_KEY, JSON.stringify({streamId:B,id:1}));
        issued[0].onclick();
        assert(captured && captured.streamId===A, "click omitted the original stream-bound target");
        assert(Object.isFrozen(captured), "notification target remained mutable");
        if(summary) {
          assert(captured.kind==="summary" && captured.count===4 && !captured.event, "summary invented a single event");
        } else {
          assert(captured.kind==="event" && captured.event.symbol==="600519.SH" && captured.event.message==="原触发内容", "click retargeted the original event");
          assert(captured.event.price===123.45 && Object.isFrozen(captured.event), "frozen trigger facts changed");
        }
        assert(reads===0 && cursor().streamId===B && cursor().id===1, "click read a reused id or changed cursor");
        assert(delivered===events.length && focused===1 && closed===1, "delivery/focus/close contract changed");
    ''')


@pytest.mark.parametrize("failure", ["throw", "reject"])
def test_notification_delivery_callback_failure_does_not_retry_constructed_alert(failure: str) -> None:
    _run_node(PRELUDE + f"const failure = {failure!r};" + r'''
        let called=0;
        const options={storage,NotificationApi,onNotificationClick:()=>{
          called += 1;
          if(failure==="throw") throw new Error("navigation failed");
          return Promise.reject(new Error("navigation failed"));
        }};
        assert(notifications.deliverAlertNotifications(state,batch([event(1)]),options)===1,"notification not delivered");
        issued[0].onclick();
        await new Promise(resolve=>setTimeout(resolve,0));
        assert(called===1,"click callback was not invoked");
        assert(notifications.deliverAlertNotifications(state,batch([event(1)]),options)===0,"navigation failure redelivered an alert");
        assert(issued.length===1 && cursor().id===1 && !state.alertNotificationDeliveryFailed,"navigation failure changed delivery state");
    ''')


def test_notification_delivery_constructor_failure_keeps_retry_cursor_and_does_not_navigate() -> None:
    _run_node(PRELUDE + r'''
        let called=0;
        class FailingNotification { constructor(){throw new Error("OS failed");} }
        const count=notifications.deliverAlertNotifications(state,batch([event(1)]),{
          storage,NotificationApi:FailingNotification,onNotificationClick:()=>{called+=1;},
        });
        assert(count===0 && cursor().id===0 && state.alertNotificationDeliveryFailed,"constructor failure advanced cursor");
        assert(called===0,"failed constructor navigated");
    ''')


def test_notification_delivery_without_callback_still_focuses_and_closes() -> None:
    _run_node(PRELUDE + r'''
        assert(notifications.deliverAlertNotifications(state,batch([event(1)]),{storage,NotificationApi})===1,"delivery failed");
        issued[0].onclick();
        assert(focused===1 && closed===1 && cursor().id===1,"optional callback broke existing consumers");
    ''')


NAVIGATION = r'''
const { createNotificationNavigation } = await import("./static/js/notification-navigation.js");
const panel = dom.element("notificationDetail");
const pending = [], selections = [];
let navigation;
const context = {
  state,
  setActiveSymbol(symbol) { navigation.cancel(); state.symbol=symbol; selections.push(symbol); },
  setWorkspaceView(view) { state.workspaceView=view; state.primaryView="research"; },
  loadAll() { state.loadSeq += 1; return new Promise((resolve,reject)=>pending.push({resolve,reject})); },
};
navigation=createNotificationNavigation(context);
const target = (id=1,symbol="600519.SH") => ({kind:"event",streamId:A,event:{...event(id),symbol}});
'''


def test_notification_navigation_loads_subject_and_keeps_trigger_prices_separate() -> None:
    _run_node(PRELUDE + NAVIGATION + r'''
        const opening=navigation.open(target());
        assert(state.symbol==="600519.SH" && state.workspaceView==="tools" && state.primaryView==="research", "wrong stock/workspace opened");
        assert(!panel.hidden && panel.innerHTML.includes("通知触发时记录") && panel.innerHTML.includes("123.45"), "original trigger details not immediately visible");
        pending[0].resolve(true);
        assert(await opening,"current navigation failed");
        assert(panel.innerHTML.includes("不是当前行情") && panel.innerHTML.includes("原触发内容"),"loaded quote replaced trigger snapshot");
        assert(cursor().id===0,"viewing snapshot marked notification progress");
    ''')


@pytest.mark.parametrize("reject", [False, True])
def test_notification_navigation_failure_preserves_subject_snapshot_and_old_stock_context(reject: bool) -> None:
    _run_node(PRELUDE + NAVIGATION + f"const reject = {str(reject).lower()};" + r'''
        const opening=navigation.open(target());
        state.symbol="000001.SZ";
        state.failedLoadSymbol="600519.SH";
        if(reject) pending[0].reject(new Error("provider detail must not leak")); else pending[0].resolve(false);
        assert(await opening===false,"failed current analysis claimed success");
        assert(state.symbol==="000001.SZ" && panel.innerHTML.includes("600519.SH"),"snapshot was relabelled as old/current stock");
        assert(panel.innerHTML.includes("当前行情未载入") && panel.innerHTML.includes("上次成功分析"),"fallback context was not disclosed");
        assert(!panel.innerHTML.includes("provider detail") && !panel.hidden,"failure erased original details or leaked internals");
    ''')


def test_notification_navigation_later_alert_owns_out_of_order_completion() -> None:
    _run_node(PRELUDE + NAVIGATION + r'''
        const first=navigation.open(target(1));
        const second=navigation.open(target(2,"000001.SZ"));
        pending[1].resolve(true); assert(await second,"latest alert failed");
        const visible=panel.innerHTML;
        pending[0].reject(new Error("late failure"));
        assert(await first===false && panel.innerHTML===visible,"late alert rewrote the newer detail");
        assert(panel.innerHTML.includes("000001.SZ") && state.symbol==="000001.SZ","navigation identity was lost");
    ''')


@pytest.mark.parametrize("change", ["symbol", "primary", "workspace", "reload"])
def test_notification_navigation_manual_selection_or_reload_suppresses_old_result(change: str) -> None:
    _run_node(PRELUDE + NAVIGATION + f"const change = {change!r};" + r'''
        const opening=navigation.open(target());
        if(change==="symbol") context.setActiveSymbol("000001.SZ");
        if(change==="primary") { navigation.cancel(); state.primaryView="monitor"; }
        if(change==="workspace") { navigation.cancel(); state.workspaceView="overview"; }
        if(change==="reload") state.loadSeq += 1;
        const html=panel.innerHTML, hidden=panel.hidden;
        pending[0].resolve(true);
        assert(await opening===false,"stale selection was accepted");
        assert(panel.innerHTML===html && panel.hidden===hidden,"late completion rewrote a cancelled/current view");
    ''')


def test_notification_navigation_summary_never_selects_a_fictional_event() -> None:
    _run_node(PRELUDE + NAVIGATION + r'''
        assert(await navigation.open({kind:"summary",streamId:A,count:7}),"summary did not open");
        assert(state.symbol==="000001.SZ" && state.workspaceView==="tools" && selections.length===0 && pending.length===0,"summary invented a stock load");
        assert(panel.innerHTML.includes("7 条新预警") && panel.innerHTML.includes("未指向单条记录"),"summary misrepresented its scope");
        assert(!panel.innerHTML.includes("触发时价格"),"summary fabricated event facts");
    ''')


@pytest.mark.parametrize("invalid", ["stream", "symbol", "id", "summary_count"])
def test_notification_navigation_rejects_invalid_target_without_read_or_selection(invalid: str) -> None:
    _run_node(PRELUDE + NAVIGATION + f"const invalid = {invalid!r};" + r'''
        const notice=target();
        if(invalid==="stream") notice.streamId="unbound";
        if(invalid==="symbol") notice.event.symbol="javascript:bad";
        if(invalid==="id") notice.event.id=-1;
        if(invalid==="summary_count") {notice.kind="summary";notice.count=Infinity;}
        assert(await navigation.open(notice)===false,"invalid navigation target accepted");
        assert(selections.length===0 && pending.length===0 && state.workspaceView==="overview","invalid target caused side effects");
    ''')


def test_notification_navigation_escapes_snapshot_text_and_retains_numeric_zero() -> None:
    _run_node(PRELUDE + NAVIGATION + r'''
        const notice=target();
        notice.event.message='<img src=x onerror="boom()">';
        notice.event.name='<script>boom()</script>';notice.event.price=0;notice.event.change_pct=0;
        const opening=navigation.open(notice);
        notice.event.message="later external mutation";
        pending[0].resolve(true); await opening;
        assert(!panel.innerHTML.includes("<img") && !panel.innerHTML.includes("<script>"),"snapshot inserted executable markup");
        assert(panel.innerHTML.includes("&lt;img") && !panel.innerHTML.includes("later external mutation"),"snapshot was not copied and escaped");
        assert(panel.innerHTML.includes("触发时价格 0.00") && panel.innerHTML.includes("涨跌幅 0.00%"),"valid zero was treated as missing");
    ''')
