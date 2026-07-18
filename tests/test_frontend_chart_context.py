from __future__ import annotations

import subprocess
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]

CHART_HARNESS = r'''
globalThis.window = { devicePixelRatio: 1 };

function makeCanvas(clientWidth = 640, clientHeight = 320) {
  const calls = [];
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
  };
  return { canvas, calls };
}

function makeDailyRows(count) {
  const start = Date.UTC(2026, 0, 1);
  return Array.from({ length: count }, (_, index) => {
    const date = new Date(start + index * 86400000).toISOString().slice(0, 10);
    const open = 100 + index * 0.1;
    return {
      date,
      open,
      close: open + 0.2,
      high: open + 0.5,
      low: open - 0.5,
    };
  });
}

function makeMinuteRows(count) {
  return Array.from({ length: count }, (_, index) => {
    const minuteOfDay = 9 * 60 + 30 + index * 5;
    const hour = String(Math.floor(minuteOfDay / 60)).padStart(2, "0");
    const minute = String(minuteOfDay % 60).padStart(2, "0");
    const open = 100 + index * 0.1;
    return {
      timestamp: `2026-07-15 ${hour}:${minute}:00`,
      open,
      close: open + 0.2,
      high: open + 0.5,
      low: open - 0.5,
    };
  });
}

function callsNamed(calls, name) {
  return calls.filter((call) => call[0] === name);
}
'''


def test_chart_range_clipping_supports_default_row_limit_and_max_rows_alias() -> None:
    _run_chart_script(
        r'''
const rows = makeDailyRows(260);

const defaultHarness = makeCanvas();
const defaultResult = drawKlineChart({ canvas: defaultHarness.canvas, rows });
if (!defaultResult.drawn || defaultResult.rowCount !== 60 || defaultResult.rowLimit !== 60) {
  throw new Error(`default range contract changed: ${JSON.stringify(defaultResult)}`);
}
if (callsNamed(defaultHarness.calls, "fillRect").length !== 60) {
  throw new Error("default chart did not draw exactly the latest 60 valid rows");
}
if (defaultResult.startLabel !== rows[200].date.slice(5)) {
  throw new Error(`default chart did not retain the latest rows: ${defaultResult.startLabel}`);
}

const rowLimitHarness = makeCanvas();
const rowLimitResult = drawKlineChart({ canvas: rowLimitHarness.canvas, rows, rowLimit: 20 });
if (rowLimitResult.rowCount !== 20 || callsNamed(rowLimitHarness.calls, "fillRect").length !== 20) {
  throw new Error(`rowLimit was not applied: ${JSON.stringify(rowLimitResult)}`);
}

const maxRowsHarness = makeCanvas();
const maxRowsResult = drawKlineChart({ canvas: maxRowsHarness.canvas, rows, maxRows: 120 });
if (maxRowsResult.rowCount !== 120 || callsNamed(maxRowsHarness.calls, "fillRect").length !== 120) {
  throw new Error(`maxRows alias was not applied: ${JSON.stringify(maxRowsResult)}`);
}
'''
    )


def test_chart_accepts_minute_timestamps_and_can_disable_marks_and_moving_averages() -> None:
    _run_chart_script(
        r'''
const harness = makeCanvas();
const result = drawKlineChart({
  canvas: harness.canvas,
  rows: makeMinuteRows(25),
  marks: [{ category: "买点", date: "2026-07-15", label: "不应绘制", visible: true }],
  activeCategories: new Set(["买点"]),
  showMarks: false,
  showMa5: false,
  showMa20: false,
});

if (!result.drawn || result.rowCount !== 25) {
  throw new Error(`minute rows were not drawn: ${JSON.stringify(result)}`);
}
if (result.startLabel !== "07-15 09:30" || result.endLabel !== "07-15 11:30") {
  throw new Error(`minute endpoint labels were unreasonable: ${result.startLabel}, ${result.endLabel}`);
}
if (result.markCount !== 0 || result.ma5Drawn || result.ma20Drawn) {
  throw new Error(`disabled overlays leaked into metadata: ${JSON.stringify(result)}`);
}
if (callsNamed(harness.calls, "arc").length !== 0) throw new Error("disabled marks were drawn");
if (harness.calls.some((call) => call[0] === "lineWidth" && call[1] === 1.6)) {
  throw new Error("disabled moving averages were drawn");
}
'''
    )


def test_chart_candles_do_not_overlap_at_supported_ranges_and_canvas_size_is_stable() -> None:
    _run_chart_script(
        r'''
globalThis.window.devicePixelRatio = 2;
const harness = makeCanvas(640, 320);
const rows = makeDailyRows(240);

for (const rowLimit of [20, 60, 120, 240]) {
  const callStart = harness.calls.length;
  const result = drawKlineChart({
    canvas: harness.canvas,
    rows,
    rowLimit,
    showMarks: false,
    showMa5: false,
    showMa20: false,
  });
  const drawCalls = harness.calls.slice(callStart);
  const candles = callsNamed(drawCalls, "fillRect");
  if (!result.drawn || result.rowCount !== rowLimit || candles.length !== rowLimit) {
    throw new Error(`range ${rowLimit} did not produce a non-empty candle draw: ${JSON.stringify(result)}`);
  }
  if (!(result.candleWidth > 0 && result.candleWidth < result.xStep)) {
    throw new Error(`range ${rowLimit} has overlapping width metadata: ${JSON.stringify(result)}`);
  }
  for (let index = 1; index < candles.length; index += 1) {
    const previousRight = candles[index - 1][1] + candles[index - 1][3];
    const currentLeft = candles[index][1];
    if (previousRight > currentLeft + 1e-9) {
      throw new Error(`range ${rowLimit} overlaps candles ${index - 1} and ${index}`);
    }
  }
  if (harness.canvas.width !== 1280 || harness.canvas.height !== 640) {
    throw new Error(`dynamic row count changed canvas dimensions at ${rowLimit}`);
  }
}
'''
    )


def test_chart_moving_averages_use_history_before_the_visible_window() -> None:
    _run_chart_script(
        r'''
const rows = makeDailyRows(240);
const expectedPointCounts = new Map([
  [20, { ma5: 20, ma20: 20 }],
  [60, { ma5: 60, ma20: 60 }],
  [120, { ma5: 120, ma20: 120 }],
  [240, { ma5: 236, ma20: 221 }],
]);

for (const [rowLimit, expected] of expectedPointCounts) {
  const harness = makeCanvas();
  const result = drawKlineChart({ canvas: harness.canvas, rows, rowLimit, showMarks: false });
  if (result.rowCount !== rowLimit || result.startLabel !== rows[240 - rowLimit].date.slice(5)) {
    throw new Error(`moving-average context changed the visible range: ${JSON.stringify(result)}`);
  }
  if (result.ma5PointCount !== expected.ma5 || result.ma20PointCount !== expected.ma20) {
    throw new Error(`range ${rowLimit} has incorrect MA coverage: ${JSON.stringify(result)}`);
  }
  if (!result.ma5Drawn || !result.ma20Drawn) {
    throw new Error(`range ${rowLimit} did not draw its available MA paths`);
  }

  if (rowLimit === 20) {
    const styleIndex = harness.calls.findIndex(
      (call) => call[0] === "strokeStyle" && call[1] === "#b7791f",
    );
    const strokeOffset = harness.calls.slice(styleIndex).findIndex((call) => call[0] === "stroke");
    const pathCalls = harness.calls.slice(styleIndex, styleIndex + strokeOffset + 1);
    const firstMove = pathCalls.find((call) => call[0] === "moveTo");
    const firstCandle = callsNamed(harness.calls, "fillRect")[0];
    const firstCandleCenter = firstCandle[1] + firstCandle[3] / 2;
    if (!firstMove || Math.abs(firstMove[1] - firstCandleCenter) > 1e-9) {
      throw new Error("MA20 did not begin on the first visible 20-day candle");
    }
    if (callsNamed(pathCalls, "lineTo").length !== 19) {
      throw new Error("20-day view did not draw all 20 warmed-up MA20 points");
    }
  }
}
'''
    )


def test_chart_moving_average_toggles_and_scale_use_only_drawn_visible_values() -> None:
    _run_chart_script(
        r'''
const rows = makeDailyRows(40).map((row, index) => {
  const close = index < 20 ? 1000 : 10;
  return { ...row, open: close, close, high: close + 1, low: close - 1 };
});

const ma20Harness = makeCanvas();
const ma20Result = drawKlineChart({
  canvas: ma20Harness.canvas,
  rows,
  rowLimit: 20,
  ma5: 88888,
  ma20: 99999,
  showMa5: false,
  showMa20: true,
  showMarks: false,
});
const expectedMa20Max = (19 * 1000 + 10) / 20;
if (ma20Result.ma5PointCount !== 0 || ma20Result.ma5Drawn) {
  throw new Error(`disabled MA5 leaked into metadata: ${JSON.stringify(ma20Result)}`);
}
if (ma20Result.ma20PointCount !== 20 || !ma20Result.ma20Drawn) {
  throw new Error(`enabled MA20 did not use warm-up history: ${JSON.stringify(ma20Result)}`);
}
if (Math.abs(ma20Result.maxPrice - expectedMa20Max) > 1e-9 || ma20Result.minPrice !== 9) {
  throw new Error(`MA20 scale used a scalar or missed visible values: ${JSON.stringify(ma20Result)}`);
}
if (ma20Harness.calls.some((call) => call[0] === "strokeStyle" && call[1] === "#2563eb")) {
  throw new Error("disabled MA5 path was drawn");
}

const ma5Harness = makeCanvas();
const ma5Result = drawKlineChart({
  canvas: ma5Harness.canvas,
  rows,
  rowLimit: 20,
  showMa5: true,
  showMa20: false,
  showMarks: false,
});
const expectedMa5Max = (4 * 1000 + 10) / 5;
if (ma5Result.ma5PointCount !== 20 || ma5Result.ma20PointCount !== 0) {
  throw new Error(`individual MA toggles returned wrong point counts: ${JSON.stringify(ma5Result)}`);
}
if (Math.abs(ma5Result.maxPrice - expectedMa5Max) > 1e-9) {
  throw new Error(`MA5 scale missed a visible warm-up value: ${JSON.stringify(ma5Result)}`);
}

const candleOnlyHarness = makeCanvas();
const candleOnlyResult = drawKlineChart({
  canvas: candleOnlyHarness.canvas,
  rows,
  rowLimit: 20,
  ma5: 88888,
  ma20: 99999,
  showMa5: false,
  showMa20: false,
  showMarks: false,
});
if (candleOnlyResult.maxPrice !== 11 || candleOnlyResult.minPrice !== 9) {
  throw new Error(`hidden or legacy MA values changed candle scale: ${JSON.stringify(candleOnlyResult)}`);
}
'''
    )


def test_chart_moving_average_warmup_ignores_dirty_historical_rows() -> None:
    _run_chart_script(
        r'''
const rows = makeDailyRows(241);
rows[220] = { ...rows[220], close: Infinity };
const harness = makeCanvas();
const result = drawKlineChart({
  canvas: harness.canvas,
  rows,
  rowLimit: 20,
  showMarks: false,
});

if (result.validRowCount !== 240 || result.rowCount !== 20) {
  throw new Error(`dirty warm-up row changed visible counts: ${JSON.stringify(result)}`);
}
if (result.startLabel !== rows[221].date.slice(5)) {
  throw new Error(`dirty warm-up row shifted the visible start: ${JSON.stringify(result)}`);
}
if (result.ma5PointCount !== 20 || result.ma20PointCount !== 20) {
  throw new Error(`dirty warm-up row broke an MA path: ${JSON.stringify(result)}`);
}
for (const call of harness.calls) {
  for (const argument of call.slice(1)) {
    if (typeof argument === "number" && !Number.isFinite(argument)) {
      throw new Error(`dirty warm-up row produced ${call[0]}(${String(argument)})`);
    }
  }
}
'''
    )


def test_chart_clears_canvas_and_returns_metadata_for_empty_or_dirty_rows() -> None:
    _run_chart_script(
        r'''
const harness = makeCanvas();
const firstResult = drawKlineChart({ canvas: harness.canvas, rows: makeDailyRows(5) });
if (!firstResult.drawn) throw new Error("valid setup draw failed");

const callStart = harness.calls.length;
const emptyResult = drawKlineChart({
  canvas: harness.canvas,
  rows: [
    { date: "2026-07-01", open: 0, close: 1, high: 2, low: 0.5 },
    { date: "2026-07-02", open: 1, close: 2, high: Infinity, low: 0.5 },
    { date: "2026-07-03", open: 1, close: 2, high: 1.5, low: 2.5 },
    null,
  ],
});
const emptyCalls = harness.calls.slice(callStart);
if (emptyResult.drawn || emptyResult.rowCount !== 0 || emptyResult.validRowCount !== 0) {
  throw new Error(`dirty data returned incorrect metadata: ${JSON.stringify(emptyResult)}`);
}
if (emptyResult.reason !== "empty-data") throw new Error(`unexpected empty reason: ${emptyResult.reason}`);
if (callsNamed(emptyCalls, "clearRect").length !== 1) throw new Error("empty redraw did not clear the canvas");
if (callsNamed(emptyCalls, "fillRect").length !== 0) throw new Error("dirty rows leaked candles after clear");
'''
    )


def test_chart_marks_only_match_dates_present_in_visible_daily_rows() -> None:
    _run_chart_script(
        r'''
const rows = [
  { date: "2026-05-01", open: 100, close: 101, high: 102, low: 99 },
  { date: "2026-05-03", open: 101, close: 102, high: 103, low: 100 },
];
const marks = [
  { category: "买点", kline_date: "2026-05-02", label: "缺失日期", visible: true },
  { category: "买点", kline_date: "2026/05/03", label: "精确命中", visible: true },
  { category: "买点", kline_date: "2026-05-32", label: "非法日期", visible: true },
];
const harness = makeCanvas();
const result = drawKlineChart({
  canvas: harness.canvas,
  rows,
  marks,
  activeCategories: ["买点"],
});
const labels = callsNamed(harness.calls, "fillText").map((call) => String(call[1]));
if (result.markCount !== 1 || callsNamed(harness.calls, "arc").length !== 1) {
  throw new Error(`marks snapped to a missing candle: ${JSON.stringify(result)}`);
}
if (!labels.includes("精确命中") || labels.includes("缺失日期") || labels.includes("非法日期")) {
  throw new Error(`mark labels were not exact-date filtered: ${labels.join(",")}`);
}

const disabledHarness = makeCanvas();
const disabledResult = drawKlineChart({
  canvas: disabledHarness.canvas,
  rows,
  marks: [marks[1]],
  activeCategories: ["买点"],
  showMarks: false,
});
if (disabledResult.markCount !== 0 || callsNamed(disabledHarness.calls, "arc").length !== 0) {
  throw new Error("showMarks=false did not suppress an exact mark");
}
'''
    )


def test_chart_marks_filter_to_visible_dates_before_applying_the_stable_limit() -> None:
    _run_chart_script(
        r'''
const rows = makeDailyRows(20);
const outsideMarks = Array.from({ length: 18 }, (_, index) => ({
  category: "买点",
  kline_date: `2025-12-${String(index + 1).padStart(2, "0")}`,
  label: `old${String(index).padStart(2, "0")}`,
  visible: true,
}));
const harness = makeCanvas();
const result = drawKlineChart({
  canvas: harness.canvas,
  rows,
  marks: [
    ...outsideMarks,
    { category: "买点", kline_date: rows[5].date, label: "inside", visible: true },
  ],
  activeCategories: ["买点"],
  showMa5: false,
  showMa20: false,
});
const labels = callsNamed(harness.calls, "fillText").map((call) => String(call[1]));
if (result.markCount !== 1 || callsNamed(harness.calls, "arc").length !== 1 || !labels.includes("inside")) {
  throw new Error(`off-window marks consumed the visible limit: ${JSON.stringify(result)}`);
}

const sameDayMarks = Array.from({ length: 22 }, (_, index) => ({
  category: "买点",
  kline_date: rows[8].date,
  label: `m${String(index).padStart(2, "0")}`,
  visible: true,
}));
const cappedHarness = makeCanvas();
const cappedResult = drawKlineChart({
  canvas: cappedHarness.canvas,
  rows,
  marks: sameDayMarks,
  activeCategories: ["买点"],
  showMa5: false,
  showMa20: false,
});
const drawnMarkLabels = callsNamed(cappedHarness.calls, "fillText")
  .map((call) => String(call[1]))
  .filter((label) => /^m\d{2}$/.test(label));
if (cappedResult.markCount !== 18 || callsNamed(cappedHarness.calls, "arc").length !== 18) {
  throw new Error(`same-day mark limit was not enforced: ${JSON.stringify(cappedResult)}`);
}
const expectedLabels = sameDayMarks.slice(0, drawnMarkLabels.length).map((mark) => mark.label);
if (!drawnMarkLabels.length || JSON.stringify(drawnMarkLabels) !== JSON.stringify(expectedLabels)) {
  throw new Error(`same-day marks were not capped in stable order: ${drawnMarkLabels.join(",")}`);
}
'''
    )


def test_chart_mark_labels_flip_at_right_edge_and_avoid_vertical_overlap() -> None:
    _run_chart_script(
        r'''
const rows = makeDailyRows(20);
const labels = ["边缘一", "边缘二", "边缘三"];
const harness = makeCanvas(360, 260);
const result = drawKlineChart({
  canvas: harness.canvas,
  rows,
  marks: labels.map((label) => ({
    category: "买点",
    kline_date: rows.at(-1).date,
    label,
    price: rows.at(-1).close,
    visible: true,
  })),
  activeCategories: ["买点"],
  showMa5: false,
  showMa20: false,
});
const calls = callsNamed(harness.calls, "fillText").filter((call) => labels.includes(String(call[1])));
if (result.markCount !== 3 || calls.length !== 3) {
  throw new Error(`edge marks were not fully labeled: ${JSON.stringify({ result, calls })}`);
}
for (const call of calls) {
  const width = String(call[1]).length * 7;
  if (call[2] < 46 || call[2] + width > 344) {
    throw new Error(`mark label escaped chart bounds: ${JSON.stringify(call)}`);
  }
}
const baselines = calls.map((call) => Number(call[3])).sort((left, right) => left - right);
if (baselines.some((value, index) => index > 0 && value - baselines[index - 1] < 14)) {
  throw new Error(`mark labels overlapped vertically: ${baselines.join(",")}`);
}
'''
    )


def _run_chart_script(assertions: str) -> None:
    source = 'import { drawKlineChart } from "./static/js/chart.js";\n' + CHART_HARNESS + assertions
    subprocess.run(["node", "--input-type=module", "-e", source], cwd=ROOT, check=True)
