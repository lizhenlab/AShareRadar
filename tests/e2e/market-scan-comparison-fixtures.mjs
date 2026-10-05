import { readFileSync } from "node:fs";
import { discoveryPreset } from "./discovery-api-fixtures.mjs";
import { marketScanPollingIdentityPayload, mockApi } from "./frontend-flow-api-fixtures.mjs";

const CONTRACT = JSON.parse(readFileSync(new URL("../fixtures/market_scan_comparison_v1.json", import.meta.url), "utf8"));
export const SYMBOLS = Object.freeze({ first: "600000.SH", missing: "600001.SH", second: "600002.SH", third: "600003.SH", fourth: "600004.SH", fifth: "600005.SH" });
export const UNSAFE_NAME = '<img src=x onerror="window.comparisonXss=true">冻结样本';

export function comparisonRun(overrides = {}) {
  return {
    id: 42, status: "degraded", trigger: "manual", mode: "official", rule_version: "full-market-score-v4-test",
    as_of: "2026-08-11 16:00:00", data_date: "2026-08-11", quote_date: "2026-08-11",
    scope: "沪市 + 深市 + 北交所当前上市A股", total_count: 102, excluded_count: 0, processed_count: 102,
    success_count: 101, missing_count: 1, skipped_count: 0, retry_count: 0, progress_pct: 100,
    coverage_pct: 99.02, created_at: "2026-08-11 16:00:00", updated_at: "2026-08-11 16:01:00",
    started_at: "2026-08-11 16:00:01", finished_at: "2026-08-11 16:01:00", duration_ms: 59000,
    message: "冻结批次完成", stock_pool_source: "fixture", task_run_id: null, retry_of_run_id: null,
    snapshot_digest: "a".repeat(64), snapshot_seal_origin: "publication", snapshot_sealed_at: "2026-08-11 16:01:00",
    ...overrides,
  };
}

function frozenItem(index, run) {
  const missing = index === 1;
  const code = String(600000 + index);
  const score = missing ? null : 90 - index % 10;
  return {
    ...structuredClone(CONTRACT.items[0]), run_id: run.id, symbol: `${code}.SH`, code, market: "SH", board: "上海A股（主板）",
    name: index === 0 ? UNSAFE_NAME : `冻结样本${index}`, industry: "银行", list_date: "2000-01-01",
    status: missing ? "missing" : "success", rank: missing ? null : index + 1,
    score, raw_score: score, trend_score: score, leader_score: score, data_quality_score: missing ? null : 96,
    price: missing ? null : 10 + index, change_pct: missing ? null : 1.2, turnover_rate: missing ? null : 0.8,
    amount: missing ? null : 800000000, confidence: missing ? null : 92, risk: missing ? null : 10 + index % 70, tradability: missing ? null : 85,
    reason: missing ? "日K缺失，不能评分" : "冻结规则，仅用于审计", error: null,
    data_date: "2026-08-11", quote_timestamp: missing ? null : "2026-08-11 15:00:00",
    quote_observed_at: missing ? null : "2026-08-11 15:00:01", updated_at: "2026-08-11 16:00:00",
    quote_source: missing ? null : "fixture", kline_source: missing ? null : "fixture", metadata_source: "fixture",
  };
}

function resultRow(item) {
  const { board: _board, confidence, risk, tradability, ...row } = item;
  return {
    ...row, volume_ratio: row.status === "success" ? 1.1 : null, tags: [], metrics: {}, upside_probabilities: {},
    score_details: { components: { score_dimensions: { scores: { confidence, risk, tradability } } } },
  };
}

function pagePayload(run, query) {
  let items = Array.from({ length: 102 }, (_, index) => frozenItem(index, run));
  const status = query.get("status") || "success";
  if (status !== "all") items = items.filter((item) => item.status === status);
  const page = Number(query.get("page")) || 1;
  const pageSize = Number(query.get("page_size")) || 100;
  return {
    run, items: items.slice((page - 1) * pageSize, page * pageSize).map(resultRow), total: items.length,
    page, page_size: pageSize, page_count: Math.ceil(items.length / pageSize), probability_research: null,
  };
}

export function comparisonPayload(run, symbols) {
  const evidence = Object.fromEntries(Object.keys(CONTRACT.evidence).map((key) => [key, key === "run_id" ? run.id : run[key]]));
  return {
    ...structuredClone(CONTRACT), evidence, requested_symbols: [...symbols], action_source_eligible: false,
    items: symbols.map((symbol) => frozenItem(Number(symbol.slice(0, 6)) - 600000, run)),
    // Browser validates the opaque server digest's shape and request binding;
    // canonical Python hashing itself is covered by the backend contract tests.
    canonical_digest: "c".repeat(64),
  };
}

function savedPreset() {
  return discoveryPreset({ name: "冻结对比方案", criteria: {}, sort: [{ field: "score", order: "desc" }], column_view: "overview" }, 1);
}

function leaderboard(preset, body, run) {
  const items = [0, 2, 3].map((index, position) => {
    const row = frozenItem(index, run);
    return { ...row, position: position + 1, source_rank: row.rank, quality: row.data_quality_score,
      trend: row.trend_score, change: row.change_pct, turnover: row.turnover_rate };
  });
  return { preset, run_id: body.run_id, rule_version: run.rule_version, items, total: items.length,
    page: body.page, page_size: body.page_size, page_count: 1 };
}

export async function routeComparisonFixture(page, options = {}) {
  const state = {
    run: comparisonRun(options.legacy ? { snapshot_seal_origin: "legacy_backfill" } : {}),
    calls: [], results: [], latestCalls: 0, failNext: false, wrongDigestNext: false,
    holdNext: false, releaseHeld: null, returned: [], schemeCalls: [],
  };
  const historical = comparisonRun({ id: 41, snapshot_digest: "d".repeat(64) });
  const preset = savedPreset();
  await mockApi(page, {
    async api(url, request) {
      const path = url.pathname;
      if (path === "/api/market-scans/polling-identity") return { payload: marketScanPollingIdentityPayload(state.run, state.run) };
      if (["/api/market-scans/latest", "/api/market-scans/latest-published"].includes(path)) { state.latestCalls += 1; return { payload: state.run }; }
      if (path === "/api/market-scans" && request.method() === "GET") return { payload: { items: [state.run, historical], total: 2, page: 1, page_size: Number(url.searchParams.get("page_size")) || 100, page_count: 1 } };
      if (/^\/api\/market-scans\/(41|42)$/.test(path)) return { payload: path.endsWith("41") ? historical : state.run };
      if (/^\/api\/market-scans\/(41|42)\/results$/.test(path)) {
        state.results.push({ path, query: Object.fromEntries(url.searchParams) });
        return { payload: pagePayload(path.includes("/41/") ? historical : state.run, url.searchParams) };
      }
      if (/^\/api\/market-scans\/(41|42)\/compare$/.test(path)) return await comparisonResponse(state, request, path.includes("/41/") ? historical : state.run);
      if (path === "/api/discovery/presets") return { payload: { items: [preset], total: 1, page: 1, page_size: 100, page_count: 1 } };
      if (path === "/api/discovery/presets/7/apply") {
        const body = request.postDataJSON(); state.schemeCalls.push(body);
        return { payload: leaderboard(preset, body, state.run) };
      }
      if (path === "/api/discovery/runs/42/rank-changes") return { payload: { current_run_id: 42, previous_run_id: null,
        current_rule_version: state.run.rule_version, previous_rule_version: null, comparable: false,
        reason: "no_previous_run", items: [], total: 0, page: 1, page_size: 200, page_count: 0 } };
      return null;
    },
  });
  return state;
}

async function comparisonResponse(state, request, run) {
  const body = request.postDataJSON();
  state.calls.push(body);
  if (state.failNext) { state.failNext = false; return { status: 409, payload: { detail: "榜单快照已变化，请重新选择" } }; }
  const payload = comparisonPayload(structuredClone(run), body.symbols);
  if (state.wrongDigestNext) { state.wrongDigestNext = false; payload.evidence.snapshot_digest = "e".repeat(64); }
  if (state.holdNext) {
    state.holdNext = false;
    await new Promise((resolve) => { state.releaseHeld = resolve; });
  }
  state.returned.push(payload);
  return { payload };
}
