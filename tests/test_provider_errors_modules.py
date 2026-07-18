from __future__ import annotations

import pytest

from app.services.provider_errors import REDACTED, sanitize_provider_error


@pytest.mark.parametrize(
    ("raw", "secret"),
    [
        ('headers={"X-Api-Key": "super-secret-123"}', "super-secret-123"),
        ("headers={'token': 'private-token-456'}", "private-token-456"),
        ('headers={"Authorization": "Basic dXNlcjpwYXNz"}', "dXNlcjpwYXNz"),
        ('payload={"client_secret" = "client-secret-789"}', "client-secret-789"),
    ],
)
def test_sanitize_provider_error_redacts_quoted_mapping_credentials(raw: str, secret: str) -> None:
    sanitized = sanitize_provider_error(raw)

    assert secret not in sanitized
    assert REDACTED in sanitized


def test_sanitize_provider_error_keeps_non_sensitive_mapping_values() -> None:
    raw = 'payload={"symbol": "600519", "message": "source unavailable"}'

    assert sanitize_provider_error(raw) == raw
