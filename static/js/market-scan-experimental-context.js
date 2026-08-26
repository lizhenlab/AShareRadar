import { MARKET_SCAN_MODES, isPublishedMarketScanRun, isMarketScanTop100RefreshRun, validateMarketScanRunPage } from "./market-scan-contracts.js";

// Navigation only locates a batch. The experimental endpoint re-verifies its full
// publication seal; this module must never promote it into formal UI state.
export function experimentalNavigation(getNavigation) {
  const value = getNavigation?.();
  if (!value) return null;
  if (!MARKET_SCAN_MODES.includes(value.mode) || (value.selectedRunId !== null
      && (!Number.isInteger(value.selectedRunId) || value.selectedRunId < 1))) throw new Error("实验榜单选择无效");
  return { mode: value.mode, selectedRunId: value.selectedRunId };
}

export function matchesExperimentalNavigation(run, navigation) {
  return Boolean(run && (!navigation || (run.mode === navigation.mode
    && (navigation.selectedRunId === null || run.id === navigation.selectedRunId))));
}

export async function locateExperimentalRun(request, navigation, signal) {
  if (!navigation) throw new Error("请先选择一个已发布的全市场榜单");
  const params = new URLSearchParams({ page: "1", page_size: "100", mode: navigation.mode, status: "published", authority: "navigation" });
  const page = validateMarketScanRunPage(await request(`/api/market-scans?${params}`, { signal, timeoutMs: 15000 }));
  if (page.items.some((run) => run.mode !== navigation.mode || !isPublishedMarketScanRun(run))) throw new Error("实验导航批次与所选模式不一致");
  const run = navigation.selectedRunId === null
    ? page.items.find((item) => !isMarketScanTop100RefreshRun(item) && item.snapshot_seal_origin === "publication")
    : page.items.find((item) => item.id === navigation.selectedRunId);
  if (!run) throw new Error(navigation.selectedRunId === null ? "此模式暂无已发布全市场榜单" : "所选历史批次已不在导航列表，请重新选择");
  if (isMarketScanTop100RefreshRun(run) || run.snapshot_seal_origin !== "publication") throw new Error("实验模式需要原始发布的全市场批次，不能使用 TOP100 快更或旧版补封榜单");
  return run;
}
