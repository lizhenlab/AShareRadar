from __future__ import annotations

from pathlib import Path
import subprocess

import pytest


ROOT = Path(__file__).resolve().parents[1]


def test_old_dashboard_read_cannot_overwrite_a_completed_simulation() -> None:
    _run_node(
        r'''
        const pendingRead = deferResponse();
        globalThis.fetch = (url) => url === "/api/paper-trading/run"
          ? Promise.resolve(json({ run_id: 2, dashboard: dashboard(2) })) : pendingRead.promise;
        const state = {};
        const oldRead = paper.loadPaperTradingDashboard(state);
        await paper.runPaperTradingSimulation(state);
        pendingRead.resolve(json(dashboard(1)));
        assert.equal(await oldRead, false);
        assert.equal(state.paperTradingDashboard.selected_run_id, 2);
        assert.equal(elements.get("paperExportJson").href, "/api/paper-trading/runs/2/export.json");
        '''
    )


@pytest.mark.parametrize("failure", [False, True])
def test_late_simulation_cannot_repaint_newer_historical_selection(failure: bool) -> None:
    _run_node(
        r'''
        const pendingSimulation = deferResponse();
        let writeSignal;
        globalThis.fetch = (url, options) => {
          if (url === "/api/paper-trading/run") {
            writeSignal = options.signal;
            return pendingSimulation.promise;
          }
          return Promise.resolve(json(dashboard(1)));
        };
        const state = {};
        const simulation = paper.runPaperTradingSimulation(state);
        await paper.selectPaperTradingRun(state, 1);
        const feedback = elements.get("paperTradingFeedback").textContent;
        assert.equal(writeSignal.aborted, false, "new selection cancelled persistence");
        pendingSimulation.resolve(FAILURE
          ? json({ detail: "late failure" }, 503)
          : json({ run_id: 2, dashboard: dashboard(2) }));
        await simulation;
        assert.equal(state.paperTradingDashboard.selected_run_id, 1);
        assert.equal(elements.get("paperTradingFeedback").textContent, feedback);
        assert.equal(elements.get("paperExportJson").href, "/api/paper-trading/runs/1/export.json");
        '''.replace("FAILURE", str(failure).lower())
    )


@pytest.mark.parametrize("operation", ["account", "strategy", "delete"])
def test_paper_write_readback_failure_keeps_a_synchronization_warning(operation: str) -> None:
    _run_node(
        r'''
        const calls = [];
        globalThis.fetch = async (url, options) => {
          calls.push({ url, options });
          return options.method
            ? json(writeResult(OPERATION))
            : json({ detail: "private readback diagnostic" }, 503);
        };
        const result = await mutate(OPERATION, initialState());
        assert.ok(result, "successful persistence was reported as failed");
        assert.equal(calls.length, 2, "write was retried or refresh was omitted");
        const feedback = elements.get("paperTradingFeedback");
        assert.match(feedback.textContent, /已.*[；，].*(刷新|同步).*(失败|未完成)/);
        assert.equal(feedback.dataset.tone, "error");
        assert.equal(feedback.textContent.includes("private readback diagnostic"), false);
        '''.replace("OPERATION", repr(operation))
    )


@pytest.mark.parametrize("operation", ["account", "strategy", "delete"])
def test_late_paper_write_cannot_start_a_readback_over_newer_history(operation: str) -> None:
    _run_node(
        r'''
        const pendingWrite = deferResponse();
        const calls = [];
        let writeSignal;
        globalThis.fetch = (url, options) => {
          calls.push({ url, options });
          if (options.method) {
            writeSignal = options.signal;
            return pendingWrite.promise;
          }
          return Promise.resolve(json(dashboard(1)));
        };
        const state = initialState();
        const write = mutate(OPERATION, state);
        await paper.selectPaperTradingRun(state, 1);
        const feedback = elements.get("paperTradingFeedback").textContent;
        assert.equal(writeSignal.aborted, false, "new selection cancelled persistence");
        pendingWrite.resolve(json(writeResult(OPERATION)));
        assert.ok(await write);
        assert.equal(calls.length, 2, "stale write started a new dashboard read");
        assert.equal(state.paperTradingDashboard.selected_run_id, 1);
        assert.equal(elements.get("paperTradingFeedback").textContent, feedback);
        '''.replace("OPERATION", repr(operation))
    )


def test_current_simulation_failure_remains_visible_to_the_caller() -> None:
    _run_node(
        r'''
        globalThis.fetch = async () => json({ detail: "simulation failed" }, 503);
        await assert.rejects(paper.runPaperTradingSimulation({}), /simulation failed/);
        '''
    )


@pytest.mark.parametrize("operation", ["account", "strategy", "delete"])
@pytest.mark.parametrize("failure", [False, True])
def test_inflight_paper_readback_cannot_repaint_newer_history(operation: str, failure: bool) -> None:
    _run_node(
        r'''
        const pendingReadback = deferResponse();
        const readbackStarted = deferResponse();
        globalThis.fetch = (url, options) => {
          if (options.method) return Promise.resolve(json(writeResult(OPERATION)));
          if (url.includes("run_id=")) return Promise.resolve(json(dashboard(1)));
          readbackStarted.resolve();
          return pendingReadback.promise;
        };
        const state = initialState();
        const write = mutate(OPERATION, state);
        await readbackStarted.promise;
        await paper.selectPaperTradingRun(state, 1);
        const feedback = elements.get("paperTradingFeedback").textContent;
        pendingReadback.resolve(FAILURE
          ? json({ detail: "late readback failure" }, 503)
          : json(dashboard(2)));
        assert.ok(await write);
        assert.equal(state.paperTradingDashboard.selected_run_id, 1);
        assert.equal(elements.get("paperTradingFeedback").textContent, feedback);
        assert.equal(elements.get("paperExportJson").href, "/api/paper-trading/runs/1/export.json");
        '''.replace("OPERATION", repr(operation)).replace("FAILURE", str(failure).lower())
    )


def test_pre_cancelled_paper_read_does_not_replace_the_rendered_view() -> None:
    _run_node(
        r'''
        const controller = new AbortController();
        controller.abort();
        globalThis.fetch = async () => { throw new Error("cancelled request was dispatched"); };
        elements.get("paperTradingSummary").innerHTML = "current rendered view";
        assert.equal(await paper.loadPaperTradingDashboard({}, { signal: controller.signal }), false);
        assert.equal(elements.get("paperTradingSummary").innerHTML, "current rendered view");
        '''
    )


def test_pre_cancelled_paper_read_keeps_the_active_read_owned() -> None:
    _run_node(
        r'''
        const pendingRead = deferResponse();
        globalThis.fetch = () => pendingRead.promise;
        const state = {};
        const active = paper.loadPaperTradingDashboard(state);
        const controller = new AbortController();
        controller.abort();
        assert.equal(await paper.loadPaperTradingDashboard(state, { signal: controller.signal }), false);
        pendingRead.resolve(json(dashboard(1)));
        assert.equal(await active, true);
        assert.equal(state.paperTradingDashboard.selected_run_id, 1);
        '''
    )


def _run_node(script: str) -> None:
    completed = subprocess.run(
        ["node", "--input-type=module", "--eval", _HARNESS + script],
        cwd=ROOT,
        check=False,
        capture_output=True,
        text=True,
        timeout=20,
    )
    assert completed.returncode == 0, completed.stderr


_HARNESS = r'''
import assert from "node:assert/strict";
const elements = new Map([
  "paperTradingSummary", "paperTradingFeedback", "paperRunHistory", "paperExportJson",
  "paperInitialCash", "paperAllocationPct", "paperReviewPlan",
].map((id) => [id, { innerHTML: "", textContent: "", value: "", dataset: {}, hidden: true }]));
elements.get("paperInitialCash").value = "1000000";
elements.get("paperAllocationPct").value = "25";
elements.get("paperReviewPlan").value = "10";
globalThis.document = { getElementById: (id) => elements.get(id) || null };
const paper = await import("./static/js/paper-trading.js");
function deferResponse() {
  let resolve;
  const promise = new Promise((complete) => { resolve = complete; });
  return { promise, resolve };
}
function json(value, status = 200) {
  return new Response(JSON.stringify(value), { status, headers: { "Content-Type": "application/json" } });
}
function dashboard(id) {
  return {
    account: { initial_cash: 1000000 }, performance: { total_equity: 1000000 },
    selected_run_id: id,
    runs: [{ id, as_of: "2026-07-03T16:00:00+08:00", input_fingerprint: "a".repeat(64),
      output_digest: "b".repeat(64), strategy_count: 0, execution_count: 0,
      closed_count: 0, data_unavailable_count: 0 }],
    strategies: [], positions: [], trades: [], events: [], equity_curve: [], cost_profiles: [], notes: [],
  };
}
function initialState() {
  return { adviceReviewDashboardDetails: [{ plan: {
    id: 10, revision: 1, advice_id: 20, symbol: "600519.SH", plan_payload_digest: "a".repeat(64),
  } }] };
}
function writeResult(operation) {
  if (operation === "strategy") return {
    id: 7, plan_id: 10, plan_revision: 1, advice_id: 20,
    symbol: "600519.SH", plan_payload_digest: "a".repeat(64),
  };
  return { ok: true };
}
function mutate(operation, state) {
  if (operation === "account") return paper.updatePaperTradingAccount(state);
  if (operation === "strategy") return paper.createPaperStrategy(state);
  return paper.deletePaperStrategy(state, 7);
}
'''
