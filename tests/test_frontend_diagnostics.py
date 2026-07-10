from __future__ import annotations

import subprocess
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]


def test_monitoring_loads_ignore_stale_responses() -> None:
    script = r'''
      import { loadMonitoring } from "./static/js/diagnostics.js";

      const { element } = installDom();
      const state = { monitorTimer: null };
      let statusCalls = 0;
      let resolveFirstStatus;
      globalThis.fetch = async (url) => {
        const target = String(url);
        if (target === "/api/tasks/status") {
          statusCalls += 1;
          if (statusCalls === 1) {
            return {
              ok: true,
              json() {
                return new Promise((resolve) => {
                  resolveFirstStatus = () => resolve({ enabled: false, running: false, tasks: [] });
                });
              },
            };
          }
          return jsonResponse({ enabled: true, running: true, tasks: [] });
        }
        if (target === "/api/tasks/runs?limit=8") return jsonResponse([]);
        if (target === "/api/monitor/events?limit=8") return jsonResponse([]);
        throw new Error(`unexpected monitoring URL ${target}`);
      };

      const staleLoad = loadMonitoring(state);
      await waitFor(() => typeof resolveFirstStatus === "function", "first status resolver");
      await loadMonitoring(state);
      if (element("schedulerState").textContent !== "运行中") {
        throw new Error(`newer monitoring response did not render: ${element("schedulerState").textContent}`);
      }
      resolveFirstStatus();
      await staleLoad;
      if (element("schedulerState").textContent !== "运行中") {
        throw new Error(`stale monitoring response overwrote current state: ${element("schedulerState").textContent}`);
      }
    '''
    _run_node_script(script)


def test_failed_monitor_task_keeps_error_without_refreshing_scheduler_panel() -> None:
    script = r'''
      import { runMonitorTask } from "./static/js/diagnostics.js";

      const { element } = installDom();
      const button = { disabled: false };
      const state = { monitorTimer: null };
      const calls = [];
      globalThis.document.querySelectorAll = () => [button];
      globalThis.fetch = async (url, options = {}) => {
        const target = String(url);
        calls.push(target);
        if (target.startsWith("/api/tasks/run-once")) {
          return { ok: false, status: 503, async json() { return { detail: "任务执行失败" }; } };
        }
        if (target === "/api/data/status") {
          return jsonResponse({
            source_plan: {},
            providers: [],
            cache: {},
            capabilities: [],
            capability_statuses: [],
          });
        }
        throw new Error(`failed task should not refresh scheduler panel via ${target}`);
      };

      const result = await runMonitorTask(state, "refresh_quotes");
      if (result !== false || element("schedulerState").textContent !== "任务执行失败") {
        throw new Error(`failed task error was not preserved: ${element("schedulerState").textContent}`);
      }
      if (button.disabled) {
        throw new Error("monitor task button did not recover after failure");
      }
      if (calls.some((url) => url === "/api/tasks/status" || url === "/api/tasks/runs?limit=8" || url === "/api/monitor/events?limit=8")) {
        throw new Error(`failed task refreshed monitoring panel and hid the error: ${calls.join(",")}`);
      }
    '''
    _run_node_script(script)


def _run_node_script(script: str) -> None:
    subprocess.run(["node", "--input-type=module", "-e", f"{_HELPERS}\n{script}"], cwd=ROOT, check=True)


_HELPERS = r'''
function installDom() {
  const elements = new Map();
  function element(id) {
    if (!elements.has(id)) {
      elements.set(id, {
        id,
        innerHTML: "",
        textContent: "",
      });
    }
    return elements.get(id);
  }
  let timerId = 0;
  globalThis.document = {
    hidden: false,
    getElementById: element,
    querySelectorAll() {
      return [];
    },
  };
  globalThis.setInterval = () => {
    timerId += 1;
    return timerId;
  };
  globalThis.clearInterval = () => {};
  return { element, elements };
}

function jsonResponse(payload) {
  return {
    ok: true,
    async json() {
      return payload;
    },
  };
}

async function waitFor(condition, label) {
  for (let index = 0; index < 20; index += 1) {
    if (condition()) return;
    await Promise.resolve();
  }
  throw new Error(`timed out waiting for ${label}`);
}
'''
