from __future__ import annotations

import json
from pathlib import Path
import subprocess

import pytest

from app.models.strategy_lab import StrategySpecInput


ROOT = Path(__file__).resolve().parents[1]


def test_confirmed_save_survives_readback_failure_without_creating_another_strategy() -> None:
    _run_node(r'''
      const writes = [];
      const controller = makeController(async (url, options) => {
        if (options.method) {
          writes.push({ url, method: options.method });
          return strategy(7, writes.length);
        }
        throw new Error("list unavailable");
      });
      controller.state.spec = structuredClone(spec);
      controller.state.compileExecutable = true;
      const saved = await click("strategySave");
      assert.equal(saved?.strategy_id, 7, "confirmed save was reported as failed");
      assert.match(status().textContent, /已保存.*同步未完成/);
      assert.equal(status().dataset.kind, "warn");
      assert.equal(element("strategySavedSelect").value, "7");
      await click("strategySave");
      assert.deepEqual(writes.map(item => item.method), ["POST", "PUT"]);
      assert.equal(writes[1].url, "/api/strategy-lab/strategies/7");
    ''')


@pytest.mark.parametrize("action", ["strategyCopy", "strategyArchive"])
def test_confirmed_strategy_mutation_keeps_success_when_list_refresh_fails(action: str) -> None:
    _run_node(r'''
      let writes = 0;
      const controller = makeController(async (url, options) => {
        if (url.endsWith("/compile")) return { normalized_spec: spec, fingerprint: "a".repeat(64), execution_plan: { executable: true } };
        if (!options.method) throw new Error("list unavailable");
        writes += 1;
        return { ...strategy(8), archived: ACTION === "strategyArchive" };
      });
      loadEditor(controller, strategy(7));
      const result = await click(ACTION);
      assert.equal(result?.strategy_id, 8, "confirmed mutation was reported as failed");
      assert.equal(writes, 1);
      assert.equal(controller.state.strategy.strategy_id, 8);
      assert.match(status().textContent, /同步未完成/);
      assert.equal(status().dataset.kind, "warn");
    '''.replace("ACTION", json.dumps(action)))


def test_confirmed_execution_keeps_result_when_candidate_readback_fails() -> None:
    _run_node(r'''
      const controller = makeController(async (url, options) => {
        if (options.method) return draft();
        if (url.includes("/candidates?")) throw new Error("candidate page unavailable");
        return emptyCollection(url);
      });
      loadEditor(controller, strategy(7));
      const result = await controller.execute("latest_scan");
      assert.equal(result?.context.execution_id, 9, "confirmed execution was reported as failed");
      assert.equal(controller.state.execution.context.execution_id, 9);
      assert.equal(element("strategyCreateSimulation").disabled, false);
      assert.match(status().textContent, /执行完成.*同步未完成/);
      assert.equal(status().dataset.kind, "warn");
    ''')


def test_save_uses_loaded_editor_identity_after_only_changing_history_selection() -> None:
    _run_node(r'''
      const writes = [];
      const controller = makeController(async (url, options) => {
        if (options.method) {
          writes.push({ url, method: options.method });
          return strategy(7, 2);
        }
        return emptyCollection(url);
      });
      loadEditor(controller, strategy(7));
      element("strategySavedSelect").value = "42";
      await click("strategySave");
      assert.deepEqual(writes, [{ url: "/api/strategy-lab/strategies/7", method: "PUT" }]);
    ''')


def test_failed_candidate_page_can_be_retried_without_skipping_a_page() -> None:
    _run_node(r'''
      const pages = [];
      const controller = makeController(async (url) => {
        const page = Number(new URL(url, "http://local").searchParams.get("page"));
        pages.push(page);
        if (pages.length === 1) throw new Error("temporary page failure");
        return candidatePage(page);
      });
      loadCandidates(controller);
      await clickAndSettle("strategyCandidateNext", controller);
      assert.equal(controller.state.candidatePageNumber, 1, "failed read advanced the page");
      await clickAndSettle("strategyCandidateNext", controller);
      assert.deepEqual(pages, [2, 2]);
      assert.equal(controller.state.candidatePageNumber, 2);
      assert.match(element("strategyCandidatePage").textContent, /第 2/);
    ''')


def test_failed_candidate_sort_preserves_the_displayed_sort_and_page() -> None:
    _run_node(r'''
      const controller = makeController(async () => { throw new Error("sort unavailable"); });
      loadCandidates(controller, 2);
      await clickAndSettle("sort-risk", controller);
      assert.equal(controller.state.candidatePageNumber, 2);
      assert.equal(controller.state.candidateSort, "utility_score");
      assert.equal(element("sort-risk").attributes["aria-pressed"], "false");
      assert.equal(element("sort-utility_score").attributes["aria-pressed"], "true");
    ''')


def test_rejected_save_keeps_unsaved_draft_and_reports_no_confirmation() -> None:
    _run_node(r'''
      let requests = 0;
      const controller = makeController(async () => { requests += 1; throw Object.assign(new Error("保存被拒绝"), { status: 422 }); });
      controller.state.spec = structuredClone(spec);
      controller.state.compileExecutable = true;
      element("strategyName").value = "尚未保存的修改";
      certifyEditor(controller, strategySpecFromEditor(root, controller.state.spec));
      assert.equal(await click("strategySave"), null);
      assert.equal(controller.state.strategy, null);
      assert.equal(element("strategyName").value, "尚未保存的修改");
      assert.equal(element("strategySave").disabled, false);
      assert.equal(status().dataset.kind, "error");
      assert.equal(requests, 1, "failed write triggered a readback");
    ''')


def test_confirmed_save_waits_for_all_readbacks_then_allows_read_only_recovery() -> None:
    _run_node(r'''
      const pending = deferred();
      const started = deferred();
      let unavailable = true;
      let writes = 0;
      const controller = makeController(async (url, options) => {
        if (options.method) { writes += 1; return strategy(7); }
        if (url.includes("/strategies?")) {
          if (unavailable) throw new Error("list unavailable");
          return { items: [strategy(7)], total: 1, page: 1, page_size: 100, page_count: 1 };
        }
        if (unavailable && url.includes("/executions?")) {
          started.resolve();
          return pending.promise;
        }
        return emptyCollection(url);
      });
      controller.state.spec = structuredClone(spec);
      controller.state.compileExecutable = true;
      const saving = click("strategySave");
      await started.promise;
      await new Promise(resolve => setImmediate(resolve));
      assert.equal(controller.state.busy, true, "one failed read released an active operation");
      pending.resolve({ items: [], total: 0, page: 1, page_size: 100, page_count: 0 });
      assert.equal((await saving).strategy_id, 7);
      unavailable = false;
      await controller.loadStrategies();
      await click("strategyHistoryRefresh");
      assert.equal(writes, 1, "recovery repeated the persisted mutation");
      assert.equal(status().dataset.kind, "ready");
      assert.equal(element("strategySavedSelect").value, "7");
    ''')


def test_failed_execution_readback_clears_old_candidates_and_recovers_through_sort() -> None:
    _run_node(r'''
      let unavailable = true;
      let writes = 0;
      const controller = makeController(async (url, options) => {
        if (options.method) { writes += 1; return draft(); }
        if (url.includes("/candidates?")) {
          if (unavailable) throw new Error("candidate unavailable");
          return candidatePage(1);
        }
        return emptyCollection(url);
      });
      loadCandidates(controller, 2);
      element("strategyCandidateRows").innerHTML = "old execution candidates";
      await controller.execute("latest_scan");
      assert.equal(controller.state.candidatePage, null);
      assert.equal(element("strategyCandidateRows").innerHTML.includes("old execution"), false);
      assert.equal(element("strategyCandidateNext").disabled, true);
      unavailable = false;
      await clickAndSettle("sort-utility_score", controller);
      assert.equal(controller.state.candidatePage.page, 1);
      assert.equal(writes, 1);
    ''')


def test_busy_page_and_sort_clicks_do_not_change_the_pending_request() -> None:
    _run_node(r'''
      const pending = deferred();
      const calls = [];
      const controller = makeController(async (url) => { calls.push(url); return pending.promise; });
      loadCandidates(controller);
      click("strategyCandidateNext");
      click("strategyCandidateNext");
      click("sort-risk");
      assert.equal(controller.state.candidatePageNumber, 1);
      assert.equal(controller.state.candidateSort, "utility_score");
      pending.resolve(candidatePage(2));
      await new Promise(resolve => setImmediate(resolve));
      assert.equal(controller.state.busy, false);
      assert.equal(controller.state.candidatePageNumber, 2);
      assert.equal(controller.state.candidateSort, "utility_score");
      assert.equal(calls.length, 1);
    ''')


def test_successful_sort_commits_first_page_and_rejected_page_identity_does_not() -> None:
    _run_node(r'''
      const controller = makeController(async () => candidatePage(1));
      loadCandidates(controller, 2);
      await clickAndSettle("sort-risk", controller);
      assert.equal(controller.state.candidatePageNumber, 1);
      assert.equal(controller.state.candidateSort, "risk");
      assert.equal(element("sort-risk").attributes["aria-pressed"], "true");
      await clickAndSettle("strategyCandidateNext", controller);
      assert.equal(controller.state.candidatePageNumber, 1);
      assert.match(status().textContent, /页码不一致/);
    ''')


def _run_node(body: str) -> None:
    payload = StrategySpecInput(name="恢复测试").model_dump(mode="json")
    script = _HARNESS + "\n" + body
    completed = subprocess.run(
        ["node", "--input-type=module", "-e", script, json.dumps(payload, ensure_ascii=False)],
        cwd=ROOT, capture_output=True, text=True, check=False,
    )
    assert completed.returncode == 0, completed.stderr


_HARNESS = r'''
import assert from "node:assert/strict";
import { createStrategyLabController } from "./static/js/strategy-lab-controller.js";
import { syncStrategyEditor, strategySpecFromEditor } from "./static/js/strategy-lab-contracts.js";
import { strategyDraftKey } from "./static/js/strategy-draft-state.js";
import { renderCandidatePage } from "./static/js/strategy-lab-view.js";
const spec = JSON.parse(process.argv[1]);
const elements = new Map();
function element(id) {
  if (!elements.has(id)) {
    const value = {
      value: "", textContent: "", dataset: {}, attributes: {}, disabled: false,
      handlers: new Map(), hidden: false, checked: true,
      addEventListener(name, handler) { this.handlers.set(name, handler); },
      setAttribute(name, value) { this.attributes[name] = value; },
      querySelectorAll() { return []; },
      classList: { toggle() {} },
      set innerHTML(html) {
        this.html = html;
        if (id === "strategySavedSelect") this.value = html.match(/value="([^"]*)"/)?.[1] || "";
      },
      get innerHTML() { return this.html || ""; },
    };
    elements.set(id, value);
  }
  return elements.get(id);
}
const boards = spec.universe.boards.map(value => ({ value, checked: true }));
const sorts = ["utility_score", "risk"].map(name => {
  const button = element(`sort-${name}`);
  button.dataset.strategySort = name;
  button.setAttribute("aria-pressed", String(name === "utility_score"));
  return button;
});
const root = {
  getElementById: element,
  querySelectorAll(selector) {
    if (selector.startsWith("[data-strategy-board]")) return boards;
    if (selector === "[data-strategy-sort]") return sorts;
    return [];
  },
};
function makeController(fetcher) {
  const controller = createStrategyLabController({ root, fetcher });
  syncStrategyEditor(root, spec);
  element("strategyNotional").value = "100000";
  certifyEditor(controller);
  return controller;
}
function certifyEditor(controller, compiled = controller.state.spec || spec) {
  Object.assign(controller.state, { compiledSpec: structuredClone(compiled), compiledFingerprint: "a".repeat(64),
    compiledEditorKey: strategyDraftKey(strategySpecFromEditor(root, compiled)) });
}
function strategy(id, revision = 1) {
  return { strategy_id: id, strategy_version: revision, revision, fingerprint: "a".repeat(64),
    archived: false, spec: structuredClone(spec) };
}
function loadEditor(controller, saved) {
  Object.assign(controller.state, { strategy: saved, spec: saved.spec, compileExecutable: true });
  element("strategySavedSelect").value = String(saved.strategy_id);
  certifyEditor(controller);
}
function draft() {
  return { context: { execution_id: 9, strategy_id: 7, strategy_version: 1,
    market_scan_run_id: 3, data_date: "2026-09-04", strategy_fingerprint: "a".repeat(64),
    execution_fingerprint: "b".repeat(64) }, selected: [],
    summary: { status: "no_trade", no_trade: true, no_trade_reasons: [], selected_count: 0 } };
}
function candidatePage(page) { return { items: [], page, page_count: 3, total: 125 }; }
function emptyCollection(url) {
  // Paginated route fixtures carry the same page metadata as the API response model.
  return /\/(strategies|executions)\?/.test(url)
    ? { items: [], total: 0, page: 1, page_size: 100, page_count: 0 }
    : { items: [], total: 0 };
}
function loadCandidates(controller, page = 1) {
  loadEditor(controller, strategy(7));
  Object.assign(controller.state, {
    execution: draft(), candidatePageNumber: page, candidatePage: candidatePage(page),
  });
  renderCandidatePage(Object.fromEntries(elements), candidatePage(page));
}
function status() { return element("strategyLabStatus"); }
function deferred() {
  let resolve;
  const promise = new Promise(done => { resolve = done; });
  return { promise, resolve };
}
function click(id) {
  const currentTarget = element(id);
  return currentTarget.handlers.get("click")({ currentTarget, target: currentTarget });
}
async function clickAndSettle(id, controller) {
  await click(id);
  for (let i = 0; i < 30 && controller.state.busy; i += 1) {
    await new Promise(resolve => setImmediate(resolve));
  }
  assert.equal(controller.state.busy, false, "operation did not settle");
}
'''
