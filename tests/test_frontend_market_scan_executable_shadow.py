from __future__ import annotations

from copy import deepcopy
import json
from pathlib import Path
import subprocess

from app.models.market_scan_executable_shadow import (
    ExecutableCandidateShadowReport,
    executable_candidate_shadow_digest,
)
from app.services.market_scan_executable_shadow import (
    executable_candidate_shadow_spec,
)


ROOT = Path(__file__).resolve().parents[1]


def test_frontend_shadow_contract_accepts_backend_shape_and_rejects_tampering() -> None:
    payload = _payload()
    v5_payload = deepcopy(payload)
    v5_payload["evidence"]["production_score_rule_version"] = "full-market-score-v5"  # type: ignore[index]
    v5_payload["canonical_digest"] = executable_candidate_shadow_digest(v5_payload)
    mutations: list[dict[str, object]] = []

    nested_extra = deepcopy(payload)
    nested_extra["candidate_preview"][0]["untrusted_probability"] = 0.99  # type: ignore[index]
    nested_extra["canonical_digest"] = executable_candidate_shadow_digest(nested_extra)
    mutations.append(nested_extra)

    wrong_rule = deepcopy(payload)
    wrong_rule["evidence"]["production_score_rule_version"] = "full-market-score-v6"  # type: ignore[index]
    wrong_rule["canonical_digest"] = executable_candidate_shadow_digest(wrong_rule)
    mutations.append(wrong_rule)

    inconsistent_cost = deepcopy(payload)
    inconsistent_cost["exposure_audit"]["estimated_round_trip_cost_cny"] = 99.0  # type: ignore[index]
    mutations.append(inconsistent_cost)

    malformed_digest = deepcopy(payload)
    malformed_digest["canonical_digest"] = "not-a-digest"
    mutations.append(malformed_digest)

    script = r"""
      import { validateExecutableShadowReport } from "./static/js/market-scan-executable-shadow-contracts.js";
      const payload = JSON.parse(process.argv[1]);
      const v5Payload = JSON.parse(process.argv[2]);
      const mutations = JSON.parse(process.argv[3]);
      validateExecutableShadowReport(payload, 77);
      validateExecutableShadowReport(v5Payload, 77);
      for (const mutation of mutations) {
        let rejected = false;
        try { validateExecutableShadowReport(mutation, 77); } catch { rejected = true; }
        if (!rejected) throw new Error("tampered Shadow response was accepted");
      }
      let wrongRunRejected = false;
      try { validateExecutableShadowReport(payload, 78); } catch { wrongRunRejected = true; }
      if (!wrongRunRejected) throw new Error("request/response run binding was not enforced");
    """
    _run_node(script, payload, v5_payload, mutations)


def test_shadow_controller_is_explicit_abortable_and_renders_null_as_missing() -> None:
    payload = _payload()
    payload["exposure_audit"]["average_risk_score"] = None  # type: ignore[index]
    payload["canonical_digest"] = executable_candidate_shadow_digest(payload)
    script = r"""
      import { createMarketScanExecutableShadowController } from "./static/js/market-scan-executable-shadow-controller.js";
      const payload = JSON.parse(process.argv[1]);
      class Element {
        constructor(id) { this.id = id; this.value = ""; this.hidden = false; this.disabled = false; this.dataset = {}; this.listeners = {}; this.open = true; this.innerHTML = ""; this.textContent = ""; }
        addEventListener(name, callback) { this.listeners[name] = callback; }
        setAttribute(name, value) { this[name] = value; }
      }
      const ids = ["strategyLab", "strategyExecutableShadow", "executableShadowForm", "executableShadowRunId", "executableShadowNotional", "executableShadowUseCurrent", "executableShadowGenerate", "executableShadowCancel", "executableShadowStatus", "executableShadowResult"];
      const elements = Object.fromEntries(ids.map((id) => [id, new Element(id)]));
      elements.executableShadowNotional.value = "1000000";
      const root = { getElementById: (id) => elements[id] || null };
      let calls = 0;
      let resolver = null;
      const fetcher = () => { calls += 1; return resolver ? new Promise((resolve) => { resolver.resolve = resolve; }) : Promise.resolve(payload); };
      const currentRun = { id: 77, status: "success", mode: "official", scope: "沪市 + 深市 + 北交所当前上市A股" };
      const controller = createMarketScanExecutableShadowController({ root, fetcher, getCurrentRun: () => currentRun });
      if (calls !== 0) throw new Error("controller auto-requested during construction");
      controller.useCurrentRun();
      if (calls !== 0 || elements.executableShadowRunId.value !== "77") throw new Error("using current run performed network work");
      await controller.load();
      if (calls !== 1 || controller.state.report?.status !== "research_shadow") throw new Error("explicit request did not render report");
      const html = elements.executableShadowResult.innerHTML;
      for (const text of ["ADV unavailable", "冻结当日成交额", "生产原排名", "Shadow 顺序", "not_generated", "未验证 Alpha"]) {
        if (!html.includes(text)) throw new Error(`required honest label missing: ${text}`);
      }
      if (!html.includes("加权平均风险</span><strong>--")) throw new Error("null risk was rendered as a numeric zero");

      resolver = {};
      const pending = controller.load();
      if (!controller.cancel()) throw new Error("active request could not be cancelled");
      resolver.resolve(payload);
      await pending;
      if (controller.state.report !== payload && controller.state.report?.canonical_digest !== payload.canonical_digest) throw new Error("previous good report was unexpectedly replaced");
      if (elements.executableShadowResult.dataset.state !== "idle") throw new Error("cancelled request overwrote local cancelled state");
    """
    _run_node(script, payload)


def test_shadow_controller_is_inert_for_partial_or_legacy_dom() -> None:
    script = r"""
      import { createMarketScanExecutableShadowController } from "./static/js/market-scan-executable-shadow-controller.js";
      let requests = 0;
      let listeners = 0;
      let writes = 0;
      const tracked = {
        dataset: {},
        addEventListener() { listeners += 1; },
        setAttribute() { writes += 1; },
      };
      const partialRoot = {
        getElementById(id) {
          return id === "strategyExecutableShadow" ? tracked : null;
        },
      };
      const partial = createMarketScanExecutableShadowController({
        root: partialRoot,
        fetcher: async () => { requests += 1; return {}; },
      });
      await partial.load();
      partial.useCurrentRun();
      partial.cancel();
      partial.destroy();

      const legacyElements = new Map();
      const legacyRoot = {
        getElementById(id) {
          if (!legacyElements.has(id)) {
            legacyElements.set(id, {
              dataset: {}, value: "", disabled: false, hidden: false,
              addEventListener() { listeners += 1; },
            });
          }
          return legacyElements.get(id);
        },
      };
      const legacy = createMarketScanExecutableShadowController({
        root: legacyRoot,
        fetcher: async () => { requests += 1; return {}; },
      });
      await legacy.load();
      legacy.useCurrentRun();
      legacy.cancel();
      legacy.destroy();
      if (requests !== 0 || listeners !== 0 || writes !== 0) {
        throw new Error(`incomplete DOM was not inert: ${requests}/${listeners}/${writes}`);
      }
    """
    _run_node(script)


def test_shadow_ui_wiring_stays_on_demand_and_locally_scoped() -> None:
    html = (ROOT / "static/index.html").read_text(encoding="utf-8")
    app = (ROOT / "static/app.js").read_text(encoding="utf-8")
    controller = (ROOT / "static/js/market-scan-executable-shadow-controller.js").read_text(encoding="utf-8")
    styles = (ROOT / "static/css/market-scan.css").read_text(encoding="utf-8")

    assert 'id="strategyExecutableShadow"' in html
    assert 'id="executableShadowGenerate"' in html
    assert 'id="executableShadowStatus" role="status"' in html
    assert "只读 · 不自动运行" in html
    assert "createMarketScanExecutableShadowController" in app
    assert "void marketScanExecutableShadowController" not in app
    assert 'addEventListener("submit"' in controller
    assert "createRequestScope(state.requestScope)" in controller
    assert "validateExecutableShadowReport(payload, input.runId)" in controller
    assert ".executable-shadow-table-wrap" in styles
    assert "overflow-x: auto" in styles


def _run_node(script: str, *values: object) -> None:
    subprocess.run(
        [
            "node",
            "--input-type=module",
            "-e",
            script,
            *(json.dumps(value, ensure_ascii=False) for value in values),
        ],
        cwd=ROOT,
        check=True,
    )


def _payload() -> dict[str, object]:
    selected = _candidate(
        "600519.SH",
        "贵州茅台",
        status="selected",
        original_rank=12,
        utility_rank=1,
        risk=31.5,
        target_weight=0.05,
        target_quantity=100,
        gross_amount=150_000.0,
        cost=312.5,
    )
    rejected = _candidate(
        "000001.SZ",
        "平安银行",
        status="rejected",
        original_rank=5,
        utility_rank=2,
        risk=72.0,
        hard_filter_failures=["risk <= 55.0"],
    )
    payload: dict[str, object] = {
        "schema_version": "market-scan-executable-candidate-shadow-v2",
        "status": "research_shadow",
        "efficacy_status": "not_generated",
        "production_effect": "none",
        "production_ranking_mutated": False,
        "database_write_performed": False,
        "evidence": {
            "run_id": 77,
            "status": "success",
            "mode": "official",
            "scope": "沪市 + 深市 + 北交所当前上市A股",
            "data_date": "2026-08-12",
            "quote_date": "2026-08-12",
            "scan_rule_version": "full-market-scan-v2",
            "production_score_rule_version": "full-market-score-v4",
            "production_score_spec_hash": "a" * 64,
            "result_count": 2,
            "successful_result_count": 2,
            "verified_point_in_time_count": 1,
        },
        "strategy_contract_version": "executable-candidate-shadow-spec-v2",
        "strategy_fingerprint": "b" * 64,
        "strategy_spec": executable_candidate_shadow_spec().model_dump(mode="json"),
        "gate_policy": {
            "exclude_st": True,
            "exclude_new": True,
            "suspension_evidence": "frozen_daily_amount_and_reason_proxy",
            "price_limit_evidence": "frozen_daily_single_price_proxy",
            "minimum_listing_days": 120,
            "minimum_history_sessions": 61,
            "minimum_amount_cny": 100_000_000.0,
            "minimum_tradability_score": 55.0,
            "maximum_risk_score": 55.0,
            "adv_evidence_status": "unavailable",
            "capacity_basis": "frozen_session_amount_participation_proxy",
            "maximum_notional_share_of_session_amount": 0.001,
        },
        "summary": {
            "status": "ready",
            "no_trade": False,
            "no_trade_reasons": [],
            "evaluated_count": 2,
            "eligible_count": 1,
            "selected_count": 1,
            "rejected_count": 1,
            "adjusted_count": 0,
            "unfilled_count": 0,
            "target_invested_weight": 0.05,
            "estimated_turnover": 0.05,
            "estimated_round_trip_cost_cny": 312.5,
            "residual_cash_cny": 850_000.0,
            "evidence_verified_count": 1,
            "replacement_attempt_count": 1,
            "pool_exhausted": True,
            "underinvested_reason": "候选池在约束后耗尽，仅入选 1/30 只",
            "notes": ["仅为冻结截面研究投影"],
        },
        "selected": [selected],
        "candidate_preview": [selected, rejected],
        "candidate_total": 2,
        "exposure_audit": {
            "selected_count": 1,
            "selected_weight": 0.05,
            "top10_weight": 0.05,
            "industry_weights": {"白酒": 0.05},
            "board_weights": {"sh_main": 0.05},
            "average_risk_score": 31.5,
            "average_tradability_score": 82.0,
            "estimated_round_trip_cost_cny": 312.5,
            "estimated_turnover": 0.05,
        },
        "draft_result_digest": "c" * 64,
        "limitations": ["不是已验证alpha，不连接券商。"],
    }
    payload["canonical_digest"] = executable_candidate_shadow_digest(payload)
    return ExecutableCandidateShadowReport.model_validate(payload).model_dump(mode="json")


def _candidate(
    symbol: str,
    name: str,
    *,
    status: str,
    original_rank: int,
    utility_rank: int,
    risk: float,
    target_weight: float = 0.0,
    target_quantity: int = 0,
    gross_amount: float = 0.0,
    cost: float = 0.0,
    hard_filter_failures: list[str] | None = None,
) -> dict[str, object]:
    return {
        "symbol": symbol,
        "code": symbol[:6],
        "name": name,
        "board": "sh_main" if symbol.endswith(".SH") else "sz_main",
        "industry": "白酒" if symbol.endswith(".SH") else "银行",
        "original_rank": original_rank,
        "utility_rank": utility_rank,
        "utility_score": 81.0,
        "alpha_1d": 60.0,
        "alpha_5d": 70.0,
        "alpha_20d": 75.0,
        "confidence": 80.0,
        "risk": risk,
        "tradability": 82.0,
        "status": status,
        "target_weight": target_weight,
        "target_quantity": target_quantity,
        "estimated_gross_amount_cny": gross_amount,
        "estimated_round_trip_cost_cny": cost,
        "evidence_verified": True,
        "hard_filter_failures": hard_filter_failures or [],
        "reasons": ["冻结批次只读评估"],
        "rank_change_reason": "保留生产原始排名，独立计算 Shadow 顺序",
    }
