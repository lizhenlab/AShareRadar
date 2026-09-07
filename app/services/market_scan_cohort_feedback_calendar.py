"""Capture calendar bytes once; all later replay uses the frozen evidence only."""

from __future__ import annotations

from datetime import date, datetime
from typing import Literal, Self

from pydantic import Field, model_validator

from app.artifacts.io import decode_json_bytes, read_regular_file, sha256_hex
from app.services.market_scan_delayed_feedback_contracts import (
    Digest, FeedbackModel, LabelText, SHANGHAI, feedback_digest, feedback_json_bytes, feedback_time,
)
from app.services.trading_calendar import BUNDLED_CALENDAR_PATH, CALENDAR_PATH, trading_date_range


def cohort_date(value: str) -> date:
    parsed = date.fromisoformat(value)
    if parsed.isoformat() != value:
        raise ValueError("cohort dates must be canonical ISO")
    return parsed


class CalendarRaw(FeedbackModel):
    schema_version: int
    source: LabelText
    updated_at: str
    min_date: str
    max_date: str
    trade_date_count: int
    trade_dates: list[str] = Field(min_length=1, max_length=100_000)

    @model_validator(mode="after")
    def valid_calendar(self) -> Self:
        parsed = [cohort_date(day) for day in self.trade_dates]
        if parsed != sorted(set(parsed)) or self.schema_version != 1:
            raise ValueError("calendar must have ordered unique sessions and supported schema")
        if (self.min_date, self.max_date, self.trade_date_count) != (self.trade_dates[0], self.trade_dates[-1], len(parsed)):
            raise ValueError("calendar raw metadata does not match sessions")
        datetime.fromisoformat(self.updated_at)
        return self


class FrozenCohortCalendar(FeedbackModel):
    source_kind: Literal["runtime_cache", "bundled_baseline"]
    captured_at: str
    raw_file_utf8: str
    raw_file_digest: Digest
    coverage_start: str
    coverage_end: str
    sessions: list[str] = Field(min_length=1, max_length=5000)
    calendar_digest: Digest

    @model_validator(mode="after")
    def valid_frozen_calendar(self) -> Self:
        raw = self.raw_file_utf8.encode("utf-8")
        if sha256_hex(raw) != self.raw_file_digest:
            raise ValueError("calendar raw file digest mismatch")
        parsed = CalendarRaw.model_validate_json(feedback_json_bytes(decode_json_bytes(raw)), strict=True)
        start, end = cohort_date(self.coverage_start), cohort_date(self.coverage_end)
        if not cohort_date(parsed.min_date) <= start <= end <= cohort_date(parsed.max_date):
            raise ValueError("frozen calendar lacks requested coverage")
        if self.sessions != [day for day in parsed.trade_dates if self.coverage_start <= day <= self.coverage_end]:
            raise ValueError("frozen sessions do not match original calendar bytes")
        updated = datetime.fromisoformat(parsed.updated_at)
        if updated.tzinfo is None:
            updated = updated.replace(tzinfo=SHANGHAI)
        if updated > feedback_time(self.captured_at):
            raise ValueError("calendar raw metadata postdates capture")
        if feedback_digest(self.model_dump(mode="json", exclude={"calendar_digest"})) != self.calendar_digest:
            raise ValueError("frozen calendar digest mismatch")
        return self


def capture_cohort_calendar(start: str, end: str, *, captured_at: str) -> dict[str, object]:
    """Read the selected trusted local file and bind its original UTF-8 bytes."""
    sessions, status = trading_date_range(cohort_date(start), cohort_date(end))
    paths = {"runtime_cache": CALENDAR_PATH, "bundled_baseline": BUNDLED_CALENDAR_PATH}
    kind = str(status.source)
    if kind not in paths:
        raise ValueError("no covered trusted local calendar to freeze")
    raw = read_regular_file(paths[kind], max_bytes=4 * 1024 * 1024)
    payload = {"source_kind": kind, "captured_at": feedback_time(captured_at).isoformat(),
               "raw_file_utf8": raw.decode("utf-8"), "raw_file_digest": sha256_hex(raw),
               "coverage_start": start, "coverage_end": end, "sessions": [day.isoformat() for day in sessions]}
    return FrozenCohortCalendar.model_validate({**payload, "calendar_digest": feedback_digest(payload)}).model_dump(mode="json")
