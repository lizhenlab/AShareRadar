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
        "discoveryPresetScreenAlert",
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
            market: value("SH,SZ"),
            industry: value("白酒，饮料"),
            isSt: value("false"),
            isNew: value("true"),
            quality: value("88"),
            qualityMax: value("99"),
            scoreMin: value("70"), scoreMax: value("95"),
            trendMin: value("60"), trendMax: value("90"),
            changeMin: value("-2.5"), changeMax: value("9.5"),
            turnoverMin: value("1"), turnoverMax: value("20"),
            amountMin: value("1000000"), amountMax: value("500000000"),
            confidenceMin: value("72"), riskMax: value("45"), tradabilityMin: value("66"),
            probabilityMin: { value: "", disabled: true },
            status: value("missing"),
            keyword: value("600519"),
            columnViews: [
              { value: "overview", checked: false },
              { value: "risk", checked: true },
            ],
            sort: value("trend_score"),
            order: value("desc"),
            sort2: value("score"), order2: value("desc"),
            sort3: value("symbol"), order3: value("asc"),
          };
          let unsupportedMessage = "";
          try {
            buildDiscoveryPresetDefinition("高质量白酒", elements);
          } catch (error) {
            unsupportedMessage = error.message;
          }
          elements.status.value = "success";
          const definition = buildDiscoveryPresetDefinition("高质量白酒", elements);
          elements.sort.value = "rank";
          elements.order.value = "asc";
          elements.sort2.value = "";
          elements.sort3.value = "";
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
              raw_score: 90.1,
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
            "market": ["SH", "SZ"],
            "industry": ["白酒", "饮料"],
            "is_st": False,
            "is_new": True,
            "quality": {"min": 88, "max": 99},
            "score": {"min": 70, "max": 95},
            "trend": {"min": 60, "max": 90},
            "change": {"min": -2.5, "max": 9.5},
            "turnover": {"min": 1, "max": 20},
            "amount": {"min": 1_000_000, "max": 500_000_000},
            "confidence": {"min": 72},
            "risk": {"max": 45},
            "tradability": {"min": 66},
            "keyword": "600519",
        },
        "sort": [
            {"field": "trend", "order": "desc"},
            {"field": "score", "order": "desc"},
            {"field": "symbol", "order": "asc"},
        ],
        "column_view": "risk",
    }
    assert "状态" in payload["unsupportedMessage"]
    assert "搜索关键词" not in payload["unsupportedMessage"]
    assert payload["rankSort"] == [{"field": "rank", "order": "asc"}]
    assert payload["symbolSort"] == [{"field": "symbol", "order": "asc"}]
    assert payload["representable"] == [True, True, True]
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


def test_discovery_invalidates_stale_requests_and_fully_edits_complex_presets() -> None:
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
          const updateBodies = [];
          const signals = [];
          const controller = createDiscoveryController({
            root: document,
            getRun: () => currentRun,
            async fetcher(url, options = {}) {
              const target = String(url);
              if (target.startsWith("/api/discovery/presets?page=")) {
                return { items: [editable, complex], total: 2, page: 1, page_size: 100, page_count: 1 };
              }
              if (target === "/api/discovery/presets/8" && options.method === "PUT") {
                const body = JSON.parse(options.body);
                updateBodies.push(body);
                return { ...complex, ...body, revision: 4 };
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
          const staleRevisionApply = controller.applyPreset(1);
          await flushPromises();
          pending.page.resolve({
            ...leaderboard(editable, 43),
            preset: { ...editable, revision: 2 },
          });
          pending.rank.resolve(rankChanges(43));
          await staleRevisionApply;
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
          assert.equal(element("discoveryPresetSave").disabled, false);
          assert.match(element("discoveryPresetFeedback").textContent, /已选择筛选方案/);
          pending = deferredPair();
          const complexApply = controller.applyPreset(1);
          await flushPromises();
          assert.equal(element("marketScanMarket").value, "SH");
          assert.equal(element("marketScanScoreMin").value, "80");
          assert.equal(element("marketScanSort").value, "score");
          assert.equal(element("marketScanSort2").value, "turnover_rate");
          pending.page.resolve(leaderboard(complex, 44));
          pending.rank.resolve({ current_run_id: 44, items: "malformed" });
          await complexApply;
          assert.equal(controller.state.applied.preset.id, 8);
          assert.match(element("discoveryPresetFeedback").textContent, /方案已应用.*排名变化暂不可用/);
          element("marketScanMarket").value = "BJ";
          element("marketScanSort").value = "symbol";
          element("marketScanSort2").value = "";
          const updated = await controller.savePreset();
          assert.equal(updated.revision, 4);
          assert.equal(updateBodies.length, 1);
          assert.equal(updateBodies[0].expected_revision, 3);
          assert.deepEqual(updateBodies[0].criteria.market, ["BJ"]);
          assert.deepEqual(updateBodies[0].sort, [{ field: "symbol", order: "desc" }]);

          function selectPreset(id) {
            element("discoveryPresetSelect").value = String(id);
            element("discoveryPresetSelect").listeners.change();
          }
          function setDisplayedRun(id) {
            element("marketScanTableWrap").dataset.marketScanRunId = String(id);
            element("marketScanTableWrap")["data-market-scan-run-id"] = String(id);
          }
              function leaderboard(preset, runId, page = 1) {
                const count = page === 1 ? 100 : 1;
                return {
                  preset, run_id: runId, rule_version: "leader-v2", total: 101,
                  page, page_size: 100, page_count: 2,
                  items: Array.from({ length: count }, (_, index) => {
                    const position = ((page - 1) * 100) + index + 1;
                    const code = index === 0 ? "600519" : String(600000 + index).padStart(6, "0");
                    return {
                      position, source_rank: index === 0 ? 350 : position, symbol: `${code}.SH`, code,
                      market: "SH", name: index === 0 ? "贵州茅台" : `样本${code}`, industry: "白酒", is_st: false,
                      is_new: false, quality: 93, trend: 81, change: 2.5, turnover: 1.2,
                      amount: 123000000, score: 90, raw_score: 90.1,
                    };
                  }),
                };
              }
          function rankChanges(runId) {
            return {
              current_run_id: runId, previous_run_id: runId - 1, comparable: true,
              reason: null, current_rule_version: "leader-v2", previous_rule_version: "leader-v2",
                  items: [], total: 0, page: 1, page_size: 200, page_count: 0,
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


def test_discovery_records_one_typed_idempotent_screen_change_event() -> None:
    _run_node(
        r'''
          import assert from "node:assert/strict";
          import { installAppDom } from "./tests/frontend_app_flow_helpers.mjs";
          import { createDiscoveryController } from "./static/js/discovery.js";

          const { elements } = installAppDom({ canvasContext: null });
          for (const node of elements.values()) {
            node.setAttribute = function setAttribute(name, value) { this[name] = String(value); };
          }
          const digest = "a".repeat(64);
          let alertRequest = null;
          const preset = {
            id: 7, name: "高质量", schema_version: 2, revision: 3,
            criteria: { score: { min: 80 } }, sort: [{ field: "rank", order: "asc" }],
            column_view: "overview", created_at: "2026-08-12", updated_at: "2026-08-12",
          };
          const controller = createDiscoveryController({
            root: document,
            getRun: () => ({ id: 42, status: "success" }),
            async fetcher(url, options = {}) {
              if (String(url).startsWith("/api/discovery/presets?page=")) {
                return { items: [preset], total: 1, page: 1, page_size: 100, page_count: 1 };
              }
              if (String(url) === "/api/discovery/presets/7/screen-alerts") {
                alertRequest = JSON.parse(options.body);
                return {
                  schema_version: "market-scan-screen-alert-v1", status: "ready", unavailable_reason: null,
                  preset: { preset_id: 7, preset_revision: 3, preset_name: "高质量", spec_digest: digest },
                  current: { run_id: 42 }, previous: { run_id: 41 }, entered_symbols: ["600519.SH"],
                  exited_symbols: [], suppressed_unrankable_symbols: ["600000.SH"],
                  event_digest: digest, created: true,
                };
              }
              throw new Error(`unexpected ${url}`);
            },
          });
          for (const node of elements.values()) {
            node.setAttribute = function setAttribute(name, value) { this[name] = String(value); };
          }
          await controller.activate();
          const select = elements.get("discoveryPresetSelect");
          select.value = "7";
          select.listeners.change();

          const payload = await controller.recordScreenAlert();

          assert.deepEqual(alertRequest, { current_run_id: 42, expected_preset_revision: 3 });
          assert.equal(payload.created, true);
          assert.match(elements.get("discoveryPresetFeedback").textContent, /新进入 1、退出 0/);
          assert.match(elements.get("discoveryPresetFeedback").textContent, /未误报退出/);
        '''
    )


def test_discovery_bulk_queue_and_preset_import_export_preserve_provenance() -> None:
    _run_node(
        r'''
          import assert from "node:assert/strict";
          import { installAppDom } from "./tests/frontend_app_flow_helpers.mjs";
          import { createDiscoveryController } from "./static/js/discovery.js";

          const { element, elements } = installAppDom({ canvasContext: null });
          for (const node of elements.values()) {
            node.setAttribute = function setAttribute(name, value) { this[name] = String(value); };
          }
          const preset = {
            id: 7, name: "批量研究", revision: 3,
            criteria: { market: ["SH", "SZ"], score: { min: 80 } },
            sort: [{ field: "score", order: "desc" }, { field: "symbol", order: "asc" }],
          };
          const archive = {
            format: "ashare-radar.discovery-preset", schema_version: 1,
            checksum_algorithm: "sha256", checksum: "a".repeat(64),
            exported_at: "2026-07-29T10:00:00Z",
            preset: { name: preset.name, criteria: preset.criteria, sort: preset.sort },
          };
          const queueBodies = [];
          const applyPages = [];
          const controller = createDiscoveryController({
            root: document,
            getRun: () => ({ id: 42, status: "success", mode: "official" }),
            async fetcher(url, options = {}) {
              const target = String(url);
              if (target.startsWith("/api/discovery/presets?page=")) {
                return { items: [preset], total: 1, page: 1, page_size: 100, page_count: 1 };
              }
              if (target === "/api/discovery/presets/7/export") return archive;
              if (target === "/api/discovery/presets/import") {
                return { ...preset, id: 8, name: "导入方案", revision: 1 };
              }
              if (target.includes("/rank-changes")) return rankChanges();
              if (target.endsWith("/apply")) {
                const page = JSON.parse(options.body).page;
                applyPages.push(page);
                return leaderboard(page);
              }
              if (target.endsWith("/research-queue")) {
                const body = JSON.parse(options.body);
                queueBodies.push(body);
                return {
                  items: body.symbols.map((symbol) => ({
                    symbol,
                    source_run_id: body.run_id,
                    source_preset_id: 7,
                    source_preset_revision: body.expected_preset_revision,
                    source_preset_name: preset.name,
                    enqueued_at: "2026-07-29T10:00:00Z",
                    added: true,
                  })),
                  added_count: body.symbols.length,
                  existing_count: 0,
                };
              }
              throw new Error(`unexpected request: ${target}`);
            },
          });
          for (const node of elements.values()) {
            node.setAttribute = function setAttribute(name, value) { this[name] = String(value); };
          }

          await controller.activate();
          element("discoveryPresetSelect").value = "7";
          element("discoveryPresetSelect").listeners.change();
          element("marketScanTableWrap").dataset.marketScanRunId = "42";
          await controller.applyPreset(1);
          assert.equal(controller.state.applied.payload.items.length, 100);

          controller.state.applied.selected.add(controller.state.applied.payload.items[0].symbol);
          controller.state.applied.selected.add(controller.state.applied.payload.items[1].symbol);
          await controller.enqueueSelected();
          assert.deepEqual(queueBodies[0].symbols.length, 2);

          const bulk = await controller.enqueueAllFiltered();
          assert.equal(bulk.total, 205);
          assert.deepEqual(queueBodies.slice(1).map((body) => body.symbols.length), [100, 100, 5]);
          assert.equal(queueBodies.every((body) => body.run_id === 42), true);
          assert.equal(queueBodies.every((body) => body.expected_preset_revision === 3), true);
          assert.deepEqual(applyPages, [1, 1, 2, 3]);

          assert.deepEqual(await controller.exportPreset(), archive);
          const imported = await controller.importPreset({ async text() { return JSON.stringify(archive); } });
          assert.equal(imported.id, 8);
          assert.equal(controller.state.selectedId, 8);

          function leaderboard(page) {
            const start = (page - 1) * 100;
            const count = Math.min(100, 205 - start);
            return {
              preset, run_id: 42, rule_version: "leader-v2", total: 205,
              page, page_size: 100, page_count: 3,
              items: Array.from({ length: count }, (_, index) => item(start + index + 1)),
            };
          }
          function item(position) {
            const code = String(position).padStart(6, "0");
            return {
              position, source_rank: position, symbol: `${code}.SH`, code, market: "SH",
              name: `样本${code}`, industry: "半导体", is_st: false, is_new: false,
              quality: 90, trend: 85, change: 2, turnover: 3, amount: 100000000, score: 88,
              raw_score: 88.1,
            };
          }
          function rankChanges() {
            return {
              current_run_id: 42, previous_run_id: 41, comparable: true, reason: null,
              current_rule_version: "leader-v2", previous_rule_version: "leader-v2",
              items: [], total: 0, page: 1, page_size: 200, page_count: 0,
            };
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
