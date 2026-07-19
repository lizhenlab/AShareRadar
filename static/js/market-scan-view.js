import { escapeHtml } from "./dom.js";
import { changeClass, formatAmount, formatNumber } from "./format.js";
import {
  isActiveMarketScanRun,
  isPublishedMarketScanRun,
  isRetryableMarketScanRun,
  marketScanRunIdentityChanged,
} from "./market-scan-contracts.js";

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

export function createMarketScanView(root) {
  const context = { actionBusy: false, announcementKey: "", elements: marketScanElements(root), root };
  return {
    announce: (message, key) => announce(context, message, key),
    announceRunUpdate: (previousRun, run, message) => announceRunUpdate(context, previousRun, run, message),
    elements: context.elements,
    renderActionBusy: (busy, run, message) => renderActionBusy(context, busy, run, message),
    renderHeadline: (message, kind) => renderHeadline(context, message, kind),
    renderResults: (payload) => renderResults(context, payload),
    renderResultsLoading: () => renderResultsLoading(context),
    renderResultState: (message, kind) => renderResultState(context, message, kind),
    renderRun: (run, message) => renderRun(context, run, message),
    resetResultPresentation: (run) => resetResultPresentation(context, run),
  };
}

export function buildMarketScanResultsUrl(runId, page, elements) {
  const params = new URLSearchParams({
    page: String(positiveInteger(page, 1)),
    page_size: String(DEFAULT_PAGE_SIZE),
    status: elements.status.value || "success",
    sort: elements.sort.value || "rank",
    order: elements.order.value || "asc",
  });
  addParam(params, "market", elements.market.value);
  addParam(params, "industry", elements.industry.value.trim());
  addParam(params, "is_st", elements.isSt.value);
  addParam(params, "is_new", elements.isNew.value);
  addParam(params, "min_data_quality_score", elements.quality.value);
  addParam(params, "keyword", elements.keyword.value.trim());
  return `/api/market-scans/${encodeURIComponent(runId)}/results?${params.toString()}`;
}

export function marketScanResultsUrl(runId, page, elements) {
  return buildMarketScanResultsUrl(runId, page, elements);
}

export function marketScanResultRow(item) {
  const view = marketScanResultView(item);
  return `<tr>
    <td>${escapeHtml(view.rank)}</td>
    <td><button type="button" class="market-scan-stock" data-market-scan-symbol="${escapeHtml(view.dataSymbol)}"><strong>${escapeHtml(view.name)}</strong><span>${escapeHtml(view.symbol)}${escapeHtml(view.flags)}</span></button></td>
    <td><span class="market-scan-meta">${escapeHtml(view.marketIndustry)}</span></td>
    <td><strong class="market-scan-score">${escapeHtml(scoreText(view.score))}</strong></td>
    <td>${escapeHtml(scoreText(view.trendScore))}</td>
    <td class="${escapeHtml(changeClass(view.changePct))}">${escapeHtml(signedPercentage(view.changePct))}</td>
    <td>${escapeHtml(percentage(view.turnoverRate))}</td>
    <td>${escapeHtml(formatAmount(view.amount))}</td>
    <td>${escapeHtml(scoreText(view.qualityScore))}</td>
    <td><span class="market-scan-status ${escapeHtml(view.status)}">${escapeHtml(marketScanResultStatusLabel(view.status))}</span><div class="market-scan-tags">${escapeHtml(view.detail)}</div></td>
  </tr>`;
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
    renderResultState(context, "扫描进行中，任务完成后将发布稳定榜单。", "loading");
  } else if (isPublishedMarketScanRun(run)) {
    renderResultState(context, "正在读取榜单...", "loading");
  } else {
    renderResultState(context, "该批次未发布正式榜单，可重试问题项或新建扫描。", "degraded");
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
  elements.progressBar.value = 0;
  setAttribute(elements.progressBar, "aria-valuetext", "尚无扫描进度");
  [
    elements.dataDate,
    elements.total,
    elements.success,
    elements.issues,
    elements.coverage,
    elements.finishedAt,
    elements.rule,
  ]
    .forEach((element) => setText(element, "--"));
  renderRunControls(context, null);
}

function renderPopulatedRun(context, run, overrideMessage) {
  const { elements } = context;
  const progress = clampPercentage(run.progress_pct);
  const statusLabel = marketScanRunStatusLabel(run.status);
  const headline = overrideMessage || run.message || `${statusLabel} · 数据日期 ${run.data_date || "--"}`;
  renderHeadline(context, headline, run.status === "degraded" ? "degraded" : run.status === "failed" ? "error" : "");
  const progressText = `${integer(run.processed_count)}/${integer(run.total_count)} · ${formatNumber(progress, 1)}%`;
  setText(elements.progressText, progressText);
  elements.progressBar.value = progress;
  setAttribute(elements.progressBar, "aria-valuetext", `${statusLabel}，${progressText}`);
  renderRunSummary(elements, run);
  renderRunControls(context, run);
}

function renderRunSummary(elements, run) {
  setText(elements.dataDate, run.data_date || "--");
  setText(
    elements.total,
    `${integer(run.total_count)}${integer(run.excluded_count) ? `（排除 ${integer(run.excluded_count)}）` : ""}`
  );
  setText(elements.success, integer(run.success_count));
  setText(elements.issues, `${integer(run.missing_count)} / ${integer(run.skipped_count)}`);
  setText(elements.coverage, `${formatNumber(clampPercentage(run.coverage_pct), 1)}%`);
  setText(elements.finishedAt, displayTimestamp(run.finished_at || run.started_at || run.created_at));
  setText(elements.rule, run.rule_version || "--");
}

function renderResults(context, payload) {
  const { elements } = context;
  setResultsBusy(elements, false);
  if (!payload.items.length) {
    renderResultState(context, "当前筛选条件下没有结果");
    renderPagination(context, payload, false);
    announceResults(context, payload, 0);
    return;
  }
  elements.rows.innerHTML = payload.items.map(marketScanResultRow).join("");
  elements.tableWrap.hidden = false;
  elements.resultState.hidden = true;
  renderPagination(context, payload, true);
  announceResults(context, payload, payload.items.length);
}

function announceResults(context, payload, visibleCount) {
  const pageCount = payload.page_count;
  announce(
    context,
    `榜单加载完成，第 ${payload.page}/${Math.max(pageCount, 1)} 页，本页 ${visibleCount} 条，共 ${payload.total} 条。`,
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
  elements.cancel.disabled = context.actionBusy || run?.status === "cancelling";
  elements.retry.disabled = context.actionBusy;
  setAttribute(elements.panel, "aria-busy", context.actionBusy ? "true" : "false");
  setAttribute(elements.progressBar, "aria-busy", context.actionBusy || active ? "true" : "false");
  setActionHidden(context, elements.cancel, !active);
  setActionHidden(context, elements.retry, !isRetryableMarketScanRun(run));
}

function setResultsBusy(elements, busy) {
  setAttribute(elements.tableWrap, "aria-busy", busy ? "true" : "false");
  setAttribute(elements.pagination, "aria-busy", busy ? "true" : "false");
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
      overrideMessage || run.message || marketScanRunStatusLabel(run.status),
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
    overrideMessage || activeRunAnnouncement(run, milestone),
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
  return `${marketScanRunStatusLabel(run.status)}，已处理 ${integer(run.processed_count)}/${integer(run.total_count)}，进度 ${milestone}%${detail}`;
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
  return {
    panel: requiredElement(root, "workspace-panel-market-scan"),
    headline: requiredElement(root, "marketScanHeadline"),
    start: requiredElement(root, "marketScanStart"),
    cancel: requiredElement(root, "marketScanCancel"),
    retry: requiredElement(root, "marketScanRetry"),
    progressText: requiredElement(root, "marketScanProgressText"),
    progressBar: requiredElement(root, "marketScanProgressBar"),
    dataDate: requiredElement(root, "marketScanDataDate"),
    total: requiredElement(root, "marketScanTotal"),
    success: requiredElement(root, "marketScanSuccess"),
    issues: requiredElement(root, "marketScanIssues"),
    coverage: requiredElement(root, "marketScanCoverage"),
    finishedAt: requiredElement(root, "marketScanFinishedAt"),
    rule: requiredElement(root, "marketScanRule"),
    filters: requiredElement(root, "marketScanFilters"),
    status: requiredElement(root, "marketScanStatus"),
    market: requiredElement(root, "marketScanMarket"),
    industry: requiredElement(root, "marketScanIndustry"),
    isSt: requiredElement(root, "marketScanSt"),
    isNew: requiredElement(root, "marketScanNew"),
    quality: requiredElement(root, "marketScanQuality"),
    keyword: requiredElement(root, "marketScanKeyword"),
    sort: requiredElement(root, "marketScanSort"),
    order: requiredElement(root, "marketScanOrder"),
    announcement: requiredElement(root, "marketScanAnnouncement"),
    resultState: requiredElement(root, "marketScanResultState"),
    tableWrap: requiredElement(root, "marketScanTableWrap"),
    rows: requiredElement(root, "marketScanRows"),
    pagination: requiredElement(root, "marketScanPagination"),
    pageText: requiredElement(root, "marketScanPageText"),
    prev: requiredElement(root, "marketScanPrev"),
    next: requiredElement(root, "marketScanNext"),
  };
}

function requiredElement(root, id) {
  const element = root.getElementById(id);
  if (!element) throw new Error(`缺少全市场扫描界面元素：${id}`);
  return element;
}

function addParam(params, key, value) {
  if (value !== null && value !== undefined && String(value).trim() !== "") {
    params.set(key, String(value).trim());
  }
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

function displayTimestamp(value) {
  const text = String(value || "").trim().replace("T", " ");
  return text ? text.slice(0, 16) : "--";
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
