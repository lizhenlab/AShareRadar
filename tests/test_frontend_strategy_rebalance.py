from __future__ import annotations

import json

import pytest

from tests.test_frontend_strategy_recovery import _run_node


@pytest.mark.parametrize("rebalance_sessions", [5, 10])
def test_editor_preserves_loaded_rebalance_interval_when_other_fields_change(
    rebalance_sessions: int,
) -> None:
    _run_node(r'''
      spec.rebalance_policy.hold_sessions = 10;
      spec.rebalance_policy.rebalance_every_sessions = INTERVAL;
      syncStrategyEditor(root, spec);
      assert.equal(Number(element("strategyHoldSessions").value), 10);
      assert.equal(Number(element("strategyRebalanceSessions").value), INTERVAL);
      assert.deepEqual(strategySpecFromEditor(root, spec), spec);
      element("strategyStockCount").value = "10";
      let edited = strategySpecFromEditor(root, spec);
      assert.equal(edited.portfolio_constraints.stock_count, 10);
      assert.equal(edited.rebalance_policy.rebalance_every_sessions, INTERVAL);
      element("strategyHoldSessions").value = "15";
      edited = strategySpecFromEditor(root, edited);
      assert.equal(edited.rebalance_policy.hold_sessions, 15);
      assert.equal(edited.rebalance_policy.rebalance_every_sessions, INTERVAL);
      assert.equal(spec.rebalance_policy.hold_sessions, 10);
    '''.replace("INTERVAL", str(rebalance_sessions)))


def test_editor_changes_rebalance_without_changing_holding_period() -> None:
    _run_node(r'''
      spec.rebalance_policy.hold_sessions = 10;
      spec.rebalance_policy.rebalance_every_sessions = 5;
      syncStrategyEditor(root, spec);
      element("strategyRebalanceSessions").value = "8";
      const edited = strategySpecFromEditor(root, spec);
      assert.equal(edited.rebalance_policy.hold_sessions, 10);
      assert.equal(edited.rebalance_policy.rebalance_every_sessions, 8);
      assert.equal(edited.rebalance_policy.cadence, "manual");
      assert.equal(spec.rebalance_policy.rebalance_every_sessions, 5);
    ''')


@pytest.mark.parametrize("field", ["strategyHoldSessions", "strategyRebalanceSessions"])
@pytest.mark.parametrize("value", ["", " ", "0", "61", "2.5", "NaN", "Infinity"])
def test_editor_rejects_invalid_session_counts(field: str, value: str) -> None:
    _run_node(r'''
      syncStrategyEditor(root, spec);
      element(FIELD).value = VALUE;
      assert.throws(() => strategySpecFromEditor(root, spec), /数值无效|整数交易日/);
    '''.replace("FIELD", json.dumps(field)).replace("VALUE", json.dumps(value)))


@pytest.mark.parametrize("missing_value", ["undefined", "null"])
def test_missing_loaded_rebalance_interval_does_not_inherit_holding_period(
    missing_value: str,
) -> None:
    _run_node(r'''
      spec.rebalance_policy.hold_sessions = 10;
      spec.rebalance_policy.rebalance_every_sessions = MISSING;
      syncStrategyEditor(root, spec);
      assert.equal(element("strategyRebalanceSessions").value, "");
      assert.throws(() => strategySpecFromEditor(root, spec), /strategyRebalanceSessions 数值无效/);
    '''.replace("MISSING", missing_value))


def test_missing_rebalance_control_fails_closed() -> None:
    _run_node(r'''
      syncStrategyEditor(root, spec);
      const incompleteRoot = { ...root, getElementById: id => id === "strategyRebalanceSessions" ? null : element(id) };
      assert.throws(() => strategySpecFromEditor(incompleteRoot, spec), /strategyRebalanceSessions 数值无效/);
    ''')
