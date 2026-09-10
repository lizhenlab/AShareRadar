import { mockApi } from "./frontend-flow-api-fixtures.mjs";

export async function mockFuyaoApi(page, options = {}) {
  const state = { reads: [], writes: [], actions: [], jobs: [], records: {} };
  await mockApi(page, { async api(url, request) {
    if (!url.pathname.startsWith("/api/fuyao/")) return null;
    const body = request.method() === "POST" ? request.postDataJSON() : null;
    if (request.method() === "POST") {
      state.actions.push(url.pathname);
      if (body) state.writes.push(body);
    } else state.reads.push({ path: url.pathname, symbol: url.searchParams.get("symbol") });
    const custom = await options.api?.(url, request, state);
    if (custom) return custom;
    const taskPath = url.pathname.match(/^\/api\/fuyao\/jobs\/([^/]+)(?:\/(cancel|retry))?$/);
    if (taskPath) return taskResponse(state, taskPath[1], taskPath[2]);
    if (url.pathname === "/api/fuyao/status") return { payload: fuyaoStatus(state.jobs) };
    if (url.pathname === "/api/fuyao/stock") return { payload: fuyaoStock(url.searchParams.get("symbol")) };
    if (url.pathname === "/api/fuyao/market") {
      return { payload: { sectors: null, sentiment: null, history: null, read_mode: "local_cache_only" } };
    }
    if (url.pathname === "/api/fuyao/jobs" && body) {
      const job = fuyaoJob(body.kind, {request:body});
      state.jobs = [job];
      state.records[job.id] = job;
      return { status: 202, payload: job };
    }
    throw new Error(`Unexpected synthetic Fuyao request: ${request.method()} ${url.pathname}`);
  } });
  return state;
}

export function fuyaoStatus(jobs = []) {
  return { enabled: true, configured: true, closed: false, persistent_daily_requests: 7,
    daily_requests: 7, daily_request_limit: 200, permissions: {}, last_error: null,
    download_hosts_configured: true, jobs, active_jobs: jobs.filter(job => ["running", "cancelling"].includes(job.status)).map(job => job.id),
    billing_status: "以账号费用条款为准", budget_scope: "当前项目每日请求上限，包含重试" };
}

export function fuyaoJob(kind = "financials", overrides = {}) {
  return { id: "synthetic-fuyao-job", kind, status: "running", created_at: "2026-09-10T02:00:00+00:00",
    request: null, parent_job_id: null, completed_symbols: [], updated_at: "2026-09-10T02:00:00+00:00", progress: null,
    finished_at: null, completed: 0, total: 2, message: "合成作业进行中", errors: [], ...overrides };
}

export function fuyaoStock(rawSymbol = "600519.SH") {
  const symbol = rawSymbol.includes(".") ? rawSymbol : `${rawSymbol}.${rawSymbol.startsWith("6") ? "SH" : "SZ"}`;
  const label = symbol === "000001.SZ" ? "平安专属事实" : "茅台专属事实";
  const periods = [2025, 2024].map(year => ({
    period_end: `${year}-12-31`, period_type: "annual", currency: "CNY", alignment: "partial",
    supplier_report_dates: [`${year + 1}-03-20T00:00:00+08:00`],
    statements: [{ source_kind: "income", supplier_report_date: `${year + 1}-03-20T00:00:00+08:00`,
      fiscal_year: year, fiscal_period: "Q4", currency: "CNY" }],
    metrics: [
      { key: "revenue", label: `${label}${year}收入`, value: year === 2025 ? 1234 : 987,
        raw_value: year === 2025 ? "1234" : "987", unit: null, source_kind: "income", ability: null },
      { key: "roe", label: "净资产收益率", value: 12.5, raw_value: "12.5%", unit: "%", source_kind: "indicators", ability: "profitability" },
      { key: "growth", label: "未标单位原值", value: 9.3, raw_value: "9.3", unit: null, source_kind: "indicators", ability: "growth" },
    ],
  }));
  return { symbol, available: true, read_mode: "local_cache_only", valuation: null, valuation_history: null,
    financial_health: null, financials: { schema_version: "fuyao-financial-v1", symbol,
      fetched_at: "2026-09-10T02:00:00+00:00", source: "同花顺扶摇", periods, score: null,
      metric_scope: "observed_financial_facts", point_in_time: false,
      warnings: ["当前财报观测不能证明历史时点可知，不生成财务评分。"] } };
}

function taskResponse(state, id, action) {
  const job = state.jobs.find(item => item.id === id) || state.records[id];
  if (!job) return { status: 404, payload: { detail: "任务不存在" } };
  if (!action) return { payload: job };
  if (action === "cancel") {
    const updated = { ...job, status: "cancelling", message: "合成停止请求已接收" };
    state.jobs = state.jobs.map(item => item.id === id ? updated : item);
    state.records[id] = updated;
    return { status: 202, payload: updated };
  }
  const symbols = (job.request?.symbols || []).filter(symbol => !job.completed_symbols?.includes(symbol));
  const child = fuyaoJob(job.kind, { id: `${id}-child`, parent_job_id: id, total: symbols.length,
    request: { ...job.request, symbols }, message: "合成补做进行中" });
  state.jobs = [child, ...state.jobs];
  state.records[child.id] = child;
  return { status: 202, payload: child };
}
