from __future__ import annotations

import subprocess
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]


def test_diagnostics_navigation_owns_pending_reads_and_queued_timer() -> None:
    _run_node(r'''
      const pending = [];
      intercept = (url, options) => diagnosticEndpoints.has(url)
        ? new Promise(resolve => pending.push({ url, signal: options.signal, resolve })) : null;
      app.setWorkspaceView("diagnostics");
      await flush();
      assert.equal(pending.length, 4, "diagnostics entry must read all four panels");
      const old = pending.slice();
      app.setWorkspaceView("finance");
      assert(old.every(request => request.signal.aborted), "leaving must abort diagnostic reads");
      app.setWorkspaceView("diagnostics");
      await flush();
      assert.equal(pending.length, 8, "reentry must obtain a new cohort of reads");
      for (const request of pending.slice(4)) request.resolve(jsonResponse(payload(request.url, "new")));
      await flush();
      assert.equal(timers.size, 1, "reentry must create one monitoring timer");
      const timer = [...timers.values()][0];
      for (const request of old) request.resolve(jsonResponse(payload(request.url, "old")));
      await flush();
      assert.equal(timers.size, 1, "old finally must not replace or duplicate the current timer");
      assert(element("taskCards").innerHTML.includes("new"));
      assert(!element("taskCards").innerHTML.includes("old"), "old results repainted the new diagnostic page");
      app.setWorkspaceView("finance");
      assert.equal(timers.size, 0, "leaving a completed diagnostic page must stop polling");
      const before = pending.length;
      await timer.callback();
      await flush();
      assert.equal(pending.length, before, "a queued timer from the old page must not start requests");
      assert.equal(timers.size, 0, "a queued old timer must not resurrect polling");
    ''')


def test_visibility_only_resumes_diagnostics_for_its_visible_workspace() -> None:
    _run_node(r'''
      assert.deepEqual(Object.keys(app.refreshGlobalPanels()).sort(), ["dataStatus", "market", "plates", "watchlist"]);
      await flush();
      assert.equal(calls.filter(call => diagnosticEndpoints.has(call.url)).length, 0);
      app.setWorkspaceView("diagnostics");
      await flush();
      assert.equal(calls.filter(call => diagnosticEndpoints.has(call.url)).length, 4);
      assert.equal(timers.size, 1);
      document.hidden = true;
      app.handleVisibilityChange();
      assert.equal(timers.size, 0);
      document.hidden = false;
      app.handleVisibilityChange();
      await flush();
      assert.equal(calls.filter(call => diagnosticEndpoints.has(call.url)).length, 8);
      assert.equal(timers.size, 1, "visible diagnostics did not resume exactly one timer");
      app.setWorkspaceView("data");
      await flush();
      assert.equal(timers.size, 0);
      document.hidden = true;
      app.handleVisibilityChange();
      document.hidden = false;
      app.handleVisibilityChange();
      await flush();
      assert.equal(calls.filter(call => diagnosticEndpoints.has(call.url)).length, 8, "visibility resumed hidden diagnostics");
      assert.equal(calls.filter(call => call.url.includes("cleanup-preview")).length, 0);
      assert.equal(timers.size, 0);
      assert(calls.some(call => call.url === "/api/data/status"), "global capability discovery was removed");
    ''')


def test_leaving_diagnostics_does_not_cancel_explicit_monitor_task_write() -> None:
    _run_node(r'''
      let releaseWrite;
      let writeSignal;
      intercept = (url, options) => url.startsWith("/api/tasks/run-once")
        ? new Promise(resolve => { releaseWrite = resolve; writeSignal = options.signal; }) : null;
      app.setWorkspaceView("diagnostics");
      await flush();
      const button = { dataset: { task: "refresh_quotes" } };
      const writing = element("monitorActions").listeners.click({ target: { closest: () => button } });
      await flush();
      assert.equal(app.state.monitorTaskRunning, true);
      app.setWorkspaceView("finance");
      assert.equal(writeSignal.aborted, false, "navigation cancelled an already issued write");
      const diagnosticReads = calls.filter(call => diagnosticEndpoints.has(call.url)).length;
      releaseWrite(jsonResponse({ ok: true }));
      await writing;
      await flush();
      assert.equal(app.state.monitorTaskRunning, false);
      assert.equal(calls.filter(call => call.url.startsWith("/api/tasks/run-once")).length, 1);
      assert.equal(calls.filter(call => diagnosticEndpoints.has(call.url)).length, diagnosticReads);
      assert.equal(timers.size, 0, "write completion restarted hidden diagnostics");
    ''')


def test_cleanup_preview_is_explicit_and_commit_rechecks_before_writing() -> None:
    _run_node(r'''
      const cleanup = "/api/local-data/cleanup-preview";
      let resolvePreview;
      let nextPreview = { total_rows: 3, user_history_rows: 0, requires_user_backup: false };
      intercept = (url) => url === cleanup
        ? new Promise(resolve => { resolvePreview = () => resolve(jsonResponse(nextPreview)); }) : null;
      element("runRuntimeCleanup").disabled = true;
      app.setWorkspaceView("data");
      await flush();
      assert.equal(calls.filter(call => call.url === cleanup).length, 0);
      const button = element("refreshRuntimeCleanupPreview");
      const preview = button.listeners.click();
      assert.equal(button.disabled, true, "preview button allowed duplicate clicks in flight");
      resolvePreview();
      await preview;
      assert.equal(button.disabled, false);
      assert.equal(element("runRuntimeCleanup").disabled, false);
      assert(element("runtimeCleanupPreview").innerHTML.includes("3 条"));
      nextPreview = { total_rows: 0, user_history_rows: 0, requires_user_backup: false };
      const commit = element("runRuntimeCleanup").listeners.click();
      assert.equal(calls.filter(call => call.url === cleanup).length, 2, "execution reused a stale preview");
      resolvePreview();
      await commit;
      assert.equal(calls.filter(call => call.method === "POST").length, 0, "empty fresh preview still wrote cleanup");
      assert.equal(element("runRuntimeCleanup").disabled, true);
      intercept = (url) => url === cleanup ? Promise.resolve(new Response(JSON.stringify({ detail: "synthetic preview failure" }), { status: 503 })) : null;
      await button.listeners.click();
      assert.equal(button.disabled, false, "failed preview cannot be retried");
      assert.equal(element("runRuntimeCleanup").disabled, true);
      assert(element("runtimeCleanupPreview").innerHTML.includes("synthetic preview failure"));
      await element("runRuntimeCleanup").listeners.click();
      assert.equal(calls.filter(call => call.method === "POST").length, 0, "failed fresh preview still wrote cleanup");
    ''')


def _run_node(script: str) -> None:
    subprocess.run(
        ["node", "--input-type=module", "-e", _SETUP + script],
        cwd=ROOT,
        check=True,
        timeout=30,
    )


_SETUP = r'''
import assert from "node:assert/strict";
import { installAppDom } from "./tests/frontend_app_flow_helpers.mjs";
const { element, jsonResponse } = installAppDom({ canvasContext: null });
const buttons = ["strategy", "finance", "data", "diagnostics"].map(view => {
  const button = element(`workspace-tab-${view}`);
  button.dataset.view = view;
  return button;
});
document.querySelectorAll = selector => selector === ".workspace-tabs button[data-view]" ? buttons : [];
const timers = new Map();
let timerId = 0;
globalThis.setInterval = (callback, delay) => { timers.set(++timerId, { callback, delay }); return timerId; };
globalThis.clearInterval = id => timers.delete(id);
const diagnosticEndpoints = new Set([
  "/api/tasks/status", "/api/tasks/runs?limit=8", "/api/monitor/events?limit=8", "/api/system/diagnostics",
]);
const calls = [];
let intercept = () => null;
globalThis.fetch = (url, options = {}) => {
  url = String(url);
  calls.push({ url, method: options.method || "GET" });
  return intercept(url, options) || Promise.resolve(jsonResponse(payload(url)));
};
const { __appTest: app } = await import("./static/app.js");
async function flush() { for (let count = 0; count < 5; count++) await new Promise(resolve => setImmediate(resolve)); }
function payload(url, name = "current") {
  if (url === "/api/tasks/status") return { enabled: false, running: false, tasks: [{ name: "refresh_quotes", display_name: name, enabled: true }] };
  if (url === "/api/system/diagnostics") return { storage: {}, warnings: [], suggestions: [] };
  if (url === "/api/data/status") return { providers: [], source_plan: {}, cache: {}, capabilities: [], capability_statuses: [] };
  if (url === "/api/market") return { indices: [] };
  if (url === "/api/strong-stocks") return { items: [] };
  if (url === "/api/fuyao/status") return { enabled: true, configured: true, persistent_daily_requests: 0, jobs: [] };
  if (url.startsWith("/api/fuyao/")) return { available: false, sectors: null, sentiment: null, history: null };
  return [];
}
'''
