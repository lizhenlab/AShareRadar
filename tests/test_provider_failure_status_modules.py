from __future__ import annotations

from types import SimpleNamespace

from app.services.provider_failure_status import (
    capability_has_failure_activity,
    capability_recently_failed,
    provider_recently_failed,
    status_updated_recently,
)
from app.utils.time import seconds_ago_text


def test_provider_recently_failed_keeps_missing_timestamp_as_recent_for_legacy_rows() -> None:
    status = SimpleNamespace(enabled=True, healthy=False, updated_at=None)

    assert provider_recently_failed(status) is True


def test_provider_recently_failed_ignores_stale_or_future_timestamps() -> None:
    stale = SimpleNamespace(enabled=True, healthy=False, updated_at=seconds_ago_text(31 * 60))
    future = SimpleNamespace(enabled=True, healthy=False, updated_at="2999-01-01 09:30:00")

    assert provider_recently_failed(stale) is False
    assert provider_recently_failed(future) is False


def test_capability_recently_failed_requires_failure_activity() -> None:
    blank_error = SimpleNamespace(enabled=True, healthy=False, last_error=" \n\t ", failure_count=0, updated_at=None)
    dirty_count = SimpleNamespace(enabled=True, healthy=False, last_error=None, failure_count=float("nan"), updated_at=None)
    positive_count = SimpleNamespace(enabled=True, healthy=False, last_error=None, failure_count=2, updated_at=None)

    assert capability_has_failure_activity(blank_error) is False
    assert capability_has_failure_activity(dirty_count) is False
    assert capability_recently_failed(blank_error) is False
    assert capability_recently_failed(dirty_count) is False
    assert capability_recently_failed(positive_count) is True


def test_status_updated_recently_honors_custom_window() -> None:
    updated_at = seconds_ago_text(45 * 60)

    assert status_updated_recently(updated_at) is False
    assert status_updated_recently(updated_at, window_seconds=60 * 60) is True
