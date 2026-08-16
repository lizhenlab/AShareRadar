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


def _run_node(script: str) -> str:
    result = subprocess.run(
        ["node", "--input-type=module", "--eval", script],
        cwd=ROOT,
        check=True,
        capture_output=True,
        text=True,
    )
    return result.stdout
