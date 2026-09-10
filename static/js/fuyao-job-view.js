import { escapeHtml } from "./dom.js";
import { canRetryFuyaoJob, FUYAO_JOB_LABELS } from "./fuyao-contracts.js";

const e = escapeHtml;
const STATUS_LABELS = { running: "进行中", cancelling: "正在停止", completed: "已完成", degraded: "部分完成", failed: "失败", cancelled: "已取消", interrupted: "已中断" };
const STAGE_LABELS = {
  pending: "等待开始", checking_previous: "核验已有版本", signing_daily: "获取日线下载地址", downloading_daily: "下载日线",
  signing_actions: "获取企业行动下载地址", downloading_actions: "下载企业行动", validating_actions: "校验企业行动",
  seeding_daily: "载入已有日线", validating_daily: "校验日线", checking_calendar: "检查交易日期", writing_daily: "写入日线",
  writing_actions: "写入企业行动", hashing_daily: "校验日线摘要", hashing_actions: "校验企业行动摘要",
  publishing: "发布版本", collecting: "采集数据",
};

export function renderFuyaoJobs(target, status, options = {}) {
  if (!target) return;
  const jobs = Array.isArray(status?.jobs) ? [...status.jobs] : [];
  if (options.accepted && !jobs.some((job) => job.id === options.accepted.id)) jobs.unshift(options.accepted);
  const focused = target.ownerDocument?.activeElement;
  const focusKey = target.contains?.(focused) ? [focused.dataset?.fuyaoAction, focused.dataset?.fuyaoId] : null;
  target.innerHTML = jobs.slice(0, 8).map((job) => jobCard(job, options)).join("")
    || "<p class='fuyao-note'>尚无同步任务。读取此页面不会自动采集供应商数据。</p>";
  if (focusKey) {
    const replacement = Array.from(target.querySelectorAll("[data-fuyao-action]")).find((node) => node.dataset.fuyaoAction === focusKey[0] && node.dataset.fuyaoId === focusKey[1]);
    replacement?.focus({ preventScroll: true });
  }
}

function jobCard(job, options) {
  return `<article class="fuyao-job"><strong>${e(FUYAO_JOB_LABELS[job.kind] || job.kind)} · ${e(STATUS_LABELS[job.status] || job.status)}</strong>
    <span>${e(job.completed ?? 0)} / ${e(job.total ?? 0)} · ${e(job.message || "")}</span>
    ${progressView(job.progress)}
    <small>${e(job.created_at)}${job.finished_at ? ` → ${e(job.finished_at)}` : ""}</small>
    ${(job.errors || []).map((error) => `<p class="fuyao-error">${e(error)}</p>`).join("")}
    <div class="fuyao-actions">${actionButton("detail", job.id, "查看任务详情")}${jobActions(job, options)}</div></article>`;
}

function jobActions(job, options) {
  if (job.status === "running") return actionButton("cancel", job.id, "停止任务", options.submitting);
  if (job.status === "cancelling") return "<span>停止请求已接收，等待已有工作安全收尾。</span>";
  if (!job.request) return "<span class='fuyao-note'>旧任务未保存输入参数，无法直接补做。</span>";
  return canRetryFuyaoJob(job) ? actionButton("retry", job.id, "补做未完成项", options.submitting || options.active) : "";
}

function actionButton(action, id, label, disabled = false) {
  return `<button type="button" data-fuyao-action="${action}" data-fuyao-id="${e(id)}"${disabled ? " disabled" : ""}>${label}</button>`;
}

export function renderFuyaoJobDetail(target, job, options = {}) {
  if (!target) return;
  target.hidden = !options.id;
  if (!options.id) { target.innerHTML = ""; return; }
  target.innerHTML = `<div class="fuyao-heading"><strong>任务详情</strong><div class="fuyao-actions">${actionButton("detail", options.id, "重新读取任务")}${actionButton("close-detail", options.id, "关闭详情")}</div></div>
    <p class="fuyao-note">任务 ${e(options.id)}</p>${options.message ? `<p role="status" class="${options.error ? "fuyao-error" : "fuyao-note"}">${e(options.message)}</p>` : ""}
    ${job ? detailContent(job, options) : ""}`;
}

function detailContent(job, options) {
  const request = job.request;
  const body = request ? `<dl class="fuyao-job-parameters"><dt>任务类型</dt><dd>${e(FUYAO_JOB_LABELS[request.kind] || request.kind)}</dd>
    <dt>股票范围</dt><dd>${e((request.symbols || []).join("、") || "不按股票拆分")}</dd>
    <dt>板块范围</dt><dd>${e((request.index_symbols || []).join("、") || "未指定成员板块")}</dd>
    <dt>财报范围</dt><dd>${request.period === "quarterly" ? "季度报告" : "年报"} · 最近 ${e(request.limit)} 期 · 指定报告 ${e(request.report || "未指定")}</dd>
    <dt>已保存股票</dt><dd>${e((job.completed_symbols || []).join("、") || "尚无逐股完成记录")}</dd></dl>`
    : "<p>此任务未保存原输入参数，不能据错误文字推断补做范围。可核对后另行创建任务。</p>";
  return `<p>${e(STATUS_LABELS[job.status] || job.status)} · ${e(job.message || "")}</p>${progressView(job.progress)}${body}
    ${job.parent_job_id ? `<p class="fuyao-note">补做来源任务 ${e(job.parent_job_id)}</p>` : ""}
    <p class="fuyao-note">创建 ${e(job.created_at)} · 更新 ${e(job.updated_at || job.created_at)}</p>
    <div class="fuyao-actions">${jobActions(job, options)}</div>`;
}

function progressView(progress) {
  if (!progress) return "";
  const stage = STAGE_LABELS[progress.stage] || progress.stage;
  const current = progressNumber(progress.current);
  const total = progressNumber(progress.total);
  const unit = { bytes: "字节", rows: "行", items: "项", symbols: "只" }[progress.unit] || progress.unit || "";
  const amount = current === null ? "" : ` · ${current}${total === null ? "" : ` / ${total}`} ${e(unit)}${total === null ? "（总量未知）" : ""}`;
  return `<p class="fuyao-job-progress">${e(stage)}${amount}</p>`;
}

function progressNumber(value) {
  return typeof value === "number" && Number.isFinite(value) && value >= 0 ? value.toLocaleString("zh-CN") : null;
}
