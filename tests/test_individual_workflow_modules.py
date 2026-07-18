from __future__ import annotations

import asyncio
import ast
from pathlib import Path
import threading
from types import SimpleNamespace
from unittest.mock import patch

import pytest

from app.models.schemas import ChartMarkSummary
from app.services import alerts as alert_service
from app.services import chart_marks as chart_marks_service
from app.workflows.individual import (
    WORKBENCH_ALERT_EVENT_LIMIT,
    WORKBENCH_ALERT_RULE_LIMIT,
    WORKBENCH_CHART_MARK_LIMIT,
    WORKBENCH_NOTE_LIMIT,
    _ensure_advice_snapshot,
    _workbench_local_state,
)


ROOT = Path(__file__).resolve().parents[1]


def test_service_refactor_graph_has_no_workflow_edges_local_imports_or_cycles() -> None:
    services = ROOT / "app/services"
    paths = {
        *services.glob("research_risk_reward*.py"),
        *services.glob("research_qa_answer*.py"),
        *services.glob("stock_rule*.py"),
        services / "alerts.py",
        services / "chart_marks.py",
    }
    module_by_path = {path: f"app.services.{path.stem}" for path in paths}
    known_modules = set(module_by_path.values())
    graph = {module: set() for module in known_modules}
    reverse_edges: list[str] = []
    local_imports: list[str] = []

    for path, module in module_by_path.items():
        tree = ast.parse(path.read_text(encoding="utf-8"))
        for node in ast.walk(tree):
            if isinstance(node, ast.ImportFrom) and node.module:
                if node.module.startswith("app.workflows"):
                    reverse_edges.append(f"{path.name}:{node.lineno}:{node.module}")
                if node.module in known_modules:
                    graph[module].add(node.module)
        for function in (node for node in ast.walk(tree) if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef))):
            for child in ast.walk(function):
                if isinstance(child, (ast.Import, ast.ImportFrom)):
                    local_imports.append(f"{path.name}:{child.lineno}:{function.name}")

    assert reverse_edges == []
    assert local_imports == []
    assert _dependency_cycles(graph) == []


def test_services_do_not_expose_mutable_process_global_loader_registration() -> None:
    assert not hasattr(alert_service, "_default_alert_analysis_loader")
    assert not hasattr(alert_service, "configure_alert_analysis_loader")
    assert not hasattr(chart_marks_service, "_default_chart_context_loader")
    assert not hasattr(chart_marks_service, "configure_chart_context_loader")


def _dependency_cycles(graph: dict[str, set[str]]) -> list[str]:
    visiting: set[str] = set()
    visited: set[str] = set()
    cycles: list[str] = []

    def visit(module: str) -> None:
        if module in visiting:
            cycles.append(module)
            return
        if module in visited:
            return
        visiting.add(module)
        for dependency in graph[module]:
            visit(dependency)
        visiting.remove(module)
        visited.add(module)

    for module in graph:
        visit(module)
    return cycles


def test_workbench_local_state_uses_normalized_symbol_and_stable_limits() -> None:
    cache = _CacheStub()
    hub = SimpleNamespace(cache=cache)
    context = SimpleNamespace(insights=SimpleNamespace(name="insights"))
    mark_calls: list[tuple[object, str, object, int]] = []

    async def fake_chart_marks(datahub, symbol, insights, limit: int):
        mark_calls.append((datahub, symbol, insights, limit))
        return ChartMarkSummary(symbol=symbol, updated_at="2026-07-03 09:00:00", marks=[])

    async def run_check():
        event_loop_thread = threading.get_ident()
        with patch("app.services.chart_marks.build_chart_marks_from_context", side_effect=fake_chart_marks):
            state = await _workbench_local_state(hub, "600519", context)  # type: ignore[arg-type]
        return state, event_loop_thread

    state, event_loop_thread = asyncio.run(run_check())

    assert state.chart_marks.symbol == "600519.SH"
    assert mark_calls == [(hub, "600519.SH", context.insights, WORKBENCH_CHART_MARK_LIMIT)]
    assert cache.alert_rule_calls == [("600519.SH", True, WORKBENCH_ALERT_RULE_LIMIT)]
    assert cache.alert_event_calls == [("600519.SH", WORKBENCH_ALERT_EVENT_LIMIT)]
    assert cache.stock_note_calls == [("600519.SH", WORKBENCH_NOTE_LIMIT)]
    assert len(cache.io_threads) == 3
    assert all(thread_id != event_loop_thread for thread_id in cache.io_threads)
    assert state.warnings == []


def test_workbench_local_state_degrades_when_local_reads_fail() -> None:
    cache = _FailingLocalStateCache()
    hub = SimpleNamespace(cache=cache)
    context = SimpleNamespace(insights=SimpleNamespace(name="insights"))

    async def run_check():
        event_loop_thread = threading.get_ident()
        with patch("app.services.chart_marks.build_chart_marks_from_context", side_effect=RuntimeError("marks down")):
            state = await _workbench_local_state(hub, "600519", context)  # type: ignore[arg-type]
        return state, event_loop_thread

    state, event_loop_thread = asyncio.run(run_check())

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
    assert cache.io_threads
    assert all(thread_id != event_loop_thread for thread_id in cache.io_threads)


def test_advice_snapshot_failure_does_not_block_workbench_response() -> None:
    class FailingAdviceCache:
        def save_advice_snapshot(self, analysis: object) -> None:
            raise RuntimeError("advice db readonly")

    hub = SimpleNamespace(cache=FailingAdviceCache())
    context = SimpleNamespace(analysis=object(), advice_snapshot_saved=False)

    warning = asyncio.run(_ensure_advice_snapshot(hub, context))  # type: ignore[arg-type]

    assert context.advice_snapshot_saved is False
    assert warning is not None
    assert warning.component == "advice_snapshot"
    assert warning.message == "分析建议快照暂未保存，本次分析结果仍可正常查看。"


def test_advice_snapshot_marks_cached_context_only_after_success() -> None:
    class AdviceCache:
        def __init__(self) -> None:
            self.calls = 0
            self.io_threads: list[int] = []

        def save_advice_snapshot(self, analysis: object) -> None:
            self.calls += 1
            self.io_threads.append(threading.get_ident())

    cache = AdviceCache()
    hub = SimpleNamespace(cache=cache)
    context = SimpleNamespace(analysis=object(), advice_snapshot_saved=False)

    async def run_check():
        event_loop_thread = threading.get_ident()
        first_warning = await _ensure_advice_snapshot(hub, context)  # type: ignore[arg-type]
        second_warning = await _ensure_advice_snapshot(hub, context)  # type: ignore[arg-type]
        return first_warning, second_warning, event_loop_thread

    first_warning, second_warning, event_loop_thread = asyncio.run(run_check())

    assert context.advice_snapshot_saved is True
    assert cache.calls == 1
    assert cache.io_threads[0] != event_loop_thread
    assert first_warning is None
    assert second_warning is None


def test_advice_snapshot_cancellation_propagates_without_marking_context_saved() -> None:
    started = threading.Event()
    release = threading.Event()

    class BlockingAdviceCache:
        def save_advice_snapshot(self, analysis: object) -> None:
            started.set()
            release.wait(timeout=2)

    hub = SimpleNamespace(cache=BlockingAdviceCache())
    context = SimpleNamespace(analysis=object(), advice_snapshot_saved=False)

    async def run_check() -> None:
        task = asyncio.create_task(_ensure_advice_snapshot(hub, context))  # type: ignore[arg-type]
        assert await asyncio.to_thread(started.wait, 1)
        task.cancel()
        try:
            with pytest.raises(asyncio.CancelledError):
                await task
        finally:
            release.set()

    asyncio.run(run_check())

    assert context.advice_snapshot_saved is False


class _CacheStub:
    def __init__(self) -> None:
        self.alert_rule_calls: list[tuple[str, bool, int]] = []
        self.alert_event_calls: list[tuple[str, int]] = []
        self.stock_note_calls: list[tuple[str, int]] = []
        self.io_threads: list[int] = []

    def alert_rules(self, *, symbol: str, include_disabled: bool, limit: int) -> list[object]:
        self.io_threads.append(threading.get_ident())
        self.alert_rule_calls.append((symbol, include_disabled, limit))
        return []

    def alert_events(self, *, symbol: str, limit: int) -> list[object]:
        self.io_threads.append(threading.get_ident())
        self.alert_event_calls.append((symbol, limit))
        return []

    def stock_notes(self, symbol: str, *, limit: int) -> list[object]:
        self.io_threads.append(threading.get_ident())
        self.stock_note_calls.append((symbol, limit))
        return []


class _FailingLocalStateCache:
    def __init__(self) -> None:
        self.events: list[tuple[str, str]] = []
        self.io_threads: list[int] = []

    def log_event(self, category: str, message: str) -> None:
        self.io_threads.append(threading.get_ident())
        self.events.append((category, message))

    def alert_rules(self, *, symbol: str, include_disabled: bool, limit: int) -> list[object]:
        self.io_threads.append(threading.get_ident())
        raise RuntimeError("alert rules down")

    def alert_events(self, *, symbol: str, limit: int) -> list[object]:
        self.io_threads.append(threading.get_ident())
        raise RuntimeError("alert events down")

    def stock_notes(self, symbol: str, *, limit: int) -> list[object]:
        self.io_threads.append(threading.get_ident())
        raise RuntimeError("notes down")
