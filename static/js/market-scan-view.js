import { escapeHtml } from "./dom.js";
import { formatAuditTimestamp } from "./audit-time.js";
import { changeClass, formatAmount, formatNumber } from "./format.js";
import {
  defaultMarketScanMode,
  isActiveMarketScanRun,
  isPublishedMarketScanRun,
  isRetryableMarketScanRun,
  marketScanModeLabel,
  marketScanRunIdentityChanged,
} from "./market-scan-contracts.js";
import {
  marketScanHistoryFilters,
  renderMarketScanHistory,
  renderMarketScanHistoryError,
  renderMarketScanHistoryLoading,
  selectedMarketScanHistoryRunId,
} from "./market-scan-history-view.js";
import { saveMarketScanExport } from "./market-scan-view-export.js";
import { marketScanFilterElements, marketScanQueryParams } from "./market-scan-filters.js";
import { renderMarketScanObservability } from "./market-scan-progress-view.js";
import { marketScanSnapshotRow, marketScanSnapshotTargetId, toggleMarketScanSnapshot } from "./market-scan-snapshot-view.js";
export { marketScanExportFilename } from "./market-scan-view-export.js";

const DEFAULT_PAGE_SIZE = 100;
const PROGRESS_ANNOUNCEMENT_STEP = 10;

const RUN_STATUS_LABELS = Object.freeze({
  queued: "等待执行",
  running: "扫描中",
  cancelling: "正在取消",
  success: "扫描完成",
  degraded: "降级完成",
  failed: "扫描失败",
  cancelled: "已取消",
  interrupted: "异常中断",
});

const RESULT_STATUS_LABELS = Object.freeze({
  pending: "待处理",
  success: "有效排名",
  missing: "数据缺失",
  skipped: "已跳过",
});

export function createMarketScanView(root, now = new Date()) {
  const elements = marketScanElements(root);
  initializeModeSelection(elements, now);
  const context = { actionBusy: false, announcementKey: "", elements, root };
  return {
    announce: (message, key) => announce(context, message, key),
    announceRunUpdate: (previousRun, run, message) => announceRunUpdate(context, previousRun, run, message),
    elements: context.elements,
    renderActionBusy: (busy, run, message) => renderActionBusy(context, busy, run, message),
    renderExportBusy: (busy, run) => renderExportBusy(context, busy, run),
    renderBrowsingContext: (taskRun, displayedRun, mode, historical) => (
      renderBrowsingContext(context, taskRun, displayedRun, mode, historical)
    ),
    renderHeadline: (message, kind) => renderHeadline(context, message, kind),
    renderHistory: (payload, selectedRunId) => (
      renderMarketScanHistory(elements, payload, selectedRunId, selectedMarketScanMode(elements))
    ),
    renderHistoryError: (message) => renderMarketScanHistoryError(elements, message),
    renderHistoryLoading: () => renderMarketScanHistoryLoading(elements),
    renderResults: (payload) => renderResults(context, payload),
    renderResultsLoading: () => renderResultsLoading(context),
    renderResultState: (message, kind) => renderResultState(context, message, kind),
    renderRun: (run, message) => renderRun(context, run, message),
    resetResultPresentation: (run) => resetResultPresentation(context, run),
    saveExport: (blob, disposition, run) => saveMarketScanExport(context, blob, disposition, run),
    historyFilters: () => marketScanHistoryFilters(elements),
    modeLabel: (mode) => marketScanModeLabel(mode),
    selectedHistoryRunId: () => selectedMarketScanHistoryRunId(elements),
    selectedMode: () => selectedMarketScanMode(elements),
    toggleSnapshot: (button) => toggleMarketScanSnapshot(root, button),
  };
}

export function buildMarketScanResultsUrl(runId, page, elements) {
  const params = marketScanQueryParams(elements, {
    page: String(positiveInteger(page, 1)),
    page_size: String(DEFAULT_PAGE_SIZE),
  });
  return `/api/market-scans/${encodeURIComponent(runId)}/results?${params.toString()}`;
}

export function buildMarketScanExportUrl(runId, elements) {
  const params = marketScanQueryParams(elements);
  return `/api/market-scans/${encodeURIComponent(runId)}/export.xlsx?${params.toString()}`;
}

export function marketScanResultsUrl(runId, page, elements) {
  return buildMarketScanResultsUrl(runId, page, elements);
}

export function marketScanResultRow(item, options = {}) {
  const view = marketScanResultView(item);
  const discovery = discoveryResultActions(view, options);
  const snapshotTarget = marketScanSnapshotTargetId(item);
  const run = options.run && typeof options.run === "object" ? options.run : {};
  return `<tr class="market-scan-result-row">
    <td data-label="排名">${escapeHtml(view.rank)}</td>
    <td data-label="股票"><div class="market-scan-stock"><strong>${escapeHtml(view.name)}</strong><span>${escapeHtml(view.symbol)}${escapeHtml(view.flags)}</span><div class="market-scan-stock-actions"><button type="button" class="mini-button" data-market-scan-snapshot-target="${escapeHtml(snapshotTarget)}" aria-controls="${escapeHtml(snapshotTarget)}" aria-expanded="false">查看扫描快照</button><button type="button" class="mini-button" data-market-scan-symbol="${escapeHtml(view.dataSymbol)}" data-market-scan-run-id="${escapeHtml(item.run_id ?? run.id ?? "")}" data-market-scan-mode="${escapeHtml(run.mode || "")}" data-market-scan-quote-date="${escapeHtml(run.quote_date || "")}" data-market-scan-data-date="${escapeHtml(run.data_date || item.data_date || "")}" title="使用当前可用数据打开个股分析，不代表历史扫描快照">打开当前个股分析</button></div></div></td>
    <td data-label="市场 / 行业"><span class="market-scan-meta">${escapeHtml(view.marketIndustry)}</span></td>
    <td data-label="短线强势"><strong class="market-scan-score">${escapeHtml(scoreText(view.score))}</strong></td>
    <td data-label="趋势">${escapeHtml(scoreText(view.trendScore))}</td>
    <td data-label="涨跌幅" class="${escapeHtml(changeClass(view.changePct))}">${escapeHtml(signedPercentage(view.changePct))}</td>
    <td data-label="换手率">${escapeHtml(percentage(view.turnoverRate))}</td>
    <td data-label="成交额">${escapeHtml(formatAmount(view.amount))}</td>
    <td data-label="质量">${escapeHtml(scoreText(view.qualityScore))}</td>
    <td data-label="状态 / 标签"><span class="market-scan-status ${escapeHtml(view.status)}">${escapeHtml(marketScanResultStatusLabel(view.status))}</span><div class="market-scan-tags">${escapeHtml(view.detail)}</div>${discovery}</td>
  </tr>${marketScanSnapshotRow(item)}`;
}

function discoveryResultActions(view, options) {
  if (!options.discovery) return "";
  const rankLabel = String(options.rankLabel || "全市场排名变化未查询");
  const movement = String(options.rankMovement || "unavailable");
  const queued = Boolean(options.queued);
  const selected = Boolean(options.selected);
  return `<div class="discovery-row-actions">
    <span class="discovery-rank-change ${escapeHtml(movement)}">${escapeHtml(rankLabel)}</span>
    <label class="discovery-select-row"><input type="checkbox" data-discovery-select-symbol="${escapeHtml(view.dataSymbol)}"${selected ? " checked" : ""}${queued ? " disabled" : ""} />选择</label>
    <button type="button" class="mini-button" data-discovery-enqueue-symbol="${escapeHtml(view.dataSymbol)}"${queued ? " disabled" : ""}>${queued ? "已在研究队列" : "加入研究队列"}</button>
  </div>`;
}

export function marketScanRunStatusLabel(status) {
  return RUN_STATUS_LABELS[status] || "未知状态";
}

export function marketScanResultStatusLabel(status) {
  return RESULT_STATUS_LABELS[status] || "未知状态";
}

function resetResultPresentation(context, run) {
  if (!run) {
    renderResultState(context, "暂无扫描记录");
  } else if (isActiveMarketScanRun(run)) {
    const message = run.mode === "intraday"
      ? "盘中临时扫描进行中，完成后将生成盘中临时榜单。"
      : "盘后正式扫描进行中，完成后将发布盘后正式榜单。";
    renderResultState(context, message, "loading");
  } else if (isPublishedMarketScanRun(run)) {
    renderResultState(context, `正在读取${marketScanModeLabel(run.mode)}榜单...`, "loading");
  } else {
    const message = run.mode === "intraday"
      ? "该批次未生成盘中临时榜单，可重试问题项或新建扫描。"
      : "该批次未发布盘后正式榜单，可重试问题项或新建扫描。";
    renderResultState(context, message, "degraded");
  }
}

function renderRun(context, run, overrideMessage = "") {
  if (!run) {
    renderEmptyRun(context);
    return;
  }
  renderPopulatedRun(context, run, overrideMessage);
}

function renderEmptyRun(context) {
  const { elements } = context;
  renderHeadline(context, "尚无全市场扫描记录");
  setText(elements.progressText, "--");
  renderProgressElement(elements.progressBar, 0, "尚无扫描进度");
  [
    elements.modeSummary,
    elements.quoteDate,
    elements.dataDate,
    elements.total,
    elements.success,
    elements.issues,
    elements.coverage,
    elements.finishedAt,
  ]
    .forEach((element) => setText(element, "--"));
  renderRuleVersion(elements.rule, null);
  renderMarketScanObservability(elements, null);
  renderRunControls(context, null);
}

function renderPopulatedRun(context, run, overrideMessage) {
  const { elements } = context;
  const progress = clampPercentage(run.progress_pct);
  const statusLabel = marketScanRunStatusLabel(run.status);
  const detail = overrideMessage || run.message || `${statusLabel} · 日K截止日 ${run.data_date || "--"}`;
  const headline = modeAwareRunMessage(run, detail);
  renderHeadline(context, headline, run.status === "degraded" ? "degraded" : run.status === "failed" ? "error" : "");
  const progressText = `${integer(run.processed_count)}/${integer(run.total_count)} · ${formatNumber(progress, 1)}%`;
  setText(elements.progressText, progressText);
  renderProgressElement(elements.progressBar, progress, modeAwareRunMessage(run, `${statusLabel}，${progressText}`));
  renderRunSummary(elements, run);
  renderMarketScanObservability(elements, run);
  renderRunControls(context, run);
}

function renderRunSummary(elements, run) {
  setText(elements.modeSummary, marketScanModeLabel(run.mode));
  setText(elements.quoteDate, run.quote_date || "--");
  setText(elements.dataDate, run.data_date || "--");
  setText(
    elements.total,
    `${integer(run.total_count)}${integer(run.excluded_count) ? `（排除 ${integer(run.excluded_count)}）` : ""}`
  );
  setText(elements.success, integer(run.success_count));
  setText(elements.issues, `${integer(run.missing_count)} / ${integer(run.skipped_count)}`);
  setText(elements.coverage, `${formatNumber(clampPercentage(run.coverage_pct), 1)}%`);
  setText(elements.finishedAt, displayTimestamp(run.finished_at || run.started_at || run.created_at));
  renderRuleVersion(elements.rule, run.rule_version);
}

function renderBrowsingContext(context, taskRun, displayedRun, selectedMode, historical) {
  const { elements } = context;
  if (displayedRun) renderRunSummary(elements, displayedRun);
  else if (taskRun?.mode === selectedMode) renderRunSummary(elements, taskRun);
  else clearRunSummary(elements);
  const browseLabel = marketScanModeLabel(selectedMode);
  const browseSource = displayedRun
    ? `${historical ? "历史批次" : "最近发布"} #${displayedRun.id} · 行情日 ${displayedRun.quote_date || "--"}`
    : "暂无已发布榜单";
  setText(elements.browseContext, `当前浏览：${browseLabel} · ${browseSource}`);
  let taskText = "后台任务：暂无扫描任务";
  if (taskRun) {
    taskText = `后台任务：${marketScanModeLabel(taskRun.mode)} #${taskRun.id} · ${marketScanRunStatusLabel(taskRun.status)}`;
  }
  const mismatch = Boolean(taskRun && isActiveMarketScanRun(taskRun) && taskRun.mode !== selectedMode);
  if (mismatch) taskText += `；与当前浏览的${browseLabel}不同`;
  setText(elements.taskContext, taskText);
  elements.context.classList?.toggle("mismatch", mismatch);
}

function clearRunSummary(elements) {
  [
    elements.modeSummary,
    elements.quoteDate,
    elements.dataDate,
    elements.total,
    elements.success,
    elements.issues,
    elements.coverage,
    elements.finishedAt,
  ].forEach((element) => setText(element, "--"));
  renderRuleVersion(elements.rule, null);
}

function renderRuleVersion(element, value) {
  const fullVersion = String(value || "").trim();
  if (!fullVersion) {
    setText(element, "--");
    setAttribute(element, "title", "");
    setAttribute(element, "aria-label", "无规则版本");
    return;
  }
  const versionMatch = fullVersion.match(/^full-market-(?:scan|score)-v(\d+)(?::([a-f0-9]{8,}))?/i);
  const shortVersion = versionMatch
    ? `v${versionMatch[1]}${versionMatch[2] ? `\n${versionMatch[2].slice(0, 8)}` : ""}`
    : fullVersion.length > 24 ? `${fullVersion.slice(0, 21)}...` : fullVersion;
  setText(element, shortVersion);
  setAttribute(element, "title", fullVersion);
  setAttribute(element, "aria-label", `规则版本 ${fullVersion}`);
}

function renderResults(context, payload) {
  const { elements } = context;
  setResultsBusy(elements, false);
  if (!payload.items.length) {
    renderResultState(context, "当前筛选条件下没有结果");
    setResultRunIdentity(elements, payload.run.id);
    renderPagination(context, payload, false);
    announceResults(context, payload, 0);
    return;
  }
  elements.rows.innerHTML = payload.items.map((item) => marketScanResultRow(item, { run: payload.run })).join("");
  setResultRunIdentity(elements, payload.run.id);
  elements.tableWrap.hidden = false;
  elements.resultState.hidden = true;
  renderPagination(context, payload, true);
  announceResults(context, payload, payload.items.length);
}

function announceResults(context, payload, visibleCount) {
  const pageCount = payload.page_count;
  const mode = marketScanModeLabel(payload.run.mode);
  announce(
    context,
    `${mode}榜单加载完成，第 ${payload.page}/${Math.max(pageCount, 1)} 页，本页 ${visibleCount} 条，共 ${payload.total} 条。`,
    `results:${payload.run.id}:${payload.page}:${pageCount}:${visibleCount}:${payload.total}`
  );
}

function renderPagination(context, payload, hasRows) {
  const { elements } = context;
  const prevDisabled = payload.page <= 1;
  const nextDisabled = payload.page_count === 0 || payload.page >= payload.page_count;
  if (
    (context.root?.activeElement === elements.prev && prevDisabled)
    || (context.root?.activeElement === elements.next && nextDisabled)
  ) {
    focusVisibleControl(context, [elements.tableWrap, elements.market, elements.start]);
  }
  elements.pagination.hidden = !hasRows && payload.total === 0;
  setText(elements.pageText, `第 ${payload.page}/${Math.max(payload.page_count, 1)} 页 · 共 ${payload.total} 条`);
  elements.prev.disabled = prevDisabled;
  elements.next.disabled = nextDisabled;
}

function renderResultsLoading(context) {
  const { elements } = context;
  if (elements.tableWrap.hidden !== false || elements.pagination.hidden !== false) {
    renderResultState(context, "正在读取榜单...", "loading");
    return;
  }
  if ([elements.prev, elements.next].includes(context.root?.activeElement)) {
    focusVisibleControl(context, [elements.tableWrap, elements.market, elements.start]);
  }
  elements.prev.disabled = true;
  elements.next.disabled = true;
  elements.resultState.hidden = false;
  elements.resultState.className = "market-scan-result-state loading";
  setText(elements.resultState, "正在读取榜单...");
  setResultsBusy(elements, true);
}

function renderResultState(context, message, kind = "") {
  const { elements } = context;
  if ([elements.tableWrap, elements.prev, elements.next].includes(context.root?.activeElement)) {
    focusVisibleControl(context, [elements.market, elements.start]);
  }
  elements.rows.innerHTML = "";
  setResultRunIdentity(elements, null);
  elements.tableWrap.hidden = true;
  elements.pagination.hidden = true;
  elements.resultState.hidden = false;
  elements.resultState.className = `market-scan-result-state${kind ? ` ${kind}` : ""}`;
  setText(elements.resultState, message);
  setResultsBusy(elements, false);
}

function renderHeadline({ elements }, message, kind = "") {
  elements.headline.className = kind || "";
  setText(elements.headline, message);
}

function renderActionBusy(context, busy, run, message = "") {
  context.actionBusy = Boolean(busy);
  renderRunControls(context, run);
  if (message) announce(context, message, `action-busy:${message}`);
}

function renderExportBusy({ elements }, busy, publishedRun) {
  elements.exportButton.disabled = Boolean(busy) || !isPublishedMarketScanRun(publishedRun);
  setAttribute(elements.exportButton, "aria-busy", busy ? "true" : "false");
  setText(elements.exportButton, busy ? "正在导出..." : "导出 Excel");
}

function renderRunControls(context, run) {
  const { elements } = context;
  const active = isActiveMarketScanRun(run);
  if (
    context.actionBusy
    && [elements.start, elements.cancel, elements.retry].includes(context.root?.activeElement)
  ) {
    focusVisibleControl(context, [elements.market, elements.tableWrap]);
  }
  elements.start.disabled = context.actionBusy || active;
  elements.modeInputs.forEach((input) => { input.disabled = context.actionBusy; });
  setAttribute(elements.modeControl, "aria-disabled", context.actionBusy ? "true" : "false");
  elements.cancel.disabled = context.actionBusy || run?.status === "cancelling";
  elements.retry.disabled = context.actionBusy;
  setAttribute(elements.panel, "aria-busy", context.actionBusy ? "true" : "false");
  setAttribute(elements.progressBar, "aria-busy", context.actionBusy || active ? "true" : "false");
  setActionHidden(context, elements.cancel, !active);
  setActionHidden(context, elements.retry, !isRetryableMarketScanRun(run));
  renderGlobalProgress(context, run);
}

function renderGlobalProgress(context, run) {
  const { elements } = context;
  const active = isActiveMarketScanRun(run);
  elements.globalProgress.hidden = !active;
  context.root?.body?.classList?.toggle("market-scan-global-active", active);
  setAttribute(elements.globalProgress, "aria-busy", active ? "true" : "false");
  elements.globalOpen.disabled = false;
  elements.globalCancel.disabled = context.actionBusy || run?.status === "cancelling";
  if (!active) {
    renderProgressElement(elements.globalBar, 0, "等待任务状态");
    setText(elements.globalText, "等待任务状态");
    return;
  }
  const progress = clampPercentage(run.progress_pct);
  const progressText = `${integer(run.processed_count)}/${integer(run.total_count)} · ${formatNumber(progress, 1)}%`;
  renderProgressElement(
    elements.globalBar,
    progress,
    modeAwareRunMessage(run, `${marketScanRunStatusLabel(run.status)}，${progressText}`),
  );
  setText(elements.globalText, modeAwareRunMessage(run, `${marketScanRunStatusLabel(run.status)} · ${progressText}`));
}

function renderProgressElement(element, progress, valueText) {
  const normalized = clampPercentage(progress);
  element.value = normalized;
  setAttribute(element, "aria-valuenow", formatNumber(normalized, 2));
  setAttribute(element, "aria-valuetext", valueText);
  setText(element, `${formatNumber(normalized, 1)}%`);
}

function setResultsBusy(elements, busy) {
  setAttribute(elements.tableWrap, "aria-busy", busy ? "true" : "false");
  setAttribute(elements.pagination, "aria-busy", busy ? "true" : "false");
}

function setResultRunIdentity(elements, runId) {
  if (runId === null || runId === undefined) {
    elements.tableWrap.removeAttribute?.("data-market-scan-run-id");
    if (elements.tableWrap.dataset) delete elements.tableWrap.dataset.marketScanRunId;
    delete elements.tableWrap["data-market-scan-run-id"];
    return;
  }
  setAttribute(elements.tableWrap, "data-market-scan-run-id", runId);
  if (elements.tableWrap.dataset) elements.tableWrap.dataset.marketScanRunId = String(runId);
}

function setActionHidden(context, element, hidden) {
  if (hidden && context.root?.activeElement === element) focusVisibleControl(context);
  element.hidden = hidden;
}

function focusVisibleControl({ elements }, candidates = [elements.start, elements.market, elements.tableWrap]) {
  const target = candidates
    .find((element) => element && !element.hidden && !element.disabled && typeof element.focus === "function");
  if (!target) return;
  try {
    target.focus({ preventScroll: true });
  } catch (error) {
    target.focus();
  }
}

function announceRunUpdate(context, previousRun, run, overrideMessage = "") {
  if (!run) {
    if (previousRun) announce(context, "当前没有可用的全市场扫描记录。", "run:none");
    return;
  }
  const runChanged = marketScanRunIdentityChanged(previousRun, run);
  const statusChanged = (previousRun?.status ?? null) !== run.status;
  if (isActiveMarketScanRun(run)) {
    announceActiveRun(context, previousRun, run, overrideMessage, runChanged, statusChanged);
  } else if (runChanged || statusChanged || overrideMessage) {
    announce(
      context,
      modeAwareRunMessage(run, overrideMessage || run.message || marketScanRunStatusLabel(run.status)),
      `run:${run.id}:${run.status}:terminal`
    );
  }
}

function announceActiveRun(context, previousRun, run, overrideMessage, runChanged, statusChanged) {
  const milestone = progressMilestone(run.progress_pct);
  const previousMilestone = progressMilestone(previousRun?.progress_pct);
  if (!runChanged && !statusChanged && milestone <= previousMilestone) return;
  announce(
    context,
    overrideMessage ? modeAwareRunMessage(run, overrideMessage) : activeRunAnnouncement(run, milestone),
    `run:${run.id}:${run.status}:${milestone}`
  );
}

function announce(context, message, key = message) {
  const normalized = String(message || "").trim();
  if (!normalized || context.announcementKey === key) return;
  context.announcementKey = key;
  setText(context.elements.announcement, normalized);
}

function activeRunAnnouncement(run, milestone) {
  const detail = run.message ? `。${run.message}` : "";
  return modeAwareRunMessage(
    run,
    `${marketScanRunStatusLabel(run.status)}，已处理 ${integer(run.processed_count)}/${integer(run.total_count)}，进度 ${milestone}%${detail}`,
  );
}

function progressMilestone(value) {
  return Math.floor(clampPercentage(value) / PROGRESS_ANNOUNCEMENT_STEP) * PROGRESS_ANNOUNCEMENT_STEP;
}

function marketScanResultView(value) {
  const item = value && typeof value === "object" ? value : {};
  const symbol = item.symbol || "--";
  return {
    rank: item.rank ?? "--",
    dataSymbol: item.symbol || "",
    symbol,
    name: item.name || item.code || "--",
    flags: marketScanResultFlags(item),
    marketIndustry: [item.market, item.industry].filter(Boolean).join(" / ") || "--",
    score: item.score,
    trendScore: item.trend_score,
    changePct: item.change_pct,
    turnoverRate: item.turnover_rate,
    amount: item.amount,
    qualityScore: item.data_quality_score,
    status: String(item.status || "pending"),
    detail: marketScanResultDetail(item),
  };
}

function marketScanResultFlags(item) {
  return `${item.is_st ? " · ST" : ""}${item.is_new ? " · 新股" : ""}`;
}

function marketScanResultDetail(item) {
  const tags = Array.isArray(item.tags) ? item.tags.filter(Boolean).join(" · ") : "";
  return [item.reason || item.error, tags].filter(Boolean).join(" · ") || "--";
}

function marketScanElements(root) {
  const modeIntraday = requiredElement(root, "marketScanModeIntraday");
  const modeOfficial = requiredElement(root, "marketScanModeOfficial");
  return {
    panel: requiredElement(root, "workspace-panel-market-scan"),
    headline: requiredElement(root, "marketScanHeadline"),
    start: requiredElement(root, "marketScanStart"),
    modeControl: requiredElement(root, "marketScanModeControl"),
    modeIntraday,
    modeOfficial,
    modeInputs: [modeIntraday, modeOfficial],
    cancel: requiredElement(root, "marketScanCancel"),
    retry: requiredElement(root, "marketScanRetry"),
    exportButton: requiredElement(root, "marketScanExport"),
    context: requiredElement(root, "marketScanContext"),
    browseContext: requiredElement(root, "marketScanBrowseContext"),
    taskContext: requiredElement(root, "marketScanTaskContext"),
    history: requiredElement(root, "marketScanHistory"),
    historyRun: requiredElement(root, "marketScanHistoryRun"),
    historyStatus: requiredElement(root, "marketScanHistoryStatus"),
    historyDate: requiredElement(root, "marketScanHistoryDate"),
    historyRefresh: requiredElement(root, "marketScanHistoryRefresh"),
    historyFeedback: requiredElement(root, "marketScanHistoryFeedback"),
    progressText: requiredElement(root, "marketScanProgressText"),
    progressBar: requiredElement(root, "marketScanProgressBar"),
    stage: requiredElement(root, "marketScanStage"),
    elapsed: requiredElement(root, "marketScanElapsed"),
    throughput: requiredElement(root, "marketScanThroughput"),
    eta: requiredElement(root, "marketScanEta"),
    marketProgress: requiredElement(root, "marketScanMarketProgress"),
    diagnostic: requiredElement(root, "marketScanDiagnostic"),
    modeSummary: requiredElement(root, "marketScanModeSummary"),
    quoteDate: requiredElement(root, "marketScanQuoteDate"),
    dataDate: requiredElement(root, "marketScanDataDate"),
    total: requiredElement(root, "marketScanTotal"),
    success: requiredElement(root, "marketScanSuccess"),
    issues: requiredElement(root, "marketScanIssues"),
    coverage: requiredElement(root, "marketScanCoverage"),
    finishedAt: requiredElement(root, "marketScanFinishedAt"),
    rule: requiredElement(root, "marketScanRule"),
    ...marketScanFilterElements(root, requiredElement),
    announcement: requiredElement(root, "marketScanAnnouncement"),
    resultState: requiredElement(root, "marketScanResultState"),
    tableWrap: requiredElement(root, "marketScanTableWrap"),
    rows: requiredElement(root, "marketScanRows"),
    pagination: requiredElement(root, "marketScanPagination"),
    pageText: requiredElement(root, "marketScanPageText"),
    prev: requiredElement(root, "marketScanPrev"),
    next: requiredElement(root, "marketScanNext"),
    globalProgress: requiredElement(root, "marketScanGlobalProgress"),
    globalText: requiredElement(root, "marketScanGlobalText"),
    globalBar: requiredElement(root, "marketScanGlobalBar"),
    globalOpen: requiredElement(root, "marketScanGlobalOpen"),
    globalCancel: requiredElement(root, "marketScanGlobalCancel"),
  };
}

function initializeModeSelection(elements, now) {
  setModeSelection(elements, defaultMarketScanMode(now));
}

function setModeSelection(elements, mode) {
  elements.modeIntraday.checked = mode === "intraday";
  elements.modeOfficial.checked = mode === "official";
}

function selectedMarketScanMode(elements) {
  return elements.modeIntraday.checked ? "intraday" : "official";
}

function modeAwareRunMessage(run, message) {
  const value = String(message || "").trim();
  if (run?.mode !== "intraday") return value;
  const safe = value
    .replaceAll("盘后正式", "盘中临时")
    .replaceAll("正式榜单", "盘中临时榜单")
    .replaceAll("稳定榜单", "盘中临时榜单")
    .replaceAll("正式扫描", "盘中临时扫描");
  return safe.includes("盘中临时") ? safe : `盘中临时 · ${safe}`;
}

function requiredElement(root, id) {
  const element = root.getElementById(id);
  if (!element) throw new Error(`缺少全市场扫描界面元素：${id}`);
  return element;
}

function setText(element, value) {
  element.textContent = String(value ?? "--");
}

function setAttribute(element, name, value) {
  if (typeof element?.setAttribute === "function") {
    element.setAttribute(name, String(value));
    return;
  }
  if (element) element[name] = String(value);
}

function signedPercentage(value) {
  const number = Number(value);
  if (!Number.isFinite(number)) return "--";
  return `${number > 0 ? "+" : ""}${formatNumber(number, 2)}%`;
}

function percentage(value) {
  const number = Number(value);
  if (!Number.isFinite(number)) return "--";
  return `${formatNumber(number, 2)}%`;
}

function scoreText(value) {
  const number = Number(value);
  return Number.isFinite(number) ? String(Math.round(number)) : "--";
}

export function displayTimestamp(value) {
  return formatAuditTimestamp(value, { includeSeconds: false });
}

function integer(value) {
  return nonNegativeInteger(value, 0);
}

function nonNegativeInteger(value, fallback) {
  const number = Number(value);
  return Number.isInteger(number) && number >= 0 ? number : fallback;
}

function positiveInteger(value, fallback) {
  const number = Number(value);
  return Number.isInteger(number) && number > 0 ? number : fallback;
}

function clampPercentage(value) {
  const number = Number(value);
  if (!Number.isFinite(number)) return 0;
  return Math.min(100, Math.max(0, number));
}
