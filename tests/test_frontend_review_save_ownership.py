from __future__ import annotations

import pytest

from test_frontend_review_drafts import PRELUDE
from test_frontend_review_scan import _run_node


SAVE_PRELUDE = PRELUDE + r'''
  const planA = completeReviewPlan();
  const planB = completeReviewPlan({ id: 8, advice_id: 4, hypothesis: "计划B" });
  let serverPlans = [planA, planB];
  const details = () => serverPlans.map(plan => ({ plan, latest_evaluation: null }));
  state.adviceReviewDetails = details();
  api.syncAdviceReviewSnapshots(state, [snapshot(3), snapshot(4), snapshot(5)], null);
  api.beginAdviceReviewEdit(state, 7);
  let releaseWrite;
  const writes = [];
  globalThis.fetch = (url, options = {}) => {
    if (!["PATCH", "POST"].includes(options.method)) return Promise.resolve(jsonResponse(details()));
    const request = { url: String(url), method: options.method, payload: JSON.parse(options.body) };
    writes.push(request);
    return new Promise(resolve => { releaseWrite = resolve; request.resolve = resolve; });
  };
  function acceptWrite(index = writes.length - 1) {
    const request = writes[index];
    const previous = request.method === "PATCH"
      ? serverPlans.find(plan => request.url.endsWith(`/${plan.id}`)) : null;
    const { expected_revision, ...fields } = request.payload;
    assert(!previous || expected_revision === previous.revision, "PATCH used a stale server revision");
    const saved = completeReviewPlan({ ...previous, ...fields,
      id: previous?.id || 9, revision: previous ? previous.revision + 1 : 1,
      plan_payload_digest: String(previous ? previous.revision + 1 : 1).repeat(64),
    });
    serverPlans = [...serverPlans.filter(plan => plan.id !== saved.id), saved];
    request.resolve(jsonResponse(saved));
    return saved;
  }
  function assertDraft(expected, message) {
    assert(JSON.stringify(values()) === JSON.stringify(expected), message);
  }
'''


@pytest.mark.parametrize("blank", [False, True])
def test_late_review_save_preserves_other_plan_draft_and_next_patch_owner(blank: bool) -> None:
    _run_node(SAVE_PRELUDE + f"const blank = {str(blank).lower()};" + r'''
      const pending = api.submitAdviceReviewPlan(state);
      api.beginAdviceReviewEdit(state, 8);
      draft("B尚未保存的输入");
      if (blank) elements.get("reviewHypothesis").value = "";
      const expected = values();
      acceptWrite();
      await pending;
      assertDraft(expected, "A acknowledgement destroyed B's draft");
      assert(state.adviceReviewEditingPlanId === 8, "A changed B's editing identity");
      assert(state.adviceReviewEditingPlanSnapshot.revision === 2, "A changed B's revision");
      assert(elements.get("reviewAdviceId").value === "4", "readback changed B's snapshot identity");
      if (blank) elements.get("reviewHypothesis").value = "B补完的输入";
      const next = api.submitAdviceReviewPlan(state);
      assert(writes[1].url === "/api/reviews/plans/8", "next save targeted A instead of B");
      assert(writes[1].payload.expected_revision === 2, "B did not retain its own revision");
      acceptWrite();
      await next;
    ''')


@pytest.mark.parametrize("blank", [False, True])
def test_late_review_save_preserves_same_plan_edits_and_advances_next_patch_revision(blank: bool) -> None:
    _run_node(SAVE_PRELUDE + f"const blank = {str(blank).lower()};" + r'''
      const pending = api.submitAdviceReviewPlan(state);
      draft("A在保存期间继续输入");
      if (blank) {
        elements.get("reviewHypothesis").value = "";
        elements.get("reviewTarget").value = "";
      }
      const expected = values();
      acceptWrite();
      await pending;
      assertDraft(expected, "A acknowledgement destroyed subsequent edits or intentional blanks");
      assert(state.adviceReviewEditingPlanId === 7, "new A draft lost its editing identity");
      assert(state.adviceReviewEditingPlanSnapshot.revision === 3, "server revision was not rebased");
      if (blank) draft("补完后再保存");
      const next = api.submitAdviceReviewPlan(state);
      assert(writes[1].url === "/api/reviews/plans/7", "next save lost A's identity");
      assert(writes[1].payload.expected_revision === 3, "next PATCH did not use acknowledged revision");
      assert(writes[1].payload.target_price === 120, "next PATCH did not include the retained draft");
      acceptWrite();
      await next;
      assert(state.adviceReviewEditingPlanId === null, "unchanged second save did not reset");
    ''')


@pytest.mark.parametrize("transition", ["reopen", "cancel_reopen", "switch_back"])
def test_explicit_review_edit_identity_survives_even_when_fields_match(transition: str) -> None:
    _run_node(SAVE_PRELUDE + f"const transition = {transition!r};" + r'''
      const pending = api.submitAdviceReviewPlan(state);
      if (transition === "cancel_reopen") api.cancelAdviceReviewEdit(state);
      if (transition === "switch_back") api.beginAdviceReviewEdit(state, 8);
      api.beginAdviceReviewEdit(state, 7);
      const expected = values();
      acceptWrite();
      await pending;
      assertDraft(expected, "explicitly reopened draft was reset");
      assert(state.adviceReviewEditingPlanId === 7, "explicitly reopened editor was closed");
      const next = api.submitAdviceReviewPlan(state);
      assert(writes[1].payload.expected_revision === 3, "reopened same plan kept stale revision");
      acceptWrite();
      await next;
    ''')


def test_late_review_save_preserves_explicit_cancel_and_new_snapshot_draft() -> None:
    _run_node(SAVE_PRELUDE + r'''
      const pending = api.submitAdviceReviewPlan(state);
      api.cancelAdviceReviewEdit(state);
      elements.get("reviewAdviceId").value = "5";
      api.selectAdviceReviewSnapshot(state);
      draft("全新快照的草稿");
      const expected = values();
      acceptWrite();
      await pending;
      assertDraft(expected, "old save reset the new snapshot draft");
      assert(state.adviceReviewEditingPlanId === null, "cancelled editor reopened");
      const next = api.submitAdviceReviewPlan(state);
      assert(writes[1].method === "POST" && writes[1].payload.advice_id === 5, "new draft lost its POST owner");
      acceptWrite();
      await next;
    ''')


@pytest.mark.parametrize("switch_plan", [False, True])
def test_failed_review_save_preserves_latest_draft_and_unconfirmed_revision(switch_plan: bool) -> None:
    _run_node(SAVE_PRELUDE + f"const switchPlan = {str(switch_plan).lower()};" + r'''
      const pending = api.submitAdviceReviewPlan(state).catch(error => error);
      if (switchPlan) api.beginAdviceReviewEdit(state, 8);
      draft("失败期间的新输入");
      elements.get("reviewTrigger").value = "";
      const expected = values();
      releaseWrite(jsonResponse({ detail: "版本冲突" }, 409));
      assert(await pending instanceof Error, "failed save was accepted");
      assertDraft(expected, "failure erased the latest draft");
      assert(state.adviceReviewEditingPlanSnapshot.revision === 2, "failure advanced an unconfirmed revision");
      assert(state.adviceReviewEditingPlanId === (switchPlan ? 8 : 7), "failure changed editor identity");
    ''')


def test_unchanged_review_save_retains_original_reset_behavior() -> None:
    _run_node(SAVE_PRELUDE + r'''
      const pending = api.submitAdviceReviewPlan(state);
      acceptWrite();
      await pending;
      assert(state.adviceReviewEditingPlanId === null, "unchanged save retained editor");
      assert(elements.get("reviewAdviceId").value === "5", "reset did not choose an unplanned snapshot");
      assert(elements.get("reviewHypothesis").value === "默认假设5", "reset did not apply snapshot defaults");
    ''')


def test_create_acknowledgement_keeps_new_inputs_as_editable_saved_plan() -> None:
    _run_node(SAVE_PRELUDE + r'''
      api.cancelAdviceReviewEdit(state);
      draft("首次创建");
      const pending = api.submitAdviceReviewPlan(state);
      draft("创建期间继续输入");
      const expected = values();
      acceptWrite();
      await pending;
      assertDraft(expected, "POST acknowledgement erased newer draft");
      assert(state.adviceReviewEditingPlanId === 9, "confirmed POST did not bind the remaining draft");
      const next = api.submitAdviceReviewPlan(state);
      assert(writes[1].method === "PATCH" && writes[1].url.endsWith("/9"), "remaining draft repeated POST");
      assert(writes[1].payload.expected_revision === 1, "remaining draft lost new plan revision");
      acceptWrite();
      await next;
    ''')


@pytest.mark.parametrize("changed", [False, True])
@pytest.mark.parametrize("read_status", [200, 500])
def test_confirmed_create_survives_incomplete_independent_read(changed: bool, read_status: int) -> None:
    _run_node(SAVE_PRELUDE + f"const changed = {str(changed).lower()}; const readStatus = {read_status};" + r'''
      api.cancelAdviceReviewEdit(state);
      const originalFetch = globalThis.fetch;
      globalThis.fetch = (url, options = {}) => options.method
        ? originalFetch(url, options) : Promise.resolve(jsonResponse(readStatus === 200 ? []
          : { detail: "列表暂不可用" }, readStatus));
      draft("已提交的新计划");
      const pending = api.submitAdviceReviewPlan(state);
      if (changed) draft("另一次尚未保存的输入");
      const expected = values();
      const saved = acceptWrite();
      const result = await pending;
      assert(result.id === saved.id, "read failure erased the confirmed save result");
      assert(state.adviceReviewDetails.some(detail => detail.plan.id === saved.id), "confirmed plan was lost");
      if (changed) assertDraft(expected, "read failure erased the new draft");
      api.cancelAdviceReviewEdit(state);
      assert(elements.get("reviewAdviceId").value !== "5", "confirmed snapshot became available for duplicate POST");
      assert(elements.get("reviewAdviceId").innerHTML.includes('value="5" disabled'), "confirmed snapshot was selectable");
      if (readStatus === 500) assert(elements.get("reviewPlanSubmit").disabled, "fully planned snapshots still allowed POST");
    ''')


def test_confirmed_create_does_not_reappear_after_later_authoritative_empty_read() -> None:
    _run_node(SAVE_PRELUDE + r'''
      api.cancelAdviceReviewEdit(state);
      const pending = api.submitAdviceReviewPlan(state);
      const saved = acceptWrite();
      await pending;
      assert(state.adviceReviewDetails.some(detail => detail.plan.id === saved.id), "initial save was lost");
      globalThis.fetch = async () => jsonResponse([]);
      assert(await api.loadAdviceReviews(state), "later authoritative read failed");
      assert(state.adviceReviewDetails.length === 0, "confirmation became a permanent ghost plan");
    ''')


EVENT_PRELUDE = SAVE_PRELUDE + r'''
  const root = { getElementById(id) {
    if (!elements.has(id)) elements.set(id, { value: "", dataset: {}, innerHTML: "", textContent: "" });
    const element = elements.get(id);
    element.listeners ||= {};
    element.addEventListener = (type, handler) => { element.listeners[type] = handler; };
    element.removeEventListener = type => { delete element.listeners[type]; };
    element.setAttribute = (name, value) => { element[name] = String(value); };
    return element;
  } };
  globalThis.document = root;
  const { readFileSync } = await import("node:fs");
  const appSource = readFileSync("static/app.js", "utf8");
  const taskSource = appSource.slice(appSource.indexOf("async function runSubmitTask("),
    appSource.indexOf("function submitButton("));
  const runSubmitTask = new Function("submitButton", "setElementAttribute", `return (${taskSource})`)(
    () => elements.get("reviewPlanSubmit"), (element, name, value) => { element[name] = value; });
  const { bindAdviceReviewEvents } = await import("./static/js/advice-review-events.js");
  let contextGeneration = 0;
  bindAdviceReviewEvents({ root, state, runSubmitTask,
    currentWorkbenchMutationOptions: () => {
      const generation = contextGeneration;
      return { isCurrent: () => contextGeneration === generation };
    },
    setInlineFeedback: (id, error) => { elements.get(id).textContent = error.message; },
  });
  const form = elements.get("reviewPlanForm");
  const submit = () => form.listeners.submit({ preventDefault() {}, currentTarget: form });
'''


@pytest.mark.parametrize("transition", ["unchanged", "other_plan", "cancel", "all_planned"])
def test_review_submit_event_restores_current_controls_after_real_task_wrapper(transition: str) -> None:
    _run_node(EVENT_PRELUDE + f"const transition = {transition!r};" + r'''
      if (transition === "all_planned") api.cancelAdviceReviewEdit(state);
      const pending = submit();
      if (transition === "other_plan") api.beginAdviceReviewEdit(state, 8);
      if (transition === "cancel") api.cancelAdviceReviewEdit(state);
      if (["other_plan", "cancel"].includes(transition)) draft("保存期间切换后的输入");
      assert(elements.get("reviewPlanSubmit").disabled, "form transition released the pending submit");
      acceptWrite();
      await pending;
      const button = elements.get("reviewPlanSubmit");
      assert(button.textContent === (transition === "other_plan" ? "更新计划" : "建立计划"),
        "old task wrapper overwrote the current form mode");
      assert(button.disabled === (transition === "all_planned"), "task wrapper overwrote snapshot availability");
    ''')


def test_review_submit_event_retains_busy_ownership_while_switching_editor() -> None:
    _run_node(EVENT_PRELUDE + r'''
      const pending = submit();
      api.beginAdviceReviewEdit(state, 8);
      draft("可以编辑的B草稿");
      assert(elements.get("reviewPlanSubmit").disabled, "switching editor released in-flight submit ownership");
      await submit();
      assert(writes.length === 1, "second submit bypassed the in-flight owner");
      acceptWrite();
      await pending;
      assert(!elements.get("reviewPlanSubmit").disabled, "finished owner kept B locked");
      const next = submit();
      assert(writes[1].url.endsWith("/8") && writes[1].payload.expected_revision === 2,
        "next real form submission did not target B's revision");
      acceptWrite();
      await next;
      assert(state.adviceReviewEditingPlanId === null, "second successful unmodified form did not reset");
    ''')


def test_old_workbench_submit_completion_cannot_unlock_new_submit_owner() -> None:
    _run_node(EVENT_PRELUDE + r'''
      const oldPending = submit();
      contextGeneration += 1;
      api.beginAdviceReviewEdit(state, 8);
      draft("新工作区的B草稿");
      const expected = values();
      const newPending = submit();
      assert(writes.length === 2, "obsolete workbench blocked the new submit owner");
      assert(form["aria-busy"] === "true", "new submission did not expose its busy state");
      acceptWrite(0);
      await oldPending;
      assert(elements.get("reviewPlanSubmit").disabled, "old completion unlocked the new pending submit");
      assert(form["aria-busy"] === "true", "old completion cleared the current owner's accessible busy state");
      assertDraft(expected, "obsolete acknowledgement changed the new workbench draft");
      acceptWrite(1);
      await newPending;
      assert(!elements.get("reviewPlanSubmit").disabled, "new owner was not released on completion");
      assert(form["aria-busy"] === "false", "completed owner left the form accessibly busy");
      assert(state.adviceReviewEditingPlanId === null, "current completed write did not reset normally");
    ''')


def test_editing_frozen_snapshot_keeps_its_selector_identity_on_list_refresh() -> None:
    _run_node(SAVE_PRELUDE + r'''
      api.syncAdviceReviewSnapshots(state, [snapshot(5)], null);
      const pending = api.submitAdviceReviewPlan(state);
      draft("冻结快照仍可编辑");
      const expected = values();
      acceptWrite();
      await pending;
      assertDraft(expected, "missing recent snapshot erased the frozen plan draft");
      assert(elements.get("reviewAdviceId").value === "3", "frozen editing snapshot lost its selector identity");
      assert(elements.get("reviewAdviceId").innerHTML.includes('value="3"'), "frozen snapshot has no real select option");
    ''')


@pytest.mark.parametrize("archive", ["none", "page_row", "confirmed_row"])
def test_confirmation_outside_full_page_does_not_skip_next_server_row(archive: str) -> None:
    _run_node(SAVE_PRELUDE + f"const archive = {archive!r};" + r'''
      api.cancelAdviceReviewEdit(state);
      const page = Array.from({ length: 20 }, (_, index) => ({
        plan: completeReviewPlan({ id: 100 + index, advice_id: 1000 + index }), latest_evaluation: null,
      }));
      const originalFetch = globalThis.fetch;
      const reads = [];
      globalThis.fetch = (url, options = {}) => {
        if (options.method === "DELETE") return Promise.resolve(jsonResponse({ ok: true, removed: true }));
        if (options.method) return originalFetch(url, options);
        reads.push(String(url));
        return Promise.resolve(jsonResponse(reads.length === 1 ? page : [{
          plan: completeReviewPlan({ id: 120, advice_id: 1020 }), latest_evaluation: null,
        }]));
      };
      const pending = api.submitAdviceReviewPlan(state);
      const saved = acceptWrite();
      await pending;
      assert(state.adviceReviewDetails.length === 21, "full page lost its separate confirmation");
      if (archive !== "none") await api.deleteAdviceReviewPlan(state, archive === "page_row" ? 100 : saved.id);
      assert(await api.loadMoreAdviceReviews(state), "next page did not load");
      const expectedOffset = archive === "page_row" ? 19 : 20;
      assert(reads[1].endsWith(`&offset=${expectedOffset}`), "confirmation or archive corrupted raw-page offset");
      assert(state.adviceReviewDetails.some(detail => detail.plan.id === 120), "first next-page row was skipped");
      if (archive !== "none") assert(!state.adviceReviewDetails.some(detail => detail.plan.id ===
        (archive === "page_row" ? 100 : saved.id)), "archived row reappeared after page append");
    ''')
