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
    assert '<fieldset class="market-scan-mode-control" id="marketScanModeControl">' in html
    assert '<legend>浏览和新建榜单模式</legend>' in html
    assert html.count('name="marketScanMode"') == 3
    assert 'id="marketScanModePreopen"' in html
    assert 'class="market-scan-kicker"' in html
    for board in ("上海A股", "科创板", "深圳A股", "创业板", "北交所"):
        assert f"<span>{board}</span>" in html
    for element_id in (
        "marketScanGateSummary",
        "marketScanPublicationBlockers",
        "marketScanPassedGates",
        "marketScanSourceWarnings",
    ):
        assert f'id="{element_id}"' in html


def test_market_scan_rows_expose_complete_mobile_labels() -> None:
    _run_node_script(
        r'''
import assert from "node:assert/strict";
import { marketScanBoardLabel, marketScanResultRow } from "./static/js/market-scan.js";

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
  reason: "短线强势分 88（历史冻结说明）",
  score_details: { components: { score_dimensions: { scores: {
    confidence: 91, risk: 28, tradability: 84,
  } } } },
  tags: ["趋势向上", "量价配合"],
});
const labels = ["排名", "股票", "上市板块 / 行业", "趋势强度", "研究信号", "涨跌幅", "换手率", "成交额", "质量", "状态 / 标签"];
for (const label of labels) assert.equal(row.includes(`data-label="${label}"`), true, label);
assert.equal(row.includes("北交所"), true);
assert.equal(row.includes("信 91"), true);
assert.equal(row.includes("险 28"), true);
assert.equal(row.includes("易 84"), true);
assert.equal(row.includes("趋势强度 88（历史冻结说明）"), true);
assert.equal(row.includes("短线强势分"), false);
assert.equal(row.includes('aria-label="查看扫描快照" title="查看该次扫描保存的证据快照">快照'), true);
assert.equal(row.includes('aria-label="打开当前个股分析"'), true);
assert.equal(row.includes('>分析</button>'), true);
assert.equal(marketScanBoardLabel({ code: "600519", market: "SH" }), "上海A股（主板）");
assert.equal(marketScanBoardLabel({ code: "688981", market: "SH" }), "科创板");
assert.equal(marketScanBoardLabel({ code: "000001", market: "SZ" }), "深圳A股（主板）");
assert.equal(marketScanBoardLabel({ code: "300750", market: "SZ" }), "创业板");
assert.equal(marketScanBoardLabel({ code: "920066", market: "BJ" }), "北交所");
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
    assert "max-height: min(72vh, 680px)" in mobile
    assert "overscroll-behavior: contain" in mobile
    assert "scrollbar-gutter: stable" in mobile
    assert ".market-scan-mode-options span" in mobile
    assert re.search(
        r"\.market-scan-mode-options\s+span\s*\{[^}]*min-height:\s*44px",
        styles,
        re.DOTALL,
    )
    assert re.search(
        r"\.market-scan-mode-options\s+input:focus-visible\s*\+\s*span\s*\{[^}]*outline:",
        styles,
        re.DOTALL,
    )
    compact = styles.split("@media (max-width: 480px)", 1)[1]
    assert ".market-scan-mode-control" in compact
    assert "grid-column: 1 / -1" in compact


def test_future_range_research_styles_have_one_explicit_owner() -> None:
    static_dir = ROOT / "static"
    css_dir = static_dir / "css"
    research_path = css_dir / "market-scan-research.css"
    research_styles = research_path.read_text(encoding="utf-8")
    owners = [
        path.relative_to(static_dir).as_posix()
        for path in sorted(static_dir.rglob("*.css"))
        if ".market-scan-future-range" in path.read_text(encoding="utf-8")
    ]

    assert owners == ["css/market-scan-research.css"]
    assert len(research_styles.splitlines()) >= 300
    assert research_styles.startswith(
        "/* Run-bound future-range research stays independent from the production leaderboard. */"
    )
    for breakpoint in ("960px", "820px", "480px"):
        assert f"@media (max-width: {breakpoint})" in research_styles


def test_layout_optimization_keeps_dense_scan_rows_readable_across_breakpoints() -> None:
    styles = (ROOT / "static/css/layout-optimizations.css").read_text(encoding="utf-8")
    scan_styles = (ROOT / "static/css/market-scan.css").read_text(encoding="utf-8")

    desktop = styles.split("@media (min-width: 821px)", 1)[1].split(
        "@media (max-width: 1180px)",
        1,
    )[0]
    assert ".market-scan-stock-actions" in desktop
    assert "display: flex" in desktop
    assert ".market-scan-result-row:hover td" in desktop
    assert "-webkit-line-clamp: 3" in desktop

    mobile = styles.split("@media (max-width: 820px)", 1)[1]
    assert ".market-scan-result-row > td:first-child" in mobile
    assert "position: absolute" in mobile
    assert 'content: "#"' in mobile
    assert "grid-template-columns: repeat(2, minmax(0, 1fr))" in mobile
    assert "#marketScanRetry:not([hidden])" in mobile
    assert "scroll-margin-top: calc(var(--primary-navigation-height) + 12px)" in mobile

    tablet = styles.split("@media (min-width: 600px) and (max-width: 820px)", 1)[1]
    assert "grid-template-columns: repeat(4, minmax(0, 1fr))" in tablet
    assert re.search(
        r"td:nth-child\(5\)\s*\{[^}]*grid-column:\s*2\s*/\s*-1",
        tablet,
        re.DOTALL,
    )
    assert re.search(
        r"td:nth-child\(8\),\s*\.market-scan-table[^{}]*td:nth-child\(9\)\s*\{[^}]*grid-column:\s*auto",
        tablet,
        re.DOTALL,
    )

    assert ".query-panel .panel-title > span" in styles
    assert "white-space: nowrap" in styles
    assert ".market-scan-table th:nth-child(2) { width: 15%; }" in scan_styles
    assert ".market-scan-table th:nth-child(5) { width: 13%; }" in scan_styles
    assert ".market-scan-table th:nth-child(10) { width: 22%; }" in scan_styles
    assert re.search(r"\.market-scan-stock-meta-row\s*\{[^}]*justify-content: flex-start", scan_styles, re.DOTALL)
    assert ".market-scan-task-line:has(#marketScanHeadline.error)" in styles
    assert ".market-scan-gate-row.blocker" in scan_styles
    assert ".market-scan-gate-row.passed" in scan_styles
    assert ".market-scan-gate-row.warning" in scan_styles
    scan_mobile = scan_styles.split("@media (max-width: 820px)", 1)[1]
    assert re.search(
        r"\.market-scan-gate-row\s*\{[^}]*grid-template-columns:\s*minmax\(0,\s*1fr\)",
        scan_mobile,
        re.DOTALL,
    )
    assert 'body[data-primary-view="market"] .topbar-status' in styles
    assert 'body[data-primary-view="monitor"] .footer #sourceLine' in styles
    assert '"initial default-cost"' in styles
    assert '"benchmark run"' in styles


def _run_node_script(script: str) -> None:
    subprocess.run(
        ["node", "--input-type=module", "-e", script],
        cwd=ROOT,
        check=True,
        text=True,
        capture_output=True,
    )
