import { readFileSync } from "node:fs";
import { discoveryPreset } from "./discovery-api-fixtures.mjs";
import { marketScanPollingIdentityPayload, mockApi } from "./frontend-flow-api-fixtures.mjs";
import { comparisonRun } from "./market-scan-comparison-fixtures.mjs";

const BASE = JSON.parse(readFileSync(new URL("../fixtures/market_scan_screen_evaluation_v2.json", import.meta.url), "utf8"));
export const IMPACT_UNSAFE_NAME = '<img src=x onerror="window.impactXss=true">零值样本';
const DIMENSIONS = [[90, 10], [0, 10], [null, 10], [70, 90], [90, 0], [90, null]];
const NAMES = ["满足全部", IMPACT_UNSAFE_NAME, "置信度缺失", "同时失败两条件", "风险零值", "风险缺失"];
const LABELS = { status: "结果状态", "range.confidence": "置信度区间", "range.risk": "风险区间", "range.score": "趋势强度区间" };

export function impactRun(overrides = {}) {
  return comparisonRun({ status: "success", total_count: 6, processed_count: 6, success_count: 6,
    missing_count: 0, coverage_pct: 100, ...overrides });
}

export function impactRows(run) {
  return DIMENSIONS.map(([confidence, risk], index) => ({
    ...structuredClone(BASE.matched.items[0]), run_id: run.id, symbol: `60000${index}.SH`, code: `60000${index}`,
    market: "SH", board: "上海A股（主板）", name: NAMES[index], industry: "银行", rank: index + 1, status: "success",
    score: 90, raw_score: 90, trend_score: 90, leader_score: 90, data_quality_score: 96,
    confidence, risk, tradability: 80, price: 10, amount: 800000000, change_pct: 1.2, turnover_rate: 0.8,
    volume_ratio: 1.1, tags: [], metrics: {}, upside_probabilities: {},
    score_details: { components: { score_dimensions: { scores: { confidence, risk, tradability: 80 } } } },
    data_date: run.data_date, quote_timestamp: "2026-08-11 15:00:00", quote_observed_at: "2026-08-11 15:00:01",
    quote_source: "合成行情", kline_source: "合成前复权日K", metadata_source: "合成元数据", adjustment_mode: "qfq",
    updated_at: "2026-08-11 16:00:00", reason: "合成冻结行", error: null,
  }));
}

export function impactSpec(ranges = {}, sort = [{ field: "rank", order: "asc" }]) {
  return { schema_version: "screen-spec-v2", status: "success", markets: [], industries: [],
    is_st: null, is_new: null, ranges, keyword: null, sort };
}

function conditions(spec) {
  const result = spec.status ? [{ code: "status", field: "status", value: spec.status }] : [];
  for (const field of Object.keys(BASE.spec.ranges)) {
    if (spec.ranges[field]) result.push({ code: `range.${field}`, field, bounds: spec.ranges[field] });
  }
  return result;
}

function fails(row, condition) {
  const value = row[condition.field];
  if (!condition.bounds) return value !== condition.value;
  return value == null || (condition.bounds.min != null && value < condition.bounds.min)
    || (condition.bounds.max != null && value > condition.bounds.max);
}

function evidence(run) {
  return Object.fromEntries(Object.keys(BASE.evidence).map((key) => [key, key === "run_id" ? run.id : run[key]]));
}

export function impactEvaluation(run, request) {
  const spec = structuredClone(request.spec);
  spec.ranges = Object.fromEntries(Object.keys(BASE.spec.ranges).map((field) => [field,
    spec.ranges[field] ? { min: null, max: null, ...spec.ranges[field] } : null]));
  const rules = conditions(spec);
  const rows = impactRows(run);
  const failures = rows.map((row) => rules.filter((rule) => fails(row, rule)));
  const matched = rows.filter((_, index) => failures[index].length === 0);
  let remaining = rows;
  const funnel = rules.map((rule, index) => {
    const input = remaining;
    remaining = input.filter((row) => !fails(row, rule));
    return { index: index + 1, condition_code: rule.code, label: LABELS[rule.code], input_count: input.length,
      matched_count: remaining.length, excluded_count: input.length - remaining.length,
      missing_count: input.filter((row) => fails(row, rule) && row[rule.field] == null).length };
  });
  const condition_impacts = rules.map((rule) => {
    const additions = rows.filter((_, index) => failures[index].length === 1 && failures[index][0].code === rule.code);
    return { condition_code: rule.code, label: LABELS[rule.code], additional_count: additions.length,
      missing_additional_count: additions.filter((row) => row[rule.field] == null).length,
      matched_without_condition: matched.length + additions.length, examples: additions.slice(0, 3).map((row) => ({
        run_id: run.id, symbol: row.symbol, code: row.code, market: row.market, name: row.name, status: row.status,
        observed_value: row[rule.field], missing: row[rule.field] == null,
      })) };
  });
  const near_misses = rows.flatMap((row, index) => failures[index].length === 1 ? [{ item: row,
    failed_conditions: failures[index].map((rule) => ({ code: `${rule.code}${row[rule.field] == null ? ".missing" : ""}`,
      label: LABELS[rule.code], missing: row[rule.field] == null })) }] : []).slice(0, request.near_miss_limit);
  // These browser fixtures exercise opaque digest shape and full request binding.
  // Backend contract tests cover canonical Python serialization and hash integrity.
  return { schema_version: "market-scan-screen-evaluation-v2", evidence: evidence(run), spec, spec_digest: "b".repeat(64),
    population_count: rows.length, matched_count: matched.length, funnel, condition_impacts,
    exclusion_reasons: rules.flatMap((rule) => {
      const excluded = rows.filter((row) => fails(row, rule));
      return excluded.length ? [{ code: rule.code, label: LABELS[rule.code], count: excluded.length,
        missing_count: excluded.filter((row) => row[rule.field] == null).length }] : [];
    }),
    matched: { items: matched, total: matched.length, page: 1, page_size: request.page_size,
      page_count: matched.length ? 1 : 0 },
    matched_explanations: matched.map((row) => ({ symbol: row.symbol, passed_conditions: rules.map((rule) => rule.code) })),
    near_misses, canonical_digest: "c".repeat(64) };
}

function resultPayload(run, query) {
  const ranges = {};
  for (const [parameter, field, bound] of [["min_confidence", "confidence", "min"], ["max_risk", "risk", "max"], ["min_score", "score", "min"]]) {
    if (query.has(parameter)) ranges[field] = { [bound]: Number(query.get(parameter)) };
  }
  const spec = impactSpec(ranges);
  const items = impactRows(run).filter((row) => conditions(spec).every((condition) => !fails(row, condition)));
  return { run, items, total: items.length, page: 1, page_size: 100, page_count: items.length ? 1 : 0, probability_research: null };
}

function breadth(run) {
  return { schema_version: "market-scan-breadth-v1", evidence: evidence(run), population: { total: 6, by_status: { success: 6 }, by_market: { SH: 6 } },
    score: { present_count: 6, missing_count: 0, min: 90, max: 90, mean: 90,
      percentiles: { p10: 90, p25: 90, p50: 90, p75: 90, p90: 90 },
      bins: Array.from({ length: 10 }, (_, index) => ({ lower: index * 10, upper: (index + 1) * 10, count: index === 9 ? 6 : 0 })) },
    change: { advancing: 6, flat: 0, declining: 0, missing: 0 }, industries: [{ industry: "银行", count: 6, score_present_count: 6, average_score: 90 }],
    canonical_digest: "d".repeat(64) };
}

function delta(run) {
  const current = evidence(run); delete current.quote_date;
  return { schema_version: "market-scan-delta-v1", status: "unavailable", unavailable_reason: "previous_same_cohort_not_found",
    current, previous: null, cohort: { mode: run.mode, scope: run.scope, rule_version: run.rule_version },
    summary: { previous_present_count: 0, current_present_count: 6, compared_symbol_count: 0,
      evidence_detail_scope: "top100_union", evidence_change_reason_counts: [] },
    top_buckets: [], rank_score_changes: [], exposure_changes: [], evidence_changes: [], canonical_digest: "e".repeat(64) };
}

function presets() {
  return [80, 99].map((minimum, index) => ({ ...discoveryPreset({ name: index ? "空榜方案" : "高置信方案",
    criteria: { confidence: { min: minimum }, risk: { max: 50 } },
    sort: [{ field: "score", order: "desc" }], column_view: "research" }, index + 1), id: index + 7 }));
}

async function evaluationResponse(state, body) {
  const payload = impactEvaluation(structuredClone(state.run), body);
  state.evaluations.push(structuredClone(body));
  if (state.mutateNext) { state.mutateNext(payload); state.mutateNext = null; }
  if (state.holdNext) {
    state.holdNext = false;
    await new Promise((resolve) => { state.releaseHeld = resolve; });
  }
  state.returned.push(payload);
  return { payload };
}

export async function routeImpactFixture(page) {
  const state = { run: impactRun(), calls: [], evaluations: [], returned: [], results: [], applications: [],
    mutateNext: null, holdNext: false, releaseHeld: null, holdResultNext: false, releaseHeldResult: null,
    returnedResults: [], heldResultRequest: null, holdPresetNext: false, releaseHeldPreset: null,
    heldPresetRequest: null, returnedPresets: [] };
  const saved = presets();
  await mockApi(page, { async api(url, request) {
    const path = url.pathname;
    state.calls.push({ method: request.method(), path });
    if (path === "/api/market-scans/polling-identity") return { payload: marketScanPollingIdentityPayload(state.run, state.run) };
    if (["/api/market-scans/latest", "/api/market-scans/latest-published", "/api/market-scans/42"].includes(path)) return { payload: state.run };
    if (path === "/api/market-scans") return { payload: { items: [state.run], total: 1, page: 1, page_size: 30, page_count: 1 } };
    if (path === "/api/market-scans/42/results") {
      state.results.push(Object.fromEntries(url.searchParams));
      const payload = resultPayload(state.run, url.searchParams);
      if (state.holdResultNext) {
        state.holdResultNext = false;
        state.heldResultRequest = request;
        await new Promise((resolve) => { state.releaseHeldResult = resolve; });
      }
      state.returnedResults.push(payload);
      return { payload };
    }
    if (path === "/api/market-scans/42/breadth") return { payload: breadth(state.run) };
    if (path === "/api/market-scans/42/delta") return { payload: delta(state.run) };
    if (path === "/api/market-scans/42/screen/evaluate") return evaluationResponse(state, request.postDataJSON());
    if (path === "/api/discovery/presets" && request.method() === "GET") return { payload: { items: saved, total: 2, page: 1, page_size: 100, page_count: 1 } };
    if (/^\/api\/discovery\/presets\/[78]\/apply$/.test(path)) {
      const body = request.postDataJSON(); const preset = saved.find((entry) => path.includes(`/${entry.id}/`));
      state.applications.push({ body, preset });
      const items = impactRows(state.run).filter((row) => conditions(impactSpec(preset.criteria)).every((rule) => !fails(row, rule)))
        .map((row, index) => ({ ...row, position: index + 1, source_rank: row.rank, quality: row.data_quality_score,
          trend: row.trend_score, change: row.change_pct, turnover: row.turnover_rate }));
      const payload = { preset, run_id: state.run.id, rule_version: state.run.rule_version, items, total: items.length,
        page: body.page, page_size: body.page_size, page_count: items.length ? 1 : 0 };
      if (state.holdPresetNext) {
        state.holdPresetNext = false; state.heldPresetRequest = request;
        await new Promise((resolve) => { state.releaseHeldPreset = resolve; });
      }
      state.returnedPresets.push(payload);
      return { payload };
    }
    if (path === "/api/discovery/runs/42/rank-changes") return { payload: { current_run_id: 42, previous_run_id: null,
      current_rule_version: state.run.rule_version, previous_rule_version: null, comparable: false,
      reason: "no_previous_run", items: [], total: 0, page: 1, page_size: 200, page_count: 0 } };
    if (request.method() !== "GET") return { status: 405, payload: { detail: "验收不允许写入" } };
    return null;
  } });
  return state;
}
