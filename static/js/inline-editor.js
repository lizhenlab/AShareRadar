export function toggleInlineEditor(button, selectors, forceOpen) {
  const editor = findInlineEditor(button, selectors);
  if (!editor) return false;
  const open = typeof forceOpen === "boolean" ? forceOpen : editor.form.hidden;
  if (open) closeOtherEditors(editor.form, selectors);
  setEditorOpen(editor, open, selectors);
  return true;
}

function findInlineEditor(button, selectors) {
  if (!button || typeof button.closest !== "function") return null;
  const row = button.closest(selectors.row);
  if (!row || typeof row.querySelector !== "function") return null;
  const form = row.querySelector(selectors.form);
  return form ? { button, row, form } : null;
}

function closeOtherEditors(current, selectors) {
  if (typeof document === "undefined" || typeof document.querySelectorAll !== "function") return;
  document.querySelectorAll(`${selectors.form}:not([hidden])`).forEach((form) => {
    if (form === current) return;
    form.hidden = true;
    form.reset?.();
    const row = form.closest?.(selectors.row);
    row?.querySelector?.(selectors.button)?.setAttribute?.("aria-expanded", "false");
  });
}

function setEditorOpen(editor, open, selectors) {
  editor.form.hidden = !open;
  if (!open) editor.form.reset?.();
  editor.button.setAttribute?.("aria-expanded", String(open));
  if (!open) return;
  editor.form.querySelector?.(selectors.focus || "input, select, textarea")?.focus?.({ preventScroll: true });
}
