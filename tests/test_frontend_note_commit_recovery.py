"""A confirmed note write remains confirmed when its UI readback fails."""

from __future__ import annotations

import pytest

from tests.test_frontend_app_flow import _run_node_script as _run_app_script
from tests.test_frontend_notes_alerts_requests import _run_node_script


@pytest.mark.parametrize("operation", ["add", "update", "remove"])
def test_confirmed_note_mutation_survives_failed_list_readback(operation: str) -> None:
    _run_node_script(SETUP + f'const operation = "{operation}";\n' + r'''
      await loadNotes(state);
      dom.element("noteContent").value = "新笔记";
      readbackFails = true;
      const result = await mutate(operation);
      const html = dom.element("noteList").innerHTML;

      assert(result === true, "confirmed persistence was reported as a failed write");
      assert(writes === 1 && marks === 1, "readback recovery repeated a write or skipped independent marks");
      assert(html.includes("保留笔记"), "failed readback erased an unrelated known note");
      const label = { add: "保存", update: "修改", remove: "删除" }[operation];
      assert(html.includes(`笔记已${label}`) && html.includes("列表刷新失败"), "commit and list failure were not distinguished");
      if (operation === "add") {
        assert(html.includes("新笔记") && dom.element("noteContent").value === "", "confirmed new note was lost");
      } else if (operation === "update") {
        assert(html.includes("修改后内容") && !html.includes("原笔记"), "confirmed update reverted to stale data");
      } else {
        assert(!html.includes('data-note-remove="1"'), "confirmed deletion remained actionable");
      }
      assert(state.researchActivityNoteSource.phase === "unavailable", "failed full readback was mislabeled as fresh");
      assert(state.researchActivityNotes.some((item) => item.id === 2), "known activity notes were discarded");
      readbackFails = false;
      await loadNotes(state);
      assert(!dom.element("noteList").innerHTML.includes("列表刷新失败"), "successful retry retained stale warning");
      assert(state.researchActivityNoteSource.phase === "ready" && writes === 1, "read-only retry repeated mutation");
    ''')


def test_confirmed_note_write_is_not_failed_by_chart_refresh_callback() -> None:
    _run_node_script(SETUP + r'''
      await loadNotes(state);
      dom.element("noteContent").value = "新笔记";
      const result = await addStockNote(state, async () => { throw new Error("图表读取暂不可用"); });

      assert(result === true && writes === 1, "a chart failure escaped as an uncommitted write");
      assert(dom.element("noteContent").value === "", "confirmed draft was not cleared");
      const html = dom.element("noteList").innerHTML;
      assert(html.includes("新笔记") && html.includes("笔记已保存") && html.includes("图表标注刷新失败"), "chart-only degradation hid confirmed note");
      assert(state.researchActivityNoteSource.phase === "ready", "successful note readback was marked unavailable");
    ''')


@pytest.mark.parametrize("operation", ["add", "update", "remove"])
def test_unconfirmed_note_write_preserves_draft_and_list(operation: str) -> None:
    _run_node_script(SETUP + f'const operation = "{operation}";\n' + r'''
      await loadNotes(state);
      const before = dom.element("noteList").innerHTML;
      dom.element("noteContent").value = "保留草稿";
      globalThis.fetch = async () => errorResponse(503, "写入暂不可用");

      assert(await rejectedMessage(mutate(operation)) === "写入暂不可用", "write error was swallowed as committed");
      assert(dom.element("noteList").innerHTML === before, "unconfirmed write changed the known list");
      assert(dom.element("noteContent").value === "保留草稿", "unconfirmed write cleared a draft");
      assert(marks === 0 && state.researchActivityNoteSource.phase === "ready", "failed mutation ran a readback");
    ''')


def test_note_commit_recovery_does_not_clear_a_newer_draft() -> None:
    _run_node_script(SETUP + r'''
      await loadNotes(state);
      const committedReply = deferredReply();
      const fetchSaved = globalThis.fetch;
      globalThis.fetch = async (url, options = {}) => {
        if (options.method === "POST") return committedReply.promise;
        return fetchSaved(url, options);
      };
      dom.element("noteContent").value = "已提交草稿";
      const addition = addStockNote(state, async () => {});
      dom.element("noteContent").value = "下一条草稿";
      readbackFails = true;
      committedReply.resolve(jsonResponse({ ...original, id: 3, content: "已提交草稿" }));

      assert(await addition === true, "accepted note lost its completion status");
      assert(dom.element("noteContent").value === "下一条草稿", "readback recovery cleared a newer draft");
      assert(dom.element("noteList").innerHTML.includes("已提交草稿"), "accepted response was not reconciled");
    ''')


def test_late_note_readback_failure_cannot_restore_previous_stock() -> None:
    _run_node_script(SETUP + r'''
      await loadNotes(state);
      const readback = deferredReply();
      const readStarted = deferredReply();
      const fetchSaved = globalThis.fetch;
      globalThis.fetch = async (url, options = {}) => {
        if (options.method === "POST") return fetchSaved(url, options);
        readStarted.resolve();
        return readback.promise;
      };
      dom.element("noteContent").value = "新笔记";
      const addition = addStockNote(state, async () => { marks += 1; });
      await readStarted.promise;
      state.symbol = "000001.SZ";
      state.researchActivityNotes = [{ ...retained, symbol: state.symbol, content: "新股票笔记" }];
      state.researchActivityNoteSource = { symbol: state.symbol, phase: "ready", message: "" };
      dom.element("noteList").innerHTML = "新股票笔记面板";
      dom.element("noteContent").value = "新股票草稿";
      readback.resolve(errorResponse(503, "旧股票回读失败"));

      assert(await addition === false && marks === 0, "late readback ran a stale refresh tail");
      assert(dom.element("noteList").innerHTML === "新股票笔记面板", "late failure restored old stock notes");
      assert(dom.element("noteContent").value === "新股票草稿", "late failure cleared a different stock draft");
      assert(state.researchActivityNoteSource.symbol === state.symbol, "late failure corrupted activity ownership");
    ''')


def test_deleting_last_known_note_never_claims_empty_list_before_readback() -> None:
    _run_node_script(SETUP + r'''
      readbackFails = true;
      await loadNotes(state);
      dom.element("noteContent").value = "新笔记";
      await addStockNote(state, async () => {});
      assert(state.researchActivityNotes.length === 1, "fixture must contain only the confirmed receipt");
      assert(state.researchActivityNoteSource.phase === "unavailable", "fixture must lack a complete readback");
      const readback = deferredReply();
      const readStarted = deferredReply();
      globalThis.fetch = async (_url, options = {}) => {
        if (options.method === "DELETE") return jsonResponse({ ok: true, removed: true });
        readStarted.resolve();
        return readback.promise;
      };
      const removal = removeStockNote(state, 3, async () => {}, { expectedRevision: "a".repeat(64) });
      await readStarted.promise;

      const pending = dom.element("noteList").innerHTML;
      assert(state.researchActivityNotes.length === 0, "confirmed deletion was not reconciled");
      assert(pending.includes("笔记列表待同步") && !pending.includes("暂无笔记"), "pending full readback was falsely shown as empty");
      readback.resolve(errorResponse(503, "完整列表仍不可用"));
      assert(await removal === true, "readback failure lost confirmed deletion");
      assert(!dom.element("noteList").innerHTML.includes("暂无笔记"), "failed readback was falsely shown as empty");
      globalThis.fetch = async () => jsonResponse([]);
      await loadNotes(state);
      assert(dom.element("noteList").innerHTML.includes("暂无笔记"), "successful empty readback was not displayed");
      assert(state.researchActivityNoteSource.phase === "ready", "successful empty readback stayed unavailable");
    ''')


@pytest.mark.parametrize("bad_response", ['{}', '{ ...original, symbol: "000001.SZ" }', '{ ...original, id: 99 }'])
def test_unusable_note_response_never_invents_confirmed_state(bad_response: str) -> None:
    _run_node_script(SETUP + f'const badResponse = {bad_response};\n' + r'''
      state.researchActivityNotes = [{ ...retained, symbol: "000001.SZ", content: "其他股票笔记" }];
      state.researchActivityNoteSource = { symbol: "000001.SZ", phase: "ready", message: "" };
      globalThis.fetch = async (_url, options = {}) => options.method === "PATCH"
        ? jsonResponse(badResponse) : errorResponse(503, "回读失败");
      const result = await updateStockNote(state, 1, { content: "不能凭提交内容推断完整结果", expected_revision: "a".repeat(64) }, async () => {});
      const html = dom.element("noteList").innerHTML;

      assert(result === true, "accepted write was reported as uncommitted");
      assert(!html.includes("其他股票笔记") && !html.includes("原笔记"), "unbound server response contaminated the list");
      assert(html.includes("笔记列表待同步") && !html.includes("暂无笔记"), "unknown list was mislabeled as empty");
      assert(state.researchActivityNotes.length === 0, "unverified response became known state");
    ''')


@pytest.mark.parametrize("malformed_list", ["{ items: [] }", "[null]"])
def test_malformed_list_and_chart_failure_preserve_confirmed_note_with_escaped_feedback(malformed_list: str) -> None:
    _run_node_script(SETUP + f'const malformedList = {malformed_list};\n' + r'''
      await loadNotes(state);
      const fetchSaved = globalThis.fetch;
      globalThis.fetch = async (url, options = {}) => options.method === "POST"
        ? fetchSaved(url, options) : jsonResponse(malformedList);
      dom.element("noteContent").value = "新笔记";
      const result = await addStockNote(state, async () => { throw new Error("<img src=x onerror=alert(1)>"); });
      const html = dom.element("noteList").innerHTML;

      assert(result === true && writes === 1, "refresh failures lost or repeated confirmed persistence");
      assert(html.includes("新笔记") && html.includes("保留笔记"), "malformed readback erased confirmed notes");
      assert(html.includes("列表刷新失败") && html.includes("图表标注刷新失败"), "independent failures were not both explained");
      assert(!html.includes("<img") && html.includes("&lt;img"), "refresh error injected HTML");
    ''')


def test_actual_note_form_confirms_save_while_list_and_chart_are_unavailable() -> None:
    _run_app_script(r'''
      import { createAppHarness } from "./tests/frontend_app_flow_helpers.mjs";
      const { __appTest, element, jsonResponse } = await createAppHarness();
      const state = __appTest.state;
      state.symbol = "600519.SH";
      state.loadSeq = 40;
      state.lastAnalysis = null;
      element("noteContent").value = "确认保存的笔记";
      element("noteType").value = "观察";
      element("noteForm-button").textContent = "保存";
      let writes = 0;
      globalThis.fetch = async (url, options = {}) => {
        if (String(url) === "/api/stock/notes" && options.method === "POST") {
          writes += 1;
          return jsonResponse({ id: 7, symbol: state.symbol, note_type: "观察", content: "确认保存的笔记", visible: true });
        }
        return { ok: false, status: 503, async json() { return { detail: "读取暂不可用" }; } };
      };
      await element("noteForm").listeners.submit({ preventDefault() {}, currentTarget: element("noteForm") });
      const html = element("noteList").innerHTML;
      if (!html.includes("确认保存的笔记") || !html.includes("笔记已保存") || !html.includes("列表刷新失败")) {
        throw new Error(`actual form lost the confirmed write: ${html}`);
      }
      if (writes !== 1 || element("noteContent").value !== "" || element("noteForm-button").disabled) {
        throw new Error("actual form repeated persistence or failed to release submitted draft/button");
      }
      if (state.mutationStatus.phase === "error" || element("noteFormFeedback").textContent.includes("写入失败")) {
        throw new Error("ordinary readback failure was mislabeled as write failure");
      }
    ''')


SETUP = r'''
  import { addStockNote, loadNotes, removeStockNote, updateStockNote } from "./static/js/notes.js";
  const dom = installNotesAlertsDom();
  const state = { symbol: "600519.SH", lastAnalysis: null };
  const original = { ...note("原笔记"), id: 1, symbol: state.symbol };
  const retained = { ...note("保留笔记"), id: 2, symbol: state.symbol };
  let saved = [original, retained];
  let readbackFails = false;
  let writes = 0;
  let marks = 0;
  globalThis.fetch = async (url, options = {}) => {
    if (options.method === "POST") {
      writes += 1;
      const item = { ...note("新笔记"), id: 3, symbol: state.symbol };
      saved = [item, ...saved];
      return jsonResponse(item);
    }
    if (options.method === "PATCH") {
      writes += 1;
      const item = { ...original, content: "修改后内容" };
      saved = [item, retained];
      return jsonResponse(item);
    }
    if (options.method === "DELETE") {
      writes += 1;
      saved = [retained];
      return jsonResponse({ ok: true, removed: true });
    }
    if (readbackFails) return errorResponse(503, "列表回读暂不可用");
    return jsonResponse(saved);
  };
  async function mutate(operation) {
    const refresh = async () => { marks += 1; };
    if (operation === "add") return addStockNote(state, refresh);
    if (operation === "update") return updateStockNote(state, 1, { content: "修改后内容", expected_revision: original.revision }, refresh);
    return removeStockNote(state, 1, refresh, { expectedRevision: original.revision });
  }
'''
