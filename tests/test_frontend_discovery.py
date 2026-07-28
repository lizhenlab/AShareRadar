from __future__ import annotations

import json
from pathlib import Path
import subprocess
import textwrap


ROOT = Path(__file__).resolve().parents[1]


def test_discovery_controls_are_wired_into_the_existing_market_scan_surface() -> None:
    index = (ROOT / "static/index.html").read_text(encoding="utf-8")
    app = (ROOT / "static/app.js").read_text(encoding="utf-8")

    assert '"/static/js/discovery.js"' in index
    for element_id in (
        "discoveryPresetControls",
        "discoveryPresetSelect",
        "discoveryPresetName",
        "discoveryPresetSave",
        "discoveryPresetApply",
        "discoveryPresetRename",
        "discoveryPresetDelete",
        "discoveryPresetFeedback",
        "discoveryRankSummary",
    ):
        assert f'id="{element_id}"' in index
    assert 'from "./js/discovery.js"' in app
    assert "createDiscoveryController" in app


def test_discovery_payload_uses_only_existing_supported_filter_fields() -> None:
    output = _run_node(
        r'''
          import {
            buildDiscoveryPresetDefinition,
            isDiscoveryPresetUiRepresentable,
            normalizeDiscoveryLeaderboard,
            rankChangeLabel,
          } from "./static/js/discovery.js";

          const value = (value) => ({ value });
          const elements = {
            market: value("SH"),
            industry: value("白酒"),
            isSt: value("false"),
            isNew: value("true"),
            quality: value("88"),
            status: value("missing"),
            keyword: value("600519"),
            sort: value("trend_score"),
            order: value("desc"),
          };
          let unsupportedMessage = "";
          try {
            buildDiscoveryPresetDefinition("高质量白酒", elements);
          } catch (error) {
            unsupportedMessage = error.message;
          }
          elements.status.value = "success";
          elements.keyword.value = "";
          const definition = buildDiscoveryPresetDefinition("高质量白酒", elements);
          elements.sort.value = "rank";
          elements.order.value = "asc";
          const rankSort = buildDiscoveryPresetDefinition("短线强势顺序", elements).sort;
          elements.sort.value = "symbol";
          const symbolSort = buildDiscoveryPresetDefinition("代码顺序", elements).sort;
          const normalized = normalizeDiscoveryLeaderboard({
            preset: {
              id: 7,
              name: "高质量白酒",
              revision: 3,
              criteria: {},
              sort: [{ field: "score", order: "desc" }],
            },
            run_id: 42,
            rule_version: "leader-v2",
            items: [{
              position: 1,
              source_rank: 4,
              symbol: "600519.SH",
              code: "600519",
              market: "SH",
              name: "贵州茅台",
              industry: "白酒",
              is_st: false,
              is_new: false,
              quality: 93,
              trend: 81,
              change: 2.5,
              turnover: 1.2,
              amount: 123000000,
              score: 90,
            }],
            total: 1,
            page: 1,
            page_size: 100,
            page_count: 1,
          });
          console.log(JSON.stringify({
            definition,
            unsupportedMessage,
            rankSort,
            symbolSort,
            representable: [
              isDiscoveryPresetUiRepresentable({ criteria: {}, sort: rankSort }),
              isDiscoveryPresetUiRepresentable({ criteria: { score: { min: 80 } }, sort: rankSort }),
              isDiscoveryPresetUiRepresentable({ criteria: {}, sort: [rankSort[0], { field: "score", order: "desc" }] }),
            ],
            item: normalized.items[0],
            labels: [
              rankChangeLabel({ movement: "up", rank_delta: 3 }),
              rankChangeLabel({ movement: "down", rank_delta: -2 }),
              rankChangeLabel({ movement: "unchanged", rank_delta: 0 }),
              rankChangeLabel({ movement: "new", rank_delta: null }),
              rankChangeLabel({ movement: "exit", rank_delta: null }),
            ],
          }));
        '''
    )
    payload = json.loads(output)

    assert payload["definition"] == {
        "name": "高质量白酒",
        "criteria": {
            "market": ["SH"],
            "industry": ["白酒"],
            "is_st": False,
            "is_new": True,
            "quality": {"min": 88},
        },
        "sort": [{"field": "trend", "order": "desc"}],
    }
    assert "状态" in payload["unsupportedMessage"]
    assert "搜索关键词" in payload["unsupportedMessage"]
    assert payload["rankSort"] == [{"field": "rank", "order": "asc"}]
    assert payload["symbolSort"] == [{"field": "symbol", "order": "asc"}]
    assert payload["representable"] == [True, False, False]
    assert payload["item"] | {
        "run_id": 42,
        "status": "success",
        "rank": 1,
        "source_rank": 4,
        "trend_score": 81,
        "change_pct": 2.5,
        "turnover_rate": 1.2,
        "data_quality_score": 93,
    } == payload["item"]
    assert payload["labels"] == [
        "全市场排名上升 3",
        "全市场排名下降 2",
        "全市场排名持平",
        "全市场排名新进",
        "全市场排名离榜",
    ]


def test_discovery_invalidates_stale_requests_and_keeps_complex_presets_read_only() -> None:
    _run_node(
        r'''
          import assert from "node:assert/strict";
          import { installAppDom } from "./tests/frontend_app_flow_helpers.mjs";
          import { createDiscoveryController } from "./static/js/discovery.js";

          const { element, elements } = installAppDom({ canvasContext: null });
          for (const node of elements.values()) {
            node.setAttribute = function setAttribute(name, value) { this[name] = String(value); };
          }
          const editable = {
            id: 7, name: "高质量白酒", revision: 1, criteria: { market: ["SH"] },
            sort: [{ field: "rank", order: "asc" }],
          };
          const complex = {
            id: 8, name: "多条件导入方案", revision: 3,
            criteria: { market: ["SH", "SZ"], score: { min: 80 } },
            sort: [{ field: "score", order: "desc" }, { field: "turnover", order: "desc" }],
          };
          let currentRun = { id: 42, status: "success" };
          let pending = null;
          let createCalls = 0;
          const signals = [];
          const controller = createDiscoveryController({
            root: document,
            getRun: () => currentRun,
            async fetcher(url, options = {}) {
              const target = String(url);
              if (target.startsWith("/api/discovery/presets?page=")) {
                return { items: [editable, complex], total: 2, page: 1, page_size: 100, page_count: 1 };
              }
              if (target === "/api/discovery/presets") {
                createCalls += 1;
                throw new Error("复杂方案不应覆盖保存");
              }
              if (target.includes("/apply")) {
                signals.push(options.signal);
                return pending.page.promise;
              }
              if (target.includes("/rank-changes")) {
                signals.push(options.signal);
                return pending.rank.promise;
              }
              throw new Error(`unexpected request: ${target}`);
            },
          });
          for (const node of elements.values()) {
            node.setAttribute = function setAttribute(name, value) { this[name] = String(value); };
          }

          await controller.activate();
          selectPreset(7);
          setDisplayedRun(42);

          pending = deferredPair();
          const clearedApply = controller.applyPreset(1);
          await flushPromises();
          element("marketScanFilters").listeners.submit({});
          assert.equal(signals.slice(-2).every((signal) => signal.aborted), true);
          pending.page.resolve(leaderboard(editable, 42));
          pending.rank.resolve(rankChanges(42));
          await clearedApply;
          assert.equal(controller.state.applied, null);
          assert.equal(element("marketScanRows").innerHTML, "");

          pending = deferredPair();
          const staleRunApply = controller.applyPreset(1);
          await flushPromises();
          currentRun = { id: 43, status: "success" };
          setDisplayedRun(43);
          pending.page.resolve(leaderboard(editable, 42));
          pending.rank.resolve(rankChanges(42));
          await staleRunApply;
          assert.equal(controller.state.applied, null);
          assert.match(element("discoveryPresetFeedback").textContent, /旧请求结果已忽略/);

          pending = deferredPair();
          const currentApply = controller.applyPreset(2);
          await flushPromises();
          pending.page.resolve(leaderboard(editable, 43, 2));
          pending.rank.resolve(rankChanges(43));
          await currentApply;
          assert.equal(controller.state.applied.runId, 43);
          assert.match(element("marketScanRows").innerHTML, /全市场排名变化未查询（当前第 350 名）/);

          currentRun = { id: 44, status: "success" };
          setDisplayedRun(44);
          const paginationEvent = {
            prevented: false,
            stopped: false,
            preventDefault() { this.prevented = true; },
            stopImmediatePropagation() { this.stopped = true; },
          };
          element("marketScanPrev").listeners.click(paginationEvent);
          assert.equal(controller.state.applied, null);
          assert.equal(paginationEvent.prevented, false);
          assert.equal(paginationEvent.stopped, false);

          selectPreset(8);
          assert.equal(element("discoveryPresetSave").disabled, true);
          assert.match(element("discoveryPresetFeedback").textContent, /后端原定义只读应用/);
          element("marketScanMarket").value = "BJ";
          element("marketScanSort").value = "symbol";
          pending = deferredPair();
          const complexApply = controller.applyPreset(1);
          await flushPromises();
          assert.equal(element("marketScanMarket").value, "BJ");
          assert.equal(element("marketScanSort").value, "symbol");
          pending.page.resolve(leaderboard(complex, 44));
          pending.rank.resolve(rankChanges(44));
          await complexApply;
          assert.equal(controller.state.applied.preset.id, 8);
          assert.match(element("discoveryPresetFeedback").textContent, /后端原定义只读应用/);
          assert.equal(await controller.savePreset(), null);
          assert.equal(createCalls, 0);

          function selectPreset(id) {
            element("discoveryPresetSelect").value = String(id);
            element("discoveryPresetSelect").listeners.change();
          }
          function setDisplayedRun(id) {
            element("marketScanTableWrap").dataset.marketScanRunId = String(id);
            element("marketScanTableWrap")["data-market-scan-run-id"] = String(id);
          }
          function leaderboard(preset, runId, page = 1) {
            return {
              preset, run_id: runId, rule_version: "leader-v2", total: 101,
              page, page_size: 100, page_count: 2,
              items: [{
                position: 1, source_rank: 350, symbol: "600519.SH", code: "600519",
                market: "SH", name: "贵州茅台", industry: "白酒", is_st: false,
                is_new: false, quality: 93, trend: 81, change: 2.5, turnover: 1.2,
                amount: 123000000, score: 90,
              }],
            };
          }
          function rankChanges(runId) {
            return {
              current_run_id: runId, previous_run_id: runId - 1, comparable: true,
              reason: null, current_rule_version: "leader-v2", previous_rule_version: "leader-v2",
              items: [], total: 500, page: 1, page_size: 200, page_count: 3,
            };
          }
          function deferredPair() { return { page: deferred(), rank: deferred() }; }
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


def _run_node(source: str) -> str:
    result = subprocess.run(
        ["node", "--input-type=module", "-e", textwrap.dedent(source)],
        cwd=ROOT,
        check=True,
        capture_output=True,
        text=True,
    )
    return result.stdout.strip()
