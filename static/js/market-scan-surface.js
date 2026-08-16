import { observeMarketScanPageSize } from "./layout-optimizations.js";

export function createMarketScanSurface(options) {
  const {
    abortHistory, elements, loadResults, refreshOwned, releaseResults,
    scheduleTracking, state, transitionReads,
  } = options;
  observeMarketScanPageSize(elements, handlePageSizeChange);

  function setActive(active) {
    const next = Boolean(active);
    if (state.surfaceActive === next) return false;
    state.surfaceActive = next;
    if (!next) {
      abortHistory();
      releaseResults();
      void transitionReads(() => {
        if (!state.surfaceActive && state.activated && state.visible && !state.actionBusy) {
          scheduleTracking();
        }
      }, { preserveCache: true });
    } else if (state.activated && state.visible && !state.actionBusy) {
      void transitionReads(() => {
        if (state.activated && state.visible && state.surfaceActive && !state.actionBusy) {
          return refreshOwned();
        }
        return null;
      });
    }
    return true;
  }

  function handlePageSizeChange() {
    state.page = 1;
    state.pageCount = 0;
    if (state.activated && state.visible && state.surfaceActive && !state.actionBusy) void loadResults();
  }

  return { setActive };
}
