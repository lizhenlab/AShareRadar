from __future__ import annotations

import asyncio
from datetime import datetime, timedelta
from types import SimpleNamespace

import pytest

from app.models.schemas import CacheStats, ProviderCapabilityStatus, ProviderStatus
from app.services.scheduler import (
    LocalDataScheduler,
    LocalTask,
    _build_local_tasks,
    _data_health_events,
    _reschedule_task,
    _runtime_cleanup_message,
    _task_state,
)
from app.utils.time import seconds_ago_text


async def _handler() -> str:
    return "ok"


def test_scheduler_uses_datahub_settings_instance() -> None:
    settings = SimpleNamespace(
        scheduler_enabled=False,
        scheduler_quote_interval_seconds=11,
        scheduler_kline_interval_seconds=121,
        scheduler_plate_interval_seconds=122,
        scheduler_health_interval_seconds=21,
    )
    scheduler = LocalDataScheduler(SimpleNamespace(settings=settings))  # type: ignore[arg-type]

    assert scheduler.settings is settings


def test_build_local_tasks_uses_explicit_specs_min_intervals_and_offsets() -> None:
    now = datetime(2026, 5, 13, 9, 30, 0)
    tasks = _build_local_tasks(_scheduler_settings(), now, _handlers())

    assert list(tasks) == [
        "refresh_watch_quotes",
        "refresh_key_klines",
        "refresh_plate_rank",
        "check_data_health",
        "evaluate_alerts",
    ]
    assert [task.interval_seconds for task in tasks.values()] == [10, 120, 120, 20, 30]
    assert [(task.next_run_at - now).total_seconds() for task in tasks.values()] == [0, 8, 12, 16, 20]


def test_build_local_tasks_clamps_invalid_interval_settings() -> None:
    now = datetime(2026, 5, 13, 9, 30, 0)
    settings = SimpleNamespace(
        scheduler_quote_interval_seconds=float("inf"),
        scheduler_kline_interval_seconds=" ",
        scheduler_plate_interval_seconds=-1,
        scheduler_health_interval_seconds="45",
    )

    tasks = _build_local_tasks(settings, now, _handlers())

    assert [task.interval_seconds for task in tasks.values()] == [10, 120, 120, 45, 30]


def test_run_once_and_status_use_task_spec_order() -> None:
    calls: list[str] = []
    scheduler = LocalDataScheduler(_SchedulerHub())
    now = datetime(2026, 5, 13, 9, 30, 0)
    scheduler.tasks = {
        "zz_custom": LocalTask("zz_custom", "自定义任务", 20, _recording_handler("zz_custom", calls), now),
        "evaluate_alerts": LocalTask(
            "evaluate_alerts",
            "评估本地预警",
            20,
            _recording_handler("evaluate_alerts", calls),
            now,
        ),
        "refresh_watch_quotes": LocalTask(
            "refresh_watch_quotes",
            "刷新观察池报价",
            20,
            _recording_handler("refresh_watch_quotes", calls),
            now,
        ),
    }

    messages = asyncio.run(scheduler.run_once())

    assert calls == ["refresh_watch_quotes", "evaluate_alerts", "zz_custom"]
    assert messages == ["refresh_watch_quotes", "evaluate_alerts", "zz_custom"]
    assert [task.name for task in scheduler.status().tasks] == [
        "refresh_watch_quotes",
        "evaluate_alerts",
        "zz_custom",
    ]


def test_task_state_serializes_local_task_runtime_fields() -> None:
    now = datetime(2026, 5, 13, 9, 30, 0)
    task = LocalTask(
        name="check_data_health",
        display_name="检查数据健康",
        interval_seconds=20,
        handler=_handler,
        next_run_at=now + timedelta(seconds=20),
        running=True,
        last_started_at=now,
        last_finished_at=now + timedelta(seconds=1),
        last_status="success",
        last_message="ok",
    )

    state = _task_state(task)

    assert state.name == "check_data_health"
    assert state.running is True
    assert state.last_started_at == "2026-05-13 09:30:00"
    assert state.last_finished_at == "2026-05-13 09:30:01"
    assert state.next_run_at == "2026-05-13 09:30:20"
    assert state.last_message == "ok"


def test_reschedule_task_uses_finished_at_for_automatic_runs() -> None:
    finished_at = datetime(2026, 5, 13, 9, 30, 0)
    task = LocalTask("check_data_health", "检查数据健康", 20, _handler, finished_at)

    _reschedule_task(task, manual=False, finished_at=finished_at)

    assert task.next_run_at == finished_at + timedelta(seconds=20)


def test_reschedule_task_uses_finished_at_for_manual_runs_and_clamps_interval() -> None:
    finished_at = datetime(2026, 5, 13, 9, 30, 0)
    task = LocalTask("check_data_health", "检查数据健康", 0, _handler, finished_at)

    _reschedule_task(task, manual=True, finished_at=finished_at)

    assert task.next_run_at == finished_at + timedelta(seconds=1)


@pytest.mark.parametrize("interval_seconds", [float("inf"), float("nan"), " ", -1])
def test_reschedule_task_clamps_non_finite_or_blank_intervals(interval_seconds) -> None:
    finished_at = datetime(2026, 5, 13, 9, 30, 0)
    task = LocalTask("check_data_health", "检查数据健康", interval_seconds, _handler, finished_at)

    _reschedule_task(task, manual=False, finished_at=finished_at)

    assert task.next_run_at == finished_at + timedelta(seconds=1)


def test_manual_task_failure_raises_after_recording_failed_run() -> None:
    scheduler = LocalDataScheduler(_SchedulerHub())
    scheduler.tasks = {
        "bad_task": LocalTask("bad_task", "失败任务", 20, _failing_handler, datetime.now()),
    }

    with pytest.raises(RuntimeError, match="boom"):
        asyncio.run(scheduler.run_once("bad_task"))

    assert scheduler.datahub.cache.finished_runs == [("failed", "boom")]
    assert scheduler.datahub.cache.monitor_events[-1] == ("warning", "task", "boom")


def test_task_run_start_failure_clears_runtime_state_and_uses_clean_message() -> None:
    calls: list[str] = []
    cache = _StartFailingCache()
    scheduler = LocalDataScheduler(_SchedulerHub(cache=cache))
    task = LocalTask("bad_task", "失败任务", 20, _recording_handler("handler-ran", calls), datetime.now())
    scheduler.tasks = {"bad_task": task}

    with pytest.raises(RuntimeError, match="database locked"):
        asyncio.run(scheduler.run_once("bad_task"))

    assert calls == []
    assert task.running is False
    assert task.last_status == "failed"
    assert task.last_message == "database locked"
    assert task.last_finished_at is not None
    assert cache.finished_runs == []
    assert cache.monitor_events[-1] == ("warning", "task", "database locked")


def test_automatic_task_failure_returns_message_without_raising() -> None:
    scheduler = LocalDataScheduler(_SchedulerHub())
    task = LocalTask("bad_task", "失败任务", 20, _failing_handler, datetime.now())

    message = asyncio.run(scheduler._execute(task, manual=False))

    assert message == "boom"
    assert task.last_status == "failed"
    assert scheduler.datahub.cache.finished_runs == [("failed", "boom")]


def test_automatic_task_failure_uses_exception_class_for_blank_message() -> None:
    scheduler = LocalDataScheduler(_SchedulerHub())
    task = LocalTask("bad_task", "失败任务", 20, _blank_failing_handler, datetime.now())

    message = asyncio.run(scheduler._execute(task, manual=False))

    assert message == "RuntimeError"
    assert task.last_status == "failed"
    assert scheduler.datahub.cache.finished_runs == [("failed", "RuntimeError")]


def test_refresh_watch_quotes_normalizes_dedupes_and_skips_invalid_symbols() -> None:
    hub = _SchedulerHub(kline_symbols=["600519", "600519.SH", "bad", " ", "SZ000001"])
    scheduler = LocalDataScheduler(hub)

    message = asyncio.run(scheduler._refresh_watch_quotes())

    assert hub.quote_calls == [["600519.SH", "000001.SZ"]]
    assert message == "已刷新 2 只观察个股报价"
    assert ("warning", "quote", "观察池报价刷新剔除 3 个重复或无效股票代码") in hub.cache.monitor_events


def test_refresh_watch_quotes_returns_skip_message_when_no_valid_symbols_exist() -> None:
    hub = _SchedulerHub(kline_symbols=["bad", " "], seed_symbols=())
    scheduler = LocalDataScheduler(hub)

    message = asyncio.run(scheduler._refresh_watch_quotes())

    assert hub.quote_calls == []
    assert message == "无有效观察个股，已跳过报价刷新"
    assert hub.cache.monitor_events[-1] == ("warning", "quote", "无有效观察个股，已跳过报价刷新")


def test_refresh_key_klines_continues_after_per_symbol_failure() -> None:
    hub = _SchedulerHub(kline_failures={"600001.SH"}, kline_symbols=["600001.SH", "600002.SH", "600003.SH"])
    scheduler = LocalDataScheduler(hub)

    message = asyncio.run(scheduler._refresh_key_klines())

    assert hub.kline_calls == ["600001.SH", "600002.SH", "600003.SH"]
    assert message == "已刷新 2 只关键个股日K线，失败 1 只"
    assert ("warning", "kline", "关键个股K线刷新失败 1 只：600001.SH: kline failed") in hub.cache.monitor_events


def test_refresh_key_klines_raises_when_all_symbols_fail() -> None:
    hub = _SchedulerHub(kline_failures={"600001.SH", "600002.SH"}, kline_symbols=["600001.SH", "600002.SH"])
    scheduler = LocalDataScheduler(hub)

    with pytest.raises(RuntimeError, match="关键个股日K线全部刷新失败"):
        asyncio.run(scheduler._refresh_key_klines())

    assert hub.kline_calls == ["600001.SH", "600002.SH"]


def test_refresh_key_klines_normalizes_dedupes_and_limits_symbols_after_filtering() -> None:
    hub = _SchedulerHub(
        kline_symbols=["bad", "600001", "600001.SH", "SZ000002", "600003"],
        kline_limit=2,
    )
    scheduler = LocalDataScheduler(hub)

    message = asyncio.run(scheduler._refresh_key_klines())

    assert hub.kline_calls == ["600001.SH", "000002.SZ"]
    assert message == "已刷新 2 只关键个股日K线"
    assert ("warning", "kline", "关键个股K线刷新剔除 2 个重复或无效股票代码") in hub.cache.monitor_events


@pytest.mark.parametrize("bad_limit", [-1, float("inf"), " "])
def test_refresh_key_klines_ignores_invalid_symbol_limit_instead_of_skipping_all(bad_limit) -> None:
    hub = _SchedulerHub(kline_symbols=["600001.SH", "600002.SH"])
    hub.settings.scheduler_kline_symbols_limit = bad_limit
    scheduler = LocalDataScheduler(hub)

    message = asyncio.run(scheduler._refresh_key_klines())

    assert hub.kline_calls == ["600001.SH", "600002.SH"]
    assert message == "已刷新 2 只关键个股日K线"


def test_refresh_key_klines_counts_empty_results_as_failures() -> None:
    hub = _SchedulerHub(
        kline_empty={"600002.SH"},
        kline_symbols=["600001.SH", "600002.SH", "600003.SH"],
    )
    scheduler = LocalDataScheduler(hub)

    message = asyncio.run(scheduler._refresh_key_klines())

    assert hub.kline_calls == ["600001.SH", "600002.SH", "600003.SH"]
    assert message == "已刷新 2 只关键个股日K线，失败 1 只"
    assert ("warning", "kline", "关键个股K线刷新失败 1 只：600002.SH: 返回空K线") in hub.cache.monitor_events


def test_refresh_key_klines_returns_skip_message_when_no_valid_symbols_exist() -> None:
    hub = _SchedulerHub(kline_symbols=[], seed_symbols=())
    scheduler = LocalDataScheduler(hub)

    message = asyncio.run(scheduler._refresh_key_klines())

    assert hub.kline_calls == []
    assert message == "无有效关键个股，已跳过日K线刷新"
    assert hub.cache.monitor_events[-1] == ("warning", "kline", "无有效关键个股，已跳过日K线刷新")


def test_data_health_events_prefers_capability_failures_over_provider_names() -> None:
    events = _data_health_events(
        _cache_stats(latest_quote_at=seconds_ago_text(1), latest_kline_at=seconds_ago_text(1)),
        [_capability_status("tencent", "quote", healthy=False, last_error="timeout")],
        [_provider_status("akshare", healthy=False)],
        _settings(),
    )

    assert [event.category for event in events] == ["provider"]
    assert events[0].level == "warning"
    assert events[0].message == "数据源最近存在失败：tencent 报价"


def test_data_health_events_deduplicates_and_sorts_capability_failures() -> None:
    events = _data_health_events(
        _cache_stats(latest_quote_at=seconds_ago_text(1), latest_kline_at=seconds_ago_text(1)),
        [
            _capability_status("tencent", "quote", healthy=False, last_error="timeout", priority=2),
            _capability_status("akshare", "kline", healthy=False, last_error="timeout", priority=1),
            _capability_status("tencent", "quote", healthy=False, last_error="timeout", priority=2),
        ],
        [],
        _settings(),
    )

    assert [event.message for event in events] == ["数据源最近存在失败：akshare 日K、tencent 报价"]


def test_data_health_events_falls_back_to_provider_failures_when_capabilities_are_inactive() -> None:
    events = _data_health_events(
        _cache_stats(latest_quote_at=seconds_ago_text(1), latest_kline_at=seconds_ago_text(1)),
        [_capability_status("tencent", "quote", healthy=False)],
        [_provider_status("akshare", healthy=False)],
        _settings(),
    )

    assert [event.message for event in events] == ["数据源最近存在失败：akshare"]


def test_data_health_events_reports_missing_quote_and_kline_cache() -> None:
    events = _data_health_events(
        _cache_stats(latest_quote_at=None, latest_kline_at=None),
        [],
        [],
        _settings(),
    )

    assert [(event.category, event.message) for event in events] == [
        ("quote", "尚未形成报价缓存，请先打开页面或手动刷新"),
        ("kline", "尚未形成K线缓存，个股趋势分析会依赖实时拉取"),
    ]


def test_data_health_events_reports_stale_quote_and_kline_cache() -> None:
    events = _data_health_events(
        _cache_stats(latest_quote_at=seconds_ago_text(120), latest_kline_at=seconds_ago_text(120)),
        [],
        [],
        _settings(quote_stale_warning_seconds=60, kline_cache_seconds=30),
    )

    assert len(events) == 2
    assert events[0].category == "quote"
    assert events[0].message.startswith("报价缓存已超过 ")
    assert events[0].message.endswith(" 秒未更新")
    assert events[1].message == "K线缓存偏旧，建议手动触发关键个股K线刷新"


def test_data_health_events_clamps_invalid_cache_thresholds() -> None:
    events = _data_health_events(
        _cache_stats(latest_quote_at=seconds_ago_text(0), latest_kline_at=seconds_ago_text(0)),
        [],
        [],
        _settings(quote_stale_warning_seconds=-1, kline_cache_seconds=float("nan")),
    )

    assert [(event.level, event.category, event.message) for event in events] == [
        ("info", "health", "报价、K线、行业背景和数据源状态正常")
    ]


def test_data_health_events_reports_future_cache_timestamps_as_invalid() -> None:
    future = (datetime.now() + timedelta(days=1)).strftime("%Y-%m-%d %H:%M:%S")

    events = _data_health_events(
        _cache_stats(latest_quote_at=future, latest_kline_at=future),
        [],
        [],
        _settings(),
    )

    assert [(event.category, event.message) for event in events] == [
        ("quote", "报价缓存时间异常，需检查系统时间或缓存数据"),
        ("kline", "K线缓存时间异常，需检查系统时间或缓存数据"),
    ]


def test_data_health_events_returns_ok_when_no_issue_exists() -> None:
    events = _data_health_events(
        _cache_stats(latest_quote_at=seconds_ago_text(1), latest_kline_at=seconds_ago_text(1)),
        [],
        [],
        _settings(),
    )

    assert [(event.level, event.category, event.message) for event in events] == [
        ("info", "health", "报价、K线、行业背景和数据源状态正常")
    ]


def test_runtime_cleanup_message_sums_removed_rows() -> None:
    assert _runtime_cleanup_message({"task_run": 2, "monitor_event": 3}) == "已清理 5 条过期运行记录"
    assert _runtime_cleanup_message({"task_run": 3, "monitor_event": -2}) == "已清理 3 条过期运行记录"
    assert _runtime_cleanup_message({"task_run": 0}) is None


def test_runtime_cleanup_message_ignores_non_finite_and_blank_counts() -> None:
    assert (
        _runtime_cleanup_message(
            {
                "task_run": float("inf"),
                "monitor_event": float("nan"),
                "cache_event": " ",
                "alert_event": "2",
            }
        )
        == "已清理 2 条过期运行记录"
    )


def _settings(*, quote_stale_warning_seconds: int = 60, kline_cache_seconds: int = 300):
    return SimpleNamespace(
        quote_stale_warning_seconds=quote_stale_warning_seconds,
        kline_cache_seconds=kline_cache_seconds,
    )


def _scheduler_settings():
    return SimpleNamespace(
        scheduler_quote_interval_seconds=1,
        scheduler_kline_interval_seconds=1,
        scheduler_plate_interval_seconds=1,
        scheduler_health_interval_seconds=1,
    )


def _handlers():
    return {
        "refresh_watch_quotes": _handler,
        "refresh_key_klines": _handler,
        "refresh_plate_rank": _handler,
        "check_data_health": _handler,
        "evaluate_alerts": _handler,
    }


async def _failing_handler() -> str:
    raise RuntimeError("boom")


async def _blank_failing_handler() -> str:
    raise RuntimeError()


def _recording_handler(name: str, calls: list[str]):
    async def handler() -> str:
        calls.append(name)
        return name

    return handler


class _SchedulerCache:
    def __init__(self, kline_symbols: list[str] | None = None) -> None:
        self.kline_symbols = kline_symbols or []
        self.finished_runs: list[tuple[str, str | None]] = []
        self.monitor_events: list[tuple[str, str, str]] = []
        self._next_run_id = 0

    def start_task_run(self, task_name: str) -> int:
        self._next_run_id += 1
        return self._next_run_id

    def finish_task_run(self, run_id: int, status: str, message: str | None = None) -> None:
        self.finished_runs.append((status, message))

    def save_monitor_event(self, level: str, category: str, message: str, symbol: str | None = None) -> None:
        self.monitor_events.append((level, category, message))

    def watchlist_symbols(self) -> list[str]:
        return list(self.kline_symbols)


class _StartFailingCache(_SchedulerCache):
    def start_task_run(self, task_name: str) -> int:
        raise RuntimeError("  database\nlocked  ")


class _SchedulerHub:
    def __init__(
        self,
        *,
        kline_empty: set[str] | None = None,
        kline_failures: set[str] | None = None,
        kline_limit: int = 10,
        kline_symbols: list[str] | None = None,
        cache: _SchedulerCache | None = None,
        seed_symbols: tuple[str, ...] = ("600519.SH",),
    ) -> None:
        self.settings = _scheduler_settings()
        self.settings.scheduler_enabled = False
        self.settings.scheduler_kline_symbols_limit = kline_limit
        self.settings.seed_symbols = seed_symbols
        self.cache = cache or _SchedulerCache(kline_symbols)
        self.kline_empty = kline_empty or set()
        self.kline_failures = kline_failures or set()
        self.kline_calls: list[str] = []
        self.quote_calls: list[list[str]] = []

    async def quotes(self, symbols, use_cache: bool = True):
        self.quote_calls.append(list(symbols))
        return [object() for _ in symbols]

    async def kline(self, symbol: str, limit: int = 120, use_cache: bool = True):
        self.kline_calls.append(symbol)
        if symbol in self.kline_failures:
            raise RuntimeError("kline failed")
        if symbol in self.kline_empty:
            return []
        return [object()]


def _cache_stats(*, latest_quote_at: str | None, latest_kline_at: str | None) -> CacheStats:
    return CacheStats(
        path=":memory:",
        quote_count=1 if latest_quote_at else 0,
        quote_history_count=0,
        kline_count=1 if latest_kline_at else 0,
        stock_count=0,
        plate_count=0,
        provider_count=0,
        latest_quote_at=latest_quote_at,
        latest_kline_at=latest_kline_at,
    )


def _capability_status(
    name: str,
    kind: str,
    *,
    healthy: bool,
    last_error: str | None = None,
    priority: int = 1,
) -> ProviderCapabilityStatus:
    return ProviderCapabilityStatus(
        name=name,
        kind=kind,
        enabled=True,
        priority=priority,
        healthy=healthy,
        last_error=last_error,
        failure_count=1 if last_error else 0,
    )


def _provider_status(name: str, *, healthy: bool) -> ProviderStatus:
    return ProviderStatus(
        name=name,
        enabled=True,
        priority=1,
        healthy=healthy,
        last_error=None if healthy else "network down",
        failure_count=0 if healthy else 1,
    )
