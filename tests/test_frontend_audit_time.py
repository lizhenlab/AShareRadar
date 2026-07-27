from __future__ import annotations

import os
from pathlib import Path
import subprocess

import pytest


ROOT = Path(__file__).resolve().parents[1]


@pytest.mark.parametrize("host_timezone", ["UTC", "Asia/Shanghai"])
def test_frontend_audit_time_is_host_independent_and_used_by_time_views(
    host_timezone: str,
) -> None:
    script = r'''
      import assert from "node:assert/strict";
      import { auditTimestampEpoch, formatAuditTimestamp } from "./static/js/audit-time.js";
      import { renderAlertEvents } from "./static/js/alerts.js";
      import { diagnosticTimestamp } from "./static/js/diagnostics.js";
      import { displayTimestamp } from "./static/js/market-scan-view.js";

      const elements = { alertEvents: { innerHTML: "" } };
      globalThis.document = { getElementById: (id) => elements[id] || null };

      assert.equal(formatAuditTimestamp("2026-07-24T01:30:00.123456Z"), "2026-07-24 09:30:00");
      assert.equal(formatAuditTimestamp("2026-07-24 09:30:00"), "2026-07-24 09:30:00");
      assert.equal(formatAuditTimestamp("2026-07-24T09:30:00+09:00"), "2026-07-24 08:30:00");
      assert.equal(formatAuditTimestamp("2026-02-30 09:30:00"), "--");
      assert.equal(
        auditTimestampEpoch("2026-07-24 09:30:00"),
        auditTimestampEpoch("2026-07-24T01:30:00Z"),
      );
      assert.equal(displayTimestamp("2026-07-24T01:30:00Z"), "2026-07-24 09:30");
      assert.equal(diagnosticTimestamp("2026-07-24T01:30:00Z"), "2026-07-24 09:30:00");

      renderAlertEvents([{
        id: 1,
        name: "测试",
        event_type: "触发",
        created_at: "2026-07-24T01:30:00Z",
        price: 10,
        change_pct: 1,
        message: "ok",
      }]);
      assert.match(elements.alertEvents.innerHTML, /2026-07-24 09:30:00/);
      assert.doesNotMatch(elements.alertEvents.innerHTML, /01:30:00Z/);
    '''
    env = os.environ.copy()
    env["TZ"] = host_timezone

    subprocess.run(
        ["node", "--input-type=module", "-e", script],
        cwd=ROOT,
        env=env,
        check=True,
    )
