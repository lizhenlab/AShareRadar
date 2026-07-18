from __future__ import annotations

import subprocess
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]


def test_latest_notes_read_aborts_previous_request_and_wins() -> None:
    script = r'''
      import { loadNotes } from "./static/js/notes.js";

      const dom = installNotesAlertsDom();
      const state = { symbol: "600519.SH" };
      const firstReply = deferredReply();
      const calls = [];
      const delays = captureRequestTimeouts();
      globalThis.fetch = async (url, options = {}) => {
        calls.push({ url: String(url), options });
        if (calls.length === 1) return firstReply.promise;
        return jsonResponse([note("新股票笔记")]);
      };

      const staleLoad = loadNotes(state);
      await Promise.resolve();
      const currentLoad = loadNotes(state);
      const [staleResult, currentResult] = await Promise.all([staleLoad, currentLoad]);

      assert(staleResult === false && currentResult === true, "notes read results did not identify the current request");
      assert(calls.length === 2, `expected two notes reads, got ${calls.length}`);
      assert(calls[0].options.signal.aborted, "new notes read did not abort the previous fetch signal");
      assert(calls.every((call) => call.options.signal), "notes reads did not receive a signal");
      assert(delays.length === 2 && delays.every((delay) => delay === 12000), `notes read timeout changed: ${delays}`);
      assert(dom.element("noteList").innerHTML.includes("新股票笔记"), "stale notes response won the render race");
    '''
    _run_node_script(script)


def test_note_and_alert_writes_survive_stale_load_abort_without_polluting_new_stock() -> None:
    script = r'''
      import { addAlertRule } from "./static/js/alerts.js";
      import { addStockNote } from "./static/js/notes.js";

      const dom = installNotesAlertsDom();
      const state = { symbol: "600519.SH", lastAnalysis: null };
      const parent = new AbortController();
      const noteReply = deferredReply();
      const alertReply = deferredReply();
      const calls = [];
      let committedWrites = 0;
      let markRefreshes = 0;
      globalThis.fetch = async (url, options = {}) => {
        const call = { url: String(url), options };
        calls.push(call);
        if (call.url === "/api/stock/notes" && options.method === "POST") {
          const response = await noteReply.promise;
          committedWrites += 1;
          return response;
        }
        if (call.url === "/api/alerts" && options.method === "POST") {
          const response = await alertReply.promise;
          committedWrites += 1;
          return response;
        }
        throw new Error(`unexpected request: ${call.url}`);
      };
      const options = {
        symbol: "600519.SH",
        signal: parent.signal,
        isCurrent: () => state.symbol === "600519.SH" && !parent.signal.aborted,
      };
      dom.element("noteContent").value = "旧股票笔记";
      dom.element("alertThreshold").value = "12.5";

      const noteWrite = addStockNote(state, async () => { markRefreshes += 1; }, options);
      const alertWrite = addAlertRule(state, options);
      await Promise.resolve();
      const writes = calls.filter((call) => call.options.method === "POST");
      assert(writes.length === 2, `expected two persistence writes, got ${writes.length}`);

      state.symbol = "000001.SZ";
      dom.element("noteList").innerHTML = "新股票笔记面板";
      dom.element("alertList").innerHTML = "新股票预警面板";
      dom.element("noteContent").value = "新股票笔记草稿";
      dom.element("alertThreshold").value = "23.5";
      parent.abort();
      assert(writes.every((call) => !call.options.signal.aborted), "stock switch aborted a persistence write");

      noteReply.resolve(jsonResponse({}));
      alertReply.resolve(jsonResponse({}));
      const [noteResult, alertResult] = await Promise.all([noteWrite, alertWrite]);

      assert(noteResult === false && alertResult === false, "stale writes ran an old UI tail");
      assert(committedWrites === 2, `not all writes finished after the old load abort: ${committedWrites}`);
      assert(calls.length === 2 && writes.every((call) => !call.options.signal.aborted), "stale writes were aborted or started stale readbacks");
      assert(markRefreshes === 0, "stale note write refreshed chart marks for the new stock");
      assert(dom.element("noteList").innerHTML === "新股票笔记面板", "old note write polluted the new stock panel");
      assert(dom.element("alertList").innerHTML === "新股票预警面板", "old alert write polluted the new stock panel");
      assert(dom.element("noteContent").value === "新股票笔记草稿" && dom.element("alertThreshold").value === "23.5", "old writes cleared new stock form values");
    '''
    _run_node_script(script)


def test_stock_abort_cancels_note_readback_but_not_the_completed_write() -> None:
    script = r'''
      import { addStockNote } from "./static/js/notes.js";

      const dom = installNotesAlertsDom();
      const state = { symbol: "600519.SH", lastAnalysis: null };
      const parent = new AbortController();
      const readReply = deferredReply();
      const calls = [];
      let markRefreshes = 0;
      globalThis.fetch = async (url, options = {}) => {
        const call = { url: String(url), options };
        calls.push(call);
        if (call.url === "/api/stock/notes" && options.method === "POST") return jsonResponse({});
        if (call.url.startsWith("/api/stock/notes?")) return readReply.promise;
        throw new Error(`unexpected request: ${call.url}`);
      };
      dom.element("noteContent").value = "等待回读";

      const addition = addStockNote(state, async () => { markRefreshes += 1; }, {
        symbol: "600519.SH",
        signal: parent.signal,
        isCurrent: () => state.symbol === "600519.SH" && !parent.signal.aborted,
      });
      for (let index = 0; index < 12 && calls.length < 2; index += 1) await Promise.resolve();
      assert(calls.length === 2, `note readback did not start after the write: ${calls.length}`);
      const [write, readback] = calls;

      state.symbol = "000001.SZ";
      dom.element("noteList").innerHTML = "新股票笔记面板";
      parent.abort();
      const result = await addition;

      assert(result === false, "cancelled note readback was reported as current");
      assert(!write.options.signal.aborted, "stock abort reached the already completed note write");
      assert(readback.options.signal.aborted, "stock abort did not cancel the note readback");
      assert(markRefreshes === 0, "cancelled readback continued into chart mark refresh");
      assert(dom.element("noteList").innerHTML === "新股票笔记面板", "cancelled readback polluted the new stock panel");
    '''
    _run_node_script(script)


def test_independent_note_mutations_do_not_abort_each_other() -> None:
    script = r'''
      import { removeStockNote, updateStockNote } from "./static/js/notes.js";

      const dom = installNotesAlertsDom();
      const state = { symbol: "600519.SH", lastAnalysis: null };
      const updateReply = deferredReply();
      const deleteReply = deferredReply();
      const calls = [];
      let markRefreshes = 0;
      globalThis.fetch = async (url, options = {}) => {
        const call = { url: String(url), options };
        calls.push(call);
        if (options.method === "PATCH") return updateReply.promise;
        if (options.method === "DELETE") return deleteReply.promise;
        if (call.url.startsWith("/api/stock/notes?")) return jsonResponse([note("并发写已同步")]);
        throw new Error(`unexpected request: ${call.url}`);
      };
      const refreshChartMarks = async () => { markRefreshes += 1; };

      const update = updateStockNote(state, "note-a", { visible: false }, refreshChartMarks);
      const removal = removeStockNote(state, "note-b", refreshChartMarks);
      await Promise.resolve();
      const writes = calls.filter((call) => call.options.method === "PATCH" || call.options.method === "DELETE");
      assert(writes.length === 2, `expected two independent writes, got ${writes.length}`);
      assert(writes.every((call) => !call.options.signal.aborted), "one note write aborted another independent write");

      deleteReply.resolve(jsonResponse({}));
      const removeResult = await removal;
      assert(removeResult === true && !writes[0].options.signal.aborted, "completed delete cancelled the pending note update");
      updateReply.resolve(jsonResponse({}));
      const updateResult = await update;

      assert(updateResult === true, "independent note update was silently lost");
      assert(writes.every((call) => !call.options.signal.aborted), "a note write signal was aborted after completion");
      assert(markRefreshes === 2, `completed note writes did not refresh chart marks: ${markRefreshes}`);
      assert(dom.element("noteList").innerHTML.includes("并发写已同步"), "completed note writes did not refresh the note list");
    '''
    _run_node_script(script)


def test_new_note_mutation_only_aborts_old_chart_mark_refresh_tail() -> None:
    script = r'''
      import { addStockNote } from "./static/js/notes.js";

      const dom = installNotesAlertsDom();
      const state = { symbol: "600519.SH", lastAnalysis: null };
      const firstMarksStarted = deferredReply();
      const markContexts = [];
      let noteReadCount = 0;
      globalThis.fetch = async (url, options = {}) => {
        if (options.method === "POST") return jsonResponse({});
        noteReadCount += 1;
        return jsonResponse([note(noteReadCount === 1 ? "第一条" : "第二条")]);
      };
      const refreshChartMarks = async (context) => {
        markContexts.push(context);
        if (markContexts.length !== 1) return;
        firstMarksStarted.resolve();
        await new Promise((resolve, reject) => {
          const onAbort = () => {
            const error = new Error("cancelled");
            error.name = "AbortError";
            reject(error);
          };
          if (context.signal.aborted) onAbort();
          else context.signal.addEventListener("abort", onAbort, { once: true });
        });
      };

      dom.element("noteContent").value = "第一条";
      const firstMutation = addStockNote(state, refreshChartMarks, { context: { symbol: state.symbol, loadSeq: 4 } });
      await firstMarksStarted.promise;
      dom.element("noteContent").value = "第二条";
      const secondMutation = addStockNote(state, refreshChartMarks, { context: { symbol: state.symbol, loadSeq: 4 } });
      const [firstResult, secondResult] = await Promise.all([firstMutation, secondMutation]);

      assert(firstResult === true && secondResult === true, "a completed note write was reported as lost when its refresh tail was superseded");
      assert(markContexts.length === 2 && markContexts[0].signal.aborted && !markContexts[1].signal.aborted, "chart mark refresh did not receive operation-scoped signals");
      assert(dom.element("noteList").innerHTML.includes("第二条"), "old chart refresh tail disrupted the current note render");
    '''
    _run_node_script(script)


def test_latest_alerts_read_aborts_both_previous_requests() -> None:
    script = r'''
      import { loadAlerts } from "./static/js/alerts.js";

      const dom = installNotesAlertsDom();
      const state = { symbol: "600519.SH" };
      const firstReply = deferredReply();
      const calls = [];
      const delays = captureRequestTimeouts();
      globalThis.fetch = async (url, options = {}) => {
        const call = { url: String(url), options };
        calls.push(call);
        if (calls.length <= 2) return firstReply.promise;
        if (call.url.startsWith("/api/alerts/events")) return jsonResponse([alertEvent("新触发事件")]);
        return jsonResponse([alertRule("新预警规则")]);
      };

      const staleLoad = loadAlerts(state);
      await Promise.resolve();
      const currentLoad = loadAlerts(state);
      const [staleResult, currentResult] = await Promise.all([staleLoad, currentLoad]);

      assert(staleResult === false && currentResult === true, "alerts read results did not identify the current request");
      assert(calls.length === 4, `expected four alert reads, got ${calls.length}`);
      assert(calls.slice(0, 2).every((call) => call.options.signal.aborted), "new alerts read did not abort both previous fetches");
      assert(delays.length === 4 && delays.every((delay) => delay === 12000), `alerts read timeout changed: ${delays}`);
      assert(dom.element("alertList").innerHTML.includes("新预警规则"), "stale alert rules overwrote the latest rules");
      assert(dom.element("alertEvents").innerHTML.includes("新触发事件"), "stale alert events overwrote the latest events");
    '''
    _run_node_script(script)


def test_independent_alert_mutations_do_not_abort_each_other() -> None:
    script = r'''
      import { removeAlertRule, updateAlertRule } from "./static/js/alerts.js";

      const dom = installNotesAlertsDom();
      const state = { symbol: "600519.SH" };
      const updateReply = deferredReply();
      const deleteReply = deferredReply();
      const calls = [];
      globalThis.fetch = async (url, options = {}) => {
        const call = { url: String(url), options };
        calls.push(call);
        if (options.method === "PATCH") return updateReply.promise;
        if (options.method === "DELETE") return deleteReply.promise;
        if (call.url.startsWith("/api/alerts/events")) return jsonResponse([alertEvent("并发事件已同步")]);
        if (call.url.startsWith("/api/alerts")) return jsonResponse([alertRule("并发规则已同步")]);
        throw new Error(`unexpected request: ${call.url}`);
      };

      const update = updateAlertRule(state, "rule-a", { enabled: false });
      const removal = removeAlertRule(state, "rule-b");
      await Promise.resolve();
      const writes = calls.filter((call) => call.options.method === "PATCH" || call.options.method === "DELETE");
      assert(writes.length === 2, `expected two alert writes, got ${writes.length}`);
      assert(writes.every((call) => !call.options.signal.aborted), "one alert write aborted another independent write");

      deleteReply.resolve(jsonResponse({}));
      const removeResult = await removal;
      assert(removeResult === true && !writes[0].options.signal.aborted, "completed alert delete cancelled the pending update");
      updateReply.resolve(jsonResponse({}));
      const updateResult = await update;

      assert(updateResult === true, "independent alert update was silently lost");
      assert(writes.every((call) => !call.options.signal.aborted), "an alert write signal was aborted after completion");
      assert(dom.element("alertList").innerHTML.includes("并发规则已同步"), "alert rules were not refreshed after both writes");
      assert(dom.element("alertEvents").innerHTML.includes("并发事件已同步"), "alert events were not refreshed after both writes");
    '''
    _run_node_script(script)


def test_alert_toggle_commit_is_reconciled_when_rules_readback_fails() -> None:
    script = r'''
      import { renderAlerts, updateAlertRule } from "./static/js/alerts.js";

      const dom = installNotesAlertsDom();
      const state = { symbol: "600519.SH" };
      const rule = alertRule("价格突破", { id: "rule-toggle" });
      renderAlerts([rule]);
      globalThis.fetch = async (url, options = {}) => {
        const target = String(url);
        if (options.method === "PATCH") return jsonResponse({ ...rule, enabled: false, updated_at: "2026-07-15 10:00:00" });
        if (target.startsWith("/api/alerts/events")) return jsonResponse([alertEvent("暂停后事件已同步")]);
        if (target.startsWith("/api/alerts?")) return errorResponse(503, "规则列表回读暂不可用");
        throw new Error(`unexpected request: ${target}`);
      };

      const result = await updateAlertRule(state, "rule-toggle", { enabled: false });
      const html = dom.element("alertList").innerHTML;

      assert(result === true, "accepted alert toggle was reported as an uncommitted write");
      assert(html.includes('data-alert-toggle="rule-toggle" data-alert-enabled="true">启用</button>'), "accepted toggle left the stale pause action rendered");
      assert(html.includes("预警已暂停，列表同步降级") && html.includes("规则列表回读暂不可用"), "toggle readback degradation was not explicit");
      assert(dom.element("alertEvents").innerHTML.includes("暂停后事件已同步"), "successful event readback was discarded with the failed rules readback");
      assert(state.researchActivityAlertSource.phase === "ready", "successful event readback was marked unavailable");
    '''
    _run_node_script(script)


def test_alert_delete_commit_is_reconciled_when_rules_readback_fails() -> None:
    script = r'''
      import { removeAlertRule, renderAlerts } from "./static/js/alerts.js";

      const dom = installNotesAlertsDom();
      const state = { symbol: "600519.SH" };
      const removed = alertRule("待删除预警", { id: "rule-delete" });
      const retained = alertRule("保留预警", { id: "rule-keep" });
      renderAlerts([removed, retained]);
      globalThis.fetch = async (url, options = {}) => {
        const target = String(url);
        if (options.method === "DELETE") return jsonResponse({ ok: true, removed: true });
        if (target.startsWith("/api/alerts/events")) return jsonResponse([alertEvent("删除后事件已同步")]);
        if (target.startsWith("/api/alerts?")) return errorResponse(503, "删除后规则回读失败");
        throw new Error(`unexpected request: ${target}`);
      };

      const result = await removeAlertRule(state, "rule-delete");
      const html = dom.element("alertList").innerHTML;

      assert(result === true, "accepted alert delete was reported as an uncommitted write");
      assert(!html.includes("待删除预警") && !html.includes('data-alert-remove="rule-delete"'), "deleted alert remained actionable after failed readback");
      assert(html.includes("保留预警"), "local delete reconciliation removed an unrelated alert");
      assert(html.includes("预警已删除，列表同步降级") && html.includes("删除后规则回读失败"), "delete readback degradation was not explicit");
      assert(dom.element("alertEvents").innerHTML.includes("删除后事件已同步"), "successful events did not render after delete readback degradation");
      assert(state.researchActivityAlertSource.phase === "ready", "delete event readback was marked unavailable");
    '''
    _run_node_script(script)


def test_alert_evaluation_survives_stock_abort_and_blocks_duplicate_operation() -> None:
    script = r'''
      import { evaluateAlerts } from "./static/js/alerts.js";

      const dom = installNotesAlertsDom();
      const state = { symbol: "600519.SH" };
      const oldParent = new AbortController();
      const oldReply = deferredReply();
      const calls = [];
      let evaluationCalls = 0;
      globalThis.fetch = async (url, options = {}) => {
        const call = { url: String(url), options };
        calls.push(call);
        if (call.url.startsWith("/api/alerts/evaluate")) {
          evaluationCalls += 1;
          if (evaluationCalls === 1) return oldReply.promise;
          return jsonResponse({
            checked_at: "2026-07-15 10:02:00",
            checked_count: 2,
            triggered_count: 1,
            new_event_count: 1,
            failed_count: 0,
          });
        }
        if (call.url.startsWith("/api/alerts/events")) return jsonResponse([alertEvent("新股票触发事件")]);
        if (call.url.startsWith("/api/alerts")) return jsonResponse([alertRule("新股票预警规则")]);
        throw new Error(`unexpected request: ${call.url}`);
      };
      dom.element("alertEvents").innerHTML = "新股票事件面板";

      const evaluation = evaluateAlerts(state, {
        symbol: "600519.SH",
        signal: oldParent.signal,
        isCurrent: () => state.symbol === "600519.SH",
      });
      await Promise.resolve();
      assert(dom.element("evaluateAlerts").disabled, "alert evaluation did not enter busy state");
      const duplicateResult = await evaluateAlerts(state, {
        symbol: "600519.SH",
        signal: oldParent.signal,
        isCurrent: () => state.symbol === "600519.SH",
      });
      assert(duplicateResult === false && calls.length === 1, "pending alert evaluation did not block a duplicate operation");

      state.symbol = "000001.SZ";
      oldParent.abort();
      assert(!calls[0].options.signal.aborted, "stock context abort reached the alert evaluation write");
      assert(!dom.element("evaluateAlerts").disabled, "stock switch did not release the shared evaluation control");
      assert(dom.element("alertEvaluation").hidden && dom.element("alertEvaluation").innerHTML === "", "old stock evaluation status survived its context");

      const newParent = new AbortController();
      const currentEvaluation = evaluateAlerts(state, {
        symbol: "000001.SZ",
        signal: newParent.signal,
        isCurrent: () => state.symbol === "000001.SZ",
      });
      oldReply.resolve(jsonResponse({
        checked_at: "2026-07-15 10:01:00",
        checked_count: 1,
        triggered_count: 0,
        new_event_count: 0,
        failed_count: 1,
      }));
      const [result, currentResult] = await Promise.all([evaluation, currentEvaluation]);

      assert(result === false, "stale alert evaluation applied its old UI tail");
      assert(currentResult === true, "new stock evaluation did not complete while the old write drained");
      assert(calls.length === 4 && !calls[0].options.signal.aborted, "alert evaluation did not finish independently from the old load");
      assert(dom.element("alertEvaluation").innerHTML.includes("2026-07-15 10:02:00"), "stale round replaced the new stock evaluation status");
      assert(!dom.element("alertEvaluation").innerHTML.includes("10:01:00"), "old stock evaluation leaked into the new stock status");
      assert(dom.element("alertEvents").innerHTML.includes("新股票触发事件"), "new stock event readback was not rendered");
      assert(!dom.element("evaluateAlerts").disabled && dom.element("evaluateAlerts").textContent === "检查", "detached evaluation left the shared button busy");
    '''
    _run_node_script(script)


def test_alert_evaluation_summary_survives_readback_failure_and_next_round_wins() -> None:
    script = r'''
      import { evaluateAlerts } from "./static/js/alerts.js";

      const dom = installNotesAlertsDom();
      const state = { symbol: "600519.SH" };
      let evaluationRound = 0;
      let eventReadRound = 0;
      globalThis.fetch = async (url) => {
        const target = String(url);
        if (target.startsWith("/api/alerts/evaluate")) {
          evaluationRound += 1;
          return jsonResponse(evaluationRound === 1
            ? {
                checked_at: "2026-07-15 10:00:00",
                checked_count: 3,
                triggered_count: 1,
                new_event_count: 1,
                failed_count: 1,
              }
            : {
                checked_at: "2026-07-15 10:05:00",
                checked_count: 3,
                triggered_count: 0,
                new_event_count: 0,
                failed_count: 0,
              });
        }
        if (target.startsWith("/api/alerts/events")) {
          eventReadRound += 1;
          if (eventReadRound === 1) {
            return {
              ok: false,
              status: 503,
              async json() { return { detail: "事件回读暂不可用" }; },
            };
          }
          return jsonResponse([alertEvent("第二轮事件已同步")]);
        }
        if (target.startsWith("/api/alerts")) return jsonResponse([alertRule("持续检查规则")]);
        throw new Error(`unexpected request: ${target}`);
      };

      const firstResult = await evaluateAlerts(state);
      const firstSummary = dom.element("alertEvaluation").innerHTML;
      assert(firstResult === true, "partial evaluation was reported as failed");
      assert(firstSummary.includes("检查部分完成") && firstSummary.includes("成功 2 / 3") && firstSummary.includes("失败 1 条"), "partial summary did not survive evaluateAlerts completion");
      assert(dom.element("alertEvents").innerHTML.includes("事件读取失败") && dom.element("alertEvents").innerHTML.includes("事件回读暂不可用"), "event readback failure lost its own panel state");
      assert(state.alertEvaluationViewOwner.symbol === "600519.SH" && state.alertEvaluationViewOwner.round === 1, "first evaluation ownership was not persisted");

      const secondResult = await evaluateAlerts(state);
      const secondSummary = dom.element("alertEvaluation").innerHTML;
      assert(secondResult === true, "second evaluation did not complete");
      assert(secondSummary.includes("检查完成") && secondSummary.includes("2026-07-15 10:05:00"), "second round did not own the evaluation status");
      assert(!secondSummary.includes("检查部分完成") && !secondSummary.includes("失败 1 条"), "first round summary survived the next round");
      assert(dom.element("alertEvents").innerHTML.includes("第二轮事件已同步"), "second event readback did not recover");
      assert(state.alertEvaluationViewOwner.round === 2 && dom.element("alertEvaluation").dataset.round === "2", "evaluation DOM round ownership did not advance");
      assert(!dom.element("evaluateAlerts").disabled && dom.element("evaluateAlerts").textContent === "检查", "evaluation control did not recover after consecutive checks");
    '''
    _run_node_script(script)


def test_note_and_alert_write_timeouts_use_twelve_seconds_without_refresh_tails() -> None:
    script = r'''
      import { addAlertRule } from "./static/js/alerts.js";
      import { addStockNote } from "./static/js/notes.js";

      const dom = installNotesAlertsDom();
      const state = { symbol: "600519.SH", lastAnalysis: null };
      const delays = expireRequestsImmediately();
      const calls = [];
      let markRefreshes = 0;
      globalThis.fetch = async (url, options = {}) => {
        calls.push({ url: String(url), options });
        return new Promise(() => {});
      };

      dom.element("noteContent").value = "超时笔记";
      const noteMessage = await rejectedMessage(addStockNote(state, async () => { markRefreshes += 1; }));
      dom.element("alertType").value = "price_above";
      dom.element("alertThreshold").value = "12.5";
      const alertMessage = await rejectedMessage(addAlertRule(state));

      assert(noteMessage === "请求超时，请稍后重试" && alertMessage === "请求超时，请稍后重试", "write timeout message changed");
      assert(delays.length === 2 && delays.every((delay) => delay === 12000), `write timeout was not 12 seconds: ${delays}`);
      assert(calls.every((call) => call.options.signal && call.options.signal.aborted), "timed out writes did not abort their fetch signals");
      assert(calls.length === 2 && markRefreshes === 0, "timed out write started a refresh tail");
      assert(dom.element("noteContent").value === "超时笔记" && dom.element("alertThreshold").value === "12.5", "timed out write cleared current form input");
    '''
    _run_node_script(script)


def test_current_note_and_alert_read_timeouts_render_errors() -> None:
    script = r'''
      import { loadAlerts } from "./static/js/alerts.js";
      import { loadNotes } from "./static/js/notes.js";

      const dom = installNotesAlertsDom();
      const state = { symbol: "600519.SH" };
      const delays = expireRequestsImmediately();
      globalThis.fetch = async () => new Promise(() => {});

      const noteResult = await loadNotes(state);
      const alertResult = await loadAlerts(state);

      assert(noteResult === false && alertResult === true, "read timeout return behavior changed");
      assert(delays.length === 3 && delays.every((delay) => delay === 12000), `read timeout was not 12 seconds: ${delays}`);
      assert(dom.element("noteList").innerHTML.includes("笔记读取失败") && dom.element("noteList").innerHTML.includes("请求超时"), "current notes timeout was hidden as an abort");
      assert(dom.element("alertList").innerHTML.includes("预警读取失败") && dom.element("alertList").innerHTML.includes("请求超时"), "current alert rules timeout was hidden as an abort");
      assert(dom.element("alertEvents").innerHTML.includes("事件读取失败") && dom.element("alertEvents").innerHTML.includes("请求超时"), "current alert events timeout was hidden as an abort");
    '''
    _run_node_script(script)


def test_rendered_note_and_alert_rows_include_local_action_feedback() -> None:
    script = r'''
      import { renderAlerts } from "./static/js/alerts.js";
      import { renderNotes } from "./static/js/notes.js";

      const dom = installNotesAlertsDom();
      renderAlerts([alertRule("价格突破")]);
      renderNotes([note("等待复核")]);

      assert(dom.element("alertList").innerHTML.includes('class="row-action-feedback"'), "alert row has no action feedback target");
      assert(dom.element("noteList").innerHTML.includes('class="row-action-feedback"'), "note row has no action feedback target");
      assert(dom.element("alertList").innerHTML.includes('role="alert" hidden'), "alert row feedback is not accessible and initially hidden");
      assert(dom.element("noteList").innerHTML.includes('role="alert" hidden'), "note row feedback is not accessible and initially hidden");
    '''
    _run_node_script(script)


def _run_node_script(test_body: str) -> None:
    script = f"{test_body}\n{FAKE_DOM_SCRIPT}"
    subprocess.run(
        ["node", "--input-type=module", "-e", script],
        cwd=ROOT,
        check=True,
        text=True,
        capture_output=True,
    )


FAKE_DOM_SCRIPT = r'''
function installNotesAlertsDom() {
  const elements = new Map();
  function element(id) {
    if (!elements.has(id)) {
      elements.set(id, {
        id,
        value: "",
        innerHTML: "",
        dataset: {},
        disabled: false,
        hidden: false,
        textContent: id === "evaluateAlerts" ? "检查" : "",
        attributes: {},
        setAttribute(name, value) {
          this.attributes[name] = String(value);
        },
      });
    }
    return elements.get(id);
  }
  element("noteType").value = "观察";
  element("alertType").value = "price_above";
  globalThis.document = { getElementById: element };
  return { element };
}

function note(content) {
  return {
    id: content,
    note_type: "观察",
    content,
    price: 10,
    trade_date: "2026-07-14",
    created_at: "2026-07-14",
    visible: true,
  };
}

function alertRule(name, overrides = {}) {
  return {
    id: name,
    name,
    condition_type: "price_above",
    condition_label: "价格高于",
    threshold: 12,
    enabled: true,
    trigger_count: 0,
    cooldown_seconds: 300,
    ...overrides,
  };
}

function alertEvent(message) {
  return {
    name: "测试预警",
    event_type: "触发",
    created_at: "2026-07-14",
    price: 12,
    change_pct: 1,
    message,
  };
}

function deferredReply() {
  let resolve;
  const promise = new Promise((settle) => {
    resolve = settle;
  });
  return { promise, resolve };
}

function jsonResponse(payload) {
  return {
    ok: true,
    async json() {
      return payload;
    },
  };
}

function errorResponse(status, detail) {
  return {
    ok: false,
    status,
    async json() {
      return { detail };
    },
  };
}

function captureRequestTimeouts() {
  const delays = [];
  const nativeSetTimeout = globalThis.setTimeout;
  globalThis.setTimeout = (callback, delay, ...args) => {
    delays.push(delay);
    return nativeSetTimeout(callback, delay, ...args);
  };
  return delays;
}

function expireRequestsImmediately() {
  const delays = [];
  globalThis.setTimeout = (callback, delay) => {
    delays.push(delay);
    queueMicrotask(callback);
    return delays.length;
  };
  globalThis.clearTimeout = () => {};
  return delays;
}

async function rejectedMessage(promise) {
  try {
    await promise;
  } catch (error) {
    return error.message;
  }
  return "resolved";
}

function assert(condition, message) {
  if (!condition) throw new Error(message);
}
'''
