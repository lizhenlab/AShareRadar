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
'''
        + _controller_fixture_script()
        + _fixture_script()
    )


def test_future_range_pagination_reuses_only_one_artifact_and_keeps_loading_controls_busy() -> None:
    _run_node_script(
        r'''
import assert from "node:assert/strict";
import { createMarketScanFutureRangeController } from "./static/js/market-scan-future-range-controller.js";

const fixture = domFixture();
const requests = [];
const run = publishedRun();
const controller = createMarketScanFutureRangeController({
  root: fixture.root, getRun: () => run,
  async request(url, options) { requests.push({ url, options }); return pagedResponse(url); },
});
controller.sync(run);
await controller.refresh();
const metrics = fixture.get("marketScanFutureRangeMetrics").innerHTML;
assert.equal(requests.length, 1);
assert.ok(requests[0].options.timeoutMs > 59_000 && requests[0].options.timeoutMs <= 60_000);
assert.match(fixture.get("marketScanFutureRangePageText").textContent, /第 1\/2 页/);
fixture.get("marketScanFutureRangeNext").dispatch("click");
assert.equal(fixture.get("marketScanFutureRangeResearch")["aria-busy"], "true");
assert.equal(fixture.get("marketScanFutureRangeNext").disabled, true);
fixture.get("marketScanFutureRangeGroup").dispatch("change");
assert.equal(fixture.get("marketScanFutureRangeResearch")["aria-busy"], "true");
fixture.get("marketScanFutureRangeNext").dispatch("click");
assert.equal(requests.length, 2, "busy pagination must not launch duplicate requests");
await flushPromises();
assert.match(requests[1].url, /page=2.*include_research=false/);
assert.equal(fixture.get("marketScanFutureRangeMetrics").innerHTML, metrics);
assert.match(fixture.get("marketScanFutureRangePageText").textContent, /第 2\/2 页/);
assert.match(fixture.get("marketScanFutureRangeDetails").innerHTML, /分页股票21/);
assert.equal(fixture.get("marketScanFutureRangeContent").hidden, false);
fixture.get("marketScanFutureRangePrev").dispatch("click");
await flushPromises();
assert.match(requests[2].url, /page=1.*include_research=false/);
assert.match(fixture.get("marketScanFutureRangePageText").textContent, /第 1\/2 页/);
'''
        + _controller_fixture_script()
        + _fixture_script()
    )


def test_future_range_retries_explicit_busy_response_with_one_trusted_read_budget() -> None:
    _run_node_script(
        r'''
import assert from "node:assert/strict";
import { fetchJson } from "./static/js/api.js";
import { createMarketScanFutureRangeController } from "./static/js/market-scan-future-range-controller.js";

const fixture = domFixture();
const requests = [];
let fetchCount = 0;
globalThis.fetch = async (url) => {
  fetchCount += 1;
  if (fetchCount === 1) return new Response(JSON.stringify({ detail: "snapshot busy" }), {
    status: 503, headers: { "Retry-After": "0", "Content-Type": "application/json" },
  });
  return new Response(JSON.stringify(pagedResponse(url)), { headers: { "Content-Type": "application/json" } });
};
const run = publishedRun();
const controller = createMarketScanFutureRangeController({
  root: fixture.root, getRun: () => run,
  request(url, options) { requests.push({ url, options }); return fetchJson(url, options); },
});
controller.sync(run);
assert.ok(await controller.refresh());
assert.equal(fetchCount, 2);
assert.equal(requests[0].url, requests[1].url);
assert.ok(requests[0].options.timeoutMs > 59_000 && requests[0].options.timeoutMs <= 60_000);
assert.ok(requests[1].options.timeoutMs < requests[0].options.timeoutMs);
assert.equal(requests[0].options.signal, requests[1].options.signal);
assert.equal(fixture.get("marketScanFutureRangeResearch")["aria-busy"], "false");
assert.equal(fixture.get("marketScanFutureRangeContent").hidden, false);
'''
        + _controller_fixture_script()
        + _fixture_script()
    )


def test_future_range_page_rejects_artifact_changes_and_does_not_resurrect_cached_research() -> None:
    _run_node_script(
        r'''
import assert from "node:assert/strict";
import { createMarketScanFutureRangeController } from "./static/js/market-scan-future-range-controller.js";

const mutations = [
  (value) => { value.artifact.integrity_digest = "b".repeat(64); },
  (value) => { value.artifact.generated_at = "2026-08-12T10:00:00+08:00"; },
  (value) => { value.generation_status = "insufficient_data"; },
  (value) => { value.schema_version = "market-scan-future-range-api-v2"; },
  (value) => { value.artifact.schema_version = "market-scan-future-range-artifact-v2"; },
  (value) => { value.artifact.integrity_digest = null; },
  (value) => { value.record_page.page = 1; },
  (value) => { value.record_page.session_offset = 2; },
];
for (const mutate of mutations) {
  const fixture = domFixture();
  const requests = [];
  const run = publishedRun();
  const controller = createMarketScanFutureRangeController({
    root: fixture.root, getRun: () => run,
    async request(url) {
      requests.push(url);
      const response = pagedResponse(url);
      if (requests.length === 2) mutate(response);
      if (requests.length === 3) {
        response.artifact.integrity_digest = "c".repeat(64);
        response.research.groups[0].metrics.level_shift_hlc3_proxy.median = 0.09;
      }
      return response;
    },
  });
  controller.sync(run);
  await controller.refresh();
  fixture.get("marketScanFutureRangeNext").dispatch("click");
  await flushPromises();
  assert.equal(requests.length, 2, "version drift must not start an unbounded refetch");
  assert.equal(fixture.get("marketScanFutureRangeContent").hidden, true);
  assert.equal(fixture.get("marketScanFutureRangeEvidenceStatus").textContent, "证据不可用");
  assert.equal(fixture.get("marketScanFutureRangeRefresh")["aria-busy"], "false");
  assert.match(fixture.get("marketScanFutureRangeState").textContent, /版本已变化|响应格式异常/);
  fixture.get("marketScanFutureRangeGroup").dispatch("change");
  fixture.get("marketScanFutureRangeKeyword").dispatch("input");
  assert.equal(fixture.get("marketScanFutureRangeContent").hidden, true);
  await controller.refresh();
  assert.match(requests.at(-1), /page=1.*include_research=true/);
  assert.equal(fixture.get("marketScanFutureRangeContent").hidden, false);
  assert.match(fixture.get("marketScanFutureRangeMetrics").innerHTML, /\+9\.00%/);
}
'''
        + _controller_fixture_script()
        + _fixture_script()
    )


def test_future_range_sync_binds_complete_publication_and_ignores_stale_responses() -> None:
    _run_node_script(
        r'''
import assert from "node:assert/strict";
import { createMarketScanFutureRangeController } from "./static/js/market-scan-future-range-controller.js";

const fixture = domFixture();
const pending = [];
let run = publishedRun();
const controller = createMarketScanFutureRangeController({
  root: fixture.root, getRun: () => run,
  request(url, options) { return new Promise((resolve) => pending.push({ url, options, resolve })); },
});
controller.sync(run);
const first = controller.refresh();
run.snapshot_digest = "b".repeat(64);
assert.equal(controller.sync(run), true, "in-place seal changes must invalidate the saved binding");
assert.equal(pending[0].options.signal.aborted, true);
const second = controller.refresh();
const current = pagedResponse(pending[1].url);
current.artifact.integrity_digest = "b".repeat(64);
current.research.groups[0].metrics.level_shift_hlc3_proxy.median = 0.08;
pending[1].resolve(current);
await second;
const freshMetrics = fixture.get("marketScanFutureRangeMetrics").innerHTML;
assert.match(freshMetrics, /\+8\.00%/);
pending[0].resolve(pagedResponse(pending[0].url));
assert.equal(await first, null);
assert.equal(fixture.get("marketScanFutureRangeMetrics").innerHTML, freshMetrics);
for (const field of ["rule_version", "scope", "data_date", "quote_date", "updated_at", "finished_at", "snapshot_seal_origin", "snapshot_sealed_at"]) {
  run = { ...run, [field]: `${run[field]}-changed` };
  assert.equal(controller.sync(run), true, `publication field ${field} must participate in the binding`);
  assert.equal(fixture.get("marketScanFutureRangeContent").hidden, true);
  assert.equal(fixture.get("marketScanFutureRangeRefresh")["aria-busy"], "false");
}
assert.equal(controller.sync({ ...run }), false);
const stale = controller.refresh();
run = { ...run, id: 30 };
pending.at(-1).resolve(pagedResponse(pending.at(-1).url));
assert.equal(await stale, null, "getter changes must reject a stale response even before sync arrives");
assert.equal(fixture.get("marketScanFutureRangeContent").hidden, true);
assert.equal(fixture.get("marketScanFutureRangeResearch")["aria-busy"], "false");
'''
        + _controller_fixture_script()
        + _fixture_script()
    )


def test_future_range_close_aborts_requests_and_retry_backoff_without_stuck_busy_state() -> None:
    _run_node_script(
        r'''
import assert from "node:assert/strict";
import { createMarketScanFutureRangeController } from "./static/js/market-scan-future-range-controller.js";

const fixture = domFixture();
const pending = [];
const run = publishedRun();
const controller = createMarketScanFutureRangeController({
  root: fixture.root, getRun: () => run,
  request(url, options) { return new Promise((resolve) => pending.push({ url, options, resolve })); },
});
controller.sync(run);
const panel = fixture.get("marketScanFutureRangeResearch");
panel.open = true;
const first = controller.refresh();
panel.open = false;
panel.dispatch("toggle");
assert.equal(pending[0].options.signal.aborted, true);
assert.equal(panel["aria-busy"], "false");
assert.equal(fixture.get("marketScanFutureRangeRefresh")["aria-busy"], "false");
assert.equal(fixture.get("marketScanFutureRangeRefresh").disabled, false);
pending[0].resolve(pagedResponse(pending[0].url));
assert.equal(await first, null);
assert.equal(fixture.get("marketScanFutureRangeContent").hidden, true);
panel.open = true;
panel.dispatch("toggle");
assert.equal(pending.length, 2);
assert.match(pending[1].url, /page=1.*include_research=true/);
pending[1].resolve(pagedResponse(pending[1].url));
await flushPromises();
assert.equal(fixture.get("marketScanFutureRangeContent").hidden, false);
panel.open = false;
panel.dispatch("toggle");
panel.open = true;
panel.dispatch("toggle");
assert.equal(pending.length, 2, "closing completed evidence should not discard a valid cache");
const publicAbort = controller.refresh();
controller.abort();
pending[2].resolve(pagedResponse(pending[2].url));
assert.equal(await publicAbort, null);
assert.equal(pending[2].options.signal.aborted, true);
assert.equal(panel["aria-busy"], "false");

const retryFixture = domFixture();
let attempts = 0;
const retryController = createMarketScanFutureRangeController({
  root: retryFixture.root, getRun: () => run,
  async request() { attempts += 1; throw Object.assign(new Error("busy"), { status: 503, retryAfterMs: 1_000 }); },
});
retryController.sync(run);
retryFixture.get("marketScanFutureRangeResearch").open = true;
const retry = retryController.refresh();
await flushPromises();
retryFixture.get("marketScanFutureRangeResearch").open = false;
retryFixture.get("marketScanFutureRangeResearch").dispatch("toggle");
assert.equal(await retry, null);
assert.equal(attempts, 1);
assert.equal(retryFixture.get("marketScanFutureRangeResearch")["aria-busy"], "false");
assert.equal(retryFixture.get("marketScanFutureRangeRefresh")["aria-busy"], "false");
'''
        + _controller_fixture_script()
        + _fixture_script()
    )


def test_future_range_not_generated_drops_cached_research_and_requires_valid_artifact_identity() -> None:
    _run_node_script(
        r'''
import assert from "node:assert/strict";
import { createMarketScanFutureRangeController } from "./static/js/market-scan-future-range-controller.js";
import { normalizeMarketScanFutureRangeResponse } from "./static/js/market-scan-future-range-view.js";

for (const mutate of [
  (value) => { value.artifact = null; },
  (value) => { value.artifact.integrity_digest = 12; },
  (value) => { value.artifact.integrity_digest = "A".repeat(64); },
  (value) => { value.artifact.generated_at = "invalid-date"; },
  (value) => { value.generation_status = "not_generated"; value.research = null; },
]) {
  const payload = readyResponse(1);
  mutate(payload);
  assert.throws(() => normalizeMarketScanFutureRangeResponse(payload, 29), /未来区间接口响应格式异常/);
}
const fixture = domFixture();
const requests = [];
const run = publishedRun();
const controller = createMarketScanFutureRangeController({
  root: fixture.root, getRun: () => run,
  async request(url) {
    requests.push(url);
    if (requests.length !== 2) return pagedResponse(url);
    return { schema_version: "market-scan-future-range-api-v1", generation_status: "not_generated",
      artifact: null, research: null,
      record_page: { page: 1, page_size: 100, total: 0, page_count: 0, session_offset: null, symbol: null, items: [] } };
  },
});
controller.sync(run);
await controller.refresh();
fixture.get("marketScanFutureRangeNext").dispatch("click");
await flushPromises();
assert.equal(fixture.get("marketScanFutureRangeContent").hidden, true);
assert.equal(fixture.get("marketScanFutureRangeSummaryStatus").textContent, "尚未生成");
fixture.offsets[0].checked = false;
fixture.offsets[1].checked = true;
fixture.offsets[1].dispatch("change");
await flushPromises();
assert.match(requests.at(-1), /session_offset=2&include_research=true/);
'''
        + _controller_fixture_script()
        + _fixture_script()
    )


def _controller_fixture_script() -> str:
    return r'''

async function flushPromises() {
  for (let index = 0; index < 20; index += 1) await Promise.resolve();
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

function publishedRun() {
  return { id: 29, mode: "official", status: "success", scope: "沪市 + 深市 + 北交所当前上市A股",
    rule_version: "full-market-score-v4", data_date: "2026-07-31", quote_date: "2026-07-31",
    updated_at: "2026-07-31T16:31:00+08:00", finished_at: "2026-07-31T16:31:00+08:00",
    snapshot_digest: "a".repeat(64), snapshot_seal_origin: "publication",
    snapshot_sealed_at: "2026-07-31T16:31:00+08:00" };
}

function pagedResponse(url) {
  const params = new URL(url, "http://local").searchParams;
  const offset = Number(params.get("session_offset"));
  const page = Number(params.get("page"));
  const payload = readyResponse(offset);
  if (params.get("include_research") === "false") payload.research = null;
  const start = (page - 1) * 20;
  const items = Array.from({ length: Math.max(0, Math.min(20, 21 - start)) }, (_, index) => ({
    ...futureRecord(offset), rank: start + index + 1,
    name: `分页股票${start + index + 1}`, symbol: `${600500 + start + index}.SH`,
  }));
  payload.record_page = { page, page_size: 20, total: 21, page_count: 2, session_offset: offset, symbol: null, items };
  return payload;
}
'''


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
        timeout=15,
    )
