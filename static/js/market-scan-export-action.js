import { compactErrorMessage } from "./errors.js";
import {
  exportTimeoutScope,
  MARKET_SCAN_XLSX_MEDIA_TYPE,
  marketScanExportError,
  marketScanExportMediaType,
} from "./market-scan-export-client.js";
import { buildMarketScanExportUrl } from "./market-scan-view.js";

export function createMarketScanExportAction(options) {
  return async function exportMarketScanResults() {
    const publishedRun = options.resultRun();
    if (!publishedRun || options.state.exportBusy) return null;
    options.state.exportBusy = true;
    options.view.renderExportBusy(true, publishedRun);
    options.view.announce(
      "正在导出当前筛选条件下的 Excel 榜单，全市场文件可能需要 1–2 分钟。",
      `export:start:${publishedRun.id}`
    );
    const timeout = exportTimeoutScope();
    try {
      const response = await options.exportRequest(buildMarketScanExportUrl(publishedRun.id, options.elements), {
        headers: { Accept: MARKET_SCAN_XLSX_MEDIA_TYPE },
        signal: timeout.signal,
      });
      if (!response?.ok) throw new Error(await marketScanExportError(response));
      if (marketScanExportMediaType(response) !== MARKET_SCAN_XLSX_MEDIA_TYPE) {
        throw new Error("服务返回的不是 Excel 文件");
      }
      const blob = await response.blob();
      if (!blob?.size) throw new Error("服务返回了空的 Excel 文件");
      const filename = options.view.saveExport(
        blob,
        response.headers?.get?.("content-disposition") || "",
        publishedRun,
      );
      options.view.announce(`Excel 榜单已导出：${filename}`, `export:success:${publishedRun.id}:${filename}`);
      return filename;
    } catch (error) {
      const detail = timeout.didTimeout() ? "请求超时，请稍后重试" : compactErrorMessage(error?.message);
      const message = `导出 Excel 失败：${detail}`;
      options.view.announce(message, `export:error:${publishedRun.id}:${String(error?.message || "")}`);
      return null;
    } finally {
      timeout.dispose();
      options.state.exportBusy = false;
      options.view.renderExportBusy(false, options.resultRun());
    }
  };
}
