from __future__ import annotations

import subprocess
from pathlib import Path

import pytest


ROOT = Path(__file__).resolve().parents[1]


@pytest.mark.parametrize("mode", ["official", "intraday", "preopen"])
def test_history_refresh_does_not_wait_for_unused_latest_publication(mode: str) -> None:
    _run_node(
        f'const mode = "{mode}";\n'
        + _HARNESS
        + r"""
await controller.activate();
await selectHistory();
const selected = controller.state.publishedRun;
const rendered = element("marketScanTableWrap").dataset.marketScanRunId;
calls.length = 0;
failPublished = true;
latest = { ...latest, id: 51 };
assert.equal((await controller.loadLatest()).id, 51);
assert.deepEqual(coreCalls(), [identityUrl, "/api/market-scans/latest", identityUrl]);
assert.equal(controller.state.publishedRun, selected);
assert.equal(controller.state.selectedHistoryRunId, history.id);
assert.equal(element("marketScanTableWrap").dataset.marketScanRunId, rendered);
assert.match(element("marketScanBrowseContext").textContent, /历史批次 #30/);

failPublished = false;
calls.length = 0;
element("marketScanHistoryRun").value = "";
element("marketScanHistoryRun").listeners.change();
await flush();
assert.equal(controller.state.selectedHistoryRunId, null);
assert.equal(controller.state.publishedRun.id, published.id);
assert.deepEqual(coreCalls().map((url) => url.split("?", 1)[0]), [
  "/api/market-scans/polling-identity", "/api/market-scans/latest",
  "/api/market-scans/latest-published", `/api/market-scans/${published.id}/results`,
  "/api/market-scans/polling-identity",
]);
controller.deactivate();
"""
    )


def test_history_refresh_keeps_identity_fence_and_commits_only_stable_task() -> None:
    _run_node(
        'const mode = "official";\n'
        + _HARNESS
        + r"""
await controller.activate();
await selectHistory();
calls.length = 0;
const originalTask = controller.state.run;
let reads = 0;
identityHook = () => {
  reads += 1;
  assert.equal(controller.state.run, originalTask, "unstable task committed before the identity fence");
  latest = { ...latest, id: reads === 1 ? 51 : 52 };
  return marketScanPollingIdentity(latest, published, mode);
};
assert.equal((await controller.loadLatest()).id, 52);
assert.equal(reads, 3, "history refresh must retry an identity change then recheck the stable pair");
assert.deepEqual(coreCalls(), [
  identityUrl, "/api/market-scans/latest", identityUrl, "/api/market-scans/latest", identityUrl,
]);
assert.equal(controller.state.publishedRun.id, history.id);
assert.equal(element("marketScanTableWrap").dataset.marketScanRunId, String(history.id));
controller.deactivate();
"""
    )


def test_history_refresh_still_rejects_invalid_latest_task() -> None:
    _run_node(
        'const mode = "official";\n'
        + _HARNESS
        + r"""
await controller.activate();
await selectHistory();
const originalTask = controller.state.run;
calls.length = 0;
latest = { ...latest, id: 51, processed_count: -1 };
assert.equal(await controller.loadLatest(), null);
assert.deepEqual(coreCalls(), [identityUrl, "/api/market-scans/latest"]);
assert.equal(controller.state.run, originalTask);
assert.equal(controller.state.publishedRun.id, history.id);
assert.match(element("marketScanHeadline").textContent, /processed_count/);
controller.deactivate();
"""
    )


def test_latest_publication_keeps_date_selector_when_newest_id_is_a_backfill() -> None:
    _run_node(
        'const mode = "official";\n'
        + _HARNESS
        + r"""
latest = scanRun(50, "2026-08-12");
await controller.activate();
assert.equal(controller.state.run.id, 50);
assert.equal(controller.state.publishedRun.id, 31);
assert.equal(element("marketScanTableWrap").dataset.marketScanRunId, "31");
assert.equal(coreCalls().includes(publishedUrl), true);
assert.equal(coreCalls().some((url) => url.startsWith("/api/market-scans/50/results?")), false);
controller.deactivate();
"""
    )


_HARNESS = r"""
import assert from "node:assert/strict";
import { installAppDom, marketScanPollingIdentity } from "./tests/frontend_app_flow_helpers.mjs";
import { createMarketScanController } from "./static/js/market-scan.js";

const { element } = installAppDom({ canvasContext: null });
const history = scanRun(30, "2026-08-13");
const published = scanRun(31, "2026-08-14");
let latest = {
  ...scanRun(50, "2026-08-14"), status: "running", finished_at: null,
  snapshot_digest: null, snapshot_seal_origin: null, snapshot_sealed_at: null,
  processed_count: 0, success_count: 0, progress_pct: 0, coverage_pct: 0,
};
const calls = [];
const identityUrl = `/api/market-scans/polling-identity?mode=${mode}`;
const publishedUrl = `/api/market-scans/latest-published?mode=${mode}`;
let failPublished = false;
let identityHook = null;
const controller = createMarketScanController({
  root: document,
  now: new Date(2026, 7, 14, { official: 16, intraday: 10, preopen: 8 }[mode], 0),
  pollIntervalMs: 60000,
  async fetcher(url) {
    const target = String(url);
    calls.push(target);
    if (target === identityUrl) return identityHook?.() || marketScanPollingIdentity(latest, published, mode);
    if (target === "/api/market-scans/latest") return { ...latest };
    if (target === publishedUrl) {
      if (failPublished) throw Object.assign(new Error("unrelated publication unavailable"), { status: 409 });
      return { ...published };
    }
    if (target.startsWith("/api/market-scans?")) {
      return { items: [{ ...published }, { ...history }], total: 2, page: 1, page_size: 100, page_count: 1 };
    }
    if (target === `/api/market-scans/${history.id}`) return { ...history };
    const match = /^\/api\/market-scans\/(\d+)\/results\?/.exec(target);
    if (match) {
      const run = [history, published, latest].find((item) => item.id === Number(match[1]));
      return { run: { ...run }, items: [], total: 0, page: 1, page_size: 100, page_count: 0 };
    }
    throw new Error(`unexpected request: ${target}`);
  },
});

async function selectHistory() {
  element("marketScanHistoryRun").value = String(history.id);
  element("marketScanHistoryRun").listeners.change();
  await flush();
  assert.equal(controller.state.selectedHistoryRunId, history.id);
  assert.equal(element("marketScanTableWrap").dataset.marketScanRunId, String(history.id));
}
function coreCalls() {
  return calls.filter((url) => /^\/api\/market-scans\/(?:polling-identity\?|latest|\d+\/results\?)/.test(url));
}
async function flush() {
  for (let index = 0; index < 120; index += 1) await Promise.resolve();
}
function scanRun(id, day) {
  return {
    id, status: "success", trigger: "manual", mode, rule_version: "full-market-score-v5",
    as_of: `${day} 16:30:00`, data_date: day, quote_date: day,
    scope: "沪市 + 深市 + 北交所当前上市A股", total_count: 1, excluded_count: 0,
    processed_count: 1, success_count: 1, missing_count: 0, skipped_count: 0, retry_count: 0,
    progress_pct: 100, coverage_pct: 100, created_at: `${day} 16:30:00`,
    updated_at: `${day} 16:31:00`, finished_at: `${day} 16:31:00`,
    snapshot_digest: String(id % 10).repeat(64), snapshot_seal_origin: "publication",
    snapshot_sealed_at: `${day} 16:31:00`,
  };
}
"""


def _run_node(script: str) -> None:
    result = subprocess.run(
        ["node", "--input-type=module", "-e", script],
        cwd=ROOT,
        check=False,
        capture_output=True,
        text=True,
        timeout=15,
    )
    assert result.returncode == 0, result.stderr or result.stdout
