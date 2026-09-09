"""A global review selection opens its exact plan without consuming a list page."""

from __future__ import annotations

import pytest

from tests.test_frontend_review_scan import _run_node


def test_dashboard_selection_fetches_the_exact_old_plan_and_keeps_pagination_offset() -> None:
    _run_node(SETUP + r'''
      const state = initialState();
      const calls = [];
      globalThis.fetch = async (url) => {
        calls.push(String(url));
        if (String(url) === "/api/reviews/plans/7") return json(detail(7));
        if (String(url).includes("offset=20")) return json([...page(121, 19), detail(7)]);
        if (String(url).includes("offset=40")) return json([]);
        throw new Error(`unexpected endpoint ${url}`);
      };
      bind(state);
      await selectPlan(7);
      assert(calls[0] === "/api/reviews/plans/7", "dashboard selection only loaded a stock, not its selected plan");
      assert(state.adviceReviewPinnedDetail.plan.id === 7, "old plan was not independently pinned");
      assert(state.adviceReviewDetails.length === 20, "pin consumed a normal pagination slot");
      assert(dom.focused === 7 && dom.scrolled === 7, "exact plan was not revealed accessibly");
      await reviews.loadMoreAdviceReviews(state);
      assert(state.adviceReviewDetails.length === 40, "normal list rows were lost while deduplicating the pin");
      assert(countPlan(7) === 1, "plan appeared twice when its normal page arrived");
      await reviews.loadMoreAdviceReviews(state);
      assert(calls.at(-1).includes("offset=40"), "next offset was based on rendered pinned rows");
      assert(!calls.some(url => url.includes("evaluate")), "navigation started an evaluation");
    ''')


def test_dashboard_button_carries_the_plan_identity_and_loaded_horizon() -> None:
    _run_node(SETUP + r'''
      const state = initialState();
      Object.assign(state, { adviceReviewDashboardDetails: [detail(7)] });
      reviews.updateAdviceReviewDashboardFilters(state);
      const html = el("reviewDashboardQueue").innerHTML;
      assert(html.includes('data-review-open-plan="7"'), "card lost its exact plan identity");
      assert(html.includes('data-review-open-symbol="600519.SH"'), "card lost its stock identity");
    ''')


def test_late_single_plan_read_cannot_replace_a_newer_selection() -> None:
    _run_node(SETUP + r'''
      const state = initialState();
      const first = deferred();
      globalThis.fetch = (url) => String(url).endsWith("/7") ? first.promise : Promise.resolve(json(detail(8)));
      const pending = reviews.loadAdviceReviewPlan(state, 7);
      assert(await reviews.loadAdviceReviewPlan(state, 8), "latest plan failed to load");
      first.resolve(json(detail(7)));
      assert(await pending === false, "late selection retained ownership");
      assert(state.adviceReviewPinnedDetail.plan.id === 8 && dom.focused === 8, "late plan replaced current focus");
    ''')


def test_late_stock_load_does_not_dispatch_an_old_plan_request() -> None:
    _run_node(SETUP + r'''
      const state = initialState();
      const first = deferred();
      const calls = [];
      let loads = 0;
      globalThis.fetch = async (url) => { calls.push(String(url)); return json(detail(8, "000001.SZ")); };
      bind(state, { loadAll: () => ++loads === 1 ? first.promise : Promise.resolve(true) });
      const oldSelection = selectPlan(7);
      await selectPlan(8, "000001.SZ");
      first.resolve(true);
      await oldSelection;
      assert(calls.length === 1 && calls[0].endsWith("/8"), "stale stock load dispatched the old plan");
      assert(state.adviceReviewPinnedDetail.plan.symbol === "000001.SZ", "stock ownership was lost");
    ''')


@pytest.mark.parametrize("status", [404, 503])
def test_missing_or_temporarily_unavailable_plan_can_be_retried(status: int) -> None:
    _run_node(SETUP + f"const status = {status};\n" + r'''
      const state = initialState();
      globalThis.fetch = async () => json({ detail: "server-private-detail" }, status);
      assert(await reviews.loadAdviceReviewPlan(state, 7) === false, "failed lookup claimed success");
      assert(!state.adviceReviewPinnedDetail, "failed lookup invented a pinned plan");
      const feedback = el("reviewPlanFeedback").textContent;
      assert(feedback.includes(status === 404 ? "不存在或已归档" : "重试"), "failure has no useful recovery message");
      assert(!feedback.includes("server-private-detail"), "private failure details were exposed");
      globalThis.fetch = async () => json(detail(7));
      assert(await reviews.loadAdviceReviewPlan(state, 7), "read-only retry did not recover");
    ''')


@pytest.mark.parametrize("mismatch", ["id", "symbol"])
def test_single_plan_response_must_match_requested_identity(mismatch: str) -> None:
    _run_node(SETUP + f'const mismatch = "{mismatch}";\n' + r'''
      const state = initialState();
      globalThis.fetch = async () => json(mismatch === "id" ? detail(8) : detail(7, "000001.SZ"));
      assert(await reviews.loadAdviceReviewPlan(state, 7) === false, "unbound response was accepted");
      assert(!state.adviceReviewPinnedDetail && !dom.focused, "unbound plan became actionable");
    ''')


def test_pin_survives_normal_list_completion_and_failed_list_refresh() -> None:
    _run_node(SETUP + r'''
      const state = initialState();
      const list = deferred();
      globalThis.fetch = (url) => String(url).includes("/plans/") ? Promise.resolve(json(detail(7))) : list.promise;
      const pendingList = reviews.loadAdviceReviews(state);
      await reviews.loadAdviceReviewPlan(state, 7);
      list.resolve(json(page(101, 20)));
      await pendingList;
      assert(countPlan(7) === 1 && state.adviceReviewDetails.length === 20, "list completion erased the independent pin");
      globalThis.fetch = async () => json({ detail: "unavailable" }, 503);
      await reviews.loadAdviceReviews(state);
      assert(countPlan(7) === 1, "failed normal list read hid the confirmed selected plan");
    ''')


def test_pinned_plan_edit_uses_its_current_revision_and_updates_the_pin() -> None:
    _run_node(SETUP + r'''
      const state = initialState();
      const original = detail(7, state.symbol, 3);
      const updated = detail(7, state.symbol, 4);
      const writes = [];
      globalThis.fetch = async (url, options = {}) => {
        if (options.method === "PATCH") { writes.push({ url, body: JSON.parse(options.body) }); return json(updated.plan); }
        return json(String(url).includes("/plans/") ? original : page(101, 20));
      };
      await reviews.loadAdviceReviewPlan(state, 7);
      assert(reviews.beginAdviceReviewEdit(state, 7), "pinned plan was not editable");
      const result = await reviews.submitAdviceReviewPlan(state);
      assert(writes[0].url.endsWith("/7") && writes[0].body.expected_revision === 3, "edit lost revision CAS");
      assert(result.revision === 4 && state.adviceReviewPinnedDetail.plan.revision === 4, "saved pin kept the old revision");
      assert(state.adviceReviewEditingPlanSnapshot === null, "successful edit retained its draft identity");
      assert(state.adviceReviewDetails.length === 20 && countPlan(7) === 1, "edit refresh corrupted pagination");
    ''')


def test_archive_acknowledgement_invalidates_only_its_pending_plan_lookup() -> None:
    _run_node(SETUP + r'''
      const state = initialState();
      state.adviceReviewDetails = [detail(7), detail(8)];
      const pending = deferred();
      globalThis.fetch = async (_url, options = {}) => options.method === "DELETE"
        ? json({ ok: true, removed: true }) : pending.promise;
      const lookup = reviews.loadAdviceReviewPlan(state, 7);
      await reviews.deleteAdviceReviewPlan(state, 7, { confirm: () => true });
      pending.resolve(json(detail(7)));
      assert(await lookup === false && !state.adviceReviewPinnedDetail, "late lookup resurrected an archived plan");
      assert(countPlan(7) === 0, "archived plan became actionable again");
      const other = deferred();
      state.adviceReviewDetails = [detail(7), detail(8)];
      globalThis.fetch = async (_url, options = {}) => options.method === "DELETE"
        ? json({ ok: true, removed: true }) : other.promise;
      const otherLookup = reviews.loadAdviceReviewPlan(state, 8);
      await reviews.deleteAdviceReviewPlan(state, 7, { confirm: () => true });
      other.resolve(json(detail(8)));
      assert(await otherLookup && state.adviceReviewPinnedDetail.plan.id === 8, "archive invalidated another plan's lookup");
    ''')


def test_page_refresh_does_not_change_the_revision_of_an_open_edit() -> None:
    _run_node(SETUP + r'''
      const state = initialState();
      let patchedRevision;
      globalThis.fetch = async (url, options = {}) => {
        if (options.method === "PATCH") { patchedRevision = JSON.parse(options.body).expected_revision; return json({ detail: "版本冲突" }, 409); }
        return json(String(url).includes("/plans/") ? detail(7, state.symbol, 3) : [detail(7, state.symbol, 4)]);
      };
      await reviews.loadAdviceReviewPlan(state, 7);
      reviews.beginAdviceReviewEdit(state, 7);
      await reviews.loadAdviceReviews(state);
      assert(state.adviceReviewPinnedDetail.plan.revision === 4, "new current version was ignored");
      let failed = false;
      try { await reviews.submitAdviceReviewPlan(state); } catch { failed = true; }
      assert(failed && patchedRevision === 3, "page refresh silently adopted a new CAS revision for an old draft");
      reviews.cancelAdviceReviewEdit(state);
      assert(state.adviceReviewEditingPlanSnapshot === null, "cancelled edit retained its draft identity");
    ''')


def test_failed_quote_switch_still_opens_the_local_plan_read_only_and_can_recover() -> None:
    _run_node(SETUP + r'''
      const state = initialState();
      state.loadSeq = 10;
      let quoteAvailable = false;
      let reads = 0;
      globalThis.fetch = async () => { reads += 1; return json(detail(7, "000001.SZ")); };
      bind(state, { loadAll: async () => {
        state.loadSeq += 1;
        if (quoteAvailable) return true;
        state.failedLoadSymbol = state.symbol;
        state.symbol = "600519.SH";
        return false;
      } });
      await selectPlan(7, "000001.SZ");
      assert(reads === 1 && state.symbol === "600519.SH", "local plan depended on quote success or replaced old market context");
      assert(state.adviceReviewPinnedReadOnly === true && countPlan(7) === 1, "local plan did not enter explicit read-only view");
      assert(el("reviewPlanList").innerHTML.includes("000001.SZ") && el("reviewPlanFeedback").textContent.includes("行情暂不可用"), "read-only stock identity was ambiguous");
      assert(!reviews.beginAdviceReviewEdit(state, 7), "read-only plan became an editable draft");
      quoteAvailable = true;
      await selectPlan(7, "000001.SZ");
      assert(state.symbol === "000001.SZ" && state.adviceReviewPinnedReadOnly === false, "same plan could not recover through a successful quote retry");
      assert(reviews.beginAdviceReviewEdit(state, 7), "recovered plan remained locked");
    ''')


def test_read_only_pin_rejects_all_domain_writes_including_a_cached_edit() -> None:
    _run_node(SETUP + r'''
      const state = initialState();
      globalThis.fetch = async () => json(detail(7));
      await reviews.loadAdviceReviewPlan(state, 7);
      reviews.beginAdviceReviewEdit(state, 7);
      await reviews.loadAdviceReviewPlan(state, 7, { contextAvailable: false });
      assert(!state.adviceReviewEditingPlanSnapshot && !state.adviceReviewEditingPlanId,
        "read-only pin retained the previously editable snapshot");
      assert(el("reviewPlanSubmit").disabled, "read-only pin left the edit submit enabled");
      let writes = 0;
      globalThis.fetch = async () => { writes += 1; throw new Error("unexpected write"); };
      const actions = [() => reviews.submitAdviceReviewPlan(state),
        () => reviews.evaluateAdviceReviewPlan(state, 7),
        () => reviews.deleteAdviceReviewPlan(state, 7, { confirm: () => true })];
      for (const action of actions) { let rejected = false; try { await action(); } catch { rejected = true; } assert(rejected, "read-only action was accepted"); }
      assert(writes === 0, "read-only plan reached a write endpoint");
    ''')


def test_archive_acknowledgement_invalidates_a_pending_normal_list() -> None:
    _run_node(SETUP + r'''
      const state = initialState();
      state.adviceReviewDetails = [detail(7)];
      const pending = deferred();
      globalThis.fetch = async (url, options = {}) => {
        if (options.method === "DELETE") return json({ ok: true, removed: true });
        return String(url).includes("/plans/") ? json(detail(7)) : pending.promise;
      };
      const list = reviews.loadAdviceReviews(state);
      await reviews.loadAdviceReviewPlan(state, 7);
      await reviews.deleteAdviceReviewPlan(state, 7, { confirm: () => true });
      pending.resolve(json([detail(7)]));
      assert(await list === false && countPlan(7) === 0, "pre-archive normal list resurrected the archived plan");
      assert(state.adviceReviewDetails.length === 0 && !state.adviceReviewPinnedDetail, "archived data returned to state");
    ''')


@pytest.mark.parametrize("field", ["plan_payload_digest", "target_price"])
def test_same_revision_lookup_rejects_a_conflicting_frozen_plan(field: str) -> None:
    _run_node(SETUP + f'const field = "{field}";\n' + r'''
      const state = initialState();
      state.adviceReviewDetails = [detail(7)];
      const conflicting = detail(7);
      conflicting.plan[field] = field === "target_price" ? 120 : "b".repeat(64);
      globalThis.fetch = async () => json(conflicting);
      assert(await reviews.loadAdviceReviewPlan(state, 7) === false, "same revision changed its frozen identity");
      assert(!state.adviceReviewPinnedDetail && state.adviceReviewDetails[0].plan.target_price === 110,
        "conflicting lookup replaced the known plan");
      assert(el("reviewDashboardFeedback").textContent.includes("同版本"), "conflict was not visible from the selected card");
    ''')


def test_stock_context_change_discards_pin_and_edit_identity() -> None:
    _run_node(SETUP + r'''
      const state = initialState();
      globalThis.fetch = async (url) => json(String(url).includes("/plans/") ? detail(7) : []);
      await reviews.loadAdviceReviewPlan(state, 7);
      reviews.beginAdviceReviewEdit(state, 7);
      state.symbol = "000001.SZ";
      await reviews.loadAdviceReviews(state);
      assert(!state.adviceReviewPinnedDetail && !state.adviceReviewEditingPlanId && !state.adviceReviewEditingPlanSnapshot,
        "stock switch retained an old pinned/edit identity");
      assert(countPlan(7) === 0, "old stock pin remained visible");
    ''')


def test_a_b_a_reload_invalidates_the_first_navigation_by_load_sequence() -> None:
    _run_node(SETUP + r'''
      const state = initialState();
      state.loadSeq = 1;
      const pending = deferred();
      let reads = 0;
      globalThis.fetch = async () => { reads += 1; return json(detail(7)); };
      bind(state, { loadAll() { state.loadSeq += 1; return pending.promise; } });
      const lookup = selectPlan(7);
      state.symbol = "000001.SZ";
      state.loadSeq += 1;
      state.symbol = "600519.SH";
      state.loadSeq += 1;
      pending.resolve(true);
      await lookup;
      assert(reads === 0 && !state.adviceReviewPinnedDetail, "A-B-A navigation reused an earlier request context");
    ''')


def test_late_normal_page_redraw_preserves_the_currently_focused_plan_without_rescrolling() -> None:
    _run_node(SETUP + FOCUS_DOM + r'''
      const state = initialState();
      const list = deferred();
      globalThis.fetch = (url) => String(url).includes("/plans/") ? Promise.resolve(json(detail(7))) : list.promise;
      const loading = reviews.loadAdviceReviews(state);
      await reviews.loadAdviceReviewPlan(state, 7);
      const original = document.activeElement;
      assert(original.dataset.reviewPlan === "7", "explicit navigation did not focus the card");
      list.resolve(json(page(101, 20)));
      assert(await loading, "normal page did not complete");
      assert(document.activeElement !== original && document.activeElement.dataset.reviewPlan === "7",
        "late normal page redraw erased focus from the revealed plan");
      assert(document.activeElement.attributes.tabindex === "-1", "replacement card is not focusable");
      assert(focusCalls.at(-1).preventScroll === true && scrollCalls.length === 1,
        "restoring focus caused another scroll or reveal");
      assert(state.adviceReviewNextOffset === 20 && countPlan(7) === 1, "focus repair changed pagination");
    ''')


@pytest.mark.parametrize("focus_target", ["input", "button", "card_child", "foreign_card"])
def test_late_page_redraw_does_not_reclaim_focus_after_the_user_moves_it(focus_target: str) -> None:
    _run_node(SETUP + FOCUS_DOM + f'const focusTarget = "{focus_target}";' + r'''
      const state = initialState();
      const list = deferred();
      globalThis.fetch = (url) => String(url).includes("/plans/") ? Promise.resolve(json(detail(7))) : list.promise;
      const loading = reviews.loadAdviceReviews(state);
      await reviews.loadAdviceReviewPlan(state, 7);
      const control = { dataset: focusTarget === "foreign_card" ? { reviewPlan: "7" } : {},
        parent: focusTarget === "card_child" ? document.activeElement : null };
      document.activeElement = control;
      const focusCount = focusCalls.length;
      list.resolve(json(page(101, 20)));
      await loading;
      assert(focusCalls.length === focusCount, "late list update stole focus back to the pin");
      if (focusTarget !== "card_child") assert(document.activeElement === control, "outside control lost focus");
    ''')


def test_redraw_preserves_the_focused_ordinary_card_instead_of_selecting_the_pin() -> None:
    _run_node(SETUP + FOCUS_DOM + r'''
      const state = initialState();
      globalThis.fetch = async () => json(detail(7));
      await reviews.loadAdviceReviewPlan(state, 7);
      el("reviewPlanList").querySelector('[data-review-plan="101"]').focus({ preventScroll: true });
      reviews.renderAdviceReviewDetails(state.adviceReviewDetails, state);
      assert(document.activeElement.dataset.reviewPlan === "101", "redraw moved focus to the selected pin");
      assert(scrollCalls.length === 1, "ordinary card redraw invoked reveal scrolling");
    ''')


def test_redraw_does_not_restore_a_plan_that_disappeared() -> None:
    _run_node(SETUP + FOCUS_DOM + r'''
      const state = initialState();
      reviews.renderAdviceReviewDetails(state.adviceReviewDetails, state);
      el("reviewPlanList").querySelector('[data-review-plan="101"]').focus({ preventScroll: true });
      const focusCount = focusCalls.length;
      reviews.renderAdviceReviewDetails([], state);
      assert(document.activeElement === document.body && focusCalls.length === focusCount,
        "removed plan was restored or another card was focused");
    ''')


FOCUS_DOM = r'''
  // Model native innerHTML replacement: detached focused descendants yield focus to body.
  const target = el("reviewPlanList");
  let rendered = "";
  let cards = new Map();
  const focusCalls = [], scrollCalls = [];
  document.body = { dataset: {} };
  document.activeElement = document.body;
  target.contains = (node) => [...cards.values()].some(card => card === node || card === node?.parent);
  Object.defineProperty(target, "innerHTML", { get: () => rendered, set(value) {
    if (target.contains(document.activeElement)) document.activeElement = document.body;
    rendered = value;
    cards = new Map([...value.matchAll(/data-review-plan="(\d+)"/g)].map((match) => {
      const id = match[1];
      const card = { dataset: { reviewPlan: id }, attributes: {},
        setAttribute(name, value) { this.attributes[name] = value; },
        focus(options) { document.activeElement = this; focusCalls.push(options); },
        scrollIntoView(options) { scrollCalls.push(options); }, closest() { return null; } };
      return [id, card];
    }));
  } });
  target.querySelector = selector => cards.get(selector.match(/data-review-plan="(\d+)"/)?.[1]) || null;
'''


SETUP = r'''
  import * as reviews from "./static/js/advice-reviews.js";
  import { bindAdviceReviewEvents } from "./static/js/advice-review-events.js";
  const dom = { elements: new Map(), focused: null, scrolled: null };
  function el(id) {
    if (!dom.elements.has(id)) dom.elements.set(id, { value: "", innerHTML: "", textContent: "", hidden: false,
      dataset: {}, disabled: false, listeners: {}, setAttribute() {}, focus() {},
      addEventListener(type, fn) { this.listeners[type] = fn; }, removeEventListener() {},
      querySelector(selector) {
        const id = Number(selector.match(/data-review-plan="(\d+)"/)?.[1]);
        if (!id || !this.innerHTML.includes(`data-review-plan="${id}"`)) return null;
        return { setAttribute() {}, focus() { dom.focused = id; }, scrollIntoView() { dom.scrolled = id; }, closest() { return null; } };
      },
    });
    return dom.elements.get(id);
  }
  globalThis.document = { getElementById: el };
  el("reviewDashboardStatus").value = "all";
  el("reviewDashboardHorizon").value = "all";
  function detail(id, symbol = "600519.SH", revision = 1) {
    return { plan: completeReviewPlan({ id, advice_id: id + 1000, symbol, revision }), latest_evaluation: null };
  }
  function page(start, count) { return Array.from({ length: count }, (_, index) => detail(start + index)); }
  function initialState() {
    // Ordinary rows represent one completed server page, separately from the pin.
    return { symbol: "600519.SH", adviceReviewHistorySymbol: "600519.SH", adviceReviewDetails: page(101, 20),
      adviceReviewNextOffset: 20, adviceReviewPagePlanIds: page(101, 20).map(detail => detail.plan.id),
      adviceReviewHasMore: true, adviceReviewSnapshots: [] };
  }
  function bind(state, overrides = {}) {
    return bindAdviceReviewEvents({ state, setActiveSymbol: (symbol) => { state.symbol = symbol; },
      setWorkspaceView() {}, loadAll: async () => true,
      currentWorkbenchMutationOptions() { const symbol = state.symbol; return { symbol, isCurrent: () => state.symbol === symbol }; },
      runButtonTask: async (_button, task) => task(),
      setInlineFeedback: (id, error) => { el(id).textContent = error.message; }, ...overrides });
  }
  function selectPlan(id, symbol = "600519.SH") {
    const button = { dataset: { reviewOpenPlan: String(id), reviewOpenSymbol: symbol } };
    return el("reviewDashboardQueue").listeners.click({ target: { closest: () => button } });
  }
  function countPlan(id) { return el("reviewPlanList").innerHTML.split(`data-review-plan="${id}"`).length - 1; }
  function json(payload, status = 200) { return new Response(JSON.stringify(payload), { status, headers: { "Content-Type": "application/json" } }); }
  function deferred() { let resolve; const promise = new Promise(done => { resolve = done; }); return { promise, resolve }; }
  function assert(condition, message) { if (!condition) throw new Error(message); }
'''
