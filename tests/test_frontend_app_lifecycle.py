from __future__ import annotations

import subprocess
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]


def test_app_lifecycle_coordinates_visibility_online_pagehide_and_dispose() -> None:
    script = r'''
      import assert from "node:assert/strict";
      import {
        createAppLifecycleController,
        workbenchNeedsOnlineRecovery,
      } from "./static/js/app-lifecycle.js";

      class Target {
        constructor() { this.listeners = new Map(); this.hidden = false; }
        addEventListener(type, handler) {
          if (!this.listeners.has(type)) this.listeners.set(type, new Set());
          this.listeners.get(type).add(handler);
        }
        removeEventListener(type, handler) { this.listeners.get(type)?.delete(handler); }
        dispatch(type, event = { type }) {
          for (const handler of this.listeners.get(type) || []) handler(event);
        }
        count(type) { return this.listeners.get(type)?.size || 0; }
      }

      const documentTarget = new Target();
      const windowTarget = new Target();
      const calls = {
        visible: [], refresh: [], load: [], invalidated: 0, symbols: [], stopped: 0,
        reconciled: 0, monitoringCancelled: 0, dataStatusCancelled: 0, probabilityCancelled: 0,
        intervals: [], pagehide: [],
      };
      const state = {
        coreStatus: { phase: "loading" }, auxiliaryStatus: { failures: {} },
        pendingLoad: { id: 7 }, failedLoadSymbol: "600519.SH", onlineRecoveryPromise: null,
        visibilityRefreshSources: new Set(), monitorTimer: 41, monitorRequest: {},
        dataStatusRequest: {}, lastAnalysis: { symbol: "600519.SH" },
      };
      let finishLoad;
      const loadPromise = new Promise((resolve) => { finishLoad = resolve; });
      const controller = createAppLifecycleController({
        state, documentTarget, windowTarget,
        marketScanController: { setVisible: (value) => calls.visible.push(value) },
        refreshGlobalPanels: (options) => { calls.refresh.push(options); return { status: Promise.resolve(true) }; },
        loadAll: (options) => { calls.load.push(options); return loadPromise; },
        invalidateActiveLoad: () => { calls.invalidated += 1; },
        setActiveSymbol: (symbol) => calls.symbols.push(symbol),
        stopStream: () => { calls.stopped += 1; },
        reconcileStreamSubscription: () => { calls.reconciled += 1; },
        cancelMonitoringRefresh: () => { calls.monitoringCancelled += 1; },
        cancelDataStatusRefresh: () => { calls.dataStatusCancelled += 1; },
        cancelIndividualProbability: () => { calls.probabilityCancelled += 1; },
        clearInterval: (timer) => calls.intervals.push(timer),
        onPageHide: (event) => calls.pagehide.push(event),
      });

      assert.equal(documentTarget.count("visibilitychange"), 1);
      assert.equal(windowTarget.count("online"), 1);
      assert.equal(windowTarget.count("pagehide"), 1);
      assert.equal(workbenchNeedsOnlineRecovery(state), true);

      documentTarget.hidden = true;
      documentTarget.dispatch("visibilitychange");
      assert.deepEqual(calls.visible, [false]);
      assert.deepEqual(calls.intervals, [41]);
      assert.equal(state.monitorTimer, null);
      assert.equal(calls.monitoringCancelled, 1);
      assert.equal(calls.dataStatusCancelled, 1);
      assert.equal(state.visibilityRefreshSources.has("monitoring"), true);
      assert.equal(state.visibilityRefreshSources.has("data-status"), true);
      assert.equal(calls.stopped, 1);

      documentTarget.hidden = false;
      documentTarget.dispatch("visibilitychange");
      assert.deepEqual(calls.visible, [false, true]);
      assert.deepEqual(calls.refresh, [{ force: true }]);
      assert.equal(calls.reconciled, 1);

      assert.equal(controller.handleOnline(), true);
      assert.equal(controller.handleOnline(), false, "concurrent online recovery was not deduplicated");
      assert.equal(calls.invalidated, 1);
      assert.deepEqual(calls.symbols, ["600519.SH"]);
      assert.deepEqual(calls.load, [{ forceGlobal: true, waitForGlobal: true }]);
      const recovery = state.onlineRecoveryPromise;
      assert.ok(recovery instanceof Promise);
      finishLoad(true);
      await recovery;
      assert.equal(state.onlineRecoveryPromise, null);

      const pagehide = { type: "pagehide", persisted: true };
      windowTarget.dispatch("pagehide", pagehide);
      assert.deepEqual(calls.pagehide, [pagehide]);
      assert.equal(calls.probabilityCancelled, 1);

      controller.dispose();
      controller.dispose();
      assert.equal(documentTarget.count("visibilitychange"), 0);
      assert.equal(windowTarget.count("online"), 0);
      assert.equal(windowTarget.count("pagehide"), 0);
      const stoppedBeforeDispatch = calls.stopped;
      documentTarget.hidden = true;
      documentTarget.dispatch("visibilitychange");
      windowTarget.dispatch("pagehide", { persisted: false });
      assert.equal(calls.stopped, stoppedBeforeDispatch);
      assert.equal(calls.pagehide.length, 1);
      assert.equal(calls.probabilityCancelled, 1);
      assert.equal(controller.handleOnline(), false);

      Object.assign(state, {
        coreStatus: { phase: "ready" }, pendingLoad: null, failedLoadSymbol: "",
        auxiliaryStatus: { failures: {} }, visibilityRefreshSources: new Set(),
      });
      assert.equal(workbenchNeedsOnlineRecovery(state), false);
    '''
    subprocess.run(
        ["node", "--input-type=module", "--eval", script],
        cwd=ROOT,
        check=True,
        capture_output=True,
        text=True,
    )
