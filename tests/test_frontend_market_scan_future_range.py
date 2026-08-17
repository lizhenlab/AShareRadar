from __future__ import annotations

import subprocess
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]


def test_future_range_view_renders_ready_insufficient_and_legacy_states_without_fake_values() -> None:
    _run_node_script(
        r'''
import assert from "node:assert/strict";
import {
  normalizeMarketScanFutureRangeResponse,
  renderMarketScanFutureRange,
} from "./static/js/market-scan-future-range-view.js";

const elements = viewElements();
const payload = normalizeMarketScanFutureRangeResponse(readyResponse(1), 29);
renderMarketScanFutureRange(elements, payload, { offset: 1, path: "specified_day", group: "top100", keyword: "" });
assert.equal(elements.evidenceStatus.textContent, "冻结证据可用");
assert.equal(elements.evidenceCount.textContent, "60 / 6000");
assert.equal(elements.content.hidden, false);
assert.match(elements.metrics.innerHTML, /HLC3 典型价代理 · 非 VWAP/);
assert.match(elements.metrics.innerHTML, /\+1\.30%/);
assert.match(elements.metrics.innerHTML, /A股 T\+1 不可执行/);
assert.match(elements.groups.innerHTML, /Top100/);
assert.match(elements.details.innerHTML, /贵州茅台 · 600519\.SH/);
assert.match(elements.details.innerHTML, /低点平移/);
assert.match(elements.probability.innerHTML, /不上屏 0 或 50% 占位值/);

renderMarketScanFutureRange(elements, payload, { offset: 1, path: "cumulative_path", group: "top100", keyword: "" });
assert.match(elements.metrics.innerHTML, /累计 MAE/);
assert.match(elements.details.innerHTML, /终值收盘/);
assert.match(elements.details.innerHTML, /\+1\.63%/);
assert.match(elements.details.innerHTML, /D\+1 仅区间诊断/);

const executable = normalizeMarketScanFutureRangeResponse(readyResponse(2), 29);
renderMarketScanFutureRange(elements, executable, { offset: 2, path: "cumulative_path", group: "top100", keyword: "" });
assert.match(elements.metrics.innerHTML, /可执行净收益/);
assert.match(elements.metrics.innerHTML, /\+1\.40%/);
assert.match(elements.groups.innerHTML, /证据可用 · \+0\.30%/);
assert.match(elements.details.innerHTML, /毛收益/);
assert.match(elements.details.innerHTML, /净超额收益/);
assert.match(elements.details.innerHTML, /base · future-range-cost-v1/);

const unavailableResponse = readyResponse(2);
unavailableResponse.record_page.items[0].offsets[0].execution = {
  status: "data_unavailable", reason: "suspended_or_zero_volume", gross_return: null,
  cost_drag: null, net_return: null, market_benchmark_net_return: null, net_excess_return: null,
};
renderMarketScanFutureRange(elements, normalizeMarketScanFutureRangeResponse(unavailableResponse, 29), { offset: 2, path: "cumulative_path", group: "top100", keyword: "" });
assert.match(elements.details.innerHTML, /执行数据不可用/);
assert.match(elements.details.innerHTML, /停牌或零成交量/);
assert.match(elements.details.innerHTML, /<dt>净收益<\/dt><dd>--<\/dd>/);

const legacy = normalizeMarketScanFutureRangeResponse({
  schema_version: "market-scan-future-range-api-v1", generation_status: "not_generated",
  artifact: null, research: null, record_page: { page: 1, page_size: 20, total: 0, page_count: 0, session_offset: 1, symbol: null, items: [] },
}, 29);
renderMarketScanFutureRange(elements, legacy, { offset: 1, path: "specified_day", group: "top100", keyword: "" });
assert.equal(elements.content.hidden, true);
assert.match(elements.state.textContent, /不显示 0 或 50% 占位值/);
assert.doesNotMatch(elements.state.textContent, /0\.0%|50\.0%/);

assert.throws(
  () => normalizeMarketScanFutureRangeResponse({ ...readyResponse(1), research: { ...readyResponse(1).research, run: { run_id: 30, mode: "official" } } }, 29),
  /run_id 与请求批次不匹配/,
);

function viewElements() {
  const names = ["research", "summaryStatus", "refresh", "evidenceStatus", "evidenceCount", "coverage", "state", "content", "metrics", "groups", "probability", "details", "detailsHelp", "pagination", "pageText", "prev", "next", "limitations"];
  return Object.fromEntries(names.map((name) => [name, element()]));
}

function element(initial = {}) {
  return { textContent: "", innerHTML: "", hidden: false, disabled: false, dataset: {}, ...initial,
    setAttribute(name, value) { this[name] = String(value); },
  };
}
'''
        + _fixture_script()
    )


def test_future_range_controller_fetches_only_published_official_record_page_for_selected_offset() -> None:
    _run_node_script(
        r'''
import assert from "node:assert/strict";
import { createMarketScanFutureRangeController } from "./static/js/market-scan-future-range-controller.js";

const fixture = domFixture();
const urls = [];
let activeRun = { id: 29, mode: "official", status: "success" };
const controller = createMarketScanFutureRangeController({
  root: fixture.root, getRun: () => activeRun,
  async request(url) {
    urls.push(url);
    const params = new URL(url, "http://local").searchParams;
    const selected = Number(params.get("session_offset"));
    const payload = readyResponse(selected);
    if (params.get("include_research") === "false") payload.research = null;
    return payload;
  },
});
controller.sync(activeRun);
await controller.refresh();
assert.match(urls.at(-1), /future-range-research\?page=1&page_size=20&session_offset=1&include_research=true$/);
assert.equal(fixture.get("marketScanFutureRangeContent").hidden, false);

fixture.offsets[0].checked = false;
fixture.offsets[1].checked = true;
fixture.offsets[1].dispatch("change");
await flushPromises();
assert.match(urls.at(-1), /session_offset=2/);
assert.match(urls.at(-1), /include_research=false/);

fixture.get("marketScanFutureRangeKeyword").value = "600519.SH";
await controller.refresh();
assert.match(urls.at(-1), /symbol=600519.SH/);

const before = urls.length;
activeRun = { id: 31, mode: "intraday", status: "success" };
controller.sync(activeRun);
await controller.refresh();
assert.equal(urls.length, before);
assert.match(fixture.get("marketScanFutureRangeState").textContent, /盘中临时批次不可用/);

activeRun = { id: 32, mode: "official", status: "success", scope: "TOP100快速更新评分" };
controller.sync(activeRun);
await controller.refresh();
assert.equal(urls.length, before);
assert.equal(fixture.get("marketScanFutureRangeRefresh").disabled, true);
assert.match(fixture.get("marketScanFutureRangeState").textContent, /不是全市场快照/);

async function flushPromises() {
  for (let index = 0; index < 12; index += 1) await Promise.resolve();
}

function domFixture() {
  const ids = [
    "marketScanFutureRangeResearch", "marketScanFutureRangeSummaryStatus", "marketScanFutureRangeRefresh",
    "marketScanFutureRangeOffsetControl", "marketScanFutureRangePathControl", "marketScanFutureRangeGroup",
    "marketScanFutureRangeEvidenceStatus", "marketScanFutureRangeEvidenceCount", "marketScanFutureRangeCoverage",
    "marketScanFutureRangeState", "marketScanFutureRangeContent", "marketScanFutureRangeMetrics",
    "marketScanFutureRangeGroups", "marketScanFutureRangeProbability", "marketScanFutureRangeKeyword",
    "marketScanFutureRangeDetails", "marketScanFutureRangeDetailsHelp", "marketScanFutureRangePagination",
    "marketScanFutureRangePageText", "marketScanFutureRangePrev", "marketScanFutureRangeNext",
    "marketScanFutureRangeLimitations",
  ];
  const map = new Map(ids.map((id) => [id, element()]));
  const offsets = [radio("1", true), radio("2"), radio("3")];
  const paths = [radio("specified_day", true), radio("cumulative_path")];
  map.get("marketScanFutureRangeOffsetControl").querySelectorAll = () => offsets;
  map.get("marketScanFutureRangePathControl").querySelectorAll = () => paths;
  map.get("marketScanFutureRangeGroup").value = "top100";
  return { root: { getElementById: (id) => map.get(id) || null }, get: (id) => map.get(id), offsets };
}

function element(initial = {}) {
  const listeners = new Map();
  return { textContent: "", innerHTML: "", hidden: false, disabled: false, open: false, value: "", dataset: {}, ...initial,
    setAttribute(name, value) { this[name] = String(value); },
    addEventListener(name, handler) { listeners.set(name, handler); },
    dispatch(name) { listeners.get(name)?.({ target: this }); },
  };
}
function radio(value, checked = false) { return element({ value, checked }); }
'''
        + _fixture_script()
    )


def _fixture_script() -> str:
    return r'''
function readyResponse(offset) {
  const record = futureRecord(offset);
  return {
    schema_version: "market-scan-future-range-api-v1", generation_status: "ready",
    artifact: { schema_version: "market-scan-future-range-artifact-v1", generated_at: "2026-08-11T10:00:00+08:00", integrity_digest: "a".repeat(64) },
    research: {
      report_contract_version: "market-scan-future-range-report-v1", status: "ok", generated_at: "2026-08-11T10:00:00+08:00",
      run: { run_id: 29, mode: "official", data_date: "2026-07-31" },
      config: { session_offsets: [1, 2, 3], center_proxy: "HLC3_proxy_not_VWAP" },
      source: { read_only: true, adjustment_mode: "qfq" }, record_count: 6000,
      groups: [{ cohort: { mode: "official" }, group_type: "top_n", group_value: "100", session_offset: offset, status: "ok", sample_size: 6000, independent_session_count: 60,
        metrics: {
          level_shift_low: { mean: 0.004, median: 0.003, ci95: [0.001, 0.007] },
          level_shift_hlc3_proxy: { mean: 0.014, median: 0.013, ci95: [0.01, 0.016] },
          level_shift_high: { mean: 0.018, median: 0.017, ci95: [0.012, 0.02] },
          mae: { mean: -0.009, median: -0.008, ci95: [-0.01, -0.006] },
          mfe: { mean: 0.021, median: 0.02, ci95: [0.016, 0.024] },
          terminal_close_return: { mean: 0.017, median: 0.016, ci95: [0.011, 0.02] },
          net_return: offset === 1 ? { status: "insufficient_data", mean: null, median: null, ci95: null } : { status: "ok", mean: 0.015, median: 0.014, ci95: [0.01, 0.018] },
          net_excess_return: offset === 1 ? { status: "insufficient_data", mean: null, median: null, ci95: null } : { status: "ok", mean: 0.004, median: 0.003, ci95: [0.001, 0.006] },
        } }],
      rank_ic: [{ session_offset: offset, metric: "level_shift_hlc3_proxy", status: "ok", independent_session_count: 60, mean_rank_ic: 0.042, ci95: [0.018, 0.066] }],
      monotonicity: [{ session_offset: offset, metric: "level_shift_hlc3_proxy", status: "ok", independent_session_count: 60, spearman: 0.94, passed: true }],
      probability_context: { status: "not_available", limitations: ["calibrated_shadow_artifact_not_supplied"] },
      limitations: ["official_only"],
    },
    record_page: { page: 1, page_size: 20, total: 1, page_count: 1, session_offset: offset, symbol: null, items: [record] },
  };
}

function futureRecord(offset) {
  return {
    run_id: 29, symbol: "600519.SH", name: "贵州茅台", rank: 1, trend_score: 94,
    d_bar: { date: "2026-07-31", hlc3_proxy: 1406.666667 }, probability: { status: "not_available", predictions: [] },
    offsets: [{ session_offset: offset, target_session_date: "2026-08-03", fixed_session_status: "available",
      level_shift: { low: 0.007914, hlc3_proxy: 0.013272, high: 0.014085 },
      d1_open_reference: { entry_date: "2026-08-03", entry_price: 1412,
        specified_day: { low: -0.00779, hlc3_proxy: 0.009443, high: 0.01983, close: 0.016289 },
        cumulative_path: { mae: -0.00779, mfe: 0.01983, terminal_close_return: 0.016289 } },
      interval_structure: { normalized_width: 0.027362, overlap_ratio: 0.487179 },
      execution: offset === 1
        ? { status: "data_unavailable", reason: "A_share_T_plus_1_no_same_session_exit", gross_return: null, net_return: null, cost_drag: null, market_benchmark_net_return: null, net_excess_return: null }
        : { status: "modelled", reason: null, entry_date: "2026-08-03", exit_date: "2026-08-04", gross_return: 0.018, cost_drag: 0.002, net_return: 0.016, market_benchmark_net_return: 0.012, net_excess_return: 0.004, cost_profile_id: "base", cost_model_version: "future-range-cost-v1" } }],
  };
}
'''


def _run_node_script(script: str) -> None:
    subprocess.run(
        ["node", "--input-type=module", "-e", script],
        cwd=ROOT,
        check=True,
        text=True,
        capture_output=True,
    )
