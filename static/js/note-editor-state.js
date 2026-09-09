import { escapeHtml } from "./dom.js";
import { formatAuditTimestamp } from "./audit-time.js";
import { normalizeUiSymbol } from "./symbols.js";

const fields = ["content", "note_type", "price", "trade_date"];

export function noteDraft(form) {
  return Object.fromEntries(fields.map(name => [name, String(form?.elements?.namedItem(name)?.value ?? "")]));
}

export function preserveNoteEditors(target, symbol, removedId) {
  if (!symbol || normalizeUiSymbol(target.dataset?.noteSymbol) !== normalizeUiSymbol(symbol)) return [];
  return [...(target.querySelectorAll?.("[data-note-edit-form]") || [])]
    .filter(form => !form.hidden && /^[1-9][0-9]*$/.test(form.dataset.noteId) && form.dataset.noteId !== String(removedId))
    .map(form => ({ form, row: form.closest("[data-note-row]"), active: document.activeElement }));
}

export function restoreNoteEditors(target, editors) {
  for (const { form, row, active } of editors) {
    const currentRow = target.querySelector(`[data-note-row="${form.dataset.noteId}"]`);
    if (currentRow) currentRow.querySelector("[data-note-edit-form]").replaceWith(form);
    else target.append(row);
    const retainedRow = currentRow || row;
    retainedRow.querySelector("[data-note-edit]")?.setAttribute("aria-expanded", "true");
    if (form.contains(active)) active.focus({ preventScroll: true });
  }
}

export function reconcileNoteEditorSave(form, submitted, saved) {
  if (!ownsNoteEditorSubmission(form, submitted) || form.dataset.noteId !== String(saved?.id)) return;
  const unchanged = JSON.stringify(noteDraft(form)) === JSON.stringify(submitted.draft);
  if (unchanged) form.hidden = true;
  else adoptNoteBaseline(form, saved, true);
}

export function ownsNoteEditorSubmission(form, submitted) {
  return Boolean(form?.isConnected && !form.hidden
    && String(form.dataset.noteEditEpoch || "0") === submitted.epoch
    && form.dataset.noteRevision === submitted.expected_revision);
}

function adoptNoteBaseline(form, item, preserveDraft) {
  const draft = noteDraft(form);
  for (const name of fields) {
    const control = form.elements.namedItem(name);
    const value = String(item[name] ?? "");
    if (control.tagName === "SELECT") {
      if (![...control.options].some(option => option.value === value)) control.add(new Option(value, value));
      for (const option of control.options) option.defaultSelected = option.value === value;
    } else control.defaultValue = value;
    control.value = preserveDraft ? draft[name] : value;
  }
  form.dataset.noteRevision = item.revision;
  form.querySelector("[data-note-conflict]")?.remove();
  const feedback = form.querySelector(".inline-edit-feedback");
  if (feedback) feedback.hidden = true;
}

export function noteConflictState(form) {
  return { epoch: String(form?.dataset.noteEditEpoch || "0"), wasOpen: Boolean(form && !form.hidden) };
}

export function showNoteConflict(form, before = null) {
  if (!form?.isConnected || form.querySelector("[data-note-conflict]")) return;
  if (before && (String(form.dataset.noteEditEpoch || "0") !== before.epoch || before.wasOpen && form.hidden)) return;
  form.hidden = false;
  form.closest("[data-note-row]")?.querySelector("[data-note-edit]")?.setAttribute("aria-expanded", "true");
  const box = document.createElement("div");
  box.dataset.noteConflict = "";
  box.setAttribute("role", "status");
  box.innerHTML = '<p>这份笔记已有更新，当前草稿已保留。请先查看最新版本，再决定如何继续。</p>'
    + '<button type="button" class="mini-button" data-note-latest>查看最新版本</button>';
  form.append(box);
}

export function displayLatestNote(form, item) {
  const box = form.querySelector("[data-note-conflict]");
  if (!box) return;
  box.innerHTML = `<p><strong>当前保存的版本</strong> · ${escapeHtml(formatAuditTimestamp(item.updated_at))}</p>`
    + `<p>${escapeHtml(item.content)}</p><p>${escapeHtml(item.note_type)} · 价格 ${escapeHtml(item.price ?? "未填写")}`
    + ` · 日期 ${escapeHtml(item.trade_date || "按创建时间标注")} · ${item.visible ? "显示" : "隐藏"}`
    + ` · 颜色 ${escapeHtml(item.color || "默认")}</p>`
    + '<p>上方输入框仍是你的草稿。选择保留后，请核对内容并再次保存。</p>'
    + '<button type="button" class="mini-button" data-note-rebase="keep">保留草稿，基于此版本编辑</button> '
    + '<button type="button" class="mini-button" data-note-rebase="replace">改用此版本内容</button>';
  box.latestNote = item;
}

export function resolveNoteConflict(button) {
  const form = button.closest("[data-note-edit-form]");
  const item = form?.querySelector("[data-note-conflict]")?.latestNote;
  if (!item || form.dataset.noteId !== String(item.id)) return false;
  adoptNoteBaseline(form, item, button.dataset.noteRebase === "keep");
  return true;
}
