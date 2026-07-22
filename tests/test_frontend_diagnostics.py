from __future__ import annotations

import subprocess
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]


def test_monitoring_loads_share_inflight_request_without_aborting_it() -> None:
    script = r'''
      import { loadMonitoring } from "./static/js/diagnostics.js";

      const { element } = installDom();
      const state = { monitorTimer: null };
      let statusCalls = 0;
      let resolveFirstStatus;
      let statusSignal;
      globalThis.fetch = async (url, options = {}) => {
        const target = String(url);
        if (target === "/api/tasks/status") {
          statusCalls += 1;
          statusSignal = options.signal;
          if (statusCalls === 1) {
            return {
              ok: true,
              json() {
                return new Promise((resolve) => {
                  resolveFirstStatus = () => resolve({ enabled: true, running: true, tasks: [] });
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
      const joinedLoad = loadMonitoring(state);
      if (statusCalls !== 1 || statusSignal.aborted) throw new Error("monitoring inflight request was duplicated or aborted");
      resolveFirstStatus();
      const joinedResult = await joinedLoad;
      await staleLoad;
      if (!joinedResult || element("schedulerState").textContent !== "运行中") {
        throw new Error(`shared monitoring response did not render: ${element("schedulerState").textContent}`);
      }
    '''
    _run_node_script(script)


def test_monitoring_partial_failures_are_reported_independently() -> None:
    script = r'''
      import { clearCachedJsonRequests } from "./static/js/api.js";
      import { loadMonitoring } from "./static/js/diagnostics.js";

      const statusPayload = {
        enabled: true,
        running: true,
        tasks: [{
          name: "refresh_quotes",
          display_name: "刷新报价",
          running: false,
          last_status: "success",
          last_message: "调度状态仍可用",
          next_run_at: null,
        }],
      };
      const runsPayload = [{ task_name: "refresh_quotes", status: "success", message: "运行记录仍可用" }];
      const eventsPayload = [{
        category: "health",
        level: "info",
        message: "健康检查仍可用",
        created_at: "2026-07-15 10:00:00",
      }];
      const scenarios = [
        { url: "/api/tasks/status", panel: "taskCards", marker: "监控暂不可用" },
        { url: "/api/tasks/runs?limit=8", panel: "taskCards", marker: "运行记录读取失败" },
        { url: "/api/monitor/events?limit=8", panel: "monitorEvents", marker: "事件读取失败" },
      ];

      for (const scenario of scenarios) {
        clearCachedJsonRequests();
        const { element } = installDom();
        const state = { monitorTimer: null };
        globalThis.fetch = async (url) => {
          const target = String(url);
          if (target === scenario.url) {
            return { ok: false, status: 503, async json() { return { detail: `${scenario.marker}：数据库忙` }; } };
          }
          if (target === "/api/tasks/status") return jsonResponse(statusPayload);
          if (target === "/api/tasks/runs?limit=8") return jsonResponse(runsPayload);
          if (target === "/api/monitor/events?limit=8") return jsonResponse(eventsPayload);
          throw new Error(`unexpected monitoring URL ${target}`);
        };

        const result = await loadMonitoring(state);
        if (result !== false) throw new Error(`${scenario.url} failure was reported as a successful monitoring load`);
        if (!element(scenario.panel).innerHTML.includes(scenario.marker)) {
          throw new Error(`${scenario.url} did not render its stable degradation: ${element(scenario.panel).innerHTML}`);
        }
        if (scenario.url !== "/api/tasks/status" && element("schedulerState").textContent !== "运行中") {
          throw new Error(`${scenario.url} failure discarded the successful task status`);
        }
        if (scenario.url !== "/api/monitor/events?limit=8" && !element("monitorEvents").innerHTML.includes("健康检查仍可用")) {
          throw new Error(`${scenario.url} failure discarded the successful monitoring events`);
        }
      }
    '''
    _run_node_script(script)


def test_market_scan_task_card_explains_automatic_schedule_state() -> None:
    script = r'''
      import { loadMonitoring } from "./static/js/diagnostics.js";

      const { element } = installDom();
      const state = { monitorTimer: null };
      globalThis.fetch = async (url) => {
        const target = String(url);
        if (target === "/api/tasks/status") {
          return jsonResponse({
            enabled: true,
            running: true,
            tasks: [
              {
                name: "full_market_scan",
                display_name: "全市场A股扫描",
                automatic_enabled: true,
                running: false,
                next_run_at: "2026-07-23 16:30:00",
              },
              {
                name: "full_market_scan_manual",
                display_name: "手动扫描入口",
                automatic_enabled: false,
                running: false,
                next_run_at: null,
              },
            ],
          });
        }
        if (target === "/api/tasks/runs?limit=8") return jsonResponse([]);
        if (target === "/api/monitor/events?limit=8") return jsonResponse([]);
        throw new Error(`unexpected URL: ${target}`);
      };

      await loadMonitoring(state);
      const html = element("taskCards").innerHTML;
      if (!html.includes("自动：已开启 · 下次：2026-07-23 16:30:00")) {
        throw new Error(`enabled automatic scan schedule was hidden: ${html}`);
      }
      if (!html.includes("自动：已关闭 · 可手动运行")) {
        throw new Error(`disabled automatic scan state was hidden: ${html}`);
      }
    '''
    _run_node_script(script)


def test_monitoring_renders_standby_and_degraded_shared_task_run() -> None:
    script = r'''
      import { loadMonitoring } from "./static/js/diagnostics.js";

      const { element } = installDom();
      const state = { monitorTimer: null };
      globalThis.fetch = async (url) => {
        const target = String(url);
        if (target === "/api/tasks/status") {
          return jsonResponse({
            enabled: true,
            running: false,
            standby: true,
            tasks: [{
              name: "refresh_plate_rank",
              display_name: "刷新行业",
              running: false,
              last_status: null,
              last_message: null,
              next_run_at: null,
            }],
          });
        }
        if (target === "/api/tasks/runs?limit=8") {
          return jsonResponse([{
            task_name: "refresh_plate_rank",
            status: "degraded",
            message: "行业背景数据源不可用，使用缓存 20 条",
          }]);
        }
        if (target === "/api/monitor/events?limit=8") return jsonResponse([]);
        throw new Error(`unexpected monitoring URL ${target}`);
      };

      if (!(await loadMonitoring(state))) throw new Error("monitoring did not load");
      if (element("schedulerState").textContent !== "其他实例运行中") {
        throw new Error(`standby state missing: ${element("schedulerState").textContent}`);
      }
      if (!element("taskCards").innerHTML.includes("降级") || !element("taskCards").innerHTML.includes("task-badge warn")) {
        throw new Error(`degraded task run missing: ${element("taskCards").innerHTML}`);
      }
    '''
    _run_node_script(script)


def test_monitoring_recovers_after_partial_runs_failure() -> None:
    script = r'''
      import { loadMonitoring } from "./static/js/diagnostics.js";

      const { element } = installDom();
      const state = { monitorTimer: null };
      let runsCalls = 0;
      globalThis.fetch = async (url) => {
        const target = String(url);
        if (target === "/api/tasks/status") {
          return jsonResponse({
            enabled: true,
            running: true,
            tasks: [{
              name: "refresh_quotes",
              display_name: "刷新报价",
              running: false,
              last_status: null,
              last_message: "",
              next_run_at: null,
            }],
          });
        }
        if (target === "/api/tasks/runs?limit=8") {
          runsCalls += 1;
          if (runsCalls === 1) {
            return { ok: false, status: 503, async json() { return { detail: "运行存储暂不可用" }; } };
          }
          return jsonResponse([{ task_name: "refresh_quotes", status: "success", message: "最近运行已恢复" }]);
        }
        if (target === "/api/monitor/events?limit=8") {
          return jsonResponse([{
            category: "health",
            level: "info",
            message: "事件服务正常",
            created_at: "2026-07-15 10:00:00",
          }]);
        }
        throw new Error(`unexpected monitoring URL ${target}`);
      };

      const degraded = await loadMonitoring(state);
      if (degraded !== false || !element("taskCards").innerHTML.includes("运行记录读取失败")) {
        throw new Error(`partial runs failure was not retained: ${element("taskCards").innerHTML}`);
      }
      if (element("schedulerState").textContent !== "运行中" || !element("monitorEvents").innerHTML.includes("事件服务正常")) {
        throw new Error("partial runs failure discarded successful monitoring panels");
      }

      const recovered = await loadMonitoring(state);
      if (recovered !== true) throw new Error("fully recovered monitoring load did not return true");
      if (element("taskCards").innerHTML.includes("运行记录读取失败") || element("taskCards").innerHTML.includes("运行存储暂不可用")) {
        throw new Error(`stale runs degradation survived recovery: ${element("taskCards").innerHTML}`);
      }
      if (!element("taskCards").innerHTML.includes("最近运行已恢复")) {
        throw new Error(`recovered runs did not render: ${element("taskCards").innerHTML}`);
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


def test_cancelled_monitor_task_clears_running_state_and_recovers_controls() -> None:
    script = r'''
      import { runMonitorTask } from "./static/js/diagnostics.js";

      const { element } = installDom();
      const button = { disabled: false };
      const state = { monitorTimer: null };
      const parent = new AbortController();
      let taskSignal;
      globalThis.document.querySelectorAll = () => [button];
      globalThis.fetch = (url, options = {}) => {
        if (!String(url).startsWith("/api/tasks/run-once")) throw new Error(`cancelled task unexpectedly fetched ${url}`);
        taskSignal = options.signal;
        return new Promise(() => {});
      };

      const task = runMonitorTask(state, "refresh_quotes", { signal: parent.signal });
      await waitFor(() => taskSignal, "monitor task signal");
      if (element("schedulerState").textContent !== "执行中" || !button.disabled) {
        throw new Error("monitor task did not enter the running state");
      }
      parent.abort();
      const result = await task;

      if (result !== false || !taskSignal.aborted) throw new Error("cancelled monitor request did not stop cleanly");
      if (element("schedulerState").textContent !== "已取消") {
        throw new Error(`cancelled monitor task remained running: ${element("schedulerState").textContent}`);
      }
      if (state.monitorTaskRunning || button.disabled) throw new Error("cancelled monitor task did not recover controls");
    '''
    _run_node_script(script)


def test_data_status_loads_share_inflight_request_without_aborting_it() -> None:
    script = r'''
      import { loadDataStatus } from "./static/js/diagnostics.js";

      const { element } = installDom();
      const state = {};
      let firstSignal;
      let resolveStatus;
      let calls = 0;
      globalThis.fetch = (url, options = {}) => {
        if (String(url) !== "/api/data/status") throw new Error(`unexpected status URL ${url}`);
        calls += 1;
        if (calls === 1) {
          firstSignal = options.signal;
          return new Promise((resolve) => {
            resolveStatus = () => resolve(jsonResponse({
              source_plan: {},
              providers: [{ name: "最新数据源", enabled: true, healthy: true, success_count: 1, failure_count: 0 }],
              cache: {},
              capabilities: [],
              capability_statuses: [],
            }));
          });
        }
        return Promise.resolve(jsonResponse({
          source_plan: {},
          providers: [{ name: "最新数据源", enabled: true, healthy: true, success_count: 1, failure_count: 0 }],
          cache: {},
          capabilities: [],
          capability_statuses: [],
        }));
      };

      const staleLoad = loadDataStatus(state);
      await waitFor(() => firstSignal, "first data status signal");
      const joinedLoad = loadDataStatus(state);
      if (calls !== 1 || firstSignal.aborted) throw new Error("data-status inflight request was duplicated or aborted");
      resolveStatus();
      await joinedLoad;
      await staleLoad;

      if (!element("providerStatus").innerHTML.includes("最新数据源")) {
        throw new Error(`latest data status did not render: ${element("providerStatus").innerHTML}`);
      }
      if (element("providerStatus").innerHTML.includes("请求已取消")) {
        throw new Error("AbortError leaked into the data status panel");
      }
    '''
    _run_node_script(script)


def test_forced_global_failures_keep_last_successful_diagnostics() -> None:
    script = r'''
      import { loadDataStatus, loadMonitoring } from "./static/js/diagnostics.js";

      const { element } = installDom();
      const state = { monitorTimer: null };
      let failing = false;
      globalThis.fetch = async (url) => {
        const target = String(url);
        if (failing) {
          return { ok: false, status: 503, async json() { return { detail: `${target} 暂不可用` }; } };
        }
        if (target === "/api/tasks/status") {
          return jsonResponse({
            enabled: true,
            running: true,
            tasks: [{
              name: "refresh_quotes",
              display_name: "保留的任务",
              running: false,
              last_status: "success",
              last_message: "上次监控成功",
              next_run_at: null,
            }],
          });
        }
        if (target === "/api/tasks/runs?limit=8") return jsonResponse([]);
        if (target === "/api/monitor/events?limit=8") {
          return jsonResponse([{ category: "health", level: "info", message: "保留的事件", created_at: "2026-07-15" }]);
        }
        if (target === "/api/data/status") {
          return jsonResponse({
            source_plan: {},
            providers: [{ name: "保留的数据源", enabled: true, healthy: true, success_count: 1, failure_count: 0 }],
            cache: {},
            capabilities: [],
            capability_statuses: [],
          });
        }
        throw new Error(`unexpected URL ${target}`);
      };

      if (!(await loadMonitoring(state)) || !(await loadDataStatus(state))) throw new Error("initial diagnostics load failed");
      failing = true;
      if (await loadMonitoring(state, { force: true })) throw new Error("failed monitoring refresh reported success");
      if (await loadDataStatus(state, { force: true })) throw new Error("failed data-status refresh reported success");

      if (!element("taskCards").innerHTML.includes("保留的任务") || !element("monitorEvents").innerHTML.includes("保留的事件")) {
        throw new Error("monitoring failure discarded the last successful values");
      }
      if (!element("providerStatus").innerHTML.includes("保留的数据源") || element("providerStatus").innerHTML.includes("状态读取失败")) {
        throw new Error("data-status failure replaced the last successful value");
      }
    '''
    _run_node_script(script)


def test_monitoring_refresh_and_explicit_task_keep_one_timer() -> None:
    script = r'''
      import { MONITORING_REFRESH_INTERVAL_MS, loadMonitoring, runMonitorTask } from "./static/js/diagnostics.js";

      installDom();
      const intervals = [];
      globalThis.setInterval = (callback, delay) => {
        intervals.push({ callback, delay });
        return intervals.length;
      };
      const state = { monitorTimer: null };
      globalThis.fetch = async (url, options = {}) => {
        const target = String(url);
        if (target.startsWith("/api/tasks/run-once") && options.method === "POST") return jsonResponse({ ok: true });
        if (target === "/api/tasks/status") return jsonResponse({ enabled: true, running: true, tasks: [] });
        if (target === "/api/tasks/runs?limit=8" || target === "/api/monitor/events?limit=8") return jsonResponse([]);
        if (target === "/api/data/status") {
          return jsonResponse({ source_plan: {}, providers: [], cache: {}, capabilities: [], capability_statuses: [] });
        }
        throw new Error(`unexpected URL ${target}`);
      };

      await loadMonitoring(state, { force: true });
      await loadMonitoring(state);
      const taskResult = await runMonitorTask(state, "refresh_quotes");
      if (!taskResult) throw new Error("explicit monitoring task failed");
      if (intervals.length !== 1 || intervals[0].delay !== MONITORING_REFRESH_INTERVAL_MS) {
        throw new Error(`monitoring created duplicate timers: ${JSON.stringify(intervals.map((item) => item.delay))}`);
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
