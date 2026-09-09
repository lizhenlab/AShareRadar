from __future__ import annotations

import subprocess
from pathlib import Path

import pytest


ROOT = Path(__file__).resolve().parents[1]


def test_history_pages_reach_older_same_day_runs_without_reloading_results() -> None:
    _run_node(_HARNESS + r"""
await controller.activate();
const baseline = detailReads.length;
assert.equal(element("marketScanHistoryNext").disabled, false);
await click("marketScanHistoryNext");
assert.equal(queries.at(-1).get("page"), "2");
assert.equal(controller.state.historyRuns.length, 100);
await click("marketScanHistoryNext");
assert.equal(queries.at(-1).get("page"), "3");
assert.match(element("marketScanHistoryRun").innerHTML, /value="1"/);
assert.equal(element("marketScanHistoryNext").disabled, true);
assert.equal(detailReads.length, baseline, "navigation must not load any trusted result graph");
element("marketScanHistoryRun").value = "1";
await click("marketScanHistoryRun", "change");
assert.equal(controller.state.selectedHistoryRunId, 1);
assert.equal(controller.state.publishedRun.id, 1);
assert.deepEqual(detailReads.slice(baseline).map((url) => url.split("?", 1)[0]), [
  "/api/market-scans/1", "/api/market-scans/1/results",
]);
await click("marketScanHistoryPrev");
assert.equal(controller.state.selectedHistoryRunId, 1);
assert.equal(element("marketScanHistoryRun").value, "1");
assert.match(element("marketScanHistoryRun").innerHTML, /当前浏览（不在本次查询中）/);
assert.equal(controller.state.historyPage, 2);
controller.deactivate();
""")


def test_history_page_failure_preserves_navigation_and_can_retry() -> None:
    _run_node(_HARNESS + r"""
await controller.activate();
const first = element("marketScanHistoryRun").innerHTML;
listFailure = true;
await click("marketScanHistoryNext");
assert.equal(controller.state.historyPage, 1);
assert.equal(element("marketScanHistoryRun").innerHTML, first);
assert.match(element("marketScanHistoryFeedback").textContent, /读取失败/);
assert.equal(element("marketScanHistoryNext").disabled, false);
assert.equal(element("marketScanHistoryRefresh").disabled, false);
listFailure = false;
await click("marketScanHistoryNext");
assert.equal(controller.state.historyPage, 2);
assert.equal(element("marketScanHistoryFeedback").className, "");
controller.deactivate();
""")


def test_history_query_resets_page_and_retention_shrink_recovers_with_bounded_reads() -> None:
    _run_node(_HARNESS + r"""
await controller.activate();
await click("marketScanHistoryNext");
element("marketScanHistoryDate").value = "2026-08-14";
await click("marketScanHistoryRefresh");
assert.equal(queries.at(-1).get("page"), "1");
assert.equal(queries.at(-1).get("data_date"), "2026-08-14");
await click("marketScanHistoryNext");
const before = queries.length;
total = 1;
await click("marketScanHistoryNext");
assert.deepEqual(queries.slice(before).map((query) => query.get("page")), ["3", "1"]);
assert.equal(controller.state.historyPage, 1);
assert.equal(element("marketScanHistoryNext").disabled, true);
assert.equal(element("marketScanHistoryPagination").hidden, true);
controller.deactivate();
""")


def test_history_mode_round_trip_during_slow_selection_restores_navigation_and_tracking() -> None:
    _run_node(_HARNESS + r"""
await controller.activate();
const original = element("marketScanHistoryRun").innerHTML;
delayedDetail = deferred();
element("marketScanHistoryRun").value = "200";
element("marketScanHistoryRun").listeners.change();
await flush();
element("marketScanModeOfficial").checked = false;
element("marketScanModePreopen").checked = true;
await click("marketScanModePreopen", "change");
element("marketScanModePreopen").checked = false;
element("marketScanModeOfficial").checked = true;
await click("marketScanModeOfficial", "change");
delayedDetail.resolve(scanRun(200));
await flush();
assert.equal(element("marketScanHistoryRun").innerHTML, original);
assert.equal(controller.state.publishedRun.id, 201);
assert.equal(controller.state.selectedHistoryRunId, null);
assert.notEqual(controller.state.pollTimer, null);
controller.deactivate();
""")


def test_history_abort_restores_query_button_and_ignores_late_page() -> None:
    _run_node(_HARNESS + r"""
await controller.activate();
const original = element("marketScanHistoryRun").innerHTML;
delayedList = deferred();
element("marketScanHistoryRefresh").listeners.click();
await flush();
assert.equal(element("marketScanHistoryRefresh").disabled, true);
controller.setVisible(false);
assert.equal(element("marketScanHistoryRefresh").disabled, false, "an aborted request left history permanently disabled");
delayedList.resolve(pagePayload(1));
await flush();
assert.equal(element("marketScanHistoryRun").innerHTML, original);
assert.equal(element("marketScanHistory")["aria-busy"], "false");
controller.deactivate();
""")


@pytest.mark.parametrize("wrong_page", [1, 3])
def test_history_rejects_response_for_another_page(wrong_page: int) -> None:
    _run_node(_HARNESS + r"""
await controller.activate();
""" + f'wrongPage = {wrong_page};\n' + r"""
await click("marketScanHistoryNext");
assert.equal(controller.state.historyPage, 1);
assert.match(element("marketScanHistoryFeedback").textContent, /分页/);
assert.equal(element("marketScanHistoryNext").disabled, false);
controller.deactivate();
""")


_HARNESS = r"""
import assert from "node:assert/strict";
import { installAppDom, marketScanPollingIdentity } from "./tests/frontend_app_flow_helpers.mjs";
import { createMarketScanController } from "./static/js/market-scan.js";
const { element } = installAppDom({ canvasContext: null });
let total = 201, listFailure = false, wrongPage = null, delayedList = null, delayedDetail = null;
const queries = [], detailReads = [];
const published = scanRun(201);
const controller = createMarketScanController({
  root: document, now: new Date(2026, 7, 14, 16), pollIntervalMs: 60000,
  async fetcher(url) {
    if (url.startsWith("/api/market-scans/polling-identity")) return marketScanPollingIdentity(published, published, "official");
    if (url.startsWith("/api/market-scans/latest")) return { ...published };
    if (url.startsWith("/api/market-scans?")) {
      const params = new URLSearchParams(url.split("?", 2)[1]);
      queries.push(params);
      if (delayedList) return delayedList.promise;
      if (listFailure) throw new Error("history unavailable");
      return pagePayload(wrongPage || Number(params.get("page")));
    }
    detailReads.push(url);
    const match = /\/market-scans\/(\d+)/.exec(url);
    if (match && url.includes("/results")) return {
      run: scanRun(Number(match[1])), items: [], total: 0, page: 1, page_size: 100, page_count: 0,
    };
    if (match) return delayedDetail ? delayedDetail.promise : scanRun(Number(match[1]));
    throw new Error(`unexpected request: ${url}`);
  },
});
function pagePayload(page) {
  const ids = Array.from({ length: total }, (_, index) => total - index);
  return { items: ids.slice((page - 1) * 100, page * 100).map(scanRun), total, page, page_size: 100, page_count: Math.ceil(total / 100) };
}
function scanRun(id) {
  const day = "2026-08-14";
  return {
    id, status: "success", trigger: "manual", mode: "official", rule_version: "full-market-score-v5",
    as_of: `${day} 16:30:00`, data_date: day, quote_date: day, scope: "沪市 + 深市 + 北交所当前上市A股",
    total_count: 1, excluded_count: 0, processed_count: 1, success_count: 1, missing_count: 0,
    skipped_count: 0, retry_count: 0, progress_pct: 100, coverage_pct: 100,
    created_at: `${day} 16:30:00`, updated_at: `${day} 16:31:00`, finished_at: `${day} 16:31:00`,
    snapshot_digest: String(id % 10).repeat(64), snapshot_seal_origin: "publication", snapshot_sealed_at: `${day} 16:31:00`,
  };
}
async function flush() { for (let index = 0; index < 200; index += 1) await Promise.resolve(); }
async function click(id, event = "click") {
  assert.equal(typeof element(id).listeners?.[event], "function", `${id} has no user action`);
  element(id).listeners[event]();
  await flush();
}
function deferred() { let resolve; const promise = new Promise((done) => { resolve = done; }); return { promise, resolve }; }
"""


def _run_node(script: str) -> None:
    result = subprocess.run(
        ["node", "--input-type=module", "-e", script], cwd=ROOT,
        check=False, capture_output=True, text=True, timeout=15,
    )
    assert result.returncode == 0, result.stderr or result.stdout
