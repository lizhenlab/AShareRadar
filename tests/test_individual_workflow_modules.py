from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import patch

from app.models.schemas import ChartMarkSummary
from app.workflows.individual import (
    WORKBENCH_ALERT_EVENT_LIMIT,
    WORKBENCH_ALERT_RULE_LIMIT,
    WORKBENCH_CHART_MARK_LIMIT,
    WORKBENCH_NOTE_LIMIT,
    _ensure_advice_snapshot,
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
    assert state.warnings == []


def test_workbench_local_state_degrades_when_local_reads_fail() -> None:
    cache = _FailingLocalStateCache()
    hub = SimpleNamespace(cache=cache)
    context = SimpleNamespace(insights=SimpleNamespace(name="insights"))

    with patch("app.services.chart_marks.build_chart_marks_from_context", side_effect=RuntimeError("marks down")):
        state = _workbench_local_state(hub, "600519", context)  # type: ignore[arg-type]

    assert state.chart_marks.symbol == "600519.SH"
    assert state.chart_marks.marks == []
    assert state.chart_marks.categories == []
    assert state.alert_rules == []
    assert state.alert_events == []
    assert state.notes == []
    assert [item.component for item in state.warnings] == ["chart_marks", "alert_rules", "alert_events", "notes"]
    assert [item.message for item in state.warnings] == [
        "图表标注暂不可用，当前显示空标注。",
        "预警规则暂不可用，当前显示空列表。",
        "预警事件暂不可用，当前显示空列表。",
        "个股笔记暂不可用，当前显示空列表。",
    ]
    assert len(cache.events) == 4
    assert all(item[0] == "fallback" for item in cache.events)
    assert all(" down" not in item[1] for item in cache.events)
    assert all("RuntimeError" in item[1] for item in cache.events)


def test_advice_snapshot_failure_does_not_block_workbench_response() -> None:
    class FailingAdviceCache:
        def save_advice_snapshot(self, analysis: object) -> None:
            raise RuntimeError("advice db readonly")

    hub = SimpleNamespace(cache=FailingAdviceCache())
    context = SimpleNamespace(analysis=object(), advice_snapshot_saved=False)

    warning = _ensure_advice_snapshot(hub, context)  # type: ignore[arg-type]

    assert context.advice_snapshot_saved is False
    assert warning is not None
    assert warning.component == "advice_snapshot"
    assert warning.message == "分析建议快照暂未保存，本次分析结果仍可正常查看。"


def test_advice_snapshot_marks_cached_context_only_after_success() -> None:
    class AdviceCache:
        def __init__(self) -> None:
            self.calls = 0

        def save_advice_snapshot(self, analysis: object) -> None:
            self.calls += 1

    cache = AdviceCache()
    hub = SimpleNamespace(cache=cache)
    context = SimpleNamespace(analysis=object(), advice_snapshot_saved=False)

    first_warning = _ensure_advice_snapshot(hub, context)  # type: ignore[arg-type]
    second_warning = _ensure_advice_snapshot(hub, context)  # type: ignore[arg-type]

    assert context.advice_snapshot_saved is True
    assert cache.calls == 1
    assert first_warning is None
    assert second_warning is None


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


class _FailingLocalStateCache:
    def __init__(self) -> None:
        self.events: list[tuple[str, str]] = []

    def log_event(self, category: str, message: str) -> None:
        self.events.append((category, message))

    def alert_rules(self, *, symbol: str, include_disabled: bool, limit: int) -> list[object]:
        raise RuntimeError("alert rules down")

    def alert_events(self, *, symbol: str, limit: int) -> list[object]:
        raise RuntimeError("alert events down")

    def stock_notes(self, symbol: str, *, limit: int) -> list[object]:
        raise RuntimeError("notes down")
