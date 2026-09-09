from __future__ import annotations

from copy import deepcopy
from dataclasses import replace
from datetime import date, datetime, timedelta
from pathlib import Path

import pytest

from app.artifacts.io import canonical_json_bytes
from app.services import market_scan_joint_execution_outcomes as outcomes
from app.services.market_scan_joint_execution_source import (
    build_joint_execution_source_artifact,
    replay_and_verify_joint_execution_source_artifact,
)
from app.services.trading_calendar import TradeCalendarSource, TradingCalendarCoverageError, next_trade_dates
from tests.test_market_scan_probability_source import (
    QUOTE_DATE,
    _compact_source_coverage_contract as _compact_source_coverage_contract,
    _joint_source_inputs,
    _official_signal_session,
)


# Pytest discovers this imported autouse fixture in the local module.
__all__ = ["_compact_source_coverage_contract"]


def _artifact(tmp_path: Path):
    source, execution, official = _joint_source_inputs(tmp_path / "source")
    source_artifact = build_joint_execution_source_artifact(
        source, execution, official, generated_at=f"{QUOTE_DATE}T16:02:00+08:00",
    )
    token = replay_and_verify_joint_execution_source_artifact(source_artifact, source, execution, official)
    dates = next_trade_dates(date.fromisoformat(QUOTE_DATE), 2)
    symbols = tuple(str(item["symbol"]) for item in token)
    sessions = {
        day.isoformat(): _official_signal_session(
            tmp_path / "forward" / day.isoformat(), symbols=symbols, session_date=day.isoformat(),
        )
        for day in dates
    }
    artifact = outcomes.build_joint_execution_outcome_artifact(
        token, sessions, generated_at=f"{dates[-1]}T16:00:00+08:00", horizons=(1,),
    )
    return artifact, token, sessions


@pytest.mark.parametrize("change", ["updated_at", "coverage", "provider_source"])
def test_trusted_calendar_metadata_refresh_preserves_frozen_outcome_replay(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, change: str,
) -> None:
    artifact, source, sessions = _artifact(tmp_path)
    original = canonical_json_bytes(artifact)
    expected = outcomes.replay_and_verify_joint_execution_outcome_artifact(artifact, source, sessions)
    original_range = outcomes.trading_date_range

    def refreshed(start: date, end: date):
        days, status = original_range(start, end)
        if change == "updated_at":
            status = replace(status, updated_at=datetime(2026, 9, 7, 9))
        elif change == "coverage":
            status = replace(status, max_date=status.max_date + timedelta(days=365))
        else:
            status = replace(status, provider_source="refreshed-trusted-calendar")
        return days, status

    monkeypatch.setattr(outcomes, "trading_date_range", refreshed)
    actual = outcomes.replay_and_verify_joint_execution_outcome_artifact(artifact, source, sessions)
    assert actual.artifact_digest == expected.artifact_digest
    assert list(actual) == list(expected)
    assert canonical_json_bytes(artifact) == original
    newly_built = outcomes.build_joint_execution_outcome_artifact(
        source, sessions, generated_at=artifact["generated_at"], horizons=(1,),
    )
    assert newly_built["calendar"] != artifact["calendar"]
    assert newly_built["artifact_digest"] != artifact["artifact_digest"]
    assert newly_built["records"] == artifact["records"]


def test_replay_still_rejects_changed_trusted_session_slots(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    artifact, source, sessions = _artifact(tmp_path)
    signal = date.fromisoformat(QUOTE_DATE)
    changed = next_trade_dates(signal, 3)[1:]
    original_range = outcomes.trading_date_range
    monkeypatch.setattr(outcomes, "next_trade_dates", lambda *_args: changed)
    monkeypatch.setattr(
        outcomes, "trading_date_range",
        lambda start, end: ((signal, *changed), original_range(start, end)[1]),
    )
    with pytest.raises(outcomes.JointExecutionOutcomeError, match="frozen calendar path conflicts"):
        outcomes.replay_and_verify_joint_execution_outcome_artifact(artifact, source, sessions)


def test_replay_requires_current_trusted_calendar_coverage(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    artifact, source, sessions = _artifact(tmp_path)
    original_range = outcomes.trading_date_range

    def unavailable(start: date, end: date):
        days, status = original_range(start, end)
        return days, replace(status, source=TradeCalendarSource.UNAVAILABLE)

    monkeypatch.setattr(outcomes, "trading_date_range", unavailable)
    with pytest.raises(outcomes.JointExecutionOutcomeError, match="complete trusted exchange calendar"):
        outcomes.replay_and_verify_joint_execution_outcome_artifact(artifact, source, sessions)

    def missing_coverage(*_args):
        raise TradingCalendarCoverageError("isolated calendar has no coverage")

    monkeypatch.setattr(outcomes, "trading_date_range", missing_coverage)
    with pytest.raises(TradingCalendarCoverageError, match="no coverage"):
        outcomes.replay_and_verify_joint_execution_outcome_artifact(artifact, source, sessions)


def test_frozen_calendar_digest_and_official_raw_path_remain_required(tmp_path: Path) -> None:
    artifact, source, sessions = _artifact(tmp_path)
    tampered = deepcopy(artifact)
    tampered["calendar"]["calendar_provider_source"] = "rewritten-source"
    with pytest.raises(outcomes.JointExecutionOutcomeError, match="failed verification"):
        outcomes.replay_and_verify_joint_execution_outcome_artifact(tampered, source, sessions)
    partial = dict(sessions)
    partial.pop(next(iter(partial)))
    with pytest.raises(outcomes.JointExecutionOutcomeError, match="session path mismatch"):
        outcomes.replay_and_verify_joint_execution_outcome_artifact(artifact, source, partial)
    with pytest.raises(outcomes.JointExecutionOutcomeError, match="verified source token"):
        outcomes.replay_and_verify_joint_execution_outcome_artifact(artifact, None, sessions)
