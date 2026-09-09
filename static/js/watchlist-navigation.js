import { isAbortError } from "./api.js";
import { compactErrorMessage } from "./errors.js";
import { appendWatchlistMessage, markWatchlistItemViewed } from "./watchlist.js";

export async function navigateWatchlistResearch(context, symbol, { changes = false } = {}) {
  const { state } = context;
  context.clearWatchlistFeedback();
  context.setActiveSymbol(symbol);
  context.setWorkspaceView(changes ? "tools" : "overview");
  const loading = context.loadAll({ reveal: !changes, waitForAdviceTimeline: changes });
  const owner = { symbol: state.symbol, loadSeq: state.loadSeq };
  try {
    const loaded = await loading;
    if (!loaded || !ownsNavigation(state, owner)) return false;
    if (!changes) return true;
    const timeline = visibleTimeline(context);
    const watermark = state.adviceTimelineWatermark;
    if (!timeline || !watermark || watermark.symbol !== owner.symbol || watermark.loadSeq !== owner.loadSeq) {
      preserveUnreadStatus(context);
      return false;
    }
    timeline.setAttribute("tabindex", "-1");
    timeline.focus?.({ preventScroll: true });
    timeline.scrollIntoView?.({ block: "start", behavior: "auto" });
    const options = context.currentWatchlistMutationOptions();
    if (!options || options.symbol !== owner.symbol || options.isCurrent?.() === false) {
      preserveUnreadStatus(context);
      return false;
    }
    return await markWatchlistItemViewed(state, owner.symbol, {
      ...options, viewedThroughAdviceId: watermark.adviceId,
    });
  } catch (error) {
    if (isAbortError(error) || !ownsNavigation(state, owner)) return false;
    const detail = compactErrorMessage(error.message);
    appendWatchlistMessage("建议变化已打开，未读状态同步失败", detail);
    context.setWatchlistFeedback(`建议变化已打开；未读状态未清除：${detail}`, "warn");
    context.setMutationStatus("degraded", "自选股未读状态同步失败", "warn");
    return false;
  }
}

function ownsNavigation(state, owner) {
  return state.symbol === owner.symbol && state.loadSeq === owner.loadSeq;
}

function visibleTimeline(context) {
  const root = context.root || globalThis.document;
  const timeline = root?.getElementById?.("adviceTimeline");
  if (root?.hidden || context.state.workspaceView !== "tools" || !timeline?.getClientRects?.().length) return null;
  return timeline;
}

function preserveUnreadStatus(context) {
  appendWatchlistMessage("未读状态保持", "建议变化尚未完整展示，请重新点击查看新变化。");
  context.setWatchlistFeedback("建议变化尚未完整展示，未读状态保持；请重新点击查看新变化。", "warn");
  context.setMutationStatus("degraded", "建议变化未完整展示，未读状态保持", "warn");
}
