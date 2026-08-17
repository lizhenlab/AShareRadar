"""Trusted exchange-session coverage for immutable market-scan evidence.

The contract records missing sessions; it never synthesizes OHLCV bars.  The
expected exchange-session list is sealed into the evidence so a later calendar
refresh cannot silently change replay semantics.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date
import hashlib
import json
from typing import Iterable, Mapping, Sequence

from app.models.market import Kline
from app.models.market_scan import MARKET_SCAN_MIN_HISTORY_ROWS
from app.services.trading_calendar import trading_date_range


MARKET_SCAN_SESSION_COVERAGE_SCHEMA_VERSION = 1
MARKET_SCAN_SESSION_COVERAGE_CONTRACT_VERSION = "market-scan-session-coverage-v1"
MARKET_SCAN_SESSION_COVERAGE_WINDOWS = (5, 20, 60)
MARKET_SCAN_SESSION_CONFIDENCE_PENALTY_CAP = 30.0
_TRUSTED_CALENDAR_SOURCES = {"runtime_cache", "bundled_baseline"}


@dataclass(frozen=True)
class MarketScanSessionWindowCoverage:
    expected_session_count: int
    observed_session_count: int
    missing_session_count: int
    missing_session_ratio: float

    def as_dict(self) -> dict[str, object]:
        return {
            "expected_session_count": self.expected_session_count,
            "observed_session_count": self.observed_session_count,
            "missing_session_count": self.missing_session_count,
            "missing_session_ratio": self.missing_session_ratio,
        }


@dataclass(frozen=True)
class MarketScanSessionCoverage:
    calendar_source: str
    expected_session_dates: tuple[str, ...]
    observed_session_dates: tuple[str, ...]
    missing_session_dates: tuple[str, ...]
    max_gap_sessions: int
    recent_windows: dict[str, MarketScanSessionWindowCoverage]

    @property
    def action_eligible(self) -> bool:
        return not self.missing_session_dates and self.max_gap_sessions == 0

    @property
    def confidence_penalty(self) -> float:
        recent_20 = self.recent_windows["20"].missing_session_ratio
        recent_60 = self.recent_windows["60"].missing_session_ratio
        return round(
            min(
                MARKET_SCAN_SESSION_CONFIDENCE_PENALTY_CAP,
                self.max_gap_sessions * 4.0 + recent_20 * 10.0 + recent_60 * 10.0,
            ),
            4,
        )

    def as_dict(self) -> dict[str, object]:
        expected = self.expected_session_dates
        observed = self.observed_session_dates
        missing = self.missing_session_dates
        return {
            "schema_version": MARKET_SCAN_SESSION_COVERAGE_SCHEMA_VERSION,
            "contract_version": MARKET_SCAN_SESSION_COVERAGE_CONTRACT_VERSION,
            "status": "verified",
            "calendar_source": self.calendar_source,
            "expected_session_count": len(expected),
            "observed_session_count": len(observed),
            "missing_session_count": len(missing),
            "missing_session_ratio": _ratio(len(missing), len(expected)),
            "max_gap_sessions": self.max_gap_sessions,
            "expected_session_dates": list(expected),
            "missing_session_dates": list(missing),
            "expected_sessions_digest": _stable_digest(expected),
            "observed_sessions_digest": _stable_digest(observed),
            "recent_windows": {
                key: value.as_dict()
                for key, value in self.recent_windows.items()
            },
            "confidence_penalty": self.confidence_penalty,
            "action_eligible": self.action_eligible,
        }


def build_market_scan_session_coverage(rows: Sequence[Kline]) -> MarketScanSessionCoverage:
    """Build coverage for the exact 61 bars persisted in score evidence."""
    if len(rows) < MARKET_SCAN_MIN_HISTORY_ROWS:
        raise ValueError(f"会话覆盖至少需要 {MARKET_SCAN_MIN_HISTORY_ROWS} 根日K")
    observed = _strict_session_dates(row.date for row in rows[-MARKET_SCAN_MIN_HISTORY_ROWS:])
    start, end = date.fromisoformat(observed[0]), date.fromisoformat(observed[-1])
    expected_dates, calendar = trading_date_range(start, end)
    expected = tuple(item.isoformat() for item in expected_dates)
    if not expected or not set(observed).issubset(expected):
        raise ValueError("日K日期不属于可信交易所会话序列")
    return _coverage(
        expected,
        observed,
        calendar_source=calendar.source.value,
    )


def verify_market_scan_session_coverage(
    value: object,
    *,
    bar_contract: object,
) -> bool:
    """Verify a sealed coverage payload against the evidence's real bars."""
    if not isinstance(value, Mapping) or not isinstance(bar_contract, Sequence):
        return False
    if (
        value.get("schema_version") != MARKET_SCAN_SESSION_COVERAGE_SCHEMA_VERSION
        or value.get("contract_version") != MARKET_SCAN_SESSION_COVERAGE_CONTRACT_VERSION
        or value.get("status") != "verified"
        or value.get("calendar_source") not in _TRUSTED_CALENDAR_SOURCES
    ):
        return False
    try:
        observed = _bar_session_dates(bar_contract)
        expected_value = value.get("expected_session_dates")
        if not isinstance(expected_value, Sequence) or isinstance(expected_value, str | bytes):
            return False
        expected = _strict_session_dates(expected_value)
        trusted_dates, _calendar = trading_date_range(
            date.fromisoformat(observed[0]),
            date.fromisoformat(observed[-1]),
        )
        trusted = tuple(item.isoformat() for item in trusted_dates)
        if expected != trusted:
            return False
        sealed = _coverage(
            expected,
            observed,
            calendar_source=str(value["calendar_source"]),
        ).as_dict()
    except (KeyError, TypeError, ValueError):
        return False
    return dict(value) == sealed


def _coverage(
    expected: tuple[str, ...],
    observed: tuple[str, ...],
    *,
    calendar_source: str,
) -> MarketScanSessionCoverage:
    if not expected or not observed or expected[0] != observed[0] or expected[-1] != observed[-1]:
        raise ValueError("会话覆盖边界必须由真实观测日K锚定")
    expected_set, observed_set = set(expected), set(observed)
    if not observed_set.issubset(expected_set):
        raise ValueError("观测日K包含非预期交易所会话")
    missing = tuple(item for item in expected if item not in observed_set)
    index_by_date = {item: index for index, item in enumerate(expected)}
    max_gap = max(
        (
            index_by_date[current] - index_by_date[previous] - 1
            for previous, current in zip(observed, observed[1:], strict=False)
        ),
        default=0,
    )
    windows = {
        str(size): _window_coverage(expected, observed_set, size)
        for size in MARKET_SCAN_SESSION_COVERAGE_WINDOWS
    }
    return MarketScanSessionCoverage(
        calendar_source=calendar_source,
        expected_session_dates=expected,
        observed_session_dates=observed,
        missing_session_dates=missing,
        max_gap_sessions=max_gap,
        recent_windows=windows,
    )


def _window_coverage(
    expected: tuple[str, ...],
    observed: set[str],
    size: int,
) -> MarketScanSessionWindowCoverage:
    sessions = expected[-size:]
    observed_count = sum(item in observed for item in sessions)
    missing_count = len(sessions) - observed_count
    return MarketScanSessionWindowCoverage(
        expected_session_count=len(sessions),
        observed_session_count=observed_count,
        missing_session_count=missing_count,
        missing_session_ratio=_ratio(missing_count, len(sessions)),
    )


def _bar_session_dates(bar_contract: Sequence[object]) -> tuple[str, ...]:
    dates: list[object] = []
    for row in bar_contract:
        if not isinstance(row, Sequence) or isinstance(row, str | bytes) or not row:
            raise ValueError("bar_contract 行格式无效")
        dates.append(row[0])
    return _strict_session_dates(dates)


def _strict_session_dates(values: Iterable[object]) -> tuple[str, ...]:
    result: list[str] = []
    for value in values:
        text = str(value)
        parsed = date.fromisoformat(text)
        if text != parsed.isoformat():
            raise ValueError("交易日必须使用规范 ISO 日期")
        result.append(text)
    if not result or result != sorted(set(result)):
        raise ValueError("交易日必须严格递增且不得重复")
    return tuple(result)


def _ratio(numerator: int, denominator: int) -> float:
    return round(numerator / denominator, 8) if denominator else 0.0


def _stable_digest(value: object) -> str:
    payload = json.dumps(
        value,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
        allow_nan=False,
    ).encode("utf-8")
    return hashlib.sha256(payload).hexdigest()


__all__ = [
    "MARKET_SCAN_SESSION_COVERAGE_CONTRACT_VERSION",
    "MARKET_SCAN_SESSION_COVERAGE_SCHEMA_VERSION",
    "MarketScanSessionCoverage",
    "build_market_scan_session_coverage",
    "verify_market_scan_session_coverage",
]
