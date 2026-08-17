import {
  isPublishedMarketScanRun,
  validateStartResponse,
} from "./market-scan-contracts.js";

export function createMarketScanTop100Refresh(options) {
  const { applyRun, elements, mutate, polling, resultRun, state, view } = options;

  async function refresh() {
    const sourceRun = resultRun();
    if (!isPublishedMarketScanRun(sourceRun)) return null;
    return mutate(
      "快速更新 TOP100 评分",
      `/api/market-scans/${encodeURIComponent(sourceRun.id)}/refresh-top100`,
      { method: "POST" },
      async (payload) => {
        const response = validateStartResponse(payload, "TOP100 快速更新响应");
        state.selectedHistoryRunId = null;
        elements.historyRun.value = "";
        applyRun(
          response.run,
          response.deduplicated
            ? "已有扫描任务正在运行，已切换到该任务。"
            : `正在重新获取源批次 #${sourceRun.id} 前 100 名的数据并生成新评分。`,
        );
        polling.scheduleDefault(state.run);
        return response;
      },
    );
  }

  function sync() {
    view.renderTop100Refresh(state.actionBusy, resultRun(), state.run);
  }

  elements.refreshTop100.addEventListener("click", () => void refresh());
  return { refresh, sync };
}
