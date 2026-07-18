from __future__ import annotations

from pathlib import Path
import subprocess


ROOT = Path(__file__).resolve().parents[1]


def test_advice_review_detail_rendering_escapes_content_and_shows_evaluation() -> None:
    _run_node(
        r'''
        const elements = new Map([["reviewPlanList", { innerHTML: "" }]]);
        globalThis.document = { getElementById(id) { return elements.get(id) || null; } };
        const { renderAdviceReviewDetails } = await import("./static/js/advice-reviews.js");

        renderAdviceReviewDetails([{
          plan: {
            id: 7, advice_id: 3, revision: 2, snapshot_market_time: "2026-07-16 10:00:00",
            snapshot_price: 100, target_price: 110, stop_price: 95, horizon_days: 20,
            hypothesis: "<script>趋势延续</script>", trigger_condition: "站稳100", invalidation_condition: "跌破95",
          },
          latest_evaluation: { status: "evaluated", conclusion: "target_hit", return_pct: 8.5, as_of: "2026-07-20 15:00:00" },
        }]);

        const html = elements.get("reviewPlanList").innerHTML;
        assert(!html.includes("<script>"), "review content was not escaped");
        assert(html.includes("目标价先触达") && html.includes("data-review-evaluate=\"7\""), "evaluation or action was not rendered");
        assert(html.includes("type=\"date\"") && html.includes("data-review-history=\"7\""), "historical evaluation controls were not rendered");
        assert(html.includes("data-review-delete=\"7\"") && html.includes("aria-label=\"删除复盘计划\""), "delete action was not rendered accessibly");
        function assert(condition, message) { if (!condition) throw new Error(message); }
        '''
    )


def test_advice_review_delete_confirms_cascading_history_and_invalidates_local_requests() -> None:
    _run_node(
        r'''
        const elements = new Map([
          ["reviewPlanList", { innerHTML: "" }],
          ["reviewPlanFeedback", { textContent: "", dataset: {}, hidden: true }],
          ["reviewAdviceId", { value: "3", innerHTML: "", disabled: true }],
          ["reviewPlanSubmit", { textContent: "", disabled: false }],
          ["reviewPlanCancel", { hidden: false }],
        ]);
        globalThis.document = { getElementById(id) { return elements.get(id) || null; } };
        const calls = [];
        globalThis.fetch = async (url, options = {}) => {
          calls.push({ url: String(url), options });
          return new Response(JSON.stringify({ ok: true, removed: true }), {
            status: 200,
            headers: { "Content-Type": "application/json" },
          });
        };
        const { deleteAdviceReviewPlan } = await import("./static/js/advice-reviews.js");
        const state = {
          symbol: "600519.SH",
          adviceReviewEditingPlanId: 7,
          adviceReviewDetails: [{ plan: plan(), latest_evaluation: { id: 11 } }],
          adviceReviewSnapshots: [],
          adviceReviewHistories: { "7": { phase: "loading", sequence: 2, items: [] } },
          adviceReviewAsOfByPlan: { "7": "2026-07-16" },
          adviceReviewEvaluationSeqByPlan: { "7": 4 },
        };
        let confirmations = 0;

        const cancelled = await deleteAdviceReviewPlan(state, 7, {
          symbol: state.symbol,
          isCurrent: () => true,
          confirm: () => false,
        });
        const removed = await deleteAdviceReviewPlan(state, 7, {
          symbol: state.symbol,
          isCurrent: () => true,
          confirm(message) {
            confirmations += 1;
            assert(message.includes("全部评估历史") && message.includes("不可撤销"), "confirmation did not describe the destructive scope");
            return true;
          },
        });

        assert(cancelled === false && calls.length === 1, "cancelled deletion still called the API");
        assert(calls[0].url === "/api/reviews/plans/7" && calls[0].options.method === "DELETE", "delete request contract changed");
        assert(removed === true && confirmations === 1, "confirmed deletion did not complete");
        assert(state.adviceReviewDetails.length === 0 && state.adviceReviewEditingPlanId === null, "deleted plan remained locally visible or editable");
        assert(!state.adviceReviewHistories["7"] && !state.adviceReviewAsOfByPlan["7"], "deleted plan retained transient history state");
        assert(state.adviceReviewEvaluationSeqByPlan["7"] === 5, "pending evaluation was not invalidated");
        assert(elements.get("reviewPlanList").innerHTML.includes("暂无复盘计划"), "empty plan state was not rendered");
        assert(elements.get("reviewPlanFeedback").textContent.includes("已删除"), "delete success was not confirmed");

        function plan() {
          return {
            id: 7, advice_id: 3, symbol: "600519.SH", revision: 2,
            snapshot_market_time: "2026-07-01 15:00:00", snapshot_price: 100,
            target_price: 110, stop_price: 95, horizon_days: 20,
            hypothesis: "趋势延续", trigger_condition: "站稳100", invalidation_condition: "跌破95",
          };
        }
        function assert(condition, message) { if (!condition) throw new Error(message); }
        '''
    )


def test_advice_review_create_uses_selected_snapshot_and_strict_price_order() -> None:
    _run_node(
        r'''
        const elements = formElements();
        globalThis.document = { getElementById(id) { return elements.get(id) || null; } };
        const calls = [];
        globalThis.fetch = async (url, options = {}) => {
          calls.push({ url: String(url), options });
          const payload = options.method === "POST" ? { id: 9 } : [];
          return new Response(JSON.stringify(payload), { status: 200, headers: { "Content-Type": "application/json" } });
        };
        const { submitAdviceReviewPlan } = await import("./static/js/advice-reviews.js");
        const state = {
          symbol: "600519.SH",
          adviceReviewSnapshots: [{ id: 3, price: 100, market_time: "2026-07-16 10:00:00" }],
          adviceReviewDetails: [],
        };

        await submitAdviceReviewPlan(state, { symbol: state.symbol, isCurrent: () => true });

        const write = calls[0];
        const body = JSON.parse(write.options.body);
        assert(write.url === "/api/reviews/plans" && write.options.method === "POST", "review plan used the wrong endpoint");
        assert(body.advice_id === 3 && body.symbol === "600519.SH", "review plan lost its snapshot binding");
        assert(body.target_price === 110 && body.stop_price === 95 && body.horizon_days === 20, "review plan values changed");

        function formElements() {
          const values = {
            reviewAdviceId: "3", reviewHypothesis: "趋势延续", reviewTrigger: "站稳100",
            reviewInvalidation: "跌破95", reviewTarget: "110", reviewStop: "95", reviewHorizon: "20",
          };
          const map = new Map(Object.entries(values).map(([id, value]) => [id, { value, disabled: false, focus() {} }]));
          map.set("reviewPlanList", { innerHTML: "" });
          map.set("reviewPlanFeedback", { textContent: "", dataset: {}, hidden: true });
          map.set("reviewPlanSubmit", { textContent: "", disabled: false });
          map.set("reviewPlanCancel", { hidden: true });
          return map;
        }
        function assert(condition, message) { if (!condition) throw new Error(message); }
        '''
    )


def test_advice_review_create_and_update_keep_displayed_owner_during_pending_switch() -> None:
    _run_node(
        r'''
        const elements = formElements();
        globalThis.document = { getElementById(id) { return elements.get(id) || null; } };
        const calls = [];
        let readCount = 0;
        globalThis.fetch = async (url, options = {}) => {
          const target = String(url);
          const method = options.method || "GET";
          calls.push({ target, method, body: options.body || "" });
          if (target === "/api/reviews/plans" && method === "POST") return jsonResponse(plan(1));
          if (target === "/api/reviews/plans/9" && method === "PATCH") return jsonResponse(plan(2));
          if (target === "/api/reviews?symbol=600519.SH&limit=20" && method === "GET") {
            readCount += 1;
            return jsonResponse([{ plan: plan(readCount), latest_evaluation: null }]);
          }
          throw new Error(`unexpected review request: ${method} ${target}`);
        };
        const { submitAdviceReviewPlan } = await import("./static/js/advice-reviews.js");
        const state = {
          symbol: "000001.SZ",
          loadSeq: 8,
          adviceReviewSnapshots: [{ id: 3, symbol: "600519.SH", price: 100, market_time: "2026-07-16 10:00:00" }],
          adviceReviewDetails: [],
        };
        const options = {
          symbol: "600519.SH",
          context: { symbol: "600519.SH", loadSeq: 8 },
          isCurrent: () => true,
        };

        await submitAdviceReviewPlan(state, options);
        state.adviceReviewEditingPlanId = 9;
        elements.get("reviewHypothesis").value = "趋势延续，等待确认";
        elements.get("reviewTarget").value = "112";
        await submitAdviceReviewPlan(state, options);

        const create = calls.find((call) => call.method === "POST");
        const update = calls.find((call) => call.method === "PATCH");
        const reads = calls.filter((call) => call.method === "GET");
        assert(JSON.parse(create.body).symbol === "600519.SH", "pending create used the requested target symbol");
        assert(update.target === "/api/reviews/plans/9", "pending update escaped the displayed plan");
        assert(!Object.hasOwn(JSON.parse(update.body), "symbol"), "update changed the existing PATCH API");
        assert(reads.length === 2 && reads.every((call) => call.target.includes("symbol=600519.SH")), "review readback used the pending target symbol");
        assert(state.symbol === "000001.SZ", "review mutation rewrote the pending target");
        assert(state.adviceReviewDetails[0].plan.symbol === "600519.SH" && state.adviceReviewDetails[0].plan.revision === 2, "displayed-owner readback was rejected");

        function plan(revision) {
          return {
            id: 9, advice_id: 3, symbol: "600519.SH", revision,
            snapshot_market_time: "2026-07-16 10:00:00", snapshot_price: 100,
            target_price: revision === 1 ? 110 : 112, stop_price: 95, horizon_days: 20,
            hypothesis: revision === 1 ? "趋势延续" : "趋势延续，等待确认",
            trigger_condition: "站稳100", invalidation_condition: "跌破95",
          };
        }
        function formElements() {
          const values = {
            reviewAdviceId: "3", reviewHypothesis: "趋势延续", reviewTrigger: "站稳100",
            reviewInvalidation: "跌破95", reviewTarget: "110", reviewStop: "95", reviewHorizon: "20",
          };
          const map = new Map(Object.entries(values).map(([id, value]) => [id, { value, disabled: false, focus() {} }]));
          map.set("reviewPlanList", { innerHTML: "" });
          map.set("reviewPlanFeedback", { textContent: "", dataset: {}, hidden: true });
          map.set("reviewPlanSubmit", { textContent: "", disabled: false });
          map.set("reviewPlanCancel", { hidden: true });
          return map;
        }
        function jsonResponse(value) {
          return new Response(JSON.stringify(value), { status: 200, headers: { "Content-Type": "application/json" } });
        }
        function assert(condition, message) { if (!condition) throw new Error(message); }
        '''
    )


def test_advice_review_details_refresh_defaults_after_excluding_planned_snapshot() -> None:
    _run_node(
        r'''
        const elements = new Map([
          ["reviewPlanList", { innerHTML: "" }],
          ["reviewAdviceId", { value: "1", innerHTML: "", disabled: false }],
          ["reviewHypothesis", { value: "旧快照" }],
          ["reviewTrigger", { value: "旧触发" }],
          ["reviewInvalidation", { value: "旧失效" }],
          ["reviewTarget", { value: "105" }],
          ["reviewStop", { value: "95" }],
          ["reviewPlanSubmit", { disabled: false }],
        ]);
        globalThis.document = { getElementById(id) { return elements.get(id) || null; } };
        globalThis.fetch = async () => new Response(JSON.stringify([{
          plan: {
            id: 8, advice_id: 1, revision: 1, snapshot_price: 100,
            target_price: 105, stop_price: 95, horizon_days: 10,
            hypothesis: "旧计划", trigger_condition: "旧触发", invalidation_condition: "旧失效",
          },
          latest_evaluation: null,
        }]), { status: 200, headers: { "Content-Type": "application/json" } });
        const { loadAdviceReviews } = await import("./static/js/advice-reviews.js");
        const state = {
          symbol: "600519.SH",
          adviceReviewSnapshots: [
            { id: 1, price: 100, resistance: 105, support: 95, market_time: "2026-07-15 10:00:00" },
            { id: 2, price: 200, resistance: 220, support: 180, market_time: "2026-07-16 10:00:00", summary: "新快照" },
          ],
          adviceReviewDetails: [],
        };

        const loaded = await loadAdviceReviews(state, { symbol: state.symbol, isCurrent: () => true });

        assert(loaded === true, "review details did not load");
        assert(elements.get("reviewAdviceId").value === "2", "planned snapshot was not excluded");
        assert(elements.get("reviewTarget").value === 220 && elements.get("reviewStop").value === 180, "price defaults stayed bound to the old snapshot");
        assert(elements.get("reviewHypothesis").value === "新快照", "text defaults stayed bound to the old snapshot");
        function assert(condition, message) { if (!condition) throw new Error(message); }
        '''
    )


def test_watchlist_scan_posts_only_checked_whitelisted_conditions_and_renders_results() -> None:
    _run_node(
        r'''
        const results = { innerHTML: "" };
        const form = {
          querySelectorAll() {
            return [
              { value: "close_above_ma20" },
              { value: "volume_surge_5d" },
              { value: "not_allowed" },
            ];
          },
        };
        globalThis.document = { getElementById(id) { return id === "watchlistScanForm" ? form : id === "watchlistScanResults" ? results : null; } };
        let request;
        globalThis.fetch = async (url, options = {}) => {
          request = { url: String(url), options };
          return new Response(JSON.stringify({
            universe: ["600519.SH"], as_of: "2026-07-16 15:00:00", rule_version: "watchlist-scan-v1",
            success: [{
              symbol: "600519.SH", data_date: "2026-07-16", matched: true,
              condition_results: { close_above_ma20: true, volume_surge_5d: true },
              matched_conditions: ["close_above_ma20", "volume_surge_5d"], metrics: { close: 100 },
            }], missing: [],
          }), { status: 200, headers: { "Content-Type": "application/json" } });
        };
        const { runWatchlistScan } = await import("./static/js/watchlist-scan.js");

        const completed = await runWatchlistScan({});

        const body = JSON.parse(request.options.body);
        assert(completed === true && request.url === "/api/watchlist/scan", "scan request did not complete");
        assert(JSON.stringify(body.conditions) === JSON.stringify(["close_above_ma20", "volume_surge_5d"]), "scan sent unchecked or unknown conditions");
        assert(results.innerHTML.includes("600519.SH") && results.innerHTML.includes("满足全部条件"), "scan result was not rendered");
        function assert(condition, message) { if (!condition) throw new Error(message); }
        '''
    )


def test_advice_review_evaluation_posts_optional_as_of_and_updates_loaded_history() -> None:
    _run_node(
        r'''
        const elements = new Map([
          ["reviewPlanList", { innerHTML: "" }],
          ["reviewPlanFeedback", { textContent: "", dataset: {}, hidden: true }],
          ["review-as-of-7", { value: "2026-07-16" }],
        ]);
        globalThis.document = { getElementById(id) { return elements.get(id) || null; } };
        const calls = [];
        globalThis.fetch = async (url, options = {}) => {
          calls.push({ url: String(url), options });
          const round = calls.length;
          return new Response(JSON.stringify({
            id: round, plan_id: 7, plan_revision: 2, status: "evaluated",
            conclusion: round === 1 ? "target_hit" : "horizon_gain",
            return_pct: round === 1 ? 8.5 : 2.1,
            as_of: round === 1 ? "2026-07-16 23:59:59" : "2026-07-17 15:00:00",
            evaluated_at: `2026-07-17 15:0${round}:00`, rule_version: "review-v1",
          }), { status: 200, headers: { "Content-Type": "application/json" } });
        };
        const { evaluateAdviceReviewPlan } = await import("./static/js/advice-reviews.js");
        const state = {
          symbol: "600519.SH",
          adviceReviewHistorySymbol: "600519.SH",
          adviceReviewHistoryEpoch: 1,
          adviceReviewEvaluationSeqByPlan: {},
          adviceReviewAsOfByPlan: {},
          adviceReviewHistories: { "7": { phase: "ready", expanded: true, items: [], sequence: 0 } },
          adviceReviewDetails: [{ plan: plan(), latest_evaluation: null }],
        };

        const first = await evaluateAdviceReviewPlan(state, 7, { symbol: state.symbol, isCurrent: () => true });
        elements.get("review-as-of-7").value = "";
        const second = await evaluateAdviceReviewPlan(state, 7, { symbol: state.symbol, isCurrent: () => true });

        assert(first === true && second === true, "evaluation did not complete");
        assert(JSON.stringify(JSON.parse(calls[0].options.body)) === JSON.stringify({ as_of: "2026-07-16T23:59:59" }), "historical as_of payload changed");
        assert(calls[1].options.body === "{}", "blank as_of did not use the current-time contract");
        assert(state.adviceReviewHistories["7"].items.length === 2, "loaded history did not receive new evaluations");
        assert(elements.get("reviewPlanList").innerHTML.includes("观察期收益为正"), "latest evaluation was not rendered");

        function plan() {
          return {
            id: 7, advice_id: 3, symbol: "600519.SH", revision: 2,
            snapshot_market_time: "2026-07-01 15:00:00", snapshot_price: 100,
            target_price: 110, stop_price: 95, horizon_days: 20,
            hypothesis: "趋势延续", trigger_condition: "站稳100", invalidation_condition: "跌破95",
          };
        }
        function assert(condition, message) { if (!condition) throw new Error(message); }
        '''
    )


def test_noon_today_as_of_uses_backend_current_time_for_review_and_scan() -> None:
    _run_node(
        r'''
        const elements = new Map([
          ["reviewPlanList", { innerHTML: "" }],
          ["reviewPlanFeedback", { textContent: "", dataset: {}, hidden: true }],
        ]);
        globalThis.document = { getElementById(id) { return elements.get(id) || null; } };
        let evaluationRequest;
        globalThis.fetch = async (url, options = {}) => {
          evaluationRequest = { url: String(url), options };
          return new Response(JSON.stringify({
            id: 1, plan_id: 7, plan_revision: 2, status: "pending", conclusion: "pending",
            return_pct: null, as_of: "2026-07-17 12:00:00", evaluated_at: "2026-07-17 12:00:00",
            rule_version: "review-v1",
          }), { status: 200, headers: { "Content-Type": "application/json" } });
        };
        const { evaluateAdviceReviewPlan } = await import("./static/js/advice-reviews.js");
        const { watchlistScanPayload } = await import("./static/js/watchlist-scan.js");
        const now = new Date("2026-07-17T04:00:00.000Z");
        const state = {
          symbol: "600519.SH",
          adviceReviewHistorySymbol: "600519.SH",
          adviceReviewEvaluationSeqByPlan: {}, adviceReviewAsOfByPlan: {}, adviceReviewHistories: {},
          adviceReviewDetails: [{ plan: {
            id: 7, advice_id: 3, symbol: "600519.SH", revision: 2,
            snapshot_market_time: "2026-07-01 15:00:00", snapshot_price: 100,
            target_price: 110, stop_price: 95, horizon_days: 20,
            hypothesis: "趋势延续", trigger_condition: "站稳100", invalidation_condition: "跌破95",
          }, latest_evaluation: null }],
        };

        assert(await evaluateAdviceReviewPlan(state, 7, {
          symbol: state.symbol, asOf: "2026-07-17", now, isCurrent: () => true,
        }) === true, "today review evaluation did not complete");
        assert(evaluationRequest.options.body === "{}", "today review sent a future end-of-day timestamp");

        const asOf = { value: "2026-07-17" };
        const form = {
          querySelector(selector) {
            if (selector === 'input[name="scanUniverse"]:checked') return { value: "watchlist" };
            if (selector === "#watchlistScanAsOf") return asOf;
            return null;
          },
          querySelectorAll() { return [{ value: "close_above_ma20" }]; },
        };
        const todayPayload = watchlistScanPayload(form, { now });
        assert(!Object.hasOwn(todayPayload, "as_of"), "today scan sent a future end-of-day timestamp");
        asOf.value = "2026-07-16";
        assert(watchlistScanPayload(form, { now }).as_of === "2026-07-16T23:59:59", "past scan lost Shanghai end-of-day");
        function assert(condition, message) { if (!condition) throw new Error(message); }
        '''
    )


def test_advice_review_history_is_lazy_collapsible_and_renders_full_audit_fields() -> None:
    _run_node(
        r'''
        const target = { innerHTML: "" };
        globalThis.document = { getElementById(id) { return id === "reviewPlanList" ? target : null; } };
        let resolveRequest;
        let callCount = 0;
        globalThis.fetch = async (url) => {
          callCount += 1;
          assert(String(url) === "/api/reviews/plans/7/evaluations?limit=100", "history endpoint changed");
          return await new Promise((resolve) => { resolveRequest = resolve; });
        };
        const { toggleAdviceReviewHistory } = await import("./static/js/advice-reviews.js");
        const state = reviewState();

        const loading = toggleAdviceReviewHistory(state, 7, { symbol: "600519.SH", isCurrent: () => true });
        assert(target.innerHTML.includes("评估历史加载中") && target.innerHTML.includes('aria-expanded="true"'), "history loading state was not visible");
        resolveRequest(new Response(JSON.stringify([{
          id: 4, plan_id: 7, plan_revision: 2, status: "evaluated", conclusion: "target_hit",
          return_pct: 8.5, as_of: "2026-07-16 23:59:59", evaluated_at: "2026-07-17 09:00:00",
          rule_version: "review-v1",
        }]), { status: 200, headers: { "Content-Type": "application/json" } }));
        assert(await loading === true, "history did not finish loading");
        assert(target.innerHTML.includes("计划版本") && target.innerHTML.includes(">2</strong>"), "plan revision was not rendered");
        assert(target.innerHTML.includes("2026-07-16 23:59:59") && target.innerHTML.includes("目标价先触达"), "history date or conclusion was missing");
        assert(target.innerHTML.includes("8.50%") && target.innerHTML.includes("已评估"), "history return or status was missing");

        assert(await toggleAdviceReviewHistory(state, 7, { symbol: "600519.SH" }) === true, "history did not collapse");
        assert(target.innerHTML.includes('aria-expanded="false"') && target.innerHTML.includes(" hidden>"), "collapsed history stayed exposed");
        assert(await toggleAdviceReviewHistory(state, 7, { symbol: "600519.SH" }) === true, "cached history did not reopen");
        assert(callCount === 1, "reopening loaded history issued another request");

        function reviewState() {
          return {
            symbol: "600519", adviceReviewHistorySymbol: "600519.SH", adviceReviewHistoryEpoch: 1,
            adviceReviewHistories: {}, adviceReviewAsOfByPlan: {}, adviceReviewEvaluationSeqByPlan: {},
            adviceReviewDetails: [{ plan: {
              id: 7, symbol: "600519.SH", revision: 2, snapshot_market_time: "2026-07-01 15:00:00",
              snapshot_price: 100, target_price: 110, stop_price: 95, horizon_days: 20,
              hypothesis: "趋势延续", trigger_condition: "站稳100", invalidation_condition: "跌破95",
            }, latest_evaluation: null }],
          };
        }
        function assert(condition, message) { if (!condition) throw new Error(message); }
        '''
    )


def test_advice_review_history_handles_error_retry_empty_and_stale_stock_response() -> None:
    _run_node(
        r'''
        const target = { innerHTML: "" };
        globalThis.document = { getElementById(id) { return id === "reviewPlanList" ? target : null; } };
        let round = 0;
        let resolveStale;
        globalThis.fetch = async () => {
          round += 1;
          if (round === 1) return new Response(JSON.stringify({ detail: "历史服务暂不可用" }), { status: 503, headers: { "Content-Type": "application/json" } });
          if (round === 2) return new Response("[]", { status: 200, headers: { "Content-Type": "application/json" } });
          return await new Promise((resolve) => { resolveStale = resolve; });
        };
        const { retryAdviceReviewHistory, toggleAdviceReviewHistory } = await import("./static/js/advice-reviews.js");
        const state = reviewState();

        assert(await toggleAdviceReviewHistory(state, 7, { symbol: state.symbol, isCurrent: () => true }) === false, "history failure was reported as success");
        assert(target.innerHTML.includes("评估历史加载失败") && target.innerHTML.includes("data-review-history-retry"), "history error and retry state was missing");
        assert(await retryAdviceReviewHistory(state, 7, { symbol: state.symbol, isCurrent: () => true }) === true, "history retry failed");
        assert(target.innerHTML.includes("暂无评估历史"), "empty history state was missing");

        state.adviceReviewHistories["7"].phase = "idle";
        state.adviceReviewHistories["7"].expanded = false;
        const stale = toggleAdviceReviewHistory(state, 7, { symbol: state.symbol, isCurrent: () => state.symbol === "600519.SH" });
        state.symbol = "000001.SZ";
        target.innerHTML = "CURRENT_STOCK";
        resolveStale(new Response(JSON.stringify([{
          id: 99, plan_revision: 2, status: "evaluated", conclusion: "stop_hit",
          return_pct: -5, as_of: "2026-07-16 15:00:00", evaluated_at: "2026-07-16 15:01:00",
        }]), { status: 200, headers: { "Content-Type": "application/json" } }));
        assert(await stale === false, "stale stock history was accepted");
        assert(target.innerHTML === "CURRENT_STOCK", "stale stock history rewrote the current view");

        function reviewState() {
          return {
            symbol: "600519.SH", adviceReviewHistorySymbol: "600519.SH", adviceReviewHistoryEpoch: 1,
            adviceReviewHistories: {}, adviceReviewAsOfByPlan: {}, adviceReviewEvaluationSeqByPlan: {},
            adviceReviewDetails: [{ plan: {
              id: 7, symbol: "600519.SH", revision: 2, snapshot_market_time: "2026-07-01 15:00:00",
              snapshot_price: 100, target_price: 110, stop_price: 95, horizon_days: 20,
              hypothesis: "趋势延续", trigger_condition: "站稳100", invalidation_condition: "跌破95",
            }, latest_evaluation: null }],
          };
        }
        function assert(condition, message) { if (!condition) throw new Error(message); }
        '''
    )


def test_watchlist_scan_posts_custom_symbols_and_historical_as_of_exactly() -> None:
    _run_node(
        r'''
        const results = { innerHTML: "", setAttribute() {} };
        const feedback = { textContent: "", dataset: {}, hidden: true };
        const symbols = { value: "600519, SZ000001\n600519；300750.SZ", disabled: false };
        const asOf = { value: "2026-07-16" };
        const customField = { hidden: false };
        const form = {
          querySelector(selector) {
            if (selector === 'input[name="scanUniverse"]:checked') return { value: "symbols" };
            if (selector === "#watchlistScanSymbols") return symbols;
            if (selector === "#watchlistScanAsOf") return asOf;
            if (selector === "#watchlistScanCustomField") return customField;
            return null;
          },
          querySelectorAll() { return [{ value: "close_above_ma20" }, { value: "volume_surge_5d" }]; },
        };
        const elements = new Map([
          ["watchlistScanForm", form], ["watchlistScanResults", results],
          ["watchlistScanFeedback", feedback], ["watchlistScanSymbols", symbols],
          ["watchlistScanAsOf", asOf], ["watchlistScanCustomField", customField],
        ]);
        globalThis.document = { getElementById(id) { return elements.get(id) || null; } };
        let request;
        globalThis.fetch = async (url, options = {}) => {
          request = { url: String(url), options };
          return new Response(JSON.stringify({
            universe: ["600519.SH", "000001.SZ", "300750.SZ"], success: [], missing: [],
            as_of: "2026-07-16 23:59:59", rule_version: "watchlist-scan-v1", conditions: [],
          }), { status: 200, headers: { "Content-Type": "application/json" } });
        };
        const { runWatchlistScan } = await import("./static/js/watchlist-scan.js");

        assert(await runWatchlistScan({}) === true, "custom scan did not complete");
        const body = JSON.parse(request.options.body);
        const expected = {
          universe: "symbols",
          conditions: ["close_above_ma20", "volume_surge_5d"],
          symbols: ["600519.SH", "000001.SZ", "300750.SZ"],
          as_of: "2026-07-16T23:59:59",
        };
        assert(JSON.stringify(body) === JSON.stringify(expected), `custom scan payload changed: ${JSON.stringify(body)}`);
        assert(!Object.hasOwn(body, "rule_version"), "frontend injected an unsupported scan field");
        function assert(condition, message) { if (!condition) throw new Error(message); }
        '''
    )


def test_watchlist_scan_rejects_empty_invalid_oversized_and_future_custom_input() -> None:
    _run_node(
        r'''
        const values = { symbols: "", asOf: "" };
        const form = {
          querySelector(selector) {
            if (selector === 'input[name="scanUniverse"]:checked') return { value: "symbols" };
            if (selector === "#watchlistScanSymbols") return { value: values.symbols };
            if (selector === "#watchlistScanAsOf") return { value: values.asOf };
            return null;
          },
          querySelectorAll() { return [{ value: "close_above_ma20" }]; },
        };
        globalThis.document = { getElementById(id) { return id === "watchlistScanForm" ? form : null; } };
        const { watchlistScanPayload } = await import("./static/js/watchlist-scan.js");

        expectError(() => watchlistScanPayload(form), "至少一个");
        values.symbols = "贵州茅台";
        expectError(() => watchlistScanPayload(form), "股票代码 贵州茅台 无效");
        values.symbols = Array.from({ length: 51 }, (_, index) => String(600001 + index)).join(",");
        expectError(() => watchlistScanPayload(form), "最多扫描 50");
        values.symbols = "600519";
        values.asOf = "2999-01-01";
        expectError(() => watchlistScanPayload(form), "不能晚于今天");

        function expectError(task, expected) {
          let message = "";
          try { task(); } catch (error) { message = error.message; }
          if (!message.includes(expected)) throw new Error(`expected ${expected}, received ${message}`);
        }
        '''
    )


def test_review_and_scan_controls_are_accessible_in_static_markup() -> None:
    html = (ROOT / "static" / "index.html").read_text(encoding="utf-8")

    assert 'name="scanUniverse"' in html
    assert 'id="watchlistScanSymbols"' in html
    assert 'id="watchlistScanAsOf"' in html
    assert 'id="watchlistScanFeedback" role="status" aria-live="polite"' in html
    assert 'id="watchlistScanResults" class="watchlist-scan-results" role="status"' in html


def _run_node(script: str) -> None:
    subprocess.run(
        ["node", "--input-type=module", "--eval", script],
        cwd=ROOT,
        check=True,
        capture_output=True,
        text=True,
    )
