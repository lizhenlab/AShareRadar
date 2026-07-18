from __future__ import annotations

import subprocess
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]


def test_chart_workspace_markup_has_complete_accessible_controls() -> None:
    html = (ROOT / "static" / "index.html").read_text(encoding="utf-8")

    assert 'id="chartWorkspace"' in html
    assert 'id="klineCanvas"' in html
    assert 'id="minuteKlineCanvas"' in html
    for value in (20, 60, 120, 240):
        assert f'data-daily-range="{value}"' in html
    for value in ("5m", "15m", "30m", "60m"):
        assert f'data-minute-interval="{value}"' in html
    assert 'id="dailyMa5Toggle" type="checkbox" checked' in html
    assert 'id="dailyMa20Toggle" type="checkbox" checked' in html
    assert html.count('data-chart-view="') == 2
    assert 'data-chart-view="daily" aria-pressed="true"' in html
    assert 'data-chart-view="minute" aria-pressed="false"' in html


def test_daily_ranges_and_overlays_only_redraw_local_state() -> None:
    _run_node_script(
        r'''
import { createAppHarness } from "./tests/frontend_app_flow_helpers.mjs";

const { __appTest } = await createAppHarness({ canvasContext: null });
__appTest.state.symbol = "600519.SH";
__appTest.state.loadSeq = 11;
__appTest.state.lastAnalysis = {
  quote: { code: "600519", market: "SH" },
  klines: [],
};
let requests = 0;
globalThis.fetch = async () => {
  requests += 1;
  throw new Error("daily chart controls must not use the network");
};

for (const range of [20, 120, 240, 60]) {
  if (!__appTest.selectDailyChartRange(range)) throw new Error(`range ${range} was not selected`);
  if (__appTest.state.dailyChartRange !== range) throw new Error(`range ${range} was not retained`);
}
if (__appTest.selectDailyChartRange(60)) throw new Error("reselecting the active daily range reported a change");
__appTest.setDailyChartOverlay("ma5", false);
__appTest.setDailyChartOverlay("ma20", false);
__appTest.redrawResearchCharts();
if (__appTest.state.dailyChartMa5 || __appTest.state.dailyChartMa20) {
  throw new Error("daily overlay state was not retained");
}
if (requests !== 0) throw new Error(`daily chart controls emitted ${requests} requests`);
'''
    )


def test_minute_interval_requests_are_single_flight_and_stale_safe() -> None:
    _run_node_script(
        r'''
import { createAppHarness } from "./tests/frontend_app_flow_helpers.mjs";

const { __appTest, element, jsonResponse, waitFor } = await createAppHarness({ canvasContext: null });
__appTest.state.symbol = "600519.SH";
__appTest.state.loadSeq = 7;
__appTest.state.lastAnalysis = {
  quote: { code: "600519", market: "SH" },
  klines: [],
};

const requests = [];
let resolve15;
let signal15;
globalThis.fetch = (url, options = {}) => {
  const target = String(url);
  requests.push(target);
  const interval = new URL(target, "http://local").searchParams.get("interval");
  if (interval === "15m") {
    signal15 = options.signal;
    return new Promise((resolve) => {
      resolve15 = () => resolve(jsonResponse(minuteReport("15m", "十五分钟旧响应")));
    });
  }
  if (interval === "30m") return Promise.resolve(jsonResponse(minuteReport("30m", "三十分钟当前响应")));
  throw new Error(`unexpected interval request: ${target}`);
};

const staleLoad = __appTest.selectMinuteChartInterval("15m");
await waitFor(() => typeof resolve15 === "function", "15m request");
const currentLoad = __appTest.selectMinuteChartInterval("30m");
if (!(await currentLoad)) throw new Error("current 30m request did not complete");
if (await staleLoad) throw new Error("aborted 15m request reported success");
if (!signal15.aborted) throw new Error("changing minute interval did not abort the previous request");

resolve15();
await Promise.resolve();
await Promise.resolve();
if (__appTest.state.lastMinuteReport.interval !== "30m") {
  throw new Error(`stale response replaced current report: ${__appTest.state.lastMinuteReport.interval}`);
}
if (!element("minuteAnalysis").innerHTML.includes("三十分钟当前响应") || element("minuteAnalysis").innerHTML.includes("十五分钟旧响应")) {
  throw new Error("stale response changed the visible minute analysis");
}
const beforeRepeat = requests.length;
if (await __appTest.selectMinuteChartInterval("30m")) throw new Error("same interval unexpectedly reloaded");
if (requests.length !== beforeRepeat || requests.length !== 2) {
  throw new Error(`minute interval request count changed: ${JSON.stringify(requests)}`);
}

function minuteReport(interval, summary) {
  return {
    symbol: "600519.SH",
    updated_at: "2026-07-15 10:00:00",
    interval,
    source: "test",
    sample_count: 8,
    klines: [],
    availability: "ok",
    availability_reason: "数据可用",
    reason_code: "ok",
    latest_price: 10,
    intraday_change_pct: 0,
    intraday_range_pct: 1,
    volume_pulse: "平稳",
    trend_label: "横盘",
    momentum_label: "中性",
    summary,
    supports: [],
    resistances: [],
    t_plan: {
      low_zone: "--",
      high_zone: "--",
      suitability: "等待",
      style: "观察",
      confidence: 0,
      summary: "等待",
      execution_steps: [],
      stop_conditions: [],
    },
    warnings: [],
    missing_data: [],
  };
}
'''
    )


def test_unavailable_minute_report_clears_canvas_and_ignores_audit_rows() -> None:
    _run_node_script(
        r'''
import { createAppHarness } from "./tests/frontend_app_flow_helpers.mjs";

const calls = [];
const context = {
  clearRect: (...args) => calls.push(["clearRect", ...args]),
  scale: (...args) => calls.push(["scale", ...args]),
  beginPath: () => calls.push(["beginPath"]),
  moveTo: (...args) => calls.push(["moveTo", ...args]),
  lineTo: (...args) => calls.push(["lineTo", ...args]),
  stroke: () => calls.push(["stroke"]),
  fillRect: (...args) => calls.push(["fillRect", ...args]),
  fillText: (...args) => calls.push(["fillText", ...args]),
  measureText: (value) => ({ width: String(value).length * 7 }),
  set fillStyle(value) {},
  get fillStyle() { return ""; },
  set strokeStyle(value) {},
  get strokeStyle() { return ""; },
  set lineWidth(value) {},
  get lineWidth() { return 1; },
  set font(value) {},
  get font() { return ""; },
};
const { __appTest, element, jsonResponse } = await createAppHarness({ canvasContext: context });
__appTest.state.symbol = "600519.SH";
__appTest.state.loadSeq = 17;
__appTest.state.lastAnalysis = { quote: { code: "600519", market: "SH" }, klines: [] };

globalThis.fetch = async (url) => {
  const interval = new URL(String(url), "http://local").searchParams.get("interval");
  return jsonResponse(minuteReport(interval));
};

await __appTest.selectMinuteChartInterval("15m");
if (!calls.some((call) => call[0] === "fillRect")) throw new Error("available minute rows were not drawn");
const beforeUnavailable = calls.length;
await __appTest.selectMinuteChartInterval("30m");
const unavailableCalls = calls.slice(beforeUnavailable);
if (!unavailableCalls.some((call) => call[0] === "clearRect")) throw new Error("unavailable minute report did not clear the canvas");
if (unavailableCalls.some((call) => call[0] === "fillRect")) throw new Error("audit-only rows were drawn as an executable chart");
if (!element("minuteChartStatus").textContent.includes("不可用")) throw new Error("unavailable chart status was not visible");
if (!element("minuteAnalysis").innerHTML.includes("分钟分析不可用")) throw new Error("unavailable analysis detail was not rendered");

function rows(interval) {
  return Array.from({ length: 8 }, (_, index) => ({
    timestamp: `2026-07-15 10:${String(index * 5).padStart(2, "0")}:00`,
    interval,
    source: "test",
    from_cache: false,
    fallback_used: false,
    open: 10 + index * 0.1,
    close: 10.05 + index * 0.1,
    high: 10.1 + index * 0.1,
    low: 9.95 + index * 0.1,
    volume: 1000 + index,
    amount: 10000 + index,
  }));
}

function minuteReport(interval) {
  const unavailable = interval === "30m";
  return {
    symbol: "600519.SH",
    updated_at: "2026-07-15 10:35:00",
    interval,
    source: "test",
    sample_count: 8,
    klines: rows(interval),
    availability: unavailable ? "unavailable" : "ok",
    availability_reason: unavailable ? "有效样本不足，仅保留审计行" : "数据可用",
    reason_code: unavailable ? "insufficient_samples" : "ok",
    latest_price: unavailable ? null : 10.75,
    intraday_change_pct: 0,
    intraday_range_pct: 1,
    volume_pulse: "平稳",
    trend_label: "横盘",
    momentum_label: "中性",
    summary: unavailable ? "不可用" : "可用",
    supports: [],
    resistances: [],
    t_plan: {
      low_zone: "--",
      high_zone: "--",
      suitability: "等待",
      style: "观察",
      confidence: 0,
      summary: "等待",
      execution_steps: [],
      stop_conditions: [],
    },
    warnings: [],
    missing_data: unavailable ? ["有效分钟样本"] : [],
  };
}
'''
    )


def test_empty_minute_responses_leave_an_explicit_unavailable_state() -> None:
    _run_node_script(
        r'''
import { createAppHarness } from "./tests/frontend_app_flow_helpers.mjs";

const { __appTest, element } = await createAppHarness({ canvasContext: null });
__appTest.state.symbol = "600519.SH";
__appTest.state.loadSeq = 29;
__appTest.state.lastAnalysis = { quote: { code: "600519", market: "SH" }, klines: [] };

const responses = [
  { ok: true, status: 204, headers: { get: () => null }, async text() { return ""; } },
  { ok: true, status: 200, headers: { get: () => "0" }, async text() { return ""; } },
  { ok: true, status: 200, async json() { return null; } },
];
for (const response of responses) {
  globalThis.fetch = async () => response;
  if (await __appTest.loadMinuteAnalysis()) throw new Error("empty minute response reported success");
  if (__appTest.state.lastMinuteReport !== null || __appTest.state.lastMinuteSymbol !== "") {
    throw new Error("empty minute response polluted current minute state");
  }
  if (!element("minuteAnalysis").innerHTML.includes("分钟分析暂不可用")) {
    throw new Error(`empty minute response left a loading panel: ${element("minuteAnalysis").innerHTML}`);
  }
  if (!element("minuteChartStatus").textContent.includes("不可用")) {
    throw new Error("empty minute response did not mark the chart unavailable");
  }
}
'''
    )


def _run_node_script(script: str) -> None:
    subprocess.run(
        ["node", "--input-type=module", "-e", script],
        cwd=ROOT,
        check=True,
        text=True,
        capture_output=True,
    )
