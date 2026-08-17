from __future__ import annotations

from collections.abc import Iterator
from datetime import date
import json
from pathlib import Path

import pytest

from app.utils import exchange_calendar_contract


@pytest.fixture(autouse=True)
def _reset_bundled_exchange_calendar() -> Iterator[None]:
    exchange_calendar_contract.bundled_exchange_sessions.cache_clear()
    yield
    exchange_calendar_contract.bundled_exchange_sessions.cache_clear()


def _install_calendar(
    path: Path,
    payload: object,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    path.write_text(json.dumps(payload), encoding="utf-8")
    exchange_calendar_contract.bundled_exchange_sessions.cache_clear()
    monkeypatch.setattr(exchange_calendar_contract, "_CALENDAR_PATH", path)


@pytest.mark.parametrize(
    "payload",
    [
        {},
        {"trade_dates": ["not-a-date"]},
        {"trade_dates": None},
    ],
)
def test_unreadable_calendar_payload_fails_closed(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    payload: object,
) -> None:
    _install_calendar(tmp_path / "calendar.json", payload, monkeypatch)

    assert exchange_calendar_contract.bundled_exchange_sessions() == frozenset()
    assert exchange_calendar_contract.is_bundled_exchange_session(date(2026, 8, 13)) is False


@pytest.mark.parametrize(
    "payload",
    [
        {"trade_dates": [], "trade_date_count": 0, "min_date": "", "max_date": ""},
        {"trade_dates": ["2026-08-13", "2026-08-12"], "trade_date_count": 2, "min_date": "2026-08-13", "max_date": "2026-08-12"},
        {"trade_dates": ["2026-08-12", "2026-08-12"], "trade_date_count": 2, "min_date": "2026-08-12", "max_date": "2026-08-12"},
        {"trade_dates": ["2026-08-12"], "trade_date_count": 2, "min_date": "2026-08-12", "max_date": "2026-08-12"},
        {"trade_dates": ["2026-08-12"], "trade_date_count": 1, "min_date": "2026-08-11", "max_date": "2026-08-12"},
        {"trade_dates": ["2026-08-12"], "trade_date_count": 1, "min_date": "2026-08-12", "max_date": "2026-08-13"},
    ],
)
def test_internally_inconsistent_calendar_manifest_fails_closed(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    payload: object,
) -> None:
    _install_calendar(tmp_path / "calendar.json", payload, monkeypatch)

    assert exchange_calendar_contract.bundled_exchange_sessions() == frozenset()


def test_valid_calendar_manifest_returns_only_attested_sessions(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _install_calendar(
        tmp_path / "calendar.json",
        {
            "trade_dates": ["2026-08-12", "2026-08-13"],
            "trade_date_count": 2,
            "min_date": "2026-08-12",
            "max_date": "2026-08-13",
        },
        monkeypatch,
    )

    assert exchange_calendar_contract.bundled_exchange_sessions() == frozenset(
        {date(2026, 8, 12), date(2026, 8, 13)}
    )
    assert exchange_calendar_contract.is_bundled_exchange_session(date(2026, 8, 11)) is False
