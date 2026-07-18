from __future__ import annotations

import subprocess
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]

CHART_INSPECTOR_HARNESS = r'''
globalThis.window = { devicePixelRatio: 2 };

function makeCanvas({
  clientWidth = 640,
  clientHeight = 320,
  rect = { left: 0, top: 0, width: clientWidth, height: clientHeight },
} = {}) {
  const calls = [];
  const listeners = new Map();
  const attributes = new Map();
  const ctx = {
    scale: (...args) => calls.push(["scale", ...args]),
    clearRect: (...args) => calls.push(["clearRect", ...args]),
    beginPath: () => calls.push(["beginPath"]),
    moveTo: (...args) => calls.push(["moveTo", ...args]),
    lineTo: (...args) => calls.push(["lineTo", ...args]),
    stroke: () => calls.push(["stroke"]),
    fillRect: (...args) => calls.push(["fillRect", ...args]),
    arc: (...args) => calls.push(["arc", ...args]),
    fill: () => calls.push(["fill"]),
    fillText: (...args) => calls.push(["fillText", ...args]),
    measureText: (value) => ({ width: String(value).length * 7 }),
    set fillStyle(value) { calls.push(["fillStyle", value]); },
    get fillStyle() { return ""; },
    set strokeStyle(value) { calls.push(["strokeStyle", value]); },
    get strokeStyle() { return ""; },
    set lineWidth(value) { calls.push(["lineWidth", value]); },
    get lineWidth() { return 1; },
    set font(value) { calls.push(["font", value]); },
    get font() { return ""; },
  };
  const canvas = {
    clientWidth,
    clientHeight,
    width: 0,
    height: 0,
    getContext(type) {
      if (type !== "2d") throw new Error(`unexpected context type: ${type}`);
      return ctx;
    },
    getBoundingClientRect() {
      return { ...rect };
    },
    addEventListener(type, listener) {
      if (!listeners.has(type)) listeners.set(type, new Set());
      listeners.get(type).add(listener);
    },
    removeEventListener(type, listener) {
      listeners.get(type)?.delete(listener);
    },
    dispatch(type, event = {}) {
      for (const listener of [...(listeners.get(type) || [])]) listener(event);
      return event;
    },
    listenerCount(type) {
      return listeners.get(type)?.size || 0;
    },
    hasAttribute(name) {
      return attributes.has(name);
    },
    setAttribute(name, value) {
      attributes.set(name, String(value));
    },
    getAttribute(name) {
      return attributes.get(name) ?? null;
    },
    removeAttribute(name) {
      attributes.delete(name);
    },
  };
  return { canvas, calls, rect };
}

function makeDailyRows(count) {
  const start = Date.UTC(2026, 6, 1);
  return Array.from({ length: count }, (_, index) => {
    const open = 100 + index;
    return {
      date: new Date(start + index * 86400000).toISOString().slice(0, 10),
      open,
      high: open + 1,
      low: open - 1,
      close: open + 0.25,
      volume: String(1000 + index),
    };
  });
}

function makeMinuteRows(count) {
  return Array.from({ length: count }, (_, index) => {
    const minuteOfDay = 9 * 60 + 30 + index * 5;
    const hour = String(Math.floor(minuteOfDay / 60)).padStart(2, "0");
    const minute = String(minuteOfDay % 60).padStart(2, "0");
    const open = 50 + index;
    return {
      timestamp: `2026-07-15 ${hour}:${minute}:00`,
      open,
      high: open + 0.8,
      low: open - 0.8,
      close: open + 0.2,
      volume: 2000 + index,
    };
  });
}

function keyEvent(key) {
  return {
    key,
    defaultPrevented: false,
    preventDefault() {
      this.defaultPrevented = true;
    },
  };
}

function assertClose(actual, expected, label) {
  if (Math.abs(actual - expected) > 1e-9) {
    throw new Error(`${label}: expected ${expected}, received ${actual}`);
  }
}
'''


def test_draw_result_exposes_frozen_daily_and_minute_inspection_snapshots() -> None:
    _run_chart_inspector_script(
        r'''
const dailyRows = makeDailyRows(25);
const dirtyRow = { ...dailyRows[21], high: Infinity };
const inputRows = [...dailyRows.slice(0, 21), dirtyRow, ...dailyRows.slice(21)];
inputRows.at(-1).volume = Infinity;
const dailyHarness = makeCanvas();
const dailyResult = drawKlineChart({
  canvas: dailyHarness.canvas,
  rows: inputRows,
  rowLimit: 6,
  showMarks: false,
});

if (!dailyResult.drawn || dailyResult.rowCount !== 6 || dailyResult.validRowCount !== 25) {
  throw new Error(`legacy row metadata changed: ${JSON.stringify(dailyResult)}`);
}
if (dailyHarness.calls.filter((call) => call[0] === "fillRect").length !== 6) {
  throw new Error("snapshot work changed candle drawing");
}
const snapshot = dailyResult.inspection;
if (!snapshot || snapshot.width !== 640 || snapshot.height !== 320) {
  throw new Error(`missing daily inspection snapshot: ${JSON.stringify(snapshot)}`);
}
if (
  !Object.isFrozen(snapshot)
  || !Object.isFrozen(snapshot.bounds)
  || !Object.isFrozen(snapshot.rows)
  || !Object.isFrozen(snapshot.rows[0])
  || !Object.isFrozen(snapshot.ma5)
  || !Object.isFrozen(snapshot.ma20)
) throw new Error("inspection snapshot is mutable");
if (snapshot.rows.length !== 6 || snapshot.ma5.length !== 6 || snapshot.ma20.length !== 6) {
  throw new Error("inspection arrays are not aligned");
}
if (snapshot.rows[0].eventTime !== dailyRows[19].date || snapshot.rows.at(-1).eventTime !== dailyRows[24].date) {
  throw new Error(`filtered daily window is wrong: ${snapshot.rows.map((row) => row.eventTime)}`);
}
const expectedMa5 = dailyRows.slice(15, 20).reduce((sum, row) => sum + row.close, 0) / 5;
const expectedMa20 = dailyRows.slice(0, 20).reduce((sum, row) => sum + row.close, 0) / 20;
assertClose(snapshot.ma5[0], expectedMa5, "first visible MA5");
assertClose(snapshot.ma20[0], expectedMa20, "first visible MA20");
if (snapshot.rows.at(-1).volume !== 0) throw new Error("invalid volume was not made finite");
const retainedClose = snapshot.rows.at(-1).close;
inputRows.at(-1).close = 9999;
if (snapshot.rows.at(-1).close !== retainedClose) throw new Error("snapshot retained a live input row");

const minuteRows = makeMinuteRows(4);
const minuteHarness = makeCanvas();
const minuteResult = drawKlineChart({
  canvas: minuteHarness.canvas,
  rows: minuteRows,
  showMarks: false,
  showMa5: false,
  showMa20: false,
});
const minuteSnapshot = minuteResult.inspection;
if (!minuteSnapshot || minuteSnapshot.rows[2].eventTime !== minuteRows[2].timestamp) {
  throw new Error("minute event time was not retained");
}
if (minuteSnapshot.ma5.some((value) => value !== null) || minuteSnapshot.ma20.some((value) => value !== null)) {
  throw new Error("moving-average warmup placeholders lost alignment");
}

const emptyResult = drawKlineChart({ canvas: makeCanvas().canvas, rows: [] });
const failedResult = drawKlineChart({ rows: dailyRows });
if (emptyResult.inspection !== null || failedResult.inspection !== null) {
  throw new Error("empty or failed draws exposed a stale snapshot");
}
'''
    )


def test_chart_inspection_at_filters_indices_and_enforces_plot_boundaries() -> None:
    _run_chart_inspector_script(
        r'''
const rows = makeDailyRows(7);
const inputRows = [rows[0], { ...rows[1], low: 0 }, ...rows.slice(1)];
const result = drawKlineChart({ canvas: makeCanvas().canvas, rows: inputRows, showMarks: false });
const snapshot = result.inspection;
const { left, right } = snapshot.bounds;

if (chartInspectionAt(snapshot, left - 0.001) !== null) throw new Error("left overflow was accepted");
if (chartInspectionAt(snapshot, right + 0.001) !== null) throw new Error("right overflow was accepted");
if (chartInspectionAt(snapshot, NaN) !== null) throw new Error("non-finite x was accepted");

const first = chartInspectionAt(snapshot, left);
const last = chartInspectionAt(snapshot, right);
if (!first || first.index !== 0 || first.item.eventTime !== rows[0].date) {
  throw new Error(`left boundary did not select the first valid row: ${JSON.stringify(first)}`);
}
if (!last || last.index !== rows.length - 1 || last.item.eventTime !== rows.at(-1).date) {
  throw new Error(`right boundary did not select the last valid row: ${JSON.stringify(last)}`);
}
const secondCenter = left + snapshot.xStep * 1.5;
const second = chartInspectionAt(snapshot, secondCenter);
if (!second || second.index !== 1 || second.item.eventTime !== rows[1].date) {
  throw new Error(`invalid source row shifted inspection indices: ${JSON.stringify(second)}`);
}
assertClose(second.x, secondCenter, "candle center");
for (const [name, value] of Object.entries(second.item)) {
  if (typeof value === "number" && !Number.isFinite(value)) {
    throw new Error(`item.${name} is not finite`);
  }
}
if (!("ma5" in second.item) || !("ma20" in second.item) || !("volume" in second.item)) {
  throw new Error("inspection item is missing required fields");
}
'''
    )


def test_pointer_inspection_maps_css_pixels_and_pointerleave_clears_state() -> None:
    _run_chart_inspector_script(
        r'''
const harness = makeCanvas({
  clientWidth: 640,
  clientHeight: 320,
  rect: { left: 100, top: 50, width: 320, height: 160 },
});
const result = drawKlineChart({ canvas: harness.canvas, rows: makeMinuteRows(4), showMarks: false });
if (harness.canvas.width !== 1280 || harness.canvas.height !== 640) {
  throw new Error("DPR setup did not create a backing-store ratio");
}
const snapshot = result.inspection;
const states = [];
const drawCallCount = harness.calls.length;
const inspector = createChartInspector({
  canvas: harness.canvas,
  getSnapshot: () => snapshot,
  onState: (state) => states.push(state),
});
if (harness.canvas.getAttribute("tabindex") !== "0") throw new Error("canvas was not keyboard reachable");

const localX = snapshot.bounds.left + snapshot.xStep * 2.5;
const localY = (snapshot.bounds.top + snapshot.bounds.bottom) / 2;
harness.canvas.dispatch("pointermove", {
  clientX: harness.rect.left + localX * harness.rect.width / snapshot.width,
  clientY: harness.rect.top + localY * harness.rect.height / snapshot.height,
});
const active = states.at(-1);
if (active.phase !== "active" || active.index !== 2 || active.item.eventTime !== makeMinuteRows(4)[2].timestamp) {
  throw new Error(`CSS coordinate mapping selected the wrong candle: ${JSON.stringify(active)}`);
}
assertClose(active.x, localX, "pointer candle center");
if (harness.calls.length !== drawCallCount) throw new Error("inspector drew on the canvas");

const outsideY = snapshot.bounds.top - 0.01;
harness.canvas.dispatch("pointermove", {
  clientX: harness.rect.left + localX * harness.rect.width / snapshot.width,
  clientY: harness.rect.top + outsideY * harness.rect.height / snapshot.height,
});
if (states.at(-1).phase !== "idle") throw new Error("pointer escaped the vertical plot boundary");

harness.canvas.dispatch("pointermove", {
  clientX: harness.rect.left + localX * harness.rect.width / snapshot.width,
  clientY: harness.rect.top + localY * harness.rect.height / snapshot.height,
});
harness.canvas.dispatch("pointerleave");
if (states.at(-1).phase !== "idle") throw new Error("pointerleave did not clear inspection");

harness.canvas.dispatch("pointermove", {
  clientX: harness.rect.left + localX * harness.rect.width / snapshot.width,
  clientY: harness.rect.top + localY * harness.rect.height / snapshot.height,
});
harness.canvas.dispatch("pointerleave", { pointerType: "touch" });
if (states.at(-1).phase !== "active") throw new Error("touch pointerleave cleared a tapped inspection");
harness.canvas.dispatch("pointercancel", { pointerType: "touch" });
if (states.at(-1).phase !== "idle") throw new Error("touch pointercancel did not clear inspection");
inspector.destroy();
'''
    )


def test_keyboard_uses_latest_snapshot_clamps_after_redraw_and_destroy_detaches() -> None:
    _run_chart_inspector_script(
        r'''
const harness = makeCanvas();
let latest = drawKlineChart({ canvas: harness.canvas, rows: makeDailyRows(8), showMarks: false }).inspection;
const states = [];
const inspector = createChartInspector({
  canvas: harness.canvas,
  getSnapshot: () => latest,
  onState: (state) => states.push(state),
});

let event = harness.canvas.dispatch("keydown", keyEvent("ArrowRight"));
if (!event.defaultPrevented || states.at(-1).index !== 0) throw new Error("ArrowRight did not start at first row");
event = harness.canvas.dispatch("keydown", keyEvent("ArrowRight"));
if (!event.defaultPrevented || states.at(-1).index !== 1) throw new Error("ArrowRight did not advance");
event = harness.canvas.dispatch("keydown", keyEvent("End"));
if (!event.defaultPrevented || states.at(-1).index !== 7) throw new Error("End did not select last row");

const shortRows = makeMinuteRows(2);
latest = drawKlineChart({ canvas: harness.canvas, rows: shortRows, showMarks: false }).inspection;
event = harness.canvas.dispatch("keydown", keyEvent("ArrowRight"));
if (
  !event.defaultPrevented
  || states.at(-1).index !== 1
  || states.at(-1).item.eventTime !== shortRows[1].timestamp
) throw new Error(`short redraw reused an old point: ${JSON.stringify(states.at(-1))}`);

event = harness.canvas.dispatch("keydown", keyEvent("ArrowLeft"));
if (!event.defaultPrevented || states.at(-1).index !== 0) throw new Error("ArrowLeft did not move left");
event = harness.canvas.dispatch("keydown", keyEvent("Home"));
if (!event.defaultPrevented || states.at(-1).index !== 0) throw new Error("Home did not select first row");
event = harness.canvas.dispatch("keydown", keyEvent("End"));
if (!event.defaultPrevented || states.at(-1).index !== 1) throw new Error("End did not select latest last row");
event = harness.canvas.dispatch("keydown", keyEvent("Escape"));
if (!event.defaultPrevented || states.at(-1).phase !== "idle") throw new Error("Escape did not clear state");

const stateCount = states.length;
event = harness.canvas.dispatch("keydown", keyEvent("x"));
if (event.defaultPrevented || states.length !== stateCount) throw new Error("unhandled key was consumed");
latest = null;
event = harness.canvas.dispatch("keydown", keyEvent("ArrowRight"));
if (event.defaultPrevented || states.at(-1).phase !== "idle") {
  throw new Error("keyboard consumed an event without a snapshot");
}

const callbackCount = states.length;
inspector.destroy();
inspector.destroy();
if (
  harness.canvas.listenerCount("pointermove")
  || harness.canvas.listenerCount("pointerdown")
  || harness.canvas.listenerCount("pointerleave")
  || harness.canvas.listenerCount("pointercancel")
  || harness.canvas.listenerCount("keydown")
  || harness.canvas.listenerCount("blur")
) {
  throw new Error("destroy left event listeners attached");
}
if (harness.canvas.hasAttribute("tabindex")) throw new Error("destroy did not restore tabindex");
harness.canvas.dispatch("pointerleave");
harness.canvas.dispatch("keydown", keyEvent("End"));
if (states.length !== callbackCount) throw new Error("destroyed inspector invoked onState");
'''
    )


def _run_chart_inspector_script(assertions: str) -> None:
    source = (
        'import { drawKlineChart } from "./static/js/chart.js";\n'
        'import { chartInspectionAt, createChartInspector } '
        'from "./static/js/chart-inspector.js";\n'
        + CHART_INSPECTOR_HARNESS
        + assertions
    )
    subprocess.run(["node", "--input-type=module", "-e", source], cwd=ROOT, check=True)
