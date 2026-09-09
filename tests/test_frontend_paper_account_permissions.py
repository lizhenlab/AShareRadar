from __future__ import annotations

import pytest

from tests.test_frontend_paper_request_ownership import _run_node


SETUP = r'''
for (const id of ["paperDefaultCostProfile", "savePaperAccount", "paperStrategyList"]) {
  elements.set(id, {innerHTML: "", textContent: "", value: "", dataset: {}, disabled: false});
}
function strategy(status = "pending", id = 7) {
  return {id, plan_id: 10, plan_revision: 1, advice_id: 20, symbol: "600519.SH",
    plan_payload_digest: "a".repeat(64), allocation_pct: 25, status};
}
function currentDashboard(mode) {
  const value = dashboard(1);
  value.account.default_cost_profile = "base";
  if (mode !== "empty-run") value.strategies = [strategy()];
  if (mode === "new" || mode === "pending") {
    value.selected_run_id = null;
    value.runs = [];
  }
  if (mode === "new") value.strategies = [];
  value.runs.forEach((run) => { run.strategy_count = value.strategies.length; });
  return value;
}
'''


@pytest.mark.parametrize("mode", ["pending", "empty-run", "historical"])
def test_locked_cash_does_not_block_default_cost_updates(mode: str) -> None:
    _run_node(SETUP + r'''
      const value = currentDashboard(MODE);
      const state = {paperTradingDashboard: value};
      paper.renderPaperTradingDashboard(state);
      assert.equal(elements.get("paperInitialCash").disabled, true);
      assert.equal(elements.get("savePaperAccount").textContent, "保存默认成本");
      elements.get("paperInitialCash").value = "invalid-disabled-value";
      elements.get("paperDefaultCostProfile").value = "stress";
      const writes = [];
      globalThis.fetch = async (url, options) => {
        if (!options.method) return json(value);
        const payload = JSON.parse(options.body);
        writes.push(payload);
        if ("initial_cash" in payload) return json({detail: "初始资金已冻结"}, 400);
        value.account.default_cost_profile = payload.default_cost_profile;
        return json(value.account);
      };
      await paper.updatePaperTradingAccount(state);
      assert.deepEqual(writes, [{default_cost_profile: "stress"}]);
      assert.match(elements.get("paperTradingFeedback").textContent, /默认成本已保存/);
    '''.replace("MODE", repr(mode)))


def test_new_account_can_save_cash_and_cost_together() -> None:
    _run_node(SETUP + r'''
      const value = currentDashboard("new");
      const state = {paperTradingDashboard: value};
      paper.renderPaperTradingDashboard(state);
      assert.equal(elements.get("paperInitialCash").disabled, false);
      elements.get("paperInitialCash").value = "2000000";
      elements.get("paperDefaultCostProfile").value = "conservative";
      let payload;
      globalThis.fetch = async (url, options) => {
        if (!options.method) return json(value);
        payload = JSON.parse(options.body);
        return json(Object.assign(value.account, payload));
      };
      await paper.updatePaperTradingAccount(state);
      assert.deepEqual(payload, {initial_cash: 2000000, default_cost_profile: "conservative"});
      assert.match(elements.get("paperTradingFeedback").textContent, /账户配置已保存/);
    ''')


@pytest.mark.parametrize("status", ["pending", "skipped", "expired", "data_unavailable"])
def test_historical_unfilled_strategy_is_not_offered_or_sent_for_deletion(status: str) -> None:
    _run_node(SETUP + r'''
      const value = currentDashboard("historical");
      value.strategies = [strategy(STATUS)];
      const state = {paperTradingDashboard: value};
      paper.renderPaperTradingDashboard(state);
      assert.doesNotMatch(elements.get("paperStrategyList").innerHTML, /data-paper-delete/);
      assert.match(elements.get("paperStrategyList").innerHTML, /不可变历史运行.*不能删除/);
      globalThis.fetch = async () => { throw new Error("historical deletion was dispatched"); };
      await assert.rejects(paper.deletePaperStrategy(state, 7), /不可变历史运行.*不能删除/);
    '''.replace("STATUS", repr(status)))


def test_unrecorded_pending_strategy_remains_deletable() -> None:
    _run_node(SETUP + r'''
      const value = currentDashboard("pending");
      const state = {paperTradingDashboard: value};
      paper.renderPaperTradingDashboard(state);
      assert.match(elements.get("paperStrategyList").innerHTML, /data-paper-delete="7"/);
      let confirmation;
      const writes = [];
      globalThis.fetch = async (url, options) => {
        if (!options.method) return json(currentDashboard("new"));
        writes.push([url, options.method]);
        return json({ok: true});
      };
      await paper.deletePaperStrategy(state, 7, {confirm: (message) => {
        confirmation = message; return true;
      }});
      assert.match(confirmation, /尚未进入历史运行/);
      assert.deepEqual(writes, [["/api/paper-trading/strategies/7", "DELETE"]]);
    ''')


def test_historical_view_does_not_invent_membership_for_an_unseen_strategy() -> None:
    _run_node(SETUP + r'''
      const value = currentDashboard("historical");
      let deleted;
      globalThis.fetch = async (url, options) => {
        if (!options.method) return json(value);
        deleted = url;
        return json({ok: true});
      };
      await paper.deletePaperStrategy({paperTradingDashboard: value}, 8);
      assert.equal(deleted, "/api/paper-trading/strategies/8");
    ''')
