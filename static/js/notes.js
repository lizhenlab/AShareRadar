import { fetchJson } from "./api.js";
import { $, escapeHtml } from "./dom.js";
import { formatNumber } from "./format.js";

export async function loadNotes(state, options = {}) {
  const symbol = options.symbol || state.symbol;
  const isCurrent = options.isCurrent || (() => true);
  try {
    const notes = await fetchJson(`/api/stock/notes?symbol=${encodeURIComponent(symbol)}&limit=8`);
    if (!isCurrent()) return false;
    renderNotes(notes);
    return true;
  } catch (error) {
    if (!isCurrent()) return false;
    $("noteList").innerHTML = `<div class="note-row"><strong>笔记读取失败</strong><span>${escapeHtml(error.message)}</span></div>`;
    return false;
  }
}

export async function addStockNote(state, refreshChartMarks, options = {}) {
  const symbol = options.symbol || state.symbol;
  const content = $("noteContent").value.trim();
  if (!content) throw new Error("请输入笔记内容");
  const quote = state.lastAnalysis && state.lastAnalysis.quote;
  await fetchJson("/api/stock/notes", {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({
      symbol,
      content,
      note_type: $("noteType").value,
      price: quote ? quote.price : undefined,
      trade_date: quote ? quote.timestamp : undefined,
    }),
  });
  if (options.isCurrent && !options.isCurrent()) return false;
  $("noteContent").value = "";
  await loadNotes(state, { symbol, isCurrent: options.isCurrent });
  await refreshChartMarks(options.context);
  return true;
}

export async function removeStockNote(state, noteId, refreshChartMarks, options = {}) {
  await fetchJson(`/api/stock/notes/${encodeURIComponent(noteId)}`, { method: "DELETE" });
  if (options.isCurrent && !options.isCurrent()) return false;
  await loadNotes(state, { symbol: options.symbol || state.symbol, isCurrent: options.isCurrent });
  await refreshChartMarks(options.context);
  return true;
}

export async function updateStockNote(state, noteId, payload, refreshChartMarks, options = {}) {
  await fetchJson(`/api/stock/notes/${encodeURIComponent(noteId)}`, {
    method: "PATCH",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify(payload),
  });
  if (options.isCurrent && !options.isCurrent()) return false;
  await loadNotes(state, { symbol: options.symbol || state.symbol, isCurrent: options.isCurrent });
  await refreshChartMarks(options.context);
  return true;
}

export function renderNotes(items) {
  $("noteList").innerHTML = items.length
    ? items
        .map(
          (item) => `
          <div class="note-row ${item.visible ? "" : "is-muted"}">
            <div>
              <strong>${escapeHtml(item.note_type)} · ${formatNumber(item.price)}</strong>
              <span>${escapeHtml(item.content)}</span>
              <small>${escapeHtml(item.trade_date || item.created_at)}${item.visible ? "" : " · 已隐藏"}</small>
            </div>
            <div class="row-actions">
              <button type="button" class="mini-button" data-note-toggle="${escapeHtml(item.id)}" data-note-visible="${item.visible ? "false" : "true"}">${item.visible ? "隐藏" : "显示"}</button>
              <button type="button" class="icon-button" title="删除笔记" aria-label="删除笔记" data-note-remove="${escapeHtml(item.id)}">×</button>
            </div>
          </div>`
        )
        .join("")
    : `<div class="note-row"><strong>暂无笔记</strong><span>记录你的个股观察，会同步为图表标注。</span></div>`;
}
