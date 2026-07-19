from __future__ import annotations

import json
import re
import subprocess
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]


def test_market_scan_frontend_contract_is_wired_into_workspace() -> None:
    html = (ROOT / "static/index.html").read_text(encoding="utf-8")
    app = (ROOT / "static/app.js").read_text(encoding="utf-8")
    preferences = (ROOT / "static/js/workspace-preferences.js").read_text(encoding="utf-8")
    styles = (ROOT / "static/styles.css").read_text(encoding="utf-8")

    assert 'data-view="market-scan"' in html
    assert 'id="marketScanStart"' in html
    assert 'id="marketScanProgressBar"' in html
    assert 'id="marketScanFinishedAt"' in html
    assert 'id="marketScanRows"' in html
    assert 'id="marketScanAnnouncement" role="status" aria-live="polite" aria-atomic="true" aria-relevant="text"' in html
    assert 'id="marketScanProgressBar" max="100" value="0" aria-label="全市场扫描进度"' in html
    assert 'aria-valuetext="尚无扫描进度" aria-busy="false"' in html
    scan_panel = html.split('id="workspace-panel-market-scan"', 1)[1].split('</section>\n\n        <section class="workspace-view"', 1)[0]
    assert scan_panel.count('aria-live="polite"') == 1
    assert 'id="marketScanHeadline" role="status"' not in scan_panel
    assert 'id="marketScanResultState" role="status"' not in scan_panel
    assert 'id="marketScanTableWrap" role="region"' in html
    assert 'aria-label="全市场扫描榜单，可横向滚动" aria-busy="false" tabindex="0"' in html
    assert 'id="marketScanPagination" aria-busy="false"' in html
    assert 'id="stockWorkbench" tabindex="-1" aria-labelledby="stockName"' in html
    assert 'id="marketScanStatus"' in html and 'value="all"' in html
    assert 'createMarketScanController' in app
    assert 'target === "market-scan"' in app
    assert '"market-scan"' in preferences
    assert '@import url("/static/css/market-scan.css")' in styles
    versions = re.findall(r'(?:href|src)="/static/(?:styles\.css|css/market-scan\.css|app\.js)\?v=([^"]+)"', html)
    assert len(versions) == 3
    import_map_match = re.search(r'<script type="importmap">\s*(\{.*?\})\s*</script>', html, re.DOTALL)
    assert import_map_match is not None
    imports = json.loads(import_map_match.group(1))["imports"]
    module_paths = {
        "/static/js/market-scan.js",
        "/static/js/market-scan-controller.js",
        "/static/js/market-scan-contracts.js",
        "/static/js/market-scan-polling.js",
        "/static/js/market-scan-view.js",
    }
    assert set(imports) == module_paths
    module_versions = [imports[path].split("?v=", 1)[1] for path in module_paths]
    assert len(set([*versions, *module_versions])) == 1


def test_market_scan_modules_have_explicit_reviewable_boundaries() -> None:
    module_dir = ROOT / "static/js"
    facade = (module_dir / "market-scan.js").read_text(encoding="utf-8")
    controller = (module_dir / "market-scan-controller.js").read_text(encoding="utf-8")
    contracts = (module_dir / "market-scan-contracts.js").read_text(encoding="utf-8")
    polling = (module_dir / "market-scan-polling.js").read_text(encoding="utf-8")
    view = (module_dir / "market-scan-view.js").read_text(encoding="utf-8")

    modules = {
        "market-scan.js": facade,
        "market-scan-controller.js": controller,
        "market-scan-contracts.js": contracts,
        "market-scan-polling.js": polling,
        "market-scan-view.js": view,
    }
    for filename, source in modules.items():
        assert len(source.splitlines()) < 600, f"{filename} should remain below 600 lines"
    assert len(controller.splitlines()) <= 450

    assert "createMarketScanController" in facade
    assert "buildMarketScanResultsUrl" in facade
    assert "fetchJson" in controller and "createMarketScanPolling" in controller
    assert "failureDelay" not in controller and "consecutiveFailures +=" not in controller
    assert "setTimeout" in polling and "createRequestScope" in polling and "fetchJson" not in polling
    assert "validateMarketScanRun" in contracts and "fetchJson" not in contracts and "setTimeout" not in contracts
    assert "marketScanResultRow" in view and "escapeHtml" in view
    assert "fetchJson" not in view and "setTimeout" not in view


def test_market_scan_query_and_rows_are_bounded_encoded_and_escaped() -> None:
    _run_node_script(
        r'''
import assert from "node:assert/strict";
import {
  buildMarketScanResultsUrl,
  marketScanResultRow,
  marketScanResultsUrl,
} from "./static/js/market-scan.js";

const input = (value = "") => ({ value });
const url = marketScanResultsUrl(17, 2, {
  status: input("all"),
  market: input("BJ"),
  industry: input("专用 设备"),
  isSt: input("false"),
  isNew: input("true"),
  quality: input("70"),
  keyword: input("920066 科拜尔"),
  sort: input("score"),
  order: input("desc"),
});
const parsed = new URL(url, "http://localhost");
assert.equal(buildMarketScanResultsUrl(17, 2, {
  status: input("all"), market: input("BJ"), industry: input("专用 设备"),
  isSt: input("false"), isNew: input("true"), quality: input("70"),
  keyword: input("920066 科拜尔"), sort: input("score"), order: input("desc"),
}), url);
assert.equal(parsed.pathname, "/api/market-scans/17/results");
assert.equal(parsed.searchParams.get("page"), "2");
assert.equal(parsed.searchParams.get("page_size"), "100");
assert.equal(parsed.searchParams.get("status"), "all");
assert.equal(parsed.searchParams.get("market"), "BJ");
assert.equal(parsed.searchParams.get("industry"), "专用 设备");
assert.equal(parsed.searchParams.get("is_st"), "false");
assert.equal(parsed.searchParams.get("is_new"), "true");
assert.equal(parsed.searchParams.get("min_data_quality_score"), "70");
assert.equal(parsed.searchParams.get("keyword"), "920066 科拜尔");
assert.equal(parsed.searchParams.get("sort"), "score");
assert.equal(parsed.searchParams.get("order"), "desc");

const row = marketScanResultRow({
  rank: 1,
  symbol: '920066.BJ"><script>alert(1)</script>',
  code: "920066",
  market: "BJ",
  name: "科拜尔<script>",
  industry: "专用设备",
  status: "success",
  score: 88,
  trend_score: 72,
  change_pct: 1.25,
  turnover_rate: 2.5,
  amount: 125000000,
  data_quality_score: 91,
  tags: ["趋势向上", "量价配合"],
  is_new: true,
});
assert.equal(row.includes("<script>"), false);
assert.equal(row.includes("&lt;script&gt;"), true);
assert.equal(row.includes("page_size=5000"), false);
assert.equal(row.includes("+1.25%"), true);
assert.equal(row.includes("1.3亿"), true);
'''
    )


def test_market_scan_controller_loads_terminal_snapshot_and_tracks_active_run() -> None:
    _run_node_script(
        r'''
import assert from "node:assert/strict";
import { installAppDom } from "./tests/frontend_app_flow_helpers.mjs";
import { createMarketScanController } from "./static/js/market-scan.js";

const { element } = installAppDom({ canvasContext: null });
const calls = [];
const terminal = {
  id: 9,
  status: "degraded",
  trigger: "manual",
  rule_version: "full-market-score-v1",
  as_of: "2026-07-17 16:30:00",
  data_date: "2026-07-17",
  scope: "SH/SZ/BJ",
  total_count: 3,
  excluded_count: 1,
  processed_count: 3,
  success_count: 2,
  missing_count: 1,
  skipped_count: 0,
  retry_count: 0,
  progress_pct: 100,
  coverage_pct: 66.7,
  created_at: "2026-07-17 16:30:00",
  updated_at: "2026-07-17 16:45:30",
  message: "全市场扫描降级完成",
  finished_at: "2026-07-17 16:45:30",
};
const resultPage = {
  run: terminal,
  page: 1,
  page_size: 100,
  page_count: 1,
  total: 1,
  items: [{
    run_id: 9,
    rank: 1,
    symbol: "920066.BJ",
    code: "920066",
    market: "BJ",
    name: "科拜尔",
    industry: "专用设备",
    status: "success",
    is_st: false,
    is_new: false,
    score: 88,
    trend_score: 72,
    change_pct: 1.25,
    turnover_rate: 2.5,
    amount: 125000000,
    data_quality_score: 91,
    tags: ["趋势向上"],
    metrics: {},
    updated_at: "2026-07-17 16:45:30",
  }],
};
let latestRun = terminal;
const controller = createMarketScanController({
  root: document,
  pollIntervalMs: 60000,
  async fetcher(url) {
    calls.push(String(url));
    if (url === "/api/market-scans/latest") return latestRun;
    if (String(url).includes("/results?")) return resultPage;
    throw new Error(`unexpected request: ${url}`);
  },
});

await controller.activate();
assert.deepEqual(calls.map((url) => url.split("?")[0]), [
  "/api/market-scans/latest",
  "/api/market-scans/9/results",
]);
assert.equal(element("marketScanHeadline").textContent, "全市场扫描降级完成");
assert.equal(element("marketScanProgressText").textContent, "3/3 · 100.0%");
assert.equal(element("marketScanTotal").textContent, "3（排除 1）");
assert.equal(element("marketScanCoverage").textContent, "66.7%");
assert.equal(element("marketScanFinishedAt").textContent, "2026-07-17 16:45");
assert.equal(element("marketScanRows").innerHTML.includes("920066.BJ"), true);
assert.equal(element("marketScanTableWrap").hidden, false);
assert.equal(element("marketScanAnnouncement").textContent, "榜单加载完成，第 1/1 页，本页 1 条，共 1 条。");
assert.equal(element("marketScanRetry").hidden, false);

latestRun = { ...terminal, id: 11, status: "running", processed_count: 1, progress_pct: 10, message: "新扫描运行中" };
controller.deactivate();
await controller.activate();
assert.equal(controller.state.run.id, 11);
assert.equal(controller.state.run.status, "running");
assert.equal(element("marketScanTableWrap").hidden, true);
assert.equal(element("marketScanRows").innerHTML, "");
assert.deepEqual(calls.map((url) => url.split("?")[0]), [
  "/api/market-scans/latest",
  "/api/market-scans/9/results",
  "/api/market-scans/latest",
]);

const activeCalls = [];
const activeController = createMarketScanController({
  root: document,
  pollIntervalMs: 60000,
  async fetcher(url) {
    activeCalls.push(String(url));
    if (url === "/api/market-scans/latest") return null;
    if (url === "/api/market-scans") return {
      accepted: true,
      deduplicated: false,
      run: { ...terminal, id: 10, status: "running", processed_count: 1, progress_pct: 33.3, message: null },
    };
    throw new Error(`unexpected request: ${url}`);
  },
});
await activeController.start();
assert.equal(activeController.state.run.status, "running");
assert.equal(element("marketScanStart").disabled, true);
assert.equal(element("marketScanCancel").hidden, false);
assert.equal(element("marketScanResultState").textContent.includes("稳定榜单"), true);
assert.equal(activeCalls.includes("/api/market-scans"), true);
activeController.deactivate();
controller.deactivate();

let resolveOldLatest;
const oldLatest = new Promise((resolve) => { resolveOldLatest = resolve; });
const raceController = createMarketScanController({
  root: document,
  pollIntervalMs: 60000,
  async fetcher(url) {
    if (url === "/api/market-scans/latest") return oldLatest;
    if (url === "/api/market-scans") return {
      accepted: true,
      deduplicated: false,
      run: { ...terminal, id: 22, status: "running", message: "用户新建扫描" },
    };
    throw new Error(`unexpected request: ${url}`);
  },
});
const activation = raceController.activate();
await Promise.resolve();
await raceController.start();
resolveOldLatest({ ...terminal, id: 21, status: "success", message: "旧的最近扫描" });
await activation;
assert.equal(raceController.state.run.id, 22);
assert.equal(raceController.state.run.message, "用户新建扫描");
raceController.deactivate();

let unpublishedResultCalls = 0;
const unpublishedController = createMarketScanController({
  root: document,
  async fetcher(url) {
    if (url === "/api/market-scans/latest") return { ...terminal, id: 23, status: "cancelled", message: "用户取消" };
    if (String(url).includes("/results?")) unpublishedResultCalls += 1;
    throw new Error(`unexpected request: ${url}`);
  },
});
await unpublishedController.activate();
assert.equal(unpublishedResultCalls, 0);
assert.equal(element("marketScanResultState").textContent.includes("未发布正式榜单"), true);
unpublishedController.deactivate();
'''
    )


def test_market_scan_controller_discovers_external_run_and_clears_previous_snapshot() -> None:
    _run_node_script(
        r'''
import assert from "node:assert/strict";
import { installAppDom } from "./tests/frontend_app_flow_helpers.mjs";
import { createMarketScanController } from "./static/js/market-scan.js";

const { element } = installAppDom({ canvasContext: null });
const timers = installFakeTimers();
const run = (id, status, message) => ({
  id,
  status,
  trigger: "manual",
  rule_version: "full-market-score-v1",
  as_of: "2026-07-17 16:30:00",
  data_date: "2026-07-17",
  scope: "SH/SZ/BJ",
  total_count: 1,
  excluded_count: 0,
  processed_count: status === "running" ? 0 : 1,
  success_count: status === "running" ? 0 : 1,
  missing_count: 0,
  skipped_count: 0,
  retry_count: 0,
  progress_pct: status === "running" ? 0 : 100,
  coverage_pct: status === "running" ? 0 : 100,
  created_at: "2026-07-17 16:30:00",
  updated_at: "2026-07-17 16:31:00",
  finished_at: status === "running" ? null : "2026-07-17 16:31:00",
  message,
});
const firstActive = run(9, "running", "首轮扫描中");
const firstTerminal = run(9, "success", "首轮扫描完成");
const externalActive = run(11, "running", "调度扫描中");
let latestCalls = 0;
const calls = [];
const controller = createMarketScanController({
  root: document,
  pollIntervalMs: 5,
  idlePollIntervalMs: 7,
  async fetcher(url) {
    calls.push(String(url));
    if (url === "/api/market-scans/latest") {
      latestCalls += 1;
      return latestCalls === 1 ? firstActive : externalActive;
    }
    if (url === "/api/market-scans/9") return firstTerminal;
    if (url === "/api/market-scans/11") return externalActive;
    if (String(url).includes("/api/market-scans/9/results?")) {
      return {
        run: firstTerminal,
        page: 1,
        page_size: 100,
        page_count: 1,
        total: 1,
        items: [{ run_id: 9, rank: 1, symbol: "600519.SH", code: "600519", market: "SH", name: "旧榜单股票", status: "success", score: 90, is_st: false, is_new: false, tags: [], metrics: {}, updated_at: "2026-07-17 16:31:00" }],
      };
    }
    throw new Error(`unexpected request: ${url}`);
  },
});

await controller.activate();
await timers.fireNext();
assert.equal(controller.state.run.status, "success");
assert.equal(element("marketScanRows").innerHTML.includes("旧榜单股票"), true);
assert.equal(element("marketScanTableWrap").hidden, false);

controller.state.page = 5;
controller.state.pageCount = 8;
await timers.fireNext();
assert.equal(latestCalls, 2);
assert.equal(controller.state.run.id, 11);
assert.equal(controller.state.run.status, "running");
assert.equal(controller.state.page, 1);
assert.equal(controller.state.pageCount, 0);
assert.equal(element("marketScanRows").innerHTML, "");
assert.equal(element("marketScanTableWrap").hidden, true);
assert.equal(element("marketScanResultState").textContent.includes("稳定榜单"), true);
assert.equal(calls.includes("/api/market-scans/9/results?page=1&page_size=100&status=success&sort=rank&order=asc"), true);
controller.deactivate();
assert.equal(timers.size(), 0);

function installFakeTimers() {
  let nextId = 0;
  const scheduled = new Map();
  globalThis.setTimeout = (callback, delay = 0) => {
    const id = ++nextId;
    scheduled.set(id, { callback, delay });
    return id;
  };
  globalThis.clearTimeout = (id) => scheduled.delete(id);
  return {
    size: () => scheduled.size,
    async fireNext() {
      const entry = [...scheduled.entries()].sort((left, right) => left[1].delay - right[1].delay || left[0] - right[0])[0];
      assert.ok(entry, "expected a scheduled refresh");
      scheduled.delete(entry[0]);
      entry[1].callback();
      for (let index = 0; index < 20; index += 1) await Promise.resolve();
    },
  };
}
'''
    )


def test_market_scan_controller_retries_results_and_reconciles_uncertain_mutation() -> None:
    _run_node_script(
        r'''
import assert from "node:assert/strict";
import { installAppDom } from "./tests/frontend_app_flow_helpers.mjs";
import { createMarketScanController } from "./static/js/market-scan.js";

const { element } = installAppDom({ canvasContext: null });
const timers = installFakeTimers();
const terminal = {
  id: 20,
  status: "success",
  trigger: "manual",
  rule_version: "full-market-score-v1",
  as_of: "2026-07-17 16:30:00",
  data_date: "2026-07-17",
  scope: "SH/SZ/BJ",
  total_count: 1,
  excluded_count: 0,
  processed_count: 1,
  success_count: 1,
  missing_count: 0,
  skipped_count: 0,
  retry_count: 0,
  progress_pct: 100,
  coverage_pct: 100,
  created_at: "2026-07-17 16:30:00",
  updated_at: "2026-07-17 16:31:00",
  finished_at: "2026-07-17 16:31:00",
  message: "扫描完成",
};
let resultCalls = 0;
const retryController = createMarketScanController({
  root: document,
  idlePollIntervalMs: 1000,
  resultRetryIntervalMs: 5,
  async fetcher(url) {
    if (url === "/api/market-scans/latest") return terminal;
    if (String(url).includes("/results?")) {
      resultCalls += 1;
      if (resultCalls === 1) throw new Error("临时读取失败");
      return {
        run: terminal,
        page: 1,
        page_size: 100,
        page_count: 1,
        total: 1,
        items: [{ run_id: 20, rank: 1, symbol: "920066.BJ", code: "920066", market: "BJ", name: "北交样本", status: "success", score: 88, is_st: false, is_new: false, tags: [], metrics: {}, updated_at: "2026-07-17 16:31:00" }],
      };
    }
    throw new Error(`unexpected request: ${url}`);
  },
});

await retryController.activate();
assert.equal(resultCalls, 1);
assert.equal(element("marketScanResultState").textContent.includes("榜单读取失败"), true);
await timers.fireNext();
assert.equal(resultCalls, 2);
assert.equal(element("marketScanTableWrap").hidden, false);
assert.equal(element("marketScanAnnouncement").textContent, "榜单加载完成，第 1/1 页，本页 1 条，共 1 条。");
retryController.deactivate();

let serverRun = null;
let latestCalls = 0;
const active = { ...terminal, id: 21, status: "running", processed_count: 0, success_count: 0, progress_pct: 0, coverage_pct: 0, finished_at: null, message: "服务端任务运行中" };
const mutationController = createMarketScanController({
  root: document,
  pollIntervalMs: 1000,
  idlePollIntervalMs: 1000,
  async fetcher(url) {
    if (url === "/api/market-scans/latest") {
      latestCalls += 1;
      return serverRun;
    }
    if (url === "/api/market-scans") {
      serverRun = active;
      throw new Error("任务创建后响应丢失");
    }
    throw new Error(`unexpected request: ${url}`);
  },
});

await mutationController.activate();
await mutationController.start();
assert.equal(latestCalls, 2);
assert.equal(mutationController.state.run.id, 21);
assert.equal(mutationController.state.run.status, "running");
assert.equal(element("marketScanHeadline").textContent, "请求响应未确认，已从服务端恢复任务状态。");
assert.equal(element("marketScanStart").disabled, true);
mutationController.deactivate();

function installFakeTimers() {
  let nextId = 0;
  const scheduled = new Map();
  globalThis.setTimeout = (callback, delay = 0) => {
    const id = ++nextId;
    scheduled.set(id, { callback, delay });
    return id;
  };
  globalThis.clearTimeout = (id) => scheduled.delete(id);
  return {
    async fireNext() {
      const entry = [...scheduled.entries()].sort((left, right) => left[1].delay - right[1].delay || left[0] - right[0])[0];
      assert.ok(entry, "expected a scheduled retry");
      scheduled.delete(entry[0]);
      entry[1].callback();
      for (let index = 0; index < 20; index += 1) await Promise.resolve();
    },
  };
}
'''
    )


def test_market_scan_controller_rejects_malformed_success_payloads() -> None:
    _run_node_script(
        r'''
import assert from "node:assert/strict";
import { installAppDom } from "./tests/frontend_app_flow_helpers.mjs";
import {
  createMarketScanController,
  validateMarketScanRun,
  validateResultPage,
} from "./static/js/market-scan.js";

const terminal = scanRun(70, "success");
assert.throws(
  () => validateMarketScanRun({ id: 70, status: "success" }),
  /trigger/
);
assert.throws(
  () => validateResultPage({ run: terminal, total: 0, page: 1, page_size: 100, page_count: 0 }, 70),
  /items 必须是数组/
);
const resultItem = {
  run_id: 70, rank: 1, symbol: "920066.BJ", code: "920066", market: "BJ", name: "北交样本",
  updated_at: "2026-07-17 16:31:00", status: "success", is_st: false, is_new: false,
  industry: null, list_date: null, metadata_source: null, reason: null, error: null, data_date: null,
  quote_timestamp: null, quote_source: null, kline_source: null, adjustment_mode: null,
  score: 80, trend_score: 80, leader_score: 80, data_quality_score: 80,
  price: 10, change_pct: 1, turnover_rate: 2, volume_ratio: 1, amount: 1000000,
  tags: [], metrics: {},
};
const resultPage = (item) => ({ run: terminal, total: 1, page: 1, page_size: 100, page_count: 1, items: [item] });
assert.equal(validateResultPage(resultPage(resultItem), 70).items[0].symbol, "920066.BJ");
for (const invalid of [
  { ...resultItem, symbol: "920066.SH" },
  { ...resultItem, market: "SH" },
  { ...resultItem, code: "920067" },
]) {
  assert.throws(() => validateResultPage(resultPage(invalid), 70), /symbol/);
}

const { element } = installAppDom({ canvasContext: null });
const controller = createMarketScanController({
  root: document,
  resultRetryIntervalMs: 60000,
  async fetcher(url) {
    if (url === "/api/market-scans/latest") return terminal;
    if (String(url).includes("/results?")) {
      return { run: terminal, total: 0, page: 1, page_size: 100, page_count: 0 };
    }
    throw new Error(`unexpected request: ${url}`);
  },
});

await controller.activate();
assert.equal(element("marketScanTableWrap").hidden, true);
assert.equal(element("marketScanRows").innerHTML, "");
assert.match(element("marketScanResultState").textContent, /响应格式异常.*items 必须是数组/);
assert.match(element("marketScanAnnouncement").textContent, /榜单读取失败.*items 必须是数组/);
assert.notEqual(controller.state.pollTimer, null);
controller.deactivate();

function scanRun(id, status) {
  const active = status === "running";
  return {
    id,
    status,
    trigger: "manual",
    rule_version: "full-market-score-v1",
    as_of: "2026-07-17 16:30:00",
    data_date: "2026-07-17",
    scope: "SH/SZ/BJ",
    total_count: 1,
    excluded_count: 0,
    processed_count: active ? 0 : 1,
    success_count: active ? 0 : 1,
    missing_count: 0,
    skipped_count: 0,
    retry_count: 0,
    progress_pct: active ? 0 : 100,
    coverage_pct: active ? 0 : 100,
    created_at: "2026-07-17 16:30:00",
    updated_at: "2026-07-17 16:31:00",
    finished_at: active ? null : "2026-07-17 16:31:00",
    message: active ? "扫描中" : "扫描完成",
  };
}
'''
    )


def test_market_scan_controller_recovers_missing_run_and_syncs_immediately_online() -> None:
    _run_node_script(
        r'''
import assert from "node:assert/strict";
import { installAppDom } from "./tests/frontend_app_flow_helpers.mjs";
import { createMarketScanController } from "./static/js/market-scan.js";

const { element } = installAppDom({ canvasContext: null });
const timers = installFakeTimers();
const listeners = {};
const connectivityTarget = {
  addEventListener(name, handler) { listeners[name] = handler; },
};
let latestCalls = 0;
const calls = [];
const controller = createMarketScanController({
  root: document,
  connectivityTarget,
  pollIntervalMs: 5,
  async fetcher(url) {
    calls.push(String(url));
    if (url === "/api/market-scans/latest") {
      latestCalls += 1;
      return scanRun(latestCalls === 1 ? 70 : latestCalls === 2 ? 71 : 72);
    }
    if (url === "/api/market-scans/70") {
      const error = new Error("全市场扫描批次不存在：70");
      error.status = 404;
      throw error;
    }
    if (url === "/api/market-scans/71" || url === "/api/market-scans/72") return scanRun(Number(url.split("/").at(-1)));
    throw new Error(`unexpected request: ${url}`);
  },
});

await controller.activate();
controller.state.page = 6;
controller.state.pageCount = 9;
assert.equal(timers.size(), 1);
await timers.fireNext();
assert.equal(latestCalls, 2);
assert.equal(controller.state.run.id, 71);
assert.equal(controller.state.page, 1);
assert.equal(controller.state.pageCount, 0);
assert.equal(element("marketScanHeadline").textContent, "原扫描记录已失效，正在同步最近扫描。");
assert.equal(element("marketScanProgressBar")["aria-busy"], "true");
assert.equal(timers.size(), 1);

listeners.online();
listeners.online();
await flushPromises();
assert.equal(latestCalls, 3);
assert.equal(controller.state.run.id, 72);
assert.equal(element("marketScanHeadline").textContent, "网络已恢复，正在同步最近扫描。");
assert.equal(timers.size(), 1);
assert.deepEqual(calls.slice(0, 4), [
  "/api/market-scans/latest",
  "/api/market-scans/70",
  "/api/market-scans/latest",
  "/api/market-scans/latest",
]);
controller.deactivate();
assert.equal(timers.size(), 0);

function scanRun(id) {
  return {
    id,
    status: "running",
    trigger: "manual",
    rule_version: "full-market-score-v1",
    as_of: "2026-07-17 16:30:00",
    data_date: "2026-07-17",
    scope: "SH/SZ/BJ",
    total_count: 100,
    excluded_count: 0,
    processed_count: 10,
    success_count: 10,
    missing_count: 0,
    skipped_count: 0,
    retry_count: 0,
    progress_pct: 10,
    coverage_pct: 10,
    created_at: "2026-07-17 16:30:00",
    updated_at: "2026-07-17 16:31:00",
    finished_at: null,
    message: `扫描 ${id} 运行中`,
  };
}

function installFakeTimers() {
  let nextId = 0;
  const scheduled = new Map();
  globalThis.setTimeout = (callback, delay = 0) => {
    const id = ++nextId;
    scheduled.set(id, { callback, delay });
    return id;
  };
  globalThis.clearTimeout = (id) => scheduled.delete(id);
  return {
    size: () => scheduled.size,
    async fireNext() {
      const entry = [...scheduled.entries()][0];
      assert.ok(entry, "expected a scheduled poll");
      scheduled.delete(entry[0]);
      entry[1].callback();
      await flushPromises();
    },
  };
}

async function flushPromises() {
  for (let index = 0; index < 50; index += 1) await Promise.resolve();
}
'''
    )


def test_market_scan_controller_uses_one_bounded_exponential_backoff_timer() -> None:
    _run_node_script(
        r'''
import assert from "node:assert/strict";
import { installAppDom } from "./tests/frontend_app_flow_helpers.mjs";
import { createMarketScanController } from "./static/js/market-scan.js";

installAppDom({ canvasContext: null });
const timers = installFakeTimers();
let latestCalls = 0;
let runCalls = 0;
const controller = createMarketScanController({
  root: document,
  pollIntervalMs: 5,
  maxPollIntervalMs: 12,
  failureFallbackThreshold: 5,
  async fetcher(url) {
    if (url === "/api/market-scans/latest") {
      latestCalls += 1;
      return scanRun(latestCalls === 1 ? 80 : 81);
    }
    if (url === "/api/market-scans/80") {
      runCalls += 1;
      throw new Error("临时网络失败");
    }
    if (url === "/api/market-scans/81") return scanRun(81);
    throw new Error(`unexpected request: ${url}`);
  },
});

await controller.activate();
assert.deepEqual(timers.delays(), [5]);
await timers.fireNext();
assert.deepEqual(timers.delays(), [5]);
await timers.fireNext();
assert.deepEqual(timers.delays(), [10]);
await timers.fireNext();
assert.deepEqual(timers.delays(), [12]);
await timers.fireNext();
assert.deepEqual(timers.delays(), [12]);
await timers.fireNext();
assert.equal(runCalls, 5);
assert.equal(latestCalls, 2);
assert.equal(controller.state.run.id, 81);
assert.equal(controller.state.consecutiveFailures, 0);
assert.deepEqual(timers.delays(), [5]);
controller.deactivate();
assert.deepEqual(timers.delays(), []);

function scanRun(id) {
  return {
    id,
    status: "running",
    trigger: "manual",
    rule_version: "full-market-score-v1",
    as_of: "2026-07-17 16:30:00",
    data_date: "2026-07-17",
    scope: "SH/SZ/BJ",
    total_count: 100,
    excluded_count: 0,
    processed_count: 1,
    success_count: 1,
    missing_count: 0,
    skipped_count: 0,
    retry_count: 0,
    progress_pct: 1,
    coverage_pct: 1,
    created_at: "2026-07-17 16:30:00",
    updated_at: "2026-07-17 16:31:00",
    finished_at: null,
    message: "扫描运行中",
  };
}

function installFakeTimers() {
  let nextId = 0;
  const scheduled = new Map();
  globalThis.setTimeout = (callback, delay = 0) => {
    const id = ++nextId;
    scheduled.set(id, { callback, delay });
    return id;
  };
  globalThis.clearTimeout = (id) => scheduled.delete(id);
  return {
    delays: () => [...scheduled.values()].map((entry) => entry.delay),
    async fireNext() {
      assert.equal(scheduled.size, 1, "polling must keep exactly one timer");
      const entry = [...scheduled.entries()][0];
      scheduled.delete(entry[0]);
      entry[1].callback();
      for (let index = 0; index < 50; index += 1) await Promise.resolve();
      assert.equal(scheduled.size, 1, "polling must reschedule exactly one timer");
    },
  };
}
'''
    )


def test_market_scan_controller_cancels_deferred_reset_when_deactivated() -> None:
    _run_node_script(
        r'''
import assert from "node:assert/strict";
import { installAppDom } from "./tests/frontend_app_flow_helpers.mjs";
import { createMarketScanController } from "./static/js/market-scan.js";

const { element } = installAppDom({ canvasContext: null });
let nextId = 0;
const timers = new Map();
globalThis.setTimeout = (callback, delay = 0) => {
  const id = ++nextId;
  timers.set(id, { callback, delay });
  return id;
};
globalThis.clearTimeout = (id) => timers.delete(id);
const terminal = {
  id: 30, status: "success", trigger: "manual", rule_version: "v1",
  as_of: "2026-07-17 16:30:00", data_date: "2026-07-17", scope: "SH/SZ/BJ",
  total_count: 0, excluded_count: 0, processed_count: 0, success_count: 0,
  missing_count: 0, skipped_count: 0, retry_count: 0, progress_pct: 100,
  coverage_pct: 0, created_at: "2026-07-17 16:30:00", updated_at: "2026-07-17 16:31:00",
  finished_at: "2026-07-17 16:31:00", message: "扫描完成",
};
let resultCalls = 0;
const controller = createMarketScanController({
  root: document,
  async fetcher(url) {
    if (url === "/api/market-scans/latest") return terminal;
    if (String(url).includes("/results?")) {
      resultCalls += 1;
      return { run: terminal, items: [], total: 0, page: 1, page_size: 100, page_count: 0 };
    }
    throw new Error(`unexpected request: ${url}`);
  },
});

await controller.activate();
assert.equal(resultCalls, 1);
element("marketScanFilters").listeners.reset();
assert.equal([...timers.values()].some((timer) => timer.delay === 0), true);
controller.deactivate();
assert.equal(timers.size, 0);
assert.equal(resultCalls, 1);
'''
    )


def test_market_scan_pagination_keeps_visible_content_and_stable_focus_while_loading() -> None:
    _run_node_script(
        r'''
import assert from "node:assert/strict";
import { installAppDom } from "./tests/frontend_app_flow_helpers.mjs";
import { createMarketScanController } from "./static/js/market-scan.js";

const { element } = installAppDom({ canvasContext: null });
for (const id of ["marketScanTableWrap", "marketScanMarket", "marketScanPrev", "marketScanNext"]) {
  element(id).focus = function focus() { document.activeElement = this; };
}
const terminal = {
  id: 40, status: "success", trigger: "manual", rule_version: "v1",
  as_of: "2026-07-17 16:30:00", data_date: "2026-07-17", scope: "SH/SZ/BJ",
  total_count: 101, excluded_count: 0, processed_count: 101, success_count: 101,
  missing_count: 0, skipped_count: 0, retry_count: 0, progress_pct: 100,
  coverage_pct: 100, created_at: "2026-07-17 16:30:00", updated_at: "2026-07-17 16:31:00",
  finished_at: "2026-07-17 16:31:00", message: "扫描完成",
};
const secondPage = deferred();
let resultCalls = 0;
const controller = createMarketScanController({
  root: document,
  pollIntervalMs: 60000,
  idlePollIntervalMs: 60000,
  async fetcher(url) {
    if (url === "/api/market-scans/latest") return terminal;
    if (String(url).includes("/results?")) {
      resultCalls += 1;
      if (resultCalls === 1) return page(1, stock("600519.SH", 1));
      return secondPage.promise;
    }
    throw new Error(`unexpected request: ${url}`);
  },
});

await controller.activate();
assert.equal(element("marketScanPagination").hidden, false);
assert.equal(element("marketScanNext").disabled, false);
document.activeElement = element("marketScanNext");
controller.state.page = 2;
const loading = controller.loadResults();
await Promise.resolve();
assert.equal(element("marketScanTableWrap").hidden, false, "page load hid the stable result region");
assert.equal(element("marketScanPagination").hidden, false, "page load hid pagination");
assert.equal(element("marketScanPagination")["aria-busy"], "true");
assert.equal(element("marketScanPrev").disabled, true);
assert.equal(element("marketScanNext").disabled, true);
assert.equal(document.activeElement, element("marketScanTableWrap"), "page load dropped focus");

secondPage.resolve(page(2, stock("920066.BJ", 101)));
await loading;
assert.equal(element("marketScanPagination")["aria-busy"], "false");
assert.equal(element("marketScanNext").disabled, true);
assert.equal(document.activeElement, element("marketScanTableWrap"), "terminal page dropped focus");
controller.deactivate();

function page(number, item) {
  return { run: terminal, total: 101, page: number, page_size: 100, page_count: 2, items: [item] };
}
function stock(symbol, rank) {
  const [code, market] = symbol.split(".");
  return {
    run_id: 40, rank, symbol, code, market, name: `股票${code}`, updated_at: "2026-07-17 16:31:00",
    status: "success", is_st: false, is_new: false, tags: [], metrics: {},
  };
}
function deferred() {
  let resolve;
  const promise = new Promise((done) => { resolve = done; });
  return { promise, resolve };
}
'''
    )


def test_market_scan_mutations_own_reads_busy_state_focus_and_duplicate_submissions() -> None:
    _run_node_script(
        r'''
import assert from "node:assert/strict";
import { installAppDom } from "./tests/frontend_app_flow_helpers.mjs";
import { createMarketScanController } from "./static/js/market-scan.js";

const { element } = installAppDom({ canvasContext: null });
const scheduled = new Map();
let timerId = 0;
globalThis.setTimeout = (callback, delay = 0) => {
  const id = ++timerId;
  scheduled.set(id, { callback, delay });
  return id;
};
globalThis.clearTimeout = (id) => scheduled.delete(id);
for (const id of ["marketScanStart", "marketScanCancel", "marketScanRetry", "marketScanMarket"]) {
  element(id).focus = function focus() { document.activeElement = this; };
}

const initialLatest = deferred();
const staleLatest = deferred();
const startResponse = deferred();
const cancelResponse = deferred();
const staleResult = deferred();
const retryResponse = deferred();
const signals = { latest: [], results: [] };
const calls = { latest: 0, start: 0, cancel: 0, retry: 0, results: 0 };
const running = scanRun(1, "running", "新扫描运行中");
const cancelled = scanRun(1, "cancelled", "扫描已取消");
const degraded = scanRun(3, "degraded", "降级完成");
const retried = scanRun(4, "running", "重试扫描运行中");
const controller = createMarketScanController({
  root: document,
  pollIntervalMs: 60000,
  idlePollIntervalMs: 60000,
  async fetcher(url, options = {}) {
    const target = String(url);
    if (target === "/api/market-scans/latest") {
      calls.latest += 1;
      signals.latest.push(options.signal);
      if (calls.latest === 1) return initialLatest.promise;
      if (calls.latest === 2) return staleLatest.promise;
      return retried;
    }
    if (target === "/api/market-scans") {
      calls.start += 1;
      return startResponse.promise;
    }
    if (target === "/api/market-scans/1/cancel") {
      calls.cancel += 1;
      return cancelResponse.promise;
    }
    if (target === "/api/market-scans/3/retry") {
      calls.retry += 1;
      return retryResponse.promise;
    }
    if (target.includes("/api/market-scans/3/results?")) {
      calls.results += 1;
      signals.results.push(options.signal);
      return staleResult.promise;
    }
    throw new Error(`unexpected request: ${target}`);
  },
});

const activation = controller.activate();
await flushPromises();
document.activeElement = element("marketScanStart");
const starting = controller.start();
assert.equal(document.activeElement, element("marketScanMarket"), "focused start was disabled without focus transfer");
assert.equal(await controller.start(), null, "duplicate start was not rejected");
assert.equal(calls.start, 1);
assert.equal(signals.latest[0].aborted, true, "start did not abort the old latest read");
assert.equal(controller.state.actionBusy, true);
assert.equal(element("workspace-panel-market-scan")["aria-busy"], "true");
assert.equal(element("marketScanProgressBar")["aria-busy"], "true");
assert.equal(element("marketScanStart").disabled, true);
assert.match(element("marketScanAnnouncement").textContent, /开始扫描请求处理中/);
initialLatest.resolve(scanRun(99, "success", "不应恢复的旧任务"));
await activation;
assert.equal(controller.state.run, null, "aborted latest response replaced state");
startResponse.resolve({ accepted: true, deduplicated: false, run: running });
await starting;
assert.equal(controller.state.run.id, 1);
assert.equal(controller.state.actionBusy, false);
assert.equal(element("workspace-panel-market-scan")["aria-busy"], "false");
assert.equal(element("marketScanStart").disabled, true, "busy completion re-enabled start for an active run");
assert.equal(scheduled.size, 1, "stale latest scheduled an extra poll");

const latestRead = controller.loadLatest();
await flushPromises();
document.activeElement = element("marketScanCancel");
const cancelling = controller.cancel();
assert.equal(await controller.cancel(), null, "duplicate cancel was not rejected");
assert.equal(calls.cancel, 1);
assert.equal(signals.latest[1].aborted, true, "cancel did not abort the old latest read");
staleLatest.resolve(scanRun(98, "success", "不应覆盖取消结果"));
await latestRead;
cancelResponse.resolve(cancelled);
await cancelling;
assert.equal(controller.state.run.status, "cancelled");
assert.equal(document.activeElement, element("marketScanMarket"), "focused cancel was hidden without focus transfer");
assert.equal(scheduled.size, 1, "stale cancel-era latest scheduled an extra poll");

controller.state.run = degraded;
const resultRead = controller.loadResults();
await flushPromises();
document.activeElement = element("marketScanRetry");
const retrying = controller.retry();
assert.equal(await controller.retry(), null, "duplicate retry was not rejected");
assert.equal(calls.retry, 1);
assert.equal(signals.results[0].aborted, true, "retry did not abort the old result read");
staleResult.resolve({});
await resultRead;
retryResponse.resolve({ accepted: true, deduplicated: false, run: retried });
await retrying;
assert.equal(controller.state.run.id, 4, "stale result replaced the retried run");
assert.equal(document.activeElement, element("marketScanMarket"), "focused retry was hidden without focus transfer");
assert.equal(element("marketScanRetry").hidden, true);
assert.equal(scheduled.size, 1, "stale result scheduled an extra poll");

await controller.activate();
await controller.activate();
assert.equal(calls.latest, 2, "repeated activation fetched latest again");
controller.deactivate();
await controller.activate();
assert.equal(calls.latest, 3, "reactivation after leaving did not refresh latest");
controller.deactivate();
assert.equal(scheduled.size, 0);

function scanRun(id, status, message) {
  const active = ["queued", "running", "cancelling"].includes(status);
  const published = ["success", "degraded"].includes(status);
  return {
    id, status, trigger: "manual", rule_version: "v1", as_of: "2026-07-17 16:30:00",
    data_date: "2026-07-17", scope: "SH/SZ/BJ", total_count: 1, excluded_count: 0,
    processed_count: active ? 0 : 1, success_count: published ? 1 : 0, missing_count: 0,
    skipped_count: 0, retry_count: 0, progress_pct: active ? 0 : 100,
    coverage_pct: published ? 100 : 0, created_at: "2026-07-17 16:30:00",
    updated_at: "2026-07-17 16:31:00", finished_at: active ? null : "2026-07-17 16:31:00", message,
  };
}
function deferred() {
  let resolve;
  const promise = new Promise((done) => { resolve = done; });
  return { promise, resolve };
}
async function flushPromises() {
  for (let index = 0; index < 20; index += 1) await Promise.resolve();
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
