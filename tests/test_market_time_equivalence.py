"""Preserve the accepted timestamp language and legacy second-level semantics."""

from datetime import UTC, date, datetime, timedelta, timezone, tzinfo
from zoneinfo import ZoneInfo

import pytest

from app.utils.market_time import (
    ASHARE_TIMEZONE,
    MARKET_DATETIME_FORMAT,
    market_datetime_epoch,
    market_local_naive,
    normalize_market_datetime,
)


@pytest.mark.parametrize(("value", "expected"), [
    ("2026/09/08 09:30:01", "2026-09-08 09:30:01"),
    ("2026-09-08T09:30:00+0800", "2026-09-08 09:30:00"),
    ("2026-09-08 09:30:00.999999+08:00", "2026-09-08 09:30:00"),
    ("2026-09-08 01:30:00Z", "2026-09-08 09:30:00"),
    ("2026-09-08 09:30:00+05:30", "2026-09-08 12:00:00"),
    ("2026-W37-2T09:30:00", "2026-09-08 09:30:00"),
    ("202609081015.99", "2026-09-08 10:15:00"),
    ("20260908101530.99", "2026-09-08 10:15:30"),
    ("20260908", "1970-08-23 20:01:48"),
    (1000000000001, "2001-09-09 09:46:40"),
    ("0.001", "1970-01-01 08:00:00"),
    ("1e4", "1970-01-01 10:46:40"),
    ("  1750000000.999  ", "2025-06-15 23:06:40"),
])
def test_normalized_text_and_epoch_preserve_existing_formats(value, expected):
    assert normalize_market_datetime(value) == expected
    expected_epoch = datetime.strptime(expected, MARKET_DATETIME_FORMAT).replace(tzinfo=ASHARE_TIMEZONE).timestamp()
    assert market_datetime_epoch(value) == expected_epoch


@pytest.mark.parametrize("value", [
    None, "", False, True, 0, -1, float("nan"), float("inf"), -float("inf"),
    "NaT", "null", "n/a", "--", "2026-09-08", "20260230123045", "202601019999",
    "20260908101530bad", "09:30", "24:00:00", "12:34:60", "1000000000000",
    "９９９９１２３１２３５９５９", "20260908T093000", "1e309",
])
def test_optimization_does_not_expand_timestamp_admission(value):
    assert normalize_market_datetime(value) is None
    assert market_datetime_epoch(value) is None


@pytest.mark.parametrize(("value", "event_date", "expected"), [
    ("09:30:00", date(2026, 9, 8), "2026-09-08 09:30:00"),
    ("09:30", "20260908", "2026-09-08 09:30:00"),
    ("09:30:00", "2026/09/08", "2026-09-08 09:30:00"),
    ("09:30:00.999999", date(2026, 9, 8), None),
    ("09:30:00Z", "2026/09/08", None),
    ("09:30:00+00:00", "2026-09-08", None),
    ("09:30", datetime(2026, 9, 8, 22, tzinfo=UTC), "2026-09-09 09:30:00"),
    ("09:30", None, None),
    ("09:30", "bad", None),
    ("24:00:00", "2026-09-08", None),
])
def test_time_only_keeps_explicit_event_date_binding(value, event_date, expected):
    assert normalize_market_datetime(value, event_date=event_date) == expected


@pytest.mark.parametrize("fold", (0, 1))
@pytest.mark.parametrize("aware", (False, True))
def test_epoch_preserves_second_truncation_and_legacy_fold_reset(fold, aware):
    value = datetime(1991, 9, 15, 1, 30, 0, 999999, fold=fold, tzinfo=ASHARE_TIMEZONE if aware else None)
    assert normalize_market_datetime(value) == "1991-09-15 01:30:00"
    expected = datetime(1991, 9, 15, 1, 30, tzinfo=ASHARE_TIMEZONE, fold=0).timestamp()
    assert market_datetime_epoch(value) == expected
    assert expected != value.replace(microsecond=0, tzinfo=ASHARE_TIMEZONE, fold=1).timestamp()


def test_datetime_range_and_offsets_match_the_original_string_round_trip():
    zones = (None, UTC, ASHARE_TIMEZONE, ZoneInfo("America/New_York"), timezone(timedelta(hours=14)))
    for year in (1, 9, 99, 999, 1000, 1900, 1988, 2026, 9999):
        for zone in zones:
            for fold in (0, 1):
                value = datetime(year, 6, 15, 1, 30, 59, 999999, tzinfo=zone, fold=fold)
                # strftime padding below year 1000 differs across supported platforms.
                normalized = market_local_naive(value).strftime(MARKET_DATETIME_FORMAT)
                assert normalize_market_datetime(value) == normalized
                try:
                    expected = datetime.strptime(normalized, MARKET_DATETIME_FORMAT).replace(tzinfo=ASHARE_TIMEZONE).timestamp()
                except (ValueError, OverflowError, OSError) as error:
                    with pytest.raises(type(error)):
                        market_datetime_epoch(value)
                else:
                    assert market_datetime_epoch(value) == expected


class _FormattedDateTime(datetime):
    def strftime(self, format):
        return "2001-02-03 04:05:06"

    def isoformat(self, *args, **kwargs):
        raise AssertionError("a subclass must retain its strftime override")

    def timestamp(self):
        raise AssertionError("the legacy epoch parser creates a native datetime")


class _InvalidFormattedDateTime(_FormattedDateTime):
    def strftime(self, format):
        return "not a timestamp"


class _NoOffset(tzinfo):
    def utcoffset(self, value):
        return None


def test_datetime_subclass_formatting_and_invalid_output_remain_observable():
    value = _FormattedDateTime(2026, 9, 8, 9, 30)
    assert normalize_market_datetime(value) == "2001-02-03 04:05:06"
    assert market_datetime_epoch(value) == datetime(2001, 2, 3, 4, 5, 6, tzinfo=ASHARE_TIMEZONE).timestamp()
    invalid = _InvalidFormattedDateTime(2026, 9, 8)
    assert normalize_market_datetime(invalid) == "not a timestamp"
    with pytest.raises(ValueError):
        market_datetime_epoch(invalid)


def test_tzinfo_without_an_offset_retains_naive_market_interpretation():
    value = datetime(2026, 9, 8, 9, 30, 0, 999999, tzinfo=_NoOffset())
    assert normalize_market_datetime(value) == "2026-09-08 09:30:00"
    assert market_datetime_epoch(value) == datetime(2026, 9, 8, 9, 30, tzinfo=ASHARE_TIMEZONE).timestamp()
