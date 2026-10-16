from __future__ import annotations

import subprocess
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]


def test_selection_binding_refresh_and_stale_controls_are_explicit():
    script = r'''
      import assert from "node:assert/strict";
      import { acceptSelection, applySelectionParams, bindSelectionControls, markSelectionConflict,
        notifySelectionMembers, requireSelectionBinding, selectionGeneration, selectionRequest,
        selectionExportUrl, setSelectionReload } from "./static/js/market-scan-selection.js";
      import { screenSpecFromPreset, screenSpecResultsQuery } from "./static/js/market-scan-screen-spec-source.js";
      const handlers = {};
      const element = id => ({ value: "watchlist", addEventListener: (event, fn) => { handlers[`${id}:${event}`] = fn; } });
      const elements = { universe: element("scope"), scopeRefresh: element("refresh"), scopeStatus: {}, tableWrap: {}, prev: {}, next: {}, exportButton: {} };
      const scope = { universe: "watchlist", watchlist_digest: "a".repeat(64), member_count: 3 };
      const page = { run: { id: 42 }, selection_scope: scope, total: 2 };
      let calls = 0;
      bindSelectionControls(elements, () => { calls += 1; });
      acceptSelection(elements, page, { universe: "watchlist" }, "/api/market-scans/42/results?page=2&min_score=80");
      assert.deepEqual(selectionRequest(elements), { universe: "watchlist", watchlist_digest: scope.watchlist_digest });
      assert.match(selectionExportUrl(elements, 42, () => "unused"), /export.xlsx\?min_score=80&universe=watchlist&watchlist_digest=/);
      assert.throws(() => selectionExportUrl(elements, 99, () => "unused"), /确认/);
      for (const bad of [null, { ...scope, watchlist_digest: "b".repeat(64) }, { ...scope, member_count: 1 }]) {
        assert.throws(() => requireSelectionBinding({ ...page, selection_scope: bad }, selectionRequest(elements)));
      }
      const generation = selectionGeneration(elements);
      notifySelectionMembers(elements, { items: [{ symbol: "a", note: "changed" }], previousItems: [{ symbol: "a" }] });
      assert.equal(selectionGeneration(elements), generation);
      notifySelectionMembers(elements, { items: [], previousItems: [{ symbol: "a" }] });
      assert.ok(selectionGeneration(elements) > generation);
      assert.throws(() => selectionRequest(elements), /刷新自选范围/);
      assert.ok(elements.next.disabled && elements.exportButton.disabled);
      assert.match(elements.scopeStatus.textContent, /保留的是刷新前结果/);
      setSelectionReload(elements, () => { calls += 10; });
      handlers["refresh:click"]();
      assert.equal(calls, 10);
      assert.deepEqual(selectionRequest(elements), { universe: "watchlist" });
      assert.match(elements.scopeStatus.textContent, /待确认/);
      assert.throws(() => selectionExportUrl(elements, 42, () => "unused"), /确认/);
      acceptSelection(elements, { ...page, total: 0, selection_scope: { ...scope, member_count: 0 } }, { universe: "watchlist" });
      assert.match(elements.scopeStatus.textContent, /当前自选 0 只/);
      assert.equal(markSelectionConflict(elements, { status: 409, message: "其他冲突" }), false);
      assert.equal(markSelectionConflict(elements, { status: 409, message: "自选成员已变化" }), true);
      elements.universe.value = "all";
      const untouched = { universe: { value: "all" }, tableWrap: {}, exportButton: { disabled: true } };
      acceptSelection(untouched, { run: { id: 42 }, total: 2 }, {}, "/api/market-scans/42/results");
      assert.equal(untouched.exportButton.disabled, true);
      const spec = screenSpecFromPreset({ criteria: { confidence: { max: 95 }, risk: { min: 10 }, tradability: { max: 80 } }, sort: [{ field: "market", order: "desc" }] });
      acceptSelection(untouched, { run: { id: 42 }, total: 2 }, {}, screenSpecResultsQuery(spec, 42));
      const allExport = new URL(selectionExportUrl(untouched, 42, () => "unused"), "http://localhost");
      assert.equal(allExport.pathname, "/api/market-scans/42/export.xlsx");
      assert.equal(allExport.searchParams.get("ranking_basis"), "frozen");
      assert.deepEqual(JSON.parse(allExport.searchParams.get("screen_spec")), spec);
      assert.equal(allExport.searchParams.has("universe"), false);
      assert.equal(selectionExportUrl(untouched, 99, () => "fallback"), "fallback");
      acceptSelection(untouched, { run: { id: 42 }, total: 2 }, {}, "/api/market-scans/42/results?min_score=20");
      assert.equal(selectionExportUrl(untouched, 42, () => "current-draft"), "current-draft");
      assert.equal(applySelectionParams(elements, new URLSearchParams("universe=watchlist&watchlist_digest=old&sort=rank")).toString(), "sort=rank");
    '''
    result = subprocess.run(["node", "--input-type=module", "--eval", script], cwd=ROOT, text=True, capture_output=True, timeout=10)
    assert result.returncode == 0, result.stdout + result.stderr
