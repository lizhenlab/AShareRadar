from __future__ import annotations

from pathlib import Path
import subprocess

import pytest


ROOT = Path(__file__).resolve().parents[1]


@pytest.mark.parametrize("resize", ["before-mobile", "before-desktop", "during-mobile", "during-desktop", "none"])
def test_collect_all_filtered_keeps_one_page_size_across_viewport_changes(resize: str) -> None:
    _run_node(
        f'const resize = "{resize}";'
        + r'''
        mobile = resize.endsWith("desktop");
        await selectAndApply();
        if (resize.startsWith("before")) mobile = !mobile;
        const expectedPageSize = mobile ? 30 : 100;
        transformPage = (payload) => {
          if (resize.startsWith("during")) mobile = !resize.endsWith("desktop");
          return payload;
        };
        const result = await controller.enqueueAllFiltered();
        assert.equal(result.total, 205, "all filtered must include every stock despite resizing");
        assert.deepEqual(writes.map(body => body.symbols.length), [100, 100, 5]);
        assert.deepEqual(writes.flatMap(body => body.symbols), allSymbols());
        assert.ok(writes.every(body => body.run_id === 42 && body.expected_preset_revision === 3));
        const reads = applications.slice(1);
        assert.ok(reads.every(body => body.page_size === expectedPageSize), "collection changed page size midway");
        assert.deepEqual(reads.map(body => body.page), Array.from({ length: Math.ceil(205 / expectedPageSize) }, (_, i) => i + 1));
        assert.match(element("discoveryPresetFeedback").textContent, /处理完成：新增 205/);
        '''
    )


@pytest.mark.parametrize("corruption", [
    "first-page", "later-page", "page-size", "total", "rule", "revision", "run", "repeated-symbol",
])
def test_collect_all_filtered_rejects_inconsistent_pages_before_any_queue_write(corruption: str) -> None:
    _run_node(
        f'const corruption = "{corruption}";'
        + r'''
        await selectAndApply();
        transformPage = (payload) => {
          if (corruption === "first-page" && payload.page === 1) return leaderboard(2, payload.page_size);
          if (payload.page !== 2) return payload;
          if (corruption === "later-page") return leaderboard(1, payload.page_size);
          if (corruption === "page-size") return leaderboard(2, 50);
          if (corruption === "total") return leaderboard(2, payload.page_size, 204);
          if (corruption === "rule") return { ...payload, rule_version: "other-rule" };
          if (corruption === "revision") return { ...payload, preset: { ...preset, revision: 4 } };
          if (corruption === "run") return { ...payload, run_id: 43 };
          if (corruption === "repeated-symbol") {
            const copy = item(1);
            payload.items[0] = { ...copy, position: payload.items[0].position };
          }
          return payload;
        };
        assert.equal(await controller.enqueueAllFiltered(), null);
        assert.deepEqual(writes, [], "unverified collection must not partially enqueue stocks");
        assert.match(element("discoveryPresetFeedback").textContent, /失败/);
        assert.equal(controller.state.busy, false);
        '''
    )


def test_replacing_filter_cancels_collection_and_old_cleanup_preserves_new_application() -> None:
    _run_node(r'''
        await selectAndApply();
        const pending = deferred();
        let oldSignal;
        transformPage = (payload, options) => { oldSignal = options.signal; return pending.promise; };
        const collecting = controller.enqueueAllFiltered();
        element("marketScanFilters").listeners.submit();
        assert.equal(oldSignal?.aborted, true, "filter replacement must cancel old collection reads");
        assert.equal(controller.state.busy, false);
        transformPage = payload => payload;
        const replacement = await controller.applyPreset();
        assert.ok(replacement);
        const current = controller.state.applied;
        const feedback = element("discoveryPresetFeedback").textContent;
        pending.resolve(leaderboard(1, 100));
        assert.equal(await collecting, null);
        assert.equal(controller.state.applied, current);
        assert.equal(controller.state.busy, false);
        assert.equal(element("discoveryPresetFeedback").textContent, feedback);
        assert.deepEqual(writes, []);
        assert.deepEqual(applications.map(body => body.page), [1, 1, 1]);
    ''')


def _run_node(script: str) -> None:
    harness = r'''
        import assert from "node:assert/strict";
        import { installAppDom } from "./tests/frontend_app_flow_helpers.mjs";
        import { createDiscoveryController } from "./static/js/discovery.js";
        const { element, elements } = installAppDom({ canvasContext: null });
        for (const node of elements.values()) node.setAttribute = function(name, value) { this[name] = String(value); };
        let mobile = false;
        globalThis.matchMedia = () => ({ matches: mobile });
        const preset = { id: 7, name: "全部筛选结果", revision: 3,
          criteria: { market: ["SH"], score: { min: 80 } },
          sort: [{ field: "score", order: "desc" }, { field: "symbol", order: "asc" }] };
        const applications = [];
        const writes = [];
        let transformPage = payload => payload;
        const controller = createDiscoveryController({
          root: document, getRun: () => ({ id: 42, status: "success", mode: "official" }),
          async fetcher(url, options = {}) {
            if (url.startsWith("/api/discovery/presets?page=")) return {
              items: [preset], total: 1, page: 1, page_size: 100, page_count: 1,
            };
            if (url.includes("/rank-changes")) return {
              current_run_id: 42, previous_run_id: null, comparable: false, reason: "no_previous_run",
              current_rule_version: "leader-v2", previous_rule_version: null,
              items: [], total: 0, page: 1, page_size: 200, page_count: 0,
            };
            const body = JSON.parse(options.body);
            if (url.endsWith("/apply")) {
              applications.push(body);
              return transformPage(leaderboard(body.page, body.page_size), options);
            }
            if (url.endsWith("/research-queue")) {
              writes.push(body);
              return { added_count: body.symbols.length, existing_count: 0,
                items: body.symbols.map(symbol => ({ symbol, source_run_id: body.run_id,
                  source_preset_id: 7, source_preset_revision: body.expected_preset_revision,
                  source_preset_name: preset.name, enqueued_at: "2026-09-10T10:00:00+08:00", added: true })) };
            }
            throw new Error(`Unexpected request: ${url}`);
          },
        });
        async function selectAndApply() {
          await controller.activate();
          element("discoveryPresetSelect").value = "7";
          element("discoveryPresetSelect").listeners.change();
          element("marketScanTableWrap").dataset.marketScanRunId = "42";
          assert.ok(await controller.applyPreset(1));
        }
        function allSymbols() { return Array.from({ length: 205 }, (_, i) => item(i + 1).symbol); }
        function leaderboard(page, pageSize, total = 205) {
          const offset = (page - 1) * pageSize;
          return { preset, run_id: 42, rule_version: "leader-v2", total, page, page_size: pageSize,
            page_count: Math.ceil(total / pageSize),
            items: Array.from({ length: Math.max(0, Math.min(pageSize, total - offset)) }, (_, i) => item(offset + i + 1)) };
        }
        function item(position) {
          const code = String(position).padStart(6, "0");
          return { position, source_rank: position, symbol: `${code}.SH`, code, market: "SH",
            name: `样本${code}`, industry: "半导体", is_st: false, is_new: false, quality: 90,
            trend: 85, change: 2, turnover: 3, amount: 100000000, score: 88, raw_score: 88.1 };
        }
        function deferred() { let resolve; const promise = new Promise(done => { resolve = done; }); return { promise, resolve }; }
    '''
    result = subprocess.run(
        ["node", "--input-type=module", "--eval", harness + script], cwd=ROOT,
        capture_output=True, text=True, check=False, timeout=20,
    )
    assert result.returncode == 0, result.stderr or result.stdout
