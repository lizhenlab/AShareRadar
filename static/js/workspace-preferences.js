export const WORKSPACE_PREFERENCES_VERSION = 2;
export const WORKSPACE_PREFERENCES_STORAGE_KEY = "ashare-radar.workspace-preferences";

export const PRIMARY_VIEW_OPTIONS = Object.freeze(["research", "market", "review", "monitor", "system"]);
export const PRIMARY_WORKSPACE_VIEWS = Object.freeze({
  research: Object.freeze(["overview", "qa", "strategy", "finance", "theme", "tools"]),
  market: Object.freeze(["market-scan"]),
  review: Object.freeze(["replay", "paper"]),
  monitor: Object.freeze([]),
  system: Object.freeze(["diagnostics", "data"]),
});
export const DEFAULT_WORKSPACE_VIEW_BY_PRIMARY = Object.freeze({
  research: "overview",
  market: "market-scan",
  review: "replay",
  monitor: null,
  system: "diagnostics",
});

export const WORKSPACE_PREFERENCE_OPTIONS = Object.freeze({
  workspaceView: Object.freeze(["overview", "market-scan", "qa", "strategy", "finance", "theme", "replay", "paper", "tools", "diagnostics", "data"]),
  dailyChartRange: Object.freeze([20, 60, 120, 240]),
  minuteChartInterval: Object.freeze(["5m", "15m", "30m", "60m"]),
  mobileChartView: Object.freeze(["daily", "minute"]),
});

export const DEFAULT_WORKSPACE_PREFERENCES = Object.freeze({
  primaryView: "research",
  workspaceView: "overview",
  workspaceByPrimary: DEFAULT_WORKSPACE_VIEW_BY_PRIMARY,
  dailyChartRange: 60,
  dailyChartMa5: true,
  dailyChartMa20: true,
  minuteChartInterval: "5m",
  mobileChartView: "daily",
});

export function loadWorkspacePreferences(storage = browserStorage()) {
  if (!storage || typeof storage.getItem !== "function") return defaultPreferences();
  try {
    const payload = JSON.parse(storage.getItem(WORKSPACE_PREFERENCES_STORAGE_KEY));
    if (!isRecord(payload) || ![1, WORKSPACE_PREFERENCES_VERSION].includes(payload.version)) {
      return defaultPreferences();
    }
    if (!isRecord(payload.preferences)) return defaultPreferences();
    const preferences = { ...payload.preferences };
    if (payload.version === 1 && preferences.primaryView === "review"
      && ["tools", "data"].includes(preferences.workspaceView)) {
      preferences.primaryView = primaryViewForWorkspace(preferences.workspaceView);
    }
    return sanitizeWorkspacePreferences(preferences);
  } catch (error) {
    return defaultPreferences();
  }
}

export function saveWorkspacePreferences(preferences, storage = browserStorage()) {
  if (!storage || typeof storage.setItem !== "function") return false;
  const payload = {
    version: WORKSPACE_PREFERENCES_VERSION,
    preferences: sanitizeWorkspacePreferences(preferences),
  };
  try {
    storage.setItem(WORKSPACE_PREFERENCES_STORAGE_KEY, JSON.stringify(payload));
    return true;
  } catch (error) {
    return false;
  }
}

export function sanitizeWorkspacePreferences(candidate) {
  const value = isRecord(candidate) ? candidate : {};
  const previousWorkspace = allowed("workspaceView", value.workspaceView)
    ? value.workspaceView
    : DEFAULT_WORKSPACE_PREFERENCES.workspaceView;
  const primaryView = PRIMARY_VIEW_OPTIONS.includes(value.primaryView)
    ? value.primaryView : primaryViewForWorkspace(previousWorkspace);
  const workspaceByPrimary = sanitizeWorkspaceMemory(value.workspaceByPrimary);
  workspaceByPrimary[primaryViewForWorkspace(previousWorkspace)] = previousWorkspace;
  const workspaceView = primaryView === "monitor" ? previousWorkspace : workspaceByPrimary[primaryView];
  return {
    primaryView,
    workspaceView,
    workspaceByPrimary,
    dailyChartRange: allowed("dailyChartRange", value.dailyChartRange)
      ? value.dailyChartRange
      : DEFAULT_WORKSPACE_PREFERENCES.dailyChartRange,
    dailyChartMa5: typeof value.dailyChartMa5 === "boolean"
      ? value.dailyChartMa5
      : DEFAULT_WORKSPACE_PREFERENCES.dailyChartMa5,
    dailyChartMa20: typeof value.dailyChartMa20 === "boolean"
      ? value.dailyChartMa20
      : DEFAULT_WORKSPACE_PREFERENCES.dailyChartMa20,
    minuteChartInterval: allowed("minuteChartInterval", value.minuteChartInterval)
      ? value.minuteChartInterval
      : DEFAULT_WORKSPACE_PREFERENCES.minuteChartInterval,
    mobileChartView: allowed("mobileChartView", value.mobileChartView)
      ? value.mobileChartView
      : DEFAULT_WORKSPACE_PREFERENCES.mobileChartView,
  };
}

export function primaryViewForWorkspace(view) {
  return PRIMARY_VIEW_OPTIONS.find((primaryView) => PRIMARY_WORKSPACE_VIEWS[primaryView].includes(view))
    || DEFAULT_WORKSPACE_PREFERENCES.primaryView;
}

function allowed(name, value) {
  return WORKSPACE_PREFERENCE_OPTIONS[name].includes(value);
}

function defaultPreferences() {
  return { ...DEFAULT_WORKSPACE_PREFERENCES, workspaceByPrimary: { ...DEFAULT_WORKSPACE_VIEW_BY_PRIMARY } };
}

function sanitizeWorkspaceMemory(value) {
  const memory = isRecord(value) ? value : {};
  return Object.fromEntries(PRIMARY_VIEW_OPTIONS.map((primaryView) => [
    primaryView,
    PRIMARY_WORKSPACE_VIEWS[primaryView].includes(memory[primaryView])
      ? memory[primaryView] : DEFAULT_WORKSPACE_VIEW_BY_PRIMARY[primaryView],
  ]));
}

function browserStorage() {
  try {
    return globalThis.localStorage || null;
  } catch (error) {
    return null;
  }
}

function isRecord(value) {
  return Boolean(value && typeof value === "object" && !Array.isArray(value));
}
