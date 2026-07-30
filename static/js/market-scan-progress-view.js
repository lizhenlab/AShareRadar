import { escapeHtml } from "./dom.js";

const STAGE_LABELS = Object.freeze({
  stock_pool: "股票池",
  bulk_quotes: "批量行情",
  klines: "K 线获取",
  scoring: "评分",
  persistence: "持久化",
  publication: "发布验证",
});

export function renderMarketScanObservability(elements, run) {
  setText(elements.stage, run?.current_stage ? STAGE_LABELS[run.current_stage] || run.current_stage : terminalStage(run));
  setText(elements.elapsed, durationText(run?.elapsed_seconds));
  setText(elements.throughput, throughputText(run?.throughput_per_second));
  setText(elements.eta, etaText(run));
  elements.marketProgress.innerHTML = marketProgressHtml(run?.market_progress);
  const diagnostic = actionableDiagnostic(run);
  elements.diagnostic.hidden = !diagnostic;
  elements.diagnostic.textContent = diagnostic;
}

export function etaText(run) {
  if (!run || run.status !== "running") return "--";
  if (run.eta_seconds === null || run.eta_seconds === undefined || !Number.isFinite(Number(run.eta_seconds))) {
    return "估算中";
  }
  return durationText(run.eta_seconds);
}

export function actionableDiagnostic(run) {
  const detail = String(run?.last_error || run?.message || "").trim();
  if (!detail || !["degraded", "failed", "interrupted"].includes(run?.status)) return "";
  if (/覆盖不足|有效样本占比不足/.test(detail)) return `诊断：${detail}。建议检查股票池与数据源完整性后重试；系统不会发布部分正式榜单。`;
  if (/快照跨度|报价时间/.test(detail)) return `诊断：${detail}。建议待行情源稳定后重试，避免混用不同时点行情。`;
  if (/数据源|provider|调用未结束|超时|不可用/i.test(detail)) return `诊断：${detail}。建议等待数据源恢复后从断点重试，不要提高不安全并发。`;
  if (/评分分布|tie|饱和/.test(detail)) return `诊断：${detail}。请核对规则版本与输入数据，当前结果不会作为正式榜单发布。`;
  return `诊断：${detail}。可从问题项重试；历史已发布快照不会被覆盖。`;
}

function marketProgressHtml(progress) {
  const byMarket = new Map((Array.isArray(progress) ? progress : []).map((item) => [item.market, item]));
  return ["SH", "SZ", "BJ"].map((market) => {
    const item = byMarket.get(market) || {};
    const total = integer(item.total_count);
    const processed = integer(item.processed_count);
    const success = integer(item.success_count);
    const missing = integer(item.missing_count);
    const skipped = integer(item.skipped_count);
    const coverage = Number.isFinite(Number(item.coverage_pct)) ? `${Number(item.coverage_pct).toFixed(1)}%` : "--";
    return `<div><strong>${escapeHtml(market)}</strong><span>${processed}/${total} 已处理</span><span>${success} 成功 · ${coverage}</span><span>${missing} 缺失 · ${skipped} 跳过</span></div>`;
  }).join("");
}

function terminalStage(run) {
  return run && ["success", "degraded", "failed", "cancelled", "interrupted"].includes(run.status)
    ? "已结束"
    : "--";
}

function durationText(value) {
  if (value === null || value === undefined || value === "") return "--";
  const seconds = Number(value);
  if (!Number.isFinite(seconds) || seconds < 0) return "--";
  if (seconds < 60) return `${Math.round(seconds)} 秒`;
  const minutes = Math.floor(seconds / 60);
  const remainder = Math.round(seconds % 60);
  return `${minutes} 分 ${remainder} 秒`;
}

function throughputText(value) {
  if (value === null || value === undefined || value === "") return "估算中";
  const number = Number(value);
  return Number.isFinite(number) && number >= 0 ? `${number.toFixed(2)} 只/秒` : "估算中";
}

function integer(value) {
  const number = Number(value);
  return Number.isInteger(number) && number >= 0 ? number : 0;
}

function setText(element, value) {
  element.textContent = String(value ?? "--");
}
