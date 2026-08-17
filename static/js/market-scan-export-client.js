export const MARKET_SCAN_XLSX_MEDIA_TYPE = "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet";

const MARKET_SCAN_EXPORT_TIMEOUT_MS = 120000;

export async function marketScanExportError(response) {
  let payload = null;
  try { payload = await response.json(); } catch { /* Use the HTTP fallback below. */ }
  const detail = marketScanExportDetail(payload?.detail);
  return detail || `请求失败（HTTP ${response?.status || "未知"}）`;
}

export function marketScanExportMediaType(response) {
  return String(response?.headers?.get?.("content-type") || "").split(";", 1)[0].trim().toLowerCase();
}

export function exportTimeoutScope() {
  const controller = new AbortController();
  let timedOut = false;
  const timer = setTimeout(() => {
    timedOut = true;
    controller.abort();
  }, MARKET_SCAN_EXPORT_TIMEOUT_MS);
  return {
    signal: controller.signal,
    didTimeout: () => timedOut,
    dispose: () => clearTimeout(timer),
  };
}

function marketScanExportDetail(detail) {
  if (typeof detail === "string") return detail.trim();
  if (Array.isArray(detail)) return detail.map(marketScanExportDetail).filter(Boolean).join("；");
  if (detail && typeof detail === "object") {
    if (typeof detail.msg === "string") return detail.msg.trim();
    try { return JSON.stringify(detail); } catch { return String(detail); }
  }
  return "";
}
