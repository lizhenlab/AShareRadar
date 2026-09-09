"""Due queues keep server-filtered page identity separate from ordinary plans."""

from __future__ import annotations

import pytest

from tests.test_frontend_review_plan_navigation import SETUP
from tests.test_frontend_review_scan import _run_node


def test_ordinary_dashboard_does_not_request_or_intersect_a_partial_due_queue() -> None:
    _run_node(SETUP + DUE_SETUP + r'''
      const state = {};
      const calls = [];
      globalThis.fetch = async url => {
        calls.push(String(url));
        if (String(url).endsWith("/summary")) return json(summary());
        if (String(url).includes("/due")) throw new Error("ordinary mode must not load due");
        return json([detail(7)]);
      };
      assert(await reviews.loadAdviceReviewDashboard(state), "ordinary dashboard failed");
      assert(calls.length === 2 && !calls.some(url => url.includes("/due")), "ordinary view fetched partial due membership");
      assert(state.adviceReviewDashboardDetails.length === 1, "ordinary plans were not retained");
    ''')


def test_due_navigation_uses_server_filters_and_pins_the_continuation_contract() -> None:
    _run_node(SETUP + DUE_SETUP + r'''
      const state = { adviceReviewDashboardDetails: [detail(7)] };
      const original = state.adviceReviewDashboardDetails;
      bind(state);
      el("reviewDashboardStatus").value = "due";
      el("reviewDashboardSymbol").value = "  sz  ";
      el("reviewDashboardFrom").value = "2026-07-01";
      el("reviewDashboardHorizon").value = "20";
      const calls = [];
      globalThis.fetch = async (url, options) => {
        calls.push({ url: new URL(url, "http://local"), method: options.method || "GET" });
        const number = Number(calls.at(-1).url.searchParams.get("page"));
        return json(duePage(number, 51, "000001.SZ"));
      };
      await reviews.updateAdviceReviewDashboardFilters(state);
      assert(calls.length === 1 && calls[0].url.searchParams.get("symbol") === "SZ", "filter was not sent to server");
      assert(calls[0].url.searchParams.get("from_date") === "2026-07-01" && calls[0].url.searchParams.get("horizon_days") === "20", "filter identity was lost");
      assert(!calls[0].url.searchParams.has("as_of") && !calls[0].url.searchParams.has("snapshot_token"), "first page invented a snapshot");
      await clickDue("reviewDueNext");
      assert(calls[1].url.searchParams.get("page") === "2" && calls[1].url.searchParams.get("as_of") === AS_OF
        && calls[1].url.searchParams.get("snapshot_token") === TOKEN, "continuation was not bound to first response");
      assert(el("reviewDashboardQueue").innerHTML.includes('data-review-dashboard-plan="251"'), "record beyond old caps is unreachable");
      assert(el("reviewDuePageStatus").textContent.includes("51") && el("reviewDuePageStatus").textContent.includes(AS_OF), "page total or effective time hidden");
      assert(state.adviceReviewDashboardDetails === original && calls.every(call => call.method === "GET"), "paging replaced ordinary plans or performed a write");
    ''')


@pytest.mark.parametrize("status", [400, 503])
def test_failed_due_page_keeps_successful_rows_and_retries_the_same_page(status: int) -> None:
    _run_node(SETUP + DUE_SETUP + f"const failureStatus={status};" + r'''
      const state = {}; bind(state); el("reviewDashboardStatus").value = "due";
      const calls = []; let failed = false;
      globalThis.fetch = async url => {
        const page = Number(new URL(url,"http://local").searchParams.get("page")); calls.push(page);
        if (page === 2 && !failed) { failed = true; return json({detail:"private-server-error"}, failureStatus); }
        return json(duePage(page, 51));
      };
      await reviews.updateAdviceReviewDashboardFilters(state);
      const html = el("reviewDashboardQueue").innerHTML;
      await clickDue("reviewDueNext");
      assert(el("reviewDashboardQueue").innerHTML === html && el("reviewDuePageStatus").textContent.includes("1 / 2"), "failed page replaced successful page");
      assert(!el("reviewDueRetry").hidden && el("reviewDueFeedback").textContent.includes("重试"), "page has no retry");
      if(failureStatus===503) assert(!el("reviewDueFeedback").textContent.includes("private-server-error"), "5xx detail leaked");
      await clickDue("reviewDueRetry");
      assert(JSON.stringify(calls) === "[1,2,2]" && el("reviewDuePageStatus").textContent.includes("2 / 2"), "retry skipped failed page");
      assert(el("reviewDueFeedback").hidden, "success kept old error");
    ''')


def test_due_snapshot_conflict_requires_an_explicit_fresh_first_page() -> None:
    _run_node(SETUP + DUE_SETUP + r'''
      const state={}; bind(state); el("reviewDashboardStatus").value="due";
      const calls=[];
      globalThis.fetch=async url=>{
        const query=new URL(url,"http://local").searchParams; calls.push(Object.fromEntries(query));
        return query.get("page")==="2"?json({detail:"队列变化"},409):json(duePage(1,51));
      };
      await reviews.updateAdviceReviewDashboardFilters(state); await clickDue("reviewDueNext");
      assert(el("reviewDueFeedback").textContent.includes("首页") && el("reviewDueNext").disabled, "conflict continued an invalid snapshot");
      await clickDue("reviewDueRetry");
      assert(calls[2].page==="1" && !calls[2].as_of && !calls[2].snapshot_token, "restart reused a stale token");
    ''')


def test_due_failure_is_unavailable_and_not_an_empty_match() -> None:
    _run_node(SETUP + DUE_SETUP + r'''
      const state={}; el("reviewDashboardStatus").value="due";
      globalThis.fetch=async()=>json({detail:"unavailable"},503);
      await reviews.updateAdviceReviewDashboardFilters(state);
      assert(!el("reviewDashboardQueue").innerHTML.includes("没有符合筛选"), "failure was shown as an empty match");
      assert(el("reviewDashboardQueue").innerHTML.includes("暂不可用"), "failure has no unavailable state");
      globalThis.fetch=async()=>json(duePage(1,0));
      await reviews.updateAdviceReviewDashboardFilters(state);
      assert(el("reviewDashboardQueue").innerHTML.includes("没有符合筛选"), "valid empty page did not finish");
    ''')


@pytest.mark.parametrize("change", ["filter", "mode", "abort"])
def test_stale_due_response_does_not_replace_a_new_filter_or_mode(change: str) -> None:
    _run_node(SETUP + DUE_SETUP + f'const change="{change}";' + r'''
      const state={adviceReviewDashboardDetails:[detail(7)]}; bind(state);
      const first=deferred(); let calls=0;
      const controller=new AbortController();
      globalThis.fetch=async()=>++calls===1?first.promise:json(duePage(1,1,"000001.SZ"));
      el("reviewDashboardStatus").value="due";
      const pending=reviews.updateAdviceReviewDashboardFilters(state,{signal:controller.signal});
      if(change==="filter") { el("reviewDashboardSymbol").value="000001"; await reviews.updateAdviceReviewDashboardFilters(state); }
      else if(change==="mode") { el("reviewDashboardStatus").value="all"; await reviews.updateAdviceReviewDashboardFilters(state); }
      else { controller.abort(); await pending; }
      const html=el("reviewDashboardQueue").innerHTML;
      first.resolve(json(duePage(1,1)));
      await pending;
      assert(el("reviewDashboardQueue").innerHTML===html, "late response replaced newer view");
      if(change==="mode") assert(el("reviewDueControls").hidden, "inactive due controls remained exposed");
    ''')


@pytest.mark.parametrize("invalid", ["page", "token", "count", "duplicates", "evaluated", "filter"])
def test_due_page_rejects_unbound_or_inconsistent_payloads(invalid: str) -> None:
    _run_node(SETUP + DUE_SETUP + f'const invalid="{invalid}";' + r'''
      const state={}; el("reviewDashboardStatus").value="due";
      el("reviewDashboardSymbol").value="600519";
      const payload=duePage(1,2);
      if(invalid==="page") payload.page=2;
      if(invalid==="token") payload.snapshot_token="bad";
      if(invalid==="count") payload.total=3;
      if(invalid==="duplicates") payload.items[1]=payload.items[0];
      if(invalid==="evaluated") payload.items[0].latest_evaluation=completeReviewEvaluation({},payload.items[0].plan);
      if(invalid==="filter") payload.items[0]=duePage(1,1,"000001.SZ").items[0];
      globalThis.fetch=async()=>json(payload);
      await reviews.updateAdviceReviewDashboardFilters(state);
      assert(el("reviewDashboardQueue").innerHTML.includes("暂不可用") && !el("reviewDueRetry").hidden, "invalid page was accepted");
    ''')


@pytest.mark.parametrize("changed", ["snapshot_token", "as_of"])
def test_due_continuation_rejects_a_different_valid_snapshot_and_retains_its_page(changed: str) -> None:
    _run_node(SETUP + DUE_SETUP + f'const changed="{changed}";' + r'''
      const state={}; bind(state); el("reviewDashboardStatus").value="due";
      const calls=[]; let wrong=true;
      globalThis.fetch=async url=>{
        const query=new URL(url,"http://local").searchParams; calls.push(Object.fromEntries(query));
        const payload=duePage(Number(query.get("page")),51);
        if(payload.page===2 && wrong) payload[changed]=changed==="as_of"?"2026-09-09 15:15:00":"b".repeat(64);
        return json(payload);
      };
      await reviews.updateAdviceReviewDashboardFilters(state);
      const first=el("reviewDashboardQueue").innerHTML;
      await clickDue("reviewDueNext");
      assert(el("reviewDashboardQueue").innerHTML===first && state.adviceReviewDue.page.page===1, "valid but different snapshot replaced first page");
      assert(!el("reviewDueRetry").hidden && !el("reviewDueFeedback").hidden, "changed binding was treated as success");
      wrong=false; await clickDue("reviewDueRetry");
      assert(calls[2].snapshot_token===TOKEN && calls[2].as_of===AS_OF && state.adviceReviewDue.page.page===2, "retry did not preserve original continuation");
    ''')


def test_external_due_abort_has_no_false_empty_success_and_explicit_refresh_recovers() -> None:
    _run_node(SETUP + DUE_SETUP + r'''
      const state={}; bind(state); el("reviewDashboardStatus").value="due";
      const controller=new AbortController(), delayed=deferred(); let calls=0, requestSignal;
      globalThis.fetch=async(_url,options)=>{ requestSignal=options.signal; return ++calls===1?delayed.promise:json(duePage(1,1)); };
      const pending=reviews.refreshAdviceReviewDue(state,{signal:controller.signal});
      controller.abort(); await pending;
      assert(requestSignal.aborted && state.adviceReviewDue.page===null && state.adviceReviewDue.phase==="idle", "external abort did not release the request");
      assert(!el("reviewDashboardQueue").innerHTML.includes("没有符合筛选"), "abort was presented as an empty success");
      await clickDue("reviewDueRefresh");
      const current=el("reviewDashboardQueue").innerHTML;
      delayed.resolve(json(duePage(1,0))); await Promise.resolve();
      assert(calls===2 && current.includes('data-review-dashboard-plan="201"') && el("reviewDashboardQueue").innerHTML===current, "refresh did not recover from external cancellation");
    ''')


def test_rapid_due_filter_input_debounces_without_dispatching_superseded_reads() -> None:
    _run_node(SETUP + DUE_SETUP + r'''
      const state={}; bind(state); el("reviewDashboardStatus").value="due";
      const calls=[];
      globalThis.fetch=async url=>{calls.push(String(url));return json(duePage(1,1,"000001.SZ"));};
      el("reviewDashboardSymbol").value="6";
      const old=reviews.updateAdviceReviewDashboardFilters(state,{debounce:true});
      el("reviewDashboardSymbol").value="000001";
      const current=reviews.updateAdviceReviewDashboardFilters(state,{debounce:true});
      await Promise.all([old,current]);
      assert(calls.length===1 && calls[0].includes("symbol=000001"), "typing dispatched a superseded filter");
      assert(el("reviewDashboardQueue").innerHTML.includes("000001.SZ"), "final filter was not rendered");
    ''')


@pytest.mark.parametrize("read_failure", [False, True])
def test_global_due_batch_receipt_survives_dashboard_readback_without_filtering_the_write(read_failure: bool) -> None:
    _run_node(SETUP + DUE_SETUP + f'const readFailure={str(read_failure).lower()};' + r'''
      const state={}; el("reviewDashboardStatus").value="due"; el("reviewDashboardSymbol").value="000001";
      const calls=[];
      globalThis.fetch=async(url,options={})=>{
        calls.push({url:String(url),method:options.method||"GET",body:options.body});
        if(options.method==="POST") return json(batch());
        if(String(url).includes("/summary")) return readFailure?json({detail:"private-readback"},503):json(summary());
        if(String(url).includes("/due")) return json(duePage(1,1,"000001.SZ"));
        return json([detail(7)]);
      };
      const result=await reviews.evaluateDueAdviceReviews(state);
      assert(result.attempted_count===3 && calls[0].url==="/api/reviews/evaluate-due?limit=100" && calls[0].body==="{}", "batch was scoped to a page or filter");
      const feedback=el("reviewDashboardFeedback").textContent;
      assert(feedback.includes("全局") && feedback.includes("3") && feedback.includes("正式完成 1") && feedback.includes("失败 1"), "readback erased the completed batch receipt");
      if(readFailure) assert(feedback.includes("统计") && !feedback.includes("private-readback"), "readback failure was lost or leaked");
      assert(calls.filter(call=>call.method==="POST").length===1, "readback retried committed work");
    ''')


@pytest.mark.parametrize("stage", ["post", "readback", "abort"])
def test_late_global_batch_receipt_does_not_overwrite_a_new_view(stage: str) -> None:
    _run_node(SETUP + DUE_SETUP + f'const stage="{stage}";' + r'''
      const state={symbol:"600519.SH",loadSeq:1,primaryView:"review",workspaceView:"replay"};
      const delayed=deferred(), controller=new AbortController(); let reads=0;
      globalThis.fetch=async(url,options={})=>{
        if(options.method==="POST") return stage==="readback"?json(batch()):delayed.promise;
        reads+=1;
        if(String(url).includes("summary")) return delayed.promise;
        return json([]);
      };
      const pending=reviews.evaluateDueAdviceReviews(state,{signal:controller.signal});
      if(stage==="readback") for(let i=0;i<20 && !reads;i+=1) await new Promise(resolve=>setImmediate(resolve));
      state.symbol="000001.SZ"; state.loadSeq+=1;
      el("reviewDashboardFeedback").textContent="new-page-feedback";
      if(stage==="abort") controller.abort();
      delayed.resolve(json(stage==="readback"?summary():batch()));
      if(stage==="abort") { try { await pending; } catch(error) { assert(error.name==="AbortError", "abort changed error contract"); } }
      else await pending;
      assert(el("reviewDashboardFeedback").textContent==="new-page-feedback", "late batch overwrote a different page");
      if(stage==="post") assert(reads===0, "late commit launched new page reads");
    ''')


DUE_SETUP = r'''
  const AS_OF="2026-09-08 15:15:00", TOKEN="a".repeat(64);
  function duePage(number=1,total=51,symbol="600519.SH") {
    const count=Math.max(0,Math.min(50,total-(number-1)*50));
    return {items:Array.from({length:count},(_,i)=>({...detail(201+(number-1)*50+i,symbol),due_date:"2026-07-29",overdue_trading_days:2})),
      total,page:number,page_size:50,page_count:Math.ceil(total/50),as_of:AS_OF,snapshot_token:TOKEN};
  }
  function batch() { return {candidate_count:3,attempted_count:3,evaluated_count:1,insufficient_count:1,pending_count:0,failed_count:1,
    items:[{status:"evaluated"},{status:"insufficient"},{status:"failed"}]}; }
  function summary() { return {generated_at:"2026-09-09 15:00:00",total_plan_count:1,pending_count:1,evaluated_count:0,insufficient_count:0,
    favorable_count:0,unfavorable_count:0,ambiguous_count:0,target_hit_count:0,stop_hit_count:0,favorable_rate_pct:null,
    average_return_pct:null,average_mfe_pct:null,average_mae_pct:null,conclusion_counts:{pending:1}}; }
  async function clickDue(id) { const target=el(id); await target.listeners.click({currentTarget:target,target}); }
'''
