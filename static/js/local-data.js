import { DEFAULT_REQUEST_TIMEOUT_MS, fetchJson } from "./api.js";
import { $, escapeHtml } from "./dom.js";

export const MAX_LOCAL_DATA_IMPORT_BYTES = 50 * 1024 * 1024;
export const LOCAL_DATA_PREVIEW_TIMEOUT_MS = 2 * 60 * 1000;

let cleanupPreviewRequestGeneration = 0;

export async function exportLocalUserData(options = {}) {
  const bundle = await fetchJson("/api/local-data/export", {
    method: "POST",
    timeoutMs: DEFAULT_REQUEST_TIMEOUT_MS,
  });
  const blob = new Blob([`${JSON.stringify(bundle, null, 2)}\n`], { type: "application/json" });
  const url = URL.createObjectURL(blob);
  try {
    const anchor = document.createElement("a");
    anchor.href = url;
    anchor.download = `ashare-radar-user-data-${dateStamp(options.now)}.json`;
    anchor.click();
  } finally {
    URL.revokeObjectURL(url);
  }
  setLocalDataFeedback("用户数据已导出", "ok");
  return bundle;
}

export async function readLocalDataFile(state, file) {
  const selectionGeneration = beginLocalDataFileSelection(state);
  if (!file) throw new Error("请选择用户数据 JSON 文件");
  const fileKey = localDataFileKey(file);
  if (file.size > MAX_LOCAL_DATA_IMPORT_BYTES) throw new Error("导入文件不能超过50 MB");
  let text;
  try {
    text = await file.text();
  } catch (error) {
    if (!ownsLocalDataFileSelection(state, selectionGeneration)) return null;
    throw error;
  }
  if (!ownsLocalDataFileSelection(state, selectionGeneration)) return null;
  let bundle;
  try {
    bundle = JSON.parse(text);
  } catch {
    throw new Error("导入文件不是有效 JSON");
  }
  state.localDataImportBundle = bundle;
  state.localDataImportFileKey = fileKey;
  setLocalDataFeedback(`已读取 ${file.name}`);
  return bundle;
}

export async function previewLocalDataImport(state) {
  const bundle = requiredImportBundle(state);
  const mode = importMode();
  const ownership = {
    bundle,
    fileKey: state.localDataImportFileKey,
    mode,
    selectionGeneration: localDataSelectionGeneration(state),
    requestGeneration: supersedeImportPreview(state),
  };
  let result;
  try {
    result = await importRequest(bundle, mode, true);
  } catch (error) {
    if (!ownsImportPreviewRequest(state, ownership)) return null;
    throw error;
  }
  if (!ownsImportPreviewRequest(state, ownership)) return null;
  state.localDataImportPreview = result;
  state.localDataImportPreviewMode = mode;
  state.localDataImportPreviewFileKey = ownership.fileKey;
  state.localDataImportPreviewSelectionGeneration = ownership.selectionGeneration;
  state.localDataImportPreviewGeneration = ownership.requestGeneration;
  scheduleImportPreviewExpiry(state, result, ownership);
  renderImportPreview(result);
  syncImportCommitButton(state);
  return result;
}

export async function commitLocalDataImport(state) {
  requireMatchingPreview(state);
  const mode = importMode();
  const ownership = {
    bundle: state.localDataImportBundle,
    fileKey: state.localDataImportFileKey,
    mode,
    preview: state.localDataImportPreview,
    selectionGeneration: localDataSelectionGeneration(state),
    requestGeneration: state.localDataImportPreviewRequestGeneration,
  };
  const token = ownership.preview.preview_token;
  let result;
  try {
    result = await importRequest(ownership.bundle, mode, false, token);
  } catch (error) {
    const ownsCurrentSelection = ownsImportCommit(state, ownership);
    if (ownsCurrentSelection) {
      clearImportPreview(state);
      throw error;
    }
    setLocalDataFeedback("先前选择的用户数据导入失败；当前文件状态保持，请按当前预览继续。", "warn");
    return null;
  }
  const ownsCurrentSelection = ownsImportCommit(state, ownership);
  if (ownsCurrentSelection) clearImportPreview(state);
  if (!ownsCurrentSelection) {
    setLocalDataFeedback("先前选择的用户数据已导入；当前文件状态保持，请按当前预览继续。", "warn");
    return result;
  }
  renderImportPreview(result);
  setLocalDataFeedback("用户数据导入已提交", "ok");
  return result;
}

export function invalidateLocalDataImportPreview(state) {
  supersedeImportPreview(state);
  setLocalDataFeedback("导入模式已变化，请重新预览");
}

export async function loadRuntimeCleanupPreview() {
  const generation = ++cleanupPreviewRequestGeneration;
  try {
    const preview = await fetchJson("/api/local-data/cleanup-preview", { timeoutMs: DEFAULT_REQUEST_TIMEOUT_MS });
    if (generation !== cleanupPreviewRequestGeneration) return null;
    renderCleanupPreview(preview);
    return preview;
  } catch (error) {
    if (generation !== cleanupPreviewRequestGeneration) return null;
    renderCleanupPreviewUnavailable(error);
    throw error;
  }
}

export async function runRuntimeCleanup(preview, options = {}) {
  if (!preview || Number(preview.total_rows) <= 0) return false;
  if (preview.requires_user_backup && options.confirm && !options.confirm("清理包含建议或预警历史，系统会先创建恢复备份。确认继续？")) {
    return false;
  }
  const result = await fetchJson("/api/local-data/cleanup?confirm=retention-cleanup", {
    method: "POST",
    timeoutMs: 0,
  });
  cleanupPreviewRequestGeneration += 1;
  renderCleanupPreview({ ...result, total_rows: 0, tables: {} });
  const backup = result.rollback_backup_path ? `；恢复备份：${result.rollback_backup_path}` : "";
  setLocalDataFeedback(`已清理 ${result.total_rows} 条过期记录${backup}`, "ok");
  return result;
}

export function renderImportPreview(result) {
  const target = $("localDataImportPreview");
  if (!target) return;
  const totals = result?.totals || {};
  const backup = result?.rollback_backup_path
    ? `<small>提交前已创建恢复备份：${escapeHtml(result.rollback_backup_path)}</small>`
    : "";
  target.innerHTML = `
    <strong>${result?.dry_run ? "导入预览" : "导入完成"}</strong>
    <span>新增 ${escapeHtml(totals.inserted || 0)} · 更新 ${escapeHtml(totals.updated || 0)} · 不变 ${escapeHtml(totals.unchanged || 0)} · 删除 ${escapeHtml(totals.deleted || 0)}</span>
    ${backup}`;
}

export function renderCleanupPreview(preview) {
  const target = $("runtimeCleanupPreview");
  const button = $("runRuntimeCleanup");
  if (!target) return;
  const total = Number(preview?.total_rows || 0);
  const userRows = Number(preview?.user_history_rows || 0);
  target.innerHTML = total
    ? `<strong>预计清理 ${escapeHtml(total)} 条</strong><span>其中建议/预警历史 ${escapeHtml(userRows)} 条${preview.requires_user_backup ? "，系统将先创建恢复备份" : ""}</span>`
    : `<strong>暂无超出保留上限的记录</strong>`;
  if (button) button.disabled = total <= 0;
}

export function renderCleanupPreviewUnavailable(error) {
  const target = $("runtimeCleanupPreview");
  const button = $("runRuntimeCleanup");
  if (target) {
    target.innerHTML = `<strong>保留策略预览读取失败</strong><span>${escapeHtml(error?.message || "请稍后重试")}</span>`;
  }
  if (button) button.disabled = true;
}

function importRequest(bundle, mode, dryRun, previewToken = "") {
  const tokenQuery = previewToken ? `&preview_token=${encodeURIComponent(previewToken)}` : "";
  return fetchJson(`/api/local-data/import?mode=${encodeURIComponent(mode)}&dry_run=${dryRun ? "true" : "false"}${tokenQuery}`, {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify(bundle),
    timeoutMs: dryRun ? LOCAL_DATA_PREVIEW_TIMEOUT_MS : 0,
  });
}

function requireMatchingPreview(state) {
  requiredImportBundle(state);
  if (
    !state.localDataImportPreview ||
    !state.localDataImportPreview.preview_token ||
    state.localDataImportPreviewMode !== importMode() ||
    state.localDataImportPreviewFileKey !== state.localDataImportFileKey ||
    state.localDataImportPreviewSelectionGeneration !== localDataSelectionGeneration(state) ||
    state.localDataImportPreviewGeneration !== state.localDataImportPreviewRequestGeneration ||
    previewExpired(state.localDataImportPreview)
  ) {
    throw new Error("请先对当前文件和导入模式执行预览");
  }
}

function requiredImportBundle(state) {
  if (!state.localDataImportBundle) throw new Error("请先选择用户数据 JSON 文件");
  return state.localDataImportBundle;
}

function importMode() {
  return $("localDataImportMode")?.value === "replace" ? "replace" : "merge";
}

function beginLocalDataFileSelection(state) {
  const generation = nextGeneration(state.localDataImportSelectionGeneration);
  state.localDataImportSelectionGeneration = generation;
  state.localDataImportBundle = null;
  state.localDataImportFileKey = "";
  supersedeImportPreview(state);
  return generation;
}

function ownsLocalDataFileSelection(state, generation) {
  return state.localDataImportSelectionGeneration === generation;
}

function localDataFileKey(file) {
  return `${file.name}:${file.size}:${file.lastModified || 0}`;
}

function localDataSelectionGeneration(state) {
  return Number.isSafeInteger(state.localDataImportSelectionGeneration)
    ? state.localDataImportSelectionGeneration
    : 0;
}

function supersedeImportPreview(state) {
  const generation = nextGeneration(state.localDataImportPreviewRequestGeneration);
  state.localDataImportPreviewRequestGeneration = generation;
  clearImportPreview(state);
  return generation;
}

function ownsImportPreviewRequest(state, ownership) {
  return (
    state.localDataImportPreviewRequestGeneration === ownership.requestGeneration &&
    localDataSelectionGeneration(state) === ownership.selectionGeneration &&
    state.localDataImportBundle === ownership.bundle &&
    state.localDataImportFileKey === ownership.fileKey &&
    importMode() === ownership.mode
  );
}

function ownsImportCommit(state, ownership) {
  return state.localDataImportPreview === ownership.preview && ownsImportPreviewRequest(state, ownership);
}

function nextGeneration(value) {
  return Number.isSafeInteger(value) && value >= 0 ? value + 1 : 1;
}

function clearImportPreview(state) {
  if (state.localDataImportPreviewExpiryTimer) {
    clearTimeout(state.localDataImportPreviewExpiryTimer);
    state.localDataImportPreviewExpiryTimer = null;
  }
  state.localDataImportPreview = null;
  state.localDataImportPreviewMode = "";
  state.localDataImportPreviewFileKey = "";
  state.localDataImportPreviewSelectionGeneration = -1;
  state.localDataImportPreviewGeneration = -1;
  const target = $("localDataImportPreview");
  if (target) target.innerHTML = "";
  syncImportCommitButton(state);
}

function syncImportCommitButton(state) {
  const button = $("commitLocalDataImport");
  if (!button) return;
  button.disabled = !(
    state.localDataImportPreview &&
    state.localDataImportPreview.preview_token &&
    !previewExpired(state.localDataImportPreview) &&
    state.localDataImportPreviewMode === importMode() &&
    state.localDataImportPreviewFileKey === state.localDataImportFileKey &&
    state.localDataImportPreviewSelectionGeneration === localDataSelectionGeneration(state) &&
    state.localDataImportPreviewGeneration === state.localDataImportPreviewRequestGeneration
  );
}

function scheduleImportPreviewExpiry(state, preview, ownership) {
  if (state.localDataImportPreviewExpiryTimer) clearTimeout(state.localDataImportPreviewExpiryTimer);
  const expiresAt = Date.parse(String(preview?.preview_expires_at || ""));
  if (!Number.isFinite(expiresAt)) return;
  const delay = Math.max(0, expiresAt - Date.now());
  const timer = setTimeout(() => {
    if (
      state.localDataImportPreview !== preview ||
      state.localDataImportPreview?.preview_token !== preview.preview_token ||
      !ownsImportPreviewRequest(state, ownership) ||
      state.localDataImportPreviewGeneration !== ownership.requestGeneration
    ) {
      return;
    }
    clearImportPreview(state);
    setLocalDataFeedback("导入预览已过期，请重新预览");
  }, delay);
  timer?.unref?.();
  state.localDataImportPreviewExpiryTimer = timer;
}

function previewExpired(preview) {
  const expiresAt = Date.parse(String(preview?.preview_expires_at || ""));
  return Number.isFinite(expiresAt) && expiresAt <= Date.now();
}

function setLocalDataFeedback(message, tone = "") {
  const target = $("localDataFeedback");
  if (!target) return;
  target.textContent = message;
  target.dataset.tone = tone;
  target.hidden = !message;
}

function dateStamp(now) {
  const value = now instanceof Date ? now : new Date();
  return value.toISOString().slice(0, 10);
}
