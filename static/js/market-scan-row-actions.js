export function createMarketScanRowClickHandler({ onSelectStock, view }) {
  return (event) => {
    const snapshot = event.target.closest("button[data-market-scan-snapshot-target]");
    if (snapshot) {
      const expanded = view.toggleSnapshot(snapshot);
      view.announce(
        expanded ? "已展开冻结扫描快照。" : "已收起冻结扫描快照。",
        `snapshot:${snapshot.dataset.marketScanSnapshotTarget}:${expanded}`,
      );
      return;
    }
    const button = event.target.closest("button[data-market-scan-symbol]");
    if (!button) return;
    view.announce(
      "即将打开当前个股分析；该页面使用当前可用数据，不是历史扫描快照。",
      `current-analysis:${button.dataset.marketScanSymbol}`,
    );
    onSelectStock(button.dataset.marketScanSymbol, currentAnalysisOrigin(button.dataset));
  };
}

function currentAnalysisOrigin(dataset) {
  return {
    source: "market-scan",
    runId: Number(dataset.marketScanRunId) || null,
    mode: dataset.marketScanMode || null,
    quoteDate: dataset.marketScanQuoteDate || null,
    dataDate: dataset.marketScanDataDate || null,
  };
}
