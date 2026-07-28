from __future__ import annotations

import re
import subprocess
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]


def test_global_market_scan_progress_is_wired_into_the_workspace() -> None:
    html = (ROOT / "static/index.html").read_text(encoding="utf-8")

    assert 'id="marketScanGlobalProgress"' in html
    assert 'id="marketScanGlobalText"' in html
    assert 'id="marketScanGlobalOpen"' in html
    assert 'id="marketScanGlobalCancel"' in html


def test_market_scan_rows_expose_complete_mobile_labels() -> None:
    _run_node_script(
        r'''
import assert from "node:assert/strict";
import { marketScanResultRow } from "./static/js/market-scan.js";

const row = marketScanResultRow({
  rank: 1,
  symbol: "920066.BJ",
  code: "920066",
  market: "BJ",
  name: "科拜尔",
  industry: "专用设备",
  status: "success",
  score: 88,
  trend_score: 72,
  change_pct: 1.25,
  turnover_rate: 2.5,
  amount: 125000000,
  data_quality_score: 91,
  tags: ["趋势向上", "量价配合"],
});
const labels = ["排名", "股票", "市场 / 行业", "短线强势", "趋势", "涨跌幅", "换手率", "成交额", "质量", "状态 / 标签"];
for (const label of labels) assert.equal(row.includes(`data-label="${label}"`), true, label);
'''
    )


def test_market_scan_layout_freezes_desktop_headers_and_exposes_mobile_equivalent_details() -> None:
    styles = (ROOT / "static/css/market-scan.css").read_text(encoding="utf-8")

    assert re.search(r"\.market-scan-table\s+th\s*\{[^}]*position:\s*sticky", styles, re.DOTALL)
    assert re.search(
        r"\.market-scan-table\s+(?:th|td):nth-child\(2\)[^{]*\{[^}]*position:\s*sticky",
        styles,
        re.DOTALL,
    )
    mobile = styles.split("@media (max-width: 820px)", 1)[1]
    assert "min-width: 0" in mobile
    assert ".market-scan-table thead" in mobile
    assert "content: attr(data-label)" in mobile


def _run_node_script(script: str) -> None:
    subprocess.run(
        ["node", "--input-type=module", "-e", script],
        cwd=ROOT,
        check=True,
        text=True,
        capture_output=True,
    )
