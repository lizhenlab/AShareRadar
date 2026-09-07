import { isAbortError } from "./api.js";
import {
  beginAdviceReviewEdit, cancelAdviceReviewEdit, deleteAdviceReviewPlan, evaluateAdviceReviewPlan,
  evaluateDueAdviceReviews, loadAdviceReviewDashboard, loadAdviceReviews, loadMoreAdviceReviews,
  retryAdviceReviewHistory, selectAdviceReviewSnapshot, setAdviceReviewEvaluationAsOf,
  submitAdviceReviewPlan, toggleAdviceReviewHistory, updateAdviceReviewDashboardFilters,
} from "./advice-reviews.js";
import { selectPaperTradingPlan, syncPaperTradingPlans } from "./paper-trading.js";

const boundRoots = new WeakMap();

export function bindAdviceReviewEvents(options) {
  const root = options.root || globalThis.document;
  if (boundRoots.has(root)) return boundRoots.get(root);
  const context = { ...options, $: (id) => root.getElementById(id), confirm: options.confirm || ((message) => globalThis.window.confirm(message)) };
  const bindings = [
    ["reviewAdviceId", "change", handleReviewAdviceIdChange],
    ["evaluateDueReviews", "click", handleEvaluateDueReviewsClick],
    ["reviewDashboardQueue", "click", handleReviewDashboardQueueClick],
    ["reviewPlanCancel", "click", handleReviewPlanCancelClick],
    ["reviewPlanLoadMore", "click", handleReviewPlanLoadMoreClick],
    ["reviewPlanForm", "submit", handleReviewPlanFormSubmit],
    ["reviewPlanList", "click", handleReviewPlanListClick],
    ["reviewPlanList", "change", handleReviewPlanListChange],
    ["reviewDashboardStatus", "change", handleDashboardFilter],
    ["reviewDashboardSymbol", "input", handleDashboardFilter],
    ["reviewDashboardFrom", "change", handleDashboardFilter],
    ["reviewDashboardHorizon", "change", handleDashboardFilter],
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

function handleReviewAdviceIdChange(context, event) {
  const { state } = context;
  selectAdviceReviewSnapshot(state);
}

async function handleEvaluateDueReviewsClick(context, event) {
  const { state, currentWorkbenchMutationOptions, runButtonTask, setInlineFeedback } = context;
  await runButtonTask(
    event.currentTarget,
    async () => {
      const result = await evaluateDueAdviceReviews(state);
      const options = currentWorkbenchMutationOptions();
      if (options) await loadAdviceReviews(state, options);
      return result;
    },
    { onError: (error) => setInlineFeedback("reviewDashboardFeedback", error) }
  );
}

function handleReviewDashboardQueueClick(context, event) {
  const { setActiveSymbol, loadAll } = context;
  const button = event.target.closest("button[data-review-open-symbol]");
  if (!button) return;
  setActiveSymbol(button.dataset.reviewOpenSymbol);
  void loadAll({ reveal: true });
}

function handleReviewPlanCancelClick(context, event) {
  const { state } = context;
  cancelAdviceReviewEdit(state);
}

async function handleReviewPlanLoadMoreClick(context, event) {
  const { state, currentWorkbenchMutationOptions, runButtonTask, setInlineFeedback } = context;
  const options = currentWorkbenchMutationOptions();
  if (!options) return;
  await runButtonTask(
    event.currentTarget,
    () => loadMoreAdviceReviews(state, options),
    { isCurrent: options.isCurrent, onError: (error) => setInlineFeedback("reviewPlanFeedback", error) }
  );
}

async function handleReviewPlanFormSubmit(context, event) {
  const { state, $, currentWorkbenchMutationOptions, setInlineFeedback, runSubmitTask } = context;
  event.preventDefault();
  const options = currentWorkbenchMutationOptions();
  if (!options) return;
  const feedback = $("reviewPlanFeedback");
  if (feedback) feedback.hidden = true;
  try {
    await runSubmitTask(event.currentTarget, state.adviceReviewEditingPlanId ? "更新中" : "建立中", async () => {
      const saved = await submitAdviceReviewPlan(state, options);
      await loadAdviceReviewDashboard(state, options);
      syncPaperTradingPlans(state);
      return saved;
    });
  } catch (error) {
    if (!isAbortError(error) && options.isCurrent()) setInlineFeedback("reviewPlanFeedback", error);
  }
}

async function handleReviewPlanListClick(context, event) {
  const { state, currentWorkbenchMutationOptions, runButtonTask, setInlineFeedback, setWorkspaceView } = context;
  const paperButton = event.target.closest("button[data-paper-from-review]");
  if (paperButton) {
    setWorkspaceView("paper");
    selectPaperTradingPlan(state, paperButton.dataset.paperFromReview);
    return;
  }
  const editButton = event.target.closest("button[data-review-edit]");
  if (editButton) {
    beginAdviceReviewEdit(state, editButton.dataset.reviewEdit);
    return;
  }
  const deleteButton = event.target.closest("button[data-review-delete]");
  if (deleteButton) return archiveReviewPlan(context, deleteButton);
  const historyRetryButton = event.target.closest("button[data-review-history-retry]");
  if (historyRetryButton) {
    const options = currentWorkbenchMutationOptions();
    if (options) await retryAdviceReviewHistory(state, historyRetryButton.dataset.reviewHistoryRetry, options);
    return;
  }
  const historyButton = event.target.closest("button[data-review-history]");
  if (historyButton) {
    const options = currentWorkbenchMutationOptions();
    if (options) await toggleAdviceReviewHistory(state, historyButton.dataset.reviewHistory, options);
    return;
  }
  const evaluateButton = event.target.closest("button[data-review-evaluate]");
  if (!evaluateButton) return;
  const options = currentWorkbenchMutationOptions();
  if (!options) return;
  await runButtonTask(
    evaluateButton,
    async () => {
      const evaluated = await evaluateAdviceReviewPlan(state, evaluateButton.dataset.reviewEvaluate, options);
      if (evaluated) await loadAdviceReviewDashboard(state, options);
      return evaluated;
    },
    { isCurrent: options.isCurrent, onError: (error) => setInlineFeedback("reviewPlanFeedback", error) }
  );
}

async function archiveReviewPlan(context, deleteButton) {
  const { state, currentWorkbenchMutationOptions, runButtonTask, setInlineFeedback } = context;
  const options = currentWorkbenchMutationOptions();
  if (!options) return;
  await runButtonTask(
    deleteButton,
    async () => {
      const removed = await deleteAdviceReviewPlan(state, deleteButton.dataset.reviewDelete, {
        ...options,
        confirm: (message) => context.confirm(message),
      });
      if (removed) {
        await loadAdviceReviewDashboard(state, options);
        syncPaperTradingPlans(state);
      }
      return removed;
    },
    { isCurrent: options.isCurrent, onError: (error) => setInlineFeedback("reviewPlanFeedback", error) }
  );
}

function handleReviewPlanListChange(context, event) {
  const { state } = context;
  const input = event.target.closest("input[data-review-as-of]");
  if (!input) return;
  setAdviceReviewEvaluationAsOf(state, input.dataset.reviewAsOf, input.value);
}

function handleDashboardFilter(context) {
  updateAdviceReviewDashboardFilters(context.state);
}
