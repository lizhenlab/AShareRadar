import { normalizeUiSymbol } from "./symbols.js";

const fields = ["name", "condition_type", "threshold", "cooldown_seconds", "note"];

export function alertEditorSubmission(form) {
  return { epoch: String(form?.dataset.alertEditEpoch || "0"), draft: alertDraft(form) };
}

function alertDraft(form) {
  return Object.fromEntries(fields.map(name => [name, String(form?.elements?.namedItem(name)?.value ?? "")]));
}

export function ownsAlertEditorSubmission(form, submitted) {
  return Boolean(form?.isConnected && !form.hidden
    && String(form.dataset.alertEditEpoch || "0") === submitted.epoch);
}

export function reconcileAlertEditorSave(form, submitted, item) {
  if (!ownsAlertEditorSubmission(form, submitted)) return;
  const draft = alertDraft(form);
  if (JSON.stringify(draft) === JSON.stringify(submitted.draft)) {
    form.hidden = true;
    return;
  }
  updateAlertEditorBaseline(form, item);
}

function updateAlertEditorBaseline(form, item) {
  const draft = alertDraft(form);
  for (const name of fields) {
    const control = form.elements.namedItem(name);
    const value = String(item[name] ?? "");
    if (control.tagName === "SELECT") {
      for (const option of control.options) option.defaultSelected = option.value === value;
    } else control.defaultValue = value;
    control.value = draft[name];
  }
}

export function preserveAlertEditors(target, previous, next) {
  const previousById = new Map((previous || []).map(item => [String(item.id), item]));
  const nextById = new Map(next.map(item => [String(item.id), item]));
  return [...(target.querySelectorAll?.("[data-alert-edit-form]") || [])]
    .filter(form => {
      const before = previousById.get(form.dataset.alertId), after = nextById.get(form.dataset.alertId);
      return !form.hidden && /^[1-9][0-9]*$/.test(form.dataset.alertId) && before && after
        && typeof before.created_at === "string" && before.created_at !== "" && before.created_at === after.created_at
        && normalizeUiSymbol(before.symbol) === normalizeUiSymbol(after.symbol);
    })
    .map(form => ({ form, item: nextById.get(form.dataset.alertId), active: document.activeElement }));
}

export function restoreAlertEditors(target, editors) {
  for (const { form, item, active } of editors) {
    updateAlertEditorBaseline(form, item);
    const row = target.querySelector(`[data-alert-row="${form.dataset.alertId}"]`);
    row.querySelector("[data-alert-edit-form]").replaceWith(form);
    row.querySelector("[data-alert-edit]")?.setAttribute("aria-expanded", "true");
    if (form.contains(active)) active.focus({ preventScroll: true });
  }
}
