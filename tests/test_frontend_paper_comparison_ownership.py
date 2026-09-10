"""Run comparison reads cannot overwrite newer paper-trading interactions."""

from __future__ import annotations

import pytest

from tests.test_frontend_paper_request_ownership import _run_node


_SETUP = r'''
for (const id of ["paperCompareLeft", "paperCompareRight", "paperRunComparison"]) {
  elements.set(id, {value: "", innerHTML: "", textContent: "", dataset: {}});
}
elements.get("paperCompareLeft").value = "1";
elements.get("paperCompareRight").value = "2";
function comparison(left=1, right=2) {
  return {left_run: dashboard(left).runs[0], right_run: dashboard(right).runs[0],
    left_performance: {}, right_performance: {}, deltas: {}};
}
'''


@pytest.mark.parametrize("dashboard_failure", [False, True])
@pytest.mark.parametrize("comparison_failure", [False, True])
def test_new_history_request_owns_feedback_after_an_older_comparison(dashboard_failure, comparison_failure):
    _run_node(_SETUP + r'''
      const pending = deferResponse();
      let signal;
      globalThis.fetch = (url, options) => {
        if (url.includes("/runs/compare?")) { signal = options.signal; return pending.promise; }
        return Promise.resolve(DASHBOARD_FAILURE ? json({detail: "history failed"}, 503) : json(dashboard(3)));
      };
      const previous = {marker: "previous comparison"};
      const state = {paperTradingComparison: previous};
      const old = paper.comparePaperTradingRuns(state).then(value => ({value}), error => ({error}));
      assert.equal(await paper.selectPaperTradingRun(state, 3), !DASHBOARD_FAILURE);
      const feedback = {...elements.get("paperTradingFeedback")};
      pending.resolve(COMPARISON_FAILURE ? json({detail: "old comparison failed"}, 503) : json(comparison()));
      assert.deepEqual(await old, {value: false}, "stale comparison did not become a silent read cancellation");
      assert.equal(signal.aborted, true, "new dashboard left obsolete comparison running");
      assert.equal(state.paperTradingComparison, previous);
      assert.equal(elements.get("paperTradingFeedback").textContent, feedback.textContent);
      assert.equal(elements.get("paperTradingFeedback").dataset.tone, feedback.dataset.tone);
    '''.replace("DASHBOARD_FAILURE", str(dashboard_failure).lower()).replace("COMPARISON_FAILURE", str(comparison_failure).lower()))


@pytest.mark.parametrize("changed_side", ["paperCompareLeft", "paperCompareRight"])
@pytest.mark.parametrize("failure", [False, True])
def test_changed_comparison_selection_rejects_old_success_and_failure(changed_side, failure):
    _run_node(_SETUP + r'''
      const pending = deferResponse();
      globalThis.fetch = () => pending.promise;
      const state = {};
      const old = paper.comparePaperTradingRuns(state).then(value => ({value}), error => ({error}));
      elements.get(CHANGED_SIDE).value = "3";
      pending.resolve(FAILURE ? json({detail: "old comparison failed"}, 503) : json(comparison()));
      assert.deepEqual(await old, {value: false});
      assert.equal(state.paperTradingComparison, undefined);
      assert.equal(elements.get("paperTradingFeedback").textContent, "");
    '''.replace("CHANGED_SIDE", repr(changed_side)).replace("FAILURE", str(failure).lower()))


def test_repeated_comparison_keeps_only_the_newest_request_and_normal_success():
    _run_node(_SETUP + r'''
      const pending = deferResponse();
      const signals = [];
      globalThis.fetch = (url, options) => {
        signals.push(options.signal);
        return signals.length === 1 ? pending.promise : Promise.resolve(json(comparison(1, 3)));
      };
      const state = {};
      const first = paper.comparePaperTradingRuns(state);
      elements.get("paperCompareRight").value = "3";
      const second = await paper.comparePaperTradingRuns(state);
      assert.equal(second.right_run.id, 3);
      assert.equal(signals[0].aborted, true);
      assert.equal(signals[1].aborted, false);
      pending.resolve(json(comparison()));
      assert.equal(await first, false);
      assert.equal(state.paperTradingComparison, second);
      assert.match(elements.get("paperTradingFeedback").textContent, /#1 与 #3/);
      assert.match(elements.get("paperRunComparison").innerHTML, /#3/);
    ''')


def test_current_comparison_failure_still_reaches_the_caller():
    _run_node(_SETUP + r'''
      globalThis.fetch = async () => json({detail: "current comparison failed"}, 503);
      await assert.rejects(paper.comparePaperTradingRuns({}), /current comparison failed/);
    ''')


def test_cancelling_comparison_does_not_cancel_an_existing_paper_write():
    _run_node(_SETUP + r'''
      const pendingWrite = deferResponse();
      const pendingComparison = deferResponse();
      let writeSignal;
      globalThis.fetch = (url, options) => {
        if (url.includes("/runs/compare?")) return pendingComparison.promise;
        if (options.method) { writeSignal = options.signal; return pendingWrite.promise; }
        return Promise.resolve(json(dashboard(3)));
      };
      const state = initialState();
      const write = paper.updatePaperTradingAccount(state);
      const read = paper.comparePaperTradingRuns(state);
      paper.cancelPaperTradingComparison(state);
      assert.equal(await read, false);
      assert.equal(writeSignal.aborted, false);
      pendingWrite.resolve(json({ok:true}));
      assert.ok(await write);
      assert.equal(state.paperTradingDashboard.selected_run_id, 3);
      assert.equal(writeSignal.aborted, false);
      pendingComparison.resolve(json(comparison()));
    ''')


def test_actual_select_change_cancels_comparison_even_when_the_same_pair_is_restored():
    _run_node(_SETUP + r'''
      const {createAppHarness} = await import("./tests/frontend_app_flow_helpers.mjs");
      const {element, __appTest} = await createAppHarness();
      element("paperCompareLeft").value = "1";
      element("paperCompareRight").value = "2";
      const pending = deferResponse();
      let signal;
      globalThis.fetch = (_url, options) => { signal = options.signal; return pending.promise; };
      const button = element("comparePaperRuns");
      const old = button.listeners.click({currentTarget:button});
      element("paperCompareLeft").value = "3";
      element("paperCompareLeft").listeners.change();
      element("paperCompareLeft").value = "1";
      element("paperCompareLeft").listeners.change();
      assert.equal(signal.aborted, true);
      await old;
      assert.equal(button.disabled, false);
      globalThis.fetch = async () => json(comparison());
      await button.listeners.click({currentTarget:button});
      const feedback = element("paperTradingFeedback").textContent;
      pending.resolve(json({detail:"abandoned comparison failed"}, 503));
      await Promise.resolve();
      assert.equal(element("paperTradingFeedback").textContent, feedback);
      assert.match(feedback, /#1 与 #2/);
      __appTest.destroyStockSearchBindings();
    ''')
