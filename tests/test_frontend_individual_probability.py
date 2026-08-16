from __future__ import annotations

import json
import subprocess
from pathlib import Path

from app.models.individual_probability import IndividualUpsideProbabilityReport


ROOT = Path(__file__).resolve().parents[1]


def test_pydantic_response_dump_matches_javascript_contract() -> None:
    counts = {
        "observation_count": 180_000,
        "eligible_observation_count": 170_000,
        "independent_session_count": 284,
        "out_of_sample_observation_count": 60_000,
        "out_of_sample_session_count": 120,
        "evaluated_fold_count": 2,
    }
    metrics = {
        "brier_score": 0.196,
        "reference_brier_score": 0.223,
        "brier_skill_score": 0.12107623318385652,
        "ece": 0.034,
        "auc": 0.681,
        "actual_positive_rate": 0.514,
        "actual_positive_rate_ci_95": {"lower": 0.49, "upper": 0.54, "level": 0.95},
        "bin_monotonic": True,
        "highest_bin_above_base_rate": True,
        "selection_gate_version": "market-scan-probability-selection-gates-v1",
        "calibration_bin_count": 5,
        "minimum_calibration_bin_session_count": 20,
        "all_folds_positive_brier_skill": True,
    }
    horizons = []
    for day, status in ((2, "calibrated_shadow"), (3, "insufficient_data"), (4, "not_generated")):
        horizons.append(
            {
                "display_day": day,
                "holding_sessions": day - 1,
                "status": status,
                "probability": 0.612 if status == "calibrated_shadow" else None,
                "confidence_interval": (
                    {"lower": 0.56, "upper": 0.66, "level": 0.95}
                    if status == "calibrated_shadow"
                    else None
                ),
                "base_rate": 0.514,
                "counts": counts,
                "calibration_metrics": metrics,
                "training_cutoff": "2026-08-11",
                "model_version": "shadow-up-probability-logit-l2-v2-convergence-required",
                "feature_version": "historical-replay-common-ohlcv-v1",
                "evidence_digest": "d" * 64,
                "gate_reasons": [] if status == "calibrated_shadow" else ["evidence_gate_not_satisfied"],
            }
        )
    response = IndividualUpsideProbabilityReport.model_validate(
        {
            "symbol": "600519.SH",
            "signal_date": "2026-08-12",
            "generated_at": "2026-08-12T18:00:00+08:00",
            "status": "calibrated_shadow",
            "target_contract": {
                "version": "individual-upside-net-return-label-v1",
                "signal_cutoff": "completed_session_D_close",
                "entry": "D_plus_1_official_daily_open_proxy_no_shift",
                "exits": {
                    "D+2": "D_plus_2_close_holding_session_1",
                    "D+3": "D_plus_3_close_holding_session_2",
                    "D+4": "D_plus_4_close_holding_session_3",
                },
                "target": "round_trip_net_return_after_declared_costs_gt_0_daily_bar_proxy",
                "cost_profile": "base-a0441d84df44",
                "execution_notional": 100_000,
                "feature_version": "historical-replay-common-ohlcv-v1",
                "point_in_time_required": True,
            },
            "horizons": horizons,
            "evidence": {
                "assessment_digest": "b" * 64,
                "history_manifest_digest": "c" * 64,
                "history_database_sha256": "a" * 64,
                "official_pit_session_count": 288,
                "required_official_pit_session_count": 288,
                "historical_replay_session_count": 288,
                "historical_replay_official": True,
                "selection_qualified": True,
            },
            "limitations": ["shadow_only"],
            "production_effect": "none",
        }
    )
    payload = json.dumps(response.model_dump(mode="json"), ensure_ascii=False)
    script = f'''
      import {{ validateIndividualProbabilityReport }} from "./static/js/individual-probability-contracts.js";
      const payload = {payload};
      const validated = validateIndividualProbabilityReport(payload, "600519.SH");
      if (validated.horizons[0].confidence_interval.level !== 0.95) throw new Error("typed point interval lost");
      if (validated.horizons[0].calibration_metrics.actual_positive_rate_ci_95.level !== 0.95) throw new Error("typed diagnostic interval lost");
    '''
    _run_node_script(script)


def test_individual_probability_contract_and_view_are_strict_and_fail_closed() -> None:
    script = r'''
      import { validateIndividualProbabilityReport } from "./static/js/individual-probability-contracts.js";
      import { createIndividualProbabilityView } from "./static/js/individual-probability-view.js";

      const payload = probabilityReport("600519.SH");
      const validated = validateIndividualProbabilityReport(payload, "600519");
      if (validated.horizons.map((item) => item.display_day).join(",") !== "2,3,4") {
        throw new Error("horizons were not frozen to D+2/D+3/D+4");
      }

      const elements = new Map();
      const root = { getElementById(id) { return elements.get(id) || null; } };
      for (const id of [
        "individualProbabilityResearch", "individualProbabilityCards", "individualProbabilityTarget",
        "individualProbabilityEvidence", "individualProbabilityLimitations",
        "individualProbabilityAnnouncement", "individualProbabilityRetry",
      ]) elements.set(id, element(id));
      elements.get("individualProbabilityResearch").dataset.individualProbabilitySurface = "true";
      const view = createIndividualProbabilityView(root);
      view.renderReport(validated);
      const html = elements.get("individualProbabilityCards").innerHTML;
      for (const marker of [
        "D+2", "D+3", "D+4", "61.2%", "56.0%–66.0%", "51.4%", "284 / 180000",
        "shadow-up-probability-logit-l2-v2-convergence-required / historical-replay-common-ohlcv-v1", "非官方历史 OOS 诊断（不是当前个股概率）",
        "Brier skill", "0.121", "AUC", "0.681", "ECE", "0.034",
      ]) if (!html.includes(marker)) throw new Error(`missing probability marker: ${marker}\n${html}`);
      const d3 = html.match(/<article[^>]+data-display-day="3"[\s\S]*?<\/article>/)?.[0] || "";
      const d4 = html.match(/<article[^>]+data-display-day="4"[\s\S]*?<\/article>/)?.[0] || "";
      if (!d3.includes("样本不足") || !d3.includes("—") || d3.includes("0.0%") || d3.includes("50.0%")) {
        throw new Error(`insufficient horizon leaked a fake probability: ${d3}`);
      }
      if (!d4.includes("尚未生成") || !d4.includes("—")) throw new Error(`not-generated horizon was not explicit: ${d4}`);
      if (!elements.get("individualProbabilityTarget").textContent.includes("D+1 官方日K开盘价代理（不保证成交）")) {
        throw new Error("executable entry/exit target was missing");
      }
      if (!elements.get("individualProbabilityTarget").textContent.includes("扣除声明成本后的日K代理净收益 > 0")) {
        throw new Error("net-return probability event was missing");
      }
      const evidence = elements.get("individualProbabilityEvidence").innerHTML;
      if (!evidence.includes("2026-08-11") || !evidence.includes("bbbbbbbbbbbb")
          || !evidence.includes("288 / 288") || !evidence.includes("288 日（正式）")) {
        throw new Error(`evidence cutoff/version missing: ${evidence}`);
      }
      const limitations = elements.get("individualProbabilityLimitations").innerHTML;
      if (!limitations.includes("不改变生产评分、排名或操作建议")
          || !limitations.includes("不是该股票单次涨跌结果区间或收益区间")
          || limitations.includes("<script>")) {
        throw new Error(`research boundary was missing or unsafe: ${limitations}`);
      }

      const partialElements = new Map([
        ["individualProbabilityResearch", element("individualProbabilityResearch")],
        ["individualProbabilityCards", element("individualProbabilityCards")],
        ["individualProbabilityEvidence", element("individualProbabilityEvidence")],
      ]);
      partialElements.get("individualProbabilityResearch").dataset.individualProbabilitySurface = "true";
      const partialView = createIndividualProbabilityView({
        getElementById(id) { return partialElements.get(id) || null; },
      });
      if (partialView.available() || partialView.renderUnavailable("fixture") !== false
          || partialView.renderLoading("600519.SH") !== false || partialView.renderReport(validated) !== false) {
        throw new Error("partial probability DOM was treated as an available surface");
      }

      const realLimitations = structuredClone(payload);
      realLimitations.limitations = [
        "qfq_provider_vintage_is_one_attested_snapshot_not_daily_vintages",
        "D_plus_1_open_is_daily_bar_proxy_not_proven_executable_fill",
        "daily_price_limit_fill_and_exit_tradeability_not_modelled",
      ];
      view.renderReport(validateIndividualProbabilityReport(realLimitations, "600519.SH"));
      const translated = elements.get("individualProbabilityLimitations").innerHTML;
      if (!translated.includes("不等于历史现金成交价") || !translated.includes("不能证明实际成交")
          || !translated.includes("涨跌停排队")) {
        throw new Error(`registered limitations were not translated: ${translated}`);
      }

      const realInsufficient = structuredClone(payload);
      realInsufficient.status = "insufficient_data";
      realInsufficient.signal_date = null;
      realInsufficient.evidence.official_pit_session_count = 0;
      realInsufficient.evidence.required_official_pit_session_count = 288;
      realInsufficient.evidence.historical_replay_session_count = 279;
      realInsufficient.evidence.historical_replay_official = false;
      realInsufficient.evidence.selection_qualified = false;
      for (const horizon of realInsufficient.horizons) {
        horizon.status = "insufficient_data";
        horizon.probability = null;
        horizon.confidence_interval = null;
        horizon.gate_reasons = ["official_pit_sessions_below_registered_minimum"];
      }
      view.renderReport(validateIndividualProbabilityReport(realInsufficient, "600519.SH"));
      const realEvidence = elements.get("individualProbabilityEvidence").innerHTML;
      const realCards = elements.get("individualProbabilityCards").innerHTML;
      if (!realEvidence.includes("0 / 288") || !realEvidence.includes("279 日（非正式）")
          || !realEvidence.includes("最新正式 PIT 日") || !realEvidence.includes("—")) {
        throw new Error(`formal and historical evidence were conflated: ${realEvidence}`);
      }
      if (!realCards.includes("非官方历史基准率") || !realCards.includes("非官方历史日期 / 观察")
          || !realCards.includes("正式点时交易日尚未达到注册门槛")) {
        throw new Error(`historical diagnostics or gate reason were ambiguous: ${realCards}`);
      }

      const invalidCases = [
        (value) => { value.schema_version = "individual-upside-probability-v2"; },
        (value) => { value.status = "partial"; },
        (value) => { value.status = "insufficient_data"; },
        (value) => { value.horizons[1].probability = 0.5; },
        (value) => { value.horizons[0].confidence_interval.lower = 0.7; },
        (value) => { value.horizons[0].calibration_metrics.actual_positive_rate_ci_95 = [0.4, 0.6]; },
        (value) => { value.horizons[0].counts.out_of_sample_observation_count = 999999; },
        (value) => { value.horizons[0].gate_reasons = ["selection_gate_failed:positive_oos_brier_skill"]; },
        (value) => { value.target_contract.entry = "same_day_close"; },
        (value) => { value.evidence.selection_qualified = false; },
        (value) => { value.symbol = "600519"; },
        (value) => { value.symbol = "600519.SZ"; },
        (value) => { value.signal_date = null; },
        (value) => { value.evidence.official_pit_session_count = 0; },
        (value) => { value.evidence.official_pit_session_count = 287; },
        (value) => { value.horizons[0].counts.evaluated_fold_count = 0; },
        (value) => { value.horizons[0].counts.out_of_sample_observation_count = 0; },
        (value) => { value.horizons[0].evidence_digest = "not-a-sha"; },
        (value) => { value.signal_date = "2026-02-30"; },
        (value) => { value.generated_at = "2026-08-12T15:14:59+08:00"; },
        (value) => { value.generated_at = "2026-08-12T18:00:00"; },
        (value) => { value.generated_at = "2099-08-12T18:00:00+08:00"; },
      ];
      for (const mutate of invalidCases) {
        const invalid = structuredClone(payload);
        mutate(invalid);
        let rejected = false;
        try { validateIndividualProbabilityReport(invalid, "600519.SH"); } catch { rejected = true; }
        if (!rejected) throw new Error(`invalid payload was accepted: ${JSON.stringify(invalid)}`);
      }
      let wrongExchangeRejected = false;
      try { validateIndividualProbabilityReport(payload, "600519.SZ"); } catch { wrongExchangeRejected = true; }
      if (!wrongExchangeRejected) throw new Error("response with the wrong exchange suffix was accepted");
      validateIndividualProbabilityReport(payload, "600519");

      function element(id) {
        return {
          id, dataset: {}, innerHTML: "", textContent: "", hidden: false,
          setAttribute(name, value) { this[name] = String(value); },
          addEventListener(type, handler) { this.listener = { type, handler }; },
        };
      }

      function probabilityReport(symbol) {
        const counts = {
          observation_count: 180000, eligible_observation_count: 170000,
          independent_session_count: 284, out_of_sample_observation_count: 60000,
          out_of_sample_session_count: 120, evaluated_fold_count: 2,
        };
        const metrics = {
          brier_score: 0.196, reference_brier_score: 0.223, brier_skill_score: 0.12107623318385652,
          ece: 0.034, auc: 0.681, actual_positive_rate: 0.514,
          actual_positive_rate_ci_95: { lower: 0.49, upper: 0.54, level: 0.95 },
          bin_monotonic: true, highest_bin_above_base_rate: true,
          selection_gate_version: "market-scan-probability-selection-gates-v1",
          calibration_bin_count: 5, minimum_calibration_bin_session_count: 20,
          all_folds_positive_brier_skill: true,
        };
        const horizon = (displayDay, status) => ({
          display_day: displayDay, holding_sessions: displayDay - 1, status,
          probability: status === "calibrated_shadow" ? 0.612 : null,
          confidence_interval: status === "calibrated_shadow" ? { lower: 0.56, upper: 0.66, level: 0.95 } : null,
          base_rate: 0.514, counts: { ...counts }, calibration_metrics: { ...metrics },
          training_cutoff: "2026-08-11", model_version: "shadow-up-probability-logit-l2-v2-convergence-required",
          feature_version: "historical-replay-common-ohlcv-v1", evidence_digest: "d".repeat(64),
          gate_reasons: status === "calibrated_shadow" ? [] : [`D+${displayDay}<script>证据门禁未通过`],
        });
        return {
          schema_version: "individual-upside-probability-v1", symbol, signal_date: "2026-08-12",
          generated_at: "2026-08-12T18:00:00+08:00", status: "calibrated_shadow",
          target_contract: {
            version: "individual-upside-net-return-label-v1", signal_cutoff: "completed_session_D_close",
            entry: "D_plus_1_official_daily_open_proxy_no_shift",
            exits: { "D+2": "D_plus_2_close_holding_session_1", "D+3": "D_plus_3_close_holding_session_2", "D+4": "D_plus_4_close_holding_session_3" },
            target: "round_trip_net_return_after_declared_costs_gt_0_daily_bar_proxy", cost_profile: "base-a0441d84df44",
            execution_notional: 100000, feature_version: "historical-replay-common-ohlcv-v1", point_in_time_required: true,
          },
          horizons: [horizon(2, "calibrated_shadow"), horizon(3, "insufficient_data"), horizon(4, "not_generated")],
          evidence: {
            assessment_digest: "b".repeat(64), history_manifest_digest: "c".repeat(64),
            history_database_sha256: "a".repeat(64), official_pit_session_count: 288,
            required_official_pit_session_count: 288, historical_replay_session_count: 288,
            historical_replay_official: true, selection_qualified: true,
          },
          limitations: ["仅作 Shadow 研究<script>"], production_effect: "none",
        };
      }
    '''
    _run_node_script(script)


def test_individual_probability_controller_aborts_stale_requests_and_degrades_locally() -> None:
    script = r'''
      import { createIndividualProbabilityController } from "./static/js/individual-probability-controller.js";

      const calls = [];
      const rendered = [];
      const unavailable = [];
      const deferred = [];
      const view = {
        available: () => true,
        bindRetry(handler) { this.retry = handler; },
        renderLoading(symbol) { rendered.push(`loading:${symbol}`); },
        renderReport(report) { rendered.push(`report:${report.symbol}`); },
        renderUnavailable(reason) { unavailable.push(reason); },
      };
      const fetcher = (url, options) => {
        calls.push({ url, options });
        return new Promise((resolve, reject) => deferred.push({ resolve, reject }));
      };
      const controller = createIndividualProbabilityController({ fetcher, view });
      let current = "600519.SH";
      const first = controller.load({ symbol: current, isCurrent: () => current === "600519.SH" });
      current = "000001.SZ";
      const second = controller.load({ symbol: current, isCurrent: () => current === "000001.SZ" });
      if (!calls[0].options.signal.aborted) throw new Error("stock switch did not abort prior probability request");
      deferred[1].resolve(report("000001.SZ"));
      await second;
      deferred[0].resolve(report("600519.SH"));
      await first;
      if (rendered.filter((item) => item.startsWith("report:")).join(",") !== "report:000001.SZ") {
        throw new Error(`stale response replaced current probability panel: ${rendered}`);
      }
      if (!calls[0].url.includes("/api/stock/upside-probability?symbol=600519.SH")
          || !calls[1].url.includes("symbol=000001.SZ")) {
        throw new Error(`independent endpoint was not symbol scoped: ${calls.map((item) => item.url)}`);
      }

      const third = controller.load({ symbol: "300750.SZ", isCurrent: () => true });
      await controller.load({ symbol: "" });
      if (!calls[2].options.signal.aborted) throw new Error("empty symbol did not abort prior probability request");
      if (await controller.retry() !== false || calls.length !== 3) {
        throw new Error("empty symbol retry revived the previous stock request");
      }
      deferred[2].resolve(report("300750.SZ"));
      await third;
      if (rendered.some((item) => item === "report:300750.SZ")) {
        throw new Error(`empty symbol allowed stale repaint: ${rendered}`);
      }

      const failing = createIndividualProbabilityController({
        view,
        async fetcher() { throw new Error("概率服务暂时离线"); },
      });
      await failing.load({ symbol: "300750.SZ", isCurrent: () => true });
      if (unavailable.length !== 2 || !unavailable[1].includes("概率服务暂时离线")) {
        throw new Error(`endpoint failure did not stay local: ${unavailable}`);
      }

      const contextView = {
        available: () => true, bindRetry() {}, renderLoading() {},
        renderReport(value) { rendered.push(`context:${value.signal_date}`); },
        renderUnavailable(reason) { unavailable.push(reason); },
      };
      const newerEvidence = report("600519.SH");
      newerEvidence.signal_date = "2026-08-12";
      newerEvidence.evidence.official_pit_session_count = 1;
      const contextController = createIndividualProbabilityController({
        view: contextView, async fetcher() { return newerEvidence; },
      });
      if (await contextController.load({ symbol: "600519.SH", signalDate: "2026-08-11", isCurrent: () => true })) {
        throw new Error("probability evidence later than the workbench signal date was rendered");
      }
      if (!unavailable.at(-1).includes("晚于当前工作台信号日")) {
        throw new Error(`workbench/probability signal mismatch was not explained: ${unavailable.at(-1)}`);
      }
      const olderEvidence = structuredClone(newerEvidence);
      olderEvidence.signal_date = "2026-08-10";
      const olderController = createIndividualProbabilityController({
        view: contextView, async fetcher() { return olderEvidence; },
      });
      if (!await olderController.load({ symbol: "600519.SH", signalDate: "2026-08-11", isCurrent: () => true })) {
        throw new Error("older non-calibrated evidence did not remain available as dated diagnostics");
      }

      function report(symbol) {
        const target = {
          version: "individual-upside-net-return-label-v1", signal_cutoff: "completed_session_D_close", entry: "D_plus_1_official_daily_open_proxy_no_shift",
          exits: { "D+2": "D_plus_2_close_holding_session_1", "D+3": "D_plus_3_close_holding_session_2", "D+4": "D_plus_4_close_holding_session_3" },
          target: "round_trip_net_return_after_declared_costs_gt_0_daily_bar_proxy", cost_profile: "base-a0441d84df44", execution_notional: 100000,
          feature_version: "historical-replay-common-ohlcv-v1", point_in_time_required: true,
        };
        const horizon = (day) => ({
          display_day: day, holding_sessions: day - 1, status: "not_generated", probability: null,
          confidence_interval: null, base_rate: null,
          counts: { observation_count: 0, eligible_observation_count: 0, independent_session_count: 0, out_of_sample_observation_count: 0, out_of_sample_session_count: 0, evaluated_fold_count: 0 },
          calibration_metrics: null, training_cutoff: null, model_version: null, feature_version: "historical-replay-common-ohlcv-v1",
          evidence_digest: null, gate_reasons: ["assessment_not_generated"],
        });
        return {
          schema_version: "individual-upside-probability-v1", symbol, signal_date: null,
          generated_at: "2026-08-12T18:00:00+08:00", status: "not_generated", target_contract: target,
          horizons: [horizon(2), horizon(3), horizon(4)],
          evidence: { assessment_digest: null, history_manifest_digest: null, history_database_sha256: null, official_pit_session_count: 0, required_official_pit_session_count: 288, historical_replay_session_count: 0, historical_replay_official: false, selection_qualified: false },
          limitations: ["assessment_not_generated"], production_effect: "none",
        };
      }
    '''
    _run_node_script(script)


def test_individual_probability_surface_is_independent_accessible_and_mobile_safe() -> None:
    html = (ROOT / "static" / "index.html").read_text(encoding="utf-8")
    css = (ROOT / "static" / "css" / "individual-probability.css").read_text(encoding="utf-8")
    app = (ROOT / "static" / "app.js").read_text(encoding="utf-8")
    controller = (ROOT / "static" / "js" / "individual-probability-controller.js").read_text(encoding="utf-8")

    assert 'id="individualProbabilityResearch"' in html
    assert 'data-individual-probability-surface="true"' in html
    assert "趋势分是序数状态分，不是上涨概率" in html
    assert "不参与生产评分、排名或操作建议" in html
    assert "互动研究观点（Shadow）" in html
    assert "不写建议历史、不影响正式建议" in html
    assert html.count('class="individual-probability-card not_generated"') == 3
    assert 'role="status" aria-live="polite"' in html
    assert "@media (max-width: 390px)" in css
    assert "min-height: 44px;" in css
    assert "overflow-wrap: anywhere;" in css
    assert "grid-template-columns: minmax(0, 1fr);" in css
    assert "individualProbabilityController.load" in app
    assert "individualProbabilityController" in app
    assert "/api/stock/upside-probability?symbol=" in controller
    assert "isAbortError(error)" in controller


def _run_node_script(script: str) -> None:
    result = subprocess.run(
        ["node", "--input-type=module", "-e", script],
        cwd=ROOT,
        check=False,
        capture_output=True,
        text=True,
    )
    assert result.returncode == 0, result.stderr or result.stdout
