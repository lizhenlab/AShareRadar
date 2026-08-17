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
    _workbench_is_non_fallback,
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


@pytest.mark.parametrize(
    (
        "quote_from_cache",
        "quote_fallback_used",
        "kline_from_cache",
        "kline_fallback_used",
        "expected",
    ),
    [
        (False, False, False, False, True),
        (True, False, False, False, False),
        (False, True, False, False, False),
        (False, False, True, False, False),
        (False, False, False, True, False),
    ],
)
def test_workbench_non_fallback_requires_uncached_primary_quote_and_kline(
    quote_from_cache: bool,
    quote_fallback_used: bool,
    kline_from_cache: bool,
    kline_fallback_used: bool,
    expected: bool,
) -> None:
    result = SimpleNamespace(
        analysis=SimpleNamespace(
            quote=SimpleNamespace(
                from_cache=quote_from_cache,
                fallback_used=quote_fallback_used,
            ),
            data_quality=SimpleNamespace(
                kline_quality=SimpleNamespace(
                    from_cache=kline_from_cache,
                    fallback_used=kline_fallback_used,
                )
            ),
        )
    )

    assert _workbench_is_non_fallback(result) is expected  # type: ignore[arg-type]


def test_workbench_non_fallback_rejects_missing_kline_quality() -> None:
    result = SimpleNamespace(
        analysis=SimpleNamespace(
            quote=SimpleNamespace(from_cache=False, fallback_used=False),
            data_quality=SimpleNamespace(kline_quality=None),
        )
    )

    assert _workbench_is_non_fallback(result) is False  # type: ignore[arg-type]


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
