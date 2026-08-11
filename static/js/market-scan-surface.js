import { observeMarketScanPageSize } from "./layout-optimizations.js";

export function createMarketScanSurface(options) {
  const { abortHistory, abortResults, elements, loadResults, refresh, releaseResults, state } = options;
  observeMarketScanPageSize(elements, handlePageSizeChange);

  function setActive(active) {
    const next = Boolean(active);
    if (state.surfaceActive === next) return false;
    state.surfaceActive = next;
    if (!next) {
      abortResults();
      abortHistory();
      releaseResults();
    } else if (state.activated && state.visible && !state.actionBusy) {
      void refresh();
    }
    return true;
  }

  function handlePageSizeChange() {
    state.page = 1;
    state.pageCount = 0;
    abortResults();
    if (state.activated && state.visible && state.surfaceActive && !state.actionBusy) void loadResults();
  }

  return { setActive };
}
