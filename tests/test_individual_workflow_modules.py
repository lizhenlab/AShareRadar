from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import patch

from app.models.schemas import ChartMarkSummary
from app.workflows.individual import (
    WORKBENCH_ALERT_EVENT_LIMIT,
    WORKBENCH_ALERT_RULE_LIMIT,
    WORKBENCH_CHART_MARK_LIMIT,
    WORKBENCH_NOTE_LIMIT,
    _workbench_local_state,
)


def test_workbench_local_state_uses_normalized_symbol_and_stable_limits() -> None:
    cache = _CacheStub()
    hub = SimpleNamespace(cache=cache)
    context = SimpleNamespace(insights=SimpleNamespace(name="insights"))
    mark_calls: list[tuple[object, str, object, int]] = []

    def fake_chart_marks(datahub, symbol, insights, limit: int):
        mark_calls.append((datahub, symbol, insights, limit))
        return ChartMarkSummary(symbol=symbol, updated_at="2026-07-03 09:00:00", marks=[])

    with patch("app.services.chart_marks.build_chart_marks_from_context", side_effect=fake_chart_marks):
        state = _workbench_local_state(hub, "600519", context)  # type: ignore[arg-type]

    assert state.chart_marks.symbol == "600519.SH"
    assert mark_calls == [(hub, "600519.SH", context.insights, WORKBENCH_CHART_MARK_LIMIT)]
    assert cache.alert_rule_calls == [("600519.SH", True, WORKBENCH_ALERT_RULE_LIMIT)]
    assert cache.alert_event_calls == [("600519.SH", WORKBENCH_ALERT_EVENT_LIMIT)]
    assert cache.stock_note_calls == [("600519.SH", WORKBENCH_NOTE_LIMIT)]


class _CacheStub:
    def __init__(self) -> None:
        self.alert_rule_calls: list[tuple[str, bool, int]] = []
        self.alert_event_calls: list[tuple[str, int]] = []
        self.stock_note_calls: list[tuple[str, int]] = []

    def alert_rules(self, *, symbol: str, include_disabled: bool, limit: int) -> list[object]:
        self.alert_rule_calls.append((symbol, include_disabled, limit))
        return []

    def alert_events(self, *, symbol: str, limit: int) -> list[object]:
        self.alert_event_calls.append((symbol, limit))
        return []

    def stock_notes(self, symbol: str, *, limit: int) -> list[object]:
        self.stock_note_calls.append((symbol, limit))
        return []
