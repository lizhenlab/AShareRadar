import { isAbortError } from "./api.js";
import { escapeHtml } from "./dom.js";
import { addAlertRule, alertRuleUpdatesFromForm, evaluateAlerts, removeAlertRule, toggleAlertRuleEditor, updateAlertRule } from "./alerts.js";
import { addStockNote, removeStockNote, stockNoteUpdatesFromForm, toggleStockNoteEditor, updateStockNote } from "./notes.js";

const boundRoots = new WeakMap();

export function bindStockNoteAlertEvents(options) {
  const root = options.root || globalThis.document;
  if (boundRoots.has(root)) return boundRoots.get(root);
  const context = { ...options, $: (id) => root.getElementById(id) };
  const bindings = [
    ["alertForm", "submit", handleAlertFormSubmit],
    ["evaluateAlerts", "click", handleEvaluateAlertsClick],
    ["alertList", "click", handleAlertListClick],
    ["alertList", "submit", handleAlertListSubmit],
    ["noteForm", "submit", handleNoteFormSubmit],
    ["noteList", "click", handleNoteListClick],
    ["noteList", "submit", handleNoteListSubmit],
  ];
  const removers = bindings.map(([id, type, handle]) => {
    const target = root.getElementById(id);
    const listener = (event) => handle(context, event);
    target.addEventListener(type, listener);
    return () => target.removeEventListener(type, listener);
  });
  const dispose = () => {
    if (boundRoots.get(root) !== dispose) return;
    removers.forEach((remove) => remove());
    boundRoots.delete(root);
  };
  boundRoots.set(root, dispose);
  return dispose;
}

async function handleAlertFormSubmit(context, event) {
  const { state, $, currentWorkbenchMutationOptions, clearInlineFeedback, runSubmitTask, setMutationStatus, renderResearchActivityPanel, setInlineFeedback, revealMobileFeedback } = context;
  event.preventDefault();
  const options = currentWorkbenchMutationOptions();
  if (!options) return;
  clearInlineFeedback("alertFormFeedback");
  try {
    await runSubmitTask(event.currentTarget, "添加中", () => addAlertRule(state, options));
    setMutationStatus("idle");
    renderResearchActivityPanel();
  } catch (error) {
    if (!isAbortError(error) && options.isCurrent()) {
      setInlineFeedback("alertFormFeedback", error);
      setMutationStatus("error", "提醒写入失败，原列表和草稿已保留", "warn");
      revealMobileFeedback($("alertForm"));
    }
  }
}

async function handleEvaluateAlertsClick(context, event) {
  const { state, $, currentWorkbenchMutationOptions, renderResearchActivityPanel, revealMobileFeedback } = context;
  const options = currentWorkbenchMutationOptions();
  if (!options) return;
  try {
    await evaluateAlerts(state, options);
    renderResearchActivityPanel();
  } catch (error) {
    if (!isAbortError(error) && options.isCurrent()) {
      $("alertEvents").innerHTML = `<div class="alert-event"><strong>检查失败</strong><p>${escapeHtml(error.message)}</p></div>`;
      revealMobileFeedback($("alertEvents"));
    }
  }
}

async function handleAlertListClick(context, event) {
  const { state, currentWorkbenchMutationOptions, renderResearchActivityPanel, clearRowActionError, runButtonTask, showRowActionError } = context;
  const editButton = event.target.closest("button[data-alert-edit]");
  if (editButton) {
    toggleAlertRuleEditor(editButton, true);
    return;
  }
  const cancelButton = event.target.closest("button[data-alert-cancel]");
  if (cancelButton) {
    const row = cancelButton.closest?.(".alert-row");
    toggleAlertRuleEditor(row?.querySelector?.("[data-alert-edit]") || cancelButton, false);
    return;
  }
  const toggleButton = event.target.closest("button[data-alert-toggle]");
  if (toggleButton) {
    const options = currentWorkbenchMutationOptions();
    if (!options) return;
    clearRowActionError(toggleButton);
    const completed = await runButtonTask(
      toggleButton,
      () => updateAlertRule(state, toggleButton.dataset.alertToggle, { enabled: toggleButton.dataset.alertEnabled === "true" }, options),
      { isCurrent: options.isCurrent, onError: (error) => showRowActionError(toggleButton, error) }
    );
    if (completed) renderResearchActivityPanel();
    return;
  }
  const button = event.target.closest("button[data-alert-remove]");
  if (!button) return;
  const options = currentWorkbenchMutationOptions();
  if (!options) return;
  clearRowActionError(button);
  const completed = await runButtonTask(
    button,
    () => removeAlertRule(state, button.dataset.alertRemove, options),
    { isCurrent: options.isCurrent, onError: (error) => showRowActionError(button, error) }
  );
  if (completed) renderResearchActivityPanel();
}

async function handleAlertListSubmit(context, event) {
  const { state, currentWorkbenchMutationOptions, runSubmitTask, renderResearchActivityPanel, setInlineEditError } = context;
  const form = event.target.closest("form[data-alert-edit-form]");
  if (!form) return;
  event.preventDefault();
  const options = currentWorkbenchMutationOptions();
  if (!options) return;
  const feedback = form.querySelector(".inline-edit-feedback");
  if (feedback) feedback.hidden = true;
  try {
    await runSubmitTask(form, "保存中", () =>
      updateAlertRule(state, form.dataset.alertId, alertRuleUpdatesFromForm(form), options)
    );
    renderResearchActivityPanel();
  } catch (error) {
    if (!isAbortError(error) && options.isCurrent()) setInlineEditError(form, error);
  }
}

async function handleNoteFormSubmit(context, event) {
  const { state, $, currentWorkbenchMutationOptions, clearInlineFeedback, runSubmitTask, setMutationStatus, renderResearchActivityPanel, setInlineFeedback, revealMobileFeedback, loadChartMarks } = context;
  event.preventDefault();
  const options = currentWorkbenchMutationOptions();
  if (!options) return;
  clearInlineFeedback("noteFormFeedback");
  try {
    await runSubmitTask(event.currentTarget, "保存中", () => addStockNote(state, loadChartMarks, options));
    setMutationStatus("idle");
    renderResearchActivityPanel();
  } catch (error) {
    if (!isAbortError(error) && options.isCurrent()) {
      setInlineFeedback("noteFormFeedback", error);
      setMutationStatus("error", "笔记写入失败，原列表和草稿已保留", "warn");
      revealMobileFeedback($("noteForm"));
    }
  }
}

async function handleNoteListClick(context, event) {
  const { state, currentWorkbenchMutationOptions, renderResearchActivityPanel, loadChartMarks, clearRowActionError, runButtonTask, showRowActionError } = context;
  const editButton = event.target.closest("button[data-note-edit]");
  if (editButton) {
    toggleStockNoteEditor(editButton, true);
    return;
  }
  const cancelButton = event.target.closest("button[data-note-cancel]");
  if (cancelButton) {
    const row = cancelButton.closest?.(".note-row");
    toggleStockNoteEditor(row?.querySelector?.("[data-note-edit]") || cancelButton, false);
    return;
  }
  const toggleButton = event.target.closest("button[data-note-toggle]");
  if (toggleButton) {
    const options = currentWorkbenchMutationOptions();
    if (!options) return;
    clearRowActionError(toggleButton);
    const completed = await runButtonTask(
      toggleButton,
      () => updateStockNote(state, toggleButton.dataset.noteToggle, { visible: toggleButton.dataset.noteVisible === "true" }, loadChartMarks, options),
      { isCurrent: options.isCurrent, onError: (error) => showRowActionError(toggleButton, error) }
    );
    if (completed) renderResearchActivityPanel();
    return;
  }
  const button = event.target.closest("button[data-note-remove]");
  if (!button) return;
  const options = currentWorkbenchMutationOptions();
  if (!options) return;
  clearRowActionError(button);
  const completed = await runButtonTask(
    button,
    () => removeStockNote(state, button.dataset.noteRemove, loadChartMarks, options),
    { isCurrent: options.isCurrent, onError: (error) => showRowActionError(button, error) }
  );
  if (completed) renderResearchActivityPanel();
}

async function handleNoteListSubmit(context, event) {
  const { state, currentWorkbenchMutationOptions, runSubmitTask, renderResearchActivityPanel, loadChartMarks, setInlineEditError } = context;
  const form = event.target.closest("form[data-note-edit-form]");
  if (!form) return;
  event.preventDefault();
  const options = currentWorkbenchMutationOptions();
  if (!options) return;
  const feedback = form.querySelector(".inline-edit-feedback");
  if (feedback) feedback.hidden = true;
  try {
    await runSubmitTask(form, "保存中", () =>
      updateStockNote(
        state,
        form.dataset.noteId,
        stockNoteUpdatesFromForm(form),
        loadChartMarks,
        options
      )
    );
    renderResearchActivityPanel();
  } catch (error) {
    if (!isAbortError(error) && options.isCurrent()) setInlineEditError(form, error);
  }
}
