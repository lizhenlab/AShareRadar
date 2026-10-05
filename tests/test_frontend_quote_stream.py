from __future__ import annotations

import subprocess
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]

HARNESS = r'''
import assert from "node:assert/strict";
import { createQuoteStreamController } from "./static/js/quote-stream-controller.js";
import { parseQuoteStreamFrame, quoteStreamSymbols } from "./static/js/quote-stream-contracts.js";

function harness() {
  let context = { symbol: "600519.SH", loadSeq: 1 };
  let hidden = false;
  let failCreation = false;
  const streams = [], timers = [], cleared = [], rows = [], statuses = [];
  const controller = createQuoteStreamController({
    getContext: () => ({ ...context }),
    getSymbols: () => [context.symbol],
    isContextCurrent: value => value.symbol === context.symbol && value.loadSeq === context.loadSeq && !value.signal?.aborted,
    canConnect: value => !hidden && value.symbol === context.symbol && value.loadSeq === context.loadSeq && !value.signal?.aborted,
    canReconcile: value => !hidden && value.symbol === context.symbol && value.loadSeq === context.loadSeq && !value.signal?.aborted,
    isHidden: () => hidden,
    createStream(url) {
      assert.equal(streams.filter(stream => !stream.closed).length, 0, "parallel connection admitted");
      if (failCreation) throw new Error("constructor failed");
      const stream = { url, closed: false, listeners: {},
        addEventListener(name, callback) { this.listeners[name] = callback; },
        close() { this.closed = true; },
      };
      streams.push(stream);
      return stream;
    },
    setTimeout(callback, delay) { const id = timers.length; timers.push({ callback, delay, id }); return id; },
    clearTimeout: id => cleared.push(id),
    onRows: value => rows.push(value),
    onStatus: (...value) => statuses.push(value),
  });
  return { controller, streams, timers, cleared, rows, statuses,
    select(symbol, loadSeq, signal) { context = { symbol, loadSeq, signal }; },
    hide(value) { hidden = value; },
    failCreation(value) { failCreation = value; },
  };
}

const frame = { data: JSON.stringify([{ name: "贵州茅台", market: "SH", code: "600519", amount: 10, price: 10, change_pct: 1 }]) };
'''


def _run_script(script: str) -> None:
    result = subprocess.run(
        ["node", "--input-type=module", "-e", HARNESS + script],
        cwd=ROOT,
        capture_output=True,
        text=True,
        check=False,
        timeout=30,
    )
    assert result.returncode == 0, result.stdout + result.stderr


def test_stream_controller_owns_single_connection_and_rejects_old_a_b_a_frames() -> None:
    _run_script(r'''
      const h = harness();
      assert.equal(h.controller.reconcile(), true);
      assert.equal(h.controller.reconcile(), false);
      const first = h.streams[0];
      h.select("000001.SZ", 2);
      h.controller.reconcile();
      h.select("600519.SH", 3);
      h.controller.reconcile();
      assert.equal(h.streams.length, 3);
      assert.equal(h.streams.filter(stream => !stream.closed).length, 1);
      const before = h.statuses.length;
      first.onmessage(frame);
      first.listeners["quote-error"]({ data: '{"message":"old"}' });
      first.onerror();
      assert.equal(h.rows.length, 0);
      assert.equal(h.statuses.length, before);
      assert.equal(h.timers.length, 0);
      h.streams[2].onmessage(frame);
      assert.equal(h.rows.length, 1);
      assert.equal(h.statuses.at(-1)[0], "ready");
      const snapshot = h.controller.snapshot();
      assert.equal(Object.isFrozen(snapshot), true);
      assert.equal("stream" in snapshot, false);
      assert.equal("context" in snapshot, false);
      h.controller.dispose();
    ''')


def test_stream_controller_cancels_zero_timer_and_rejects_queued_old_reconnects() -> None:
    _run_script(r'''
      const h = harness();
      h.controller.start();
      h.streams[0].onerror();
      h.streams[0].onerror();
      assert.equal(h.timers.length, 1);
      assert.equal(h.timers[0].id, 0);
      assert.equal(h.controller.snapshot().retryScheduled, true);
      h.controller.stop();
      assert.deepEqual(h.cleared, [0]);
      h.select("000001.SZ", 2);
      h.controller.start();
      h.select("600519.SH", 3);
      h.controller.start();
      const count = h.streams.length;
      h.timers[0].callback();
      assert.equal(h.streams.length, count);
      assert.equal(h.controller.snapshot().retryScheduled, false);
      h.streams.at(-1).onerror();
      assert.equal(h.timers.at(-1).delay, 2000);
      h.timers.at(-1).callback();
      const active = h.streams.at(-1);
      h.timers.at(-1).callback();
      assert.equal(h.streams.at(-1), active);
      h.controller.dispose();
    ''')


def test_stream_controller_aborted_context_drops_reconnect_and_releases_timer() -> None:
    _run_script(r'''
      const h = harness();
      const request = new AbortController();
      h.select("600519.SH", 1, request.signal);
      h.controller.start();
      h.streams[0].onerror();
      request.abort();
      h.timers[0].callback();
      assert.equal(h.streams.length, 1);
      assert.equal(h.controller.snapshot().connected, false);
      assert.equal(h.controller.snapshot().retryScheduled, false);
      h.select("000001.SZ", 2);
      assert.equal(h.controller.reconcile(), true);
      h.controller.dispose();
    ''')


def test_stream_controller_visibility_and_disposal_release_all_resources() -> None:
    _run_script(r'''
      const h = harness();
      h.controller.start();
      h.hide(true);
      h.controller.stop();
      assert.equal(h.controller.start(), false);
      assert.equal(h.controller.reconcile(), false);
      assert.equal(h.streams[0].closed, true);
      h.hide(false);
      h.controller.reconcile();
      const stream = h.streams.at(-1);
      stream.onerror();
      const timer = h.timers.at(-1);
      h.controller.dispose();
      const before = h.statuses.length;
      h.controller.dispose();
      h.controller.stop();
      timer.callback();
      stream.onmessage(frame);
      stream.onerror();
      assert.equal(h.statuses.length, before);
      assert.equal(h.controller.start(), false);
      assert.equal(h.controller.reconcile(), false);
      assert.equal(h.controller.snapshot().disposed, true);
      assert.equal(h.controller.snapshot().connected, false);
      assert.equal(h.controller.snapshot().retryScheduled, false);
      assert.equal(h.streams.filter(value => !value.closed).length, 0);
    ''')


def test_stream_controller_constructor_failure_has_no_active_connection() -> None:
    _run_script(r'''
      const h = harness();
      h.controller.start();
      h.failCreation(true);
      assert.equal(h.controller.start(), false);
      assert.equal(h.streams[0].closed, true);
      assert.equal(h.controller.snapshot().connected, false);
      assert.equal(h.controller.snapshot().subscriptionKey, "");
      assert.equal(h.statuses.at(-1)[0], "error");
      assert.match(h.statuses.at(-1)[1], /constructor failed/);
      h.controller.dispose();
    ''')


def test_stream_contracts_keep_exclusions_capacity_and_atomic_frame_validation() -> None:
    _run_script(r'''
      const excluded = item => item?.research_status === "excluded";
      const items = [
        { symbol: "600519.SH", research_status: "excluded" },
        { symbol: "000001.SZ", research_status: "excluded" },
        { symbol: "600000.SH" }, { symbol: "600000.SH" }, { symbol: "bad&x=1" },
      ];
      assert.deepEqual(quoteStreamSymbols("600519.SH", items, excluded), ["600519.SH", "600000.SH", "300750.SZ", "002594.SZ", "600036.SH"]);
      const many = Array.from({ length: 20 }, (_, index) => ({ symbol: `${600100 + index}.SH` }));
      assert.equal(quoteStreamSymbols("600519.SH", many, excluded).length, 8);
      assert.equal(quoteStreamSymbols("600519.SH", {}, excluded)[0], "600519.SH");
      assert.equal(parseQuoteStreamFrame(frame).rows.length, 1);
      assert.deepEqual(parseQuoteStreamFrame({ data: "[]" }).rows, []);
      for (const data of ["{", "null", "{}", JSON.stringify([...JSON.parse(frame.data), { name: "bad" }])]) {
        const parsed = parseQuoteStreamFrame({ data });
        assert.equal(parsed.rows, null);
        assert.ok(parsed.error);
      }
    ''')


def test_app_pagehide_closes_quote_stream_and_bfcache_can_resume() -> None:
    _run_script(r'''
      const { createAppHarness } = await import("./tests/frontend_app_flow_helpers.mjs");
      const { __appTest: app, streams } = await createAppHarness();
      app.quoteStreamController.start();
      app.handleStockSearchPageHide({ persisted: true });
      assert.equal(streams[0].closed, true);
      assert.equal(app.quoteStreamController.snapshot().disposed, false);
      assert.equal(app.quoteStreamController.start(), true);
      app.handleStockSearchPageHide({ persisted: false });
      assert.equal(streams.at(-1).closed, true);
      assert.equal(app.quoteStreamController.snapshot().disposed, true);
      assert.equal(app.quoteStreamController.start(), false);
    ''')
