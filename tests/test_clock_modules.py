from __future__ import annotations

from datetime import UTC, datetime
import os
from pathlib import Path
import subprocess
import sys

from app.utils import clock
from app.utils import audit_time
from app.utils.audit_time import audit_datetime_to_text, parse_audit_time
from app.utils.time import datetime_to_text, parse_text_time


ROOT = Path(__file__).resolve().parents[1]


def test_market_clock_converts_one_instant_to_shanghai(monkeypatch) -> None:
    instant = datetime(2026, 7, 24, 1, 30, tzinfo=UTC)
    monkeypatch.setattr(clock, "utc_now", lambda: instant)

    assert clock.market_now().isoformat() == "2026-07-24T09:30:00+08:00"
    assert clock.market_now_naive() == datetime(2026, 7, 24, 9, 30)


def test_utc_clock_is_timezone_aware() -> None:
    current = clock.utc_now()

    assert current.tzinfo is UTC
    assert current.utcoffset().total_seconds() == 0


def test_legacy_text_time_stays_shanghai_naive() -> None:
    aware_utc = datetime(2026, 7, 24, 1, 30, tzinfo=UTC)

    assert datetime_to_text(aware_utc) == "2026-07-24 09:30:00"
    assert parse_text_time("2026-07-24 09:30:00") == datetime(2026, 7, 24, 9, 30)
    assert parse_text_time("2026-07-24T01:30:00Z") == datetime(2026, 7, 24, 9, 30)


def test_audit_text_is_utc_and_accepts_legacy_shanghai_values(monkeypatch) -> None:
    instant = datetime(2026, 7, 24, 1, 30, 0, 123456, tzinfo=UTC)
    monkeypatch.setattr(audit_time, "utc_now", lambda: instant)

    assert audit_time.audit_now_text() == "2026-07-24T01:30:00.123456Z"
    assert audit_datetime_to_text(datetime(2026, 7, 24, 9, 30)) == "2026-07-24T01:30:00.000000Z"
    assert parse_audit_time("2026-07-24 09:30:00") == datetime(2026, 7, 24, 1, 30, tzinfo=UTC)


def test_audit_text_uses_fixed_width_and_configurable_legacy_timezone() -> None:
    assert audit_datetime_to_text(datetime(2026, 7, 24, 1, 30, tzinfo=UTC)) == "2026-07-24T01:30:00.000000Z"
    assert audit_datetime_to_text(datetime(2026, 7, 24, 1, 30, 0, 1, tzinfo=UTC)) == "2026-07-24T01:30:00.000001Z"
    assert parse_audit_time(
        "2026-07-24 09:30:00",
        legacy_timezone="America/Los_Angeles",
    ) == datetime(2026, 7, 24, 16, 30, tzinfo=UTC)


def test_default_market_text_is_independent_of_host_timezone() -> None:
    script = "from app.utils.time import now_text; print(now_text()[:10])"
    outputs = []
    for timezone_name in ("UTC", "Asia/Shanghai"):
        env = os.environ.copy()
        env["TZ"] = timezone_name
        result = subprocess.run(
            [sys.executable, "-c", script],
            cwd=ROOT,
            env=env,
            check=True,
            capture_output=True,
            text=True,
        )
        outputs.append(result.stdout.strip())

    assert outputs[0] == outputs[1]
