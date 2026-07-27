"""Backward-compatible provider error exports.

Provider error contracts live in ``app.utils.provider_errors`` so persistence
and mapping layers can sanitize external failures without depending on services.
"""

from app.utils.provider_errors import (
    REDACTED,
    CoverageMissError,
    ProviderChainUnavailable,
    ProviderCoverageMiss,
    ProviderError,
    ProviderInstrumentDataError,
    ProviderProtocolError,
    ProviderTransportError,
    is_provider_coverage_miss,
    sanitize_provider_error,
)


__all__ = [
    "REDACTED",
    "CoverageMissError",
    "ProviderChainUnavailable",
    "ProviderCoverageMiss",
    "ProviderError",
    "ProviderInstrumentDataError",
    "ProviderProtocolError",
    "ProviderTransportError",
    "is_provider_coverage_miss",
    "sanitize_provider_error",
]
