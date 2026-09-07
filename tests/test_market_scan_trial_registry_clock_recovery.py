from __future__ import annotations

from datetime import timedelta

import pytest

from app.services import market_scan_trial_registry as registry
from app.services.market_scan_trial_registry_contract import TrialRegistryError
from tests.test_market_scan_trial_registry import contract as contract_fixture, frozen_clock as clock_fixture


contract = contract_fixture
frozen_clock = clock_fixture


def test_backward_seal_clock_rejected_before_immutable_publication_can_retry(tmp_path, contract, frozen_clock, monkeypatch) -> None:
    registry.create_trial_registry(tmp_path, "clock-recovery", contract)
    for trial in contract["trials"]:
        registry.start_trial(tmp_path, "clock-recovery", trial["trial_id"])
        registry.finish_trial(tmp_path, "clock-recovery", trial["trial_id"], status="cancelled", reason="test cancellation")
    before = registry.load_trial_registry(tmp_path, "clock-recovery")
    monkeypatch.setattr(registry, "utc_now", lambda: frozen_clock - timedelta(seconds=1))
    with pytest.raises(TrialRegistryError, match="seal clock moved backwards"):
        registry.seal_trial_registry(tmp_path, "clock-recovery")
    assert not (tmp_path / "clock-recovery" / "seal.json").exists()
    assert registry.load_trial_registry(tmp_path, "clock-recovery") == before
    monkeypatch.setattr(registry, "utc_now", lambda: frozen_clock + timedelta(seconds=1))
    registry.seal_trial_registry(tmp_path, "clock-recovery")
    verified = registry.verify_trial_registry(tmp_path, "clock-recovery")
    assert verified["declared_family_complete"] is True
    assert verified["statuses"] == {trial["trial_id"]: "cancelled" for trial in contract["trials"]}
