from pathlib import Path
import subprocess


ROOT = Path(__file__).resolve().parents[1]


def test_experimental_opt_in_filtering_stale_response_and_formal_isolation():
    script = r'''
import assert from "node:assert/strict";
import { createExperimentalProbabilityController, validateExperimentalProbability } from "./static/js/market-scan-experimental.js";
const elements = new Map();
const root = { getElementById(id) {
  if (!elements.has(id)) elements.set(id, { id, value: "", checked: false, hidden: false, innerHTML: "", textContent: "", listeners: {},
    addEventListener(name, callback) { this.listeners[name] = callback; }, setAttribute() {} });
  return elements.get(id);
}};
const el = (suffix) => root.getElementById(`marketScanExperimental${suffix}`);
el("Sort").value = "probability";
el("Enable").checked = true; // Simulate a browser restoring old form values on reload.
const pending = [], calls = [];
const controller = createExperimentalProbabilityController({ root, request(url, options) {
  calls.push({ url, signal: options.signal });
  return new Promise((resolve) => pending.push(resolve));
}});
const run = { id: 141, snapshot_digest: "f".repeat(64), data_date: "2026-08-25" };
controller.sync(run);
assert.equal(el("Enable").checked, false, "restored form state must not silently opt in");
assert.equal(calls.length, 0, "default must never request personal probabilities");
el("Enable").checked = true;
el("Enable").listeners.change();
assert.match(calls[0].url, /acknowledge_experimental=true/);
const first = response(141);
pending.shift()(first);
await flush();
assert.equal(el("Table").hidden, false);
assert.match(el("Rows").innerHTML, /60.00%/);
assert.match(el("Rows").innerHTML, /&lt;img/);
assert.doesNotMatch(el("Rows").innerHTML, /<img/);
el("Minimum").value = "60";
el("Controls").listeners.submit({ preventDefault() {} });
assert.match(calls[1].url, /min_probability=0.6/);
controller.sync({ id: 142, snapshot_digest: "a".repeat(64), data_date: "2026-08-25" });
assert.equal(calls[1].signal.aborted, true);
pending.shift()(response(141, .6));
await flush();
assert.equal(el("Rows").innerHTML, "", "old run response must not render");
pending.shift()(response(142, .6));
await flush();
assert.match(el("Status").textContent, /#142/);
el("Enable").checked = false;
el("Enable").listeners.change();
assert.equal(el("Table").hidden, true);
assert.equal(el("Rows").innerHTML, "");
assert.match(el("Status").textContent, /正式榜单未改变/);

el("Minimum").value = "";
el("Kind").value = "close_d1";
el("Kind").listeners.change();
assert.equal(calls.length, 3, "changing kind while disabled must not request a model");
el("Enable").checked = true;
el("Enable").listeners.change();
assert.match(calls.at(-1).url, /prediction_kind=close_d1/);
pending.shift()(response(142, null, "close_d1"));
await flush();
assert.match(el("ProbabilityLabel").textContent, /D\+1 收盘上涨/);
assert.match(el("Semantics").textContent, /不扣费/);
assert.match(el("Evidence").textContent, /尚未做独立样本外评估/);
el("Controls").listeners.submit({ preventDefault() {} });
const oldDirection = calls.at(-1);
el("Kind").value = "close_d2";
el("Kind").listeners.change();
assert.equal(oldDirection.signal.aborted, true);
assert.match(calls.at(-1).url, /prediction_kind=close_d2/);
pending.shift()(response(142, null, "close_d1"));
await flush();
assert.equal(el("Rows").innerHTML, "", "late D+1 result must not render under D+2");
pending.shift()(response(142, null, "close_d2"));
await flush();
assert.match(el("ProbabilityLabel").textContent, /D\+2 收盘上涨/);
assert.match(el("Status").textContent, /D\+2 收盘上涨/);
const directionFilters = { prediction_kind: "close_d2", min_probability: null, market: null, keyword: "", sort: "probability" };
assert.throws(() => validateExperimentalProbability(response(142, null, "close_d1"), 142, 1, directionFilters));
for (const patch of [{ target: "net_return_positive" }, { target_session_offset: 1 }, { reference: "next_session_open" }]) {
  assert.throws(() => validateExperimentalProbability({ ...response(142, null, "close_d2"), ...patch }, 142, 1, directionFilters));
}
const borrowed = response(142, null, "close_d2");
borrowed.model.historical_recipe_evaluation = { status: "calibrated_shadow" };
assert.throws(() => validateExperimentalProbability(borrowed, 142, 1, directionFilters));
el("Kind").value = "net_h5";
el("Kind").listeners.change();
pending.shift()(response(142, null, "net_h5"));
await flush();
assert.match(el("ProbabilityLabel").textContent, /H5/);
assert.match(el("Semantics").textContent, /扣除模型成本/);

el("Kind").value = "close_d5";
el("Kind").listeners.change();
pending.shift()(response(142, null, "close_d5"));
await flush();
assert.match(el("ProbabilityLabel").textContent, /D\+5/);
assert.match(el("Semantics").textContent, /固定第 5 个交易日/);

// Paging binds the applied filters and both model/data versions, regardless of JSON key order.
el("Controls").listeners.submit({ preventDefault() {} });
pending.shift()(multipage(1)); await flush();
el("Next").listeners.click();
assert.match(calls.at(-1).url, /page=2/);
const changedPage = multipage(2); changedPage.input_digest = "c".repeat(64);
pending.shift()(changedPage); await flush();
assert.equal(el("Rows").innerHTML, "");
assert.match(el("Status").textContent, /模型或行情数据已更新/);
el("Controls").listeners.submit({ preventDefault() {} });
pending.shift()(multipage(1)); await flush();
el("Next").listeners.click(); pending.shift()(multipage(2)); await flush();
assert.match(el("Page").textContent, /第 2 \/ 2 页/);
el("Minimum").value = "70"; // Programmatic change without a DOM event must also fail closed.
const beforeDraft = calls.length;
el("Prev").listeners.click(); await flush();
assert.equal(calls.length, beforeDraft);
assert.equal(el("Rows").innerHTML, "");
assert.match(el("Status").textContent, /筛选条件已修改/);
el("Minimum").value = "";
el("Controls").listeners.submit({ preventDefault() {} });
const draftRequest = calls.at(-1);
el("Keyword").value = "new draft"; el("Keyword").listeners.input();
assert.equal(draftRequest.signal.aborted, true);
pending.shift()(multipage(1)); await flush();
assert.equal(el("Table").hidden, true, "late response must not fill a newly edited form");
el("Keyword").value = "";
const filters = { prediction_kind: "close_d1", min_probability: null, market: null, keyword: "", sort: "probability" };
for (const patch of [{ formal_filter_qualified: true }, { run_id: 142 }, { production_ranking_effect: "v6" }, { total: 2 }, { mode: "official" },
    { input_digest: "bad" }, { base_snapshot_digest: null }, { signal_date: "2026-02-30" }, { target_session_date: "2026-09-31" }]) {
  assert.throws(() => validateExperimentalProbability({ ...first, ...patch }, 141, 1, filters));
}
for (const value of [null, NaN, 1.2, -0.2, "0.6"]) {
  const bad = structuredClone(first); bad.items[0].probability = value;
  assert.throws(() => validateExperimentalProbability(bad, 141, 1, filters));
}

// Cold-start navigation locates the requested batch, without calling a formal read or changing formal state.
let navigation = { mode: "official", selectedRunId: 142 };
const navigationCalls = [];
const independent = createExperimentalProbabilityController({ root, getNavigation: () => navigation,
  async request(url) {
    navigationCalls.push(url);
    if (url.startsWith("/api/market-scans?")) return navigationPage();
    assert.match(url, /^\/api\/market-scans\/142\/experimental-probability\?/);
    return response(142);
  } });
independent.sync(null);
assert.equal(navigationCalls.length, 0);
el("Enable").checked = true; el("Enable").listeners.change(); await flush();
assert.match(navigationCalls[0], /authority=navigation/);
assert.equal(navigationCalls.length, 2);
assert.match(el("Status").textContent, /独立校验批次/);
assert.match(el("Status").textContent, /不表示正式榜单已完成校验/);
assert.equal(el("Table").hidden, false);
navigation = { mode: "official", selectedRunId: 999 };
independent.navigationChanged(); await flush();
assert.equal(navigationCalls.length, 3, "a missing selected batch must not fall back to the latest");
assert.match(el("Status").textContent, /所选历史批次/);
assert.equal(el("Table").hidden, true);
independent.navigationChanged(); await flush();
assert.equal(navigationCalls.length, 3, "unchanged navigation must not restart a request");
navigation = { mode: "intraday", selectedRunId: null };
independent.sync(null); await flush();
assert.match(el("Status").textContent, /与所选模式不一致/);
assert.equal(navigationCalls.length, 4);

function navigationPage() {
  const run = { id: 142, status: "success", trigger: "manual", mode: "official", rule_version: "full-market-score-v4:test",
    as_of: "2026-08-25 16:00:00", data_date: "2026-08-25", quote_date: "2026-08-25", scope: "沪市 + 深市 + 北交所当前上市A股",
    total_count: 1, excluded_count: 0, processed_count: 1, success_count: 1, missing_count: 0, skipped_count: 0,
    retry_count: 0, progress_pct: 100, coverage_pct: 100, created_at: "2026-08-25 16:00:00", updated_at: "2026-08-25 16:10:00",
    finished_at: "2026-08-25 16:10:00", snapshot_digest: "a".repeat(64), snapshot_seal_origin: "publication", snapshot_sealed_at: "2026-08-25 16:10:00" };
  return { items: [run], page: 1, page_size: 100, total: 1, page_count: 1 };
}
function multipage(page) {
  const value = response(142, null, "close_d5");
  Object.assign(value, { page, total: 51, page_count: 2 });
  Object.assign(value.coverage, { successful_scan_count: 51, predicted_count: 51 });
  const template = value.items[0];
  value.items = Array.from({ length: page === 1 ? 50 : 1 }, (_, index) => {
    const rank = (page - 1) * 50 + index + 1;
    return { ...template, symbol: `${600000 + rank}.SH`, base_rank: rank, experimental_rank: rank };
  });
  value.filters = Object.fromEntries(Object.entries(value.filters).reverse());
  return value;
}
function response(runId, minimum = null, kind = "close_d1") {
  const offset = kind === "net_h5" ? 6 : {close_d1: 1, close_d2: 2, close_d5: 5}[kind];
  const target = kind === "net_h5" ? "net_return_positive" : "close_return_positive";
  const reference = kind === "net_h5" ? "next_session_open" : "signal_day_qfq_close";
  return { schema_version: "market-scan-personal-experimental-probability-v1", run_id: runId,
    mode: "personal_experimental", experimental: true, formal_filter_qualified: false, production_ranking_effect: "none",
    prediction_kind: kind, horizon: kind === "net_h5" ? 5 : offset, target, reference, target_session_offset: offset,
    target_session_date: "2026-09-02", page: 1, page_size: 50, page_count: 1, total: 1, model_digest: "a".repeat(64),
    signal_date: "2026-08-25", input_digest: "b".repeat(64), base_snapshot_digest: (runId === 141 ? "f" : "a").repeat(64), filters: { prediction_kind: kind, min_probability: minimum, market: null, keyword: "", sort: "probability" },
    model: { base_rate: .5, training_symbol_count: 174, train_session_count: 373, calibration_session_count: 40, latest_label_date: "2026-07-31",
      direction_evidence: kind === "net_h5" ? null : { label_version: "close-to-close-fixed-session-v1", reference, target_session_offset: offset,
        comparison: "target_close_gt_signal_close", adjustment_mode: "qfq", fees_included: false, execution_modelled: false },
      historical_recipe_evaluation: kind === "net_h5" ? { status: "insufficient_data" } : { status: "not_evaluated", horizon: offset, target } },
    coverage: { successful_scan_count: 1, predicted_count: 1, unavailable_count: 0, unavailable_reasons: {} },
    items: [{ experimental_rank: 1, base_rank: 7, base_score: 80, symbol: "600001.SH", market: "SH",
      name: '<img src=x onerror="alert(1)">', probability: .6, training_universe_member: false }],
  };
}
async function flush() { for (let i = 0; i < 10; i += 1) await Promise.resolve(); }
'''
    result = subprocess.run(["node", "--input-type=module", "-e", script], cwd=ROOT, text=True, capture_output=True)
    assert result.returncode == 0, result.stderr


def test_experimental_busy_retry_is_bounded_and_cancellation_stops_pending_retries():
    script = r'''
import assert from "node:assert/strict";
import { requestExperimentalProbability } from "./static/js/market-scan-experimental.js";
const timers = new Map(); let nextTimer = 0, now = 0;
performance.now = () => now;
globalThis.setTimeout = (callback, delay) => { assert.ok(delay >= 1000 && delay <= 5000); timers.set(++nextTimer, () => { now += delay; callback(); }); return nextTimer; };
globalThis.clearTimeout = (id) => timers.delete(id);
const busy = Object.assign(new Error("busy"), { status: 503, retryAfterMs: 2000 });
let calls = 0, notices = 0;
const scope = new AbortController();
const result = requestExperimentalProbability(async () => { calls += 1; if (calls === 1) throw busy; return { ready: true }; },
  "/experiment", scope.signal, () => { notices += 1; });
await flush();
assert.equal(notices, 1); assert.equal(calls, 1);
tick();
assert.deepEqual(await result, { ready: true });
assert.equal(calls, 2);

const aborted = new AbortController(); calls = 0;
const cancellation = requestExperimentalProbability(async () => { calls += 1; throw busy; }, "/experiment", aborted.signal)
  .catch((error) => error);
await flush(); aborted.abort();
assert.equal((await cancellation).name, "AbortError");
assert.equal(timers.size, 0); assert.equal(calls, 1);

calls = 0;
const bounded = requestExperimentalProbability(async () => { calls += 1; throw busy; }, "/experiment", scope.signal).catch((error) => error);
for (let attempt = 0; attempt < 44; attempt += 1) { await flush(); tick(); }
assert.equal(await bounded, busy); assert.equal(calls, 45);
await assert.rejects(requestExperimentalProbability(async () => { throw Object.assign(new Error("invalid"), { status: 422 }); },
  "/experiment", scope.signal), /invalid/);
assert.equal(timers.size, 0);
function tick() { const [id, callback] = [...timers][0]; timers.delete(id); callback(); }
async function flush() { for (let i = 0; i < 10; i += 1) await Promise.resolve(); }
'''
    result = subprocess.run(["node", "--input-type=module", "-e", script], cwd=ROOT, text=True, capture_output=True)
    assert result.returncode == 0, result.stderr
