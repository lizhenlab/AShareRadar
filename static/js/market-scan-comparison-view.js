import { escapeHtml } from "./dom.js";
import { formatAmount } from "./format.js";

const FIELDS = [
  ["status", "冻结状态"], ["rank", "原始冻结排名"], ["score", "原始冻结得分"], ["raw_score", "未取整原始分"],
  ["trend_score", "趋势分"], ["leader_score", "龙头分"], ["data_quality_score", "数据质量"],
  ["confidence", "置信度"], ["risk", "风险（越高风险越大）"], ["tradability", "可交易性维度"],
  ["price", "冻结价格"], ["change_pct", "涨跌幅（%）"], ["turnover_rate", "换手率（%）"], ["amount", "成交额"],
  ["board", "上市板块"], ["industry", "行业"], ["is_st", "ST 标记"], ["is_new", "次新股标记"], ["list_date", "上市日期"],
  ["data_date", "数据日期"], ["quote_timestamp", "行情时间"], ["quote_observed_at", "行情观测时间"],
  ["quote_source", "行情来源"], ["kline_source", "K 线来源"], ["metadata_source", "基础资料来源"],
  ["adjustment_mode", "复权方式"], ["quote_fallback_used", "行情使用回退"], ["kline_fallback_used", "K 线使用回退"],
  ["metadata_degraded", "基础资料降级"], ["degradation_reasons", "降级原因"], ["reason", "冻结说明"], ["error", "数据错误"], ["updated_at", "结果保存时间"],
];
const STATUS = { success: "有效排名", missing: "数据缺失", skipped: "已跳过", pending: "待处理" };

export function createMarketScanComparisonView(root) {
  const ids = ["Comparison", "CompareCount", "CompareRun", "CompareClear", "CompareDiffOnly", "CompareExport", "CompareStatus", "CompareTable", "CompareSelected"];
  const elements = Object.fromEntries(ids.map((key) => [key, root?.getElementById?.(`marketScan${key}`)]));
  if (Object.values(elements).some((element) => !element)) return null;
  return {
    bind: (handlers) => bindComparisonView(root, elements, handlers),
    render: (state) => renderComparisonView(root, elements, state),
    renderRows: (state) => renderSelectionButtons(root, state),
    save: (payload) => saveComparison(root, payload),
  };
}

function bindComparisonView(root, elements, handlers) {
  const listeners = [];
  const on = (element, event, handler) => {
    element?.addEventListener(event, handler);
    if (element) listeners.push(() => element.removeEventListener(event, handler));
  };
  on(elements.CompareRun, "click", handlers.compare);
  on(elements.CompareClear, "click", handlers.clear);
  on(elements.CompareExport, "click", handlers.export);
  on(elements.CompareDiffOnly, "change", () => handlers.differences(elements.CompareDiffOnly.checked));
  on(root.getElementById("marketScanRows"), "click", (event) => {
    const button = event.target.closest?.("[data-market-scan-compare-symbol]");
    if (button) handlers.toggle(button.dataset.marketScanCompareSymbol, Number(button.dataset.marketScanCompareRunId));
  });
  on(elements.CompareSelected, "click", (event) => {
    const button = event.target.closest?.("[data-market-scan-compare-remove]");
    if (button) handlers.remove(button.dataset.marketScanCompareRemove);
  });
  const Observer = root.defaultView?.MutationObserver;
  const rows = root.getElementById("marketScanRows");
  const observer = Observer && rows ? new Observer(handlers.rowsChanged) : null;
  observer?.observe(rows, { childList: true });
  return () => { observer?.disconnect(); listeners.forEach((remove) => remove()); };
}

function renderComparisonView(root, elements, state) {
  const { symbols, payload, busy, message, differencesOnly, active } = state;
  elements.CompareCount.textContent = `已选 ${symbols.length} / 4`;
  elements.CompareRun.disabled = !active || !state.run || symbols.length < 2 || busy;
  elements.CompareClear.disabled = symbols.length === 0 && !busy;
  elements.CompareExport.disabled = !active || !payload || busy;
  elements.CompareDiffOnly.disabled = !payload || busy;
  elements.CompareDiffOnly.checked = differencesOnly;
  elements.Comparison.setAttribute("aria-busy", String(busy));
  elements.CompareStatus.textContent = message;
  elements.CompareSelected.innerHTML = symbols.map((symbol) => `<button type="button" class="mini-button" data-market-scan-compare-remove="${escapeHtml(symbol)}" aria-label="从对比移除 ${escapeHtml(symbol)}">${escapeHtml(symbol)} ×</button>`).join("");
  elements.CompareTable.hidden = !payload;
  elements.CompareTable.innerHTML = payload ? comparisonTable(payload, differencesOnly) : "";
  renderSelectionButtons(root, state);
}

function renderSelectionButtons(root, state) {
  for (const button of root.querySelectorAll?.("[data-market-scan-compare-symbol]") || []) {
    const selected = state.symbols.includes(button.dataset.marketScanCompareSymbol);
    const bound = Number(button.dataset.marketScanCompareRunId) === state.run?.id;
    button.setAttribute("aria-pressed", String(selected && bound));
    button.textContent = selected && bound ? "移出对比" : "加入对比";
    button.disabled = !state.active || !bound || (!selected && state.symbols.length >= 4);
  }
}

export function comparisonTable(payload, differencesOnly = false) {
  const items = payload.items;
  const fields = FIELDS.filter(([key]) => !differencesOnly || new Set(items.map((item) => JSON.stringify(item[key]))).size > 1);
  const header = items.map((item) => `<th scope="col">${escapeHtml(item.name)}<br><span>${escapeHtml(item.symbol)}</span></th>`).join("");
  const rows = fields.map(([key, label]) => `<tr data-comparison-field="${key}"><th scope="row">${escapeHtml(label)}</th>${items.map((item) => `<td>${escapeHtml(comparisonValue(item[key], key))}</td>`).join("")}</tr>`).join("");
  const evidence = payload.evidence;
  const empty = fields.length ? "" : `<tr><td colspan="${items.length + 1}">无可显示差异；相同或同样缺失不证明风险相同。</td></tr>`;
  const source = payload.action_source_eligible ? "发布来源已核验；不代表交易许可或收益预测。" : "仅供历史审计，未取得当前动作来源资格。";
  return `<p class="market-scan-comparison-evidence">批次 #${escapeHtml(evidence.run_id)} · 数据日 ${escapeHtml(evidence.data_date)} · 行情日 ${escapeHtml(evidence.quote_date)} · ${escapeHtml(evidence.rule_version)}<br>${source}<br>冻结摘要 <code>${escapeHtml(evidence.snapshot_digest)}</code><br>响应摘要 <code>${escapeHtml(payload.canonical_digest)}</code></p><table class="market-scan-comparison-table"><caption>同批次候选冻结数据对比${differencesOnly ? " · 只看差异" : ""}</caption><thead><tr><th scope="col">指标</th>${header}</tr></thead><tbody>${rows}${empty}</tbody></table>`;
}

function comparisonValue(value, key) {
  if (value === null || value === "") return "不可用";
  if (Array.isArray(value)) return value.length ? value.join("；") : "无记录（不代表无风险）";
  if (typeof value === "boolean") return value ? "是" : "否";
  if (key === "status") return STATUS[value];
  if (key === "amount") return `${formatAmount(value)}（${value} 元）`;
  return String(value);
}

function saveComparison(root, payload) {
  const urlApi = root.defaultView?.URL || globalThis.URL;
  const blob = new Blob([JSON.stringify(payload, null, 2)], { type: "application/json;charset=utf-8" });
  const url = urlApi.createObjectURL(blob);
  try {
    const link = root.createElement("a");
    link.href = url;
    link.download = `market-scan-${payload.evidence.run_id}-comparison.json`;
    link.click();
  } finally {
    urlApi.revokeObjectURL(url);
  }
}
