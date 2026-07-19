export const WORKSPACE_PREFERENCES_VERSION = 1;
export const WORKSPACE_PREFERENCES_STORAGE_KEY = "ashare-radar.workspace-preferences";

export const WORKSPACE_PREFERENCE_OPTIONS = Object.freeze({
  workspaceView: Object.freeze(["overview", "market-scan", "qa", "strategy", "finance", "theme", "replay", "tools"]),
  dailyChartRange: Object.freeze([20, 60, 120, 240]),
  minuteChartInterval: Object.freeze(["5m", "15m", "30m", "60m"]),
  mobileChartView: Object.freeze(["daily", "minute"]),
});

export const DEFAULT_WORKSPACE_PREFERENCES = Object.freeze({
  workspaceView: "overview",
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
    if (!isRecord(payload) || payload.version !== WORKSPACE_PREFERENCES_VERSION) {
      return defaultPreferences();
    }
    if (!isRecord(payload.preferences)) return defaultPreferences();
    return sanitizeWorkspacePreferences(payload.preferences);
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
  return {
    workspaceView: allowed("workspaceView", value.workspaceView)
      ? value.workspaceView
      : DEFAULT_WORKSPACE_PREFERENCES.workspaceView,
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

function allowed(name, value) {
  return WORKSPACE_PREFERENCE_OPTIONS[name].includes(value);
}

function defaultPreferences() {
  return { ...DEFAULT_WORKSPACE_PREFERENCES };
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
