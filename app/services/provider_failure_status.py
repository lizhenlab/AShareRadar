from __future__ import annotations

from app.utils.market_data import finite_float
from app.utils.time import non_negative_seconds_since_text


RECENT_PROVIDER_FAILURE_SECONDS = 30 * 60


def provider_recently_failed(status: object, *, window_seconds: int = RECENT_PROVIDER_FAILURE_SECONDS) -> bool:
    return bool(
        getattr(status, "enabled", False)
        and not getattr(status, "healthy", True)
        and status_updated_recently(getattr(status, "updated_at", None), window_seconds=window_seconds)
    )


def capability_recently_failed(status: object, *, window_seconds: int = RECENT_PROVIDER_FAILURE_SECONDS) -> bool:
    return bool(
        getattr(status, "enabled", False)
        and not getattr(status, "healthy", True)
        and capability_has_failure_activity(status)
        and status_updated_recently(getattr(status, "updated_at", None), window_seconds=window_seconds)
    )


def capability_has_failure_activity(status: object) -> bool:
    return bool(_clean_text(getattr(status, "last_error", None)) or _positive_count(getattr(status, "failure_count", 0)) > 0)


def status_updated_recently(updated_at: object, *, window_seconds: int = RECENT_PROVIDER_FAILURE_SECONDS) -> bool:
    if updated_at is None:
        return True
    if not isinstance(updated_at, str):
        return False
    text = updated_at.strip()
    if not text:
        return True
    age = non_negative_seconds_since_text(text)
    return age is not None and age <= max(0, window_seconds)


def _positive_count(raw_value: object) -> int:
    value = finite_float(raw_value)
    if value is None or value <= 0:
        return 0
    return int(value)


def _clean_text(value: object) -> str | None:
    if value is None:
        return None
    if isinstance(value, str):
        text = " ".join(value.split())
    else:
        try:
            float(value)
        except (TypeError, ValueError):
            text = " ".join(str(value).split())
        else:
            return None
    invalid_text = {"nan", "none", "null", "inf", "+inf", "-inf", "infinity", "+infinity", "-infinity"}
    return text if text and text.lower() not in invalid_text else None
