export function inertMarketScanController() {
  const noOp = () => null;
  return {
    activate: async () => null,
    cancel: async () => null,
    deactivate: noOp,
    exportResults: async () => null,
    loadHistory: async () => null,
    loadLatest: async () => null,
    loadResults: async () => null,
    retry: async () => null,
    setVisible: noOp,
    start: async () => null,
    state: { activated: false, browseMode: "official", exportBusy: false, publishedRun: null, run: null },
  };
}
