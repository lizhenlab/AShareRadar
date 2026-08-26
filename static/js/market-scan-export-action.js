import { compactErrorMessage } from "./errors.js";
import {
  exportTimeoutScope,
  MARKET_SCAN_EXPORT_TIMEOUT_MS,
  MARKET_SCAN_XLSX_MEDIA_TYPE,
  marketScanExportFailure,
  marketScanExportMediaType,
} from "./market-scan-export-client.js";
import { requestMarketScanRead } from "./market-scan-read-client.js";
import { buildMarketScanExportUrl } from "./market-scan-view.js";

export function createMarketScanExportAction(options) {
  return async function exportMarketScanResults() {
    const publishedRun = options.resultRun();
    if (!publishedRun || options.state.exportBusy) return null;
    options.state.exportBusy = true;
    options.view.renderExportBusy(true, publishedRun);
    options.view.announce(
      `正在导出批次 #${publishedRun.id} 当前输入筛选条件下的 Excel 榜单，校验及下载可能需要 1–2 分钟。`,
      `export:start:${publishedRun.id}`
    );
    let timeout = null;
    try {
      const url = buildMarketScanExportUrl(publishedRun.id, options.elements);
      const read = () => {
        timeout = exportTimeoutScope();
        return readExport(options.exportRequest, url, timeout.signal);
      };
      const file = options.withHeavyRead ? await options.withHeavyRead(read) : await read();
      if (!file) {
        options.view.announce("浏览条件已切换，已取消排队中的导出，请按当前榜单重新导出。", `export:cancelled:${publishedRun.id}`);
        return null;
      }
      const filename = options.view.saveExport(
        file.blob,
        file.disposition,
        publishedRun,
      );
      options.view.announce(`Excel 榜单已导出：${filename}`, `export:success:${publishedRun.id}:${filename}`);
      return filename;
    } catch (error) {
      const detail = timeout?.didTimeout() ? "请求超时，请稍后重试" : compactErrorMessage(error?.message);
      const message = `导出 Excel 失败：${detail}`;
      options.view.announce(message, `export:error:${publishedRun.id}:${String(error?.message || "")}`);
      return null;
    } finally {
      timeout?.dispose();
      options.state.exportBusy = false;
      options.view.renderExportBusy(false, options.resultRun());
    }
  };
}

async function readExport(exportRequest, url, signal) {
  return requestMarketScanRead(
    (target, requestOptions) => readExportAttempt(exportRequest, target, requestOptions.signal),
    url, { signal, timeoutMs: MARKET_SCAN_EXPORT_TIMEOUT_MS },
  );
}

async function readExportAttempt(exportRequest, url, signal) {
  const response = await exportRequest(url, { headers: { Accept: MARKET_SCAN_XLSX_MEDIA_TYPE }, signal });
  if (!response?.ok) throw await marketScanExportFailure(response);
  if (marketScanExportMediaType(response) !== MARKET_SCAN_XLSX_MEDIA_TYPE) throw new Error("服务返回的不是 Excel 文件");
  const blob = await response.blob();
  if (!blob?.size) throw new Error("服务返回了空的 Excel 文件");
  return { blob, disposition: response.headers?.get?.("content-disposition") || "" };
}
