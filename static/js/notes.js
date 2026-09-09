import { DEFAULT_REQUEST_TIMEOUT_MS, fetchJson, isAbortError } from "./api.js";
import { formatAuditTimestamp } from "./audit-time.js";
import { $, escapeHtml } from "./dom.js";
import { formatNumber } from "./format.js";
import { toggleInlineEditor } from "./inline-editor.js";
import { createStockPanelRequestOwner } from "./stock-panel-requests.js";
import { normalizeUiSymbol } from "./symbols.js";
import { displayLatestNote, noteConflictState, noteDraft, ownsNoteEditorSubmission, preserveNoteEditors, reconcileNoteEditorSave, restoreNoteEditors, showNoteConflict } from "./note-editor-state.js";

const requests = createStockPanelRequestOwner({ readPrefix: "notesRead", mutationPrefix: "noteMutation" });

const NOTES_ACTIVITY_READ_ERROR = "\u7b14\u8bb0\u8bfb\u53d6\u5931\u8d25";
const NOTES_ACTIVITY_FORMAT_ERROR = "\u7b14\u8bb0\u6570\u636e\u683c\u5f0f\u5f02\u5e38";

export async function loadNotes(state, options = {}) {
  const request = requests.beginRead(state, options);
  try {
    return await refreshNotes(state, request, options.preserveOnError === true);
  } finally {
    requests.finishRead(state, request);
  }
}

export async function addStockNote(state, refreshChartMarks, options = {}) {
  const symbol = options.symbol || state.symbol;
  const content = $("noteContent").value.trim();
  const noteType = $("noteType").value;
  if (!content) throw new Error("请输入笔记内容");
  const quote = state.lastAnalysis && state.lastAnalysis.quote;
  const request = requests.beginMutation(state, options, symbol);
  try {
    const item = await fetchJson("/api/stock/notes", requestOptions(request, {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({
        symbol,
        content,
        note_type: noteType,
        price: quote ? quote.price : undefined,
        trade_date: quote ? quote.timestamp : undefined,
      }),
    }));
    if (!request.isCurrent()) return false;
    if ($("noteType").value === noteType && $("noteContent").value.trim() === content) {
      $("noteContent").value = "";
    }
    return await finishNoteMutation(state, request, refreshChartMarks, options.context, { action: "保存", item });
  } catch (error) {
    if (isAbortError(error) || !request.isCurrent()) return false;
    throw error;
  } finally {
    requests.finishMutation(state, request);
  }
}

export async function removeStockNote(state, noteId, refreshChartMarks, options = {}) {
  const revision = requiredNoteRevision(options.expectedRevision);
  const conflictBefore = noteConflictState(options.conflictForm);
  const request = requests.beginMutation(state, options);
  try {
    await fetchJson(`/api/stock/notes/${encodeURIComponent(noteId)}?expected_revision=${revision}`, requestOptions(request, { method: "DELETE" }));
    return await finishNoteMutation(state, request, refreshChartMarks, options.context, { action: "删除", id: noteId });
  } catch (error) {
    if (isAbortError(error) || !request.isCurrent()) return false;
    if (error.status === 409) showNoteConflict(options.conflictForm, conflictBefore);
    throw error;
  } finally {
    requests.finishMutation(state, request);
  }
}

export async function updateStockNote(state, noteId, payload, refreshChartMarks, options = {}) {
  requiredNoteRevision(payload.expected_revision);
  const conflictBefore = noteConflictState(options.conflictForm);
  const submitted = { expected_revision: payload.expected_revision, draft: noteDraft(options.noteForm),
    epoch: String(options.noteForm?.dataset.noteEditEpoch || "0") };
  const request = requests.beginMutation(state, options);
  try {
    const item = await fetchJson(`/api/stock/notes/${encodeURIComponent(noteId)}`, requestOptions(request, {
      method: "PATCH",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify(payload),
    }));
    if (!request.isCurrent()) return false;
    if (matchesNoteReceipt(item, noteId, request.symbol)) {
      reconcileNoteEditorSave(options.noteForm, submitted, item);
    }
    return await finishNoteMutation(state, request, refreshChartMarks, options.context, { action: "修改", id: noteId, item });
  } catch (error) {
    if (isAbortError(error) || !request.isCurrent()) return false;
    if (options.noteForm && !ownsNoteEditorSubmission(options.noteForm, submitted)) return false;
    if (error.status === 409) showNoteConflict(options.noteForm || options.conflictForm, options.noteForm ? null : conflictBefore);
    throw error;
  } finally {
    requests.finishMutation(state, request);
  }
}

function requiredNoteRevision(value) {
  if (typeof value !== "string" || !/^[0-9a-f]{64}$/.test(value)) throw new Error("笔记版本缺失，请重新读取后再操作");
  return value;
}

function matchesNoteReceipt(item, noteId, symbol) {
  return Number.isSafeInteger(item?.id) && item.id > 0 && item.id === Number(noteId)
    && normalizeUiSymbol(item.symbol) === normalizeUiSymbol(symbol)
    && typeof item.revision === "string" && /^[0-9a-f]{64}$/.test(item.revision)
    && ["content", "note_type", "created_at", "updated_at"].every(key => typeof item[key] === "string")
    && (item.price === null || typeof item.price === "number" && Number.isFinite(item.price))
    && ["trade_date", "color"].every(key => item[key] === null || typeof item[key] === "string")
    && typeof item.visible === "boolean";
}

export async function readLatestStockNote(state, noteId, form, options = {}) {
  const box = form.querySelector("[data-note-conflict]");
  const epoch = String(form.dataset.noteEditEpoch || "0");
  const owner = Symbol("latest-note");
  form.noteLatestRead = owner;
  const current = () => !options.signal?.aborted && (!options.isCurrent || options.isCurrent())
    && form.isConnected && !form.hidden && form.noteLatestRead === owner
    && form.querySelector("[data-note-conflict]") === box && String(form.dataset.noteEditEpoch || "0") === epoch;
  let item;
  try {
    item = await fetchJson(`/api/stock/notes/${encodeURIComponent(noteId)}`, {
      signal: options.signal, cache: "no-store", timeoutMs: DEFAULT_REQUEST_TIMEOUT_MS,
    });
  } catch (error) {
    if (!current()) return false;
    throw error;
  }
  if (!current()) return false;
  if (form.dataset.noteId !== String(noteId) || !matchesNoteReceipt(item, noteId, options.symbol || state.symbol)) {
    throw new Error("最新笔记身份或格式不匹配，草稿已保留");
  }
  displayLatestNote(form, item);
  return true;
}

async function finishNoteMutation(state, request, refreshChartMarks, context, mutation) {
  if (!request.isCurrent()) return false;
  reconcileNoteCommit(state, request.symbol, mutation);
  const refresh = requests.beginRefresh(state, request);
  const warnings = [];
  try {
    const loaded = await loadNotes(state, {
      symbol: request.symbol,
      signal: refresh.signal,
      isCurrent: refresh.isCurrent,
      preserveOnError: true,
    });
    if (!refresh.isCurrent()) return request.isCurrent();
    if (!loaded) warnings.push("列表刷新失败，当前仅显示已确认的笔记，请稍后刷新页面");
    try {
      await refreshChartMarks(scopedRefreshContext(refresh, context));
    } catch (error) {
      if (!isAbortError(error)) warnings.push(`图表标注刷新失败：${error.message}`);
    }
    if (refresh.isCurrent() && warnings.length) renderNoteCommitWarning(mutation.action, warnings);
    return request.isCurrent();
  } finally {
    requests.finishRefresh(state, refresh);
  }
}

function reconcileNoteCommit(state, symbol, mutation) {
  const known = knownNotes(state, symbol);
  const item = mutation.item;
  let items;
  if (mutation.action === "删除") {
    items = known.filter((note) => String(note.id) !== String(mutation.id));
  } else {
    if (!item || normalizeUiSymbol(item.symbol) !== normalizeUiSymbol(symbol) || !Number.isSafeInteger(item.id) || item.id <= 0) return;
    if (mutation.id !== undefined && String(item.id) !== String(mutation.id)) return;
    const present = known.some((note) => note.id === item.id);
    items = present ? known.map((note) => note.id === item.id ? item : note) : [item, ...known];
  }
  syncResearchActivityNotes(state, symbol, items, "unavailable", "笔记已提交，等待列表刷新");
  renderKnownNotesAfterReadFailure(items, symbol, mutation.action === "删除" ? mutation.id : undefined);
}

function knownNotes(state, symbol) {
  return normalizeUiSymbol(state.researchActivityNoteSource?.symbol) === normalizeUiSymbol(symbol) && Array.isArray(state.researchActivityNotes)
    ? state.researchActivityNotes : [];
}

function renderNoteCommitWarning(action, warnings) {
  const target = $("noteList");
  target.insertAdjacentHTML("afterbegin", `<div class="note-row" role="status"><strong>笔记已${escapeHtml(action)}</strong>`
    + `<span>${escapeHtml(warnings.join("；"))}。请勿重复提交。</span></div>`);
}

function scopedRefreshContext(request, context) {
  if (!context || typeof context !== "object") return context;
  return { ...context, symbol: request.symbol, signal: request.signal, isCurrent: request.isCurrent };
}

async function refreshNotes(state, request, preserveOnError) {
  try {
    const notes = await fetchJson(
      `/api/stock/notes?symbol=${encodeURIComponent(request.symbol)}&limit=8`,
      requestOptions(request)
    );
    if (!request.isCurrent()) return false;
    if (!Array.isArray(notes)) throw new TypeError(NOTES_ACTIVITY_FORMAT_ERROR);
    renderNotes(notes, request.symbol);
    syncResearchActivityNotes(state, request.symbol, notes, "ready", "");
    return true;
  } catch (error) {
    if (isAbortError(error) || !request.isCurrent()) return false;
    const message = error instanceof TypeError && error.message === NOTES_ACTIVITY_FORMAT_ERROR
      ? NOTES_ACTIVITY_FORMAT_ERROR
      : NOTES_ACTIVITY_READ_ERROR;
    const retained = preserveOnError ? knownNotes(state, request.symbol) : [];
    syncResearchActivityNotes(state, request.symbol, retained, "unavailable", message);
    if (preserveOnError) renderKnownNotesAfterReadFailure(retained, request.symbol);
    else $("noteList").innerHTML = `<div class="note-row"><strong>笔记读取失败</strong><span>${escapeHtml(error.message)}</span></div>`;
    return false;
  }
}

function renderKnownNotesAfterReadFailure(items, symbol, removedId) {
  if (items.length || preserveNoteEditors($("noteList"), symbol, removedId).length) renderNotes(items, symbol, removedId);
  else $("noteList").innerHTML = `<div class="note-row"><strong>笔记列表待同步</strong><span>当前无法确认完整列表，请刷新页面重试。</span></div>`;
}

function syncResearchActivityNotes(state, symbol, notes, phase, message) {
  state.researchActivityNotes = [...notes];
  state.researchActivityNoteSource = { symbol, phase, message };
}

function requestOptions(request, options = {}) {
  return {
    ...options,
    signal: request.signal,
    timeoutMs: DEFAULT_REQUEST_TIMEOUT_MS,
  };
}

export function renderNotes(items, symbol = null, removedId) {
  const target = $("noteList");
  const editors = preserveNoteEditors(target, symbol, removedId);
  target.innerHTML = items.length
    ? items
        .map(
          (item, index) => {
            const editorId = `note-editor-${escapeHtml(item.id || index)}`;
            return `
          <article class="note-row ${item.visible ? "" : "is-muted"}" data-note-row="${escapeHtml(item.id)}">
            <div class="editable-row-summary">
              <div>
              <strong>${escapeHtml(item.note_type)} · ${formatNumber(item.price)}</strong>
              <span>${escapeHtml(item.content)}</span>
              <small>${escapeHtml(item.trade_date || formatAuditTimestamp(item.created_at))}${item.visible ? "" : " · 已隐藏"}</small>
              </div>
              <div class="row-actions">
                <button type="button" class="mini-button" aria-label="编辑笔记" aria-expanded="false" aria-controls="${editorId}" data-note-edit="${escapeHtml(item.id)}">编辑</button>
                <button type="button" class="mini-button" data-note-toggle="${escapeHtml(item.id)}" data-note-revision="${escapeHtml(item.revision)}" data-note-visible="${item.visible ? "false" : "true"}">${item.visible ? "隐藏" : "显示"}</button>
                <button type="button" class="icon-button" title="删除笔记" aria-label="删除笔记" data-note-remove="${escapeHtml(item.id)}" data-note-revision="${escapeHtml(item.revision)}">×</button>
              </div>
            </div>
            <p class="row-action-feedback" role="alert" hidden></p>
            ${renderNoteEditor(item, editorId)}
          </article>`;
          }
        )
        .join("")
    : `<div class="note-row"><strong>${editors.length ? "当前列表未返回编辑中的笔记" : "暂无笔记"}</strong>`
      + `<span>${editors.length ? "草稿已保留，保存时将核对当前记录。" : "记录你的个股观察，会同步为图表标注。"}</span></div>`;
  if (target.dataset) target.dataset.noteSymbol = normalizeUiSymbol(symbol);
  restoreNoteEditors(target, editors);
}

function renderNoteEditor(item, editorId) {
  return `
    <form class="inline-edit-form note-edit-form" id="${editorId}" data-note-edit-form data-note-id="${escapeHtml(item.id)}" data-note-revision="${escapeHtml(item.revision)}" hidden>
      <div class="inline-edit-grid">
        <label><span>笔记类型</span><select name="note_type">${noteTypeOptions(item.note_type)}</select></label>
        <label><span>标注价格</span><input name="price" type="number" min="0.01" step="0.01" value="${escapeHtml(item.price ?? "")}" placeholder="可留空" /></label>
        <label class="inline-edit-wide"><span>日期或时间</span><input name="trade_date" value="${escapeHtml(item.trade_date || "")}" maxlength="20" placeholder="YYYY-MM-DD" /></label>
        <label class="inline-edit-wide"><span>笔记内容</span><textarea name="content" maxlength="500" required>${escapeHtml(item.content)}</textarea></label>
      </div>
      <div class="inline-edit-actions">
        <p class="inline-edit-feedback" role="alert" hidden></p>
        <span>
          <button type="button" class="mini-button" data-note-cancel="${escapeHtml(item.id)}">取消</button>
          <button type="submit" class="mini-button primary">保存</button>
        </span>
      </div>
    </form>`;
}

function noteTypeOptions(selected) {
  const values = ["观察", "买点", "卖点", "风险", "复盘"];
  if (selected && !values.includes(selected)) values.push(selected);
  return values
    .map((value) => `<option value="${escapeHtml(value)}"${value === selected ? " selected" : ""}>${escapeHtml(value)}</option>`)
    .join("");
}

export function stockNoteUpdatesFromForm(form) {
  const content = formValue(form, "content");
  const noteType = formValue(form, "note_type");
  const rawPrice = formValue(form, "price");
  if (!content) throw new Error("请输入笔记内容");
  if (!noteType) throw new Error("请选择笔记类型");
  const price = rawPrice ? Number(rawPrice) : null;
  if (rawPrice && (!Number.isFinite(price) || price <= 0)) throw new Error("标注价格必须大于0");
  return {
    expected_revision: requiredNoteRevision(form.dataset.noteRevision),
    content,
    note_type: noteType,
    price,
    trade_date: formValue(form, "trade_date") || null,
  };
}

export function toggleStockNoteEditor(button, forceOpen) {
  const form = button?.closest?.(".note-row")?.querySelector?.(".note-edit-form");
  const toggled = toggleInlineEditor(
    button,
    { row: ".note-row", form: ".note-edit-form", button: "[data-note-edit]", focus: "textarea, input, select" },
    forceOpen
  );
  if (toggled && form) {
    form.dataset.noteEditEpoch = String(Number(form.dataset.noteEditEpoch || 0) + 1);
    if (form.hidden) form.querySelector("[data-note-conflict]")?.remove();
  }
  return toggled;
}

function formValue(form, name) {
  const control = form?.elements?.namedItem?.(name) || form?.querySelector?.(`[name="${name}"]`);
  return String(control?.value || "").trim();
}
