"""Alert rule readbacks preserve draft and submission ownership."""
from __future__ import annotations

import pytest

from tests.test_frontend_notes_alerts_requests import _run_node_script


@pytest.mark.parametrize("next_id", [1, 2])
@pytest.mark.parametrize("readback_fails", [False, True])
def test_delayed_alert_save_preserves_the_current_draft(next_id: int, readback_fails: bool) -> None:
    _run_node_script(SETUP + f"const nextId = {next_id}, readbackFails = {str(readback_fails).lower()};\n" + r'''
      const form = openEditor(1);
      form.elements.namedItem("name").value = "本次提交名称";
      const payload = alertRuleUpdatesFromForm(form);
      const pending = updateAlertRule(state, 1, payload, { alertForm: form });
      const editing = nextId === 1 ? form : openEditor(2);
      editing.elements.namedItem("name").value = "等待期间的新草稿";
      editing.elements.namedItem("note").value = "不能丢失的备注";
      document.activeElement = editing.elements.namedItem("note");
      saved[0] = { ...saved[0], ...payload };
      failReadback = readbackFails;
      reply.resolve(jsonResponse(saved[0]));
      assert(await pending, "confirmed alert write lost completion");
      assert(target.rows.get(String(nextId)).form === editing && editing.isConnected && !editing.hidden,
        "late alert receipt discarded the current editor");
      assert(editing.elements.namedItem("name").value === "等待期间的新草稿", "late save lost the newer name draft");
      assert(editing.elements.namedItem("note").value === "不能丢失的备注", "late save lost the newer note draft");
      assert(document.activeElement === editing.elements.namedItem("note"), "restored editor lost focus");
      const next = alertRuleUpdatesFromForm(editing);
      assert(next.name === "等待期间的新草稿" && next.threshold === saved[nextId - 1].threshold,
        "next save changed the target rule condition");
      if (nextId === 1) {
        editing.reset();
        assert(editing.elements.namedItem("name").value === "本次提交名称", "cancel baseline reverted behind confirmed save");
      }
      assert(writes.length === 1 && !writes[0].signal.aborted, "draft recovery repeated or cancelled the independent write");
      failReadback = false;
      globalThis.fetch = async (url, options = {}) => {
        if (options.method === "PATCH") {
          assert(String(url) === `/api/alerts/${nextId}`, "retry targeted the previous editor rule");
          assert(JSON.stringify(JSON.parse(options.body)) === JSON.stringify(next), "retry lost the current draft or rule condition");
          writes.push(options);
          saved[nextId - 1] = { ...saved[nextId - 1], ...next };
          return jsonResponse(saved[nextId - 1]);
        }
        return jsonResponse(String(url).startsWith("/api/alerts/events") ? [] : saved);
      };
      for (const [name, value] of Object.entries(next)) editing.elements.namedItem(name).value = String(value ?? "");
      assert(await updateAlertRule(state, nextId, next, { alertForm: editing }), "explicit next save failed");
      assert(writes.length === 2 && target.rows.get(String(nextId)).form.hidden, "next unchanged save did not confirm and close normally");
    ''')


def test_unchanged_alert_submission_closes_after_confirmation() -> None:
    _run_node_script(SETUP + r'''
      const form = openEditor(1);
      form.elements.namedItem("name").value = "已确认的新名称";
      const payload = alertRuleUpdatesFromForm(form);
      const pending = updateAlertRule(state, 1, payload, { alertForm: form });
      saved[0] = { ...saved[0], ...payload };
      reply.resolve(jsonResponse(saved[0]));
      assert(await pending, "confirmed save was not successful");
      assert(target.rows.get("1").form.hidden, "unchanged confirmed draft stayed open");
      assert(target.rows.get("1").form.elements.namedItem("name").value === payload.name, "confirmed baseline was not rendered");
    ''')


@pytest.mark.parametrize("reopen", [False, True])
def test_cancelled_alert_save_failure_does_not_claim_a_later_editor(reopen: bool) -> None:
    _run_node_script(SETUP + f"const reopen = {str(reopen).lower()};\n" + r'''
      const form = openEditor(1);
      const pending = updateAlertRule(state, 1, alertRuleUpdatesFromForm(form), { alertForm: form });
      toggleAlertRuleEditor(target.rows.get("1").button, false);
      if (reopen) openEditor(1).elements.namedItem("note").value = "新一轮草稿";
      reply.resolve(errorResponse(503, "旧保存失败"));
      assert(await pending === false, "cancelled save leaked its error into a later editing session");
      assert(form.hidden === !reopen && (!reopen || form.elements.namedItem("note").value === "新一轮草稿"),
        "old failure reopened or changed the editor");
    ''')


def test_current_alert_write_failure_preserves_retryable_draft() -> None:
    _run_node_script(SETUP + r'''
      const form = openEditor(1);
      form.elements.namedItem("name").value = "失败后可重试";
      const pending = updateAlertRule(state, 1, alertRuleUpdatesFromForm(form), { alertForm: form });
      reply.resolve(errorResponse(503, "保存暂不可用"));
      assert((await rejectedMessage(pending)).includes("保存暂不可用"), "current write error was hidden");
      assert(form.isConnected && !form.hidden && form.elements.namedItem("name").value === "失败后可重试", "failed write discarded the editable draft");
    ''')


@pytest.mark.parametrize("reopen", [False, True])
def test_cancelled_alert_save_success_cannot_close_a_reopened_draft(reopen: bool) -> None:
    _run_node_script(SETUP + f"const reopen = {str(reopen).lower()};\n" + r'''
      const form = openEditor(1);
      const payload = alertRuleUpdatesFromForm(form);
      const pending = updateAlertRule(state, 1, payload, { alertForm: form });
      toggleAlertRuleEditor(target.rows.get("1").button, false);
      if (reopen) openEditor(1);
      reply.resolve(jsonResponse(saved[0]));
      assert(await pending, "completed independent write lost confirmation");
      assert(target.rows.get("1").form.hidden === !reopen, "old success closed the reopened editing session with the same draft");
    ''')


@pytest.mark.parametrize("change,preserved", [
    ('{ symbol: "SH600519", trigger_count: 9 }', True),
    ('{ symbol: "000001.SZ" }', False),
    ('{ created_at: "2026-09-10T04:00:00Z" }', False),
])
def test_alert_readback_preserves_only_the_same_rule_identity(change: str, preserved: bool) -> None:
    _run_node_script(SETUP + f"const change = {change}, preserved = {str(preserved).lower()};\n" + r'''
      const form = openEditor(1);
      form.elements.namedItem("note").value = "刷新中保留的草稿";
      saved[0] = { ...saved[0], ...change };
      assert(await loadAlerts(state), "current ordinary alert readback failed");
      const current = target.rows.get("1").form;
      assert((current === form && !current.hidden) === preserved, "draft crossed rule identity or disappeared on valid refresh");
      assert(!preserved || current.elements.namedItem("note").value === "刷新中保留的草稿", "ordinary readback lost current draft");
      assert(writes.length === 0, "readback recovery sent a write");
    ''')


def test_confirmed_rule_deletion_does_not_restore_its_open_editor() -> None:
    _run_node_script(SETUP + r'''
      const form = openEditor(1);
      globalThis.fetch = async (url, options = {}) => {
        if (options.method === "DELETE") { saved.shift(); return jsonResponse({ removed: true }); }
        return jsonResponse(String(url).startsWith("/api/alerts/events") ? [] : saved);
      };
      assert(await removeAlertRule(state, 1), "confirmed deletion was lost");
      assert(!form.isConnected && !target.rows.has("1"), "draft preservation restored a deleted rule");
    ''')


def test_alert_refresh_rebases_cancel_without_replacing_the_current_draft() -> None:
    _run_node_script(SETUP + r'''
      const form = openEditor(1);
      form.elements.namedItem("name").value = "刷新前尚未保存的草稿";
      saved[0] = { ...saved[0], name: "另一会话已保存的名称", condition_type: "price_below", threshold: 30,
        cooldown_seconds: 600, note: "另一会话已保存的备注" };
      assert(await loadAlerts(state), "ordinary refresh failed");
      assert(form.isConnected && !form.hidden && form.elements.namedItem("name").value === "刷新前尚未保存的草稿",
        "new server values replaced an unsubmitted draft");
      toggleAlertRuleEditor(target.rows.get("1").button, false);
      openEditor(1);
      for (const name of ["name", "condition_type", "threshold", "cooldown_seconds", "note"]) {
        assert(form.elements.namedItem(name).value === String(saved[0][name]), `cancel/reopen restored obsolete ${name}`);
      }
      assert(writes.length === 0, "refresh or cancel sent a write");
    ''')


SETUP = r'''
  import { alertRuleUpdatesFromForm, loadAlerts, removeAlertRule, renderAlerts, toggleAlertRuleEditor, updateAlertRule } from "./static/js/alerts.js";
  const dom = installNotesAlertsDom();
  const state = { symbol: "600519.SH" }, reply = deferredReply(), writes = [];
  const saved = [1, 2].map(id => alertRule(`规则${id}`, { id, symbol: state.symbol, note: "原备注", threshold: id * 10,
    created_at: "2026-09-09T04:00:00Z" }));
  let failReadback = false;
  const target = dom.element("alertList");
  installEditorTarget(target);
  globalThis.fetch = async (url, options = {}) => {
    if (options.method === "PATCH") { writes.push(options); return reply.promise; }
    if (String(url).startsWith("/api/alerts/events")) return jsonResponse([]);
    return failReadback ? errorResponse(503, "列表暂不可用") : jsonResponse(saved);
  };
  renderAlerts(saved);
  function openEditor(id) {
    const row = target.rows.get(String(id));
    toggleAlertRuleEditor(row.button, true);
    return row.form;
  }
  function installEditorTarget(list) {
    list.rows = new Map();
    let html = "";
    Object.defineProperty(list, "innerHTML", {
      get() { return html; },
      set(value) {
        html = value;
        for (const row of this.rows.values()) row.form.isConnected = false;
        this.rows = new Map();
        for (const match of value.matchAll(/<article[^>]*data-alert-row="([0-9]+)"[^>]*>([\s\S]*?)<\/article>/g)) {
          const id = match[1], markup = match[2], controls = new Map();
          for (const input of markup.matchAll(/<input name="([^"]+)"[^>]*value="([^"]*)"/g)) controls.set(input[1], control(input[2]));
          const condition = markup.match(/<option value="([^"]+)" selected/)[1];
          controls.set("condition_type", { ...control(condition), tagName: "SELECT", options: [condition, "price_below"].map(value => ({ value, defaultSelected: value === condition })) });
          const row = { id, querySelector(selector) { return selector === "[data-alert-edit]" ? this.button : this.form; } };
          row.button = { closest() { return row; }, setAttribute(name, value) { this[name] = value; } };
          row.form = { hidden: true, isConnected: true, dataset: { alertId: id }, row,
            elements: { namedItem(name) { return controls.get(name); } },
            closest() { return this.row; }, contains(item) { return [...controls.values()].includes(item); },
            querySelector(selector) { return selector === ".inline-edit-feedback" ? null : controls.get("name"); },
            reset() { for (const item of controls.values()) item.value = item.tagName === "SELECT" ? item.options.find(option => option.defaultSelected).value : item.defaultValue; },
            replaceWith(form) { this.isConnected = false; form.row = row; form.isConnected = true; row.form = form; } };
          this.rows.set(id, row);
        }
      },
    });
    list.insertAdjacentHTML = (_position, value) => { html += value; };
    list.querySelectorAll = () => [...list.rows.values()].map(row => row.form);
    list.querySelector = selector => list.rows.get(selector.match(/"([0-9]+)"/)[1]) || null;
    document.querySelectorAll = () => [...list.rows.values()].map(row => row.form).filter(form => !form.hidden);
    function control(value) { return { value, defaultValue: value, tagName: "INPUT", focus() { document.activeElement = this; } }; }
  }
'''
