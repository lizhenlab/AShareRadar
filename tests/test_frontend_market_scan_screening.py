from __future__ import annotations

from copy import deepcopy
import json
import subprocess
from pathlib import Path

from app.models.market_scan_screening import ScreenSpecV2


ROOT = Path(__file__).resolve().parents[1]


def test_screening_workbench_contracts_keep_nulls_and_digests_strict() -> None:
    script = r'''
      import {
        SCREEN_SPEC_SCHEMA_VERSION,
        validateMarketScanBreadth,
        validateMarketScanDelta,
        validateMarketScanScreenAlert,
        validateScreenEvaluation,
      } from "./static/js/market-scan-screening-contracts.js";

      const digest = "a".repeat(64);
      const evidence = {
        run_id: 42, status: "success", mode: "official", scope: "SH/SZ/BJ listed A-shares",
        data_date: "2026-08-11", quote_date: "2026-08-11", rule_version: "full-market-score-v4",
        finished_at: "2026-08-11T16:00:00+08:00", snapshot_digest: digest,
        snapshot_seal_origin: "publication", snapshot_sealed_at: "2026-08-11T16:00:00+08:00",
      };
      const spec = {
        schema_version: SCREEN_SPEC_SCHEMA_VERSION, status: "success", markets: [], industries: [],
        is_st: null, is_new: null, ranges: {}, keyword: null, sort: [{ field: "rank", order: "asc" }],
      };
      evidence.scope = "沪市 + 深市 + 北交所当前上市A股";
      const bins = Array.from({ length: 10 }, (_, index) => ({ lower: index * 10, upper: (index + 1) * 10, count: 0 }));
      const item = { run_id: 42, symbol: "600519.SH", code: "600519", market: "SH", score: null, amount: null };
      const breadthPayload = {
        schema_version: "market-scan-breadth-v1", evidence,
        population: { total: 1, by_status: { success: 1 }, by_market: { SH: 1 } },
        score: { present_count: 0, missing_count: 1, min: null, max: null, mean: null,
          percentiles: { p10: null, p25: null, p50: null, p75: null, p90: null }, bins },
        change: { advancing: 0, flat: 0, declining: 0, missing: 1 },
        industries: [{ industry: null, count: 1, score_present_count: 0, average_score: null }],
        canonical_digest: digest,
      };
      validateMarketScanBreadth(breadthPayload, 42);
      const evaluationPayload = {
        schema_version: "market-scan-screen-evaluation-v1", evidence, spec, spec_digest: digest,
        population_count: 1, matched_count: 1,
        funnel: [{ index: 1, condition_code: "status", label: "结果状态", input_count: 1, matched_count: 1, excluded_count: 0, missing_count: 0 }],
        exclusion_reasons: [],
        matched: { items: [item], total: 1, page: 1, page_size: 100, page_count: 1 },
        matched_explanations: [{ symbol: "600519.SH", passed_conditions: ["status"] }],
        near_misses: [], canonical_digest: digest,
      };
      validateScreenEvaluation(evaluationPayload, 42);
      const deltaPayload = {
        schema_version: "market-scan-delta-v1", status: "unavailable",
        unavailable_reason: "previous_same_cohort_not_found", current: {
          run_id: 42, status: "success", mode: "official", scope: "沪市 + 深市 + 北交所当前上市A股",
          rule_version: "full-market-score-v4", data_date: "2026-08-11",
          finished_at: "2026-08-11T16:00:00+08:00", snapshot_digest: digest,
          snapshot_seal_origin: "publication", snapshot_sealed_at: "2026-08-11T16:00:00+08:00",
        }, previous: null,
        cohort: { mode: "official", scope: "沪市 + 深市 + 北交所当前上市A股", rule_version: "full-market-score-v4" },
        summary: { previous_present_count: 0, current_present_count: 1, compared_symbol_count: 0,
          evidence_detail_scope: "top100_union", evidence_change_reason_counts: [] },
        top_buckets: [], rank_score_changes: [], exposure_changes: [],
        evidence_changes: [], canonical_digest: digest,
      };
      validateMarketScanDelta(deltaPayload, 42);
      validateMarketScanScreenAlert({
        schema_version: "market-scan-screen-alert-v1", status: "ready", unavailable_reason: null,
        preset: { preset_id: 7, preset_revision: 2, preset_name: "高质量", spec_digest: digest },
        current: { run_id: 42 }, previous: { run_id: 41 }, entered_symbols: ["600519.SH"],
        exited_symbols: [], suppressed_unrankable_symbols: [], event_digest: digest, created: true,
      }, 7, 42);

      let rejected = false;
      try {
        validateMarketScanBreadth({
          schema_version: "market-scan-breadth-v1", evidence,
          population: { total: 0, by_status: {}, by_market: {} },
          score: { present_count: 0, missing_count: 0, min: null, max: null, mean: null,
            percentiles: { p10: null, p25: null, p50: null, p75: null, p90: null }, bins },
          change: { advancing: 0, flat: 0, declining: 0, missing: 0 }, industries: [], canonical_digest: "short",
        }, 42);
      } catch (error) {
        rejected = error.message.includes("64 位");
      }
      if (!rejected) throw new Error("invalid digest was accepted");

      const attacks = [
        (payload) => { payload.population.by_status.success = 2; },
        (payload) => { payload.change.advancing = 1; },
      ];
      attacks.forEach((attack) => {
        const payload = structuredClone(breadthPayload);
        attack(payload);
        let accepted = true;
        try { validateMarketScanBreadth(payload, 42); } catch { accepted = false; }
        if (accepted) throw new Error("inconsistent breadth payload was accepted");
      });
      const evaluationAttacks = [
        (payload) => { payload.funnel[0].matched_count = 0; },
        (payload) => { payload.matched.page_count = 2; },
        (payload) => { payload.matched.items[0].run_id = 99; },
        (payload) => { payload.matched_explanations[0].passed_conditions = ["range.score"]; },
        (payload) => { payload.near_misses = [{ item: payload.matched.items[0], failed_conditions: [{ code: "status", label: "状态", missing: false }] }]; },
      ];
      evaluationAttacks.forEach((attack) => {
        const payload = structuredClone(evaluationPayload);
        attack(payload);
        let accepted = true;
        try { validateScreenEvaluation(payload, 42); } catch { accepted = false; }
        if (accepted) throw new Error("inconsistent screening evaluation was accepted");
      });
      const deltaAttacks = [
        (payload) => { payload.current.snapshot_digest = "short"; },
        (payload) => { payload.cohort.rule_version = "other-rule"; },
        (payload) => { payload.summary.compared_symbol_count = 2; },
        (payload) => { payload.unavailable_reason = "current_not_published"; },
      ];
      deltaAttacks.forEach((attack) => {
        const payload = structuredClone(deltaPayload);
        attack(payload);
        let accepted = true;
        try { validateMarketScanDelta(payload, 42); } catch { accepted = false; }
        if (accepted) throw new Error("inconsistent market-scan delta was accepted");
      });
    '''
    _run_node(script)


def test_screen_spec_v2_frontend_validation_matches_the_python_contract() -> None:
    base = {
        "schema_version": "screen-spec-v2",
        "status": "success",
        "markets": [],
        "industries": [],
        "is_st": None,
        "is_new": None,
        "ranges": {},
        "keyword": None,
        "sort": [{"field": "rank", "order": "asc"}],
    }

    def changed(**updates: object) -> dict[str, object]:
        payload = deepcopy(base)
        payload.update(updates)
        return payload

    valid_cases = {
        "pydantic_defaults": {},
        "partial_markets": {"markets": ["SH", "BJ"]},
        "partial_range": {"ranges": {"score": {"min": 0}}},
        "partial_sort": {"sort": [{"field": "confidence", "order": "desc"}]},
        "canonical": deepcopy(base),
        "all_range_boundaries": changed(
            status=None,
            markets=["SH", "SZ", "BJ"],
            industries=["半导体", "白酒\n制造"],
            is_st=False,
            is_new=True,
            ranges={
                "score": {"min": 0, "max": 100},
                "trend_score": {"min": 0, "max": 100},
                "change_pct": {"min": -1000, "max": 1000},
                "turnover_rate": {"min": 0, "max": 10_000},
                "amount": {"min": 0, "max": 1_000_000_000_000_000},
                "data_quality_score": {"min": 0, "max": 100},
                "confidence": {"min": 0, "max": 100},
                "risk": {"min": 0, "max": 100},
                "tradability": {"min": 0, "max": 100},
            },
            keyword="  价值 成长  ",
            sort=[
                {"field": "market", "order": "asc"},
                {"field": "industry", "order": "desc"},
                {"field": "is_st", "order": "asc"},
            ],
        ),
        "nullable_range_and_empty_keyword": changed(
            ranges={"score": None, "risk": {"max": 40}},
            keyword="   ",
            sort=[{"field": "is_new", "order": "desc"}],
        ),
        "whitespace_only_keyword_strips_before_length_check": changed(keyword=" " * 100),
        "unicode_keyword_uses_code_points": changed(keyword="📈" * 80),
    }
    invalid_cases = {
        "unknown_spec_field": changed(unknown=True),
        "wrong_schema": changed(schema_version="screen-spec-v3"),
        "unknown_status": changed(status="degraded"),
        "non_boolean_flag": changed(is_st=0),
        "unknown_market": changed(markets=["HK"]),
        "duplicate_market": changed(markets=["SH", "SH"]),
        "too_many_markets": changed(markets=["SH", "SZ", "BJ", "SH"]),
        "duplicate_normalized_industry": changed(industries=["白  酒", "白 酒"]),
        "empty_industry": changed(industries=[" \n "]),
        "control_character_industry": changed(industries=["白酒\u0000"]),
        "too_many_industries": changed(industries=[f"行业{index}" for index in range(21)]),
        "unknown_ranges_field": changed(ranges={"probability": {"min": 50}}),
        "unknown_range_field": changed(ranges={"score": {"min": 50, "median": 60}}),
        "empty_range": changed(ranges={"score": {}}),
        "null_only_range": changed(ranges={"score": {"min": None}}),
        "reversed_range": changed(ranges={"score": {"min": 81, "max": 80}}),
        "string_range": changed(ranges={"score": {"min": "80"}}),
        "boolean_range": changed(ranges={"score": {"min": True}}),
        "non_finite_range": changed(ranges={"score": {"min": float("nan")}}),
        "score_below_zero": changed(ranges={"score": {"min": -0.01}}),
        "score_above_one_hundred": changed(ranges={"score": {"max": 100.01}}),
        "change_out_of_range": changed(ranges={"change_pct": {"min": -1000.01}}),
        "turnover_out_of_range": changed(ranges={"turnover_rate": {"max": 10_000.01}}),
        "amount_out_of_range": changed(ranges={"amount": {"max": 1_000_000_000_000_001}}),
        "bounded_research_out_of_range": changed(ranges={"risk": {"max": 100.01}}),
        "empty_sort": changed(sort=[]),
        "too_many_sorts": changed(
            sort=[
                {"field": "rank", "order": "asc"},
                {"field": "score", "order": "desc"},
                {"field": "amount", "order": "desc"},
                {"field": "symbol", "order": "asc"},
            ]
        ),
        "unknown_sort_field": changed(sort=[{"field": "leader_score", "order": "desc"}]),
        "unknown_sort_order": changed(sort=[{"field": "score", "order": "sideways"}]),
        "duplicate_sort_field": changed(
            sort=[{"field": "score", "order": "desc"}, {"field": "score", "order": "asc"}]
        ),
        "unknown_sort_property": changed(sort=[{"field": "rank", "order": "asc", "nulls": "last"}]),
        "internal_whitespace_counts_before_normalization": changed(keyword="a" + (" " * 100) + "b"),
        "keyword_too_long": changed(keyword="x" * 81),
    }
    cases = {**valid_cases, **invalid_cases}
    encoded_cases = json.dumps(cases, ensure_ascii=False)
    script = f'''
      import {{ screenEvaluationRequest, validateScreenSpec }}
        from "./static/js/market-scan-screening-contracts.js";

      const cases = {encoded_cases};
      const outcomes = Object.fromEntries(Object.entries(cases).map(([name, spec]) => {{
        const before = JSON.stringify(spec);
        try {{
          const validated = validateScreenSpec(spec);
          const request = screenEvaluationRequest(spec);
          return [name, {{
            accepted: true,
            sameReference: validated === spec && request.spec === spec,
            unchanged: JSON.stringify(spec) === before,
          }}];
        }} catch (error) {{
          return [name, {{ accepted: false, errorName: error?.name, message: error?.message }}];
        }}
      }}));
      console.log(JSON.stringify(outcomes));
    '''
    frontend = json.loads(_run_node(script))
    backend = {}
    for name, payload in cases.items():
        try:
            ScreenSpecV2.model_validate(payload)
            backend[name] = True
        except ValueError:
            backend[name] = False

    assert {name: outcome["accepted"] for name, outcome in frontend.items()} == backend
    assert all(frontend[name]["accepted"] for name in valid_cases)
    assert all(frontend[name]["sameReference"] for name in valid_cases)
    assert all(frontend[name]["unchanged"] for name in valid_cases)
    assert all(not frontend[name]["accepted"] for name in invalid_cases)
    assert all(
        frontend[name]["errorName"] == "MarketScanScreeningContractError"
        for name in invalid_cases
    )


def test_screening_workbench_is_lazy_and_column_views_are_accessible() -> None:
    html = (ROOT / "static/index.html").read_text(encoding="utf-8")
    entry = (ROOT / "static/js/market-scan-screening.js").read_text(encoding="utf-8")
    controller = (ROOT / "static/js/market-scan-screening-controller.js").read_text(encoding="utf-8")

    assert 'id="marketScanScreeningWorkbench" aria-busy="false"' in html
    assert 'id="marketScanScreeningFeedback" role="status"' in html
    assert 'id="marketScanColumnViews" aria-controls="marketScanTable"' in html
    assert html.count('name="marketScanColumnView"') == 5
    assert 'id="marketScanTable" data-column-view="overview"' in html
    assert 'import("./market-scan-screening-controller.js")' in entry
    assert 'shell?.addEventListener("toggle"' in entry
    assert "/breadth`" in controller and "/screen/evaluate`" in controller and "/delta`" in controller


def test_screening_workbench_renders_nullable_ranges_from_the_backend_model() -> None:
    spec = ScreenSpecV2.model_validate({"ranges": {"score": {"min": 99}}}).model_dump(mode="json")
    _run_node(f'''
      import assert from "node:assert/strict";
      import {{ renderScreenSpecChips }} from "./static/js/market-scan-screening-view.js";
      const chips = renderScreenSpecChips({json.dumps(spec, ensure_ascii=False)});
      assert.ok(chips.includes("趋势强度：99.00–不限"));
      assert.equal(chips.length, 4, "Absent nullable ranges are not active conditions");
      assert.ok(!chips.some((chip) => chip.includes("--") || chip.includes("NaN")));
    ''')


def test_market_scan_read_client_retries_only_busy_with_one_monotonic_budget() -> None:
    _run_node(r'''
      import assert from "node:assert/strict";
      import { requestMarketScanRead } from "./static/js/market-scan-read-client.js";
      let now = 100;
      const delays = [], timeouts = [];
      globalThis.performance = { now: () => now };
      globalThis.setTimeout = (callback, delay) => {
        delays.push(delay); now += delay; queueMicrotask(callback); return delays.length;
      };
      globalThis.clearTimeout = () => {};
      const busy = Object.assign(new Error("busy"), { status: 503, retryAfterMs: 2000 });
      const controller = new AbortController();
      const options = { method: "POST", body: "frozen-spec", headers: { "Content-Type": "application/json" }, signal: controller.signal };
      const payload = { ready: true };
      const result = await requestMarketScanRead(async (url, supplied) => {
        assert.equal(url, "/screen/evaluate");
        assert.equal(supplied.method, "POST");
        assert.equal(supplied.body, options.body);
        assert.equal(supplied.headers, options.headers);
        assert.equal(supplied.signal, controller.signal);
        timeouts.push(supplied.timeoutMs);
        now += 50;
        if (timeouts.length === 1) throw busy;
        return payload;
      }, "/screen/evaluate", options);
      assert.equal(result, payload);
      assert.deepEqual(timeouts, [60000, 57950]);
      assert.deepEqual(delays, [2000]);
      for (const error of [
        new Error("network"), { status: 409, retryAfterMs: 10 }, { status: 422, retryAfterMs: 10 },
        { status: 503 }, { status: 503, retryAfterMs: NaN }, { status: 503, retryAfterMs: -1 },
      ]) {
        let attempts = 0;
        await assert.rejects(requestMarketScanRead(async () => { attempts += 1; throw error; }, "/read"), (actual) => actual === error);
        assert.equal(attempts, 1);
      }
      let attempts = 0;
      await assert.rejects(requestMarketScanRead(async () => {
        attempts += 1; now += 50; throw busy;
      }, "/read", { timeoutMs: 2000 }), (actual) => actual === busy);
      assert.equal(attempts, 1, "Retry-After must fit the remaining total budget");
      const zeroDelay = Object.assign(new Error("busy"), { status: 503, retryAfterMs: 0 });
      attempts = 0;
      await assert.rejects(requestMarketScanRead(async () => { attempts += 1; throw zeroDelay; }, "/read"), (actual) => actual === zeroDelay);
      assert.equal(attempts, 31, "Even a stalled clock cannot cause unlimited retries");
      assert.ok(delays.slice(1).every((delay) => delay >= 250));
    ''')


def test_market_scan_read_client_cancels_backoff_and_ignores_late_success() -> None:
    _run_node(r'''
      import assert from "node:assert/strict";
      import { requestMarketScanRead } from "./static/js/market-scan-read-client.js";
      const timers = new Map();
      let nextTimer = 0, attempts = 0;
      globalThis.setTimeout = (callback) => { timers.set(++nextTimer, callback); return nextTimer; };
      globalThis.clearTimeout = (timer) => timers.delete(timer);
      const controller = new AbortController();
      const pending = requestMarketScanRead(async () => {
        attempts += 1;
        throw Object.assign(new Error("busy"), { status: 503, retryAfterMs: 2000 });
      }, "/read", { signal: controller.signal });
      const cancelled = assert.rejects(pending, { name: "AbortError" });
      for (let index = 0; index < 6; index += 1) await Promise.resolve();
      assert.equal(timers.size, 1);
      controller.abort();
      await cancelled;
      assert.equal(timers.size, 0);
      assert.equal(attempts, 1);
      await assert.rejects(requestMarketScanRead(async () => { attempts += 1; }, "/read", { signal: controller.signal }), { name: "AbortError" });
      assert.equal(attempts, 1);
      const lateController = new AbortController();
      let release;
      const late = requestMarketScanRead(() => new Promise((resolve) => { release = resolve; }), "/read", { signal: lateController.signal });
      const rejected = assert.rejects(late, { name: "AbortError" });
      lateController.abort();
      release({ stale: true });
      await rejected;
    ''')


def test_market_scan_read_client_rejects_success_at_or_after_the_total_deadline() -> None:
    _run_node(r'''
      import assert from "node:assert/strict";
      import { requestMarketScanRead } from "./static/js/market-scan-read-client.js";
      let now = 100;
      globalThis.performance = { now: () => now };
      for (const elapsed of [59_999, 60_000, 60_001]) {
        let attempts = 0;
        const request = requestMarketScanRead(async (_url, options) => {
          attempts += 1;
          assert.equal(options.timeoutMs, 60_000);
          now += elapsed;
          return { ready: true };
        }, "/read", { timeoutMs: 60_000 });
        if (elapsed < 60_000) assert.deepEqual(await request, { ready: true });
        else await assert.rejects(request, /冻结快照读取等待超时/);
        assert.equal(attempts, 1, "A late success must not restart the read");
      }
      const controller = new AbortController();
      await assert.rejects(requestMarketScanRead(async () => {
        now += 60_001;
        controller.abort();
        return { stale: true };
      }, "/read", { signal: controller.signal }), { name: "AbortError" });
    ''')


def test_screening_controller_serializes_heavy_reads_and_renders_each_result() -> None:
    _run_node(r'''
      import assert from "node:assert/strict";
      import { createMarketScanScreeningController } from "./static/js/market-scan-screening-controller.js";
      const fixture = screeningFixture();
      const pending = [], urls = [];
      const controller = createMarketScanScreeningController({ root: fixture.root, fetcher: (url, options) => {
        urls.push(url);
        assert.ok(options.timeoutMs > 59000 && options.timeoutMs <= 60000);
        return new Promise((resolve) => pending.push({ url, options, resolve }));
      } });
      const loading = controller.open();
      assert.deepEqual(urls, ["/api/market-scans/42/breadth"]);
      pending[0].resolve(screeningResponses().breadth);
      await flushPromises();
      assert.match(fixture.get("marketScanScreeningBreadth").innerHTML, /冻结股票池/);
      assert.deepEqual(urls, ["/api/market-scans/42/breadth", "/api/market-scans/42/screen/evaluate"]);
      assert.equal(pending[1].options.method, "POST");
      pending[1].resolve(screeningResponses().evaluation);
      await flushPromises();
      assert.match(fixture.get("marketScanScreeningEvaluation").innerHTML, /筛选漏斗/);
      assert.equal(urls.at(-1), "/api/market-scans/42/delta");
      pending[2].resolve(screeningResponses().delta);
      await loading;
      assert.equal(controller.state.loaded, true);
      assert.equal(fixture.get("marketScanScreeningWorkbench")["aria-busy"], "false");
      assert.match(fixture.get("marketScanScreeningSummaryStatus").textContent, /命中 0\/0/);
      await controller.open();
      assert.equal(urls.length, 3, "A completed open panel should not reread unchanged evidence");
      controller.dispose();
    ''' + _screening_controller_fixture())


def test_screening_controller_cancels_changed_run_before_deferred_refresh_and_skips_same_run_mutations() -> None:
    _run_node(r'''
      import assert from "node:assert/strict";
      import { createMarketScanScreeningController } from "./static/js/market-scan-screening-controller.js";
      let mutationCallback;
      const scheduled = [];
      globalThis.MutationObserver = class {
        constructor(callback) { mutationCallback = callback; }
        observe() {}
        disconnect() {}
      };
      globalThis.setTimeout = (callback) => { scheduled.push(callback); return scheduled.length; };
      const fixture = screeningFixture();
      const pending = [];
      const controller = createMarketScanScreeningController({ root: fixture.root, fetcher: (url, options) => (
        new Promise((resolve) => pending.push({ url, options, resolve }))
      ) });
      const first = controller.open();
      mutationCallback([{ attributeName: "data-market-scan-run-id" }]);
      assert.equal(pending[0].options.signal.aborted, false, "Rewriting the same run id must not cancel a valid read");
      assert.equal(scheduled.length, 0);
      pending[0].resolve(screeningResponses().breadth);
      await flushPromises();
      assert.equal(pending.length, 2);
      assert.match(fixture.get("marketScanScreeningBreadth").innerHTML, /冻结股票池/);
      fixture.get("marketScanTableWrap").dataset.marketScanRunId = "43";
      mutationCallback([{ attributeName: "data-market-scan-run-id" }]);
      assert.equal(pending[1].options.signal.aborted, true, "Cancel immediately, not in the deferred refresh");
      assert.equal(scheduled.length, 1);
      assert.equal(fixture.get("marketScanScreeningBreadth").innerHTML, "");
      assert.equal(fixture.get("marketScanScreeningWorkbench")["aria-busy"], "false");
      pending[1].resolve(screeningResponses().evaluation);
      assert.equal(await first, null);
      assert.equal(pending.length, 2, "Stale evaluation must not start the old run's delta request");
      assert.equal(fixture.get("marketScanScreeningEvaluation").innerHTML, "");
      assert.equal(controller.state.loaded, false);
      scheduled.shift()();
      assert.equal(pending[2].url, "/api/market-scans/43/breadth");
      const later = screeningResponses();
      later.breadth.evidence.run_id = 43;
      pending[2].resolve(later.breadth);
      await flushPromises();
      assert.equal(pending[3].url, "/api/market-scans/43/screen/evaluate");
      pending[3].resolve(later.evaluation);
      await flushPromises();
      assert.equal(pending[4].url, "/api/market-scans/43/delta");
      pending[4].resolve(later.delta);
      await flushPromises();
      assert.equal(controller.state.loaded, true);
      mutationCallback([{ attributeName: "data-market-scan-run-id" }]);
      assert.equal(scheduled.length, 0);
      await controller.open();
      assert.equal(pending.length, 5, "A completed same-run snapshot should remain cached");
      controller.dispose();
    ''' + _screening_controller_fixture())


def test_screening_controller_rechecks_dom_binding_after_each_read_before_the_observer_runs() -> None:
    _run_node(r'''
      import assert from "node:assert/strict";
      import { createMarketScanScreeningController } from "./static/js/market-scan-screening-controller.js";
      const fields = ["breadth", "evaluation", "delta"];
      for (const changeAt of [0, 1, 2]) {
        for (const replacement of ["43", ""]) {
          const fixture = screeningFixture();
          const pending = [];
          const controller = createMarketScanScreeningController({ root: fixture.root, fetcher: (url, options) => (
            new Promise((resolve) => pending.push({ url, options, resolve }))
          ) });
          const reading = controller.open();
          for (let index = 0; index < changeAt; index += 1) {
            pending[index].resolve(screeningResponses()[fields[index]]);
            await flushPromises();
          }
          // DOM mutation may be observed after the already-queued response microtask.
          fixture.get("marketScanTableWrap").dataset.marketScanRunId = replacement;
          pending[changeAt].resolve(screeningResponses()[fields[changeAt]]);
          assert.equal(await reading, null);
          assert.equal(pending.length, changeAt + 1, "An obsolete read must not start the next old-run endpoint");
          assert.equal(controller.state.loaded, false);
          assert.equal(controller.state.requestScope, null);
          assert.equal(fixture.get("marketScanScreeningWorkbench")["aria-busy"], "false");
          for (const region of ["Breadth", "Evaluation", "Diff"]) {
            assert.equal(fixture.get(`marketScanScreening${region}`).innerHTML, "");
          }
          controller.dispose();
        }
      }
    ''' + _screening_controller_fixture())


def test_screening_controller_stops_closed_work_and_does_not_cache_failed_evidence() -> None:
    _run_node(r'''
      import assert from "node:assert/strict";
      import { createMarketScanScreeningController } from "./static/js/market-scan-screening-controller.js";
      const fixture = screeningFixture();
      let attempts = 0, release, signal;
      const controller = createMarketScanScreeningController({ root: fixture.root, fetcher: (_url, options) => {
        attempts += 1; signal = options.signal;
        return new Promise((resolve) => { release = resolve; });
      } });
      const reading = controller.open();
      fixture.get("marketScanScreeningWorkbench").open = false;
      fixture.get("marketScanScreeningWorkbench").dispatch("toggle");
      assert.equal(signal.aborted, true);
      assert.equal(fixture.get("marketScanScreeningWorkbench")["aria-busy"], "false");
      assert.equal(fixture.get("marketScanScreeningRefresh").disabled, false);
      release(screeningResponses().breadth);
      await reading;
      assert.equal(attempts, 1, "Closing must not schedule evaluation or delta reads");
      assert.equal(controller.state.loaded, false);
      controller.dispose();
      const partial = screeningFixture();
      const partialController = createMarketScanScreeningController({ root: partial.root, async fetcher(url) {
        attempts += 1;
        if (url.endsWith("breadth")) return screeningResponses().breadth;
        throw Object.assign(new Error("bad frozen evidence"), { status: 409 });
      } });
      await partialController.open();
      assert.equal(attempts, 4);
      assert.equal(partialController.state.loaded, false);
      assert.match(partial.get("marketScanScreeningSummaryStatus").textContent, /部分证据读取失败/);
      assert.match(partial.get("marketScanScreeningBreadth").innerHTML, /冻结股票池/);
      await partialController.open();
      assert.equal(attempts, 7, "A failed read cannot become a permanently cached success");
      partialController.dispose();
    ''' + _screening_controller_fixture())


def _screening_controller_fixture() -> str:
    return r'''
      async function flushPromises() {
        for (let index = 0; index < 20; index += 1) await Promise.resolve();
      }
      function screeningFixture() {
        const elements = new Map();
        function get(id) {
          if (!elements.has(id)) {
            const handlers = new Map();
            elements.set(id, {
              textContent: "", innerHTML: "", disabled: false, open: true, value: "", dataset: {},
              setAttribute(name, value) { this[name] = String(value); },
              removeAttribute(name) { delete this[name]; },
              addEventListener(name, handler) { handlers.set(name, handler); },
              dispatch(name) { handlers.get(name)?.({ target: this }); },
            });
          }
          return elements.get(id);
        }
        get("marketScanTableWrap").dataset.marketScanRunId = "42";
        get("marketScanStatus").value = "success";
        get("marketScanSort").value = "rank";
        get("marketScanOrder").value = "asc";
        return { get, root: { getElementById: get, querySelectorAll: () => [] } };
      }
      function screeningResponses() {
        const digest = "a".repeat(64);
        const evidence = {
          run_id: 42, status: "success", mode: "official", scope: "沪市 + 深市 + 北交所当前上市A股",
          data_date: "2026-08-11", quote_date: "2026-08-11", rule_version: "full-market-score-v4",
          finished_at: "2026-08-11T16:00:00+08:00", snapshot_digest: digest,
          snapshot_seal_origin: "publication", snapshot_sealed_at: "2026-08-11T16:00:00+08:00",
        };
        const spec = {
          schema_version: "screen-spec-v2", status: "success", markets: [], industries: [],
          is_st: null, is_new: null, ranges: {}, keyword: null, sort: [{ field: "rank", order: "asc" }],
        };
        return {
          breadth: {
            schema_version: "market-scan-breadth-v1", evidence, canonical_digest: digest,
            population: { total: 0, by_status: {}, by_market: {} },
            score: { present_count: 0, missing_count: 0, min: null, max: null, mean: null,
              percentiles: { p10: null, p25: null, p50: null, p75: null, p90: null },
              bins: Array.from({ length: 10 }, (_, index) => ({ lower: index * 10, upper: (index + 1) * 10, count: 0 })) },
            change: { advancing: 0, flat: 0, declining: 0, missing: 0 }, industries: [],
          },
          evaluation: {
            schema_version: "market-scan-screen-evaluation-v1", evidence, spec, spec_digest: digest, canonical_digest: digest,
            population_count: 0, matched_count: 0,
            funnel: [{ index: 1, condition_code: "status", label: "结果状态", input_count: 0, matched_count: 0, excluded_count: 0, missing_count: 0 }],
            exclusion_reasons: [], matched: { items: [], total: 0, page: 1, page_size: 100, page_count: 0 },
            matched_explanations: [], near_misses: [],
          },
          delta: {
            schema_version: "market-scan-delta-v1", status: "unavailable", unavailable_reason: "previous_same_cohort_not_found",
            current: evidence, previous: null, canonical_digest: digest,
            cohort: { mode: "official", scope: evidence.scope, rule_version: evidence.rule_version },
            summary: { previous_present_count: 0, current_present_count: 0, compared_symbol_count: 0,
              evidence_detail_scope: "top100_union", evidence_change_reason_counts: [] },
            top_buckets: [], rank_score_changes: [], exposure_changes: [], evidence_changes: [],
          },
        };
      }
    '''


def _run_node(script: str) -> str:
    result = subprocess.run(
        ["node", "--input-type=module", "--eval", script],
        cwd=ROOT,
        check=True,
        capture_output=True,
        text=True,
    )
    return result.stdout
