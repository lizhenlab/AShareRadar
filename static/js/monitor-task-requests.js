import { DEFAULT_REQUEST_TIMEOUT_MS, fetchJson, isAbortError } from "./api.js";
import { $ } from "./dom.js";

const STORAGE_KEY = "ashare-monitor-task-request-v1";
const TERMINAL = new Set(["completed", "failed", "interrupted"]);
const RECEIPT_STATUSES = new Set(["accepted", "running", "completed", "failed", "interrupted", "unknown"]);
const UUID = /^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$/;

function pendingRequest(state) {
  if (state.monitorTaskPending) return state.monitorTaskPending;
  if (state.monitorTaskStorageRead) return null;
  state.monitorTaskStorageRead = true;
  try {
    const saved = JSON.parse(globalThis.sessionStorage?.getItem(STORAGE_KEY) || "null");
    if (saved && UUID.test(saved.requestId) && /^[a-z_]+$/.test(saved.task)) {
      state.monitorTaskPending = { requestId: saved.requestId, task: saved.task, status: "unknown" };
    }
  } catch { /* Storage availability never decides whether a server task ran. */ }
  return state.monitorTaskPending || null;
}

function persistPending(state) {
  try {
    const pending = state.monitorTaskPending;
    if (pending) globalThis.sessionStorage?.setItem(STORAGE_KEY, JSON.stringify({ requestId: pending.requestId, task: pending.task }));
    else globalThis.sessionStorage?.removeItem(STORAGE_KEY);
  } catch { /* Keep the request identity in memory when browser storage is unavailable. */ }
}

function viewCurrent(options) {
  return !options.signal?.aborted && (!options.isCurrent || options.isCurrent())
    && (!options.isVisible || options.isVisible());
}

function acceptReceipt(state, pending, receipt) {
  if (!receipt || receipt.request_id !== pending.requestId || receipt.requested_task !== pending.task
      || !RECEIPT_STATUSES.has(receipt.status) || !Array.isArray(receipt.runs) || !Array.isArray(receipt.messages)) {
    throw new Error("任务回执身份或格式无效");
  }
  if (state.monitorTaskPending !== pending) return;
  state.monitorTaskReceipt = receipt;
  pending.status = receipt.status;
  if (TERMINAL.has(receipt.status)) state.monitorTaskPending = null;
  persistPending(state);
}

export async function submitMonitorTaskRequest(state, task, options = {}) {
  if (state.monitorTaskRunning || !viewCurrent(options)) return false;
  let pending = pendingRequest(state);
  if (pending && pending.task !== task) { renderMonitorTaskRequest(state); return false; }
  if (!pending) {
    pending = { requestId: globalThis.crypto.randomUUID(), task, status: "submitting" };
    state.monitorTaskPending = pending;
    state.monitorTaskReceipt = null;
    persistPending(state);
  }
  state.monitorTaskRunning = true;
  renderMonitorTaskRequest(state);
  try {
    // Navigation cancels reads, never ownership of an already submitted write.
    const result = await fetchJson(`/api/tasks/run-once?task=${encodeURIComponent(task)}&request_id=${pending.requestId}`, {
      method: "POST", timeoutMs: DEFAULT_REQUEST_TIMEOUT_MS,
    });
    acceptReceipt(state, pending, result.receipt);
    return true;
  } catch {
    if (state.monitorTaskPending === pending) pending.status = "unknown";
    return false;
  } finally {
    state.monitorTaskRunning = false;
    if (viewCurrent(options)) renderMonitorTaskRequest(state);
  }
}

export async function refreshMonitorTaskRequest(state, options = {}) {
  const pending = pendingRequest(state);
  if (!pending || state.monitorTaskRunning) return;
  try {
    const receipt = await fetchJson(`/api/tasks/requests/${pending.requestId}`, {
      signal: options.signal, timeoutMs: DEFAULT_REQUEST_TIMEOUT_MS, cache: "no-store",
    });
    if (viewCurrent(options)) acceptReceipt(state, pending, receipt);
  } catch (error) {
    if (!isAbortError(error) && viewCurrent(options) && state.monitorTaskPending === pending) pending.status = "unknown";
  }
}

export function renderMonitorTaskRequest(state) {
  const pending = pendingRequest(state);
  const receipt = state.monitorTaskReceipt;
  const status = state.monitorTaskRunning ? "submitting" : (pending?.status || receipt?.status);
  const labels = {
    submitting: "正在提交任务，结果待确认", accepted: "任务已受理，等待后台执行", running: "任务正在后台执行",
    completed: completedLabel(receipt), failed: "任务执行失败，请查看运行记录", interrupted: "执行已中断，实际结果请核对运行记录",
    unknown: "请求结果未确认；正在核对后台状态，可点击原按钮核对或重试同一请求",
  };
  if (status && $("schedulerState")) $("schedulerState").textContent = labels[status] || labels.unknown;
  document.querySelectorAll(".monitor-actions button").forEach((button) => {
    button.disabled = Boolean(state.monitorTaskRunning || (pending && button.dataset?.task && button.dataset.task !== pending.task));
  });
}

function completedLabel(receipt) {
  const statuses = new Set((receipt?.runs || []).map((run) => run.status));
  if (statuses.has("failed")) return "任务执行失败，请查看本次运行记录";
  if (statuses.has("cancelled") || statuses.has("running")) return "请求处理已结束，实际执行结果仍需核对";
  if (statuses.has("degraded")) return "任务已结束，部分数据未完成更新，请查看运行记录";
  if (statuses.has("pending")) return "任务已结束，数据仍待就绪，请查看运行记录";
  if (!statuses.size) return "请求处理已结束，未保留本次新执行记录；请核对任务状态";
  return "本次任务执行已完成";
}

export function monitorTaskRequestSettled(state) {
  return Boolean(state.monitorTaskReceipt && TERMINAL.has(state.monitorTaskReceipt.status) && !state.monitorTaskPending);
}
