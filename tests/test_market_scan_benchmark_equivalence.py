from __future__ import annotations

from copy import deepcopy

import pytest

from app.models.market import Kline
from tools import benchmark_market_scan as benchmark


def _rows() -> dict[str, list[Kline]]:
    return {"600001.SH": [Kline(
        date=f"2026-09-{index:02}", open=10, close=10.1, high=10.2, low=9.9,
        volume=1000, source="synthetic", adjustment_mode="qfq",
        fetched_at="2026-09-08T00:00:00Z", as_of="2026-09-07T15:00:00+08:00",
        data_version="test-v1", from_cache=True,
    ) for index in range(1, 4)], "000001.SZ": []}


@pytest.mark.parametrize("field,value", [
    ("close", 10.15), ("volume", 999.0), ("source", "other-source"),
    ("adjustment_mode", "hfq"), ("data_version", "test-v2"),
    ("contract_version", "other-contract"), ("fallback_used", True),
    ("from_cache", False), ("fetched_at", "2026-09-08T00:01:00Z"),
    ("as_of", "2026-09-07T14:00:00+08:00"), ("point_in_time", True),
    ("session_status", "suspended"), ("adjustment_factor", 0.9),
])
def test_equivalence_rejects_changed_middle_bar_or_provenance(field: str, value: object) -> None:
    original = _rows()
    changed = deepcopy(original)
    changed["600001.SH"][1] = changed["600001.SH"][1].model_copy(update={field: value})
    assert benchmark._cache_signature(original)[0] == benchmark._cache_signature(changed)[0]
    assert benchmark._cache_signature(original) != benchmark._cache_signature(changed)


def test_signature_ignores_dictionary_insertion_order_but_keeps_symbol_membership_and_bar_order() -> None:
    rows = _rows()
    assert benchmark._cache_signature(rows) == benchmark._cache_signature(dict(reversed(list(rows.items()))))
    assert benchmark._cache_signature(rows) != benchmark._cache_signature({"600001.SH": rows["600001.SH"]})
    assert benchmark._cache_signature(rows) != benchmark._cache_signature({**rows, "600001.SH": list(reversed(rows["600001.SH"]))})


def test_comparison_fails_when_batch_read_changes_a_middle_bar() -> None:
    rows = _rows()

    class MismatchedRepository:
        def get_klines(self, symbol, *_args):
            return deepcopy(rows[symbol])

        def get_klines_many(self, symbols, *_args):
            result = {symbol: deepcopy(rows[symbol]) for symbol in symbols}
            result["600001.SH"][1].source = "wrong-cache-source"
            return result

    with pytest.raises(RuntimeError, match="逐股读取与批量预取结果不一致"):
        benchmark._comparison(MismatchedRepository(), list(rows), limit=3, batch_size=2, iterations=1)


def test_measure_rejects_cache_content_drift_and_excludes_digest_time(monkeypatch) -> None:
    rows = _rows()
    clock = [0.0]
    original_signature = benchmark._cache_signature

    def slow_signature(value):
        clock[0] += 100
        return original_signature(value)

    def operation():
        clock[0] += 2
        return deepcopy(rows)

    monkeypatch.setattr(benchmark, "perf_counter", lambda: clock[0])
    monkeypatch.setattr(benchmark, "_cache_signature", slow_signature)
    samples, _ = benchmark._measure(operation, 2)
    assert samples == [2.0, 2.0]

    def drifting_operation():
        rows["600001.SH"][1].volume += 1
        return deepcopy(rows)

    with pytest.raises(RuntimeError, match="基准期间缓存读取结果不稳定"):
        benchmark._measure(drifting_operation, 1)
