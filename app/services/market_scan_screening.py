"""Read-only screening, explanations and breadth over one frozen scan snapshot."""

from __future__ import annotations

from collections import Counter
from collections.abc import Callable, Sequence
import math
from typing import Protocol, TypeVar

from pydantic import BaseModel

from app.artifacts.io import canonical_json_text, sha256_hex
from app.market_scan_screening import (
    CompiledScreenCondition,
    compile_screen_conditions,
    screen_spec_digest,
)
from app.models.market_scan import MarketScanResultItem, MarketScanRun
from app.models.market_scan_screening import (
    MarketBreadthBin,
    MarketBreadthChange,
    MarketBreadthIndustry,
    MarketBreadthPopulation,
    MarketBreadthScore,
    MarketBreadthV1,
    MarketScanExclusionReason,
    MarketScanFailedCondition,
    MarketScanFunnelStep,
    MarketScanMatchExplanation,
    MarketScanNearMiss,
    MarketScanScreenEvaluateRequest,
    MarketScanScreenEvaluationV1,
    MarketScanScreenEvidence,
    MarketScanScreenMatchedPage,
    ScreenSortField,
    ScreenSortV2,
)
from app.repositories.market_scan_screening import (
    MarketScanBreadthRow,
    MarketScanScreeningRow,
)
from app.services.market_scan_universe import FULL_MARKET_SCOPE


class MarketScanScreeningUnavailable(ValueError):
    """The requested run is not immutable full-market publication evidence."""


class MarketScanScreeningRepositoryProtocol(Protocol):
    def market_scan_screening_breadth_snapshot(
        self,
        run_id: int,
    ) -> tuple[MarketScanRun, list[MarketScanBreadthRow]]: ...

    def market_scan_screening_evaluation_snapshot(
        self,
        run_id: int,
    ) -> tuple[MarketScanRun, list[MarketScanScreeningRow]]: ...

    def market_scan_screening_result_items(
        self,
        run_id: int,
        symbols: Sequence[str],
    ) -> list[MarketScanResultItem]: ...


class MarketScanScreeningService:
    """Compute only from persisted rows; this service has no market-data dependency."""

    def __init__(self, repository: MarketScanScreeningRepositoryProtocol) -> None:
        self._repository = repository

    def breadth(self, run_id: int) -> MarketBreadthV1:
        run, rows = self._repository.market_scan_screening_breadth_snapshot(run_id)
        _require_eligible_run(run)
        score_values = sorted(
            value for item in rows if (value := _number(item.score)) is not None
        )
        return _sealed_response(MarketBreadthV1,
            evidence=_evidence(run),
            population=_population(rows),
            score=_score_breadth(score_values, total=len(rows)),
            change=_change_breadth(rows),
            industries=_industry_breadth(rows),
        )

    def evaluate(
        self,
        run_id: int,
        request: MarketScanScreenEvaluateRequest,
    ) -> MarketScanScreenEvaluationV1:
        run, population = self._repository.market_scan_screening_evaluation_snapshot(run_id)
        _require_eligible_run(run)
        conditions = compile_screen_conditions(request.spec)
        funnel = _funnel(population, conditions)
        failures = {item.symbol: _failures(item, conditions) for item in population}
        matched_rows = [item for item in population if not failures[item.symbol]]
        ordered = _ordered(matched_rows, request.spec.sort)
        page_items, near_misses = _hydrate_evaluation_items(
            self._repository,
            run_id,
            population,
            failures,
            ordered,
            request,
        )
        return _sealed_response(MarketScanScreenEvaluationV1,
            evidence=_evidence(run),
            spec=request.spec,
            spec_digest=screen_spec_digest(request.spec),
            population_count=len(population),
            matched_count=len(matched_rows),
            funnel=funnel,
            exclusion_reasons=_exclusion_reasons(failures, conditions),
            matched=MarketScanScreenMatchedPage(
                items=page_items,
                total=len(matched_rows),
                page=request.page,
                page_size=request.page_size,
                page_count=_page_count(len(matched_rows), request.page_size),
            ),
            matched_explanations=[
                MarketScanMatchExplanation(
                    symbol=item.symbol,
                    passed_conditions=(
                        [condition.code for condition in conditions]
                        if conditions
                        else ["all_conditions_passed"]
                    ),
                )
                for item in page_items
            ],
            near_misses=near_misses,
        )


def _require_eligible_run(run: MarketScanRun) -> None:
    if run.status not in {"success", "degraded"}:
        raise MarketScanScreeningUnavailable("可信筛选仅支持已发布的全市场扫描批次")
    if run.scope != FULL_MARKET_SCOPE:
        raise MarketScanScreeningUnavailable("可信筛选仅支持完整全市场扫描批次")
    if run.snapshot_digest is None or run.snapshot_seal_origin is None or run.snapshot_sealed_at is None:
        raise MarketScanScreeningUnavailable("可信筛选批次缺少完整快照封印证据")


_ScreenResponse = TypeVar("_ScreenResponse", bound=BaseModel)


def _sealed_response(
    response_type: type[_ScreenResponse],
    **values: object,
) -> _ScreenResponse:
    draft = response_type.model_construct(canonical_digest="0" * 64, **values)
    payload = draft.model_dump(mode="json", exclude={"canonical_digest"})
    payload["canonical_digest"] = sha256_hex(canonical_json_text(payload))
    return response_type.model_validate(payload)


def _evidence(run: MarketScanRun) -> MarketScanScreenEvidence:
    if run.snapshot_digest is None or run.snapshot_seal_origin is None or run.snapshot_sealed_at is None:
        raise MarketScanScreeningUnavailable("可信筛选批次缺少完整快照封印证据")
    return MarketScanScreenEvidence(
        run_id=run.id,
        status=run.status,
        mode=run.mode,
        scope=run.scope,
        data_date=run.data_date,
        quote_date=run.quote_date,
        rule_version=run.rule_version,
        finished_at=run.finished_at,
        snapshot_digest=run.snapshot_digest,
        snapshot_seal_origin=run.snapshot_seal_origin,
        snapshot_sealed_at=run.snapshot_sealed_at,
    )


def _population(rows: Sequence[MarketScanBreadthRow]) -> MarketBreadthPopulation:
    return MarketBreadthPopulation(
        total=len(rows),
        by_status=dict(sorted(Counter(item.status for item in rows).items())),
        by_market=dict(sorted(Counter(item.market for item in rows).items())),
    )


def _score_breadth(scores: list[float], *, total: int) -> MarketBreadthScore:
    return MarketBreadthScore(
        present_count=len(scores),
        missing_count=total - len(scores),
        min=scores[0] if scores else None,
        max=scores[-1] if scores else None,
        mean=sum(scores) / len(scores) if scores else None,
        percentiles={
            label: _percentile(scores, quantile)
            for label, quantile in (("p10", 0.10), ("p25", 0.25), ("p50", 0.50), ("p75", 0.75), ("p90", 0.90))
        },
        bins=[
            MarketBreadthBin(
                lower=float(lower),
                upper=float(lower + 10),
                count=sum(
                    lower <= score < lower + 10 or (lower == 90 and score == 100)
                    for score in scores
                ),
            )
            for lower in range(0, 100, 10)
        ],
    )


def _change_breadth(rows: Sequence[MarketScanBreadthRow]) -> MarketBreadthChange:
    values = [_number(item.change_pct) for item in rows]
    return MarketBreadthChange(
        advancing=sum(value is not None and value > 0 for value in values),
        flat=sum(value == 0 for value in values if value is not None),
        declining=sum(value is not None and value < 0 for value in values),
        missing=sum(value is None for value in values),
    )


def _industry_breadth(rows: Sequence[MarketScanBreadthRow]) -> list[MarketBreadthIndustry]:
    grouped: dict[str | None, list[MarketScanBreadthRow]] = {}
    for item in rows:
        grouped.setdefault(item.industry, []).append(item)
    result: list[MarketBreadthIndustry] = []
    for industry, items in sorted(grouped.items(), key=lambda pair: (pair[0] is None, pair[0] or "")):
        scores = [value for item in items if (value := _number(item.score)) is not None]
        result.append(
            MarketBreadthIndustry(
                industry=industry,
                count=len(items),
                score_present_count=len(scores),
                average_score=sum(scores) / len(scores) if scores else None,
            )
        )
    return result


def _funnel(
    population: Sequence[MarketScanScreeningRow],
    conditions: Sequence[CompiledScreenCondition],
) -> list[MarketScanFunnelStep]:
    current = list(population)
    steps: list[MarketScanFunnelStep] = []
    for index, condition in enumerate(conditions, start=1):
        outcomes = [(item, _condition_failure(item, condition)) for item in current]
        matched = [item for item, failure in outcomes if failure is None]
        failures = [failure for _item, failure in outcomes if failure is not None]
        steps.append(
            MarketScanFunnelStep(
                index=index,
                condition_code=condition.code,
                label=condition.label,
                input_count=len(current),
                matched_count=len(matched),
                excluded_count=len(current) - len(matched),
                missing_count=sum(failure.missing for failure in failures),
            )
        )
        current = matched
    return steps


def _failures(
    item: MarketScanScreeningRow,
    conditions: Sequence[CompiledScreenCondition],
) -> list[MarketScanFailedCondition]:
    return [failure for condition in conditions if (failure := _condition_failure(item, condition))]


def _condition_failure(
    item: MarketScanScreeningRow,
    condition: CompiledScreenCondition,
) -> MarketScanFailedCondition | None:
    value = _field_value(item, condition.field)
    missing = value is None or (condition.kind == "range" and _number(value) is None)
    passed = False if missing else _condition_passed(item, value, condition)
    if passed:
        return None
    code = f"{condition.code}.missing" if missing else condition.code
    return MarketScanFailedCondition(code=code, label=condition.label, missing=missing)


def _condition_passed(
    item: MarketScanScreeningRow,
    value: object,
    condition: CompiledScreenCondition,
) -> bool:
    if condition.kind == "exact":
        return value == condition.values[0]
    if condition.kind == "in":
        return value in condition.values
    if condition.kind == "contains_any":
        return any(_sqlite_like_contains(str(value), str(candidate)) for candidate in condition.values)
    if condition.kind == "keyword":
        needle = str(condition.values[0])
        return any(
            _sqlite_like_contains(candidate, needle)
            for candidate in (item.symbol, item.code, item.name)
        )
    number = _number(value)
    return number is not None and (
        condition.minimum is None or number >= condition.minimum
    ) and (condition.maximum is None or number <= condition.maximum)


def _field_value(item: MarketScanScreeningRow, field: str) -> object | None:
    if field == "keyword":
        return item.symbol
    if field in {"alpha_5d", "confidence", "risk", "tradability"}:
        return getattr(item, field)
    return getattr(item, field, None)


def _exclusion_reasons(
    failures: dict[str, list[MarketScanFailedCondition]],
    conditions: Sequence[CompiledScreenCondition],
) -> list[MarketScanExclusionReason]:
    labels = {condition.code: condition.label for condition in conditions}
    counters: Counter[str] = Counter()
    missing: Counter[str] = Counter()
    for item_failures in failures.values():
        for failure in item_failures:
            base_code = failure.code.removesuffix(".missing")
            counters[base_code] += 1
            missing[base_code] += int(failure.missing)
    return [
        MarketScanExclusionReason(
            code=code,
            label=labels[code],
            count=counters[code],
            missing_count=missing[code],
        )
        for code in labels
        if counters[code]
    ]


def _near_miss_candidates(
    population: Sequence[MarketScanScreeningRow],
    failures: dict[str, list[MarketScanFailedCondition]],
    sort: list[ScreenSortV2],
    *,
    limit: int,
    maximum_failures: int,
) -> list[MarketScanScreeningRow]:
    candidates = [
        item for item in population if 1 <= len(failures[item.symbol]) <= maximum_failures
    ]
    return _ordered(candidates, sort)[:limit]


def _hydrate_evaluation_items(
    repository: MarketScanScreeningRepositoryProtocol,
    run_id: int,
    population: Sequence[MarketScanScreeningRow],
    failures: dict[str, list[MarketScanFailedCondition]],
    ordered: Sequence[MarketScanScreeningRow],
    request: MarketScanScreenEvaluateRequest,
) -> tuple[list[MarketScanResultItem], list[MarketScanNearMiss]]:
    offset = (request.page - 1) * request.page_size
    page_rows = list(ordered[offset : offset + request.page_size])
    near_miss_rows = _near_miss_candidates(
        population,
        failures,
        request.spec.sort,
        limit=request.near_miss_limit,
        maximum_failures=request.near_miss_max_failures,
    )
    hydrated = _hydrate_selected(
        repository,
        run_id,
        [item.symbol for item in (*page_rows, *near_miss_rows)],
    )
    page_items = [hydrated[item.symbol] for item in page_rows]
    near_misses = [
        MarketScanNearMiss(
            item=hydrated[item.symbol],
            failed_conditions=failures[item.symbol],
        )
        for item in near_miss_rows
    ]
    return page_items, near_misses


def _hydrate_selected(
    repository: MarketScanScreeningRepositoryProtocol,
    run_id: int,
    symbols: Sequence[str],
) -> dict[str, MarketScanResultItem]:
    unique_symbols = tuple(dict.fromkeys(symbols))
    if not unique_symbols:
        return {}
    items = repository.market_scan_screening_result_items(run_id, unique_symbols)
    observed_symbols = [item.symbol for item in items]
    if len(observed_symbols) != len(set(observed_symbols)):
        raise RuntimeError("冻结筛选结果详情包含重复股票")
    unexpected = sorted(set(observed_symbols) - set(unique_symbols))
    if unexpected:
        raise RuntimeError("冻结筛选结果详情包含未请求股票：" + "、".join(unexpected[:10]))
    by_symbol = {item.symbol: item for item in items}
    missing = [symbol for symbol in unique_symbols if symbol not in by_symbol]
    if missing:
        raise RuntimeError("冻结筛选结果在详情读取时发生变化：" + "、".join(missing[:10]))
    return by_symbol


def _ordered(
    rows: Sequence[MarketScanScreeningRow],
    sort: list[ScreenSortV2],
) -> list[MarketScanScreeningRow]:
    ordered = sorted(rows, key=lambda item: item.symbol)
    if not (len(sort) == 1 and sort[0].field == "rank"):
        ordered = _sort_present_first(
            ordered,
            lambda item: _sortable(item.raw_score),
            reverse=True,
        )
    for field in reversed(sort):
        ordered = _sort_present_first(
            ordered,
            _field_sort_key(field.field),
            reverse=field.order == "desc",
        )
    return ordered


def _sort_present_first(
    rows: Sequence[MarketScanScreeningRow],
    key: Callable[[MarketScanScreeningRow], tuple[int, str, float] | None],
    *,
    reverse: bool,
) -> list[MarketScanScreeningRow]:
    present = [item for item in rows if key(item) is not None]
    missing = [item for item in rows if key(item) is None]
    return sorted(
        present,
        key=lambda item: _required_sort_key(key, item),
        reverse=reverse,
    ) + missing


def _required_sort_key(
    key: Callable[[MarketScanScreeningRow], tuple[int, str, float] | None],
    item: MarketScanScreeningRow,
) -> tuple[int, str, float]:
    value = key(item)
    if value is None:
        raise RuntimeError("已筛除的空排序键重新出现")
    return value


def _field_sort_key(
    field: ScreenSortField,
) -> Callable[[MarketScanScreeningRow], tuple[int, str, float] | None]:
    return lambda item: _sortable(_field_value(item, field))


def _sortable(value: object | None) -> tuple[int, str, float] | None:
    if isinstance(value, bool):
        return (0, "", float(value))
    if isinstance(value, str):
        return (1, value, 0.0)
    number = _number(value)
    return None if number is None else (0, "", number)


def _sqlite_like_contains(value: str, needle: str) -> bool:
    """Match SQLite LIKE's default ASCII folding while treating wildcards literally."""

    return _ascii_fold(needle) in _ascii_fold(value)


def _ascii_fold(value: str) -> str:
    return value.translate(str.maketrans("ABCDEFGHIJKLMNOPQRSTUVWXYZ", "abcdefghijklmnopqrstuvwxyz"))


def _number(value: object | None) -> float | None:
    if isinstance(value, bool) or not isinstance(value, int | float):
        return None
    parsed = float(value)
    return parsed if math.isfinite(parsed) else None


def _percentile(values: list[float], quantile: float) -> float | None:
    if not values:
        return None
    position = (len(values) - 1) * quantile
    lower = math.floor(position)
    upper = math.ceil(position)
    if lower == upper:
        return values[lower]
    weight = position - lower
    return values[lower] * (1 - weight) + values[upper] * weight


def _page_count(total: int, page_size: int) -> int:
    return (total + page_size - 1) // page_size if total else 0


__all__ = [
    "MarketScanScreeningRepositoryProtocol",
    "MarketScanScreeningService",
    "MarketScanScreeningUnavailable",
]
