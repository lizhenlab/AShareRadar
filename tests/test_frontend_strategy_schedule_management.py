from __future__ import annotations

import subprocess
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]


def test_schedule_list_is_lazy_and_reads_only_the_loaded_strategy() -> None:
    _node(r'''
      const calls = [];
      const manager = create(async url => { calls.push(url); return page(); });
      manager.setStrategy(strategy());
      assert.equal(calls.length, 0);
      await open(manager);
      const query = new URL(calls[0], "http://local").searchParams;
      assert.equal(query.get("strategy_id"), "7");
      assert.equal(query.get("include_disabled"), "true");
      assert.equal(query.get("page_size"), "20");
      assert.equal(manager.state.page.page, 1);
      assert.match(el("strategyScheduleRows").innerHTML, /固定 v1/);
    ''')


def test_failed_schedule_pagination_retries_the_same_page() -> None:
    _node(r'''
      const pages = [];
      const manager = create(async url => {
        const number = Number(new URL(url, "http://local").searchParams.get("page"));
        pages.push(number);
        if (pages.length === 2) throw new Error("暂时不可用");
        return page(number);
      });
      manager.setStrategy(strategy()); await open(manager);
      await click("strategyScheduleNext");
      assert.equal(manager.state.page.page, 1);
      assert.match(el("strategyScheduleStatus").textContent, /显示上次成功/);
      await click("strategyScheduleNext");
      assert.deepEqual(pages, [1, 2, 2]);
      assert.equal(manager.state.page.page, 2);
    ''')


def test_schedule_toggle_updates_only_the_confirmed_target() -> None:
    _node(r'''
      const writes = [];
      const manager = create(async (url, options) => {
        if (options.method) { writes.push([url, JSON.parse(options.body)]); return schedule(1, false); }
        return {...page(), items:[schedule(1, writes.length === 0)]};
      });
      manager.setStrategy(strategy()); await open(manager); await toggle(1);
      assert.deepEqual(writes, [["/api/strategy-lab/schedules/1", {enabled:false}]]);
      assert.equal(manager.state.page.items[0].enabled, false);
      assert.match(el("strategyScheduleRows").innerHTML, /已停用/);
      assert.match(el("strategyScheduleStatus").textContent, /任务 #1 已停用/);
    ''')


def test_unknown_schedule_write_result_is_not_retried_or_optimistically_committed() -> None:
    _node(r'''
      let writes = 0;
      const manager = create(async (_url, options) => {
        if (options.method) { writes += 1; throw new Error("网络超时"); }
        return {...page(), items:[schedule(1, writes === 0)]};
      });
      manager.setStrategy(strategy()); await open(manager); await toggle(1);
      assert.equal(writes, 1);
      assert.equal(manager.state.page.items[0].enabled, true);
      assert.match(el("strategyScheduleStatus").textContent, /结果未确认.*刷新任务/);
    ''')


def test_late_schedule_list_cannot_replace_another_strategy() -> None:
    _node(r'''
      const pending = deferred();
      const manager = create(async url => new URL(url, "http://local").searchParams.get("strategy_id") === "7" ? pending.promise : page(1, 8));
      manager.setStrategy(strategy()); const first = open(manager);
      manager.setStrategy(strategy(8)); await settle();
      pending.resolve(page()); await first;
      assert.equal(manager.state.strategy.strategy_id, 8);
      assert.equal(manager.state.page.items[0].strategy_id, 8);
    ''')


def test_archived_strategy_can_stop_a_task_but_cannot_resume_it() -> None:
    _node(r'''
      let writes = 0;
      const manager = create(async (_url, options) => {
        if (options.method) { writes += 1; return schedule(1, false); }
        return {...page(), items:[schedule(1, writes === 0)]};
      });
      manager.setStrategy({...strategy(), archived:true}); await open(manager);
      await toggle(1); await toggle(1);
      assert.equal(writes, 1);
      assert.match(el("strategyScheduleRows").innerHTML, /已归档.*无法恢复/);
    ''')


def test_invalid_schedule_page_keeps_last_verified_page() -> None:
    _node(r'''
      let reads = 0;
      const manager = create(async () => ++reads === 1 ? page() : page(2, 99));
      manager.setStrategy(strategy()); await open(manager); await click("strategyScheduleNext");
      assert.equal(manager.state.page.page, 1);
      assert.match(el("strategyScheduleStatus").textContent, /读取失败/);
    ''')


def test_late_schedule_write_does_not_mutate_another_strategy_view() -> None:
    _node(r'''
      const pending = deferred(); let writeSignal;
      const manager = create(async (url, options) => {
        if (options.method) { writeSignal = options.signal; return pending.promise; }
        return page(1, Number(new URL(url, "http://local").searchParams.get("strategy_id")));
      });
      manager.setStrategy(strategy()); await open(manager); const write = toggle(1);
      manager.setStrategy(strategy(8)); await settle();
      pending.resolve(schedule(1, false)); await write;
      assert.equal(writeSignal, undefined, "writes must not inherit a read abort signal");
      assert.equal(manager.state.page.items[0].strategy_id, 8);
      assert.equal(manager.state.page.items[0].enabled, true);
      assert.doesNotMatch(el("strategyScheduleStatus").textContent, /任务 #1 已停用/);
    ''')


def test_closing_schedule_panel_releases_cancelled_read_controls() -> None:
    _node(r'''
      const pending = deferred();
      const manager = create(async () => pending.promise);
      manager.setStrategy(strategy()); const first = open(manager);
      el("strategyScheduleManager").open = false;
      await el("strategyScheduleManager").handlers.get("toggle")();
      assert.equal(manager.state.reading, false);
      assert.equal(el("strategyScheduleManager").attributes["aria-busy"], "false");
      pending.resolve(page()); await first;
      assert.equal(manager.state.page, null);
    ''')


def test_created_schedule_is_acknowledged_if_list_refresh_fails() -> None:
    _node(r'''
      let reads = 0;
      const manager = create(async () => { if (++reads > 1) throw new Error("offline"); return page(); });
      manager.setStrategy(strategy()); await open(manager);
      await manager.confirmCreation(schedule(2));
      assert.match(el("strategyScheduleStatus").textContent, /任务 #2 已创建.*同步未完成/);
      assert.equal(manager.state.page.page, 1);
    ''')


def test_mismatched_schedule_acknowledgement_is_never_applied() -> None:
    _node(r'''
      const manager = create(async (_url, options) => options.method ? schedule(2, false) : page());
      manager.setStrategy(strategy()); await open(manager); await toggle(1);
      assert.equal(manager.state.page.items[0].schedule_id, 1);
      assert.equal(manager.state.page.items[0].enabled, true);
      assert.match(el("strategyScheduleStatus").textContent, /结果未确认/);
    ''')


def test_schedule_mutation_ownership_blocks_duplicate_and_parent_busy_writes() -> None:
    _node(r'''
      const pending = deferred(); let writes=0; const owners=[];
      const manager = createStrategyScheduleManager({root:{getElementById:el},
        onWritingChange: busy => owners.push(busy), fetcher:async (_url, options) => {
          if (options.method) { writes+=1; return pending.promise; } return page();
        }});
      manager.setStrategy(strategy()); await open(manager);
      manager.setBusy(true); await toggle(1); assert.equal(writes, 0);
      manager.setBusy(false); const first=toggle(1); await toggle(1);
      assert.equal(writes, 1); assert.deepEqual(owners, [true]);
      pending.resolve(schedule(1,false)); await first;
      assert.deepEqual(owners, [true,false]);
    ''')


def test_confirmed_toggle_refreshes_server_order_before_allowing_next_page() -> None:
    _node(r'''
      const rows=Array.from({length:41},(_,i)=>schedule(i+1)); const reads=[];
      const manager=create(async(url,options)=>{
        if(options.method) { rows.find(item=>item.schedule_id===40).enabled=false; return schedule(40,false); }
        const number=Number(new URL(url,"http://local").searchParams.get("page")); reads.push(number);
        const ordered=[...rows].sort((left,right)=>Number(right.enabled)-Number(left.enabled)||left.schedule_id-right.schedule_id);
        return {items:ordered.slice((number-1)*20,number*20),total:41,page:number,page_size:20,page_count:3};
      });
      manager.setStrategy(strategy()); await open(manager); await click("strategyScheduleNext");
      await toggle(40);
      assert.equal(manager.state.page.items.at(-1).schedule_id,41,"next page's first task must move into refreshed page 2");
      await click("strategyScheduleNext");
      assert.equal(manager.state.page.items[0].schedule_id,40);
      assert.deepEqual(reads,[1,2,2,3]);
    ''')


def test_confirmed_toggle_survives_failed_sorted_page_refresh() -> None:
    _node(r'''
      let reads=0;
      const manager=create(async(_url,options)=>{
        if(options.method) return schedule(1,false);
        if(++reads>1) throw new Error("列表暂时离线");
        return page();
      });
      manager.setStrategy(strategy()); await open(manager); await toggle(1);
      assert.equal(manager.state.page.items[0].enabled,false);
      assert.match(el("strategyScheduleStatus").textContent,/任务 #1 已停用.*同步未完成/);
      assert.equal(manager.state.writing,false);
      assert.equal(el("strategyScheduleNext").disabled,true);
      await click("strategyScheduleNext"); assert.equal(reads,2,"paging requires a refreshed server order");
    ''')


def test_unknown_toggle_result_requires_refreshed_order_before_paging() -> None:
    _node(r'''
      const rows=Array.from({length:41},(_,i)=>schedule(i+1)); const reads=[]; let writes=0;
      const manager=create(async(url,options)=>{
        if(options.method) {
          writes+=1; rows.find(item=>item.schedule_id===40).enabled=false;
          throw new Error("回执超时");
        }
        const number=Number(new URL(url,"http://local").searchParams.get("page")); reads.push(number);
        const ordered=structuredClone(rows).sort((left,right)=>Number(right.enabled)-Number(left.enabled)||left.schedule_id-right.schedule_id);
        return {items:ordered.slice((number-1)*20,number*20),total:41,page:number,page_size:20,page_count:3};
      });
      manager.setStrategy(strategy()); await open(manager); await click("strategyScheduleNext");
      await toggle(40);
      assert.equal(manager.state.page.items.at(-1).enabled,true,"unknown ACK must not change the last confirmed state");
      assert.equal(el("strategyScheduleNext").disabled,true,"unknown write may have reordered the server list");
      await click("strategyScheduleNext"); assert.deepEqual(reads,[1,2]);
      await click("strategyScheduleRefresh");
      assert.equal(manager.state.page.items.at(-1).schedule_id,41);
      await click("strategyScheduleNext");
      assert.equal(manager.state.page.items[0].schedule_id,40);
      assert.deepEqual(reads,[1,2,2,3]); assert.equal(writes,1);
    ''')


def test_reopening_panel_during_pending_write_cannot_read_old_state() -> None:
    _node(r'''
      let reads=0; const pending=deferred(); let confirmed=false;
      const manager=create(async(_url,options)=>{
        if(options.method) return pending.promise;
        reads+=1; return {...page(),items:[schedule(1,!confirmed)]};
      });
      manager.setStrategy(strategy()); await open(manager); const write=toggle(1);
      el("strategyScheduleManager").open=false; await el("strategyScheduleManager").handlers.get("toggle")();
      el("strategyScheduleManager").open=true; await el("strategyScheduleManager").handlers.get("toggle")();
      assert.equal(reads,1);
      confirmed=true; pending.resolve(schedule(1,false)); await write;
      assert.equal(reads,2); assert.equal(manager.state.page.items[0].enabled,false);
    ''')


def test_unknown_schedule_creation_locks_existing_manager_pagination() -> None:
    from tests.test_frontend_strategy_recovery import _run_node as run_strategy_node

    run_strategy_node(r'''
      let writes=0; const pages=[]; const pending=deferred();
      const controller=makeController(async(url,options={})=>{
        if(options.method === "POST") { writes+=1; await pending.promise; throw new Error("创建回执超时"); }
        const number=Number(new URL(url,"http://local").searchParams.get("page")); pages.push(number);
        return {items:[],total:41,page:number,page_size:20,page_count:3};
      });
      loadEditor(controller,strategy(7));
      controller.scheduleManager.setStrategy(controller.state.strategy);
      element("strategyScheduleManager").open=true;
      await controller.scheduleManager.refresh(2);
      const write=click("strategyCreateSchedule");
      await controller.scheduleManager.refresh();
      assert.deepEqual(pages,[2],"reopening during creation must not restore stale page ordering");
      pending.resolve(); await write;
      assert.equal(writes,1);
      assert.equal(element("strategyScheduleNext").disabled,true,"creation may have inserted a task before the current page");
      await click("strategyScheduleNext");
      assert.deepEqual(pages,[2],"unknown creation must not automatically read or page over changed ordering");
      assert.match(status().textContent,/创建回执超时/);
      assert.equal(controller.scheduleManager.state.page.page,2);
    ''')


def _node(body: str) -> None:
    result = subprocess.run(
        ["node", "--input-type=module", "-e", _HARNESS + body],
        cwd=ROOT, capture_output=True, text=True, check=False,
    )
    assert result.returncode == 0, result.stderr


_HARNESS = r'''
import assert from "node:assert/strict";
import { createStrategyScheduleManager } from "./static/js/strategy-schedule-manager.js";
const elements = new Map();
function el(id) {
  if (!elements.has(id)) elements.set(id, { textContent:"", innerHTML:"", dataset:{}, attributes:{},
    disabled:false, open:false, handlers:new Map(),
    setAttribute(key,value) { this.attributes[key] = value; },
    addEventListener(key,callback) { this.handlers.set(key,callback); }, querySelectorAll() { return []; } });
  return elements.get(id);
}
function strategy(id=7) { return {strategy_id:id, strategy_version:1, fingerprint:"a".repeat(64), archived:false, spec:{name:`策略${id}`}}; }
function schedule(id=1, enabled=true, strategyId=7) { return {schedule_id:id, strategy_id:strategyId,
  strategy_version:1, strategy_fingerprint:"a".repeat(64), cadence:"daily_after_close", mode:"official",
  notional_cash_cny:100000, alert_conditions:[], enabled, last_execution_id:null,
  last_market_scan_run_id:null, created_at:"2026-09-07T00:00:00Z", updated_at:"2026-09-07T00:00:00Z"}; }
function page(number=1, strategyId=7) { return {items:[schedule(1,true,strategyId)], total:101, page:number, page_size:20, page_count:6}; }
function create(fetcher) { return createStrategyScheduleManager({root:{getElementById:el}, fetcher}); }
function deferred() { let resolve; const promise = new Promise(done => { resolve=done; }); return {promise,resolve}; }
async function settle() { for (let i=0;i<10;i+=1) await new Promise(resolve => setImmediate(resolve)); }
async function open(manager) { el("strategyScheduleManager").open=true; return manager.refresh(); }
async function click(id) { return el(id).handlers.get("click")(); }
async function toggle(id) { const button = {dataset:{strategyScheduleToggle:String(id)}};
  return el("strategyScheduleRows").handlers.get("click")({target:{closest() { return button; }}}); }
'''
