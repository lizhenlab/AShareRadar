"""Chart labels use market time while inspection snapshots retain source evidence."""
from __future__ import annotations

import json
import os
from pathlib import Path
import subprocess

import pytest

from tests.test_frontend_chart_context import CHART_HARNESS


ROOT = Path(__file__).resolve().parents[1]


@pytest.mark.parametrize("host_timezone", ["UTC", "Asia/Shanghai", "America/Los_Angeles"])
@pytest.mark.parametrize("timestamp,label", [
    ("2026-07-15T02:15:00Z", "07-15 10:15"),
    ("2026-07-15T11:15:00+09:00", "07-15 10:15"),
    ("2026-07-14T19:15:00-0700", "07-15 10:15"),
    ("2026-07-15T10:15:00+08:00", "07-15 10:15"),
    ("2026-07-15 10:15:00", "07-15 10:15"),
    ("2026-07-14T16:05:00.123456Z", "07-15 00:05"),
    ("2026-07-15T00:05:00+14:00", "07-14 18:05"),
])
def test_chart_timestamps_display_shanghai_time_and_preserve_source(
    host_timezone: str, timestamp: str, label: str,
) -> None:
    _run_script(host_timezone, f"const timestamp = {json.dumps(timestamp)}, label = {json.dumps(label)};\n" + r'''
      const harness = makeCanvas();
      const row = { ...makeMinuteRows(1)[0], timestamp, fetched_at: "2026-07-15T02:20:00Z", source: "原始证据" };
      const before = JSON.stringify(row);
      const result = drawKlineChart({ canvas: harness.canvas, rows: [row], showMarks: false });
      assert(result.drawn && result.startLabel === label && result.endLabel === label,
        `expected market label ${label}, received ${result.startLabel}`);
      assert(harness.calls.some(call => call[0] === "fillText" && call[1] === label), "metadata was changed without changing the actual canvas label");
      const inspection = chartInspectionAt(result.inspection, result.inspection.bounds.left + result.xStep / 2);
      assert(inspection.item.eventTime === timestamp && result.inspection.rows[0].timestamp === timestamp,
        "market display replaced the raw event timestamp evidence");
      assert(inspection.item.fetchedAt === row.fetched_at && JSON.stringify(row) === before,
        "formatting mutated input rows or their acquisition evidence");
    ''')


@pytest.mark.parametrize("host_timezone", ["UTC", "Asia/Shanghai", "America/Los_Angeles"])
def test_daily_dates_are_not_converted_to_a_different_market_day(host_timezone: str) -> None:
    _run_script(host_timezone, r'''
      for (const date of ["2026-07-15", "2026/7/15"]) {
        const harness = makeCanvas();
        const row = { ...makeDailyRows(1)[0], date, timestamp: "2026-07-15T23:55:00-07:00" };
        const result = drawKlineChart({ canvas: harness.canvas, rows: [row], showMarks: false });
        assert(result.startLabel === "07-15" && result.endLabel === "07-15", "a daily trading date was treated as a timezone instant");
        assert(result.inspection.rows[0].date === date && result.inspection.rows[0].eventTime === date,
          "daily date precedence or source evidence changed");
      }
    ''')


def _run_script(host_timezone: str, assertions: str) -> None:
    source = ('import { drawKlineChart } from "./static/js/chart.js";\n'
              'import { chartInspectionAt } from "./static/js/chart-inspector.js";\n'
              'function assert(value, message) { if (!value) throw new Error(message); }\n'
              + CHART_HARNESS + assertions)
    env = os.environ.copy()
    env["TZ"] = host_timezone
    subprocess.run(["node", "--input-type=module", "-e", source], cwd=ROOT, env=env, check=True, capture_output=True, text=True)
