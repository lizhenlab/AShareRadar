"""Pure diagnostic calibration metrics for market-scan evaluations."""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from statistics import fmean
from typing import Protocol


class CalibrationObservation(Protocol):
    """Read-only observation fields required by ordinal calibration diagnostics."""

    @property
    def mode(self) -> str: ...

    @property
    def scope(self) -> str: ...

    @property
    def rule_version(self) -> str: ...

    @property
    def quote_date(self) -> str: ...

    @property
    def raw_score(self) -> float: ...

    @property
    def returns(self) -> Mapping[int, float]: ...


class CalibrationConfig(Protocol):
    """Read-only evaluation settings required by calibration diagnostics."""

    @property
    def horizons(self) -> Sequence[int]: ...

    @property
    def minimum_session_count(self) -> int: ...


def calibration_metrics(
    observations: Sequence[CalibrationObservation],
    config: CalibrationConfig,
) -> list[dict[str, object]]:
    records: list[dict[str, object]] = []
    for mode, scope, rule_version, rows in _contract_rows(observations):
        for horizon in config.horizons:
            records.append(calibration_record(mode, scope, rule_version, rows, horizon, config))
    return records


def calibration_record(
    mode: str,
    scope: str,
    rule_version: str,
    rows: Sequence[CalibrationObservation],
    horizon: int,
    config: CalibrationConfig,
) -> dict[str, object]:
    horizon_rows = [item for item in rows if horizon in item.returns]
    session_count = len({item.quote_date for item in horizon_rows})
    return {
        "mode": mode,
        "scope": scope,
        "rule_version": rule_version,
        "horizon_trading_days": horizon,
        "status": "diagnostic-only" if session_count >= config.minimum_session_count else "insufficient_data",
        "independent_session_count": session_count,
        "score_semantics": "ordinal-not-probability",
        "probability_calibration_allowed": False,
        "buckets": [calibration_bucket(horizon_rows, horizon, lower) for lower in range(0, 100, 10)],
    }


def calibration_bucket(
    rows: Sequence[CalibrationObservation],
    horizon: int,
    lower: int,
) -> dict[str, object]:
    upper = lower + 10
    selected = [
        item
        for item in rows
        if lower <= item.raw_score <= upper and (upper == 100 or item.raw_score < upper)
    ]
    values = [item.returns[horizon] for item in selected]
    return {
        "score_range": [lower, upper],
        "sample_size": len(values),
        "independent_session_count": len({item.quote_date for item in selected}),
        "average_return": fmean(values) if values else None,
        "positive_return_rate": sum(value > 0 for value in values) / len(values) if values else None,
    }


def _contract_rows(
    observations: Sequence[CalibrationObservation],
) -> list[tuple[str, str, str, tuple[CalibrationObservation, ...]]]:
    contracts = sorted({(item.mode, item.scope, item.rule_version) for item in observations})
    return [
        (
            mode,
            scope,
            rule_version,
            tuple(
                item
                for item in observations
                if (item.mode, item.scope, item.rule_version) == (mode, scope, rule_version)
            ),
        )
        for mode, scope, rule_version in contracts
    ]


__all__ = [
    "CalibrationConfig",
    "CalibrationObservation",
    "calibration_bucket",
    "calibration_metrics",
    "calibration_record",
]
