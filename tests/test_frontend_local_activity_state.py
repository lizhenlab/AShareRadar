from __future__ import annotations

import subprocess
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]


def test_notes_read_syncs_activity_state_and_sanitizes_failures() -> None:
    script = r'''
      import { loadNotes } from "./static/js/notes.js";

      installDom();
      const symbol = "600519.SH";
      const payload = [note("first"), note("second")];
      const state = {
        symbol,
        researchActivityNotes: [note("old")],
        researchActivityNoteSource: { symbol, phase: "ready", message: "" },
      };
      globalThis.fetch = async () => jsonResponse(payload);

      const success = await loadNotes(state);
      assert(success === true, "valid notes did not load");
      assert(state.researchActivityNotes !== payload, "notes state reused the API array");
      assert(state.researchActivityNotes.length === 2, "notes state lost records");
      assert(state.researchActivityNotes[0] === payload[0], "notes state was not a shallow copy");
      assertSource(state.researchActivityNoteSource, symbol, "ready", false);
      payload.push(note("late mutation"));
      assert(state.researchActivityNotes.length === 2, "notes state followed a later API array mutation");

      globalThis.fetch = async () => jsonResponse({ items: [] });
      const malformed = await loadNotes(state);
      assert(malformed === false, "non-array notes were accepted");
      assert(state.researchActivityNotes.length === 0, "non-array notes left old activity data");
      assertSource(state.researchActivityNoteSource, symbol, "unavailable", true);

      const privateDetail = "private backend path /srv/notes.db";
      state.researchActivityNotes = [note("stale")];
      globalThis.fetch = async () => failedResponse(privateDetail);
      const failed = await loadNotes(state);
      assert(failed === false, "failed notes request reported success");
      assert(state.researchActivityNotes.length === 0, "failed notes request left old activity data");
      assertSource(state.researchActivityNoteSource, symbol, "unavailable", true);
      assert(!state.researchActivityNoteSource.message.includes(privateDetail), "notes source leaked backend detail");
    '''
    _run_node_script(script)


def test_alert_events_alone_control_activity_state() -> None:
    script = r'''
      import { loadAlerts } from "./static/js/alerts.js";

      const dom = installDom();
      const symbol = "600519.SH";
      const events = [alertEvent("current event")];
      const state = {
        symbol,
        researchActivityAlerts: [alertEvent("old event")],
        researchActivityAlertSource: { symbol, phase: "ready", message: "" },
      };
      globalThis.fetch = async (url) => {
        if (String(url).startsWith("/api/alerts/events")) return jsonResponse(events);
        throw new Error("rules service unavailable");
      };

      const partial = await loadAlerts(state);
      assert(partial === true, "rules failure changed alert load completion");
      assert(dom.element("alertList").innerHTML.includes("rules service unavailable") === false, "unexpected raw rules error");
      assert(state.researchActivityAlerts !== events, "alerts state reused the API array");
      assert(state.researchActivityAlerts.length === 1, "alert events were not synchronized");
      assert(state.researchActivityAlerts[0] === events[0], "alerts state was not a shallow copy");
      assertSource(state.researchActivityAlertSource, symbol, "ready", false);

      globalThis.fetch = async (url) => String(url).startsWith("/api/alerts/events")
        ? jsonResponse({ items: [] })
        : jsonResponse([alertRule("rule")]);
      const malformed = await loadAlerts(state);
      assert(malformed === true, "non-array events changed alert load completion");
      assert(state.researchActivityAlerts.length === 0, "non-array events left old activity data");
      assertSource(state.researchActivityAlertSource, symbol, "unavailable", true);

      const privateDetail = "token=secret-alert-token";
      state.researchActivityAlerts = [alertEvent("stale event")];
      globalThis.fetch = async (url) => String(url).startsWith("/api/alerts/events")
        ? failedResponse(privateDetail)
        : jsonResponse([alertRule("rule")]);
      const failed = await loadAlerts(state);
      assert(failed === true, "event failure changed alert load completion");
      assert(state.researchActivityAlerts.length === 0, "failed events request left old activity data");
      assertSource(state.researchActivityAlertSource, symbol, "unavailable", true);
      assert(!state.researchActivityAlertSource.message.includes(privateDetail), "alert source leaked backend detail");
    '''
    _run_node_script(script)


def test_stale_and_aborted_notes_reads_preserve_new_activity_state() -> None:
    script = r'''
      import { loadNotes } from "./static/js/notes.js";

      installDom();
      const symbol = "600519.SH";
      const state = { symbol };
      const oldReply = deferred();
      let callCount = 0;
      globalThis.fetch = async () => {
        callCount += 1;
        return callCount === 1 ? oldReply.promise : jsonResponse([note("new state")]);
      };

      const oldLoad = loadNotes(state);
      await Promise.resolve();
      const newLoad = loadNotes(state);
      const [oldResult, newResult] = await Promise.all([oldLoad, newLoad]);
      assert(oldResult === false && newResult === true, "notes ownership results were incorrect");
      assert(state.researchActivityNotes[0].content === "new state", "old notes request won");
      oldReply.resolve(jsonResponse([note("old state")]));
      await Promise.resolve();
      assert(state.researchActivityNotes[0].content === "new state", "late notes response replaced new state");

      const notesRef = state.researchActivityNotes;
      const sourceRef = state.researchActivityNoteSource;
      const abortReply = deferred();
      const controller = new AbortController();
      globalThis.fetch = async () => abortReply.promise;
      const abortedLoad = loadNotes(state, { signal: controller.signal });
      await Promise.resolve();
      controller.abort();
      assert(await abortedLoad === false, "aborted notes request reported success");
      assert(state.researchActivityNotes === notesRef, "aborted notes request replaced activity data");
      assert(state.researchActivityNoteSource === sourceRef, "aborted notes request replaced source state");
      abortReply.resolve(jsonResponse([note("aborted state")]));

      globalThis.fetch = async () => jsonResponse([note("stale state")]);
      assert(await loadNotes(state, { isCurrent: () => false }) === false, "stale notes request reported success");
      assert(state.researchActivityNotes === notesRef, "stale notes request replaced activity data");
      assert(state.researchActivityNoteSource === sourceRef, "stale notes request replaced source state");
    '''
    _run_node_script(script)


def test_stale_and_aborted_alert_reads_preserve_new_activity_state() -> None:
    script = r'''
      import { loadAlerts } from "./static/js/alerts.js";

      installDom();
      const symbol = "600519.SH";
      const state = { symbol };
      const oldReply = deferred();
      let callCount = 0;
      globalThis.fetch = async (url) => {
        callCount += 1;
        if (callCount <= 2) return oldReply.promise;
        return String(url).startsWith("/api/alerts/events")
          ? jsonResponse([alertEvent("new state")])
          : jsonResponse([alertRule("new rule")]);
      };

      const oldLoad = loadAlerts(state);
      await Promise.resolve();
      const newLoad = loadAlerts(state);
      const [oldResult, newResult] = await Promise.all([oldLoad, newLoad]);
      assert(oldResult === false && newResult === true, "alerts ownership results were incorrect");
      assert(state.researchActivityAlerts[0].message === "new state", "old events request won");
      oldReply.resolve(jsonResponse([alertEvent("old state")]));
      await Promise.resolve();
      assert(state.researchActivityAlerts[0].message === "new state", "late events response replaced new state");

      const alertsRef = state.researchActivityAlerts;
      const sourceRef = state.researchActivityAlertSource;
      const abortReply = deferred();
      const controller = new AbortController();
      globalThis.fetch = async () => abortReply.promise;
      const abortedLoad = loadAlerts(state, { signal: controller.signal });
      await Promise.resolve();
      controller.abort();
      assert(await abortedLoad === false, "aborted alerts request reported success");
      assert(state.researchActivityAlerts === alertsRef, "aborted alerts request replaced activity data");
      assert(state.researchActivityAlertSource === sourceRef, "aborted alerts request replaced source state");
      abortReply.resolve(jsonResponse([]));

      globalThis.fetch = async (url) => String(url).startsWith("/api/alerts/events")
        ? jsonResponse([alertEvent("stale state")])
        : jsonResponse([alertRule("stale rule")]);
      assert(await loadAlerts(state, { isCurrent: () => false }) === false, "stale alerts request reported success");
      assert(state.researchActivityAlerts === alertsRef, "stale alerts request replaced activity data");
      assert(state.researchActivityAlertSource === sourceRef, "stale alerts request replaced source state");
    '''
    _run_node_script(script)


def _run_node_script(test_body: str) -> None:
    subprocess.run(
        ["node", "--input-type=module", "-e", f"{test_body}\n{JS_FIXTURE}"],
        cwd=ROOT,
        check=True,
        text=True,
        capture_output=True,
    )


JS_FIXTURE = r'''
function installDom() {
  const elements = new Map();
  function element(id) {
    if (!elements.has(id)) {
      elements.set(id, {
        id,
        value: "",
        innerHTML: "",
        dataset: {},
        disabled: false,
        hidden: false,
        textContent: "",
        attributes: {},
        setAttribute(name, value) {
          this.attributes[name] = String(value);
        },
      });
    }
    return elements.get(id);
  }
  globalThis.document = { getElementById: element };
  return { element };
}

function note(content) {
  return {
    id: content,
    note_type: "observation",
    content,
    price: 10,
    trade_date: "2026-07-15",
    created_at: "2026-07-15",
    visible: true,
  };
}

function alertRule(name) {
  return {
    id: name,
    name,
    condition_label: "above",
    threshold: 12,
    enabled: true,
    trigger_count: 0,
    cooldown_seconds: 300,
  };
}

function alertEvent(message) {
  return {
    id: message,
    name: "test alert",
    event_type: "triggered",
    created_at: "2026-07-15 10:00:00",
    price: 12,
    threshold: 11,
    change_pct: 1,
    message,
  };
}

function jsonResponse(payload) {
  return {
    ok: true,
    async json() {
      return payload;
    },
  };
}

function failedResponse(detail) {
  return {
    ok: false,
    status: 503,
    async text() {
      return JSON.stringify({ detail });
    },
  };
}

function deferred() {
  let resolve;
  const promise = new Promise((settle) => {
    resolve = settle;
  });
  return { promise, resolve };
}

function assertSource(source, symbol, phase, requiresMessage) {
  assert(source && source.symbol === symbol, `source symbol mismatch: ${JSON.stringify(source)}`);
  assert(source.phase === phase, `source phase mismatch: ${JSON.stringify(source)}`);
  assert(typeof source.message === "string", "source message is not a string");
  assert(requiresMessage ? source.message.length > 0 : source.message === "", "source message contract mismatch");
}

function assert(condition, message) {
  if (!condition) throw new Error(message);
}
'''
