from __future__ import annotations

import json
import re
import subprocess
from pathlib import Path
from typing import get_args

from app.models.market_scan import (
    MARKET_SCAN_PUBLICATION_DIAGNOSTIC_SEVERITIES,
    MARKET_SCAN_PUBLICATION_DIAGNOSTICS_SCHEMA_VERSION,
    MarketScanDiagnosticSeverity,
    MarketScanMode,
    MarketScanPublicationDiagnostic,
    MarketScanPublicationDiagnostics,
    MarketScanRunStatus,
)


ROOT = Path(__file__).resolve().parents[1]


def test_python_and_javascript_market_scan_publication_contracts_stay_in_parity() -> None:
    script = r'''
import {
  MARKET_SCAN_PUBLICATION_DIAGNOSTIC_FIELDS,
  MARKET_SCAN_PUBLICATION_DIAGNOSTIC_SEVERITIES,
  MARKET_SCAN_PUBLICATION_DIAGNOSTICS_FIELDS,
  MARKET_SCAN_PUBLICATION_DIAGNOSTICS_SCHEMA_VERSION,
  MARKET_SCAN_MODES,
  MARKET_SCAN_RUN_STATUSES,
} from "./static/js/market-scan-contracts.js";
console.log(JSON.stringify({
  diagnosticFields: MARKET_SCAN_PUBLICATION_DIAGNOSTIC_FIELDS,
  diagnosticSeverities: MARKET_SCAN_PUBLICATION_DIAGNOSTIC_SEVERITIES,
  diagnosticsFields: MARKET_SCAN_PUBLICATION_DIAGNOSTICS_FIELDS,
  diagnosticsSchemaVersion: MARKET_SCAN_PUBLICATION_DIAGNOSTICS_SCHEMA_VERSION,
  modes: MARKET_SCAN_MODES,
  runStatuses: MARKET_SCAN_RUN_STATUSES,
}));
'''
    completed = subprocess.run(
        ["node", "--input-type=module", "-e", script],
        cwd=ROOT,
        check=True,
        text=True,
        capture_output=True,
    )
    contract = json.loads(completed.stdout)

    assert contract["runStatuses"] == list(get_args(MarketScanRunStatus))
    assert contract["modes"] == list(get_args(MarketScanMode))
    assert contract["diagnosticSeverities"] == list(
        MARKET_SCAN_PUBLICATION_DIAGNOSTIC_SEVERITIES
    )
    assert contract["diagnosticSeverities"] == list(get_args(MarketScanDiagnosticSeverity))
    assert contract["diagnosticsSchemaVersion"] == MARKET_SCAN_PUBLICATION_DIAGNOSTICS_SCHEMA_VERSION
    assert contract["diagnosticFields"] == list(MarketScanPublicationDiagnostic.model_fields)
    assert contract["diagnosticsFields"] == list(MarketScanPublicationDiagnostics.model_fields)


def test_market_scan_frontend_contract_is_wired_into_workspace() -> None:
    html = (ROOT / "static/index.html").read_text(encoding="utf-8")
    app = (ROOT / "static/app.js").read_text(encoding="utf-8")
    preferences = (ROOT / "static/js/workspace-preferences.js").read_text(encoding="utf-8")
    styles = (ROOT / "static/styles.css").read_text(encoding="utf-8")

    assert 'data-view="market-scan"' in html
    assert 'id="marketScanStart"' in html
    assert 'id="marketScanRefreshTop100" aria-busy="false" disabled>更新 TOP100 评分</button>' in html
    assert 'id="marketScanExport" aria-busy="false" disabled>导出 Excel</button>' in html
    assert 'id="marketScanModeControl"' in html
    assert 'id="marketScanModePreopen" name="marketScanMode" value="preopen"' in html
    assert 'id="marketScanModeIntraday" name="marketScanMode" value="intraday"' in html
    assert 'id="marketScanModeOfficial" name="marketScanMode" value="official" checked' in html
    assert 'id="marketScanProgressBar"' in html
    assert 'id="marketScanHistoryToggle" aria-controls="marketScanHistory" aria-expanded="false"' in html
    assert 'id="marketScanHistory" aria-label="历史扫描批次" aria-busy="false" hidden' in html
    assert 'id="marketScanProbabilityResearch"' in html
    assert 'id="marketScanProbabilityEffectiveness"' in html
    assert 'id="marketScanProbabilityHorizon5d" name="marketScanProbabilityHorizon" value="5" checked' in html
    assert 'id="marketScanProbabilityMin"' in html and 'aria-describedby="marketScanProbabilityFilterHelp" disabled' in html
    assert 'id="marketScanFutureRangeResearch" data-generation-status="not_generated" aria-busy="false"' in html
    assert html.count('name="marketScanFutureRangeOffset"') == 3
    assert html.count('name="marketScanFutureRangePath"') == 2
    assert 'HLC3 典型价代理 · 非 VWAP' in html
    assert 'id="marketScanFutureRangePagination" hidden' in html
    assert '>研究信号</th>' in html
    assert 'id="marketScanFinishedAt"' in html
    assert 'id="marketScanExecutedAt">评分执行时间：--</time>' in html
    assert 'id="marketScanRows"' in html
    assert 'id="marketScanFilterToggle" aria-controls="marketScanFilterPanel"' in html
    assert 'id="marketScanDetailsToggle" aria-controls="marketScanDetails"' in html
    assert 'id="marketScanStrategyToggle" aria-controls="strategyLab"' in html
    assert '<h3 id="marketScanTitle">全市场选股</h3>' in html
    assert 'class="market-scan-mode-block"' in html
    assert 'class="market-scan-action-buttons" aria-label="扫描操作"' in html
    assert '<details class="strategy-lab-shell" id="strategyLab" hidden>' in html
    assert '<details class="discovery-preset-more" id="discoveryPresetMore">' in html
    assert html.count('class="market-scan-filter-section-heading"') == 1
    assert '<details class="market-scan-advanced-filters" id="marketScanAdvancedFilters">' in html
    assert 'class="market-scan-advanced-filter-grid"' in html
    filters_markup = html.split('<form class="market-scan-filters" id="marketScanFilters">', 1)[1].split("</form>", 1)[0]
    basic_markup, advanced_markup = filters_markup.split(
        '<details class="market-scan-advanced-filters" id="marketScanAdvancedFilters">', 1
    )
    for element_id in ("marketScanKeyword", "marketScanMarket", "marketScanScoreMin", "marketScanQuality", "marketScanSort"):
        assert f'id="{element_id}"' in basic_markup
    for element_id in ("marketScanScoreMax", "marketScanTrendMin", "marketScanAmountMin", "marketScanSort2", "marketScanSort3"):
        assert f'id="{element_id}"' in advanced_markup
    assert '<span>有效覆盖率</span><strong id="marketScanCoverage">--</strong>' in html
    assert '<span>榜单类型</span><strong id="marketScanModeSummary">--</strong>' in html
    assert '<span>行情日期</span><strong id="marketScanQuoteDate">--</strong>' in html
    assert '<span>日K截止日</span><strong id="marketScanDataDate">--</strong>' in html
    assert 'id="marketScanAnnouncement" role="status" aria-live="polite" aria-atomic="true" aria-relevant="text"' in html
    assert 'id="marketScanProgressBar" max="100" value="0" aria-label="全市场扫描进度"' in html
    assert 'aria-valuetext="尚无扫描进度" aria-busy="false"' in html
    scan_panel = html.split('id="workspace-panel-market-scan"', 1)[1].split('</section>\n\n        <section class="workspace-view"', 1)[0]
    assert scan_panel.count('aria-live="polite"') == 2
    assert 'id="strategyLabAnnouncement" role="status" aria-live="polite"' in scan_panel
    assert 'id="marketScanHeadline" role="status"' not in scan_panel
    assert 'id="marketScanResultState" role="status"' not in scan_panel
    assert 'id="marketScanTableWrap" role="region"' in html
    assert 'aria-label="全市场扫描榜单" aria-busy="false" tabindex="0"' in html
    assert 'id="marketScanPagination" aria-busy="false"' in html
    assert 'id="stockWorkbench" tabindex="-1" aria-labelledby="stockName"' in html
    assert 'id="currentAnalysisContext" role="status" hidden' in html
    assert 'id="marketScanStatus"' in html and 'value="all"' in html
    ordered_ids = [
        'id="marketScanFilterToggle"', 'id="strategyLab"', 'id="marketScanDetails"', 'id="marketScanFilterPanel"',
        'id="marketScanAnnouncement"', 'id="marketScanTableWrap"', 'id="discoveryBulkControls"',
    ]
    assert [html.index(marker) for marker in ordered_ids] == sorted(html.index(marker) for marker in ordered_ids)
    top_ids = ['id="marketScanHistoryToggle"', 'id="marketScanContext"', 'id="marketScanProgress"', 'id="marketScanHistory"']
    assert [html.index(marker) for marker in top_ids] == sorted(html.index(marker) for marker in top_ids)
    assert html.index('id="discoveryPresetControls"') < html.index('id="marketScanTableWrap"')
    assert html.index('id="marketScanFutureRangeResearch"') < html.index('id="marketScanTableWrap"')
    assert 'data-closed-label="筛选条件">筛选条件</button>' in html
    assert 'createMarketScanController' in app
    assert 'target === "market-scan"' in app
    assert '"market-scan"' in preferences
    assert "@import" not in styles
    css_modules = re.findall(r'<link rel="stylesheet" href="/static/css/([^"]+)\?v=([^"]+)" />', html)
    assert [name for name, _ in css_modules] == [
        "base.css", "sidebar.css", "workspace-core.css", "research-panels.css", "individual-probability.css",
        "market-scan.css",
        "market-scan-research.css",
        "interactions.css", "side-footer.css", "responsive.css", "primary-navigation.css", "layout-optimizations.css",
    ]
    versions = [version for _, version in css_modules]
    versions.extend(re.findall(r'(?:href|src)="/static/(?:styles\.css|app\.js)\?v=([^"]+)"', html))
    assert len(versions) == 14
    import_map_match = re.search(r'<script type="importmap">\s*(\{.*?\})\s*</script>', html, re.DOTALL)
    assert import_map_match is not None
    imports = json.loads(import_map_match.group(1))["imports"]
    module_paths = {
        "/static/js/api.js",
        "/static/js/market-scan.js",
        "/static/js/market-scan-controller.js",
        "/static/js/market-scan-controller-inert.js",
        "/static/js/market-scan-contracts.js",
        "/static/js/market-scan-export-client.js",
        "/static/js/market-scan-export-action.js",
        "/static/js/market-scan-executable-shadow-contracts.js",
        "/static/js/market-scan-executable-shadow-controller.js",
        "/static/js/market-scan-executable-shadow-view.js",
        "/static/js/market-scan-filters.js",
        "/static/js/market-scan-future-range-controller.js",
        "/static/js/market-scan-future-range-view.js",
        "/static/js/market-scan-history.js",
        "/static/js/market-scan-history-view.js",
        "/static/js/market-scan-latest-sync.js",
        "/static/js/layout-optimizations.js",
        "/static/js/market-scan-message-view.js",
        "/static/js/market-scan-polling.js",
        "/static/js/market-scan-probability-binding.js",
        "/static/js/market-scan-probability-contracts.js",
        "/static/js/market-scan-probability-copy.js",
        "/static/js/market-scan-probability-interval.js",
        "/static/js/market-scan-probability-horizon-controller.js",
        "/static/js/market-scan-probability-polling.js",
        "/static/js/market-scan-probability-view.js",
        "/static/js/market-scan-read-transition.js",
        "/static/js/market-scan-progress-view.js",
        "/static/js/market-scan-row-actions.js",
        "/static/js/market-scan-run-context-view.js",
        "/static/js/market-scan-snapshot-view.js",
        "/static/js/market-scan-surface.js",
        "/static/js/market-scan-top100-refresh.js",
        "/static/js/market-scan-view.js",
        "/static/js/market-scan-view-export.js",
        "/static/js/individual-probability-contracts.js",
        "/static/js/individual-probability-controller.js",
        "/static/js/individual-probability-view.js",
        "/static/js/discovery.js",
        "/static/js/strategy-lab.js",
        "/static/js/strategy-lab-contracts.js",
        "/static/js/strategy-lab-controller.js",
        "/static/js/strategy-lab-view.js",
        "/static/js/strategy-template-catalog.js",
        "/static/js/workbench-contracts.js",
    }
    assert set(imports) == module_paths
    module_versions = [imports[path].split("?v=", 1)[1] for path in module_paths]
    assert len(set([*versions, *module_versions])) == 1


def test_strategy_template_catalog_contract_and_ui_are_wired_fail_closed() -> None:
    html = (ROOT / "static/index.html").read_text(encoding="utf-8")
    controller = (ROOT / "static/js/strategy-lab-controller.js").read_text(encoding="utf-8")
    catalog = (ROOT / "static/js/strategy-template-catalog.js").read_text(encoding="utf-8")
    styles = (ROOT / "static/css/market-scan.css").read_text(encoding="utf-8")

    assert 'id="strategyTemplateCatalog"' in html
    assert 'id="strategyTemplatePicker"' in html
    assert 'id="strategyTemplateCatalogStatus" role="status"' in html
    assert '模板用于组织研究目标与约束，不是上涨概率、投资建议或自动交易信号' in html
    assert "模板身份固定绑定历史合同，来源批次生产评分与排名不会改变" in html
    assert 'createStrategyTemplateCatalog' in controller
    assert 'await Promise.all([loadStrategies(), templateCatalog.load()]);' in controller
    assert 'state.strategy = null;' in controller
    assert 'await compileEditor(true);' in controller
    assert 'templateCatalog.markCustom();' in controller
    assert '/api/strategy-lab/templates' in catalog
    assert 'full-market-strategy-template-catalog-v1' in catalog
    assert 'available_for_draft' in catalog and 'shadow_only' in catalog and 'unavailable' in catalog
    assert '收益有效性未生成' in catalog and '假设未匹配' in catalog
    assert '.strategy-template-choice' in styles and 'min-height: 44px;' in styles
    assert '@media (max-width: 480px)' in styles


def test_market_scan_modules_have_explicit_reviewable_boundaries() -> None:
    module_dir = ROOT / "static/js"
    facade = (module_dir / "market-scan.js").read_text(encoding="utf-8")
    controller = (module_dir / "market-scan-controller.js").read_text(encoding="utf-8")
    contracts = (module_dir / "market-scan-contracts.js").read_text(encoding="utf-8")
    latest_loader = (module_dir / "market-scan-latest-loader.js").read_text(encoding="utf-8")
    latest_sync = (module_dir / "market-scan-latest-sync.js").read_text(encoding="utf-8")
    polling = (module_dir / "market-scan-polling.js").read_text(encoding="utf-8")
    polling_identity = (module_dir / "market-scan-polling-identity.js").read_text(encoding="utf-8")
    view = (module_dir / "market-scan-view.js").read_text(encoding="utf-8")
    history = (module_dir / "market-scan-history.js").read_text(encoding="utf-8")
    history_view = (module_dir / "market-scan-history-view.js").read_text(encoding="utf-8")
    message_view = (module_dir / "market-scan-message-view.js").read_text(encoding="utf-8")
    export_client = (module_dir / "market-scan-export-client.js").read_text(encoding="utf-8")
    export_action = (module_dir / "market-scan-export-action.js").read_text(encoding="utf-8")
    row_actions = (module_dir / "market-scan-row-actions.js").read_text(encoding="utf-8")
    snapshot_view = (module_dir / "market-scan-snapshot-view.js").read_text(encoding="utf-8")
    probability_view = (module_dir / "market-scan-probability-view.js").read_text(encoding="utf-8")
    probability_contracts = (module_dir / "market-scan-probability-contracts.js").read_text(encoding="utf-8")
    probability_horizon_controller = (module_dir / "market-scan-probability-horizon-controller.js").read_text(encoding="utf-8")
    probability_polling = (module_dir / "market-scan-probability-polling.js").read_text(encoding="utf-8")
    future_range_controller = (module_dir / "market-scan-future-range-controller.js").read_text(encoding="utf-8")
    future_range_view = (module_dir / "market-scan-future-range-view.js").read_text(encoding="utf-8")

    modules = {
        "market-scan.js": facade,
        "market-scan-controller.js": controller,
        "market-scan-contracts.js": contracts,
        "market-scan-latest-loader.js": latest_loader,
        "market-scan-latest-sync.js": latest_sync,
        "market-scan-polling.js": polling,
        "market-scan-polling-identity.js": polling_identity,
        "market-scan-probability-view.js": probability_view,
        "market-scan-probability-contracts.js": probability_contracts,
        "market-scan-probability-horizon-controller.js": probability_horizon_controller,
        "market-scan-probability-polling.js": probability_polling,
        "market-scan-read-transition.js": (module_dir / "market-scan-read-transition.js").read_text(encoding="utf-8"),
        "market-scan-future-range-controller.js": future_range_controller,
        "market-scan-future-range-view.js": future_range_view,
        "market-scan-view.js": view,
        "market-scan-history.js": history,
        "market-scan-history-view.js": history_view,
        "market-scan-message-view.js": message_view,
        "market-scan-export-client.js": export_client,
        "market-scan-export-action.js": export_action,
        "market-scan-row-actions.js": row_actions,
        "market-scan-run-context-view.js": (module_dir / "market-scan-run-context-view.js").read_text(encoding="utf-8"),
        "market-scan-snapshot-view.js": snapshot_view,
        "market-scan-surface.js": (module_dir / "market-scan-surface.js").read_text(encoding="utf-8"),
        "market-scan-top100-refresh.js": (module_dir / "market-scan-top100-refresh.js").read_text(encoding="utf-8"),
    }
    line_limits = {
        "market-scan-controller.js": 575,
        "market-scan-view.js": 675,
    }
    for filename, source in modules.items():
        limit = line_limits.get(filename, 600)
        assert len(source.splitlines()) < limit, f"{filename} should remain below {limit} lines"

    assert "createMarketScanController" in facade
    assert "buildMarketScanResultsUrl" in facade
    assert "fetchJson" in controller and "createMarketScanPolling" in controller
    assert "failureDelay" not in controller and "consecutiveFailures +=" not in controller
    assert "setTimeout" in polling and "createRequestScope" in polling and "fetchJson" not in polling
    assert "validateMarketScanRun" in contracts and "fetchJson" not in contracts and "setTimeout" not in contracts
    assert "marketScanResultRow" in view and "escapeHtml" in view
    assert "marketScanSnapshotContent" in snapshot_view and "不请求当前行情" in snapshot_view
    assert "renderMarketScanMessageSummary" in message_view and "发布阻断" not in view
    assert "calibrated_shadow" in probability_contracts and "probability_horizon" not in probability_view
    assert "buildMarketScanExportUrl" in view and "marketScanQueryParams" in view
    assert "exportBusy" in controller and "exportFetcher" in controller
    assert "const MARKET_SCAN_EXPORT_TIMEOUT_MS = 120000" in export_client
    assert "MARKET_SCAN_TRUSTED_READ_TIMEOUT_MS = Math.max(DEFAULT_REQUEST_TIMEOUT_MS, 60000)" in latest_loader
    assert 'authority: "navigation"' in history
    assert "历史扫描可信批次响应" in history and "samePublishedMarketScanRun" in history
    assert "fetchJson" not in view and "setTimeout" not in view


def test_market_scan_busy_retry_uses_server_delay_without_counting_failure() -> None:
    _run_node_script(
        r'''
import assert from "node:assert/strict";
import {
  createMarketScanPolling,
  isMarketScanReadBusy,
  marketScanReadBusyMessage,
} from "./static/js/market-scan-polling.js";

let scheduled = null;
globalThis.setTimeout = (callback, delay) => { scheduled = { callback, delay }; return 1; };
globalThis.clearTimeout = () => { scheduled = null; };
const state = {
  activated: true, visible: true, pollTimer: null, consecutiveFailures: 4,
};
let latestCalls = 0;
const polling = createMarketScanPolling({
  callbacks: { latest: () => { latestCalls += 1; } },
  state,
});
const error = new Error("全市场冻结快照正在校验，请稍后重试");
error.status = 503;
error.retryAfterMs = 1750;
assert.equal(isMarketScanReadBusy(error), true);
assert.equal(marketScanReadBusyMessage(error), "冻结快照正在校验，将在 2 秒后自动重试。");
polling.retryBusy(error, "latest");
assert.equal(state.consecutiveFailures, 0);
assert.equal(scheduled.delay, 1750);
scheduled.callback();
assert.equal(latestCalls, 1);
'''
    )


def test_market_scan_modes_default_request_contract_and_mode_copy() -> None:
    _run_node_script(
        r'''
import assert from "node:assert/strict";
import { installAppDom, marketScanPollingIdentity } from "./tests/frontend_app_flow_helpers.mjs";
import { createMarketScanController, validateMarketScanRun } from "./static/js/market-scan.js";
import { defaultMarketScanMode } from "./static/js/market-scan-contracts.js";

assert.equal(defaultMarketScanMode(new Date(2026, 6, 27, 0, 0)), "preopen");
assert.equal(defaultMarketScanMode(new Date(2026, 6, 27, 8, 0)), "preopen");
assert.equal(defaultMarketScanMode(new Date(2026, 6, 27, 9, 14, 59)), "preopen");
assert.equal(defaultMarketScanMode(new Date(2026, 6, 27, 9, 15)), "official");
assert.equal(defaultMarketScanMode(new Date(2026, 6, 27, 9, 29)), "official");
assert.equal(defaultMarketScanMode(new Date(2026, 6, 27, 9, 30)), "intraday");
assert.equal(defaultMarketScanMode(new Date(2026, 6, 27, 15, 14)), "intraday");
assert.equal(defaultMarketScanMode(new Date(2026, 6, 27, 15, 15)), "official");
assert.equal(defaultMarketScanMode(new Date(2026, 6, 26, 10, 0)), "official");

const { element } = installAppDom({ canvasContext: null });
const intradayRun = {
  id: 91, status: "running", trigger: "manual", mode: "intraday",
  rule_version: "full-market-score-v1", as_of: "2026-07-27 10:00:00",
  data_date: "2026-07-24", quote_date: "2026-07-27", scope: "SH/SZ/BJ",
  total_count: 100, excluded_count: 0, processed_count: 10, success_count: 10,
  missing_count: 0, skipped_count: 0, retry_count: 0, progress_pct: 10,
  coverage_pct: 10, created_at: "2026-07-27 10:00:00", updated_at: "2026-07-27 10:01:00",
  finished_at: null, snapshot_digest: null, snapshot_seal_origin: null, snapshot_sealed_at: null,
  message: "全市场扫描运行中",
};
assert.equal(validateMarketScanRun(intradayRun), intradayRun);
const rollingUpgradeRun = { ...intradayRun };
delete rollingUpgradeRun.snapshot_digest;
delete rollingUpgradeRun.snapshot_seal_origin;
delete rollingUpgradeRun.snapshot_sealed_at;
assert.equal(validateMarketScanRun(rollingUpgradeRun), rollingUpgradeRun);
assert.equal(validateMarketScanRun({ ...intradayRun, mode: "preopen" }).mode, "preopen");
assert.throws(() => validateMarketScanRun({ ...intradayRun, mode: "preview" }), /mode/);
assert.throws(() => validateMarketScanRun({ ...intradayRun, quote_date: "2026-7-27" }), /quote_date/);
assert.throws(() => validateMarketScanRun({ ...intradayRun, quote_date: "2026-02-30" }), /quote_date/);
assert.throws(() => validateMarketScanRun({ ...intradayRun, processed_count: 11 }), /计数不守恒/);
assert.throws(() => validateMarketScanRun({ ...intradayRun, progress_pct: 9 }), /progress_pct/);
assert.throws(() => validateMarketScanRun({ ...intradayRun, coverage_pct: 9 }), /coverage_pct/);
assert.throws(
  () => validateMarketScanRun({
    ...intradayRun,
    market_progress: [{
      market: "SH", total_count: 100, processed_count: 10, success_count: 9,
      missing_count: 0, skipped_count: 0, coverage_pct: 9,
    }],
  }),
  /计数不守恒/
);
const publicationDiagnostics = {
  schema_version: "market-scan-publication-diagnostics-v1",
  headline: "盘后正式扫描未达到发布可信度",
  blockers: [{
    code: "publication.snapshot.span_exceeded", label: "报价快照跨度超限",
    detail: "全市场报价快照跨度 1918 秒超过 1200 秒门槛", severity: "error",
  }],
  passed_gates: [], source_warnings: [],
};
assert.equal(
  validateMarketScanRun({ ...intradayRun, publication_diagnostics: publicationDiagnostics }).publication_diagnostics,
  publicationDiagnostics,
);
assert.throws(
  () => validateMarketScanRun({
    ...intradayRun,
    publication_diagnostics: { ...publicationDiagnostics, schema_version: "v2" },
  }),
  /schema_version/,
);
assert.throws(
  () => validateMarketScanRun({
    ...intradayRun,
    publication_diagnostics: {
      ...publicationDiagnostics,
      blockers: [{ ...publicationDiagnostics.blockers[0], severity: "critical" }],
    },
  }),
  /severity/,
);

let startInit = null;
const controller = createMarketScanController({
  root: document,
  now: new Date(2026, 6, 27, 10, 0),
  pollIntervalMs: 60000,
  async fetcher(url, init = {}) {
    assert.equal(url, "/api/market-scans");
    startInit = init;
    return { accepted: true, deduplicated: false, run: intradayRun };
  },
});
assert.equal(element("marketScanModeIntraday").checked, true);
assert.equal(element("marketScanModeOfficial").checked, false);
await controller.start();
assert.deepEqual(JSON.parse(startInit.body), { mode: "intraday" });
assert.equal(element("marketScanModeSummary").textContent, "盘中临时");
assert.equal(element("marketScanQuoteDate").textContent, "2026-07-27");
assert.equal(element("marketScanDataDate").textContent, "2026-07-24");
assert.match(element("marketScanHeadline").textContent, /盘中临时/);
assert.match(element("marketScanResultState").textContent, /盘中临时/);
assert.doesNotMatch(element("marketScanHeadline").textContent, /正式|稳定榜单/);
assert.doesNotMatch(element("marketScanResultState").textContent, /正式|稳定榜单/);
assert.equal(element("marketScanModeIntraday").disabled, false);
controller.deactivate();

const morning = installAppDom({ canvasContext: null });
let morningInit = null;
const preopenRun = {
  ...intradayRun, id: 94, mode: "preopen", as_of: "2026-07-27 08:00:00",
  created_at: "2026-07-27 08:00:00", updated_at: "2026-07-27 08:01:00",
};
const morningController = createMarketScanController({
  root: document,
  now: new Date(2026, 6, 27, 8, 0),
  async fetcher(url, init = {}) {
    assert.equal(url, "/api/market-scans");
    morningInit = init;
    return { accepted: true, deduplicated: false, run: preopenRun };
  },
});
assert.equal(morning.element("marketScanModePreopen").checked, true);
assert.equal(morning.element("marketScanModeIntraday").checked, false);
assert.equal(morning.element("marketScanModeOfficial").checked, false);
await morningController.start();
assert.deepEqual(JSON.parse(morningInit.body), { mode: "preopen" });
assert.equal(morning.element("marketScanModeSummary").textContent, "盘前复盘");
assert.match(morning.element("marketScanHeadline").textContent, /盘前复盘/);
assert.match(morning.element("marketScanResultState").textContent, /盘前复盘/);
assert.doesNotMatch(morning.element("marketScanHeadline").textContent, /盘后正式|盘中临时/);
assert.doesNotMatch(morning.element("marketScanResultState").textContent, /盘后正式|盘中临时/);
morningController.deactivate();

const manual = installAppDom({ canvasContext: null });
let manualInit = null;
const officialRun = {
  ...intradayRun, id: 93, mode: "official", data_date: "2026-07-27", quote_date: "2026-07-27",
};
const manualController = createMarketScanController({
  root: document,
  now: new Date(2026, 6, 27, 10, 0),
  async fetcher(url, init = {}) {
    manualInit = init;
    return { accepted: true, deduplicated: false, run: officialRun };
  },
});
manual.element("marketScanModeIntraday").checked = false;
manual.element("marketScanModeOfficial").checked = true;
await manualController.start();
assert.deepEqual(JSON.parse(manualInit.body), { mode: "official" });
manualController.deactivate();

const reloaded = installAppDom({ canvasContext: null });
const officialActiveRun = {
  ...intradayRun, id: 92, mode: "official", data_date: "2026-07-27", quote_date: "2026-07-27",
};
const reloadController = createMarketScanController({
  root: document,
  now: new Date(2026, 6, 27, 10, 0),
  pollIntervalMs: 60000,
  async fetcher(url) {
    if (String(url).startsWith("/api/market-scans/polling-identity?")) {
      return marketScanPollingIdentity(officialActiveRun, null, "intraday");
    }
    if (url === "/api/market-scans/latest") return officialActiveRun;
    if (String(url).startsWith("/api/market-scans/latest-published?mode=")) return null;
    if (String(url).startsWith("/api/market-scans?")) {
      return { items: [], total: 0, page: 1, page_size: 100, page_count: 0 };
    }
    throw new Error(`unexpected request: ${url}`);
  },
});
assert.equal(reloaded.element("marketScanModeIntraday").checked, true);
await reloadController.activate();
assert.equal(reloaded.element("marketScanModeIntraday").checked, true);
assert.equal(reloaded.element("marketScanModeOfficial").checked, false);
assert.equal(reloaded.element("marketScanModeIntraday").disabled, false);
assert.match(reloaded.element("marketScanBrowseContext").textContent, /当前浏览：盘中临时/);
assert.match(reloaded.element("marketScanTaskContext").textContent, /盘后正式.*不同/);
reloadController.deactivate();
'''
    )


def test_market_scan_run85_legacy_progress_projection_uses_eligible_denominator() -> None:
    _run_node_script(
        r'''
import assert from "node:assert/strict";
import { validateMarketScanRun } from "./static/js/market-scan-contracts.js";

const run85 = {
  id: 85, task_run_id: 273680, retry_of_run_id: null, status: "degraded", trigger: "scheduled",
  mode: "official", rule_version: `full-market-scan-v6:${"4".repeat(64)}`,
  as_of: "2026-08-14 16:30:58", data_date: "2026-08-14", quote_date: "2026-08-14",
  scope: "沪市 + 深市 + 北交所当前上市A股", total_count: 5543, excluded_count: 0,
  processed_count: 5543, success_count: 5497, missing_count: 0, skipped_count: 46,
  retry_count: 0, progress_pct: 100, coverage_pct: 100,
  created_at: "2026-08-14T08:30:05.385058Z", updated_at: "2026-08-14T08:44:40.022100Z",
  finished_at: "2026-08-14T08:44:40.022100Z", message: "盘后正式扫描降级完成",
  snapshot_digest: "5".repeat(64), snapshot_seal_origin: "publication",
  snapshot_sealed_at: "2026-08-14T08:44:40.022100Z",
  market_progress: [
    { market: "SH", total_count: 2312, processed_count: 2312, success_count: 2302, missing_count: 0, skipped_count: 10, coverage_pct: 100 },
    { market: "SZ", total_count: 2896, processed_count: 2896, success_count: 2882, missing_count: 0, skipped_count: 14, coverage_pct: 100 },
    { market: "BJ", total_count: 335, processed_count: 335, success_count: 313, missing_count: 0, skipped_count: 22, coverage_pct: 100 },
  ],
};
assert.equal(validateMarketScanRun(structuredClone(run85)).id, 85);

const currentCounts = {
  ...run85, success_count: 5496, missing_count: 1, coverage_pct: 99.98,
  market_progress: [
    { ...run85.market_progress[0], success_count: 2301, missing_count: 1, coverage_pct: 99.96 },
    run85.market_progress[1],
    run85.market_progress[2],
  ],
};
assert.equal(validateMarketScanRun(structuredClone(currentCounts)).success_count, 5496);

const zeroEligibleMarket = {
  ...run85, total_count: 3232, processed_count: 3232, success_count: 3195, skipped_count: 37,
  market_progress: [
    { market: "SH", total_count: 1, processed_count: 1, success_count: 0, missing_count: 0, skipped_count: 1, coverage_pct: 0 },
    run85.market_progress[1], run85.market_progress[2],
  ],
};
assert.equal(validateMarketScanRun(structuredClone(zeroEligibleMarket)).market_progress[0].coverage_pct, 0);
zeroEligibleMarket.market_progress[0].coverage_pct = 1;
assert.throws(() => validateMarketScanRun(zeroEligibleMarket), /market_progress\[0\]\.coverage_pct/);

const coverageShape = (coverage) => ({
  ...run85,
  market_progress: [
    { ...run85.market_progress[0], coverage_pct: coverage },
    run85.market_progress[1],
    run85.market_progress[2],
  ],
});
assert.throws(() => validateMarketScanRun(coverageShape(99.57)), /market_progress\[0\]\.coverage_pct/);
assert.throws(() => validateMarketScanRun(coverageShape(99)), /market_progress\[0\]\.coverage_pct/);
'''
    )


def test_market_scan_shows_execution_time_and_starts_top100_refresh() -> None:
    _run_node_script(
        r'''
import assert from "node:assert/strict";
import { installAppDom, marketScanPollingIdentity } from "./tests/frontend_app_flow_helpers.mjs";
import { createMarketScanController } from "./static/js/market-scan.js";

const { element } = installAppDom({ canvasContext: null });
const source = scanRun(29, "success", "沪市 + 深市 + 北交所当前上市A股");
const queued = {
  ...scanRun(30, "queued", "TOP100快速更新评分"),
  trigger: "retry", retry_of_run_id: 29, processed_count: 0, success_count: 0,
  progress_pct: 0, coverage_pct: 0, started_at: null, finished_at: null, duration_ms: null,
};
const calls = [];
const controller = createMarketScanController({
  root: document,
  now: new Date(2026, 7, 7, 16, 0),
  surfaceActive: false,
  pollIntervalMs: 60000,
  async fetcher(url, init = {}) {
    const target = String(url);
    calls.push([target, init.method || "GET"]);
    if (target.startsWith("/api/market-scans/polling-identity?")) {
      return marketScanPollingIdentity(source, source);
    }
    if (target === "/api/market-scans/latest") return source;
    if (target.startsWith("/api/market-scans/latest-published?mode=")) return source;
    if (target.startsWith("/api/market-scans?")) {
      return { items: [source], total: 1, page: 1, page_size: 100, page_count: 1 };
    }
    if (target === "/api/market-scans/29/refresh-top100") {
      return { accepted: true, deduplicated: false, run: queued };
    }
    throw new Error(`unexpected request: ${target}`);
  },
});

await controller.activate();
assert.equal(element("marketScanExecutedAt").textContent, "评分完成时间：2026-08-07 15:03:04");
assert.equal(element("marketScanExecutedAt").datetime, "2026-08-07 15:03:04");
assert.equal(element("marketScanRefreshTop100").disabled, false);
assert.match(element("marketScanRefreshTop100").title, /批次 #29.*前 100 名/);

await controller.refreshTop100();
assert.equal(calls.some(([url, method]) => url === "/api/market-scans/29/refresh-top100" && method === "POST"), true);
assert.equal(controller.state.run.id, 30);
assert.equal(element("marketScanRefreshTop100").disabled, true);
assert.equal(element("marketScanRefreshTop100").textContent, "TOP100 更新中");
assert.match(element("marketScanTaskContext").textContent, /TOP100 快更 #30/);
assert.match(element("marketScanBrowseContext").textContent, /最近发布 #29/);
controller.deactivate();

function scanRun(id, status, scope) {
  const terminal = status === "success";
  return {
    id, status, trigger: "manual", mode: "official", rule_version: "full-market-score-v5:test",
    as_of: "2026-08-07 15:03:04", data_date: "2026-08-07", quote_date: "2026-08-07", scope,
    total_count: 100, excluded_count: 0, processed_count: terminal ? 100 : 0,
    success_count: terminal ? 100 : 0, missing_count: 0, skipped_count: 0, retry_count: 0,
    progress_pct: terminal ? 100 : 0, coverage_pct: terminal ? 100 : 0,
    created_at: "2026-08-07 15:00:00", updated_at: "2026-08-07 15:03:04",
    started_at: "2026-08-07 15:00:01", finished_at: terminal ? "2026-08-07 15:03:04" : null,
    snapshot_digest: terminal ? "a".repeat(64) : null, snapshot_seal_origin: terminal ? "publication" : null,
    snapshot_sealed_at: terminal ? "2026-08-07 15:03:04" : null,
    duration_ms: terminal ? 183000 : null, message: terminal ? "扫描完成" : "等待执行",
  };
}
'''
    )


def test_market_scan_history_selection_binds_results_export_filters_and_mode() -> None:
    _run_node_script(
        r'''
import assert from "node:assert/strict";
import { installAppDom, marketScanPollingIdentity } from "./tests/frontend_app_flow_helpers.mjs";
import { createMarketScanController } from "./static/js/market-scan.js";

const { element } = installAppDom({ canvasContext: null });
const officialLatest = scanRun(31, "official", "2026-07-29");
const officialHistory = scanRun(30, "official", "2026-07-28");
const mismatchedHistory = scanRun(29, "official", "2026-07-27");
const mismatchedTrusted = { ...mismatchedHistory, snapshot_digest: "b".repeat(64) };
const intradayLatest = scanRun(41, "intraday", "2026-07-29");
const preopenLatest = scanRun(45, "preopen", "2026-07-29");
const activeTask = { ...scanRun(50, "official", "2026-07-29"), status: "running", finished_at: null, snapshot_digest: null, snapshot_seal_origin: null, snapshot_sealed_at: null, total_count: 100, processed_count: 20, success_count: 20, progress_pct: 20, coverage_pct: 20 };
const calls = [];
const exportCalls = [];
let historyDetailTimeout = null;
document.createElement = () => ({ click() {} });
globalThis.URL = { createObjectURL() { return "blob:history"; }, revokeObjectURL() {} };
const controller = createMarketScanController({
  root: document,
  now: new Date(2026, 6, 29, 16, 0),
  pollIntervalMs: 60000,
  async fetcher(url, options = {}) {
    const target = String(url);
    calls.push(target);
    if (target.startsWith("/api/market-scans/polling-identity?")) {
      const mode = new URLSearchParams(target.split("?", 2)[1]).get("mode");
      const published = mode === "intraday" ? intradayLatest : mode === "preopen" ? preopenLatest : officialLatest;
      return marketScanPollingIdentity(activeTask, published, mode);
    }
    if (target === "/api/market-scans/latest") return activeTask;
    if (target.startsWith("/api/market-scans/latest-published?mode=official")) return officialLatest;
    if (target.startsWith("/api/market-scans/latest-published?mode=intraday")) return intradayLatest;
    if (target.startsWith("/api/market-scans/latest-published?mode=preopen")) return preopenLatest;
    if (target.startsWith("/api/market-scans?")) {
      const query = new URLSearchParams(target.split("?", 2)[1]);
      const items = query.get("mode") === "intraday"
        ? [intradayLatest]
        : query.get("mode") === "preopen" ? [preopenLatest] : [officialLatest, officialHistory, mismatchedHistory];
      return { items, total: items.length, page: 1, page_size: 100, page_count: 1 };
    }
    const detail = /^\/api\/market-scans\/(\d+)$/.exec(target);
    if (detail) {
      historyDetailTimeout = options.timeoutMs;
      if (Number(detail[1]) === 29) return mismatchedTrusted;
      return [officialLatest, officialHistory, intradayLatest, preopenLatest]
        .find((item) => item.id === Number(detail[1]));
    }
    const match = /^\/api\/market-scans\/(\d+)\/results\?/.exec(target);
    if (match) return resultPage(Number(match[1]));
    throw new Error(`unexpected request: ${target}`);
  },
  async exportFetcher(url) {
    exportCalls.push(String(url));
    return {
      ok: true,
      headers: { get(name) { return name === "content-type" ? "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet" : null; } },
      async blob() { return new Blob(["xlsx"]); },
    };
  },
});

await controller.activate();
if (!controller.state.publishedRun) throw new Error(element("marketScanHeadline").textContent);
assert.equal(controller.state.publishedRun.id, 31);
assert.equal(element("marketScanTableWrap").dataset.marketScanRunId, "31");
assert.match(element("marketScanTaskContext").textContent, /#50/);

element("marketScanHistoryStatus").value = "success";
element("marketScanHistoryDate").value = "2026-07-28";
element("marketScanHistoryRefresh").listeners.click();
await flushPromises();
assert.equal(calls.some((url) => url.includes("mode=official") && url.includes("status=success") && url.includes("data_date=2026-07-28")), true);
assert.equal(calls.some((url) => url.includes("authority=navigation")), true);

element("marketScanHistoryRun").value = "30";
element("marketScanHistoryRun").listeners.change();
await flushPromises();
assert.equal(controller.state.selectedHistoryRunId, 30);
assert.equal(historyDetailTimeout, 60000);
assert.equal(controller.state.publishedRun.id, 30);
assert.equal(element("marketScanTableWrap").dataset.marketScanRunId, "30");
assert.match(element("marketScanBrowseContext").textContent, /历史批次 #30/);
await controller.exportResults();
assert.equal(exportCalls.at(-1).startsWith("/api/market-scans/30/export.xlsx?"), true);

element("marketScanHistoryRun").value = "29";
element("marketScanHistoryRun").listeners.change();
await flushPromises();
assert.equal(controller.state.selectedHistoryRunId, 30);
assert.equal(controller.state.publishedRun.id, 30);
assert.equal(element("marketScanHistoryRun").value, "30");
assert.match(element("marketScanHistoryFeedback").textContent, /导航身份不一致/);
assert.equal(calls.some((url) => url.startsWith("/api/market-scans/29/results?")), false);

element("marketScanModeOfficial").checked = false;
element("marketScanModeIntraday").checked = true;
element("marketScanModeIntraday").listeners.change();
await flushPromises();
assert.equal(controller.state.browseMode, "intraday");
assert.equal(controller.state.selectedHistoryRunId, null);
assert.equal(controller.state.publishedRun.id, 41);
assert.equal(element("marketScanTableWrap").dataset.marketScanRunId, "41");
assert.match(element("marketScanBrowseContext").textContent, /盘中临时/);
assert.match(element("marketScanTaskContext").textContent, /盘后正式.*不同/);

element("marketScanModeIntraday").checked = false;
element("marketScanModePreopen").checked = true;
element("marketScanModePreopen").listeners.change();
await flushPromises();
assert.equal(controller.state.browseMode, "preopen");
assert.equal(controller.state.selectedHistoryRunId, null);
assert.equal(controller.state.publishedRun.id, 45);
assert.equal(element("marketScanTableWrap").dataset.marketScanRunId, "45");
assert.match(element("marketScanBrowseContext").textContent, /盘前复盘/);
assert.match(element("marketScanTaskContext").textContent, /盘后正式.*不同/);
assert.equal(calls.some((url) => url.includes("mode=preopen")), true);
controller.deactivate();

function scanRun(id, mode, dataDate) {
  return {
    id, status: "success", trigger: "manual", mode, rule_version: "full-market-score-v4:test",
      as_of: `${dataDate} 16:00:00`, data_date: dataDate, quote_date: dataDate, scope: "沪市 + 深市 + 北交所当前上市A股",
    total_count: 1, excluded_count: 0, processed_count: 1, success_count: 1, missing_count: 0,
    skipped_count: 0, retry_count: 0, progress_pct: 100, coverage_pct: 100,
    created_at: `${dataDate} 16:00:00`, updated_at: `${dataDate} 16:10:00`, finished_at: `${dataDate} 16:10:00`,
    snapshot_digest: "a".repeat(64), snapshot_seal_origin: "publication", snapshot_sealed_at: `${dataDate} 16:10:00`,
  };
}

function resultPage(runId) {
  const run = [officialLatest, officialHistory, intradayLatest, preopenLatest].find((item) => item.id === runId);
  return {
    run, total: 1, page: 1, page_size: 100, page_count: 1,
    items: [{
      run_id: runId, rank: 1, symbol: "600519.SH", code: "600519", market: "SH", name: "贵州茅台",
      status: "success", is_st: false, is_new: false, score: 90, raw_score: 90.1,
      trend_score: 90, leader_score: 90, data_quality_score: 90, price: 100,
      data_date: run.data_date, quote_timestamp: `${run.quote_date} 15:00:00`,
      quote_observed_at: `${run.quote_date}T07:00:00Z`, quote_source: "fixture",
      kline_source: "fixture", adjustment_mode: "qfq", reason: null, error: null,
      tags: [], metrics: {}, updated_at: `${run.data_date} 16:10:00`,
    }],
  };
}

async function flushPromises() {
  for (let index = 0; index < 80; index += 1) await Promise.resolve();
}
'''
    )


def test_market_scan_query_and_rows_are_bounded_encoded_and_escaped() -> None:
    _run_node_script(
        r'''
import assert from "node:assert/strict";
import {
  buildMarketScanResultsUrl,
  marketScanResultRow,
  marketScanResultsUrl,
} from "./static/js/market-scan.js";
import { buildMarketScanExportUrl, marketScanExportFilename } from "./static/js/market-scan-view.js";

const input = (value = "") => ({ value });
const url = marketScanResultsUrl(17, 2, {
  status: input("all"),
  market: input("BJ"),
  industry: input("专用 设备"),
  isSt: input("false"),
  isNew: input("true"),
  quality: input("70"),
  keyword: input("920066 科拜尔"),
  sort: input("score"),
  order: input("desc"),
});
const parsed = new URL(url, "http://localhost");
assert.equal(buildMarketScanResultsUrl(17, 2, {
  status: input("all"), market: input("BJ"), industry: input("专用 设备"),
  isSt: input("false"), isNew: input("true"), quality: input("70"),
  keyword: input("920066 科拜尔"), sort: input("score"), order: input("desc"),
}), url);
assert.equal(parsed.pathname, "/api/market-scans/17/results");
assert.equal(parsed.searchParams.get("page"), "2");
assert.equal(parsed.searchParams.get("page_size"), "100");
assert.equal(parsed.searchParams.get("status"), "all");
assert.equal(parsed.searchParams.get("market"), "BJ");
assert.equal(parsed.searchParams.get("industry"), "专用 设备");
assert.equal(parsed.searchParams.get("is_st"), "false");
assert.equal(parsed.searchParams.get("is_new"), "true");
assert.equal(parsed.searchParams.get("min_data_quality_score"), "70");
assert.equal(parsed.searchParams.get("keyword"), "920066 科拜尔");
assert.equal(parsed.searchParams.get("sort"), "score");
assert.equal(parsed.searchParams.get("order"), "desc");

const mobileUrl = new URL(buildMarketScanResultsUrl(17, 1, {
  status: input("success"), market: input(""), industry: input(""),
  isSt: input(""), isNew: input(""), quality: input(""), keyword: input(""),
  sort: input("rank"), order: input("asc"),
  rows: { ownerDocument: { defaultView: { matchMedia: () => ({ matches: true }) } } },
}), "http://localhost");
assert.equal(mobileUrl.searchParams.get("page_size"), "30");

const exportUrl = new URL(buildMarketScanExportUrl(17, {
  status: input("all"), market: input("BJ"), industry: input("专用 设备"),
  isSt: input("false"), isNew: input("true"), quality: input("70"),
  keyword: input("920066 科拜尔"), sort: input("score"), order: input("desc"),
}), "http://localhost");
assert.equal(exportUrl.pathname, "/api/market-scans/17/export.xlsx");
assert.equal(exportUrl.searchParams.has("page"), false);
assert.equal(exportUrl.searchParams.has("page_size"), false);
for (const key of [
  "status", "market", "industry", "is_st", "is_new",
  "min_data_quality_score", "keyword", "sort", "order",
]) {
  assert.equal(exportUrl.searchParams.get(key), parsed.searchParams.get(key), `${key} drifted from results query`);
}
const advancedElements = {
  status: input("success"),
  market: { value: "SH", selectedOptions: [{ value: "SH" }, { value: "SZ" }] },
  industry: input("银行，半导体"), isSt: input(""), isNew: input("false"),
  scoreMin: input("60"), scoreMax: input("95"),
  trendMin: input("55"), trendMax: input("90"),
  changeMin: input("-2.5"), changeMax: input("9.5"),
  turnoverMin: input("1.2"), turnoverMax: input("30"),
  amountMin: input("1000000"), amountMax: input("500000000"),
  quality: input("75"), qualityMax: input("99"), keyword: input("龙头"),
  confidenceMin: input("80"), riskMax: input("35"), tradabilityMin: input("70"),
  probabilityMin: { value: "61.2", disabled: false },
  probabilityHorizonInputs: [{ value: "1", checked: false }, { value: "5", checked: true }, { value: "20", checked: false }],
  sort: input("score"), order: input("desc"),
  sort2: input("amount"), order2: input("desc"),
  sort3: input("symbol"), order3: input("asc"),
};
const advancedResults = new URL(buildMarketScanResultsUrl(17, 1, advancedElements), "http://localhost");
const advancedExport = new URL(buildMarketScanExportUrl(17, advancedElements), "http://localhost");
assert.deepEqual(advancedResults.searchParams.getAll("market"), ["SH", "SZ"]);
assert.deepEqual(advancedResults.searchParams.getAll("industry"), ["银行", "半导体"]);
assert.deepEqual(advancedResults.searchParams.getAll("sort"), ["score", "amount", "symbol"]);
assert.deepEqual(advancedResults.searchParams.getAll("order"), ["desc", "desc", "asc"]);
for (const key of [
  "min_score", "max_score", "min_trend_score", "max_trend_score",
  "min_change_pct", "max_change_pct", "min_turnover_rate", "max_turnover_rate",
  "min_amount", "max_amount", "min_data_quality_score", "max_data_quality_score",
  "min_confidence", "max_risk", "min_tradability",
  "probability_horizon", "min_upside_probability",
]) {
  assert.equal(advancedExport.searchParams.get(key), advancedResults.searchParams.get(key), `${key} drifted`);
}
assert.equal(advancedResults.searchParams.get("probability_horizon"), "5");
assert.equal(advancedResults.searchParams.get("min_upside_probability"), "0.612");
const disabledProbability = new URL(buildMarketScanResultsUrl(17, 1, {
  ...advancedElements, probabilityMin: { value: "99", disabled: true },
}), "http://localhost");
assert.equal(disabledProbability.searchParams.has("probability_horizon"), false);
assert.equal(disabledProbability.searchParams.has("min_upside_probability"), false);
assert.throws(
  () => buildMarketScanResultsUrl(17, 1, { ...advancedElements, scoreMin: input("96") }),
  /下限不能大于上限/,
);
assert.equal(
  marketScanExportFilename("attachment; filename*=UTF-8''%E5%85%A8%E5%B8%82%E5%9C%BA%E6%A6%9C%E5%8D%95.xlsx", { id: 17 }),
  "全市场榜单.xlsx",
);
assert.equal(
  marketScanExportFilename('attachment; filename="AShareRadar-full-market-run17.xlsx"', { id: 17 }),
  "AShareRadar-full-market-run17.xlsx",
);
assert.equal(marketScanExportFilename("", { id: 17, quote_date: "2026-07-29" }), "AShareRadar-market-scan-2026-07-29.xlsx");

const row = marketScanResultRow({
  rank: 1,
  symbol: '920066.BJ"><script>alert(1)</script>',
  code: "920066",
  market: "BJ",
  name: "科拜尔<script>",
  industry: "专用设备",
  status: "success",
  score: 88,
  trend_score: 72,
  change_pct: 1.25,
  turnover_rate: 2.5,
  amount: 125000000,
  data_quality_score: 91,
  tags: ["趋势向上", "量价配合"],
  is_new: true,
});
assert.equal(row.includes("<script>"), false);
assert.equal(row.includes("&lt;script&gt;"), true);
assert.equal(row.includes("page_size=5000"), false);
assert.equal(row.includes("+1.25%"), true);
assert.equal(row.includes("1.3亿"), true);
'''
    )


def test_market_scan_snapshot_is_persisted_read_only_evidence_with_distinct_current_action() -> None:
    _run_node_script(
        r'''
import assert from "node:assert/strict";
import { marketScanResultRow } from "./static/js/market-scan.js";
import {
  marketScanSnapshotContent,
  toggleMarketScanSnapshot,
} from "./static/js/market-scan-snapshot-view.js";
import { createMarketScanRowClickHandler } from "./static/js/market-scan-row-actions.js";

const item = {
  run_id: 31, rank: 2, symbol: "600519.SH", code: "600519", market: "SH", name: "贵州茅台",
  industry: "白酒", metadata_source: "provider-pool", status: "success", score: 93,
  raw_score: 92.123456, trend_score: 88, leader_score: 95, data_quality_score: 90,
  quote_timestamp: "2026-07-29 15:00:00", data_date: "2026-07-29",
  quote_source: "tencent", kline_source: "akshare", adjustment_mode: "qfq",
  quote_fallback_used: true, kline_fallback_used: false, metadata_degraded: true,
  degradation_reasons: ["quote_fallback", "industry_missing"], tags: [],
  score_details: {
    run_rule_version: "full-market-score-v4:abc12345", score_spec_hash: "abc12345",
    components: {
      leader_score: { base: 50, trend_delta: 12, rule_deltas: { amount: 8, "<script>": 1 } },
      final_score: { quality_penalty: 1.5, base: 93.5, rank_discount: 1.376544, raw: 92.123456, score: 93 },
      rank_refinement: { score: 0.6, weighted_terms: { ma_alignment: 0.24, return_20d_pct: 0.18 } },
      score_dimensions: {
        scores: { alpha_1d: 61, alpha_5d: 72, alpha_20d: 75, confidence: 90, risk: 22, tradability: 85 },
        volume_context: {
          price_volume_alignment: "intraday-time-aligned-volume-unavailable-neutralized",
          volume_data_date: "2026-07-28",
        },
        point_in_time_evidence: { status: "verified-persisted-at-scan-time", payload_digest: "abcdef1234567890" },
      },
    },
    ranking: { tie_break: [["raw_score", "desc"], ["symbol", "asc"]], tie_break_values: { raw_score: 92.123456, symbol: "600519.SH" } },
  },
};
const run = { id: 31, mode: "official", quote_date: "2026-07-29", data_date: "2026-07-29" };
const html = marketScanResultRow(item, { run });
assert.match(html, /查看扫描快照/);
assert.match(html, /打开当前个股分析/);
assert.match(html, /只读持久化证据/);
assert.match(html, /质量扣分/);
assert.match(html, /raw_score 降序 → symbol 升序/);
assert.match(html, /行情使用兜底源/);
assert.match(html, /盘中缺少同一时刻量能证据，量能生命周期已置零/);
assert.equal(html.includes("<script>"), false);
assert.equal(html.includes("&lt;script&gt;"), true);
assert.match(marketScanSnapshotContent({ run_id: 1, score_details: {} }), /不会用当前规则补算历史证据/);

const v5Item = structuredClone(item);
v5Item.score_details.components.continuous_trend = v5Item.score_details.components.rank_refinement;
delete v5Item.score_details.components.rank_refinement;
delete v5Item.score_details.components.final_score.rank_discount;
v5Item.score_details.components.final_score.continuous_trend_adjustment = 1.303336;
const v5Html = marketScanSnapshotContent(v5Item);
assert.match(v5Html, /连续趋势调整/);
assert.match(v5Html, /连续中期趋势/);
assert.equal(v5Html.includes("精排扣分"), false);

const target = { hidden: true };
const attributes = {};
const button = { dataset: { marketScanSnapshotTarget: "snapshot-31" }, setAttribute(name, value) { attributes[name] = value; } };
const root = { getElementById(id) { assert.equal(id, "snapshot-31"); return target; } };
assert.equal(toggleMarketScanSnapshot(root, button), true);
assert.equal(target.hidden, false);
assert.equal(attributes["aria-expanded"], "true");

const messages = [];
let origin = null;
const currentButton = {
  dataset: { marketScanSymbol: "600519.SH", marketScanRunId: "31", marketScanMode: "official", marketScanQuoteDate: "2026-07-29", marketScanDataDate: "2026-07-29" },
};
const handler = createMarketScanRowClickHandler({
  view: { announce(message) { messages.push(message); }, toggleSnapshot() { throw new Error("not snapshot"); } },
  onSelectStock(symbol, value) { assert.equal(symbol, "600519.SH"); origin = value; },
});
handler({ target: { closest(selector) { return selector.includes("snapshot") ? null : currentButton; } } });
assert.deepEqual(origin, { source: "market-scan", runId: 31, mode: "official", quoteDate: "2026-07-29", dataDate: "2026-07-29" });
assert.match(messages[0], /当前可用数据，不是历史扫描快照/);
'''
    )


def test_market_scan_upside_probability_shadow_contract_is_gated_and_auditable() -> None:
    _run_node_script(
        r'''
import assert from "node:assert/strict";
import {
  marketScanProbabilityCell,
  marketScanProbabilitySnapshot,
  isMarketScanProbabilitySourceCapturePending,
  normalizeMarketScanProbabilityResearch,
  normalizeMarketScanUpsideProbabilities,
  renderMarketScanProbabilityResearch,
  resetMarketScanProbabilityResearch,
  selectedMarketScanProbabilityHorizon,
} from "./static/js/market-scan-probability-view.js";

const binding = (runId) => ({
  binding_status: "verified", legacy: false, run_id: runId,
  mode: "official", scope: "沪市 + 深市 + 北交所当前上市A股",
  rule_version: `full-market-scan-v6:${"a".repeat(64)}`,
  quote_date: "2026-07-31", data_date: "2026-07-31",
  scan_rule_hash: "a".repeat(64),
  production_score_rule_version: "full-market-score-v4",
  production_score_spec_hash: "b".repeat(64),
  cohort_contract: {
    mode: "official", scope: "沪市 + 深市 + 北交所当前上市A股",
    rule_version: `full-market-scan-v6:${"a".repeat(64)}`,
  },
});
const artifact = {
  schema_version: "market-scan-probability-artifact-v1",
  run_id: 42,
  run_binding: binding(42),
  horizons: {
    "5": { net_excess_positive: {
      status: "calibrated_shadow", horizon: 5, target: "net_excess_positive", base_rate: 0.514,
      selection_qualified: true, selection_qualification: { passed: true }, filter_qualified: true,
      counts: { training_session_count: 120, calibration_session_count: 40, test_session_count: 60, observation_count: 180000 },
      calibration_metrics: { calibrated: { brier_score: 0.1964, brier_skill_score: 0.127, ece: 0.034, auc: 0.681, bin_monotonic: true } },
      versions: { model: "up-probability-v1", feature: "features-v1", label: "label-v1", cost_model: "cost-v1" },
      training_cutoff: "2026-07-31",
      limitations: ["Shadow only"],
    } },
    "20": { net_excess_positive: {
      status: "calibrated_shadow", horizon: 20, target: "net_excess_positive", base_rate: 0.55,
      selection_qualified: true, selection_qualification: { passed: true }, filter_qualified: true,
      counts: { training_session_count: 100, calibration_session_count: 30, test_session_count: 50, observation_count: 90000 },
      model_version: "up-probability-v1", feature_version: "features-v1",
      label_version: "label-v1", cost_model_version: "cost-v1", training_cutoff: "2026-07-11",
      limitations: ["20d Shadow only"],
    } },
  },
};
const research = normalizeMarketScanProbabilityResearch(artifact, 42);
const probabilities = normalizeMarketScanUpsideProbabilities({
  "5": { net_excess_positive: {
    status: "calibrated_shadow", probability: 0.612, base_rate: 0.514,
    calibration_bias_interval: {
      lower: -0.052, upper: 0.048, level: 0.95,
      method: "date_block_bootstrap_signed_calibration_bias",
      semantics: "signed_observed_rate_minus_probability_bias",
    },
    calibration_adjusted_probability_interval: {
      lower: 0.56, upper: 0.66, level: 0.95,
      method: "date_block_bootstrap_calibration_offset",
      semantics: "calibration_adjusted_probability_interval_not_individual_outcome_interval",
    },
  } },
}, research);
const item = { symbol: "600519.SH", upside_probabilities: probabilities };
assert.match(marketScanProbabilityCell(item, 5, research), /持有5日（D\+6） 61.2%/);
assert.match(marketScanProbabilityCell(item, 5, research), /群体校准调整区间 56.0%–66.0%（非个股结果区间）/);
assert.match(marketScanProbabilityCell(item, 1, research), />—</);
assert.doesNotMatch(marketScanProbabilityCell(item, 1, research), /证据不足|0\.0%|50\.0%/);
assert.match(marketScanProbabilityCell(item, 20, research), /个股预测不可用/);
const snapshot = marketScanProbabilitySnapshot(item, research);
assert.match(snapshot, /上涨概率研究 · 冻结 Shadow 证据/);
assert.match(snapshot, /up-probability-v1/);
assert.match(snapshot, /2026-07-31/);
assert.match(snapshot, /Shadow only/);
assert.match(snapshot, /训练 120 日 · 校准 40 日 · 测试 60 日 · 180000 条/);
assert.match(snapshot, /校准指标/);
assert.match(snapshot, /Brier 0.1964 · BSS 0.127 · ECE 0.034 · AUC 0.681 · 分箱单调 是/);
const positiveBias = normalizeMarketScanUpsideProbabilities({ "5": {
  status: "calibrated_shadow", probability: 0.7,
  calibration_bias_interval: {
    lower: 0.1, upper: 0.2, level: 0.95,
    method: "date_block_bootstrap_signed_calibration_bias",
    semantics: "signed_observed_rate_minus_probability_bias",
  },
  calibration_adjusted_probability_interval: {
    lower: 0.8, upper: 0.9, level: 0.95,
    method: "date_block_bootstrap_calibration_offset",
    semantics: "calibration_adjusted_probability_interval_not_individual_outcome_interval",
  },
} }, research);
assert.equal(positiveBias["5"].calibration_bias_interval.lower, 0.1);
assert.equal(positiveBias["5"].calibration_adjusted_probability_interval.lower, 0.8);

const element = () => ({
  value: "", disabled: false, checked: false, dataset: {}, className: "", textContent: "",
  setAttribute(name, value) { this[name] = String(value); },
});
const elements = {
  probabilityResearch: element(), probabilityStatus: element(), probabilityTarget: element(),
  probabilityBaseRate: element(), probabilityEvidence: element(), probabilityEffectiveness: element(), probabilityVersion: element(),
  probabilityCutoff: element(), probabilityLimitations: element(), probabilityMin: element(),
  probabilityFilterHelp: element(), probabilityHorizonInputs: [element(), element(), element()],
};
elements.probabilityHorizonInputs[0].value = "1";
elements.probabilityHorizonInputs[1].value = "5";
elements.probabilityHorizonInputs[1].checked = true;
elements.probabilityHorizonInputs[2].value = "20";
assert.equal(selectedMarketScanProbabilityHorizon(elements), 5);
renderMarketScanProbabilityResearch(elements, research);
assert.equal(elements.probabilityStatus.textContent, "样本外已校准");
assert.equal(elements.probabilityTarget.textContent, "未来所选周期净超额收益为正");
assert.equal(elements.probabilityBaseRate.textContent, "51.4%");
assert.equal(elements.probabilityEffectiveness.textContent, "通过选股门禁");
assert.equal(elements.probabilityMin.disabled, false);

research.horizons["5"].selection_qualified = false;
research.horizons["5"].selection_qualification = {
  passed: false, gates: { positive_oos_brier_skill: false, multiple_complete_oos_folds: false },
};
renderMarketScanProbabilityResearch(elements, research);
assert.equal(elements.probabilityStatus.textContent, "样本外已校准");
assert.equal(elements.probabilityEffectiveness.textContent, "未通过：总体 Brier Skill、完整 OOS 折数");
assert.equal(elements.probabilityMin.disabled, true);
assert.match(elements.probabilityFilterHelp.textContent, /效力门禁未通过/);
research.horizons["5"].selection_qualified = true;
research.horizons["5"].selection_qualification = { passed: true };
elements.probabilityHorizonInputs[1].checked = false;
elements.probabilityHorizonInputs[0].checked = true;
renderMarketScanProbabilityResearch(elements, research);
assert.equal(selectedMarketScanProbabilityHorizon(elements), 1);
assert.equal(elements.probabilityStatus.textContent, "尚未生成研究证据");
assert.equal(elements.probabilityMin.disabled, true);
elements.probabilityHorizonInputs[0].checked = false;
elements.probabilityHorizonInputs[2].checked = true;
renderMarketScanProbabilityResearch(elements, research);
assert.equal(selectedMarketScanProbabilityHorizon(elements), 20);
assert.equal(elements.probabilityBaseRate.textContent, "55.0%");
assert.equal(elements.probabilityMin.disabled, false);

const legacy = normalizeMarketScanProbabilityResearch(undefined, 42);
const legacyProbabilities = normalizeMarketScanUpsideProbabilities(undefined, legacy);
assert.equal(legacy.horizons["5"].status, "not_generated");
assert.equal(legacyProbabilities["5"].probability, null);
const legacySnapshot = marketScanProbabilitySnapshot({ upside_probabilities: legacyProbabilities }, legacy);
assert.match(legacySnapshot, /上涨概率研究 · 尚未生成/);
assert.match(legacySnapshot, /不展示概率或群体校准调整区间/);
assert.doesNotMatch(legacySnapshot, /冻结 Shadow 证据/);
renderMarketScanProbabilityResearch(elements, legacy);
assert.equal(elements.probabilityStatus.textContent, "尚未生成研究证据");
assert.equal(elements.probabilityBaseRate.textContent, "--");
assert.equal(elements.probabilityEvidence.textContent, "--");
assert.equal(elements.probabilityMin.disabled, true);
const pending = normalizeMarketScanProbabilityResearch({
  run_id: 46,
  status: "not_generated",
  availability: "source_capture_pending",
  pipeline_stage: "source_capture_pending",
  limitations: ["source_capture_pending"],
  horizons: {},
}, 46);
assert.equal(isMarketScanProbabilitySourceCapturePending(pending), true);
assert.equal(pending.horizons["5"].probability, null);
assert.equal(pending.horizons["5"].filter_qualified, false);
renderMarketScanProbabilityResearch(elements, pending);
assert.equal(elements.probabilityStatus.textContent, "正在归档研究样本");
assert.equal(elements.probabilityMin.disabled, true);
assert.match(elements.probabilityLimitations.textContent, /真实点时源样本正在进入研究归档/);
assert.match(elements.probabilityFilterHelp.textContent, /筛选保持关闭/);
const actionIneligible = normalizeMarketScanProbabilityResearch({
  run_id: 47,
  status: "not_generated",
  availability: "source_scan_action_ineligible",
  limitations: ["source_scan_action_ineligible"],
  horizons: {},
}, 47);
assert.equal(isMarketScanProbabilitySourceCapturePending(actionIneligible), false);
renderMarketScanProbabilityResearch(elements, actionIneligible);
assert.equal(elements.probabilityStatus.textContent, "未进入研究归档");
assert.match(elements.probabilityLimitations.textContent, /评分分布或动作源证据未通过，未进入研究归档/);
assert.equal(elements.probabilityMin.disabled, true);
const publishedIntradayIneligible = normalizeMarketScanProbabilityResearch({
  run_id: 48,
  status: "not_generated",
  availability: "ineligible_run_contract",
  limitations: ["probability_requires_published_official_full_market_run"],
  run_binding: {
    binding_status: "legacy_unbound", legacy: true, run_id: 48, mode: "intraday",
  },
  horizons: {},
}, 48);
assert.equal(isMarketScanProbabilitySourceCapturePending(publishedIntradayIneligible), false);
assert.equal(publishedIntradayIneligible.run_binding.mode, "intraday");
for (const horizon of ["1", "5", "20"]) {
  assert.equal(publishedIntradayIneligible.horizons[horizon].probability, null);
  assert.equal(publishedIntradayIneligible.horizons[horizon].filter_qualified, false);
}
elements.probabilityMin.value = "75";
renderMarketScanProbabilityResearch(elements, publishedIntradayIneligible);
assert.equal(elements.probabilityResearch["aria-busy"], "false");
assert.equal(elements.probabilityStatus.textContent, "来源批次不符合研究归档合同·未进入归档");
assert.doesNotMatch(elements.probabilityStatus.textContent, /批次未发布/);
assert.equal(elements.probabilityBaseRate.textContent, "--");
assert.equal(elements.probabilityMin.disabled, true);
assert.equal(elements.probabilityMin.value, "");
assert.match(elements.probabilityLimitations.textContent, /仅已发布的盘后正式全市场原发布封印批次/);
assert.match(elements.probabilityFilterHelp.textContent, /来源批次不符合.*筛选保持关闭/);
const ineligibleSnapshot = marketScanProbabilitySnapshot(
  { upside_probabilities: normalizeMarketScanUpsideProbabilities(undefined, publishedIntradayIneligible) },
  publishedIntradayIneligible,
);
assert.match(ineligibleSnapshot, /上涨概率研究 · 未进入归档/);
assert.match(ineligibleSnapshot, /概率、区间与选股筛选保持为空或关闭/);
assert.match(ineligibleSnapshot, /来源批次不符合概率研究归档合同/);
assert.doesNotMatch(ineligibleSnapshot, /当前批次未发布/);

elements.probabilityMin.value = "80";
resetMarketScanProbabilityResearch(elements, 86, { terminalUnpublished: true });
assert.equal(elements.probabilityResearch["aria-busy"], "false");
assert.equal(elements.probabilityStatus.textContent, "批次未发布·未进入研究归档");
assert.equal(elements.probabilityBaseRate.textContent, "--");
assert.equal(elements.probabilityEvidence.textContent, "--");
assert.equal(elements.probabilityMin.disabled, true);
assert.equal(elements.probabilityMin.value, "");
assert.match(elements.probabilityLimitations.textContent, /未发布盘后正式全市场榜单/);
resetMarketScanProbabilityResearch(elements, 87);
assert.equal(elements.probabilityResearch["aria-busy"], "true");
assert.equal(elements.probabilityStatus.textContent, "正在读取证据");
assert.throws(
  () => normalizeMarketScanProbabilityResearch({
    run_id: 49, status: "not_generated", horizons: {
      "5": { status: "not_generated", probability: 0.6, filter_qualified: false },
    },
  }, 49),
  /证据不足时必须为空/,
);
const insufficient = normalizeMarketScanProbabilityResearch({
  run_id: 43, run_binding: binding(43), horizons: { "5": { net_excess_positive: {
    status: "insufficient_data", horizon: 5, target: "net_excess_positive", base_rate: null,
    counts: { available_independent_session_count: 3, observation_count: 16470, label_coverage: 0.8897 },
    contract: {
      split: { minimum_train_sessions: 120, minimum_calibration_sessions: 40, minimum_test_sessions: 60, gap_sessions: 5 },
      evaluation: { minimum_label_coverage: 0.95 },
    },
    limitations: ["minimum_independent_sessions", "minimum_label_coverage"],
  } } },
}, 43);
elements.probabilityHorizonInputs[2].checked = false;
elements.probabilityHorizonInputs[1].checked = true;
renderMarketScanProbabilityResearch(elements, insufficient);
assert.equal(elements.probabilityStatus.textContent, "研究已生成·样本不足");
assert.equal(elements.probabilityEvidence.textContent, "独立日期 3/230 · 标签覆盖 89.0%/95.0% · 16470 条");
assert.equal(elements.probabilityLimitations.textContent, "独立交易日不足；标签覆盖率不足");
assert.match(marketScanProbabilityCell(item, 5, insufficient), />—</);
assert.doesNotMatch(marketScanProbabilityCell(item, 5, insufficient), /证据不足|样本不足|61\.2%/);
const archived = normalizeMarketScanProbabilityResearch({
  run_id: 44, run_binding: binding(44), horizons: { "5": { net_excess_positive: {
    status: "insufficient_data", horizon: 5, target: "net_excess_positive", base_rate: null,
    counts: {
      available_independent_session_count: 0, archived_independent_session_count: 1,
      mature_label_session_count: 0, observation_count: 5499, label_coverage: 0,
    },
    contract: {
      split: { minimum_train_sessions: 120, minimum_calibration_sessions: 40, minimum_test_sessions: 60, gap_sessions: 5 },
      evaluation: { minimum_label_coverage: 0.95 },
    },
    limitations: ["live_point_in_time_source_archived", "waiting_fixed_horizon_labels"],
  } } },
}, 44);
renderMarketScanProbabilityResearch(elements, archived);
assert.equal(elements.probabilityEvidence.textContent, "已归档 1 日 · 成熟 / 可用 0 / 0 / 230 · 标签覆盖 0.0%/95.0% · 5499 条点时样本");
assert.equal(elements.probabilityLimitations.textContent, "已归档真实点时样本；等待固定周期标签成熟");
const archivedProbabilities = normalizeMarketScanUpsideProbabilities(undefined, archived);
const archivedSnapshot = marketScanProbabilitySnapshot({ upside_probabilities: archivedProbabilities }, archived);
assert.match(archivedSnapshot, /上涨概率研究 · 点时样本积累中/);
assert.match(archivedSnapshot, /尚未形成成熟标签和样本外校准概率/);
assert.match(archivedSnapshot, /已归档 1 日/);
assert.doesNotMatch(archivedSnapshot, /冻结 Shadow 证据|61\.2%/);
const sampled = normalizeMarketScanProbabilityResearch({
  run_id: 45, run_binding: binding(45), horizons: { "5": { net_excess_positive: {
    status: "insufficient_data", horizon: 5, target: "net_excess_positive",
    probability: null, fit_status: "sampled_oos_assessment", pipeline_stage: "sampled_fit_assessed",
    selection_qualified: false,
    selection_qualification: {
      passed: false,
      gates: { full_market_benchmark_contract: false, full_market_top100_contract: false, deterministic_sample_replay: true },
    },
    counts: {
      available_independent_session_count: 260, archived_independent_session_count: 260,
      mature_label_session_count: 260, observation_count: 1430000, label_coverage: 0.98,
    },
    limitations: [
      "individual_probability_projection_not_published",
      "selection_filter_fail_closed",
      "bounded_sample_benchmark_not_full_market_contract_selection_forbidden",
    ],
  } } },
}, 45);
renderMarketScanProbabilityResearch(elements, sampled);
assert.equal(elements.probabilityEffectiveness.textContent, "有界样本评估完成 · 不具备选股资格");
assert.equal(elements.probabilityMin.disabled, true);
assert.match(elements.probabilityLimitations.textContent, /有界样本不满足全市场基准与 Top100 契约/);
const sampledProbabilities = normalizeMarketScanUpsideProbabilities(undefined, sampled);
const sampledSnapshot = marketScanProbabilitySnapshot(
  { upside_probabilities: sampledProbabilities },
  sampled,
);
assert.match(sampledSnapshot, /上涨概率研究 · 有界样本评估完成/);
assert.match(sampledSnapshot, /逐股概率、群体校准调整区间和选股筛选继续保持为空或关闭/);
assert.doesNotMatch(sampledSnapshot, /样本外已校准|冻结 Shadow 证据/);
const insufficientProbabilities = normalizeMarketScanUpsideProbabilities(undefined, insufficient);
const insufficientSnapshot = marketScanProbabilitySnapshot(
  { upside_probabilities: insufficientProbabilities },
  insufficient,
);
assert.match(insufficientSnapshot, /上涨概率研究 · 样本不足/);
assert.match(insufficientSnapshot, /概率与群体校准调整区间保持为空/);
assert.doesNotMatch(insufficientSnapshot, /冻结 Shadow 证据|61\.2%/);
assert.throws(
  () => normalizeMarketScanUpsideProbabilities({ "5": { status: "insufficient_data", probability: 0.5 } }, research),
  /证据不足时必须为空/,
);
assert.throws(
  () => normalizeMarketScanUpsideProbabilities({ "5": {
    status: "calibrated_shadow", probability: 0.7,
    calibration_bias_interval: { lower: -1.1, upper: 0.1, level: 0.95, method: "date_block_bootstrap_signed_calibration_bias", semantics: "signed_observed_rate_minus_probability_bias" },
    calibration_adjusted_probability_interval: { lower: 0.6, upper: 0.8, level: 0.95, method: "date_block_bootstrap_calibration_offset", semantics: "calibration_adjusted_probability_interval_not_individual_outcome_interval" },
  } }, research),
  /calibration_bias_interval/,
);
assert.throws(
  () => normalizeMarketScanUpsideProbabilities({ "5": {
    status: "calibrated_shadow", probability: 0.7,
    calibration_bias_interval: { lower: 0.1, upper: 0.2, level: 0.95, method: "date_block_bootstrap_signed_calibration_bias", semantics: "signed_observed_rate_minus_probability_bias" },
  } }, research),
  /必须同时存在/,
);
assert.throws(
  () => normalizeMarketScanUpsideProbabilities({ "5": { status: "calibrated_shadow", probability: 0.7 } }, legacy),
  /不能超越批次研究证据状态/,
);
assert.throws(
  () => normalizeMarketScanProbabilityResearch({ run_id: 42, horizons: { "5": {
    status: "calibrated_shadow", selection_qualified: true,
    selection_qualification: { passed: false },
  } } }, 42),
  /选股效力资格与证据状态不一致/,
);
'''
    )


def test_probability_capture_polling_uses_terminal_fake_timers_and_bounded_failures() -> None:
    _run_node_script(
        r'''
import assert from "node:assert/strict";
import { createMarketScanProbabilityPolling } from "./static/js/market-scan-probability-polling.js";

const pending = {
  run: { id: 82, status: "success" },
  probability_research: {
    status: "not_generated", availability: "source_capture_pending",
    pipeline_stage: "source_capture_pending",
  },
};
const terminal = {
  run: pending.run,
  probability_research: { status: "insufficient_data", availability: null, pipeline_stage: null },
};

function fakeTimerHarness(maximum = 3) {
  const timers = [];
  const state = { run: pending.run };
  const polling = {
    scheduleProbabilityResults() { timers.push("probabilityResults"); },
    scheduleDefault() { timers.push("default"); },
  };
  const coordinator = createMarketScanProbabilityPolling({
    options: { probabilityCapturePollMaxAttempts: maximum },
    polling,
    resultRun: () => state.run,
    state,
  });
  return { coordinator, timers };
}

const arrival = fakeTimerHarness();
arrival.coordinator.schedule(pending);
assert.deepEqual(arrival.timers, ["probabilityResults"]);
arrival.timers.shift();
let arrivalCalls = 0;
await arrival.coordinator.poll(async () => {
  arrivalCalls += 1;
  arrival.coordinator.schedule(terminal);
  return terminal;
});
assert.equal(arrivalCalls, 1);
assert.deepEqual(arrival.timers, ["default"]);

for (const availability of ["source_scan_action_ineligible", "source_capture_skipped", "ineligible_run_contract"]) {
  const terminalState = fakeTimerHarness();
  terminalState.coordinator.schedule({
    run: pending.run,
    probability_research: { status: "not_generated", availability, pipeline_stage: null },
  });
  assert.deepEqual(terminalState.timers, ["default"]);
  assert.equal(terminalState.timers.includes("probabilityResults"), false);
}

const failures = fakeTimerHarness(3);
failures.coordinator.schedule(pending);
let failureCalls = 0;
while (failures.timers.shift() === "probabilityResults") {
  await failures.coordinator.poll(async () => {
    failureCalls += 1;
    const retry = failures.coordinator.retryTarget(82);
    if (retry === "probabilityResults") failures.timers.push(retry);
    return null;
  });
}
assert.equal(failureCalls, 3);
assert.equal(failures.timers.includes("probabilityResults"), false);
'''
    )


def test_market_scan_observability_renders_eta_market_coverage_and_actionable_diagnostics() -> None:
    _run_node_script(
        r'''
import assert from "node:assert/strict";
import {
  actionableDiagnostic,
  etaText,
  renderMarketScanObservability,
} from "./static/js/market-scan-progress-view.js";

const element = () => ({ textContent: "", innerHTML: "", hidden: false });
const elements = {
  stage: element(), elapsed: element(), throughput: element(), eta: element(),
  marketProgress: element(), diagnostic: element(),
};
renderMarketScanObservability(elements, {
  status: "running", current_stage: "klines", elapsed_seconds: 125,
  throughput_per_second: 6.25, eta_seconds: null,
  market_progress: [
    { market: "SH", total_count: 100, processed_count: 80, success_count: 78, missing_count: 1, skipped_count: 1, coverage_pct: 78 },
    { market: "SZ", total_count: 90, processed_count: 60, success_count: 59, missing_count: 1, skipped_count: 0, coverage_pct: 65.56 },
    { market: "BJ", total_count: 10, processed_count: 5, success_count: 5, missing_count: 0, skipped_count: 0, coverage_pct: 50 },
  ],
});
assert.equal(elements.stage.textContent, "K 线获取");
assert.equal(elements.elapsed.textContent, "2 分 5 秒");
assert.equal(elements.throughput.textContent, "6.25 只/秒");
assert.equal(elements.eta.textContent, "估算中");
assert.match(elements.marketProgress.innerHTML, /SH/);
assert.match(elements.marketProgress.innerHTML, /1 缺失 · 1 跳过/);
assert.equal(etaText({ status: "running", eta_seconds: 65 }), "1 分 5 秒");
assert.match(actionableDiagnostic({ status: "failed", last_error: "SH 发布覆盖不足" }), /检查股票池与数据源完整性/);
assert.match(actionableDiagnostic({ status: "failed", last_error: "全市场报价采集耗时 1201 秒超过 1200 秒门槛" }), /避免混用不同时点行情/);
assert.match(actionableDiagnostic({ status: "failed", last_error: "provider 超时" }), /等待数据源恢复/);
'''
    )


def test_market_scan_message_summary_separates_snapshot_blocker_passed_distribution_and_source_warning() -> None:
    _run_node_script(
        r'''
import assert from "node:assert/strict";
import {
  marketScanHeadlineMessage,
  marketScanMessagePresentation,
  renderMarketScanMessageSummary,
} from "./static/js/market-scan-message-view.js";

const audit = "评分分布门禁 raw-score-distribution-v2：raw_score样本 5499/5499，distinct ratio 99.65%，最大并列组 2/5499（0.04%），0/100饱和 0/5499（0.00%），前100并列 0/100（0.00%），最大组 1";
const legacyMessage = `盘后正式扫描未达到发布可信度：全市场报价快照跨度 1918 秒超过 1200 秒门槛；${audit}`;
const message = `盘后正式扫描未达到发布可信度：发布阻断：全市场报价快照跨度 1918 秒超过 1200 秒门槛；已通过：${audit}`;
const run = {
  status: "failed",
  message,
  last_error: "批量行情缺失 1 只：tencent 未覆盖；akshare 最近失败，短暂冷却中；全市场报价快照跨度 1918 秒超过 1200 秒门槛；逐股结果含缺失 0、跳过 43",
};
const presentation = marketScanMessagePresentation(run);
assert.equal(
  marketScanHeadlineMessage(message),
  "盘后正式扫描未达到发布可信度：发布阻断：全市场报价快照跨度 1918 秒超过 1200 秒门槛",
);
assert.equal(presentation.headline, marketScanHeadlineMessage(message));
assert.equal(presentation.publicationBlockers, "全市场报价快照跨度 1918 秒超过 1200 秒门槛");
assert.match(presentation.passedGates, /^评分分布 · raw-score-distribution-v2/);
assert.match(presentation.sourceWarnings, /tencent 未覆盖/);
assert.doesNotMatch(presentation.sourceWarnings, /快照跨度/);
const structuredPresentation = marketScanMessagePresentation({
  ...run,
  publication_diagnostics: {
    schema_version: "market-scan-publication-diagnostics-v1",
    headline: "盘后正式扫描未达到发布可信度：发布阻断：结构化快照跨度超限",
    blockers: [{
      code: "publication.snapshot.span_exceeded", label: "报价快照跨度超限",
      detail: "结构化快照跨度超限", severity: "error",
    }],
    passed_gates: [{
      code: "score_distribution.pass", label: "评分分布",
      detail: "raw-score-distribution-v2：raw_score样本 5499/5499", severity: "info",
    }],
    source_warnings: [{
      code: "source.runtime_warning", label: "数据源告警",
      detail: "结构化数据源告警", severity: "warning",
    }],
  },
});
assert.match(structuredPresentation.headline, /结构化快照/);
assert.equal(structuredPresentation.publicationBlockers, "结构化快照跨度超限");
assert.match(structuredPresentation.passedGates, /^评分分布 · raw-score-distribution-v2/);
assert.equal(structuredPresentation.sourceWarnings, "结构化数据源告警");
const legacyPresentation = marketScanMessagePresentation({ ...run, message: legacyMessage });
assert.equal(
  legacyPresentation.headline,
  "盘后正式扫描未达到发布可信度：全市场报价快照跨度 1918 秒超过 1200 秒门槛",
);
assert.match(legacyPresentation.passedGates, /^评分分布 · raw-score-distribution-v2/);

const nodes = new Map();
const root = {
  getElementById(id) {
    if (!nodes.has(id)) nodes.set(id, { hidden: true, textContent: "", title: "" });
    return nodes.get(id);
  },
};
renderMarketScanMessageSummary(root, run);
assert.equal(root.getElementById("marketScanGateSummary").hidden, false);
assert.equal(root.getElementById("marketScanPublicationBlockers").hidden, false);
assert.equal(root.getElementById("marketScanPassedGates").hidden, false);
assert.equal(root.getElementById("marketScanSourceWarnings").hidden, false);
assert.match(root.getElementById("marketScanPassedGatesText").textContent, /评分分布/);

const failedDistribution = marketScanMessagePresentation({
  status: "failed",
  message: `盘后正式扫描未达到发布可信度：成功结果 raw_score 全部相同；${audit}`,
  last_error: `成功结果 raw_score 全部相同；${audit}`,
});
assert.equal(failedDistribution.passedGates, "");

const oldRun = { status: "failed", message: "全市场扫描失败", last_error: null };
assert.equal(marketScanHeadlineMessage(oldRun.message), oldRun.message);
renderMarketScanMessageSummary(root, oldRun);
assert.equal(root.getElementById("marketScanGateSummary").hidden, true);
'''
    )


def test_market_scan_export_uses_published_run_blob_filename_and_independent_busy_state() -> None:
    _run_node_script(
        r'''
import assert from "node:assert/strict";
import { installAppDom, marketScanPollingIdentity } from "./tests/frontend_app_flow_helpers.mjs";
import { createMarketScanController } from "./static/js/market-scan.js";

const { element } = installAppDom({ canvasContext: null });
const published = {
  id: 41, status: "degraded", trigger: "manual", mode: "official",
  rule_version: "full-market-score-v1", as_of: "2026-07-28 16:30:00",
  data_date: "2026-07-28", quote_date: "2026-07-28", scope: "沪市 + 深市 + 北交所当前上市A股",
  total_count: 3, excluded_count: 0, processed_count: 3, success_count: 2,
  missing_count: 1, skipped_count: 0, retry_count: 0, progress_pct: 100,
  coverage_pct: 66.67, created_at: "2026-07-28 16:30:00", updated_at: "2026-07-28 16:35:00",
  started_at: "2026-07-28 16:30:01", finished_at: "2026-07-28 16:35:00",
  snapshot_digest: "a".repeat(64), snapshot_seal_origin: "publication", snapshot_sealed_at: "2026-07-28 16:35:00",
  duration_ms: 299000, message: "旧榜单已发布",
};
const active = {
  ...published, id: 42, status: "running", quote_date: "2026-07-29",
  processed_count: 1, success_count: 1, missing_count: 0, progress_pct: 33.33,
  coverage_pct: 33.33, updated_at: "2026-07-29 10:01:00", finished_at: null,
  snapshot_digest: null, snapshot_seal_origin: null, snapshot_sealed_at: null,
  duration_ms: null, message: "新扫描进行中",
};
let exportResolve;
let exportMode = "success";
const exportCalls = [];
const anchors = [];
const revoked = [];
document.createElement = () => {
  const anchor = { href: "", download: "", clicked: false, click() { this.clicked = true; } };
  anchors.push(anchor);
  return anchor;
};
globalThis.URL = {
  createObjectURL(blob) { assert.equal(blob instanceof Blob, true); return `blob:export-${anchors.length}`; },
  revokeObjectURL(url) { revoked.push(url); },
};
const controller = createMarketScanController({
  root: document,
  now: new Date(2026, 6, 17, 16, 30),
  pollIntervalMs: 60000,
  async fetcher(url) {
    if (String(url).startsWith("/api/market-scans/polling-identity?")) {
      return marketScanPollingIdentity(active, published);
    }
    if (url === "/api/market-scans/latest") return active;
    if (String(url).startsWith("/api/market-scans/latest-published?mode=")) return published;
    if (String(url).startsWith("/api/market-scans?")) {
      return { items: [published], total: 1, page: 1, page_size: 100, page_count: 1 };
    }
    if (String(url).startsWith("/api/market-scans/41/results?")) {
      return { run: published, total: 0, page: 1, page_size: 100, page_count: 0, items: [] };
    }
    throw new Error(`unexpected request: ${url}`);
  },
  async exportFetcher(url, init) {
    exportCalls.push({ url: String(url), init });
    if (exportMode === "error") {
      return { ok: false, status: 422, async json() { return { detail: "当前筛选条件无法导出" }; } };
    }
    return await new Promise((resolve) => { exportResolve = resolve; });
  },
});

assert.equal(element("marketScanExport").disabled, true);
assert.equal(await controller.exportResults(), null);
await controller.activate();
assert.equal(controller.state.run.id, 42);
assert.equal(controller.state.publishedRun.id, 41);
assert.equal(element("marketScanExport").disabled, false, "active scan hid the old published export");

element("marketScanStatus").value = "all";
element("marketScanMarket").value = "BJ";
element("marketScanIndustry").value = "专用 设备";
element("marketScanSt").value = "false";
element("marketScanNew").value = "true";
element("marketScanQuality").value = "85";
element("marketScanKeyword").value = "北交 样本";
element("marketScanSort").value = "score";
element("marketScanOrder").value = "desc";
const exporting = controller.exportResults();
await Promise.resolve();
assert.equal(controller.state.exportBusy, true);
assert.equal(controller.state.actionBusy, false);
assert.equal(element("workspace-panel-market-scan")["aria-busy"], "false");
assert.equal(element("marketScanExport").disabled, true);
assert.equal(element("marketScanExport")["aria-busy"], "true");
assert.equal(element("marketScanExport").textContent, "正在导出...");
assert.equal(await controller.exportResults(), null, "duplicate export was not rejected");
assert.equal(exportCalls.length, 1);
const requestUrl = new globalThis.URLSearchParams(exportCalls[0].url.split("?", 2)[1]);
assert.equal(exportCalls[0].url.startsWith("/api/market-scans/41/export.xlsx?"), true);
assert.equal(requestUrl.has("page"), false);
assert.equal(requestUrl.has("page_size"), false);
assert.deepEqual(Object.fromEntries(requestUrl), {
  status: "all", market: "BJ", industry: "专用 设备", is_st: "false", is_new: "true",
  min_data_quality_score: "85", keyword: "北交 样本", sort: "score", order: "desc",
});
assert.equal(exportCalls[0].init.headers.Accept, "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet");
assert.equal(exportCalls[0].init.signal instanceof AbortSignal, true);
exportResolve({
  ok: true,
  headers: { get(name) {
    if (name === "content-type") return "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet";
    return name === "content-disposition" ? "attachment; filename*=UTF-8''%E5%85%A8%E5%B8%82%E5%9C%BA%E6%A6%9C%E5%8D%95.xlsx" : null;
  } },
  async blob() { return new Blob(["xlsx"]); },
});
assert.equal(await exporting, "全市场榜单.xlsx");
assert.equal(anchors[0].clicked, true);
assert.equal(anchors[0].download, "全市场榜单.xlsx");
assert.equal(revoked[0], anchors[0].href);
assert.equal(controller.state.exportBusy, false);
assert.equal(element("marketScanExport").disabled, false);
assert.equal(element("marketScanExport")["aria-busy"], "false");
assert.equal(element("marketScanExport").textContent, "导出 Excel");
assert.match(element("marketScanAnnouncement").textContent, /Excel 榜单已导出/);

exportMode = "error";
assert.equal(await controller.exportResults(), null);
assert.match(element("marketScanAnnouncement").textContent, /当前筛选条件无法导出/);
assert.equal(element("marketScanExport").disabled, false);
assert.equal(anchors.length, 1, "error response created a download");
controller.deactivate();
'''
    )


def test_market_scan_controller_loads_terminal_snapshot_and_tracks_active_run() -> None:
    _run_node_script(
        r'''
import assert from "node:assert/strict";
import { installAppDom, marketScanPollingIdentity } from "./tests/frontend_app_flow_helpers.mjs";
import { createMarketScanController } from "./static/js/market-scan.js";

const { element } = installAppDom({ canvasContext: null });
const calls = [];
const terminal = {
  id: 9,
  status: "degraded",
  trigger: "manual",
  mode: "official",
  rule_version: "full-market-score-v1",
  as_of: "2026-07-17 16:30:00",
  data_date: "2026-07-17",
  quote_date: "2026-07-17",
  scope: "沪市 + 深市 + 北交所当前上市A股",
  total_count: 3,
  excluded_count: 1,
  processed_count: 3,
  success_count: 2,
  missing_count: 1,
  skipped_count: 0,
  retry_count: 0,
  progress_pct: 100,
  coverage_pct: 66.67,
  created_at: "2026-07-17 16:30:00",
  updated_at: "2026-07-17 16:45:30",
  message: "全市场扫描降级完成",
  finished_at: "2026-07-17 16:45:30",
  snapshot_digest: "a".repeat(64), snapshot_seal_origin: "publication", snapshot_sealed_at: "2026-07-17 16:45:30",
};
const resultPage = {
  run: terminal,
  page: 1,
  page_size: 100,
  page_count: 1,
  total: 1,
  items: [{
    run_id: 9,
    rank: 1,
    symbol: "920066.BJ",
    code: "920066",
    market: "BJ",
    name: "科拜尔",
    industry: "专用设备",
    status: "success",
    is_st: false,
    is_new: false,
    score: 88,
    raw_score: 88.1,
    trend_score: 72,
    leader_score: 80,
    change_pct: 1.25,
    turnover_rate: 2.5,
    amount: 125000000,
    data_quality_score: 91,
    price: 12.3,
    data_date: "2026-07-17",
    quote_timestamp: "2026-07-17 15:00:00",
    quote_observed_at: "2026-07-17T07:00:00Z",
    quote_source: "fixture",
    kline_source: "fixture",
    adjustment_mode: "qfq",
    reason: null,
    error: null,
    tags: ["趋势向上"],
    metrics: {},
    updated_at: "2026-07-17 16:45:30",
  }],
};
let latestRun = terminal;
let latestPublished = terminal;
let delayNextResult = false;
let resolveDelayedResult;
const controller = createMarketScanController({
  root: document,
  now: new Date(2026, 6, 17, 16, 30),
  pollIntervalMs: 60000,
  async fetcher(url) {
    calls.push(String(url));
    if (String(url).startsWith("/api/market-scans/polling-identity?")) {
      return marketScanPollingIdentity(latestRun, latestPublished);
    }
    if (url === "/api/market-scans/latest") return latestRun;
    if (String(url).startsWith("/api/market-scans/latest-published?mode=")) return latestPublished;
    if (String(url).startsWith("/api/market-scans?")) {
      return { items: [terminal], total: 1, page: 1, page_size: 100, page_count: 1 };
    }
    if (String(url).includes("/results?")) {
      if (delayNextResult) {
        delayNextResult = false;
        return await new Promise((resolve) => { resolveDelayedResult = resolve; });
      }
      return resultPage;
    }
    throw new Error(`unexpected request: ${url}`);
  },
});

await controller.activate();
assert.deepEqual({
  latest: calls.filter((url) => url === "/api/market-scans/latest").length,
  history: calls.filter((url) => url.startsWith("/api/market-scans?")).length,
  results: calls.filter((url) => url.startsWith("/api/market-scans/9/results?")).length,
  }, { latest: 1, history: 1, results: 1 });
assert.equal(element("marketScanHeadline").textContent, "全市场扫描降级完成");
assert.equal(element("marketScanProgressText").textContent, "3/3 · 100.0%");
assert.equal(element("marketScanProgressBar").textContent, "100.0%");
assert.equal(element("marketScanProgressBar")["aria-valuenow"], "100.00");
assert.equal(element("marketScanProgressBar")["aria-valuetext"], "降级完成，3/3 · 100.0%");
assert.equal(element("marketScanModeSummary").textContent, "盘后正式");
assert.equal(element("marketScanQuoteDate").textContent, "2026-07-17");
assert.equal(element("marketScanDataDate").textContent, "2026-07-17");
assert.equal(element("marketScanTotal").textContent, "3（排除 1）");
assert.equal(element("marketScanCoverage").textContent, "66.7%");
assert.equal(element("marketScanFinishedAt").textContent, "2026-07-17 16:45");
assert.equal(element("marketScanRule").textContent, "v1");
assert.equal(element("marketScanRule").title, "full-market-score-v1");
assert.equal(element("marketScanRule")["aria-label"], "规则版本 full-market-score-v1");
assert.equal(element("marketScanRows").innerHTML.includes("920066.BJ"), true);
assert.equal(element("marketScanTableWrap").hidden, false);
assert.equal(element("marketScanAnnouncement").textContent, "盘后正式榜单加载完成，第 1/1 页，本页 1 条，共 1 条。");
assert.equal(element("marketScanRetry").hidden, false);
assert.equal(element("marketScanProbabilityResearch").dataset.marketScanRunId, "9");
assert.equal(element("marketScanProbabilityResearch")["aria-busy"], "false");

delayNextResult = true;
const staleResultRead = controller.loadResults();
while (!resolveDelayedResult) await Promise.resolve();
assert.equal(element("marketScanProbabilityResearch")["aria-busy"], "true");
latestRun = {
  ...terminal, id: 10, status: "failed",
  snapshot_digest: null, snapshot_seal_origin: null, snapshot_sealed_at: null,
  message: "未发布终态覆盖读取中的旧榜单",
};
latestPublished = null;
const replacementRead = controller.loadLatest();
assert.equal(element("marketScanProbabilityResearch")["aria-busy"], "true");
resolveDelayedResult(resultPage);
await Promise.all([staleResultRead, replacementRead]);
assert.equal(element("marketScanProbabilityResearch")["aria-busy"], "false");
assert.equal(element("marketScanProbabilityStatus").textContent, "批次未发布·未进入研究归档");

latestRun = {
  ...terminal,
  id: 11,
  status: "running",
  total_count: 10,
  processed_count: 1,
  success_count: 1,
  missing_count: 0,
  progress_pct: 10,
  coverage_pct: 10,
  finished_at: null,
  snapshot_digest: null, snapshot_seal_origin: null, snapshot_sealed_at: null,
  rule_version: "full-market-scan-v3:085ad66526be0b28",
  message: "新扫描运行中",
};
latestPublished = terminal;
controller.deactivate();
await controller.activate();
assert.equal(controller.state.run.id, 11);
assert.equal(controller.state.run.status, "running");
assert.equal(element("marketScanProgressBar").textContent, "10.0%");
assert.equal(element("marketScanProgressBar")["aria-valuenow"], "10.00");
assert.equal(element("marketScanRule").textContent, "v1");
assert.equal(element("marketScanRule").title, "full-market-score-v1");
assert.match(element("marketScanTaskContext").textContent, /#11.*扫描中/);
assert.equal(controller.state.publishedRun.id, 9);
assert.equal(element("marketScanTableWrap").hidden, false);
assert.equal(element("marketScanRows").innerHTML.includes("920066.BJ"), true);
const requestCounts = {
  latest: calls.filter((url) => url === "/api/market-scans/latest").length,
  published: calls.filter((url) => url.startsWith("/api/market-scans/latest-published?mode=")).length,
  history: calls.filter((url) => url.startsWith("/api/market-scans?")).length,
};
  if (JSON.stringify(requestCounts) !== JSON.stringify({ latest: 3, published: 3, history: 2 })) {
  throw new Error(`unexpected request counts ${JSON.stringify(requestCounts)}`);
}

for (const [id, status, message] of [
  [11, "failed", "新扫描失败"],
  [12, "cancelled", "新扫描已取消"],
]) {
  latestRun = { ...terminal, id, status, message, snapshot_digest: null, snapshot_seal_origin: null, snapshot_sealed_at: null };
  controller.deactivate();
  await controller.activate();
  assert.equal(controller.state.run.status, status);
  assert.equal(controller.state.publishedRun.id, 9);
  assert.equal(element("marketScanHeadline").textContent, message);
  assert.equal(element("marketScanRows").innerHTML.includes("920066.BJ"), true);
  assert.equal(element("marketScanTableWrap").hidden, false);
  assert.equal(element("marketScanRetry").hidden, false);
  assert.equal(element("marketScanProbabilityResearch").dataset.marketScanRunId, "9");
  assert.equal(element("marketScanProbabilityResearch")["aria-busy"], "false");
}

const activeCalls = [];
const activeController = createMarketScanController({
  root: document,
  pollIntervalMs: 60000,
  async fetcher(url) {
    activeCalls.push(String(url));
    if (url === "/api/market-scans/latest") return null;
    if (url === "/api/market-scans") return {
      accepted: true,
      deduplicated: false,
      run: { ...terminal, id: 10, status: "running", processed_count: 1, success_count: 1, missing_count: 0, progress_pct: 33.33, coverage_pct: 33.33, finished_at: null, snapshot_digest: null, snapshot_seal_origin: null, snapshot_sealed_at: null, message: null },
    };
    throw new Error(`unexpected request: ${url}`);
  },
});
await activeController.start();
assert.equal(activeController.state.run.status, "running");
assert.equal(element("marketScanStart").disabled, true);
assert.equal(element("marketScanCancel").hidden, false);
assert.equal(element("marketScanResultState").textContent.includes("盘后正式榜单"), true);
assert.equal(element("marketScanProbabilityResearch")["aria-busy"], "true");
assert.equal(element("marketScanProbabilityStatus").textContent, "正在读取证据");
assert.equal(element("marketScanProbabilityMin").disabled, true);
assert.equal(activeCalls.includes("/api/market-scans"), true);
activeController.deactivate();

for (const status of ["queued", "running"]) {
  let activeResultCalls = 0;
  const activeRun = {
    ...terminal, id: status === "queued" ? 18 : 19, status,
    processed_count: 0, success_count: 0, missing_count: 0, progress_pct: 0, coverage_pct: 0,
    finished_at: null, snapshot_digest: null, snapshot_seal_origin: null, snapshot_sealed_at: null,
    message: `${status} test`,
  };
  const freshActiveController = createMarketScanController({
    root: document,
    pollIntervalMs: 60000,
    async fetcher(url) {
      if (String(url).startsWith("/api/market-scans/polling-identity?")) {
        return marketScanPollingIdentity(activeRun, null);
      }
      if (url === "/api/market-scans/latest") return activeRun;
      if (String(url).startsWith("/api/market-scans/latest-published?mode=")) return null;
      if (String(url).startsWith("/api/market-scans?")) {
        return { items: [], total: 0, page: 1, page_size: 100, page_count: 0 };
      }
      if (String(url).includes("/results?")) activeResultCalls += 1;
      throw new Error(`unexpected request: ${url}`);
    },
  });
  await freshActiveController.activate();
  assert.equal(activeResultCalls, 0);
  assert.equal(element("marketScanProbabilityResearch")["aria-busy"], "true");
  assert.equal(element("marketScanProbabilityStatus").textContent, "正在读取证据");
  assert.equal(element("marketScanProbabilityMin").disabled, true);
  freshActiveController.deactivate();
}
controller.deactivate();

let resolveOldLatest;
const oldLatest = new Promise((resolve) => { resolveOldLatest = resolve; });
const oldIdentityRun = { ...terminal, id: 21, status: "success", message: "旧的最近扫描" };
const raceController = createMarketScanController({
  root: document,
  pollIntervalMs: 60000,
  async fetcher(url) {
    if (String(url).startsWith("/api/market-scans/polling-identity?")) {
      return marketScanPollingIdentity(oldIdentityRun, oldIdentityRun);
    }
    if (url === "/api/market-scans/latest") return oldLatest;
    if (url === "/api/market-scans") return {
      accepted: true,
      deduplicated: false,
      run: { ...terminal, id: 22, status: "running", snapshot_digest: null, snapshot_seal_origin: null, snapshot_sealed_at: null, message: "用户新建扫描" },
    };
    throw new Error(`unexpected request: ${url}`);
  },
});
const activation = raceController.activate();
await Promise.resolve();
await raceController.start();
resolveOldLatest(oldIdentityRun);
await activation;
assert.equal(raceController.state.run.id, 22);
assert.equal(raceController.state.run.message, "用户新建扫描");
raceController.deactivate();

for (const [offset, status] of ["failed", "cancelled", "interrupted"].entries()) {
  let unpublishedResultCalls = 0;
  const unpublishedRun = {
    ...terminal, id: 23 + offset, status,
    snapshot_digest: null, snapshot_seal_origin: null, snapshot_sealed_at: null,
    message: `${status} unpublished`,
  };
  const unpublishedController = createMarketScanController({
    root: document,
    async fetcher(url) {
      if (String(url).startsWith("/api/market-scans/polling-identity?")) {
        return marketScanPollingIdentity(unpublishedRun, null);
      }
      if (url === "/api/market-scans/latest") return unpublishedRun;
      if (String(url).startsWith("/api/market-scans/latest-published?mode=")) return null;
      if (String(url).startsWith("/api/market-scans?")) {
        return { items: [], total: 0, page: 1, page_size: 100, page_count: 0 };
      }
      if (String(url).includes("/results?")) unpublishedResultCalls += 1;
      throw new Error(`unexpected request: ${url}`);
    },
  });
  await unpublishedController.activate();
  assert.equal(unpublishedResultCalls, 0);
  assert.equal(element("marketScanResultState").textContent.includes("未发布盘后正式榜单"), true);
  assert.equal(element("marketScanProbabilityResearch")["aria-busy"], "false");
  assert.equal(element("marketScanProbabilityStatus").textContent, "批次未发布·未进入研究归档");
  assert.equal(element("marketScanProbabilityBaseRate").textContent, "--");
  assert.equal(element("marketScanProbabilityEvidence").textContent, "--");
  assert.equal(element("marketScanProbabilityMin").disabled, true);
  assert.equal(element("marketScanProbabilityMin").value, "");
  unpublishedController.deactivate();
}

const noRunController = createMarketScanController({
  root: document,
  async fetcher(url) {
    if (String(url).startsWith("/api/market-scans/polling-identity?")) {
      return marketScanPollingIdentity(null, null);
    }
    if (url === "/api/market-scans/latest") return null;
    if (String(url).startsWith("/api/market-scans/latest-published?mode=")) return null;
    if (String(url).startsWith("/api/market-scans?")) {
      return { items: [], total: 0, page: 1, page_size: 100, page_count: 0 };
    }
    throw new Error(`unexpected request: ${url}`);
  },
});
await noRunController.activate();
assert.equal(element("marketScanProbabilityResearch")["aria-busy"], "false");
assert.equal(element("marketScanProbabilityStatus").textContent, "尚未生成研究证据");
noRunController.deactivate();
'''
    )


def test_market_scan_controller_discovers_external_run_and_retains_published_snapshot() -> None:
    _run_node_script(
        r'''
import assert from "node:assert/strict";
import { installAppDom, marketScanPollingIdentity } from "./tests/frontend_app_flow_helpers.mjs";
import { createMarketScanController } from "./static/js/market-scan.js";

const { element } = installAppDom({ canvasContext: null });
const timers = installFakeTimers();
const run = (id, status, message) => ({
  id,
  status,
  trigger: "manual",
  mode: "official",
  rule_version: "full-market-score-v1",
  as_of: "2026-07-17 16:30:00",
  data_date: "2026-07-17",
  quote_date: "2026-07-17",
  scope: "沪市 + 深市 + 北交所当前上市A股",
  total_count: 1,
  excluded_count: 0,
  processed_count: status === "running" ? 0 : 1,
  success_count: status === "running" ? 0 : 1,
  missing_count: 0,
  skipped_count: 0,
  retry_count: 0,
  progress_pct: status === "running" ? 0 : 100,
  coverage_pct: status === "running" ? 0 : 100,
  created_at: "2026-07-17 16:30:00",
  updated_at: "2026-07-17 16:31:00",
  finished_at: status === "running" ? null : "2026-07-17 16:31:00",
  snapshot_digest: status === "success" ? "a".repeat(64) : null,
  snapshot_seal_origin: status === "success" ? "publication" : null,
  snapshot_sealed_at: status === "success" ? "2026-07-17 16:31:00" : null,
  message,
});
const firstActive = run(9, "running", "首轮扫描中");
const firstTerminal = run(9, "success", "首轮扫描完成");
const externalActive = run(11, "running", "调度扫描中");
let latestCalls = 0;
let publishedRun = null;
let identityLatest = firstActive;
const calls = [];
const controller = createMarketScanController({
  root: document,
  now: new Date(2026, 6, 17, 16, 30),
  pollIntervalMs: 5,
  idlePollIntervalMs: 7,
  async fetcher(url) {
    calls.push(String(url));
    if (String(url).startsWith("/api/market-scans/polling-identity?")) {
      return marketScanPollingIdentity(identityLatest, publishedRun);
    }
    if (url === "/api/market-scans/latest") {
      latestCalls += 1;
      return identityLatest;
    }
    if (String(url).startsWith("/api/market-scans/latest-published?mode=")) return publishedRun;
    if (String(url).startsWith("/api/market-scans?")) {
      return { items: publishedRun ? [publishedRun] : [], total: publishedRun ? 1 : 0, page: 1, page_size: 100, page_count: publishedRun ? 1 : 0 };
    }
    if (url === "/api/market-scans/9") {
      publishedRun = firstTerminal;
      identityLatest = externalActive;
      return firstTerminal;
    }
    if (url === "/api/market-scans/11") return externalActive;
    if (String(url).includes("/api/market-scans/9/results?")) {
      return {
        run: firstTerminal,
        page: 1,
        page_size: 100,
        page_count: 1,
        total: 1,
        items: [{
          run_id: 9, rank: 1, symbol: "600519.SH", code: "600519", market: "SH", name: "旧榜单股票",
          status: "success", score: 90, raw_score: 90.1, trend_score: 90, leader_score: 90,
          data_quality_score: 90, price: 100, is_st: false, is_new: false, data_date: "2026-07-17",
          quote_timestamp: "2026-07-17 15:00:00", quote_observed_at: "2026-07-17T07:00:00Z",
          quote_source: "fixture", kline_source: "fixture", adjustment_mode: "qfq",
          reason: null, error: null, tags: [], metrics: {}, updated_at: "2026-07-17 16:31:00",
        }],
      };
    }
    throw new Error(`unexpected request: ${url}`);
  },
});

await controller.activate();
await timers.fireNext();
assert.equal(controller.state.run.status, "success");
assert.equal(element("marketScanRows").innerHTML.includes("旧榜单股票"), true);
assert.equal(element("marketScanTableWrap").hidden, false);

controller.state.page = 5;
controller.state.pageCount = 8;
await timers.fireNext();
assert.equal(latestCalls, 2);
assert.equal(controller.state.run.id, 11);
assert.equal(controller.state.run.status, "running");
assert.equal(controller.state.publishedRun.id, 9);
assert.equal(controller.state.page, 1);
assert.equal(controller.state.pageCount, 1);
assert.equal(element("marketScanRows").innerHTML.includes("旧榜单股票"), true);
assert.equal(element("marketScanTableWrap").hidden, false);
assert.equal(calls.includes("/api/market-scans/9/results?page=1&page_size=100&status=success&sort=rank&order=asc"), true);
controller.deactivate();
assert.equal(timers.size(), 0);

function installFakeTimers() {
  let nextId = 0;
  const scheduled = new Map();
  globalThis.setTimeout = (callback, delay = 0) => {
    const id = ++nextId;
    scheduled.set(id, { callback, delay });
    return id;
  };
  globalThis.clearTimeout = (id) => scheduled.delete(id);
  return {
    size: () => scheduled.size,
    async fireNext() {
      const entry = [...scheduled.entries()].sort((left, right) => left[1].delay - right[1].delay || left[0] - right[0])[0];
      assert.ok(entry, "expected a scheduled refresh");
      scheduled.delete(entry[0]);
      entry[1].callback();
      for (let index = 0; index < 20; index += 1) await Promise.resolve();
    },
  };
}
'''
    )


def test_market_scan_controller_retries_results_and_reconciles_uncertain_mutation() -> None:
    _run_node_script(
        r'''
import assert from "node:assert/strict";
import { installAppDom, marketScanPollingIdentity } from "./tests/frontend_app_flow_helpers.mjs";
import { createMarketScanController } from "./static/js/market-scan.js";

const { element } = installAppDom({ canvasContext: null });
const timers = installFakeTimers();
const terminal = {
  id: 20,
  status: "success",
  trigger: "manual",
  mode: "official",
  rule_version: "full-market-score-v1",
  as_of: "2026-07-17 16:30:00",
  data_date: "2026-07-17",
  quote_date: "2026-07-17",
  scope: "沪市 + 深市 + 北交所当前上市A股",
  total_count: 1,
  excluded_count: 0,
  processed_count: 1,
  success_count: 1,
  missing_count: 0,
  skipped_count: 0,
  retry_count: 0,
  progress_pct: 100,
  coverage_pct: 100,
  created_at: "2026-07-17 16:30:00",
  updated_at: "2026-07-17 16:31:00",
  finished_at: "2026-07-17 16:31:00",
  snapshot_digest: "a".repeat(64), snapshot_seal_origin: "publication", snapshot_sealed_at: "2026-07-17 16:31:00",
  message: "扫描完成",
};
let resultCalls = 0;
const retryController = createMarketScanController({
  root: document,
  now: new Date(2026, 6, 17, 16, 30),
  idlePollIntervalMs: 1000,
    resultRetryIntervalMs: 5,
    async fetcher(url) {
      if (String(url).startsWith("/api/market-scans/polling-identity?")) {
        return marketScanPollingIdentity(terminal, terminal);
          }
          if (url === "/api/market-scans/latest") return terminal;
          if (String(url).startsWith("/api/market-scans/latest-published?mode=")) return terminal;
        if (String(url).includes("/results?")) {
      resultCalls += 1;
      if (resultCalls === 1) throw new Error("临时读取失败");
      return {
        run: terminal,
        page: 1,
        page_size: 100,
        page_count: 1,
        total: 1,
        items: [{
          run_id: 20, rank: 1, symbol: "920066.BJ", code: "920066", market: "BJ", name: "北交样本",
          status: "success", score: 88, raw_score: 88.1, trend_score: 88, leader_score: 88,
          data_quality_score: 88, price: 10, is_st: false, is_new: false, data_date: "2026-07-17",
          quote_timestamp: "2026-07-17 15:00:00", quote_observed_at: "2026-07-17T07:00:00Z",
          quote_source: "fixture", kline_source: "fixture", adjustment_mode: "qfq",
          reason: null, error: null, tags: [], metrics: {}, updated_at: "2026-07-17 16:31:00",
        }],
      };
    }
    throw new Error(`unexpected request: ${url}`);
  },
});

await retryController.activate();
assert.equal(resultCalls, 1);
assert.equal(element("marketScanResultState").textContent.includes("榜单读取失败"), true);
assert.equal(element("marketScanProbabilityResearch")["aria-busy"], "false");
assert.equal(element("marketScanProbabilityStatus").textContent, "证据读取失败·等待重试");
assert.doesNotMatch(element("marketScanProbabilityStatus").textContent, /正在读取/);
assert.match(element("marketScanProbabilityLimitations").textContent, /读取失败.*自动重试/);
assert.equal(element("marketScanProbabilityBaseRate").textContent, "--");
assert.equal(element("marketScanProbabilityEvidence").textContent, "--");
assert.equal(element("marketScanProbabilityMin").disabled, true);
assert.equal(element("marketScanProbabilityMin").value, "");
await timers.fireNext();
assert.equal(resultCalls, 2);
assert.equal(element("marketScanTableWrap").hidden, false);
assert.equal(element("marketScanAnnouncement").textContent, "盘后正式榜单加载完成，第 1/1 页，本页 1 条，共 1 条。");
assert.equal(element("marketScanProbabilityResearch")["aria-busy"], "false");
assert.equal(element("marketScanProbabilityStatus").textContent, "尚未生成研究证据");
assert.doesNotMatch(element("marketScanProbabilityStatus").textContent, /读取失败|正在读取/);
retryController.deactivate();

let serverRun = null;
let latestCalls = 0;
const active = { ...terminal, id: 21, status: "running", processed_count: 0, success_count: 0, progress_pct: 0, coverage_pct: 0, finished_at: null, snapshot_digest: null, snapshot_seal_origin: null, snapshot_sealed_at: null, message: "服务端任务运行中" };
const mutationController = createMarketScanController({
  root: document,
  pollIntervalMs: 1000,
    idlePollIntervalMs: 1000,
    async fetcher(url) {
      if (String(url).startsWith("/api/market-scans/polling-identity?")) {
        return marketScanPollingIdentity(serverRun, null);
      }
      if (url === "/api/market-scans/latest") {
      latestCalls += 1;
        return serverRun;
      }
      if (String(url).startsWith("/api/market-scans/latest-published?mode=")) return null;
    if (url === "/api/market-scans") {
      serverRun = active;
      throw new Error("任务创建后响应丢失");
    }
    throw new Error(`unexpected request: ${url}`);
  },
});

await mutationController.activate();
await mutationController.start();
assert.equal(latestCalls, 2);
assert.equal(mutationController.state.run.id, 21);
assert.equal(mutationController.state.run.status, "running");
assert.equal(element("marketScanHeadline").textContent, "请求响应未确认，已从服务端恢复任务状态。");
assert.equal(element("marketScanStart").disabled, true);
mutationController.deactivate();

function installFakeTimers() {
  let nextId = 0;
  const scheduled = new Map();
  globalThis.setTimeout = (callback, delay = 0) => {
    const id = ++nextId;
    scheduled.set(id, { callback, delay });
    return id;
  };
  globalThis.clearTimeout = (id) => scheduled.delete(id);
  return {
    async fireNext() {
      const entry = [...scheduled.entries()].sort((left, right) => left[1].delay - right[1].delay || left[0] - right[0])[0];
      assert.ok(entry, "expected a scheduled retry");
      scheduled.delete(entry[0]);
      entry[1].callback();
      for (let index = 0; index < 20; index += 1) await Promise.resolve();
    },
  };
}
'''
    )


def test_market_scan_controller_rejects_malformed_success_payloads() -> None:
    _run_node_script(
        r'''
import assert from "node:assert/strict";
import { installAppDom, marketScanPollingIdentity } from "./tests/frontend_app_flow_helpers.mjs";
import {
  createMarketScanController,
  validateMarketScanRun,
  validateResultPage,
} from "./static/js/market-scan.js";

const terminal = scanRun(70, "success");
assert.throws(
  () => validateMarketScanRun({ id: 70, status: "success" }),
  /trigger/
);
assert.throws(() => validateMarketScanRun({ ...terminal, snapshot_digest: null }), /snapshot_digest/);
assert.throws(
  () => validateMarketScanRun({ ...terminal, snapshot_sealed_at: "2026-07-17 16:30:59" }),
  /snapshot_sealed_at/,
);
assert.throws(
  () => validateMarketScanRun({ ...terminal, updated_at: "2026-07-17 16:30:59" }),
  /updated_at.*finished_at/,
);
assert.throws(
  () => validateMarketScanRun({
    ...terminal,
    updated_at: "2026-07-17 16:32:00",
    snapshot_sealed_at: "2026-07-17 16:31:00",
  }),
  /snapshot_sealed_at.*updated_at/,
);
assert.throws(
  () => validateMarketScanRun({ ...scanRun(71, "running"), snapshot_digest: "a".repeat(64) }),
  /未发布批次/,
);
const currentV6 = {
  ...terminal,
  rule_version: `full-market-scan-v6:${"b".repeat(64)}`,
  scope: "沪市 + 深市 + 北交所当前上市A股",
  market_progress: [{ market: "SH", total_count: 1, processed_count: 1, success_count: 1, missing_count: 0, skipped_count: 0, coverage_pct: 100 }],
};
assert.throws(() => validateMarketScanRun(currentV6), /完整覆盖 SH\/SZ\/BJ/);
assert.equal(validateMarketScanRun({
  ...currentV6,
  market_progress: [
    currentV6.market_progress[0],
    { market: "SZ", total_count: 0, processed_count: 0, success_count: 0, missing_count: 0, skipped_count: 0, coverage_pct: 0 },
    { market: "BJ", total_count: 0, processed_count: 0, success_count: 0, missing_count: 0, skipped_count: 0, coverage_pct: 0 },
  ],
}).id, 70);
assert.throws(
  () => validateResultPage({ run: terminal, total: 0, page: 1, page_size: 100, page_count: 0 }, 70),
  /items 必须是数组/
);
const malformedLegacyItem = {
  run_id: 70, rank: 1, symbol: "920066.BJ", code: "920066", market: "BJ", name: "北交样本",
  updated_at: "2026-07-17 16:31:00", status: "success", is_st: false, is_new: false,
  industry: null, list_date: null, metadata_source: null, reason: null, error: null, data_date: null,
  quote_timestamp: null, quote_source: null, kline_source: null, adjustment_mode: null,
  score: 80, trend_score: 80, leader_score: 80, data_quality_score: 80,
  price: 10, change_pct: 1, turnover_rate: 2, volume_ratio: 1, amount: 1000000,
  tags: [], metrics: {},
};
const resultPage = (item) => ({ run: terminal, total: 1, page: 1, page_size: 100, page_count: 1, items: [item] });
assert.throws(
  () => validateResultPage(resultPage(malformedLegacyItem), 70),
  /raw_score|data_date|success/
);
const resultItem = {
  ...malformedLegacyItem,
  raw_score: 80.1,
  data_date: "2026-07-17",
  quote_timestamp: "2026-07-17 15:00:00",
  quote_observed_at: "2026-07-17T07:00:00Z",
  quote_source: "fixture",
  kline_source: "fixture",
  adjustment_mode: "qfq",
};
const validatedPage = validateResultPage(resultPage(resultItem), 70);
assert.equal(validatedPage.items[0].symbol, "920066.BJ");
assert.equal(validatedPage.probability_research.horizons["5"].status, "not_generated");
assert.equal(validatedPage.items[0].upside_probabilities["5"].probability, null);
assert.throws(
  () => validateResultPage(resultPage({ ...resultItem, updated_at: "2099-01-01 00:00:00" }), 70),
  /updated_at.*批次 updated_at/,
);
for (const invalid of [
  { ...resultItem, symbol: "920066.SH" },
  { ...resultItem, market: "SH" },
  { ...resultItem, code: "920067" },
]) {
  assert.throws(() => validateResultPage(resultPage(invalid), 70), /symbol/);
}
for (const invalid of [
  { ...resultItem, status: "missing", rank: 1, error: "行情缺失" },
  { ...resultItem, status: "skipped", rank: null, score: null, raw_score: null, trend_score: null, leader_score: null, data_quality_score: null, reason: null, error: null },
  { ...resultItem, price: Number.POSITIVE_INFINITY },
  { ...resultItem, adjustment_mode: "none" },
]) {
  assert.throws(() => validateResultPage(resultPage(invalid), 70));
}
assert.throws(
  () => validateResultPage({ ...resultPage(resultItem), total: 2, page_size: 2, page_count: 1 }, 70),
  /当前分页/
);

const { element } = installAppDom({ canvasContext: null });
const controller = createMarketScanController({
  root: document,
  now: new Date(2026, 6, 17, 16, 30),
      resultRetryIntervalMs: 60000,
      async fetcher(url) {
        if (String(url).startsWith("/api/market-scans/polling-identity?")) {
          return marketScanPollingIdentity(terminal, terminal);
            }
            if (url === "/api/market-scans/latest") return terminal;
            if (String(url).startsWith("/api/market-scans/latest-published?mode=")) return terminal;
        if (String(url).includes("/results?")) {
      return { run: terminal, total: 0, page: 1, page_size: 100, page_count: 0 };
    }
    throw new Error(`unexpected request: ${url}`);
  },
});

await controller.activate();
assert.equal(element("marketScanTableWrap").hidden, true);
assert.equal(element("marketScanRows").innerHTML, "");
assert.match(element("marketScanResultState").textContent, /响应格式异常.*items 必须是数组/);
assert.match(element("marketScanAnnouncement").textContent, /榜单读取失败.*items 必须是数组/);
assert.notEqual(controller.state.pollTimer, null);
controller.deactivate();

function scanRun(id, status) {
  const active = status === "running";
  return {
    id,
    status,
    trigger: "manual",
    mode: "official",
    rule_version: "full-market-score-v1",
    as_of: "2026-07-17 16:30:00",
    data_date: "2026-07-17",
    quote_date: "2026-07-17",
        scope: "沪市 + 深市 + 北交所当前上市A股",
    total_count: 1,
    excluded_count: 0,
    processed_count: active ? 0 : 1,
    success_count: active ? 0 : 1,
    missing_count: 0,
    skipped_count: 0,
    retry_count: 0,
    progress_pct: active ? 0 : 100,
    coverage_pct: active ? 0 : 100,
    created_at: "2026-07-17 16:30:00",
    updated_at: "2026-07-17 16:31:00",
    finished_at: active ? null : "2026-07-17 16:31:00",
    snapshot_digest: active ? null : "a".repeat(64), snapshot_seal_origin: active ? null : "publication",
    snapshot_sealed_at: active ? null : "2026-07-17 16:31:00",
    message: active ? "扫描中" : "扫描完成",
  };
}
'''
    )


def test_market_scan_controller_recovers_missing_run_and_syncs_immediately_online() -> None:
    _run_node_script(
        r'''
import assert from "node:assert/strict";
import { installAppDom, marketScanPollingIdentity } from "./tests/frontend_app_flow_helpers.mjs";
import { createMarketScanController } from "./static/js/market-scan.js";

const { element } = installAppDom({ canvasContext: null });
const timers = installFakeTimers();
const listeners = {};
const connectivityTarget = {
  addEventListener(name, handler) { listeners[name] = handler; },
};
let latestCalls = 0;
let identityRun = scanRun(70);
const calls = [];
const controller = createMarketScanController({
  root: document,
  connectivityTarget,
  pollIntervalMs: 5,
  async fetcher(url) {
      calls.push(String(url));
      if (String(url).startsWith("/api/market-scans/polling-identity?")) {
        return marketScanPollingIdentity(identityRun, null);
      }
      if (url === "/api/market-scans/latest") {
        latestCalls += 1;
        return identityRun;
    }
    if (String(url).startsWith("/api/market-scans/latest-published?mode=")) return null;
    if (String(url).startsWith("/api/market-scans?")) {
      return { items: [], total: 0, page: 1, page_size: 100, page_count: 0 };
    }
      if (url === "/api/market-scans/70") {
        identityRun = scanRun(71);
        const error = new Error("全市场扫描批次不存在：70");
      error.status = 404;
      throw error;
    }
    if (url === "/api/market-scans/71" || url === "/api/market-scans/72") return scanRun(Number(url.split("/").at(-1)));
    throw new Error(`unexpected request: ${url}`);
  },
});

await controller.activate();
controller.state.page = 6;
controller.state.pageCount = 9;
assert.equal(timers.size(), 1);
await timers.fireNext();
assert.equal(latestCalls, 2);
assert.equal(controller.state.run.id, 71);
assert.equal(controller.state.page, 1);
assert.equal(controller.state.pageCount, 0);
assert.equal(element("marketScanHeadline").textContent, "原扫描记录已失效，正在同步最近扫描。");
assert.equal(element("marketScanProgressBar")["aria-busy"], "true");
assert.equal(timers.size(), 1);

identityRun = scanRun(72);
listeners.online();
listeners.online();
await flushPromises();
assert.equal(latestCalls, 3);
assert.equal(controller.state.run.id, 72);
assert.equal(element("marketScanHeadline").textContent, "网络已恢复，正在同步最近扫描。");
assert.equal(timers.size(), 1);
assert.deepEqual(calls.filter((url) => (
  url === "/api/market-scans/latest" || /^\/api\/market-scans\/\d+$/.test(url)
)).slice(0, 4), [
  "/api/market-scans/latest",
  "/api/market-scans/70",
  "/api/market-scans/latest",
  "/api/market-scans/latest",
]);
controller.deactivate();
assert.equal(timers.size(), 0);

function scanRun(id) {
  return {
    id,
    status: "running",
    trigger: "manual",
    mode: "official",
    rule_version: "full-market-score-v1",
    as_of: "2026-07-17 16:30:00",
    data_date: "2026-07-17",
    quote_date: "2026-07-17",
    scope: "SH/SZ/BJ",
    total_count: 100,
    excluded_count: 0,
    processed_count: 10,
    success_count: 10,
    missing_count: 0,
    skipped_count: 0,
    retry_count: 0,
    progress_pct: 10,
    coverage_pct: 10,
    created_at: "2026-07-17 16:30:00",
    updated_at: "2026-07-17 16:31:00",
    finished_at: null,
    snapshot_digest: null, snapshot_seal_origin: null, snapshot_sealed_at: null,
    message: `扫描 ${id} 运行中`,
  };
}

function installFakeTimers() {
  let nextId = 0;
  const scheduled = new Map();
  globalThis.setTimeout = (callback, delay = 0) => {
    const id = ++nextId;
    scheduled.set(id, { callback, delay });
    return id;
  };
  globalThis.clearTimeout = (id) => scheduled.delete(id);
  return {
    size: () => scheduled.size,
    async fireNext() {
      const entry = [...scheduled.entries()][0];
      assert.ok(entry, "expected a scheduled poll");
      scheduled.delete(entry[0]);
      entry[1].callback();
      await flushPromises();
    },
  };
}

async function flushPromises() {
  for (let index = 0; index < 50; index += 1) await Promise.resolve();
}
'''
    )


def test_market_scan_controller_uses_one_bounded_exponential_backoff_timer() -> None:
    _run_node_script(
        r'''
import assert from "node:assert/strict";
import { installAppDom, marketScanPollingIdentity } from "./tests/frontend_app_flow_helpers.mjs";
import { createMarketScanController } from "./static/js/market-scan.js";

installAppDom({ canvasContext: null });
const timers = installFakeTimers();
let latestCalls = 0;
let runCalls = 0;
let identityRun = scanRun(80);
const controller = createMarketScanController({
  root: document,
  pollIntervalMs: 5,
  maxPollIntervalMs: 12,
    failureFallbackThreshold: 5,
    async fetcher(url) {
      if (String(url).startsWith("/api/market-scans/polling-identity?")) {
        return marketScanPollingIdentity(identityRun, null);
      }
      if (url === "/api/market-scans/latest") {
        latestCalls += 1;
        return identityRun;
      }
      if (String(url).startsWith("/api/market-scans/latest-published?mode=")) return null;
      if (url === "/api/market-scans/80") {
        runCalls += 1;
        if (runCalls === 5) identityRun = scanRun(81);
        throw new Error("临时网络失败");
    }
    if (url === "/api/market-scans/81") return scanRun(81);
    throw new Error(`unexpected request: ${url}`);
  },
});

await controller.activate();
assert.deepEqual(timers.delays(), [5]);
await timers.fireNext();
assert.deepEqual(timers.delays(), [5]);
await timers.fireNext();
assert.deepEqual(timers.delays(), [10]);
await timers.fireNext();
assert.deepEqual(timers.delays(), [12]);
await timers.fireNext();
assert.deepEqual(timers.delays(), [12]);
await timers.fireNext();
assert.equal(runCalls, 5);
assert.equal(latestCalls, 2);
assert.equal(controller.state.run.id, 81);
assert.equal(controller.state.consecutiveFailures, 0);
assert.deepEqual(timers.delays(), [5]);
controller.deactivate();
assert.deepEqual(timers.delays(), []);

function scanRun(id) {
  return {
    id,
    status: "running",
    trigger: "manual",
    mode: "official",
    rule_version: "full-market-score-v1",
    as_of: "2026-07-17 16:30:00",
    data_date: "2026-07-17",
    quote_date: "2026-07-17",
    scope: "SH/SZ/BJ",
    total_count: 100,
    excluded_count: 0,
    processed_count: 1,
    success_count: 1,
    missing_count: 0,
    skipped_count: 0,
    retry_count: 0,
    progress_pct: 1,
    coverage_pct: 1,
    created_at: "2026-07-17 16:30:00",
    updated_at: "2026-07-17 16:31:00",
    finished_at: null,
    snapshot_digest: null, snapshot_seal_origin: null, snapshot_sealed_at: null,
    message: "扫描运行中",
  };
}

function installFakeTimers() {
  let nextId = 0;
  const scheduled = new Map();
  globalThis.setTimeout = (callback, delay = 0) => {
    const id = ++nextId;
    scheduled.set(id, { callback, delay });
    return id;
  };
  globalThis.clearTimeout = (id) => scheduled.delete(id);
  return {
    delays: () => [...scheduled.values()].map((entry) => entry.delay),
    async fireNext() {
      assert.equal(scheduled.size, 1, "polling must keep exactly one timer");
      const entry = [...scheduled.entries()][0];
      scheduled.delete(entry[0]);
      entry[1].callback();
      for (let index = 0; index < 50; index += 1) await Promise.resolve();
      assert.equal(scheduled.size, 1, "polling must reschedule exactly one timer");
    },
  };
}
'''
    )


def test_market_scan_controller_cancels_deferred_reset_when_deactivated() -> None:
    _run_node_script(
        r'''
import assert from "node:assert/strict";
import { installAppDom, marketScanPollingIdentity } from "./tests/frontend_app_flow_helpers.mjs";
import { createMarketScanController } from "./static/js/market-scan.js";

const { element } = installAppDom({ canvasContext: null });
let nextId = 0;
const timers = new Map();
globalThis.setTimeout = (callback, delay = 0) => {
  const id = ++nextId;
  timers.set(id, { callback, delay });
  return id;
};
globalThis.clearTimeout = (id) => timers.delete(id);
const terminal = {
  id: 30, status: "success", trigger: "manual", mode: "official", rule_version: "v1",
    as_of: "2026-07-17 16:30:00", data_date: "2026-07-17", quote_date: "2026-07-17", scope: "沪市 + 深市 + 北交所当前上市A股",
  total_count: 0, excluded_count: 0, processed_count: 0, success_count: 0,
  missing_count: 0, skipped_count: 0, retry_count: 0, progress_pct: 100,
  coverage_pct: 0, created_at: "2026-07-17 16:30:00", updated_at: "2026-07-17 16:31:00",
  finished_at: "2026-07-17 16:31:00", message: "扫描完成",
  snapshot_digest: "a".repeat(64), snapshot_seal_origin: "publication", snapshot_sealed_at: "2026-07-17 16:31:00",
};
let resultCalls = 0;
const controller = createMarketScanController({
  root: document,
    now: new Date(2026, 6, 17, 16, 30),
    async fetcher(url) {
      if (String(url).startsWith("/api/market-scans/polling-identity?")) {
        return marketScanPollingIdentity(terminal, terminal);
          }
          if (url === "/api/market-scans/latest") return terminal;
          if (String(url).startsWith("/api/market-scans/latest-published?mode=")) return terminal;
        if (String(url).includes("/results?")) {
      resultCalls += 1;
      return { run: terminal, items: [], total: 0, page: 1, page_size: 100, page_count: 0 };
    }
    throw new Error(`unexpected request: ${url}`);
  },
});

await controller.activate();
assert.equal(resultCalls, 1);
element("marketScanFilters").listeners.reset();
assert.equal([...timers.values()].some((timer) => timer.delay === 0), true);
controller.deactivate();
assert.equal(timers.size, 0);
assert.equal(resultCalls, 1);
'''
    )


def test_market_scan_pagination_keeps_visible_content_and_stable_focus_while_loading() -> None:
    _run_node_script(
        r'''
import assert from "node:assert/strict";
import { installAppDom, marketScanPollingIdentity } from "./tests/frontend_app_flow_helpers.mjs";
import { createMarketScanController } from "./static/js/market-scan.js";

const { element } = installAppDom({ canvasContext: null });
for (const id of ["marketScanTableWrap", "marketScanMarket", "marketScanPrev", "marketScanNext"]) {
  element(id).focus = function focus() { document.activeElement = this; };
}
const terminal = {
  id: 40, status: "success", trigger: "manual", mode: "official", rule_version: "v1",
  as_of: "2026-07-17 16:30:00", data_date: "2026-07-17", quote_date: "2026-07-17", scope: "沪市 + 深市 + 北交所当前上市A股",
  total_count: 101, excluded_count: 0, processed_count: 101, success_count: 101,
  missing_count: 0, skipped_count: 0, retry_count: 0, progress_pct: 100,
  coverage_pct: 100, created_at: "2026-07-17 16:30:00", updated_at: "2026-07-17 16:31:00",
  finished_at: "2026-07-17 16:31:00", message: "扫描完成",
  snapshot_digest: "a".repeat(64), snapshot_seal_origin: "publication", snapshot_sealed_at: "2026-07-17 16:31:00",
};
const secondPage = deferred();
let resultCalls = 0;
const controller = createMarketScanController({
  root: document,
  now: new Date(2026, 6, 17, 16, 30),
  pollIntervalMs: 60000,
  idlePollIntervalMs: 60000,
  async fetcher(url) {
    if (String(url).startsWith("/api/market-scans/polling-identity?")) {
      return marketScanPollingIdentity(terminal, terminal);
        }
        if (url === "/api/market-scans/latest") return terminal;
        if (String(url).startsWith("/api/market-scans/latest-published?mode=")) return terminal;
        if (String(url).includes("/results?")) {
      resultCalls += 1;
      if (resultCalls === 1) return page(1, stock("600519.SH", 1));
      return secondPage.promise;
    }
    throw new Error(`unexpected request: ${url}`);
  },
});

await controller.activate();
assert.equal(element("marketScanPagination").hidden, false);
assert.equal(element("marketScanNext").disabled, false);
document.activeElement = element("marketScanNext");
controller.state.page = 2;
const loading = controller.loadResults();
while (resultCalls < 2) await Promise.resolve();
assert.equal(element("marketScanTableWrap").hidden, false, "page load hid the stable result region");
assert.equal(element("marketScanPagination").hidden, false, "page load hid pagination");
assert.equal(element("marketScanPagination")["aria-busy"], "true");
assert.equal(element("marketScanPrev").disabled, true);
assert.equal(element("marketScanNext").disabled, true);
assert.equal(document.activeElement, element("marketScanTableWrap"), "page load dropped focus");

secondPage.resolve(page(2, stock("920066.BJ", 101)));
await loading;
assert.equal(element("marketScanPagination")["aria-busy"], "false");
assert.equal(element("marketScanNext").disabled, true);
assert.equal(document.activeElement, element("marketScanTableWrap"), "terminal page dropped focus");
controller.deactivate();

function page(number, item) {
  const items = number === 1
    ? [item, ...Array.from({ length: 99 }, (_, index) => stock(`${String(600000 + index).padStart(6, "0")}.SH`, index + 2))]
    : [item];
  return { run: terminal, total: 101, page: number, page_size: 100, page_count: 2, items };
}
function stock(symbol, rank) {
  const [code, market] = symbol.split(".");
  return {
    run_id: 40, rank, symbol, code, market, name: `股票${code}`, updated_at: "2026-07-17 16:31:00",
    status: "success", is_st: false, is_new: false, tags: [], metrics: {},
    score: 80, raw_score: 80, trend_score: 80, leader_score: 80, data_quality_score: 80,
    price: 10, data_date: "2026-07-17", quote_timestamp: "2026-07-17 15:00:00",
    quote_observed_at: "2026-07-17T07:00:00Z", quote_source: "fixture",
    kline_source: "fixture", adjustment_mode: "qfq", reason: null, error: null,
  };
}
function deferred() {
  let resolve;
  const promise = new Promise((done) => { resolve = done; });
  return { promise, resolve };
}
'''
    )


def test_market_scan_mutations_own_reads_busy_state_focus_and_duplicate_submissions() -> None:
    _run_node_script(
        r'''
import assert from "node:assert/strict";
import { installAppDom, marketScanPollingIdentity } from "./tests/frontend_app_flow_helpers.mjs";
import { createMarketScanController } from "./static/js/market-scan.js";

const { element } = installAppDom({ canvasContext: null });
const scheduled = new Map();
let timerId = 0;
globalThis.setTimeout = (callback, delay = 0) => {
  const id = ++timerId;
  scheduled.set(id, { callback, delay });
  return id;
};
globalThis.clearTimeout = (id) => scheduled.delete(id);
for (const id of ["marketScanStart", "marketScanCancel", "marketScanRetry", "marketScanMarket"]) {
  element(id).focus = function focus() { document.activeElement = this; };
}

const initialLatest = deferred();
const staleLatest = deferred();
const startResponse = deferred();
const cancelResponse = deferred();
const staleResult = deferred();
const retryResponse = deferred();
const signals = { latest: [], results: [] };
const calls = { latest: 0, start: 0, cancel: 0, retry: 0, results: 0 };
const running = scanRun(1, "running", "新扫描运行中");
const cancelled = scanRun(1, "cancelled", "扫描已取消");
const degraded = scanRun(3, "degraded", "降级完成");
const retried = scanRun(4, "running", "重试扫描运行中");
let identityLatest = scanRun(99, "running", "旧任务读取中");
const identityPublished = null;
const controller = createMarketScanController({
  root: document,
  now: new Date(2026, 6, 17, 16, 30),
  pollIntervalMs: 60000,
  idlePollIntervalMs: 60000,
  async fetcher(url, options = {}) {
    const target = String(url);
    if (target.startsWith("/api/market-scans/polling-identity?")) {
      return marketScanPollingIdentity(identityLatest, identityPublished);
    }
    if (target.startsWith("/api/market-scans/latest-published?")) return identityPublished;
    if (target === "/api/market-scans/latest") {
      calls.latest += 1;
      signals.latest.push(options.signal);
      if (calls.latest === 1) return initialLatest.promise;
      if (calls.latest === 2) return staleLatest.promise;
      return retried;
    }
    if (target === "/api/market-scans") {
      calls.start += 1;
      return startResponse.promise;
    }
    if (target === "/api/market-scans/1/cancel") {
      calls.cancel += 1;
      return cancelResponse.promise;
    }
    if (target === "/api/market-scans/3/retry") {
      calls.retry += 1;
      return retryResponse.promise;
    }
    if (target.includes("/api/market-scans/3/results?")) {
      calls.results += 1;
      signals.results.push(options.signal);
      return staleResult.promise;
    }
    throw new Error(`unexpected request: ${target}`);
  },
});

const activation = controller.activate();
await flushPromises();
document.activeElement = element("marketScanStart");
const starting = controller.start();
assert.equal(document.activeElement, element("marketScanMarket"), "focused start was disabled without focus transfer");
assert.equal(await controller.start(), null, "duplicate start was not rejected");
assert.equal(calls.start, 1);
assert.equal(signals.latest[0].aborted, false, "start aborted an admitted latest read");
assert.equal(controller.state.actionBusy, true);
assert.equal(element("workspace-panel-market-scan")["aria-busy"], "true");
assert.equal(element("marketScanProgressBar")["aria-busy"], "true");
assert.equal(element("marketScanStart").disabled, true);
assert.match(element("marketScanAnnouncement").textContent, /开始扫描请求处理中/);
initialLatest.resolve(scanRun(99, "success", "不应恢复的旧任务"));
await activation;
assert.equal(controller.state.run, null, "aborted latest response replaced state");
startResponse.resolve({ accepted: true, deduplicated: false, run: running });
await starting;
assert.equal(controller.state.run.id, 1);
assert.equal(controller.state.actionBusy, false);
assert.equal(element("workspace-panel-market-scan")["aria-busy"], "false");
assert.equal(element("marketScanStart").disabled, true, "busy completion re-enabled start for an active run");
assert.equal(scheduled.size, 1, "stale latest scheduled an extra poll");

identityLatest = scanRun(98, "running", "取消前读取中");
const latestRead = controller.loadLatest();
await flushPromises();
document.activeElement = element("marketScanCancel");
const cancelling = controller.cancel();
assert.equal(await controller.cancel(), null, "duplicate cancel was not rejected");
assert.equal(calls.cancel, 1);
assert.equal(signals.latest[1].aborted, false, "cancel aborted an admitted latest read");
staleLatest.resolve(scanRun(98, "success", "不应覆盖取消结果"));
await latestRead;
cancelResponse.resolve(cancelled);
await cancelling;
assert.equal(controller.state.run.status, "cancelled");
assert.equal(document.activeElement, element("marketScanStart"), "cancel completion did not restore a visible action");
assert.equal(scheduled.size, 1, "stale cancel-era latest scheduled an extra poll");

controller.state.run = degraded;
const resultRead = controller.loadResults();
await flushPromises();
document.activeElement = element("marketScanRetry");
const retrying = controller.retry();
assert.equal(await controller.retry(), null, "duplicate retry was not rejected");
assert.equal(calls.retry, 1);
assert.equal(signals.results[0].aborted, false, "task mutation cancelled an independent published-result read");
staleResult.resolve({});
await resultRead;
retryResponse.resolve({ accepted: true, deduplicated: false, run: retried });
await retrying;
assert.equal(controller.state.run.id, 4, "stale result replaced the retried run");
assert.equal(document.activeElement, element("marketScanCancel"), "retry completion did not focus the active-run action");
assert.equal(element("marketScanRetry").hidden, true);
assert.equal(scheduled.size, 1, "stale result scheduled an extra poll");

await controller.activate();
await controller.activate();
assert.equal(calls.latest, 2, "repeated activation fetched latest again");
controller.deactivate();
identityLatest = retried;
await controller.activate();
assert.equal(calls.latest, 3, "reactivation after leaving did not refresh latest");
controller.deactivate();
assert.equal(scheduled.size, 0);

function scanRun(id, status, message) {
  const active = ["queued", "running", "cancelling"].includes(status);
  const published = ["success", "degraded"].includes(status);
  return {
    id, status, trigger: "manual", mode: "official", rule_version: "v1", as_of: "2026-07-17 16:30:00",
    data_date: "2026-07-17", quote_date: "2026-07-17", scope: "沪市 + 深市 + 北交所当前上市A股", total_count: 1, excluded_count: 0,
      processed_count: published ? 1 : 0, success_count: published ? 1 : 0, missing_count: 0,
      skipped_count: 0, retry_count: 0, progress_pct: published ? 100 : 0,
      coverage_pct: published ? 100 : 0, created_at: "2026-07-17 16:30:00",
    updated_at: "2026-07-17 16:31:00", finished_at: active ? null : "2026-07-17 16:31:00", message,
    snapshot_digest: published ? "a".repeat(64) : null, snapshot_seal_origin: published ? "publication" : null,
    snapshot_sealed_at: published ? "2026-07-17 16:31:00" : null,
  };
}
function deferred() {
  let resolve;
  const promise = new Promise((done) => { resolve = done; });
  return { promise, resolve };
}
async function flushPromises() {
  for (let index = 0; index < 20; index += 1) await Promise.resolve();
}
'''
    )


def test_market_scan_surface_lifecycle_and_responsive_page_size_are_coherent() -> None:
    _run_node_script(
        r'''
import assert from "node:assert/strict";
import { installAppDom, marketScanPollingIdentity } from "./tests/frontend_app_flow_helpers.mjs";
import { createMarketScanController } from "./static/js/market-scan.js";

const { element } = installAppDom({ canvasContext: null });
const media = {
  matches: false,
  listeners: [],
  addEventListener(type, listener) { if (type === "change") this.listeners.push(listener); },
};
globalThis.matchMedia = () => media;
element("marketScanRows").ownerDocument = { defaultView: { matchMedia: () => media } };
const terminal = {
  id: 90, status: "success", trigger: "manual", mode: "official", rule_version: "v1",
  as_of: "2026-07-31 16:30:00", data_date: "2026-07-31", quote_date: "2026-07-31", scope: "沪市 + 深市 + 北交所当前上市A股",
  total_count: 130, excluded_count: 0, processed_count: 130, success_count: 130,
  missing_count: 0, skipped_count: 0, retry_count: 0, progress_pct: 100, coverage_pct: 100,
  created_at: "2026-07-31 16:30:00", updated_at: "2026-07-31 16:31:00",
  finished_at: "2026-07-31 16:31:00", message: "扫描完成",
  snapshot_digest: "a".repeat(64), snapshot_seal_origin: "publication", snapshot_sealed_at: "2026-07-31 16:31:00",
};
const resultUrls = [];
const controller = createMarketScanController({
  root: document,
  now: new Date(2026, 6, 31, 16, 30),
  pollIntervalMs: 60000,
  idlePollIntervalMs: 60000,
      async fetcher(url) {
        const value = String(url);
        if (value.startsWith("/api/market-scans/polling-identity?")) {
          return marketScanPollingIdentity(terminal, terminal);
            }
            if (value === "/api/market-scans/latest") return terminal;
            if (value.startsWith("/api/market-scans/latest-published?mode=")) return terminal;
        if (value.startsWith("/api/market-scans?")) return { items: [terminal], total: 1, page: 1, page_size: 100, page_count: 1 };
    if (value.includes("/results?")) {
      resultUrls.push(value);
      const params = new URL(value, "http://localhost").searchParams;
      const page = Number(params.get("page"));
      const pageSize = Number(params.get("page_size"));
      const itemCount = Math.max(0, Math.min(pageSize, 130 - ((page - 1) * pageSize)));
      const items = Array.from({ length: itemCount }, (_, index) => stock(index + ((page - 1) * pageSize)));
      return { run: terminal, items, total: 130, page, page_size: pageSize, page_count: Math.ceil(130 / pageSize) };
    }
    throw new Error(`unexpected request: ${value}`);
  },
});

await controller.activate();
assert.match(resultUrls.at(-1), /page=1&page_size=100/);
controller.state.page = 4;
media.matches = true;
media.listeners.forEach((listener) => listener({ matches: true }));
await flushPromises();
assert.equal(controller.state.page, 1);
assert.match(resultUrls.at(-1), /page=1&page_size=30/);

const renderedRequests = resultUrls.length;
assert.equal(controller.setSurfaceActive(false), true);
assert.equal(controller.state.activated, true, "leaving the surface stopped background task tracking");
assert.equal(element("marketScanRows").innerHTML, "");
await controller.loadLatest();
assert.equal(resultUrls.length, renderedRequests, "hidden surface rehydrated result rows");
assert.equal(controller.setSurfaceActive(true), true);
await flushPromises();
assert.equal(resultUrls.length, renderedRequests + 1, "returning to the surface did not restore results");
controller.deactivate();

function stock(index) {
  const code = String(600000 + index).padStart(6, "0");
  return {
    run_id: terminal.id, rank: index + 1, symbol: `${code}.SH`, code, market: "SH", name: `股票${code}`,
    updated_at: terminal.updated_at, status: "success", is_st: false, is_new: false, tags: [], metrics: {},
    score: 80, raw_score: 80, trend_score: 80, leader_score: 80, data_quality_score: 80,
    price: 10, data_date: terminal.data_date, quote_timestamp: "2026-07-31 15:00:00",
    quote_observed_at: "2026-07-31T07:00:00Z", quote_source: "fixture",
    kline_source: "fixture", adjustment_mode: "qfq", reason: null, error: null,
  };
}

async function flushPromises() {
  for (let index = 0; index < 30; index += 1) await Promise.resolve();
}
'''
    )


def test_market_scan_polling_identity_sync_is_non_authorizing_bounded_and_coalesced() -> None:
    _run_node_script(
        r'''
import assert from "node:assert/strict";
import { marketScanPollingIdentity } from "./tests/frontend_app_flow_helpers.mjs";
import { createMarketScanLatestSync } from "./static/js/market-scan-latest-sync.js";
import { marketScanContractError } from "./static/js/market-scan-contracts.js";
import { validateMarketScanPollingIdentity } from "./static/js/market-scan-polling-identity.js";

const identityA = marketScanPollingIdentity(run(1), run(1));
const identityB = marketScanPollingIdentity(run(2), run(2));
const identityC = marketScanPollingIdentity(run(3), run(3));
const identityE = marketScanPollingIdentity(run(5), run(5));
const identityF = marketScanPollingIdentity(run(6), run(6));
const identityG = marketScanPollingIdentity(run(7), run(7));
const identityD = marketScanPollingIdentity(run(4), run(4), "intraday");
const valid = validateMarketScanPollingIdentity(identityA, "official");
assert.equal(Object.isFrozen(valid), true);
assert.equal(Object.isFrozen(valid.latest), true);
assert.throws(() => validateMarketScanPollingIdentity({ ...identityA, authorization: "results" }, "official"));
assert.throws(() => validateMarketScanPollingIdentity({
  ...identityA, latest: { ...identityA.latest, status: "success" },
}, "official"));
assert.throws(() => validateMarketScanPollingIdentity({
  ...identityA,
  latest: { run_id: null, token: "a".repeat(64) },
  latest_published: { run_id: 1, token: "b".repeat(64) },
}, "official"), /不能脱离/);
assert.throws(() => validateMarketScanPollingIdentity({
  ...identityA,
  latest: { run_id: 1, token: "a".repeat(64) },
  latest_published: { run_id: 2, token: "b".repeat(64) },
}, "official"), /不能晚于/);
assert.throws(() => validateMarketScanPollingIdentity({
  ...identityA,
  latest: { run_id: null, token: "a".repeat(64) },
  latest_published: { run_id: null, token: "b".repeat(64) },
}, "official"), /token 不一致/);

const state = {
  actionBusy: false, browseMode: "official", pollingIdentity: identityA,
  run: { id: 1 }, runRequest: null, runRequestSeq: 0,
};
let identityCalls = 0;
let scheduleCalls = 0;
let stageCalls = 0;
let errors = 0;
const errorOptions = [];
let requestImpl = async () => identityA;
let stageImpl = async () => { throw new Error("unchanged poll must not stage trusted reads"); };
let commitImpl = () => { throw new Error("unchanged poll must not commit"); };
const sync = createMarketScanLatestSync({
  abortRequest,
  beginRequest,
  commit: (...args) => commitImpl(...args),
  finishRequest,
  handleError(_error, options) { errors += 1; errorOptions.push(options); },
  isCurrentRequest,
  polling: {
    clear() {}, resetFailures() {},
    scheduleDefault() { scheduleCalls += 1; },
  },
  renderLoading() {},
  async request(url, options) {
    identityCalls += 1;
    return requestImpl(url, options);
  },
  stage: (...args) => { stageCalls += 1; return stageImpl(...args); },
  state,
});

for (let tick = 0; tick < 1_000; tick += 1) await sync.sync();
assert.equal(identityCalls, 1_000);
assert.equal(scheduleCalls, 1_000);
assert.equal(stageCalls, 0);
assert.equal(errors, 0);

const pendingIdentity = deferred();
let pendingSignal = null;
identityCalls = 0;
requestImpl = async (_url, options) => {
  pendingSignal = options.signal;
  return pendingIdentity.promise;
};
const concurrent = Array.from({ length: 100 }, () => sync.sync());
assert.equal(new Set(concurrent).size, 1, "unchanged concurrent ticks were not coalesced");
assert.equal(identityCalls, 1);
pendingIdentity.resolve(identityA);
await Promise.all(concurrent);
assert.equal(pendingSignal.aborted, false);
assert.equal(identityCalls, 1);

const raceResponses = [identityB, identityC, identityC];
const stagedIds = [];
const committed = [];
state.pollingIdentity = identityA;
state.run = { id: 1 };
requestImpl = async () => raceResponses.shift();
stageImpl = async (identity) => {
  stagedIds.push(identity.latest.run_id);
  return { run: { id: identity.latest.run_id } };
};
commitImpl = (staged, identity) => committed.push([staged.run.id, identity.latest.run_id]);
await sync.sync();
assert.deepEqual(stagedIds, [2, 3]);
assert.deepEqual(committed, [[3, 3]]);
assert.equal(state.pollingIdentity.fingerprint, identityC.fingerprint);

const oldIdentity = deferred();
let oldSignal = null;
let supersedeCalls = 0;
requestImpl = async (_url, options) => {
  supersedeCalls += 1;
  if (supersedeCalls === 1) {
    oldSignal = options.signal;
    return oldIdentity.promise;
  }
  return identityD;
};
stagedIds.length = 0;
committed.length = 0;
const oldOperation = sync.sync({ forceTrusted: true });
state.browseMode = "intraday";
const drain = sync.supersede();
assert.equal(oldSignal.aborted, false, "mode supersede aborted the admitted identity request");
oldIdentity.resolve(identityC);
await Promise.all([oldOperation, drain]);
const replacement = sync.sync({ forceTrusted: true });
await replacement;
assert.deepEqual(committed, [[4, 4]]);
assert.equal(state.pollingIdentity.request_mode, "intraday");
assert.equal(errors, 0);

state.browseMode = "official";
state.pollingIdentity = null;
state.run = null;
requestImpl = async () => identityA;
stageImpl = async () => { throw marketScanContractError("固定批次合同错误"); };
const circuitIdentityCalls = identityCalls;
const circuitStageCalls = stageCalls;
await sync.sync();
assert.equal(stageCalls, circuitStageCalls + 1);
assert.equal(errors, 1);
assert.equal(errorOptions.at(-1).deterministicFailure, true);
for (let tick = 0; tick < 100; tick += 1) await sync.sync();
assert.equal(identityCalls, circuitIdentityCalls + 101);
assert.equal(stageCalls, circuitStageCalls + 1, "stable failed fingerprint repeated trusted selectors");
assert.equal(errors, 1);

await sync.sync({ forceTrusted: true });
assert.equal(stageCalls, circuitStageCalls + 2, "explicit refresh did not retry trusted selectors once");
assert.equal(errors, 2);
requestImpl = async () => identityB;
await sync.sync();
assert.equal(stageCalls, circuitStageCalls + 3, "changed fingerprint did not reopen trusted sync");
assert.equal(errors, 3);

requestImpl = async () => identityC;
stageImpl = async () => { throw new Error("临时网络失败"); };
await sync.sync();
await sync.sync();
assert.equal(stageCalls, circuitStageCalls + 5, "transient network failures stopped bounded retries");
assert.deepEqual(errorOptions.slice(-2).map((options) => options.deterministicFailure), [false, false]);

requestImpl = async () => identityE;
stageImpl = async () => {
  const error = new Error("全市场冻结快照正在校验，请稍后重试");
  error.status = 503;
  throw error;
};
await sync.sync();
await sync.sync();
assert.equal(stageCalls, circuitStageCalls + 7, "admission busy did not remain retryable");
assert.deepEqual(errorOptions.slice(-2).map((options) => options.deterministicFailure), [false, false]);

requestImpl = async () => identityF;
stageImpl = async () => {
  const error = new Error("全市场冻结快照完整性校验失败");
  error.status = 409;
  throw error;
};
await sync.sync();
for (let tick = 0; tick < 100; tick += 1) await sync.sync();
assert.equal(stageCalls, circuitStageCalls + 8, "stable HTTP 409 repeated trusted selectors");

requestImpl = async () => identityG;
stageImpl = async () => { throw new Error("请求超时，请稍后重试"); };
await sync.sync();
for (let tick = 0; tick < 100; tick += 1) await sync.sync();
assert.equal(stageCalls, circuitStageCalls + 9, "stable request timeout repeated trusted selectors");
await sync.sync({ forceTrusted: true });
assert.equal(stageCalls, circuitStageCalls + 10, "explicit retry did not reopen timed-out fingerprint once");

function abortRequest(controllerField, sequenceField) {
  state[controllerField]?.abort?.();
  state[controllerField] = null;
  state[sequenceField] += 1;
}
function beginRequest(controllerField, sequenceField) {
  abortRequest(controllerField, sequenceField);
  state[controllerField] = new AbortController();
  return state[sequenceField];
}
function finishRequest(controllerField, sequenceField, sequence) {
  if (state[sequenceField] === sequence) state[controllerField] = null;
}
function isCurrentRequest(sequenceField, sequence) { return state[sequenceField] === sequence; }
function deferred() {
  let resolve;
  const promise = new Promise((done) => { resolve = done; });
  return { promise, resolve };
}
function run(id) {
  return { id, status: "success", mode: "official", scope: "full", updated_at: `2026-08-${String(id).padStart(2, "0")}` };
}
'''
    )


def test_market_scan_polling_identity_cannot_select_or_authorize_an_old_run() -> None:
    _run_node_script(
        r'''
import assert from "node:assert/strict";
import { installAppDom, marketScanPollingIdentity } from "./tests/frontend_app_flow_helpers.mjs";
import { createMarketScanController } from "./static/js/market-scan.js";

const { element } = installAppDom({ canvasContext: null });
const staleIdentityRun = scanRun(1, "success", "被伪造为最近的旧批次");
const trustedPublished = scanRun(2, "success", "可信最近已发布批次");
const trustedLatest = scanRun(3, "running", "可信全局最近任务");
const calls = [];
const controller = createMarketScanController({
  root: document,
  pollIntervalMs: 60000,
  idlePollIntervalMs: 60000,
  async fetcher(url) {
    const target = String(url);
    calls.push(target);
    if (target.startsWith("/api/market-scans/polling-identity?")) {
      return marketScanPollingIdentity(staleIdentityRun, staleIdentityRun);
    }
    if (target === "/api/market-scans/latest") return trustedLatest;
    if (target.startsWith("/api/market-scans/latest-published?mode=")) return trustedPublished;
    if (target.startsWith("/api/market-scans?")) {
      return { items: [trustedPublished], total: 1, page: 1, page_size: 100, page_count: 1 };
    }
    if (target.startsWith("/api/market-scans/2/results?")) {
      return { run: trustedPublished, items: [], total: 0, page: 1, page_size: 100, page_count: 0 };
    }
    throw new Error(`unexpected request: ${target}`);
  },
});

await controller.activate();
assert.equal(controller.state.run.id, 3);
assert.equal(controller.state.publishedRun.id, 2);
assert.equal(element("marketScanHeadline").textContent, "可信全局最近任务");
assert.equal(element("marketScanTableWrap").dataset.marketScanRunId, "2");
assert.equal(calls.filter((url) => url === "/api/market-scans/latest").length, 1);
assert.equal(calls.filter((url) => url.startsWith("/api/market-scans/latest-published?")).length, 1);
assert.equal(calls.filter((url) => url.startsWith("/api/market-scans/2/results?")).length, 1);
assert.equal(calls.some((url) => url.startsWith("/api/market-scans/1/results?")), false);
controller.deactivate();

function scanRun(id, status, message) {
  const published = status === "success";
  return {
    id, status, trigger: "manual", mode: "official", rule_version: "full-market-score-v5",
    as_of: "2026-08-14 16:30:00", data_date: "2026-08-14", quote_date: "2026-08-14",
    scope: "沪市 + 深市 + 北交所当前上市A股", total_count: 100, excluded_count: 0,
    processed_count: published ? 100 : 1, success_count: published ? 100 : 1,
    missing_count: 0, skipped_count: 0, retry_count: 0,
    progress_pct: published ? 100 : 1, coverage_pct: published ? 100 : 1,
    created_at: "2026-08-14 16:30:00", updated_at: `2026-08-14 16:3${id}:00`,
    finished_at: published ? `2026-08-14 16:3${id}:00` : null, message,
    snapshot_digest: published ? String(id).repeat(64) : null,
    snapshot_seal_origin: published ? "publication" : null,
    snapshot_sealed_at: published ? `2026-08-14 16:3${id}:00` : null,
  };
}
'''
    )


def test_market_scan_latest_sync_is_invalidated_by_history_surface_and_user_queries() -> None:
    _run_node_script(
        r'''
import assert from "node:assert/strict";
import { installAppDom, marketScanPollingIdentity, waitFor } from "./tests/frontend_app_flow_helpers.mjs";
import { createMarketScanController } from "./static/js/market-scan.js";

const nativeSetTimeout = globalThis.setTimeout;
const nativeClearTimeout = globalThis.clearTimeout;
await historySelectionRace();
await surfaceDeactivationRace();
await userQueryRace();
await selectorStageQueryRace();
await historyListGapRace();

async function historySelectionRace() {
  const { element } = installAppDom({ canvasContext: null });
  const current = scanRun(10, "当前批次");
  const historical = scanRun(9, "历史批次");
  const replacement = scanRun(11, "晚到的新批次");
  let identityRun = current;
  let selectedLatest = current;
  let selectedPublished = current;
  let delayed = null;
  let delayedSignal = null;
  const controller = createMarketScanController({
    root: document, pollIntervalMs: 60000, idlePollIntervalMs: 60000,
    async fetcher(url, options = {}) {
      const target = String(url);
      if (target.startsWith("/api/market-scans/polling-identity?")) {
        return marketScanPollingIdentity(identityRun, identityRun);
      }
      if (target === "/api/market-scans/latest") return selectedLatest;
      if (target.startsWith("/api/market-scans/latest-published?")) return selectedPublished;
      if (target.startsWith("/api/market-scans?")) {
        return { items: [current, historical], total: 2, page: 1, page_size: 100, page_count: 1 };
      }
      if (target === "/api/market-scans/9") return historical;
      if (target.startsWith("/api/market-scans/11/results?")) {
        delayedSignal = options.signal;
        return delayed.promise;
      }
      if (target.includes("/results?")) {
        const run = target.includes("/9/results?") ? historical : current;
        return resultPage(run, run.message);
      }
      throw new Error(`unexpected request: ${target}`);
    },
  });
  await controller.activate();
  identityRun = replacement;
  selectedLatest = replacement;
  selectedPublished = replacement;
  delayed = deferred();
  const lateSync = controller.loadLatest();
  await waitFor(() => delayedSignal !== null, "new publication result read");
  element("marketScanHistoryRun").value = "9";
  element("marketScanHistoryRun").listeners.change();
  await flushPromises();
  assert.equal(delayedSignal.aborted, false, "history selection aborted an admitted result worker");
  assert.notEqual(controller.state.publishedRun?.id, 9, "history selection bypassed the read tail");
  delayed.resolve(resultPage(replacement, replacement.message));
  await lateSync;
  await waitFor(() => controller.state.publishedRun?.id === 9, "historical selection");
  assert.equal(controller.state.run.id, 10, "late latest sync replaced the task run");
  assert.equal(controller.state.publishedRun.id, 9, "late latest sync replaced the selected history run");
  assert.equal(element("marketScanTableWrap").dataset.marketScanRunId, "9");
  assert.match(element("marketScanRows").innerHTML, /历史批次/);
  controller.deactivate();
}

async function surfaceDeactivationRace() {
  const { element } = installAppDom({ canvasContext: null });
  const timers = new Map();
  let timerId = 0;
  globalThis.setTimeout = (callback, delay = 0) => {
    const id = ++timerId;
    timers.set(id, { callback, delay });
    return id;
  };
  globalThis.clearTimeout = (id) => timers.delete(id);
  const current = scanRun(20, "可见批次");
  let delayed = null;
  let delayedSignal = null;
  let delayResults = false;
  let identityCalls = 0;
  let selectorCalls = 0;
  let resultCalls = 0;
  const controller = createMarketScanController({
    root: document, pollIntervalMs: 60000, idlePollIntervalMs: 60000,
    async fetcher(url, options = {}) {
      const target = String(url);
      if (target.startsWith("/api/market-scans/polling-identity?")) {
        identityCalls += 1;
        return marketScanPollingIdentity(current, current);
      }
      if (target === "/api/market-scans/latest" || target.startsWith("/api/market-scans/latest-published?")) {
        selectorCalls += 1;
        return current;
      }
      if (target.startsWith("/api/market-scans?")) {
        return { items: [current], total: 1, page: 1, page_size: 100, page_count: 1 };
      }
      if (target.includes("/results?")) {
        resultCalls += 1;
        if (delayResults) {
          delayedSignal = options.signal;
          return delayed.promise;
        }
        return resultPage(current, current.message);
      }
      throw new Error(`unexpected request: ${target}`);
    },
  });
  await controller.activate();
  delayed = deferred();
  delayResults = true;
  const lateSync = controller.loadLatest();
  await waitFor(() => delayedSignal !== null, "hidden-surface result read");
  const admittedSignal = delayedSignal;
  const callsBeforeToggle = { identityCalls, selectorCalls, resultCalls };
  assert.equal(controller.setSurfaceActive(false), true);
  assert.equal(controller.setSurfaceActive(true), true);
  await flushPromises();
  assert.equal(admittedSignal.aborted, false, "surface toggle aborted an admitted result worker");
  assert.deepEqual(
    { identityCalls, selectorCalls, resultCalls },
    callsBeforeToggle,
    "surface reactivation bypassed the admitted result worker",
  );
  delayed.resolve(resultPage(current, "不得晚到渲染"));
  await lateSync;
  await waitFor(
    () => resultCalls === callsBeforeToggle.resultCalls + 1
      && element("marketScanTableWrap").dataset.marketScanRunId === "20"
      && timers.size === 1,
    "surface refresh after drain",
  );
  assert.equal(controller.state.publishedRun.id, 20);
  assert.equal(element("marketScanTableWrap").dataset.marketScanRunId, "20");
  assert.equal(element("marketScanRows").innerHTML.includes("不得晚到渲染"), true);
  assert.equal(timers.size, 1, "surface reactivation did not restore one polling timer");
  const callsBeforeTick = { identityCalls, selectorCalls, resultCalls };
  const [timerKey, timer] = [...timers.entries()][0];
  timers.delete(timerKey);
  timer.callback();
  await flushPromises();
  assert.equal(identityCalls, callsBeforeTick.identityCalls + 1);
  assert.equal(selectorCalls, callsBeforeTick.selectorCalls, "unchanged hidden tick used trusted selectors");
  assert.equal(resultCalls, callsBeforeTick.resultCalls, "unchanged hidden tick read results");
  assert.equal(timers.size, 1);
  controller.deactivate();
  globalThis.setTimeout = nativeSetTimeout;
  globalThis.clearTimeout = nativeClearTimeout;
}

async function selectorStageQueryRace() {
  const { element } = installAppDom({ canvasContext: null });
  const current = scanRun(40, "selector旧榜单");
  const latestSelector = deferred();
  let delayLatest = false;
  let admittedSignal = null;
  let publishedSelectors = 0;
  const resultQueries = [];
  const controller = createMarketScanController({
    root: document, pollIntervalMs: 60000, idlePollIntervalMs: 60000,
    async fetcher(url, options = {}) {
      const target = String(url);
      if (target.startsWith("/api/market-scans/polling-identity?")) {
        return marketScanPollingIdentity(current, current);
      }
      if (target === "/api/market-scans/latest") {
        if (delayLatest) {
          delayLatest = false;
          admittedSignal = options.signal;
          return latestSelector.promise;
        }
        return current;
      }
      if (target.startsWith("/api/market-scans/latest-published?")) {
        publishedSelectors += 1;
        return current;
      }
      if (target.startsWith("/api/market-scans?")) {
        return { items: [current], total: 1, page: 1, page_size: 100, page_count: 1 };
      }
      if (target.includes("/results?")) {
        resultQueries.push(target);
        return resultPage(current, target.includes("market=BJ") ? "selector后筛选" : current.message);
      }
      throw new Error(`unexpected request: ${target}`);
    },
  });
  await controller.activate();
  const baselinePublished = publishedSelectors;
  const baselineResults = resultQueries.length;
  delayLatest = true;
  const staleLatest = controller.loadLatest();
  await waitFor(() => admittedSignal !== null, "latest selector admission");
  element("marketScanMarket").value = "BJ";
  const query = controller.loadResults();
  await flushPromises();
  assert.equal(admittedSignal.aborted, false, "selector-stage query aborted the admitted selector");
  assert.equal(publishedSelectors, baselinePublished, "stale chain continued to latest-published before release");
  assert.equal(resultQueries.length, baselineResults, "query bypassed selector-stage ownership");
  latestSelector.resolve(current);
  await staleLatest;
  await query;
  assert.equal(publishedSelectors, baselinePublished, "stale selector chain continued after supersede");
  assert.equal(resultQueries.length, baselineResults + 1);
  assert.match(resultQueries.at(-1), /market=BJ/);
  assert.match(element("marketScanRows").innerHTML, /selector后筛选/);
  controller.deactivate();
}

async function historyListGapRace() {
  const { element } = installAppDom({ canvasContext: null });
  const official = scanRun(50, "正式旧榜单");
  const intraday = { ...scanRun(51, "盘中新榜单"), mode: "intraday" };
  let selected = official;
  let delayedHistory = null;
  let delayedHistorySignal = null;
  const selectors = [];
  const resultQueries = [];
  const calls = [];
  const controller = createMarketScanController({
    root: document, pollIntervalMs: 60000, idlePollIntervalMs: 60000,
    async fetcher(url, options = {}) {
      const target = String(url);
      calls.push(target);
      if (target.startsWith("/api/market-scans/polling-identity?")) {
        const mode = target.includes("mode=intraday") ? "intraday" : "official";
        const run = mode === "intraday" ? intraday : official;
        return marketScanPollingIdentity(run, run, mode);
      }
      if (target === "/api/market-scans/latest" || target.startsWith("/api/market-scans/latest-published?")) {
        selectors.push(target);
        return selected;
      }
      if (target.startsWith("/api/market-scans?")) {
        if (delayedHistory) {
          const pending = delayedHistory;
          delayedHistory = null;
          delayedHistorySignal = options.signal;
          return pending.promise;
        }
        return { items: [selected], total: 1, page: 1, page_size: 100, page_count: 1 };
      }
      if (target.includes("/results?")) {
        resultQueries.push(target);
        return resultPage(selected, selected.message);
      }
      throw new Error(`unexpected request: ${target}`);
    },
  });
  await controller.activate();
  const baseline = { results: resultQueries.length, selectors: selectors.length };
  const historyRead = deferred();
  delayedHistory = historyRead;
  element("marketScanHistoryRefresh").listeners.click();
  await waitFor(() => delayedHistorySignal !== null, "deferred history list");
  selected = intraday;
  element("marketScanModeOfficial").checked = false;
  element("marketScanModeIntraday").checked = true;
  element("marketScanModeIntraday").listeners.change();
  await flushPromises();
  assert.equal(delayedHistorySignal.aborted, true, "new mode did not supersede the light history read");
  assert.deepEqual(
    { results: resultQueries.length, selectors: selectors.length },
    baseline,
    "stale history continuation started a heavy selector",
  );
  historyRead.resolve({ items: [official], total: 1, page: 1, page_size: 100, page_count: 1 });
  await flushPromises();
  await waitFor(() => controller.state.browseMode === "intraday", "final mode transition");
  for (let index = 0; index < 120 && controller.state.publishedRun?.id !== 51; index += 1) {
    await Promise.resolve();
  }
  assert.equal(
    controller.state.publishedRun?.id,
    51,
    `final intraday publication missing: ${JSON.stringify({ calls, selectors, resultQueries })}`,
  );
  assert.equal(selectors.length, baseline.selectors + 2);
  assert.equal(resultQueries.length, baseline.results + 1);
  controller.deactivate();
}

async function userQueryRace() {
  const { element } = installAppDom({ canvasContext: null });
  const current = scanRun(30, "旧榜单");
  const replacement = scanRun(31, "新榜单");
  let identityRun = current;
  let selectedRun = current;
  let delayed = null;
  let delayedSignal = null;
  let delayedUsed = false;
  const resultQueries = [];
  const controller = createMarketScanController({
    root: document, pollIntervalMs: 60000, idlePollIntervalMs: 60000,
    async fetcher(url, options = {}) {
      const target = String(url);
      if (target.startsWith("/api/market-scans/polling-identity?")) {
        return marketScanPollingIdentity(identityRun, identityRun);
      }
      if (target === "/api/market-scans/latest" || target.startsWith("/api/market-scans/latest-published?")) {
        return selectedRun;
      }
      if (target.startsWith("/api/market-scans?")) {
        return { items: [current], total: 1, page: 1, page_size: 100, page_count: 1 };
      }
      if (target.startsWith("/api/market-scans/31/results?")) {
        resultQueries.push(target);
        if (!delayedUsed) {
          delayedUsed = true;
          delayedSignal = options.signal;
          return delayed.promise;
        }
        return resultPage(replacement, target.includes("market=BJ") ? "用户筛选保留" : replacement.message);
      }
      if (target.startsWith("/api/market-scans/30/results?")) {
        return resultPage(current, target.includes("market=BJ") ? "用户筛选保留" : current.message);
      }
      throw new Error(`unexpected request: ${target}`);
    },
  });
  await controller.activate();
  identityRun = replacement;
  selectedRun = replacement;
  delayed = deferred();
  const lateSync = controller.loadLatest();
  await waitFor(() => delayedSignal !== null, "staged replacement result read");
  element("marketScanMarket").value = "BJ";
  element("marketScanProbabilityMin").disabled = false;
  element("marketScanProbabilityMin").value = "70";
  controller.state.page = 4;
  const userResults = controller.loadResults();
  assert.equal(delayedSignal.aborted, false, "user query aborted an admitted trusted result read");
  delayed.resolve(resultPage(replacement, replacement.message));
  await lateSync;
  await userResults;
  assert.equal(controller.state.run.id, 31);
  assert.equal(controller.state.publishedRun.id, 31);
  assert.equal(controller.state.pollingIdentity.latest.run_id, 31);
  assert.match(element("marketScanRows").innerHTML, /用户筛选保留/);
  assert.equal(element("marketScanTableWrap").dataset.marketScanRunId, "31");
  const rebound = Object.fromEntries(new URLSearchParams(resultQueries.at(-1).split("?", 2)[1]));
  assert.equal(rebound.page, "1");
  assert.equal(rebound.market, "BJ");
  assert.equal("probability_horizon" in rebound, false);
  assert.equal("min_upside_probability" in rebound, false);
  controller.deactivate();
}

function scanRun(id, message) {
  return {
    id, status: "success", trigger: "manual", mode: "official", rule_version: "full-market-score-v5",
    as_of: "2026-08-14 16:30:00", data_date: "2026-08-14", quote_date: "2026-08-14",
    scope: "沪市 + 深市 + 北交所当前上市A股", total_count: 1, excluded_count: 0,
    processed_count: 1, success_count: 1, missing_count: 0, skipped_count: 0, retry_count: 0,
    progress_pct: 100, coverage_pct: 100, created_at: "2026-08-14 16:30:00",
    updated_at: "2026-08-14 16:31:00", finished_at: "2026-08-14 16:31:00", message,
    snapshot_digest: String(id % 10).repeat(64), snapshot_seal_origin: "publication",
    snapshot_sealed_at: "2026-08-14 16:31:00",
  };
}
function resultPage(run, name) {
  return {
    run, total: 1, page: 1, page_size: 100, page_count: 1,
    items: [{
      run_id: run.id, rank: 1, symbol: "600001.SH", code: "600001", market: "SH", name,
      updated_at: run.updated_at, status: "success", is_st: false, is_new: false, tags: [], metrics: {},
      score: 80, raw_score: 80, trend_score: 80, leader_score: 80, data_quality_score: 80,
      price: 10, data_date: run.data_date, quote_timestamp: "2026-08-14 15:00:00",
      quote_observed_at: "2026-08-14T07:00:00Z", quote_source: "fixture",
      kline_source: "fixture", adjustment_mode: "qfq", reason: null, error: null,
    }],
  };
}
function deferred() {
  let resolve;
  const promise = new Promise((done) => { resolve = done; });
  return { promise, resolve };
}
async function flushPromises() {
  for (let index = 0; index < 40; index += 1) await Promise.resolve();
}
'''
    )


def test_market_scan_polling_query_reset_tracks_trusted_publication_not_force_refresh() -> None:
    _run_node_script(
        r'''
import assert from "node:assert/strict";
import { installAppDom, marketScanPollingIdentity } from "./tests/frontend_app_flow_helpers.mjs";
import { createMarketScanController } from "./static/js/market-scan.js";

const { element } = installAppDom({ canvasContext: null });
const published = {
  id: 40, status: "success", trigger: "manual", mode: "official", rule_version: "full-market-score-v5",
  as_of: "2026-08-14 16:30:00", data_date: "2026-08-14", quote_date: "2026-08-14",
  scope: "沪市 + 深市 + 北交所当前上市A股", total_count: 101, excluded_count: 0,
  processed_count: 101, success_count: 101, missing_count: 0, skipped_count: 0, retry_count: 0,
  progress_pct: 100, coverage_pct: 100, created_at: "2026-08-14 16:30:00",
  updated_at: "2026-08-14 16:31:00", finished_at: "2026-08-14 16:31:00", message: "扫描完成",
  snapshot_digest: "a".repeat(64), snapshot_seal_origin: "publication",
  snapshot_sealed_at: "2026-08-14 16:31:00",
};
const replacement = {
  ...published, id: 41, message: "可信selector已切换", snapshot_digest: "b".repeat(64),
};
let identity = marketScanPollingIdentity(published, published);
let selectedRun = published;
const resultUrls = [];
const controller = createMarketScanController({
  root: document, pollIntervalMs: 60000, idlePollIntervalMs: 60000,
  async fetcher(url) {
    const target = String(url);
    if (target.startsWith("/api/market-scans/polling-identity?")) return identity;
    if (target === "/api/market-scans/latest" || target.startsWith("/api/market-scans/latest-published?")) {
      return selectedRun;
    }
    if (target.startsWith("/api/market-scans?")) {
      return { items: [published], total: 1, page: 1, page_size: 100, page_count: 1 };
    }
    if (target.includes("/results?")) {
      resultUrls.push(target);
      const params = new URLSearchParams(target.split("?", 2)[1]);
      const pageNumber = Number(params.get("page"));
      if (resultUrls.length !== 2) {
        return { run: selectedRun, items: [], total: 0, page: 1, page_size: 100, page_count: 0 };
      }
      return populatedPage(selectedRun, pageNumber);
    }
    throw new Error(`unexpected request: ${target}`);
  },
});

await controller.activate();
controller.state.page = 2;
element("marketScanProbabilityMin").disabled = false;
element("marketScanProbabilityMin").value = "70";
await controller.loadLatest();
const sameTokenUrl = resultUrls.at(-1);
assert.match(sameTokenUrl, /page=2/);
assert.match(sameTokenUrl, /probability_horizon=5/);
assert.match(sameTokenUrl, /min_upside_probability=0.7/);

controller.state.page = 2;
element("marketScanProbabilityMin").disabled = false;
element("marketScanProbabilityMin").value = "70";
selectedRun = replacement;
await controller.loadLatest();
const selectorChangedUrl = resultUrls.at(-1);
assert.match(selectorChangedUrl, /market-scans\/41\/results\?page=1/);
assert.doesNotMatch(selectorChangedUrl, /probability_horizon|min_upside_probability/);
assert.equal(controller.state.publishedRun.id, 41);
assert.equal(controller.state.page, 1);
assert.equal(element("marketScanProbabilityMin").disabled, true);
assert.equal(element("marketScanProbabilityMin").value, "");

controller.state.page = 2;
element("marketScanProbabilityMin").disabled = false;
element("marketScanProbabilityMin").value = "70";
identity = marketScanPollingIdentity(replacement, replacement, "official", "replacement-db");
await controller.loadLatest();
const replacementDatabaseUrl = resultUrls.at(-1);
assert.match(replacementDatabaseUrl, /page=1/);
assert.doesNotMatch(replacementDatabaseUrl, /probability_horizon|min_upside_probability/);
controller.deactivate();

function populatedPage(run, pageNumber) {
  const itemCount = pageNumber === 1 ? 100 : 1;
  const offset = (pageNumber - 1) * 100;
  return {
    run, total: 101, page: pageNumber, page_size: 100, page_count: 2,
    items: Array.from({ length: itemCount }, (_, index) => stock(run, offset + index)),
  };
}
function stock(run, index) {
  const code = String(600000 + index).padStart(6, "0");
  return {
    run_id: run.id, rank: index + 1, symbol: `${code}.SH`, code, market: "SH", name: `股票${code}`,
    updated_at: run.updated_at, status: "success", is_st: false, is_new: false, tags: [], metrics: {},
    score: 80, raw_score: 80, trend_score: 80, leader_score: 80, data_quality_score: 80,
    price: 10, data_date: run.data_date, quote_timestamp: "2026-08-14 15:00:00",
    quote_observed_at: "2026-08-14T07:00:00Z", quote_source: "fixture",
    kline_source: "fixture", adjustment_mode: "qfq", reason: null, error: null,
  };
}
'''
    )


def test_market_scan_selector_failure_is_serial_bounded_and_never_reads_results() -> None:
    _run_node_script(
        r'''
import assert from "node:assert/strict";
import { installAppDom, marketScanPollingIdentity } from "./tests/frontend_app_flow_helpers.mjs";
import { createMarketScanController } from "./static/js/market-scan.js";

const { element } = installAppDom({ canvasContext: null });
const timers = new Map();
let timerId = 0;
globalThis.setTimeout = (callback, delay = 0) => {
  const id = ++timerId;
  timers.set(id, { callback, delay });
  return id;
};
globalThis.clearTimeout = (id) => timers.delete(id);
const run = {
  id: 50, status: "success", trigger: "manual", mode: "official", rule_version: "full-market-score-v5",
  as_of: "2026-08-14 16:30:00", data_date: "2026-08-14", quote_date: "2026-08-14",
  scope: "沪市 + 深市 + 北交所当前上市A股", total_count: 1, excluded_count: 0,
  processed_count: 1, success_count: 1, missing_count: 0, skipped_count: 0, retry_count: 0,
  progress_pct: 100, coverage_pct: 100, created_at: "2026-08-14 16:30:00",
  updated_at: "2026-08-14 16:31:00", finished_at: "2026-08-14 16:31:00", message: "扫描完成",
  snapshot_digest: "a".repeat(64), snapshot_seal_origin: "publication",
  snapshot_sealed_at: "2026-08-14 16:31:00",
};
let latestCalls = 0;
let publishedCalls = 0;
let resultCalls = 0;
const controller = createMarketScanController({
  root: document, retryBaseMs: 1000, retryMaxMs: 1000,
  async fetcher(url) {
    const target = String(url);
    if (target.startsWith("/api/market-scans/polling-identity?")) {
      return marketScanPollingIdentity(run, run);
    }
    if (target === "/api/market-scans/latest") {
      latestCalls += 1;
      throw new Error("可信latest读取失败");
    }
    if (target.startsWith("/api/market-scans/latest-published?")) {
      publishedCalls += 1;
      return run;
    }
    if (target.startsWith("/api/market-scans?")) {
      return { items: [], total: 0, page: 1, page_size: 100, page_count: 0 };
    }
    if (target.includes("/results?")) resultCalls += 1;
    throw new Error(`unexpected request: ${target}`);
  },
});

await controller.activate();
assert.equal(latestCalls, 1);
assert.equal(publishedCalls, 0, "serial selector failure started a sibling full verification");
assert.equal(resultCalls, 0);
assert.equal(timers.size, 1, "selector failure scheduled overlapping retries");
assert.equal(controller.state.run, null);
assert.equal(controller.state.publishedRun, null);
assert.match(element("marketScanHeadline").textContent, /最近扫描读取失败.*可信latest读取失败/);
controller.deactivate();
assert.equal(timers.size, 0);

latestCalls = 0;
publishedCalls = 0;
resultCalls = 0;
const mismatchedPublished = {
  ...run,
  market_progress: [{
    market: "SH", total_count: 1, processed_count: 1, success_count: 1,
    missing_count: 0, skipped_count: 0, coverage_pct: 99,
  }],
};
const mismatchController = createMarketScanController({
  root: document, retryBaseMs: 1000, retryMaxMs: 1000,
  async fetcher(url) {
    const target = String(url);
    if (target.startsWith("/api/market-scans/polling-identity?")) {
      return marketScanPollingIdentity(run, run);
    }
    if (target === "/api/market-scans/latest") {
      latestCalls += 1;
      return run;
    }
    if (target.startsWith("/api/market-scans/latest-published?")) {
      publishedCalls += 1;
      return mismatchedPublished;
    }
    if (target.startsWith("/api/market-scans?")) {
      return { items: [], total: 0, page: 1, page_size: 100, page_count: 0 };
    }
    if (target.includes("/results?")) resultCalls += 1;
    throw new Error(`unexpected request: ${target}`);
  },
});
await mismatchController.activate();
assert.equal(latestCalls, 1);
assert.equal(publishedCalls, 1);
assert.equal(resultCalls, 0);
assert.equal(timers.size, 1, "published contract failure scheduled overlapping retries");
assert.equal([...timers.values()][0].delay, 30000, "deterministic failure did not enter idle circuit polling");
assert.match(element("marketScanHeadline").textContent, /market_progress\[0\]\.coverage_pct/);
mismatchController.deactivate();
assert.equal(timers.size, 0);
'''
    )


def test_market_scan_active_progress_identity_stabilizes_then_uses_run_polling() -> None:
    _run_node_script(
        r'''
import assert from "node:assert/strict";
import { installAppDom, marketScanPollingIdentity, waitFor } from "./tests/frontend_app_flow_helpers.mjs";
import { createMarketScanController } from "./static/js/market-scan.js";

const { element } = installAppDom({ canvasContext: null });
const timers = new Map();
let timerId = 0;
globalThis.setTimeout = (callback, delay = 0) => {
  const id = ++timerId;
  timers.set(id, { callback, delay });
  return id;
};
globalThis.clearTimeout = (id) => timers.delete(id);
const published = scanRun(60, "success", "旧榜单", "2026-08-14 16:31:00");
const activeEarly = scanRun(61, "running", "外部任务 10/100", "2026-08-14 16:32:00");
const activeLate = { ...activeEarly, processed_count: 50, success_count: 50, progress_pct: 50,
  coverage_pct: 50, updated_at: "2026-08-14 16:34:00", message: "外部任务 50/100" };
let identityValue = marketScanPollingIdentity(published, published);
let identityCalls = 0;
let selectorCalls = 0;
let resultCalls = 0;
let runPollCalls = 0;
const controller = createMarketScanController({
  root: document, pollIntervalMs: 2000, idlePollIntervalMs: 60000,
  async fetcher(url) {
    const target = String(url);
    if (target.startsWith("/api/market-scans/polling-identity?")) {
      identityCalls += 1;
      if (identityCalls === 3) {
        const early = marketScanPollingIdentity(activeEarly, published);
        identityValue = marketScanPollingIdentity(activeLate, published);
        assert.equal(early.fingerprint, identityValue.fingerprint, "active progress leaked into identity");
        return early;
      }
      return identityValue;
    }
    if (target === "/api/market-scans/latest") {
      selectorCalls += 1;
      return identityCalls >= 3 ? activeLate : published;
    }
    if (target.startsWith("/api/market-scans/latest-published?")) {
      selectorCalls += 1;
      return published;
    }
    if (target.startsWith("/api/market-scans?")) {
      return { items: [published], total: 1, page: 1, page_size: 100, page_count: 1 };
    }
    if (target.startsWith("/api/market-scans/60/results?")) {
      resultCalls += 1;
      return { run: published, items: [], total: 0, page: 1, page_size: 100, page_count: 0 };
    }
    if (target === "/api/market-scans/61") {
      runPollCalls += 1;
      return activeLate;
    }
    throw new Error(`unexpected request: ${target}`);
  },
});

await controller.activate();
assert.equal(identityCalls, 2);
assert.equal(selectorCalls, 2);
assert.equal(resultCalls, 1);
const [idleKey, idleTimer] = [...timers.entries()][0];
timers.delete(idleKey);
idleTimer.callback();
await waitFor(() => controller.state.run?.id === 61, "external active run");
assert.equal(controller.state.run.updated_at, activeLate.updated_at);
assert.equal(controller.state.publishedRun.id, 60);
assert.equal(selectorCalls, 4, "active discovery did not use both trusted selectors exactly once");
assert.equal(resultCalls, 1, "unchanged published slot was re-read during active discovery");
assert.equal([...timers.values()].filter((timer) => timer.delay === 2000).length, 1);

const selectorsBeforeRunPoll = selectorCalls;
const identitiesBeforeRunPoll = identityCalls;
const [activeKey, activeTimer] = [...timers.entries()].find(([_id, timer]) => timer.delay === 2000);
timers.delete(activeKey);
activeTimer.callback();
await waitFor(() => runPollCalls === 1, "active run poll");
assert.equal(selectorCalls, selectorsBeforeRunPoll);
assert.equal(identityCalls, identitiesBeforeRunPoll);
assert.match(element("marketScanHeadline").textContent, /外部任务 50\/100/);
controller.deactivate();

function scanRun(id, status, message, updatedAt) {
  const active = status === "running";
  return {
    id, status, trigger: "manual", mode: "official", rule_version: "full-market-score-v5",
    as_of: "2026-08-14 16:30:00", data_date: "2026-08-14", quote_date: "2026-08-14",
    scope: "沪市 + 深市 + 北交所当前上市A股", total_count: 100, excluded_count: 0,
    processed_count: active ? 10 : 100, success_count: active ? 10 : 100,
    missing_count: 0, skipped_count: 0, retry_count: 0,
    progress_pct: active ? 10 : 100, coverage_pct: active ? 10 : 100,
    created_at: "2026-08-14 16:30:00", updated_at: updatedAt,
    finished_at: active ? null : updatedAt, message,
    snapshot_digest: active ? null : "a".repeat(64), snapshot_seal_origin: active ? null : "publication",
    snapshot_sealed_at: active ? null : updatedAt,
  };
}
'''
    )


def test_market_scan_probability_horizon_switches_are_local_serial_and_retryable() -> None:
    _run_node_script(
        r'''
import assert from "node:assert/strict";
import { installAppDom, marketScanPollingIdentity, waitFor } from "./tests/frontend_app_flow_helpers.mjs";
import { createMarketScanController } from "./static/js/market-scan.js";

const { element } = installAppDom({ canvasContext: null });
const run = scanRun(87);
const resultUrls = [];
const selectorUrls = [];
let delayedResult = null;
let delayedSignal = null;
let failNextResult = false;
const controller = createMarketScanController({
  root: document,
  now: new Date(2026, 7, 15, 0, 38),
  pollIntervalMs: 60000,
  idlePollIntervalMs: 60000,
  resultRetryIntervalMs: 1,
  async fetcher(url, options = {}) {
    const target = String(url);
    if (target.startsWith("/api/market-scans/polling-identity?")) {
      return marketScanPollingIdentity(run, run);
    }
    if (target === "/api/market-scans/latest" || target.startsWith("/api/market-scans/latest-published?")) {
      selectorUrls.push(target);
      return run;
    }
    if (target.startsWith("/api/market-scans?")) {
      return { items: [run], total: 1, page: 1, page_size: 100, page_count: 1 };
    }
    if (target.startsWith("/api/market-scans/87/results?")) {
      resultUrls.push(target);
      if (delayedResult) {
        const pending = delayedResult;
        delayedResult = null;
        delayedSignal = options.signal;
        return pending.promise;
      }
      if (failNextResult) {
        failNextResult = false;
        const error = new Error("全市场冻结快照正在校验，请稍后重试");
        error.status = 503;
        error.retryAfterMs = 1;
        throw error;
      }
      return resultPage();
    }
    throw new Error(`unexpected request: ${target}`);
  },
});
configureHorizons();
await controller.activate();
assert.equal(resultUrls.length, 1);
assert.deepEqual(selectorCounts(), { latest: 1, published: 1 });

selectHorizon(1);
assert.equal(element("marketScanProbabilityStatus").textContent, "正在归档研究样本");
selectHorizon(20);
assert.equal(element("marketScanProbabilityStatus").textContent, "样本外已校准");
selectHorizon(5);
assert.equal(element("marketScanProbabilityStatus").textContent, "研究已生成·样本不足");
assert.equal(resultUrls.length, 1, "unfiltered horizon changes re-read results");
assert.deepEqual(selectorCounts(), { latest: 1, published: 1 });
const hiddenDeferred = deferred();
delayedResult = hiddenDeferred;
const hiddenRead = controller.loadLatest();
await waitFor(() => resultUrls.length === 2 && delayedSignal !== null, "visibility result admission");
const hiddenSignal = delayedSignal;
const hiddenHeavy = { results: resultUrls.length, selectors: selectorCounts() };
controller.setVisible(false);
controller.setVisible(true);
await flushPromises();
assert.equal(hiddenSignal.aborted, false, "visibility transition aborted an admitted result worker");
assert.equal(resultUrls.length, hiddenHeavy.results, "visibility recovery bypassed the admitted worker");
assert.deepEqual(selectorCounts(), hiddenHeavy.selectors);
hiddenDeferred.resolve(resultPage());
await hiddenRead;
await flushPromises();
assert.equal(resultUrls.length, hiddenHeavy.results, "unchanged visibility recovery re-read results");
assert.deepEqual(selectorCounts(), hiddenHeavy.selectors, "unchanged visibility recovery used selectors");
selectHorizon(20);
assert.equal(element("marketScanProbabilityStatus").textContent, "样本外已校准");
assert.equal(resultUrls.length, hiddenHeavy.results, "restored last-good payload did not rerender locally");
const activeOnly = {
  ...scanRun(88), status: "running", finished_at: null,
  snapshot_digest: null, snapshot_seal_origin: null, snapshot_sealed_at: null,
};
controller.state.pollingIdentity = marketScanPollingIdentity(activeOnly, run);
const beforeActiveOnlySwitch = resultUrls.length;
selectHorizon(1);
selectHorizon(5);
assert.equal(resultUrls.length, beforeActiveOnlySwitch, "active-only identity change invalidated the published cache");

selectHorizon(20);
element("marketScanMarket").value = "SH";
element("marketScanKeyword").value = "已应用";
element("marketScanProbabilityMin").value = "60";
await controller.loadResults();
assert.equal(resultUrls.length, 3);
assert.equal(queryAt(-1).probability_horizon, "20");
assert.equal(queryAt(-1).min_upside_probability, "0.6");
element("marketScanMarket").value = "BJ";
element("marketScanKeyword").value = "未提交";
const exactDeferred = deferred();
delayedResult = exactDeferred;
selectHorizon(1);
selectHorizon(5);
selectHorizon(20);
await waitFor(() => resultUrls.length === 4, "single exact unfilter admission");
assert.equal(resultUrls.length, 4, "applied probability filter spawned concurrent unfilters");
assert.equal(delayedSignal.aborted, false, "horizon switch aborted an admitted result read");
assert.equal(element("marketScanProbabilityMin").value, "");
const exactUnfilter = queryAt(-1);
assert.equal(exactUnfilter.market, "SH");
assert.equal(exactUnfilter.keyword, "已应用");
assert.equal(exactUnfilter.page, "1");
assert.equal("probability_horizon" in exactUnfilter, false);
assert.equal("min_upside_probability" in exactUnfilter, false);
exactDeferred.resolve(resultPage());
await waitFor(() => element("marketScanProbabilityStatus").textContent === "样本外已校准", "coalesced unfilter");
await flushPromises();
assert.equal(resultUrls.length, 4);
assert.deepEqual(selectorCounts(), { latest: 2, published: 2 });

element("marketScanProbabilityMin").value = "61";
const directDeferred = deferred();
delayedResult = directDeferred;
const staleDirect = controller.loadResults();
await waitFor(() => resultUrls.length === 5, "direct filtered admission");
selectHorizon(1);
selectHorizon(5);
selectHorizon(20);
assert.equal(delayedSignal.aborted, false, "direct filtered worker was aborted");
directDeferred.resolve(resultPage());
await staleDirect;
await waitFor(() => resultUrls.length === 6, "one direct unfilter follow-up");
await flushPromises();
assert.equal("min_upside_probability" in queryAt(-1), false);
assert.equal(resultUrls.length, 6);

element("marketScanProbabilityMin").value = "62";
await controller.loadResults();
assert.equal(resultUrls.length, 7);
const trustedDeferred = deferred();
delayedResult = trustedDeferred;
const staleTrusted = controller.loadLatest();
await waitFor(() => resultUrls.length === 8, "trusted filtered result read");
selectHorizon(1);
selectHorizon(5);
selectHorizon(20);
assert.equal(delayedSignal.aborted, false, "trusted filtered worker was aborted");
trustedDeferred.resolve(resultPage());
await staleTrusted;
await waitFor(() => resultUrls.length === 9, "one trusted unfilter follow-up");
await flushPromises();
assert.equal("min_upside_probability" in queryAt(-1), false);
assert.deepEqual(selectorCounts(), { latest: 3, published: 3 });

const integrityDeferred = deferred();
delayedResult = integrityDeferred;
const staleIntegrityRead = controller.loadLatest();
await waitFor(() => resultUrls.length === 10, "stale integrity result admission");
const integritySignal = delayedSignal;
const selectorsAtIntegrity = selectorCounts();
controller.setVisible(false);
controller.setVisible(true);
await flushPromises();
assert.equal(integritySignal.aborted, false, "visibility transition aborted the integrity-checking worker");
assert.equal(resultUrls.length, 10, "visibility recovery bypassed the integrity-checking worker");
const trustedRecovery = deferred();
delayedResult = trustedRecovery;
const integrityError = new Error("全市场冻结快照完整性校验失败，已拒绝读取");
integrityError.status = 409;
integrityDeferred.reject(integrityError);
await staleIntegrityRead;
await waitFor(() => resultUrls.length === 11, "trusted recovery after stale integrity failure");
const recoveryRequestCount = resultUrls.length;
selectHorizon(1);
assert.equal(resultUrls.length, recoveryRequestCount, "cleared last-good cache spawned a parallel horizon read");
assert.equal(element("marketScanProbabilityResearch")["aria-busy"], "true");
trustedRecovery.resolve(resultPage());
await waitFor(() => element("marketScanProbabilityStatus").textContent === "正在归档研究样本", "trusted integrity recovery");
assert.deepEqual(selectorCounts(), {
  latest: selectorsAtIntegrity.latest + 1,
  published: selectorsAtIntegrity.published + 1,
});
assert.equal(resultUrls.length, recoveryRequestCount);

selectHorizon(5);
const beforeFailure = resultUrls.length;
const selectorsBeforeFailure = selectorCounts();
failNextResult = true;
await controller.loadResults();
assert.match(element("marketScanResultState").textContent, /已保留上次已验证结果.*1 秒后自动重试/);
assert.doesNotMatch(element("marketScanResultState").textContent, /读取失败/);
assert.equal(element("marketScanProbabilityStatus").textContent, "研究已生成·样本不足");
await waitForTimer(
  () => resultUrls.length === beforeFailure + 2
    && element("marketScanProbabilityStatus").textContent === "研究已生成·样本不足",
  "503 result retry recovery",
);
assert.deepEqual(selectorCounts(), selectorsBeforeFailure, "503 recovery escalated to latest selector");
controller.deactivate();

function configureHorizons() {
  for (const [id, value] of [["marketScanProbabilityHorizon1d", "1"], ["marketScanProbabilityHorizon5d", "5"], ["marketScanProbabilityHorizon20d", "20"]]) {
    element(id).value = value;
    element(id).checked = value === "5";
  }
}
function selectHorizon(value) {
  for (const input of [element("marketScanProbabilityHorizon1d"), element("marketScanProbabilityHorizon5d"), element("marketScanProbabilityHorizon20d")]) {
    input.checked = input.value === String(value);
  }
  const selected = value === 1 ? element("marketScanProbabilityHorizon1d")
    : value === 20 ? element("marketScanProbabilityHorizon20d") : element("marketScanProbabilityHorizon5d");
  selected.listeners.change();
}
function selectorCounts() {
  return {
    latest: selectorUrls.filter((url) => url === "/api/market-scans/latest").length,
    published: selectorUrls.filter((url) => url.startsWith("/api/market-scans/latest-published?")).length,
  };
}
function queryAt(index) {
  const target = resultUrls.at(index);
  return Object.fromEntries(new URLSearchParams(target.split("?", 2)[1]));
}
function scanRun(id) {
  return {
    id, status: "success", trigger: "manual", mode: "official", rule_version: "full-market-score-v5",
    as_of: "2026-08-14 16:30:00", data_date: "2026-08-14", quote_date: "2026-08-14",
    scope: "沪市 + 深市 + 北交所当前上市A股", total_count: 100, excluded_count: 0,
    processed_count: 100, success_count: 100, missing_count: 0, skipped_count: 0, retry_count: 0,
    progress_pct: 100, coverage_pct: 100, created_at: "2026-08-14 16:30:00",
    updated_at: "2026-08-14 16:31:00", finished_at: "2026-08-14 16:31:00",
    snapshot_digest: "8".repeat(64), snapshot_seal_origin: "publication",
    snapshot_sealed_at: "2026-08-14 16:31:00",
  };
}
function resultPage() {
  return { run, items: [], total: 0, page: 1, page_size: 100, page_count: 0, probability_research: research() };
}
function research() {
  const scope = "沪市 + 深市 + 北交所当前上市A股";
  const ruleVersion = "full-market-score-v5";
  const runBinding = {
    binding_status: "verified", legacy: false, run_id: 87, mode: "official", scope,
    rule_version: ruleVersion, quote_date: "2026-08-14", data_date: "2026-08-14",
    scan_rule_hash: "a".repeat(64), production_score_rule_version: ruleVersion,
    production_score_spec_hash: "b".repeat(64),
    cohort_contract: { mode: "official", scope, rule_version: ruleVersion },
  };
  return {
    schema_version: "market-scan-probability-artifact-v3", run_id: 87,
    status: "calibrated_shadow", run_binding: runBinding,
    horizons: {
      "1": { horizon: 1, status: "not_generated", availability: "source_capture_pending", probability: null, filter_qualified: false },
      "5": { horizon: 5, status: "insufficient_data", pipeline_stage: "source_archived", probability: null, filter_qualified: false },
      "20": {
        horizon: 20, status: "calibrated_shadow", probability: 0.7, base_rate: 0.55,
        filter_qualified: true, selection_qualified: true,
        selection_qualification: { passed: true, gates: {} },
      },
    },
  };
}
function deferred() {
  let reject;
  let resolve;
  const promise = new Promise((done, fail) => { resolve = done; reject = fail; });
  return { promise, reject, resolve };
}
async function waitForTimer(condition, label) {
  for (let index = 0; index < 200; index += 1) {
    if (condition()) return;
    await new Promise((resolve) => setTimeout(resolve, 2));
  }
  throw new Error(`timed out waiting for ${label}`);
}
async function flushPromises() {
  for (let index = 0; index < 30; index += 1) await Promise.resolve();
}
'''
    )


def test_market_scan_stale_trust_failure_cannot_clear_cross_context_cache() -> None:
    _run_node_script(
        r'''
import assert from "node:assert/strict";
import { createMarketScanProbabilityHorizonController } from "./static/js/market-scan-probability-horizon-controller.js";

let currentRun = scanRun(87, "official");
const state = {
  browseMode: "official", pollingIdentity: null, renderedResultRunId: 87,
  selectedHistoryRunId: null,
};
const elements = { probabilityMin: { value: "" } };
const rendered = [];
const controller = createMarketScanProbabilityHorizonController({
  clearResetTimer() {}, elements, resultRun: () => currentRun, state,
  view: { renderProbabilityHorizon: (page) => rendered.push(page.run.id) },
});

controller.trustedChainStarted();
currentRun = scanRun(88, "intraday");
state.browseMode = "intraday";
state.selectedHistoryRunId = 88;
state.renderedResultRunId = 88;
controller.remember(resultPage(currentRun), "/api/market-scans/88/results?page=1", null);

const integrityError = new Error("旧正式榜单完整性失败");
integrityError.status = 409;
assert.equal(controller.staleTrustedFailure(integrityError), false);
assert.equal(controller.needsTrustedRefresh(), false);
controller.change();
assert.deepEqual(rendered, [88], "旧上下文 409 清除了新历史/模式上下文缓存");

function scanRun(id, mode) {
  return {
    id, status: "success", trigger: "manual", mode, rule_version: "full-market-score-v5",
    as_of: "2026-08-14 16:30:00", data_date: "2026-08-14", quote_date: "2026-08-14",
    scope: "沪市 + 深市 + 北交所当前上市A股", total_count: 1, excluded_count: 0,
    processed_count: 1, success_count: 1, missing_count: 0, skipped_count: 0, retry_count: 0,
    progress_pct: 100, coverage_pct: 100, created_at: "2026-08-14 16:30:00",
    updated_at: "2026-08-14 16:31:00", finished_at: "2026-08-14 16:31:00",
    snapshot_digest: String(id % 10).repeat(64), snapshot_seal_origin: "publication",
    snapshot_sealed_at: "2026-08-14 16:31:00",
  };
}
function resultPage(run) {
  return { run, items: [], total: 0, page: 1, page_size: 100, page_count: 0 };
}
'''
    )


def test_market_scan_probability_horizon_drops_queued_filtered_query() -> None:
    _run_node_script(
        r'''
import assert from "node:assert/strict";
import { createRequestScope } from "./static/js/api.js";
import { createMarketScanProbabilityHorizonController } from "./static/js/market-scan-probability-horizon-controller.js";

const run = scanRun();
const state = {
  actionBusy: false, browseMode: "official", pollingIdentity: null,
  renderedResultRunId: 87, resultRequest: null, resultRequestSeq: 0,
  runRequest: null, selectedHistoryRunId: null, surfaceActive: true,
};
const calls = [];
const first = deferred();
let desiredQuery = "/api/market-scans/87/results?page=3&market=SH&keyword=kept";
let committed = 0;
const controller = createMarketScanProbabilityHorizonController({
  abortLatest() {},
  beginRequest(scope, sequence) {
    state[scope] = createRequestScope(state[scope]);
    state[sequence] += 1;
    return state[sequence];
  },
  clearResetTimer() {},
  elements: { probabilityMin: { value: "64" } },
  finishRequest(scope, sequence, value) {
    if (state[sequence] !== value) return;
    state[scope]?.dispose();
    state[scope] = null;
  },
  isCurrentRequest: (sequence, value) => state[sequence] === value,
  polling: { clear() {}, handleScopedFailure() { return false; }, resetFailures() {} },
  probabilityPolling: { retryTarget() { return "results"; }, schedule() {} },
  recoverLatest: async () => null,
  async request(query) {
    calls.push(query);
    return calls.length === 1 ? first.promise : resultPage();
  },
  resultErrorMessage: (error) => String(error?.message || "error"),
  resultRun: () => run,
  resultsUrl: () => desiredQuery,
  state,
  view: {
    announce() {}, renderProbabilityHorizon() {}, renderProbabilityResearch() {},
    renderResults() { committed += 1; }, renderResultsLoading() {}, renderResultState() {},
    resetProbabilityResearch() {}, resetResultPresentation() {},
  },
});

const admitted = controller.load();
assert.equal(calls.length, 1);
const admittedSignal = state.resultRequest.signal;
desiredQuery = "/api/market-scans/87/results?page=7&market=SH&keyword=kept&probability_horizon=5&min_upside_probability=0.64";
const dropped = controller.load();
controller.change();
assert.equal(admittedSignal.aborted, false, "queued filter change aborted the admitted worker");
assert.equal(await dropped, null, "queued old-horizon filter was not dropped");
first.resolve(resultPage());
await admitted;
await waitFor(() => calls.length === 2 && state.resultRequest === null, "single queued unfilter");
assert.equal(committed, 1, "stale admitted response committed before the unfilter");
assert.equal(calls.length, 2);
const query = Object.fromEntries(new URLSearchParams(calls[1].split("?", 2)[1]));
assert.deepEqual(query, { page: "1", market: "SH", keyword: "kept" });

function scanRun() {
  return {
    id: 87, status: "success", trigger: "manual", mode: "official", rule_version: "full-market-score-v5",
    as_of: "2026-08-14 16:30:00", data_date: "2026-08-14", quote_date: "2026-08-14",
    scope: "沪市 + 深市 + 北交所当前上市A股", total_count: 1, excluded_count: 0,
    processed_count: 1, success_count: 1, missing_count: 0, skipped_count: 0, retry_count: 0,
    progress_pct: 100, coverage_pct: 100, created_at: "2026-08-14 16:30:00",
    updated_at: "2026-08-14 16:31:00", finished_at: "2026-08-14 16:31:00",
    snapshot_digest: "8".repeat(64), snapshot_seal_origin: "publication",
    snapshot_sealed_at: "2026-08-14 16:31:00",
  };
}
function resultPage() {
  return { run, items: [], total: 0, page: 1, page_size: 100, page_count: 0 };
}
function deferred() {
  let resolve;
  const promise = new Promise((done) => { resolve = done; });
  return { promise, resolve };
}
async function waitFor(condition, label) {
  for (let index = 0; index < 50; index += 1) {
    if (condition()) return;
    await Promise.resolve();
  }
  throw new Error(`timed out waiting for ${label}`);
}
'''
    )


def test_market_scan_heavy_read_tail_is_single_owner_last_intent_and_rejection_safe() -> None:
    _run_node_script(
        r'''
import assert from "node:assert/strict";
import { createMarketScanReadTransition } from "./static/js/market-scan-read-transition.js";

const state = { runRequest: null, runRequestSeq: 0 };
const first = deferred();
const calls = [];
let active = 0;
let peak = 0;
const transition = createMarketScanReadTransition({
  latestSync: { supersede: async () => null },
  probabilityHorizon: { supersede() {} },
  state,
});
const admitted = transition.run(() => owned("first", first.promise));
await waitFor(() => calls.length === 1, "first owner admission");
const stale = transition.transition(() => owned("stale", Promise.resolve()));
const final = transition.transition(() => owned("final", Promise.resolve()));
assert.deepEqual(calls, ["first:start"]);
first.resolve();
assert.equal(await admitted, "first");
assert.equal(await stale, null);
assert.equal(await final, "final");
assert.equal(peak, 1);
assert.deepEqual(calls, ["first:start", "first:end", "final:start", "final:end"]);

const failed = transition.run(() => owned("failed", Promise.reject(new Error("expected"))));
const recovered = transition.run(() => owned("recovered", Promise.resolve()));
assert.equal((await Promise.allSettled([failed]))[0].status, "rejected");
assert.equal(await recovered, "recovered");
assert.equal(peak, 1, "rejected owner did not release the heavy-read tail");

async function owned(name, promise) {
  active += 1;
  peak = Math.max(peak, active);
  calls.push(`${name}:start`);
  try {
    await promise;
    return name;
  } finally {
    calls.push(`${name}:end`);
    active -= 1;
  }
}
function deferred() {
  let resolve;
  const promise = new Promise((done) => { resolve = done; });
  return { promise, resolve };
}
async function waitFor(condition, label) {
  for (let index = 0; index < 20; index += 1) {
    if (condition()) return;
    await Promise.resolve();
  }
  throw new Error(`timed out waiting for ${label}`);
}
'''
    )


def test_market_scan_probability_stale_tail_resolves_without_http_or_unhandled_rejection() -> None:
    _run_node_script(
        r'''
import assert from "node:assert/strict";
import { createRequestScope } from "./static/js/api.js";
import { createMarketScanProbabilityHorizonController } from "./static/js/market-scan-probability-horizon-controller.js";
import { createMarketScanReadTransition } from "./static/js/market-scan-read-transition.js";

const run = scanRun();
const state = {
  actionBusy: false, browseMode: "official", pollingIdentity: null,
  renderedResultRunId: 87, resultRequest: null, resultRequestSeq: 0,
  runRequest: null, runRequestSeq: 0, selectedHistoryRunId: null, surfaceActive: true,
};
const blocker = deferred();
const calls = [];
const unhandled = [];
let blockerStarted = false;
let committed = 0;
let transition = null;
process.on("unhandledRejection", (error) => unhandled.push(error));
const horizon = createMarketScanProbabilityHorizonController({
  beginRequest(scope, sequence) {
    state[scope] = createRequestScope(state[scope]);
    state[sequence] += 1;
    return state[sequence];
  },
  clearResetTimer() {},
  detachOwnedRead: (options) => transition.invalidateOwner(options),
  elements: { probabilityMin: { value: "" } },
  finishRequest(scope, sequence, value) {
    if (state[sequence] !== value) return;
    state[scope]?.dispose();
    state[scope] = null;
  },
  isCurrentRequest: (sequence, value) => state[sequence] === value,
  polling: { clear() {}, handleScopedFailure() { return false; }, resetFailures() {} },
  probabilityPolling: { retryTarget() { return "results"; }, schedule() {} },
  recoverLatest: async () => null,
  async request(query) { calls.push(query); return resultPage(); },
  resultErrorMessage: (error) => String(error?.message || "error"),
  resultRun: () => run,
  resultsUrl: () => "/api/market-scans/87/results?page=1",
  state,
  view: {
    announce() {}, renderProbabilityHorizon() {}, renderProbabilityResearch() {},
    renderResults() { committed += 1; }, renderResultsLoading() {}, renderResultState() {},
    resetProbabilityResearch() {}, resetResultPresentation() {},
  },
  withHeavyRead: (operation) => transition.run(operation),
});
transition = createMarketScanReadTransition({
  latestSync: { supersede: async () => null }, probabilityHorizon: horizon, state,
});

const admitted = transition.run(async () => {
  blockerStarted = true;
  await blocker.promise;
});
await waitFor(() => blockerStarted, "blocking owner admission");
const staleRefresh = horizon.load({
  horizonRefresh: true,
  query: "/api/market-scans/87/results?page=1&market=SH",
});
const finalLoad = horizon.load({
  query: "/api/market-scans/87/results?page=1&market=BJ&keyword=final",
});
assert.deepEqual(calls, [], "queued stale refresh issued HTTP before owning the tail");
blocker.resolve();
const [admittedValue, staleValue, finalValue] = await Promise.all([admitted, staleRefresh, finalLoad]);
assert.equal(admittedValue, undefined);
assert.equal(staleValue, null);
assert.equal(finalValue.run.id, 87);
assert.deepEqual(calls, ["/api/market-scans/87/results?page=1&market=BJ&keyword=final"]);
assert.equal(committed, 1);
for (let index = 0; index < 10; index += 1) await Promise.resolve();
assert.deepEqual(unhandled, []);

function resultPage() {
  return { run, items: [], total: 0, page: 1, page_size: 100, page_count: 0 };
}
function scanRun() {
  return {
    id: 87, status: "success", trigger: "manual", mode: "official", rule_version: "full-market-score-v5",
    as_of: "2026-08-14 16:30:00", data_date: "2026-08-14", quote_date: "2026-08-14",
    scope: "沪市 + 深市 + 北交所当前上市A股", total_count: 1, excluded_count: 0,
    processed_count: 1, success_count: 1, missing_count: 0, skipped_count: 0, retry_count: 0,
    progress_pct: 100, coverage_pct: 100, created_at: "2026-08-14 16:30:00",
    updated_at: "2026-08-14 16:31:00", finished_at: "2026-08-14 16:31:00",
    snapshot_digest: "8".repeat(64), snapshot_seal_origin: "publication",
    snapshot_sealed_at: "2026-08-14 16:31:00",
  };
}
function deferred() {
  let resolve;
  const promise = new Promise((done) => { resolve = done; });
  return { promise, resolve };
}
async function waitFor(condition, label) {
  for (let index = 0; index < 20; index += 1) {
    if (condition()) return;
    await Promise.resolve();
  }
  throw new Error(`timed out waiting for ${label}`);
}
'''
    )


def test_market_scan_poll_run_failure_releases_owner_before_latest_recovery() -> None:
    _run_node_script(
        r'''
import assert from "node:assert/strict";
import { installAppDom, marketScanPollingIdentity, waitFor } from "./tests/frontend_app_flow_helpers.mjs";
import { createMarketScanController } from "./static/js/market-scan.js";

installAppDom({ canvasContext: null });
const timers = installFakeTimers();
const failedRun = deferred();
let identityRun = scanRun(70);
let latestCalls = 0;
let runSignal = null;
let activeHeavy = 0;
let peakHeavy = 0;
const targets = [];
const controller = createMarketScanController({
  root: document,
  failureFallbackThreshold: 1,
  pollIntervalMs: 5,
  async fetcher(url, options = {}) {
    const target = String(url);
    targets.push(target);
    if (target.startsWith("/api/market-scans/polling-identity?")) {
      return marketScanPollingIdentity(identityRun, null);
    }
    return ownedHeavy(async () => {
      if (target === "/api/market-scans/latest") {
        latestCalls += 1;
        return identityRun;
      }
      if (target.startsWith("/api/market-scans/latest-published?")) return null;
      if (target.startsWith("/api/market-scans?")) {
        return { items: [], total: 0, page: 1, page_size: 100, page_count: 0 };
      }
      if (target === "/api/market-scans/70") {
        runSignal = options.signal;
        return failedRun.promise;
      }
      throw new Error(`unexpected request: ${target}`);
    });
  },
});

await controller.activate();
assert.equal(latestCalls, 1);
timers.fireNext();
for (let index = 0; index < 100 && runSignal === null; index += 1) await Promise.resolve();
assert.ok(runSignal, `active run was not admitted: ${JSON.stringify(targets)}`);
assert.equal(latestCalls, 1, "latest recovery overlapped the active run request");
identityRun = scanRun(71);
const failure = new Error("临时网络失败");
failedRun.reject(failure);
for (let index = 0; index < 200 && !(latestCalls === 2 && controller.state.run?.id === 71); index += 1) {
  await Promise.resolve();
}
assert.equal(
  `${latestCalls}:${controller.state.run?.id}`,
  "2:71",
  `post-owner recovery stalled: ${JSON.stringify(targets)}`,
);
assert.equal(runSignal.aborted, false);
assert.equal(peakHeavy, 1, "run failure recovery overlapped capacity-one heavy reads");
assert.equal(controller.state.consecutiveFailures, 0);
controller.deactivate();

async function ownedHeavy(operation) {
  activeHeavy += 1;
  peakHeavy = Math.max(peakHeavy, activeHeavy);
  try { return await operation(); }
  finally { activeHeavy -= 1; }
}
function scanRun(id) {
  return {
    id, status: "running", trigger: "manual", mode: "official", rule_version: "full-market-score-v5",
    as_of: "2026-08-14 16:30:00", data_date: "2026-08-14", quote_date: "2026-08-14",
    scope: "沪市 + 深市 + 北交所当前上市A股", total_count: 100, excluded_count: 0,
    processed_count: 1, success_count: 1, missing_count: 0, skipped_count: 0, retry_count: 0,
    progress_pct: 1, coverage_pct: 1, created_at: "2026-08-14 16:30:00",
    updated_at: `2026-08-14 17:${String(id % 60).padStart(2, "0")}:00`,
    finished_at: null, message: `running ${id}`,
    snapshot_digest: null, snapshot_seal_origin: null, snapshot_sealed_at: null,
  };
}
function deferred() {
  let reject;
  const promise = new Promise((_resolve, fail) => { reject = fail; });
  return { promise, reject };
}
function installFakeTimers() {
  let nextId = 0;
  const scheduled = new Map();
  globalThis.setTimeout = (callback, delay = 0) => {
    const id = ++nextId;
    scheduled.set(id, { callback, delay });
    return id;
  };
  globalThis.clearTimeout = (id) => scheduled.delete(id);
  return {
    fireNext() {
      assert.equal(scheduled.size, 1);
      const [id, entry] = [...scheduled.entries()][0];
      scheduled.delete(id);
      entry.callback();
    },
  };
}
'''
    )


def test_market_scan_filter_waits_for_active_run_owner_without_abort() -> None:
    _run_node_script(
        r'''
import assert from "node:assert/strict";
import { installAppDom, marketScanPollingIdentity, waitFor } from "./tests/frontend_app_flow_helpers.mjs";
import { createMarketScanController } from "./static/js/market-scan.js";

const { element } = installAppDom({ canvasContext: null });
const timers = installFakeTimers();
const published = scanRun(87, "success");
const active = scanRun(88, "running");
const activeRead = deferred();
const resultQueries = [];
const targets = [];
let runSignal = null;
const controller = createMarketScanController({
  root: document,
  pollIntervalMs: 5,
  async fetcher(url, options = {}) {
    const target = String(url);
    targets.push(target);
    if (target.startsWith("/api/market-scans/polling-identity?")) {
      return marketScanPollingIdentity(active, published);
    }
    if (target === "/api/market-scans/latest") return active;
    if (target.startsWith("/api/market-scans/latest-published?")) return published;
    if (target.startsWith("/api/market-scans?")) {
      return { items: [published], total: 1, page: 1, page_size: 100, page_count: 1 };
    }
    if (target === "/api/market-scans/88") {
      runSignal = options.signal;
      return activeRead.promise;
    }
    if (target.startsWith("/api/market-scans/87/results?")) {
      resultQueries.push(target);
      return resultPage(target.includes("market=BJ") ? "用户最终筛选" : "初始榜单");
    }
    throw new Error(`unexpected request: ${target}`);
  },
});

await controller.activate();
assert.equal(resultQueries.length, 1, JSON.stringify(targets));
timers.fireNext();
await waitFor(() => runSignal !== null, "active run owner");
element("marketScanMarket").value = "BJ";
const filtered = controller.loadResults();
await flushPromises();
assert.equal(runSignal.aborted, false, "filter intent aborted the admitted run worker");
assert.equal(resultQueries.length, 1, "filter intent bypassed the admitted run worker");
activeRead.resolve(active);
await filtered;
assert.equal(resultQueries.length, 2);
assert.match(resultQueries.at(-1), /market=BJ/);
assert.match(element("marketScanRows").innerHTML, /用户最终筛选/);
controller.deactivate();

function scanRun(id, status) {
  const terminal = status === "success";
  return {
    id, status, trigger: "manual", mode: "official", rule_version: "full-market-score-v5",
    as_of: "2026-08-14 16:30:00", data_date: "2026-08-14", quote_date: "2026-08-14",
    scope: "沪市 + 深市 + 北交所当前上市A股",
    total_count: terminal ? 1 : 100, excluded_count: 0,
    processed_count: 1, success_count: 1, missing_count: 0, skipped_count: 0, retry_count: 0,
    progress_pct: terminal ? 100 : 1, coverage_pct: terminal ? 100 : 1,
    created_at: "2026-08-14 16:30:00",
    updated_at: terminal ? "2026-08-14 16:31:00" : "2026-08-14 16:32:00",
    finished_at: terminal ? "2026-08-14 16:31:00" : null, message: `run ${id}`,
    snapshot_digest: terminal ? "8".repeat(64) : null,
    snapshot_seal_origin: terminal ? "publication" : null,
    snapshot_sealed_at: terminal ? "2026-08-14 16:31:00" : null,
  };
}
function resultPage(name) {
  return {
    run: published, total: 1, page: 1, page_size: 100, page_count: 1,
    items: [{
      run_id: 87, rank: 1, symbol: "600001.SH", code: "600001", market: "SH", name,
      updated_at: published.updated_at, status: "success", is_st: false, is_new: false,
      tags: [], metrics: {}, score: 80, raw_score: 80, trend_score: 80, leader_score: 80,
      data_quality_score: 80, price: 10, data_date: published.data_date,
      quote_timestamp: "2026-08-14 15:00:00", quote_observed_at: "2026-08-14T07:00:00Z",
      quote_source: "fixture", kline_source: "fixture", adjustment_mode: "qfq",
      reason: null, error: null,
    }],
  };
}
function deferred() {
  let resolve;
  const promise = new Promise((done) => { resolve = done; });
  return { promise, resolve };
}
function installFakeTimers() {
  let nextId = 0;
  const scheduled = new Map();
  globalThis.setTimeout = (callback, delay = 0) => {
    const id = ++nextId;
    scheduled.set(id, { callback, delay });
    return id;
  };
  globalThis.clearTimeout = (id) => scheduled.delete(id);
  return {
    fireNext() {
      assert.equal(scheduled.size, 1);
      const [id, entry] = [...scheduled.entries()][0];
      scheduled.delete(id);
      entry.callback();
    },
  };
}
async function flushPromises() {
  for (let index = 0; index < 80; index += 1) await Promise.resolve();
}
'''
    )


def _run_node_script(script: str) -> None:
    fixed_clock = r'''
const NativeDate = globalThis.Date;
globalThis.Date = class extends NativeDate {
  constructor(...args) {
    super(...(args.length ? args : [2026, 6, 17, 16, 30]));
  }
};
'''
    completed = subprocess.run(
        ["node", "--input-type=module", "-e", fixed_clock + script],
        cwd=ROOT,
        check=False,
        text=True,
        capture_output=True,
    )
    assert completed.returncode == 0, completed.stderr
