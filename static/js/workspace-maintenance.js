import { fetchJson, isAbortError } from "./api.js";
import { escapeHtml } from "./dom.js";
import { formatAuditTimestamp } from "./audit-time.js";

const API = "/api/system/workspace-maintenance";
const STATUS = { healthy: "巡检正常", fault: "需要处理", unknown: "状态未知", manifest_checked: "清单身份已核对", unavailable: "无法读取" };
const PHASE = { checking: "检查中", waiting: "等待下一轮", stopped: "已停止", complete: "采集阶段结束", fault: "故障" };
const FAULT = { heartbeat_expired: "巡检已超时", runner_not_active: "采集进程未运行", runner_not_watching: "持续采集已停止", clock_regressed: "时钟发生回退", health_invalid_or_unreadable: "健康记录无法核对", health_changed_during_inspection: "状态正在变化，请刷新", study_blocked: "计划存在待满足条件", service_unavailable: "应用服务不可达", submission_uncertain: "扫描提交回执待确认", submission_conflict: "扫描提交身份冲突", capture_failed: "归档失败", runtime_validation_failed: "运行环境校验失败", cycle_failed: "巡检失败", scan_fault: "扫描异常", health_write_failed: "健康记录写入失败" };

export function createWorkspaceMaintenanceController({ isVisible = () => true } = {}) {
  const state = { sequence: 0, request: null, report: null };
  const element = (id) => document.getElementById(id);
  const current = (sequence) => sequence === state.sequence && isVisible() && !state.request?.signal.aborted;
  const deactivate = () => { state.sequence += 1; state.request?.abort(); state.request = null; };
  async function load() {
    if (!isVisible() || !element("workspaceMaintenanceStatus")) return false;
    deactivate();
    const sequence = state.sequence;
    state.request = new AbortController();
    setText("workspaceMaintenanceStatus", "正在读取维护状态…");
    setDisabled("workspaceStorageInspect", true);
    try {
      const report = await fetchJson(API, { signal: state.request.signal, timeoutMs: 12000, cache: "no-store" });
      if (!current(sequence)) return false;
      validateReport(report);
      state.report = report;
      renderReport(report);
      return true;
    } catch (error) {
      if (current(sequence) && !isAbortError(error)) setText("workspaceMaintenanceStatus", "维护状态读取失败，请重试；已显示的记录可能过期。");
      return false;
    }
  }
  async function inspectStorage() {
    if (!isVisible() || !state.report?.scope_digest) return false;
    deactivate();
    const sequence = state.sequence;
    state.request = new AbortController();
    setDisabled("workspaceStorageInspect", true);
    setText("workspaceStorageStatus", "正在检查已配置范围的容量…");
    try {
      const report = await fetchJson(`${API}/storage?scope_digest=${encodeURIComponent(state.report.scope_digest)}`, { method: "POST", signal: state.request.signal, timeoutMs: 12000 });
      if (!current(sequence)) return false;
      renderStorage(report);
      return true;
    } catch (error) {
      if (current(sequence) && !isAbortError(error)) setText("workspaceStorageStatus", "容量检查未完成，请刷新维护状态后重试；旧结果仅供参考。");
      return false;
    } finally {
      if (current(sequence)) setDisabled("workspaceStorageInspect", !state.report?.storage_root_count);
    }
  }
  element("workspaceMaintenanceRefresh")?.addEventListener("click", () => { void load(); });
  element("workspaceStorageInspect")?.addEventListener("click", () => { void inspectStorage(); });
  return { load, inspectStorage, deactivate };
}

function setText(id, value) {
  const element = document.getElementById(id);
  if (element) element.textContent = value;
}

function setDisabled(id, value) {
  const element = document.getElementById(id);
  if (element) element.disabled = value;
}

function setHtml(id, value) {
  const element = document.getElementById(id);
  if (element) element.innerHTML = value;
}

function validateReport(report) {
  if (report?.schema_version !== "workspace-maintenance-status-v1" || !["configured", "not_configured", "unavailable"].includes(report.status)) throw new Error("维护状态合同无效");
  if (!Array.isArray(report.backups) || !Array.isArray(report.runners)) throw new Error("维护记录合同无效");
  if (report.status === "configured" && !/^[a-f0-9]{64}$/.test(report.scope_digest || "")) throw new Error("维护范围身份无效");
}

function renderReport(report) {
  const status = report.status === "configured" ? `最近读取 ${time(report.checked_at)} · 容量检查范围 ${report.storage_root_count} 项` : report.status === "not_configured" ? "尚未配置工作区维护范围。配置后可查看容量、备份与采集状态。" : "维护范围配置无法读取，请检查服务配置。";
  setText("workspaceMaintenanceStatus", status);
  setDisabled("workspaceStorageInspect", report.status !== "configured" || !report.storage_root_count);
  setHtml("workspaceBackupRecords", report.backups.map(backupCard).join("") || '<p class="local-data-intro">未配置备份记录。</p>');
  setHtml("workspaceRunnerRecords", report.runners.map(runnerCard).join("") || '<p class="local-data-intro">未配置前瞻采集任务。</p>');
  setText("workspaceStorageStatus", "容量需手动检查；仅统计已配置范围，不扫描整个磁盘。");
  setHtml("workspaceStorageRoots", "");
}

function backupCard(item) {
  const verified = verificationLabel(item);
  const scope = Array.isArray(item.root_names) ? item.root_names.map(escapeHtml).join("、") : "";
  return `<article class="provider-item workspace-maintenance-card"><strong>${escapeHtml(item.name)} · ${escapeHtml(STATUS[item.status] || "状态未知")}</strong><span>${escapeHtml(verified)}</span><small>${escapeHtml(bytes(item.size_bytes))} · ${Number(item.file_count) || 0} 个文件 · 排除 ${Number(item.excluded_count) || 0} 项</small><small>覆盖：${scope || "未知"}</small><small>历史验证记录不代表当前文件已重新完整校验，也不代表恢复后可直接运行。</small></article>`;
}

function verificationLabel(item) {
  if (item.verification_record_status === "recorded") return `最近验证记录 ${time(item.last_verified_at)}`;
  if (item.verification_record_status === "undated") return "已有历史验证回执，旧格式未记录验证时间";
  return item.verification_record_status === "invalid" ? "验证回执无法核对" : "尚无可核对的验证时间";
}

function runnerCard(item) {
  const faults = Array.isArray(item.fault_codes) ? item.fault_codes.map((code) => FAULT[code] || "需要进一步核查").join("；") : "";
  return `<article class="provider-item workspace-maintenance-card"><strong>${escapeHtml(item.name)} · ${escapeHtml(STATUS[item.status] || "状态未知")}</strong><span>${escapeHtml(item.plan_id)} · ${escapeHtml(PHASE[item.phase] || "未取得巡检状态")}</span><small>最近完成 ${escapeHtml(time(item.last_completed_at))} · 下次检查 ${escapeHtml(time(item.next_check_at))}</small>${faults ? `<span>${escapeHtml(faults)}</span>` : ""}<small>巡检健康与计划身份按配置核对；运行环境及样本资格需独立验证。</small></article>`;
}

function renderStorage(report) {
  if (!["complete", "partial", "not_configured"].includes(report?.status) || !Array.isArray(report.roots)) throw new Error("容量报告合同无效");
  const partial = report.status === "partial";
  const counts = report.totals || {};
  setText("workspaceStorageStatus", `${partial ? "部分结果（未覆盖全部范围）" : report.status === "not_configured" ? "未配置容量范围" : "已配置范围检查完成"} · 已观察 ${bytes(counts.unique_bytes)} · ${Number(counts.unique_file_count) || 0} 个文件 · ${time(report.checked_at)}`);
  setHtml("workspaceStorageRoots", report.roots.map((root) => `<article class="provider-item workspace-maintenance-card"><strong>${escapeHtml(root.name)} · ${root.status === "complete" ? "完整" : "未完整"}</strong><span>${escapeHtml(bytes(root.counts?.unique_bytes))} · 排除 ${Number(root.counts?.excluded_count) || 0} 项</span></article>`).join(""));
}

function bytes(value) {
  const number = Number(value);
  if (!Number.isFinite(number) || number < 0) return "未知容量";
  return number >= 1024 ** 3 ? `${(number / 1024 ** 3).toFixed(2)} GiB` : `${(number / 1024 ** 2).toFixed(2)} MiB`;
}

function time(value) {
  return value ? formatAuditTimestamp(value) : "无记录";
}
