from __future__ import annotations

from collections.abc import Callable, Iterator
from dataclasses import dataclass

from app.services.datahub_runtime import ProviderCallBusyError, ProviderCallTimeoutError
from app.services.market_scan_contracts import MarketScanProviderStateProtocol
from app.utils.provider_errors import ProviderChainUnavailable, ProviderCoverageMiss


SYSTEMIC_UNAVAILABLE_RATIO = 0.25
SYSTEMIC_COOLDOWN_RECHECK_CAP_SECONDS = 5.0
MAX_ADAPTIVE_BACKOFF_SECONDS = 30.0
_JITTER_SEQUENCE = (0.10, -0.05, 0.05, 0.0)


@dataclass(frozen=True)
class MarketScanPressureDecision:
    pressured: bool
    provider_failure_observed: bool
    minimum_delay_seconds: float


@dataclass(frozen=True)
class MarketScanPressureSnapshot:
    max_concurrency: int
    current_concurrency: int
    completed_batches: int
    pressure_events: int
    consecutive_healthy_batches: int
    last_unavailable_ratio: float
    last_retry_after_seconds: float
    last_backoff_seconds: float
    total_backoff_seconds: float
    last_signal: str


class MarketScanPressureController:
    """Apply batch-level AIMD without retaining symbol-level diagnostics."""

    def __init__(
        self,
        max_concurrency: int,
        *,
        retry_backoff_seconds: float,
        provider_chain_state: Callable[[str], object | None] | None = None,
    ) -> None:
        self.max_concurrency = max(1, int(max_concurrency))
        self.retry_backoff_seconds = max(0.0, float(retry_backoff_seconds))
        self._provider_chain_state = provider_chain_state
        self.reset()

    @classmethod
    def from_settings(cls, settings: object) -> MarketScanPressureController:
        return cls(
            getattr(settings, "market_scan_concurrency", 1),
            retry_backoff_seconds=getattr(
                settings,
                "market_scan_retry_backoff_seconds",
                0.0,
            ),
        )

    @classmethod
    def from_datahub(cls, datahub: object) -> MarketScanPressureController:
        controller = cls.from_settings(getattr(datahub, "settings"))
        if isinstance(datahub, MarketScanProviderStateProtocol):
            controller._provider_chain_state = datahub.provider_chain_state
        return controller

    @property
    def current_concurrency(self) -> int:
        return self._current_concurrency

    def reset(self) -> None:
        self._current_concurrency = self.max_concurrency
        self._completed_batches = 0
        self._pressure_events = 0
        self._pressure_streak = 0
        self._consecutive_healthy_batches = 0
        self._last_unavailable_ratio = 0.0
        self._last_retry_after_seconds = 0.0
        self._last_backoff_seconds = 0.0
        self._total_backoff_seconds = 0.0
        self._last_signal = "none"
        self._batch_provider_failure_observed = False

    def observe_failures(
        self,
        errors: tuple[BaseException, ...],
        *,
        attempted_count: int,
        unavailable_count: int | None = None,
    ) -> MarketScanPressureDecision:
        pressure_errors = tuple(error for error in errors if _is_pressure_candidate(error) and not _is_coverage_only(error))
        if not pressure_errors:
            return MarketScanPressureDecision(False, False, 0.0)

        self._batch_provider_failure_observed = True
        busy_count = sum(_contains_exception(error, ProviderCallBusyError) for error in pressure_errors)
        timeout_count = sum(_contains_timeout(error) for error in pressure_errors)
        retry_after = max(
            (provider_retry_after_seconds(error) for error in pressure_errors),
            default=0.0,
        )
        unavailable = unavailable_count if unavailable_count is not None else sum(isinstance(error, ProviderChainUnavailable) for error in pressure_errors)
        unavailable_ratio = min(1.0, max(0, unavailable) / max(1, attempted_count))
        systemic = unavailable_ratio >= SYSTEMIC_UNAVAILABLE_RATIO
        pressured = bool(busy_count or timeout_count or retry_after > 0 or systemic)

        self._consecutive_healthy_batches = 0
        self._last_unavailable_ratio = unavailable_ratio
        self._last_retry_after_seconds = retry_after
        if not pressured:
            self._last_backoff_seconds = 0.0
            self._last_signal = "isolated_unavailable"
            return MarketScanPressureDecision(False, True, 0.0)

        self._pressure_events += 1
        self._pressure_streak += 1
        self._current_concurrency = max(1, self._current_concurrency // 2)
        delay = self._adaptive_delay(retry_after)
        if systemic and not busy_count and not timeout_count:
            delay = min(delay, SYSTEMIC_COOLDOWN_RECHECK_CAP_SECONDS)
        self._last_backoff_seconds = delay
        self._last_signal = _signal_name(
            busy=bool(busy_count),
            timeout=bool(timeout_count),
            retry_after=retry_after > 0,
            systemic=systemic,
        )
        return MarketScanPressureDecision(True, True, delay)

    def observe_quote_failure(
        self,
        error: ProviderChainUnavailable,
        attempted_count: int,
    ) -> MarketScanPressureDecision:
        return self.observe_failures(
            (error,),
            attempted_count=attempted_count,
            unavailable_count=attempted_count,
        )

    def observe_kline_failures(
        self,
        errors: tuple[ProviderChainUnavailable, ...],
        attempted_count: int,
    ) -> MarketScanPressureDecision:
        return self.observe_failures(
            errors,
            attempted_count=attempted_count,
            unavailable_count=len(errors),
        )

    def complete_batch(self) -> None:
        healthy = not self._batch_provider_failure_observed
        self._batch_provider_failure_observed = False
        self._completed_batches += 1
        if not healthy:
            self._consecutive_healthy_batches = 0
            return
        self._pressure_streak = 0
        self._consecutive_healthy_batches += 1
        if self._current_concurrency < self.max_concurrency:
            self._current_concurrency += 1

    def snapshot(self) -> MarketScanPressureSnapshot:
        return MarketScanPressureSnapshot(
            max_concurrency=self.max_concurrency,
            current_concurrency=self._current_concurrency,
            completed_batches=self._completed_batches,
            pressure_events=self._pressure_events,
            consecutive_healthy_batches=self._consecutive_healthy_batches,
            last_unavailable_ratio=self._last_unavailable_ratio,
            last_retry_after_seconds=self._last_retry_after_seconds,
            last_backoff_seconds=self._last_backoff_seconds,
            total_backoff_seconds=self._total_backoff_seconds,
            last_signal=self._last_signal,
        )

    def record_backoff(self, seconds: float) -> None:
        self._total_backoff_seconds += max(0.0, seconds)

    def terminal_warnings(self, warnings: list[str]) -> tuple[str, ...]:
        unique = list(dict.fromkeys(warnings))
        snapshot = self.snapshot()
        if snapshot.pressure_events:
            summary = (
                f"扫描压力控制：触发 {snapshot.pressure_events} 次，"
                f"结束并发 {snapshot.current_concurrency}/{snapshot.max_concurrency}，"
                f"最后信号 {snapshot.last_signal}，累计退避 {snapshot.total_backoff_seconds:.2f} 秒"
            )
            unique.insert(0, summary)
        return tuple(unique)

    def unavailable_error(
        self,
        error: BaseException,
        message: str,
    ) -> ProviderChainUnavailable:
        retry_after = provider_retry_after_seconds(error)
        if not isinstance(error, ProviderCallBusyError):
            retry_after = max(self.retry_backoff_seconds, retry_after)
        return ProviderChainUnavailable(message, retry_after_seconds=retry_after)

    def recovery_errors(
        self,
        errors: tuple[ProviderChainUnavailable, ...],
        minimum_seconds: float,
    ) -> tuple[ProviderChainUnavailable, ...]:
        return errors_with_minimum_retry_after(errors, minimum_seconds)

    def provider_chain_state(self, kind: str) -> object | None:
        if self._provider_chain_state is None:
            return None
        return self._provider_chain_state(kind)

    def _adaptive_delay(self, retry_after_seconds: float) -> float:
        exponent = min(5, max(0, self._pressure_streak - 1))
        base_delay = min(
            MAX_ADAPTIVE_BACKOFF_SECONDS,
            self.retry_backoff_seconds * (2**exponent),
        )
        jitter = _JITTER_SEQUENCE[(self._pressure_events - 1) % len(_JITTER_SEQUENCE)]
        jittered_delay = min(
            MAX_ADAPTIVE_BACKOFF_SECONDS,
            base_delay * (1.0 + jitter),
        )
        return max(retry_after_seconds, jittered_delay)


def provider_retry_after_seconds(error: BaseException) -> float:
    values = [max(0.0, float(value)) for nested in _exception_chain(error) if (value := getattr(nested, "retry_after_seconds", None)) is not None]
    return max(values, default=0.0)


def errors_with_minimum_retry_after(
    errors: tuple[ProviderChainUnavailable, ...],
    minimum_seconds: float,
) -> tuple[ProviderChainUnavailable, ...]:
    minimum = max(0.0, minimum_seconds)
    if minimum <= 0:
        return errors
    adjusted: list[ProviderChainUnavailable] = []
    for error in errors:
        delay = max(minimum, provider_retry_after_seconds(error))
        replacement = ProviderChainUnavailable(str(error), retry_after_seconds=delay)
        replacement.__cause__ = error.__cause__
        adjusted.append(replacement)
    return tuple(adjusted)


def _contains_exception(error: BaseException, error_type: type[BaseException]) -> bool:
    return any(isinstance(nested, error_type) for nested in _exception_chain(error))


def _contains_timeout(error: BaseException) -> bool:
    return any(isinstance(nested, (ProviderCallTimeoutError, TimeoutError)) for nested in _exception_chain(error))


def _is_coverage_only(error: BaseException) -> bool:
    nested = tuple(_exception_chain(error))
    has_coverage_miss = any(isinstance(item, ProviderCoverageMiss) for item in nested)
    has_pressure = any(isinstance(item, (ProviderCallBusyError, ProviderCallTimeoutError, TimeoutError)) for item in nested)
    return has_coverage_miss and not has_pressure


def _is_pressure_candidate(error: BaseException) -> bool:
    return any(
        isinstance(
            item,
            (
                ProviderChainUnavailable,
                ProviderCallBusyError,
                ProviderCallTimeoutError,
                TimeoutError,
            ),
        )
        for item in _exception_chain(error)
    )


def _exception_chain(error: BaseException) -> Iterator[BaseException]:
    pending = [error]
    seen: set[int] = set()
    while pending:
        current = pending.pop()
        if id(current) in seen:
            continue
        seen.add(id(current))
        yield current
        if isinstance(current, BaseExceptionGroup):
            pending.extend(current.exceptions)
        if current.__cause__ is not None:
            pending.append(current.__cause__)
        elif current.__context__ is not None:
            pending.append(current.__context__)


def _signal_name(*, busy: bool, timeout: bool, retry_after: bool, systemic: bool) -> str:
    names = [
        name
        for enabled, name in (
            (busy, "busy"),
            (timeout, "timeout"),
            (retry_after, "retry_after"),
            (systemic, "systemic_unavailable"),
        )
        if enabled
    ]
    return "+".join(names) or "none"


__all__ = [
    "MarketScanPressureController",
    "MarketScanPressureDecision",
    "MarketScanPressureSnapshot",
    "errors_with_minimum_retry_after",
    "provider_retry_after_seconds",
]
