"""Condition tokens and delayed conflict reads cannot bypass draft ownership."""
from __future__ import annotations

import pytest

from tests.test_frontend_notes_alerts_requests import _run_node_script


@pytest.mark.parametrize('revision', ['undefined', 'null', '123', '""', '"a".repeat(63)', '"A".repeat(64)'])
def test_missing_or_malformed_note_precondition_never_sends_write(revision: str) -> None:
    _run_node_script(f'const revision = {revision};\n' + r'''
      import { removeStockNote, updateStockNote } from "./static/js/notes.js";
      installNotesAlertsDom();
      let calls = 0;
      globalThis.fetch = async () => { calls += 1; return jsonResponse({}); };
      const state = { symbol: "600519.SH" };
      const update = await rejectedMessage(updateStockNote(state, 1, { content: "我的草稿", expected_revision: revision }, async () => {}));
      const removal = await rejectedMessage(removeStockNote(state, 1, async () => {}, { expectedRevision: revision }));
      assert(update.includes("版本缺失") && removal.includes("版本缺失"), "unsafe write lacked actionable precondition feedback");
      assert(calls === 0, "missing precondition still sent a persistence request");
    ''')


@pytest.mark.parametrize('change', [
    '{ id: 2 }', '{ symbol: "000001.SZ" }', '{ revision: "bad" }',
    '{ content: null }', '{ visible: "true" }', '{ price: "10" }',
])
def test_conflict_read_rejects_mismatched_or_invalid_representation(change: str) -> None:
    _run_node_script(CONFLICT_SETUP + f'Object.assign(latest, {change});\n' + r'''
      globalThis.fetch = async () => jsonResponse(latest);
      const message = await rejectedMessage(readLatestStockNote(state, 1, form));
      assert(message.includes("不匹配") && form.dataset.noteRevision === "a".repeat(64), "bad representation silently replaced the draft baseline");
      assert(!box.innerHTML, "untrusted note became a conflict-resolution choice");
    ''')


@pytest.mark.parametrize('invalidate', [
    'current = false;', 'form.isConnected = false;', 'parent.abort();',
])
def test_delayed_latest_note_read_is_suppressed_after_losing_ownership(invalidate: str) -> None:
    _run_node_script(CONFLICT_SETUP + r'''
      const reply = deferredReply();
      const parent = new AbortController();
      let current = true;
      globalThis.fetch = async () => reply.promise;
      const pending = readLatestStockNote(state, 1, form, { signal: parent.signal, isCurrent: () => current });
    ''' + invalidate + r'''
      reply.resolve(jsonResponse(latest));
      try { assert(await pending === false, "old read claimed the current form"); }
      catch (error) { assert(error.name === "AbortError", "unexpected cancellation failure"); }
      assert(!box.innerHTML && form.dataset.noteRevision === "a".repeat(64), "late read exposed a stale rebase action");
    ''')


def test_latest_note_is_displayed_without_automatically_rebasing_or_writing() -> None:
    _run_node_script(CONFLICT_SETUP + r'''
      let calls = 0;
      globalThis.fetch = async (url, options) => {
        calls += 1;
        assert(String(url) === "/api/stock/notes/1" && !options.method && options.cache === "no-store", "latest lookup was not an uncached read");
        return jsonResponse(latest);
      };
      assert(await readLatestStockNote(state, 1, form), "current conflict representation was not displayed");
      assert(calls === 1 && form.dataset.noteRevision === "a".repeat(64), "lookup retried a write or silently rebased the draft");
      assert(box.innerHTML.includes("最新保存内容") && box.innerHTML.includes("保留草稿"), "conflict recovery omitted content or explicit user choice");
    ''')


CONFLICT_SETUP = r'''
  import { readLatestStockNote } from "./static/js/notes.js";
  installNotesAlertsDom();
  const state = { symbol: "600519.SH" };
  const box = { innerHTML: "" };
  const form = { isConnected: true, dataset: { noteId: "1", noteRevision: "a".repeat(64) },
    querySelector(selector) { return selector === "[data-note-conflict]" ? box : null; } };
  const latest = { id: 1, symbol: state.symbol, revision: "b".repeat(64), content: "最新保存内容",
    note_type: "观察", price: 10, trade_date: null, color: null, visible: true,
    created_at: "2026-09-09T10:00:00.000000Z", updated_at: "2026-09-09T11:00:00.000000Z" };
'''


@pytest.mark.parametrize("before,after", [
    ("600519", "600519.SH"), ("SH600519", "600519.SH"), ("600519.SH", "600519"),
])
def test_note_editor_preservation_matches_stock_identity_across_symbol_forms(before: str, after: str) -> None:
    _run_node_script(f'const before = {before!r}, after = {after!r};\n' + r'''
      import { preserveNoteEditors, restoreNoteEditors } from "./static/js/note-editor-state.js";
      installNotesAlertsDom();
      const focused = { focus(options) { assert(options.preventScroll, "restored focus scrolled the page"); this.restored = true; } };
      document.activeElement = focused;
      const editButton = { setAttribute(name, value) { this[name] = value; } };
      const oldRow = { querySelector() { return editButton; } };
      const makeForm = (id, hidden) => ({ hidden, dataset: { noteId: id },
        closest() { return oldRow; }, contains(control) { return control === focused; } });
      const first = makeForm("7", false), second = makeForm("8", false), cancelled = makeForm("9", true);
      const target = { dataset: { noteSymbol: before }, querySelectorAll() { return [first, second, cancelled]; },
        querySelector() { return null; }, append(row) { this.retained = row; } };
      const editors = preserveNoteEditors(target, after, "8");
      assert(editors.length === 1 && editors[0].form === first, "canonical refresh discarded the current same-stock form or retained removed/cancelled forms");
      assert(preserveNoteEditors(target, "000001.SZ").length === 0, "previous-stock draft leaked into another stock");
      restoreNoteEditors(target, editors);
      assert(target.retained === oldRow && editors[0].form === first, "a note missing from the eight-row page lost its original editor");
      assert(editButton["aria-expanded"] === "true" && focused.restored === true, "reattached draft lost open/focus state");
    ''')


@pytest.mark.parametrize("reopen", [False, True])
def test_late_save_conflict_cannot_reopen_or_claim_a_cancelled_editor(reopen: bool) -> None:
    _run_node_script(EDITOR_SETUP + f'const reopen = {str(reopen).lower()};\n' + r'''
      const reply = deferredReply();
      globalThis.fetch = async () => reply.promise;
      const pending = updateStockNote(state, 7,
        { content: control.value, expected_revision: form.dataset.noteRevision }, async () => {}, { noteForm: form });
      assert(toggleStockNoteEditor(button, false), "actual editor cancellation failed");
      assert(form.hidden && control.value === "旧保存内容", "cancel did not preserve the existing explicit-discard behavior");
      if (reopen) {
        assert(toggleStockNoteEditor(button, true), "editor could not be reopened");
        control.value = "重新打开后写的新草稿";
        feedback.textContent = "新编辑轮次反馈";
      }
      const expectedDraft = control.value, expectedFeedback = feedback.textContent;
      reply.resolve(errorResponse(409, "笔记版本冲突"));
      assert(await pending === false, "cancelled save conflict escaped into the new form feedback");
      assert(form.hidden === !reopen && conflict === null, "late conflict reopened a cancelled editor or installed an old recovery action");
      assert(form.dataset.noteRevision === "a".repeat(64), "late conflict silently changed the reopened draft baseline");
      assert(control.value === expectedDraft && feedback.textContent === expectedFeedback, "late conflict changed the current draft or feedback");
    ''')


@pytest.mark.parametrize("action", ["hide", "delete"])
def test_row_action_conflict_still_opens_an_initially_hidden_editor(action: str) -> None:
    _run_node_script(EDITOR_SETUP + f'const action = {action!r};\n' + r'''
      form.hidden = true;
      globalThis.fetch = async () => errorResponse(409, "笔记版本冲突");
      const pending = action === "hide"
        ? updateStockNote(state, 7, { visible: false, expected_revision: form.dataset.noteRevision }, async () => {}, { conflictForm: form })
        : removeStockNote(state, 7, async () => {}, { expectedRevision: form.dataset.noteRevision, conflictForm: form });
      assert((await rejectedMessage(pending)).includes("版本冲突"), "current row action suppressed the real version conflict");
      assert(!form.hidden && conflict?.innerHTML.includes("查看最新版本"), "hidden row editor lost the explicit recovery path");
      assert(form.dataset.noteRevision === "a".repeat(64), "row conflict silently rebased its version");
    ''')


@pytest.mark.parametrize("reopen", [False, True])
def test_late_latest_note_read_cannot_install_a_comparison_after_cancel(reopen: bool) -> None:
    _run_node_script(EDITOR_SETUP + f'const reopen = {str(reopen).lower()};\n' + r'''
      showNoteConflict(form);
      const originalBox = conflict;
      const reply = deferredReply();
      globalThis.fetch = async () => reply.promise;
      const pending = readLatestStockNote(state, 7, form);
      toggleStockNoteEditor(button, false);
      assert(conflict === null, "cancel retained the previous comparison controls");
      if (reopen) {
        toggleStockNoteEditor(button, true);
        control.value = "新编辑轮次的草稿";
        showNoteConflict(form);
        assert(conflict !== originalBox, "reopened editor reused the cancelled conflict box");
        conflict.innerHTML = "当前新冲突仍待读取";
      }
      const currentBox = conflict, expectedDraft = control.value;
      reply.resolve(jsonResponse(latest));
      assert(await pending === false, "old latest lookup claimed a cancelled or reopened editor");
      assert(conflict === currentBox && !conflict?.latestNote, "old response exposed a rebase action in the new editor");
      if (reopen) assert(conflict.innerHTML === "当前新冲突仍待读取", "old response replaced the new conflict feedback");
      assert(control.value === expectedDraft && form.dataset.noteRevision === "a".repeat(64), "old latest read changed draft content or version");
    ''')


def test_new_latest_note_read_owns_the_same_conflict_box() -> None:
    _run_node_script(EDITOR_SETUP + r'''
      showNoteConflict(form);
      const replies = [deferredReply(), deferredReply()];
      let calls = 0;
      globalThis.fetch = async () => replies[calls++].promise;
      const oldRead = readLatestStockNote(state, 7, form);
      const newRead = readLatestStockNote(state, 7, form);
      const newest = { ...latest, revision: "c".repeat(64), content: "最新一次明确读取" };
      replies[1].resolve(jsonResponse(newest));
      assert(await newRead === true, "current latest read was not displayed");
      replies[0].resolve(jsonResponse(latest));
      assert(await oldRead === false, "earlier lookup replaced a later confirmed comparison");
      assert(conflict.latestNote === newest && conflict.innerHTML.includes(newest.content), "the old response won the comparison race");
      assert(form.dataset.noteRevision === "a".repeat(64) && control.value === "待提交草稿", "comparison lookup silently adopted a server baseline");
    ''')


EDITOR_SETUP = r'''
  import { updateStockNote, removeStockNote, readLatestStockNote, toggleStockNoteEditor } from "./static/js/notes.js";
  import { showNoteConflict } from "./static/js/note-editor-state.js";
  installNotesAlertsDom();
  const state = { symbol: "600519.SH" };
  let conflict = null;
  const control = { value: "待提交草稿", focus() {} };
  const feedback = { textContent: "原表单反馈", hidden: true };
  const button = { setAttribute() {}, closest() { return row; } };
  const form = { isConnected: true, hidden: false, dataset: { noteId: "7", noteRevision: "a".repeat(64) },
    elements: { namedItem() { return control; } }, reset() { control.value = "旧保存内容"; },
    closest() { return row; }, append(item) { conflict = item; },
    querySelector(selector) {
      if (selector === "[data-note-conflict]") return conflict;
      if (selector === ".inline-edit-feedback") return feedback;
      return selector === "textarea, input, select" ? control : null;
    } };
  const row = { querySelector(selector) { return selector === "[data-note-edit]" ? button : form; } };
  document.createElement = () => ({ dataset: {}, innerHTML: "", setAttribute() {},
    remove() { if (conflict === this) conflict = null; } });
  const latest = { id: 7, symbol: state.symbol, revision: "b".repeat(64), content: "另一会话的保存内容",
    note_type: "观察", price: 10, trade_date: null, color: null, visible: true,
    created_at: "2026-09-09T10:00:00.000000Z", updated_at: "2026-09-09T11:00:00.000000Z" };
'''
