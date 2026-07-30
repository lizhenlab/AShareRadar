import { escapeHtml } from "./dom.js";
import { formatNumber } from "./format.js";
import { marketScanModeLabel } from "./market-scan-contracts.js";

const RUN_STATUS_LABELS = Object.freeze({
  success: "扫描完成",
  degraded: "降级完成",
});

export function renderMarketScanHistoryLoading(elements) {
  setAttribute(elements.history, "aria-busy", "true");
  elements.historyRefresh.disabled = true;
  setText(elements.historyFeedback, "正在读取历史批次...");
  elements.historyFeedback.className = "";
}

export function renderMarketScanHistory(elements, payload, selectedRunId, selectedMode) {
  const selected = selectedRunId == null ? "" : String(selectedRunId);
  const options = payload.items.map((run) => {
    const status = RUN_STATUS_LABELS[run.status] || run.status;
    const label = `#${run.id} · ${run.quote_date || run.data_date} · ${status} · ${formatNumber(run.coverage_pct, 1)}%`;
    return `<option value="${run.id}">${escapeHtml(label)}</option>`;
  }).join("");
  elements.historyRun.innerHTML = `<option value="">最近发布</option>${options}`;
  elements.historyRun.value = payload.items.some((run) => String(run.id) === selected) ? selected : "";
  setAttribute(elements.history, "aria-busy", "false");
  elements.historyRefresh.disabled = false;
  setText(elements.historyFeedback, `找到 ${payload.total} 个${marketScanModeLabel(selectedMode)}已发布批次。`);
  elements.historyFeedback.className = "";
}

export function renderMarketScanHistoryError(elements, message) {
  setAttribute(elements.history, "aria-busy", "false");
  elements.historyRefresh.disabled = false;
  setText(elements.historyFeedback, message);
  elements.historyFeedback.className = "error";
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
