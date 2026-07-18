from __future__ import annotations

import subprocess
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]


def test_stock_search_debounces_by_default_and_trims_the_query() -> None:
    script = r'''
      import assert from "node:assert/strict";
      import { createStockSearchController } from "./static/js/stock-search.js";

      const calls = [];
      const states = [];
      const timers = new Map();
      const realSetTimeout = globalThis.setTimeout;
      const realClearTimeout = globalThis.clearTimeout;
      let nextTimerId = 0;
      globalThis.setTimeout = (callback, delay) => {
        const timerId = ++nextTimerId;
        timers.set(timerId, { callback, delay });
        return timerId;
      };
      globalThis.clearTimeout = (timerId) => timers.delete(timerId);
      globalThis.fetch = async (url, options = {}) => {
        calls.push({ url: String(url), options });
        return jsonResponse([stock("600519.SH", "Moutai")]);
      };
      const controller = createStockSearchController({ onState: (state) => states.push(state) });

      controller.input("  600  ");
      controller.input("  600519  ");
      assert.equal(calls.length, 0, "request escaped the default 250ms debounce");
      assert.equal(timers.size, 1, "superseded debounce timer was not cleared");
      const [timerId, timer] = timers.entries().next().value;
      assert.equal(timer.delay, 250);
      timers.delete(timerId);
      globalThis.setTimeout = realSetTimeout;
      globalThis.clearTimeout = realClearTimeout;
      timer.callback();
      await waitFor(() => states.at(-1)?.phase === "ready", "debounced ready state");

      assert.equal(calls.length, 1);
      assert.equal(calls[0].url, "/api/stocks?keyword=600519&limit=8");
      assert.equal(states.at(-1).query, "600519");
      assert.equal(states.at(-1).activeIndex, -1);
      controller.destroy();
    '''
    _run_node_script(script)


def test_stock_search_aborts_old_requests_and_rejects_stale_results() -> None:
    script = r'''
      import assert from "node:assert/strict";
      import { createStockSearchController } from "./static/js/stock-search.js";

      const firstJson = deferred();
      const calls = [];
      const states = [];
      globalThis.fetch = async (url, options = {}) => {
        const call = { url: String(url), options };
        calls.push(call);
        if (calls.length === 1) {
          return { ok: true, status: 200, json: () => firstJson.promise };
        }
        return jsonResponse([stock("000001.SZ", "Current")]);
      };
      const controller = createStockSearchController({
        debounceMs: 0,
        onState: (state) => states.push(state),
      });

      controller.input("600519");
      await waitFor(() => calls.length === 1, "first request");
      controller.input("000001");
      await waitFor(() => states.at(-1)?.phase === "ready", "latest response");

      assert.equal(calls.length, 2);
      assert.equal(calls[0].options.signal.aborted, true);
      assert.equal(states.at(-1).items[0].symbol, "000001.SZ");
      firstJson.resolve([stock("600519.SH", "Stale")]);
      await sleep(20);
      assert.equal(states.at(-1).items[0].symbol, "000001.SZ");
      assert.equal(states.some((state) => state.phase === "unavailable"), false);
      controller.destroy();
    '''
    _run_node_script(script)


def test_stock_search_uses_a_bounded_lru_cache_without_refetching_hits() -> None:
    script = r'''
      import assert from "node:assert/strict";
      import { createStockSearchController } from "./static/js/stock-search.js";

      const rows = {
        alpha: stock("600000.SH", "Alpha"),
        beta: stock("000001.SZ", "Beta"),
        gamma: stock("300001.SZ", "Gamma"),
      };
      const calls = [];
      let latest;
      globalThis.fetch = async (url) => {
        const query = new URL(String(url), "http://local").searchParams.get("keyword");
        calls.push(query);
        return jsonResponse([rows[query.toLowerCase()]]);
      };
      const controller = createStockSearchController({
        debounceMs: 0,
        cacheSize: 2,
        onState: (state) => { latest = state; },
      });

      await searchFor(controller, () => latest, "Alpha");
      await searchFor(controller, () => latest, " alpha ");
      assert.deepEqual(calls, ["Alpha"], "case-normalized cache hit made a request");
      await searchFor(controller, () => latest, "beta");
      await searchFor(controller, () => latest, "gamma");
      await searchFor(controller, () => latest, "alpha");

      assert.deepEqual(calls, ["Alpha", "beta", "gamma", "alpha"]);
      assert.equal(latest.items[0].symbol, "600000.SH");
      controller.destroy();
    '''
    _run_node_script(script)


def test_stock_search_clears_empty_input_immediately_without_a_request() -> None:
    script = r'''
      import assert from "node:assert/strict";
      import { createStockSearchController } from "./static/js/stock-search.js";

      const calls = [];
      const states = [];
      globalThis.fetch = async (url, options = {}) => {
        calls.push({ url, options });
        return new Promise(() => {});
      };
      const controller = createStockSearchController({
        debounceMs: 0,
        onState: (state) => states.push(state),
      });

      controller.input("600519");
      await waitFor(() => calls.length === 1, "inflight request");
      controller.input("   ");

      assert.equal(calls[0].options.signal.aborted, true);
      assert.deepEqual(states.at(-1), {
        phase: "idle",
        query: "",
        items: [],
        activeIndex: -1,
        message: "",
      });
      await sleep(20);
      assert.equal(calls.length, 1);
      assert.equal(states.some((state) => state.phase === "unavailable"), false);
      controller.destroy();
    '''
    _run_node_script(script)


def test_stock_search_marks_dirty_or_malicious_payloads_unavailable() -> None:
    script = r'''
      import assert from "node:assert/strict";
      import { createStockSearchController } from "./static/js/stock-search.js";

      const inherited = Object.create({
        symbol: "600519.SH",
        code: "600519",
        market: "SH",
        name: "Inherited",
      });
      const payloads = [
        { symbol: "600519.SH" },
        [inherited],
        [{ symbol: "600519.SH", code: "600519", market: "SH" }],
        [{ symbol: "600519.SH?next=/admin", code: "600519", market: "SH", name: "Injected" }],
        [{ symbol: "600519.SH", code: "000001", market: "SH", name: "Mismatch" }],
        [{ symbol: "000000.SZ", code: "000000", market: "SZ", name: "Zero" }],
        [{ symbol: "600519.SH", code: "600519", market: "SH", name: "x".repeat(81) }],
        [{ symbol: "600519.SH", code: "600519", market: "SH", name: "Valid", industry: { bad: true } }],
        Array.from({ length: 9 }, (_, index) => stock(`${String(600000 + index).padStart(6, "0")}.SH`, `Row${index}`)),
      ];
      const states = [];
      let callIndex = 0;
      globalThis.fetch = async () => jsonResponse(payloads[callIndex++]);
      const controller = createStockSearchController({
        debounceMs: 0,
        onState: (state) => states.push(state),
      });

      for (let index = 0; index < payloads.length; index += 1) {
        controller.input(`dirty-${index}`);
        await waitFor(
          () => states.at(-1)?.phase === "unavailable" && states.at(-1)?.query === `dirty-${index}`,
          `dirty payload ${index}`
        );
        const state = states.at(-1);
        assert.deepEqual(state.items, []);
        assert.equal(state.activeIndex, -1);
        assert.match(state.message, /^Stock search unavailable:/);
      }
      controller.destroy();
    '''
    _run_node_script(script)


def test_stock_search_surfaces_failures_and_recovers_on_the_next_query() -> None:
    script = r'''
      import assert from "node:assert/strict";
      import { createStockSearchController } from "./static/js/stock-search.js";

      const states = [];
      let failing = true;
      globalThis.fetch = async () => {
        if (failing) {
          return { ok: false, status: 503, async json() { return { detail: "search service down" }; } };
        }
        return jsonResponse([]);
      };
      const controller = createStockSearchController({
        debounceMs: 0,
        onState: (state) => states.push(state),
      });

      controller.input("first");
      await waitFor(() => states.at(-1)?.phase === "unavailable", "failure state");
      assert.match(states.at(-1).message, /search service down/);
      failing = false;
      controller.input("second");
      await waitFor(() => states.at(-1)?.phase === "empty", "empty recovery state");
      assert.equal(states.at(-1).message, "");
      controller.destroy();
    '''
    _run_node_script(script)


def test_stock_search_keyboard_boundaries_and_selection_are_deterministic() -> None:
    script = r'''
      import assert from "node:assert/strict";
      import { createStockSearchController } from "./static/js/stock-search.js";

      const states = [];
      const selections = [];
      globalThis.fetch = async () => jsonResponse([
        stock("600000.SH", "First"),
        stock("000001.SZ", "Second"),
        stock("300001.SZ", "Third"),
      ]);
      const controller = createStockSearchController({
        debounceMs: 0,
        onState(state) {
          states.push(state);
          if (state.phase === "ready") state.items[0].symbol = "mutated";
        },
        onSelect(symbol, item) { selections.push({ symbol, item }); },
      });

      controller.input("bank");
      await waitFor(() => states.at(-1)?.phase === "ready", "keyboard results");
      assert.equal(controller.move(1), 0);
      assert.equal(controller.move(-1), 0, "lower boundary did not clamp");
      assert.equal(controller.move(99), 2);
      assert.equal(controller.move(1), 2, "upper boundary did not clamp");
      assert.equal(controller.move(0), 2);
      assert.equal(controller.move(1.5), 2);

      const selected = controller.selectActive();
      assert.equal(selected.symbol, "300001.SZ");
      assert.deepEqual(selections, [{ symbol: "300001.SZ", item: selected }]);
      assert.equal(states.at(-1).phase, "closed");

      controller.input("bank");
      assert.equal(states.at(-1).phase, "ready");
      assert.equal(controller.move(-1), 2, "initial ArrowUp did not select the last item");
      assert.equal(controller.selectIndex(-1), null);
      assert.equal(controller.selectIndex(1.2), null);
      assert.equal(controller.selectIndex(3), null);
      assert.equal(selections.length, 1);
      assert.equal(controller.selectIndex(0).symbol, "600000.SH");
      assert.equal(selections[1].symbol, "600000.SH", "state callback mutated controller data");
      controller.destroy();
    '''
    _run_node_script(script)


def test_stock_search_destroy_cancels_work_and_prevents_all_later_callbacks() -> None:
    script = r'''
      import assert from "node:assert/strict";
      import { createStockSearchController } from "./static/js/stock-search.js";

      const responseJson = deferred();
      const calls = [];
      const states = [];
      const selections = [];
      globalThis.fetch = async (url, options = {}) => {
        calls.push({ url, options });
        return { ok: true, status: 200, json: () => responseJson.promise };
      };
      const controller = createStockSearchController({
        debounceMs: 0,
        onState: (state) => states.push(state),
        onSelect: (...args) => selections.push(args),
      });

      controller.input("600519");
      await waitFor(() => calls.length === 1, "request before destroy");
      const callbackCount = states.length;
      assert.equal(controller.destroy(), true);
      assert.equal(calls[0].options.signal.aborted, true);
      assert.equal(controller.destroy(), false);

      responseJson.resolve([stock("600519.SH", "Late")]);
      controller.input("000001");
      controller.move(1);
      controller.selectIndex(0);
      controller.close();
      await sleep(20);
      assert.equal(states.length, callbackCount);
      assert.equal(selections.length, 0);
      assert.equal(calls.length, 1);
    '''
    _run_node_script(script)


def _run_node_script(test_body: str) -> None:
    subprocess.run(
        ["node", "--input-type=module", "-e", f"{test_body}\n{NODE_HELPERS}"],
        cwd=ROOT,
        check=True,
        text=True,
        capture_output=True,
    )


NODE_HELPERS = r'''
function stock(symbol, name) {
  const [code, market] = symbol.split(".");
  return { symbol, code, market, name, source: "test", updated_at: "2026-07-15" };
}

function jsonResponse(value) {
  return { ok: true, status: 200, async json() { return value; } };
}

function deferred() {
  let resolve;
  let reject;
  const promise = new Promise((resolvePromise, rejectPromise) => {
    resolve = resolvePromise;
    reject = rejectPromise;
  });
  return { promise, resolve, reject };
}

function sleep(milliseconds) {
  return new Promise((resolve) => setTimeout(resolve, milliseconds));
}

async function waitFor(predicate, label, timeoutMs = 1200) {
  const startedAt = Date.now();
  while (!predicate()) {
    if (Date.now() - startedAt > timeoutMs) throw new Error(`timed out waiting for ${label}`);
    await sleep(5);
  }
}

async function searchFor(controller, currentState, query) {
  controller.input(query);
  await waitFor(
    () => currentState()?.query === String(query).trim() && ["ready", "empty"].includes(currentState()?.phase),
    `search result for ${query}`
  );
}
'''
