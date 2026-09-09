import { escapeHtml } from "./dom.js";
import { formatNumber } from "./format.js";
import { isMarketScanTop100RefreshRun, marketScanModeLabel } from "./market-scan-contracts.js";

const RUN_STATUS_LABELS = Object.freeze({
  success: "扫描完成",
  degraded: "降级完成",
});

export function marketScanHistoryElements(getElement) {
  return {
    history: getElement("marketScanHistory"),
    historyRun: getElement("marketScanHistoryRun"),
    historyStatus: getElement("marketScanHistoryStatus"),
    historyDate: getElement("marketScanHistoryDate"),
    historyRefresh: getElement("marketScanHistoryRefresh"),
    historyFeedback: getElement("marketScanHistoryFeedback"),
    historyPagination: getElement("marketScanHistoryPagination"),
    historyPrev: getElement("marketScanHistoryPrev"),
    historyNext: getElement("marketScanHistoryNext"),
    historyPageInfo: getElement("marketScanHistoryPageInfo"),
  };
}

export function renderMarketScanHistoryLoading(elements) {
  setHistoryBusy(elements, true);
  setText(elements.historyFeedback, "正在读取历史批次...");
  elements.historyFeedback.className = "";
}

export function renderMarketScanHistory(elements, payload, selectedRunId, selectedMode, retainedRun = null) {
  const selected = selectedRunId == null ? "" : String(selectedRunId);
  const items = retainedRun ? [retainedRun, ...payload.items] : payload.items;
  const options = items.map((run) => {
    const status = RUN_STATUS_LABELS[run.status] || run.status;
    const kind = isMarketScanTop100RefreshRun(run) ? "TOP100快更" : "全市场";
    const retained = run === retainedRun ? " · 当前浏览（不在本次查询中）" : "";
    const label = `#${run.id} · ${kind} · ${run.quote_date || run.data_date} · ${status} · ${formatNumber(run.coverage_pct, 1)}%${retained}`;
    return `<option value="${run.id}">${escapeHtml(label)}</option>`;
  }).join("");
  elements.historyRun.innerHTML = `<option value="">最近发布</option>${options}`;
  elements.historyRun.value = items.some((run) => String(run.id) === selected) ? selected : "";
  elements.historyPagination.dataset.page = String(payload.page || 1);
  elements.historyPagination.dataset.pageCount = String(payload.page_count || 0);
  setHistoryBusy(elements, false);
  const range = payload.items.length ? `，当前第 ${(payload.page - 1) * payload.page_size + 1}–${(payload.page - 1) * payload.page_size + payload.items.length} 个` : "";
  setText(elements.historyFeedback, `找到 ${payload.total} 个${marketScanModeLabel(selectedMode)}已发布批次${range}。`);
  elements.historyFeedback.className = "";
}

export function renderMarketScanHistoryError(elements, message) {
  setHistoryBusy(elements, false);
  setText(elements.historyFeedback, message);
  elements.historyFeedback.className = "error";
}


export function renderMarketScanHistoryCancelled(elements) {
  setHistoryBusy(elements, false);
  setText(elements.historyFeedback, "历史读取已取消，原批次列表保留，可重新查询。");
  elements.historyFeedback.className = "";
}


function setHistoryBusy(elements, busy) {
  setAttribute(elements.history, "aria-busy", String(busy));
  elements.historyRefresh.disabled = busy;
  const page = Number(elements.historyPagination.dataset.page) || 1;
  const pageCount = Number(elements.historyPagination.dataset.pageCount) || 0;
  elements.historyPagination.hidden = pageCount <= 1;
  elements.historyPrev.disabled = busy || page <= 1;
  elements.historyNext.disabled = busy || page >= pageCount;
  setText(elements.historyPageInfo, `历史第 ${pageCount ? page : 0} / ${pageCount} 页`);
}

export function marketScanHistoryFilters(elements) {
  return {
    status: elements.historyStatus.value || "published",
    dataDate: elements.historyDate.value || "",
  };
}

export function selectedMarketScanHistoryRunId(elements) {
  const value = Number(elements.historyRun.value);
  return Number.isInteger(value) && value > 0 ? value : null;
}

function setText(element, value) {
  element.textContent = String(value ?? "--");
}

function setAttribute(element, name, value) {
  if (typeof element?.setAttribute === "function") element.setAttribute(name, String(value));
  else if (element) element[name] = String(value);
}
