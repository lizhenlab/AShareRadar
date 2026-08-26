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
const run = { id: 141, snapshot_digest: "f".repeat(64) };
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
controller.sync({ id: 142, snapshot_digest: "a".repeat(64) });
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

const filters = { min_probability: null, market: null, keyword: "", sort: "probability" };
for (const patch of [{ formal_filter_qualified: true }, { run_id: 142 }, { production_ranking_effect: "v6" }, { total: 2 }, { mode: "official" }]) {
  assert.throws(() => validateExperimentalProbability({ ...first, ...patch }, 141, 1, filters));
}
for (const value of [null, NaN, 1.2, -0.2, "0.6"]) {
  const bad = structuredClone(first); bad.items[0].probability = value;
  assert.throws(() => validateExperimentalProbability(bad, 141, 1, filters));
}
function response(runId, minimum = null) {
  return { schema_version: "market-scan-personal-experimental-probability-v1", run_id: runId,
    mode: "personal_experimental", experimental: true, formal_filter_qualified: false, production_ranking_effect: "none",
    horizon: 5, target: "net_return_positive", page: 1, page_size: 50, page_count: 1, total: 1, model_digest: "a".repeat(64),
    signal_date: "2026-08-25", base_snapshot_digest: (runId === 141 ? "f" : "a").repeat(64), filters: { min_probability: minimum, market: null, keyword: "", sort: "probability" },
    model: { training_symbol_count: 174, train_session_count: 373, calibration_session_count: 40, latest_label_date: "2026-07-31",
      historical_recipe_evaluation: { status: "insufficient_data" } },
    coverage: { successful_scan_count: 1, predicted_count: 1, unavailable_count: 0 },
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
const timers = new Map(); let nextTimer = 0;
globalThis.setTimeout = (callback, delay) => { assert.ok(delay >= 1000 && delay <= 5000); timers.set(++nextTimer, callback); return nextTimer; };
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
for (let attempt = 0; attempt < 8; attempt += 1) { await flush(); tick(); }
assert.equal(await bounded, busy); assert.equal(calls, 9);
await assert.rejects(requestExperimentalProbability(async () => { throw Object.assign(new Error("invalid"), { status: 422 }); },
  "/experiment", scope.signal), /invalid/);
assert.equal(timers.size, 0);
function tick() { const [id, callback] = [...timers][0]; timers.delete(id); callback(); }
async function flush() { for (let i = 0; i < 10; i += 1) await Promise.resolve(); }
'''
    result = subprocess.run(["node", "--input-type=module", "-e", script], cwd=ROOT, text=True, capture_output=True)
    assert result.returncode == 0, result.stderr
