import { DEFAULT_REQUEST_TIMEOUT_MS, fetchJson, isAbortError } from "./api.js";
import { formatAuditTimestamp } from "./audit-time.js";
import { $, escapeHtml } from "./dom.js";
import { formatNumber } from "./format.js";
import { toggleInlineEditor } from "./inline-editor.js";
import { createStockPanelRequestOwner } from "./stock-panel-requests.js";

const requests = createStockPanelRequestOwner({ readPrefix: "notesRead", mutationPrefix: "noteMutation" });

const NOTES_ACTIVITY_READ_ERROR = "\u7b14\u8bb0\u8bfb\u53d6\u5931\u8d25";
const NOTES_ACTIVITY_FORMAT_ERROR = "\u7b14\u8bb0\u6570\u636e\u683c\u5f0f\u5f02\u5e38";

export async function loadNotes(state, options = {}) {
  const request = requests.beginRead(state, options);
  try {
    return await refreshNotes(state, request);
  } finally {
    requests.finishRead(state, request);
  }
}

export async function addStockNote(state, refreshChartMarks, options = {}) {
  const symbol = options.symbol || state.symbol;
  const content = $("noteContent").value.trim();
  if (!content) throw new Error("请输入笔记内容");
  const quote = state.lastAnalysis && state.lastAnalysis.quote;
  const request = requests.beginMutation(state, options, symbol);
  try {
    await fetchJson("/api/stock/notes", requestOptions(request, {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({
        symbol,
        content,
        note_type: $("noteType").value,
        price: quote ? quote.price : undefined,
        trade_date: quote ? quote.timestamp : undefined,
      }),
    }));
    if (!request.isCurrent()) return false;
    if ($("noteContent").value.trim() === content) $("noteContent").value = "";
    return await finishNoteMutation(state, request, refreshChartMarks, options.context);
  } catch (error) {
    if (isAbortError(error) || !request.isCurrent()) return false;
    throw error;
  } finally {
    requests.finishMutation(state, request);
  }
}

export async function removeStockNote(state, noteId, refreshChartMarks, options = {}) {
  const request = requests.beginMutation(state, options);
  try {
    await fetchJson(`/api/stock/notes/${encodeURIComponent(noteId)}`, requestOptions(request, { method: "DELETE" }));
    return await finishNoteMutation(state, request, refreshChartMarks, options.context);
  } catch (error) {
    if (isAbortError(error) || !request.isCurrent()) return false;
    throw error;
  } finally {
    requests.finishMutation(state, request);
  }
}

export async function updateStockNote(state, noteId, payload, refreshChartMarks, options = {}) {
  const request = requests.beginMutation(state, options);
  try {
    await fetchJson(`/api/stock/notes/${encodeURIComponent(noteId)}`, requestOptions(request, {
      method: "PATCH",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify(payload),
    }));
    return await finishNoteMutation(state, request, refreshChartMarks, options.context);
  } catch (error) {
    if (isAbortError(error) || !request.isCurrent()) return false;
    throw error;
  } finally {
    requests.finishMutation(state, request);
  }
}

async function finishNoteMutation(state, request, refreshChartMarks, context) {
  if (!request.isCurrent()) return false;
  const refresh = requests.beginRefresh(state, request);
  try {
    await loadNotes(state, {
      symbol: request.symbol,
      signal: refresh.signal,
      isCurrent: refresh.isCurrent,
    });
    if (!refresh.isCurrent()) return request.isCurrent();
    await refreshChartMarks(scopedRefreshContext(refresh, context));
    return request.isCurrent();
  } catch (error) {
    if (isAbortError(error) || !refresh.isCurrent()) return request.isCurrent();
    throw error;
  } finally {
    requests.finishRefresh(state, refresh);
  }
}

function scopedRefreshContext(request, context) {
  if (!context || typeof context !== "object") return context;
  return { ...context, symbol: request.symbol, signal: request.signal, isCurrent: request.isCurrent };
}

async function refreshNotes(state, request) {
  try {
    const notes = await fetchJson(
      `/api/stock/notes?symbol=${encodeURIComponent(request.symbol)}&limit=8`,
      requestOptions(request)
    );
    if (!request.isCurrent()) return false;
    if (!Array.isArray(notes)) throw new TypeError(NOTES_ACTIVITY_FORMAT_ERROR);
    syncResearchActivityNotes(state, request.symbol, notes, "ready", "");
    renderNotes(notes);
    return true;
  } catch (error) {
    if (isAbortError(error) || !request.isCurrent()) return false;
    const message = error instanceof TypeError && error.message === NOTES_ACTIVITY_FORMAT_ERROR
      ? NOTES_ACTIVITY_FORMAT_ERROR
      : NOTES_ACTIVITY_READ_ERROR;
    syncResearchActivityNotes(state, request.symbol, [], "unavailable", message);
    $("noteList").innerHTML = `<div class="note-row"><strong>笔记读取失败</strong><span>${escapeHtml(error.message)}</span></div>`;
    return false;
  }
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

export function renderNotes(items) {
  $("noteList").innerHTML = items.length
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
                <button type="button" class="mini-button" data-note-toggle="${escapeHtml(item.id)}" data-note-visible="${item.visible ? "false" : "true"}">${item.visible ? "隐藏" : "显示"}</button>
                <button type="button" class="icon-button" title="删除笔记" aria-label="删除笔记" data-note-remove="${escapeHtml(item.id)}">×</button>
              </div>
            </div>
            <p class="row-action-feedback" role="alert" hidden></p>
            ${renderNoteEditor(item, editorId)}
          </article>`;
          }
        )
        .join("")
    : `<div class="note-row"><strong>暂无笔记</strong><span>记录你的个股观察，会同步为图表标注。</span></div>`;
}

function renderNoteEditor(item, editorId) {
  return `
    <form class="inline-edit-form note-edit-form" id="${editorId}" data-note-edit-form data-note-id="${escapeHtml(item.id)}" hidden>
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
    content,
    note_type: noteType,
    price,
    trade_date: formValue(form, "trade_date") || null,
  };
}

export function toggleStockNoteEditor(button, forceOpen) {
  return toggleInlineEditor(
    button,
    { row: ".note-row", form: ".note-edit-form", button: "[data-note-edit]", focus: "textarea, input, select" },
    forceOpen
  );
}

function formValue(form, name) {
  const control = form?.elements?.namedItem?.(name) || form?.querySelector?.(`[name="${name}"]`);
  return String(control?.value || "").trim();
}
