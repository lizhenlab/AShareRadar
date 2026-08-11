import { formatAuditTimestamp } from "./audit-time.js";
import {
  isActiveMarketScanRun,
  isMarketScanTop100RefreshRun,
  isPublishedMarketScanRun,
  marketScanModeLabel,
  marketScanRunModeLabel,
} from "./market-scan-contracts.js";

export function renderMarketScanBrowsingContext(
  elements,
  taskRun,
  displayedRun,
  selectedMode,
  historical,
  statusLabel,
) {
  const browseLabel = marketScanModeLabel(selectedMode);
  const browseKind = isMarketScanTop100RefreshRun(displayedRun)
    ? `TOP100 快速更新 #${displayedRun.id} · 源批次 #${displayedRun.retry_of_run_id || "--"}`
    : `${historical ? "历史批次" : "最近发布"} #${displayedRun?.id || "--"}`;
  const browseSource = displayedRun
    ? `${browseKind} · 行情日 ${displayedRun.quote_date || "--"}`
    : "暂无已发布榜单";
  setText(elements.browseContext, `当前浏览：${browseLabel} · ${browseSource}`);
  renderExecutionTime(elements.executedAt, displayedRun || sameModeRun(taskRun, selectedMode));

  let taskText = "后台任务：暂无扫描任务";
  if (taskRun) {
    taskText = `后台任务：${marketScanRunModeLabel(taskRun)} #${taskRun.id} · ${statusLabel(taskRun.status)}`;
    const startedAt = taskRun.started_at || taskRun.created_at;
    if (startedAt) taskText += ` · 启动 ${displayExecutionTimestamp(startedAt)}`;
  }
  const mismatch = Boolean(taskRun && taskRun.mode !== selectedMode);
  if (mismatch) taskText += `；与当前浏览的${browseLabel}不同`;
  setText(elements.taskContext, taskText);
  elements.context.classList?.toggle("mismatch", mismatch);
}

export function renderMarketScanTop100Refresh(elements, busy, sourceRun, taskRun) {
  const active = isActiveMarketScanRun(taskRun);
  const available = isPublishedMarketScanRun(sourceRun) && Number(sourceRun?.success_count) > 0;
  elements.refreshTop100.disabled = Boolean(busy) || active || !available;
  setAttribute(elements.refreshTop100, "aria-busy", busy ? "true" : "false");
  setText(
    elements.refreshTop100,
    busy ? "正在创建快更..." : active && isMarketScanTop100RefreshRun(taskRun) ? "TOP100 更新中" : "更新 TOP100 评分",
  );
  const title = active
    ? "当前已有扫描任务正在执行"
    : available
      ? `基于批次 #${sourceRun.id} 的前 100 名重新拉取数据并评分`
      : "完成并发布榜单后可快速更新前 100 名评分";
  setAttribute(elements.refreshTop100, "title", title);
}

export function displayExecutionTimestamp(value) {
  return formatAuditTimestamp(value, { includeSeconds: true });
}

function renderExecutionTime(element, run) {
  if (!run) {
    setText(element, "评分执行时间：--");
    setAttribute(element, "datetime", "");
    return;
  }
  const timestamp = run.finished_at || run.started_at || run.created_at;
  const label = run.finished_at ? "评分完成时间" : run.started_at ? "评分启动时间" : "任务创建时间";
  setText(element, `${label}：${displayExecutionTimestamp(timestamp)}`);
  setAttribute(element, "datetime", timestamp || "");
}

function sameModeRun(run, mode) {
  return run?.mode === mode ? run : null;
}

function setText(element, value) {
  element.textContent = String(value ?? "--");
}

function setAttribute(element, name, value) {
  if (typeof element?.setAttribute === "function") element.setAttribute(name, String(value));
  else if (element) element[name] = String(value);
}
