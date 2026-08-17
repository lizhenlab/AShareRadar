const REQUIRED_EFFECTS = Object.freeze([
  "refreshGlobalPanels",
  "loadAll",
  "invalidateActiveLoad",
  "setActiveSymbol",
  "stopStream",
  "reconcileStreamSubscription",
  "cancelMonitoringRefresh",
  "cancelDataStatusRefresh",
  "cancelIndividualProbability",
  "onPageHide",
]);

export function createAppLifecycleController(options = {}) {
  const settings = lifecycleSettings(options);
  let disposed = false;
  const handlers = {
    online: () => (disposed ? false : recoverWorkbenchOnline(settings)),
    visibilitychange: () => { if (!disposed) syncPageVisibility(settings); },
    pagehide: (event) => {
      if (disposed) return;
      settings.cancelIndividualProbability();
      settings.onPageHide(event);
    },
  };
  const removeListeners = bindLifecycleEvents(settings, handlers);
  return {
    needsOnlineRecovery: () => !disposed && workbenchNeedsOnlineRecovery(settings.state),
    handleOnline: handlers.online,
    handleVisibilityChange: handlers.visibilitychange,
    handlePageHide: handlers.pagehide,
    dispose() {
      if (disposed) return;
      disposed = true;
      removeListeners();
    },
  };
}

export function workbenchNeedsOnlineRecovery(state) {
  const phase = state.coreStatus?.phase || "idle";
  const failures = state.auxiliaryStatus?.failures || {};
  return Boolean(
    state.pendingLoad
    || state.failedLoadSymbol
    || phase === "loading"
    || phase === "error"
    || state.visibilityRefreshSources.size
    || Object.keys(failures).length
  );
}

function lifecycleSettings(options) {
  if (!options.state || typeof options.state !== "object") throw new TypeError("state must be an object");
  for (const name of REQUIRED_EFFECTS) {
    if (typeof options[name] !== "function") throw new TypeError(`${name} must be a function`);
  }
  return {
    ...options,
    documentTarget: options.documentTarget || globalThis.document,
    windowTarget: options.windowTarget || globalThis.window,
    clearInterval: options.clearInterval || globalThis.clearInterval,
  };
}

function bindLifecycleEvents(settings, handlers) {
  settings.documentTarget?.addEventListener?.("visibilitychange", handlers.visibilitychange);
  settings.windowTarget?.addEventListener?.("online", handlers.online);
  settings.windowTarget?.addEventListener?.("pagehide", handlers.pagehide);
  return () => {
    settings.documentTarget?.removeEventListener?.("visibilitychange", handlers.visibilitychange);
    settings.windowTarget?.removeEventListener?.("online", handlers.online);
    settings.windowTarget?.removeEventListener?.("pagehide", handlers.pagehide);
  };
}

function recoverWorkbenchOnline(settings) {
  const { state } = settings;
  if (settings.documentTarget.hidden || state.onlineRecoveryPromise || !workbenchNeedsOnlineRecovery(state)) return false;
  const recoverCore = Boolean(state.pendingLoad || state.failedLoadSymbol)
    || ["loading", "error"].includes(state.coreStatus?.phase)
    || Object.keys(state.auxiliaryStatus?.failures || {}).length > 0;
  if (state.pendingLoad) settings.invalidateActiveLoad();
  if (state.failedLoadSymbol) settings.setActiveSymbol(state.failedLoadSymbol);
  const task = recoverCore
    ? settings.loadAll({ forceGlobal: true, waitForGlobal: true })
    : Promise.allSettled(Object.values(settings.refreshGlobalPanels({ force: true })));
  const recovery = Promise.resolve(task).finally(() => {
    if (state.onlineRecoveryPromise === recovery) state.onlineRecoveryPromise = null;
  });
  state.onlineRecoveryPromise = recovery;
  return true;
}

function syncPageVisibility(settings) {
  const { state } = settings;
  if (settings.documentTarget.hidden) {
    settings.marketScanController.setVisible(false);
    if (state.monitorTimer) {
      settings.clearInterval(state.monitorTimer);
      state.monitorTimer = null;
    }
    if (state.monitorRequest) {
      state.visibilityRefreshSources.add("monitoring");
      settings.cancelMonitoringRefresh(state);
    }
    if (state.dataStatusRequest) {
      state.visibilityRefreshSources.add("data-status");
      settings.cancelDataStatusRefresh(state);
    }
    settings.stopStream();
    return;
  }
  settings.marketScanController.setVisible(true);
  settings.refreshGlobalPanels({ force: true });
  if (state.lastAnalysis) settings.reconcileStreamSubscription();
}
