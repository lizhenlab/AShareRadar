from __future__ import annotations

import json

import pytest

from tests.test_frontend_diagnostics import _run_node_script as run_node_script


def test_timed_out_write_is_recovered_by_receipt_without_a_second_post() -> None:
    run_node_script(r'''
      import { submitMonitorTaskRequest, refreshMonitorTaskRequest, renderMonitorTaskRequest } from "./static/js/monitor-task-requests.js";
      const { element } = installDom();
      const state = {};
      let posts = 0;
      let requestId;
      const originalSetTimeout = globalThis.setTimeout;
      globalThis.setTimeout = (callback, delay) => {
        if (delay === 12000) { queueMicrotask(callback); return 1; }
        return originalSetTimeout(callback, delay);
      };
      globalThis.fetch = (url, options) => {
        if (options.method === "POST") {
          posts += 1;
          requestId = new URL(url, "http://localhost").searchParams.get("request_id");
          return new Promise(() => {});
        }
        if (url !== `/api/tasks/requests/${requestId}`) throw new Error(`wrong receipt URL ${url}`);
        return Promise.resolve(jsonResponse({ request_id: requestId, requested_task: "refresh_quotes",
          status: "completed", runs: [{ id: 123, status: "success" }], messages: ["完成"] }));
      };
      if (await submitMonitorTaskRequest(state, "refresh_quotes")) throw new Error("timeout reported confirmed receipt");
      if (state.monitorTaskPending?.requestId !== requestId || state.monitorTaskPending?.status !== "unknown") {
        throw new Error("timeout discarded identity or fabricated cancellation");
      }
      globalThis.setTimeout = originalSetTimeout;
      await refreshMonitorTaskRequest(state);
      renderMonitorTaskRequest(state);
      if (posts !== 1 || state.monitorTaskPending || state.monitorTaskReceipt.runs[0].id !== 123) throw new Error("recovery resubmitted or lost actual run");
      if (!element("schedulerState").textContent.includes("本次任务执行已完成")) throw new Error("recovered completion was not displayed");
    ''')


def test_unconfirmed_same_button_retry_reuses_identity_and_other_tasks_stay_disabled() -> None:
    run_node_script(r'''
      import { submitMonitorTaskRequest } from "./static/js/monitor-task-requests.js";
      installDom();
      const state = {};
      const same = { disabled: false, dataset: { task: "refresh_quotes" } };
      const other = { disabled: false, dataset: { task: "refresh_klines" } };
      document.querySelectorAll = () => [same, other];
      const ids = [];
      globalThis.fetch = async (url) => {
        const requestId = new URL(url, "http://localhost").searchParams.get("request_id");
        ids.push(requestId);
        if (ids.length === 1) throw new Error("response lost after commit");
        return jsonResponse({ receipt: { request_id: requestId, requested_task: "refresh_quotes",
          status: "running", runs: [{ id: 4, status: "running" }], messages: [] } });
      };
      await submitMonitorTaskRequest(state, "refresh_quotes");
      if (same.disabled || !other.disabled) throw new Error("pending identity did not control task buttons");
      if (await submitMonitorTaskRequest(state, "refresh_klines")) throw new Error("different task replaced an unresolved request");
      if (!(await submitMonitorTaskRequest(state, "refresh_quotes"))) throw new Error("same request retry failed");
      if (ids.length !== 2 || ids[0] !== ids[1]) throw new Error("retry minted a second execution identity");
    ''')


def test_page_reload_restores_pending_identity_and_rejects_mismatched_receipt() -> None:
    run_node_script(r'''
      import { submitMonitorTaskRequest, refreshMonitorTaskRequest } from "./static/js/monitor-task-requests.js";
      installDom();
      const storage = new Map();
      globalThis.sessionStorage = { getItem: (key) => storage.get(key), setItem: (key, value) => storage.set(key, value), removeItem: (key) => storage.delete(key) };
      globalThis.fetch = async () => { throw new Error("lost response"); };
      const first = {};
      await submitMonitorTaskRequest(first, "refresh_quotes");
      const requestId = first.monitorTaskPending.requestId;
      if (storage.size !== 1) throw new Error("pending request was not persisted");
      const reloaded = {};
      let wrongIdentity = true;
      globalThis.fetch = async (url, options) => {
        if (options.method === "POST" || url !== `/api/tasks/requests/${requestId}`) throw new Error("reload should only query saved identity");
        return jsonResponse({ request_id: wrongIdentity ? crypto.randomUUID() : requestId,
          requested_task: "refresh_quotes", status: "failed", runs: [{ id: 6, status: "failed" }], messages: ["失败"] });
      };
      await refreshMonitorTaskRequest(reloaded);
      if (reloaded.monitorTaskPending?.status !== "unknown" || reloaded.monitorTaskReceipt) throw new Error("foreign receipt replaced pending identity");
      wrongIdentity = false;
      await refreshMonitorTaskRequest(reloaded);
      if (reloaded.monitorTaskPending || reloaded.monitorTaskReceipt.status !== "failed" || storage.size) throw new Error("terminal receipt did not clear restored pending identity");
    ''')


@pytest.mark.parametrize(("status", "run_status", "label"), [
    ("completed", "success", "本次任务执行已完成"),
    ("completed", "degraded", "部分数据未完成更新"),
    ("completed", "pending", "数据仍待就绪"),
    ("completed", None, "未保留本次新执行记录"),
    ("failed", "failed", "任务执行失败"),
    ("interrupted", "cancelled", "执行已中断"),
])
def test_receipt_result_does_not_promote_partial_or_skipped_work_to_success(status, run_status, label) -> None:
    receipt = {"status": status, "runs": [{"status": run_status}] if run_status else []}
    run_node_script(f'''
      import {{ renderMonitorTaskRequest }} from "./static/js/monitor-task-requests.js";
      const {{ element }} = installDom();
      renderMonitorTaskRequest({{ monitorTaskStorageRead: true, monitorTaskReceipt: {json.dumps(receipt)} }});
      if (!element("schedulerState").textContent.includes({json.dumps(label)})) throw new Error(element("schedulerState").textContent);
    ''')
