from __future__ import annotations

import subprocess
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]


def test_workspace_preferences_round_trip_uses_a_strict_allowlist() -> None:
    _run_node_script(
        r'''
import assert from "node:assert/strict";
import {
  WORKSPACE_PREFERENCES_STORAGE_KEY,
  WORKSPACE_PREFERENCES_VERSION,
  loadWorkspacePreferences,
  saveWorkspacePreferences,
} from "./static/js/workspace-preferences.js";

const values = new Map();
const storage = {
  getItem: (key) => values.get(key) ?? null,
  setItem: (key, value) => values.set(key, value),
};
const expected = {
  workspaceView: "strategy",
  dailyChartRange: 120,
  dailyChartMa5: false,
  dailyChartMa20: true,
  minuteChartInterval: "30m",
  mobileChartView: "minute",
};

assert.equal(saveWorkspacePreferences({
  ...expected,
  symbol: "600519.SH",
  stockData: { price: 1688.88 },
  apiToken: "do-not-persist-this-token",
}, storage), true);

const raw = values.get(WORKSPACE_PREFERENCES_STORAGE_KEY);
const payload = JSON.parse(raw);
assert.deepEqual(Object.keys(payload), ["version", "preferences"]);
assert.equal(payload.version, WORKSPACE_PREFERENCES_VERSION);
assert.deepEqual(Object.keys(payload.preferences).sort(), Object.keys(expected).sort());
assert.deepEqual(payload.preferences, expected);
assert.equal(raw.includes("600519"), false);
assert.equal(raw.includes("1688.88"), false);
assert.equal(raw.includes("do-not-persist-this-token"), false);
assert.deepEqual(loadWorkspacePreferences(storage), expected);
'''
    )


def test_workspace_preferences_recover_from_bad_storage_and_validate_each_value() -> None:
    _run_node_script(
        r'''
import assert from "node:assert/strict";
import {
  DEFAULT_WORKSPACE_PREFERENCES,
  WORKSPACE_PREFERENCES_VERSION,
  loadWorkspacePreferences,
  saveWorkspacePreferences,
} from "./static/js/workspace-preferences.js";

const defaults = { ...DEFAULT_WORKSPACE_PREFERENCES };
const malformedValues = [
  "{",
  "null",
  "[]",
  JSON.stringify({ version: WORKSPACE_PREFERENCES_VERSION + 1, preferences: defaults }),
  JSON.stringify({ version: String(WORKSPACE_PREFERENCES_VERSION), preferences: defaults }),
  JSON.stringify({ version: WORKSPACE_PREFERENCES_VERSION, preferences: [] }),
];

for (const raw of malformedValues) {
  const storage = { getItem: () => raw };
  assert.deepEqual(loadWorkspacePreferences(storage), defaults);
}

const partlyValid = {
  version: WORKSPACE_PREFERENCES_VERSION,
  preferences: {
    workspaceView: "finance",
    dailyChartRange: "120",
    dailyChartMa5: false,
    dailyChartMa20: "false",
    minuteChartInterval: "1m",
    mobileChartView: "minute",
    symbol: "000001.SZ",
  },
};
assert.deepEqual(loadWorkspacePreferences({
  getItem: () => JSON.stringify(partlyValid),
}), {
  workspaceView: "finance",
  dailyChartRange: 60,
  dailyChartMa5: false,
  dailyChartMa20: true,
  minuteChartInterval: "5m",
  mobileChartView: "minute",
});

assert.deepEqual(loadWorkspacePreferences({
  getItem() { throw new Error("storage denied"); },
}), defaults);
assert.equal(saveWorkspacePreferences(defaults, {
  setItem() { throw new Error("storage full"); },
}), false);
assert.equal(saveWorkspacePreferences(defaults, null), false);
'''
    )


def test_app_restores_and_persists_preferences_through_existing_setters() -> None:
    _run_node_script(
        r'''
import assert from "node:assert/strict";
import { installAppDom } from "./tests/frontend_app_flow_helpers.mjs";
import {
  WORKSPACE_PREFERENCES_STORAGE_KEY,
  WORKSPACE_PREFERENCES_VERSION,
} from "./static/js/workspace-preferences.js";

const { element } = installAppDom({ canvasContext: null });
const views = ["overview", "qa", "strategy", "finance", "theme", "replay", "tools"];
const tabs = views.map((view) => control(`workspace-tab-${view}`, "view", view));
const panels = views.map((view) => control(`workspace-panel-${view}`, "viewPanel", view));
const dailyRanges = [20, 60, 120, 240].map((range) => control(`daily-${range}`, "dailyRange", String(range)));
const minuteIntervals = ["5m", "15m", "30m", "60m"].map((interval) => control(`minute-${interval}`, "minuteInterval", interval));
const mobileViews = ["daily", "minute"].map((view) => control(`mobile-${view}`, "chartView", view));

const selectorResults = new Map([
  [".workspace-tabs button[data-view]", tabs],
  [".workspace-view[data-view-panel]", panels],
  ["button[data-daily-range]", dailyRanges],
  ["button[data-minute-interval]", minuteIntervals],
  ["button[data-chart-view]", mobileViews],
]);
globalThis.document.querySelectorAll = (selector) => selectorResults.get(selector) || [];

const restored = {
  workspaceView: "strategy",
  dailyChartRange: 120,
  dailyChartMa5: false,
  dailyChartMa20: true,
  minuteChartInterval: "30m",
  mobileChartView: "minute",
};
const values = new Map([[
  WORKSPACE_PREFERENCES_STORAGE_KEY,
  JSON.stringify({ version: WORKSPACE_PREFERENCES_VERSION, preferences: restored }),
]]);
const writes = [];
globalThis.localStorage = {
  getItem: (key) => values.get(key) ?? null,
  setItem(key, value) {
    writes.push([key, value]);
    values.set(key, value);
  },
};
let cleanupPreviewCalls = 0;
globalThis.fetch = async (url) => {
  if (String(url) !== "/api/local-data/cleanup-preview") throw new Error(`unexpected request: ${url}`);
  cleanupPreviewCalls += 1;
  return { ok: true, async json() { return { total_rows: 0, user_history_rows: 0, tables: {} }; } };
};

const { __appTest } = await import("./static/app.js");
assert.deepEqual(__appTest.currentWorkspacePreferences(), restored);
assert.equal(__appTest.state.workspaceView, "strategy");
assert.equal(tabs.find((tab) => tab.dataset.view === "strategy").classList.contains("active"), true);
assert.equal(panels.find((panel) => panel.dataset.viewPanel === "strategy").hidden, false);
assert.equal(element("dailyMa5Toggle").checked, false);
assert.equal(element("dailyMa20Toggle").checked, true);
assert.equal(element("chartWorkspace").dataset.mobileChart, "minute");
assert.equal(writes.length, 0, "restoration should not rewrite storage once per setter");

__appTest.state.symbol = "600519.SH";
__appTest.state.privateSessionToken = "never-store-state-wholesale";
__appTest.setWorkspaceView("tools");
await Promise.resolve();
await Promise.resolve();
assert.equal(cleanupPreviewCalls, 1, "entering the tools view did not load its cleanup preview");
__appTest.selectDailyChartRange(240);
__appTest.setDailyChartOverlay("ma5", true);
__appTest.setDailyChartOverlay("ma20", false);
await __appTest.selectMinuteChartInterval("60m");
__appTest.setMobileChartView("daily");

const payload = JSON.parse(values.get(WORKSPACE_PREFERENCES_STORAGE_KEY));
assert.deepEqual(payload, {
  version: WORKSPACE_PREFERENCES_VERSION,
  preferences: {
    workspaceView: "tools",
    dailyChartRange: 240,
    dailyChartMa5: true,
    dailyChartMa20: false,
    minuteChartInterval: "60m",
    mobileChartView: "daily",
  },
});
const finalRaw = values.get(WORKSPACE_PREFERENCES_STORAGE_KEY);
assert.equal(finalRaw.includes("600519"), false);
assert.equal(finalRaw.includes("never-store-state-wholesale"), false);

function control(id, dataName, dataValue) {
  const item = element(id);
  item.dataset[dataName] = dataValue;
  return item;
}
'''
    )


def _run_node_script(script: str) -> None:
    subprocess.run(
        ["node", "--input-type=module", "-e", script],
        cwd=ROOT,
        check=True,
        text=True,
        capture_output=True,
    )
